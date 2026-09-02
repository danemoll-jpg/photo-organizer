"""Native Windows multi-select folder picker.

Wraps the Win32 `IFileOpenDialog` COM interface with
`FOS_PICKFOLDERS | FOS_ALLOWMULTISELECT` so the user can ctrl/shift-click
several sibling folders in one dialog, instead of the old tkinter
`askdirectory()` loop (which only ever allowed one folder per pick).

`IFileOpenDialog` is a plain vtable (non-IDispatch) COM interface, so it
isn't reachable through pywin32's dynamic-dispatch `win32com.client` or
through `win32com.shell.shell` (that module wraps `IShellItem`/
`IShellItemArray`/`IFileOperation` but not `IFileDialog`/`IFileOpenDialog`
in the pywin32 version available for Python 3.14 at the time this was
written). `comtypes` can talk to arbitrary vtable interfaces via ctypes, so
the interfaces are declared by hand below, matching the layout in
`shobjidl_core.h`. Vtable slot order matters: every method between the ones
we actually call must still be declared, in order, even if unused, or every
call after the gap resolves to the wrong C++ vtable slot.

This has been exercised structurally (interface creation, SetOptions/
GetOptions round-trip, SetFolder/GetFolder round-trip) but the interactive
`Show()` dialog itself needs a human to click through — see the module
docstring in `pick_sources.py` / the session summary for what to verify.
"""
from __future__ import annotations

import ctypes
from ctypes import HRESULT, POINTER, byref, c_int, c_void_p, c_wchar_p
from ctypes.wintypes import DWORD, HWND, LPWSTR, UINT
from pathlib import Path

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, IUnknown


class FolderPickerUnavailable(Exception):
    """Raised when the native multi-select dialog can't be used at all
    (COM/interface setup failed) — caller should fall back to another
    picker rather than treat this as "user cancelled"."""


class FolderPickerCancelled(Exception):
    """Raised when the user cancels the dialog — not an error."""


# --- GUIDs (shobjidl_core.h) ---
_CLSID_FileOpenDialog = GUID("{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}")

# --- FILEOPENDIALOGOPTIONS flags we use ---
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_ALLOWMULTISELECT = 0x00000200
_FOS_PATHMUSTEXIST = 0x00000800

# --- SIGDN_FILESYSPATH (SIGDN enum value; large unsigned/negative-signed
# constant, kept as the raw DWORD bit pattern) ---
_SIGDN_FILESYSPATH = 0x80058000

_S_OK = 0
_HRESULT_CANCELLED = -2147023673  # 0x800704C7 as signed 32-bit — user cancelled


# --- Interfaces, declared in the exact vtable order shobjidl_core.h uses.
# Methods we never call are still declared (with best-effort signatures)
# so later methods land on the correct vtable slot. ---

class IShellItem(IUnknown):
    _iid_ = GUID("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")
    _methods_: list = []


class IShellItemArray(IUnknown):
    _iid_ = GUID("{B63EA76D-1F85-456F-A19C-48159EFA858B}")
    _methods_: list = []


class IEnumShellItems(IUnknown):
    _iid_ = GUID("{70629033-E363-4A28-A567-0DB78006E6D7}")
    _methods_: list = []


class IShellItemFilter(IUnknown):
    _iid_ = GUID("{2659B475-EEB8-48B7-8F07-B378810F48CF}")
    _methods_: list = []


class IFileDialogEvents(IUnknown):
    _iid_ = GUID("{973510DB-7D7F-452B-8975-74A85828D354}")
    _methods_: list = []


class IModalWindow(IUnknown):
    _iid_ = GUID("{B4DB1657-70D7-485E-8E3E-6FCB5A5C1802}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Show",
                  (["in"], HWND, "hwndOwner")),
    ]


class IFileDialog(IModalWindow):
    _iid_ = GUID("{42F85136-DB7E-439C-85F1-E4075D135FC8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "SetFileTypes",
                  (["in"], UINT, "cFileTypes"),
                  (["in"], c_void_p, "rgFilterSpec")),
        COMMETHOD([], HRESULT, "SetFileTypeIndex",
                  (["in"], UINT, "iFileType")),
        COMMETHOD([], HRESULT, "GetFileTypeIndex",
                  (["out"], POINTER(UINT), "piFileType")),
        COMMETHOD([], HRESULT, "Advise",
                  (["in"], POINTER(IFileDialogEvents), "pfde"),
                  (["out"], POINTER(DWORD), "pdwCookie")),
        COMMETHOD([], HRESULT, "Unadvise",
                  (["in"], DWORD, "dwCookie")),
        COMMETHOD([], HRESULT, "SetOptions",
                  (["in"], DWORD, "fos")),
        COMMETHOD([], HRESULT, "GetOptions",
                  (["out"], POINTER(DWORD), "pfos")),
        COMMETHOD([], HRESULT, "SetDefaultFolder",
                  (["in"], POINTER(IShellItem), "psi")),
        COMMETHOD([], HRESULT, "SetFolder",
                  (["in"], POINTER(IShellItem), "psi")),
        COMMETHOD([], HRESULT, "GetFolder",
                  (["out"], POINTER(POINTER(IShellItem)), "ppsi")),
        COMMETHOD([], HRESULT, "GetCurrentSelection",
                  (["out"], POINTER(POINTER(IShellItem)), "ppsi")),
        COMMETHOD([], HRESULT, "SetFileName",
                  (["in"], LPWSTR, "pszName")),
        COMMETHOD([], HRESULT, "GetFileName",
                  (["out"], POINTER(LPWSTR), "pszName")),
        COMMETHOD([], HRESULT, "SetTitle",
                  (["in"], LPWSTR, "pszTitle")),
        COMMETHOD([], HRESULT, "SetOkButtonLabel",
                  (["in"], LPWSTR, "pszText")),
        COMMETHOD([], HRESULT, "SetFileNameLabel",
                  (["in"], LPWSTR, "pszLabel")),
        COMMETHOD([], HRESULT, "GetResult",
                  (["out"], POINTER(POINTER(IShellItem)), "ppsi")),
        COMMETHOD([], HRESULT, "AddPlace",
                  (["in"], POINTER(IShellItem), "psi"),
                  (["in"], c_int, "fdap")),
        COMMETHOD([], HRESULT, "SetDefaultExtension",
                  (["in"], LPWSTR, "pszDefaultExtension")),
        COMMETHOD([], HRESULT, "Close",
                  (["in"], HRESULT, "hr")),
        COMMETHOD([], HRESULT, "SetClientGuid",
                  (["in"], POINTER(GUID), "guid")),
        COMMETHOD([], HRESULT, "ClearClientData"),
        COMMETHOD([], HRESULT, "SetFilter",
                  (["in"], POINTER(IShellItemFilter), "pFilter")),
    ]


class IFileOpenDialog(IFileDialog):
    _iid_ = GUID("{D57C7288-D4AD-4768-BE02-9D969532D960}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetResults",
                  (["out"], POINTER(POINTER(IShellItemArray)), "ppenum")),
        COMMETHOD([], HRESULT, "GetSelectedItems",
                  (["out"], POINTER(POINTER(IShellItemArray)), "ppsai")),
    ]


IShellItem._methods_ = [
    COMMETHOD([], HRESULT, "BindToHandler",
              (["in"], POINTER(IUnknown), "pbc"),
              (["in"], POINTER(GUID), "bhid"),
              (["in"], POINTER(GUID), "riid"),
              (["out"], POINTER(c_void_p), "ppv")),
    COMMETHOD([], HRESULT, "GetParent",
              (["out"], POINTER(POINTER(IShellItem)), "ppsi")),
    COMMETHOD([], HRESULT, "GetDisplayName",
              (["in"], DWORD, "sigdnName"),
              (["out"], POINTER(LPWSTR), "ppszName")),
    COMMETHOD([], HRESULT, "GetAttributes",
              (["in"], DWORD, "sfgaoMask"),
              (["out"], POINTER(DWORD), "psfgaoAttribs")),
    COMMETHOD([], HRESULT, "Compare",
              (["in"], POINTER(IShellItem), "psi"),
              (["in"], DWORD, "hint"),
              (["out"], POINTER(c_int), "piOrder")),
]

IShellItemArray._methods_ = [
    COMMETHOD([], HRESULT, "BindToHandler",
              (["in"], POINTER(IUnknown), "pbc"),
              (["in"], POINTER(GUID), "bhid"),
              (["in"], POINTER(GUID), "riid"),
              (["out"], POINTER(c_void_p), "ppvOut")),
    COMMETHOD([], HRESULT, "GetPropertyStore",
              (["in"], DWORD, "flags"),
              (["in"], POINTER(GUID), "riid"),
              (["out"], POINTER(c_void_p), "ppv")),
    COMMETHOD([], HRESULT, "GetPropertyDescriptionList",
              (["in"], POINTER(GUID), "keyType"),
              (["in"], POINTER(GUID), "riid"),
              (["out"], POINTER(c_void_p), "ppv")),
    COMMETHOD([], HRESULT, "GetAttributes",
              (["in"], c_int, "AttribFlags"),
              (["in"], DWORD, "sfgaoMask"),
              (["out"], POINTER(DWORD), "psfgaoAttribs")),
    COMMETHOD([], HRESULT, "GetCount",
              (["out"], POINTER(DWORD), "pdwNumItems")),
    COMMETHOD([], HRESULT, "GetItemAt",
              (["in"], DWORD, "dwIndex"),
              (["out"], POINTER(POINTER(IShellItem)), "ppsi")),
    COMMETHOD([], HRESULT, "EnumItems",
              (["out"], POINTER(POINTER(IEnumShellItems)), "ppenumShellItems")),
]


_shell32 = ctypes.windll.shell32
_SHCreateItemFromParsingName = _shell32.SHCreateItemFromParsingName
_SHCreateItemFromParsingName.argtypes = [c_wchar_p, c_void_p, POINTER(GUID), POINTER(c_void_p)]
_SHCreateItemFromParsingName.restype = ctypes.c_long


def _shell_item_from_path(path: str) -> IShellItem:
    ptr = c_void_p()
    hr = _SHCreateItemFromParsingName(str(path), None, IShellItem._iid_, byref(ptr))
    if hr != _S_OK or not ptr:
        raise OSError(f"SHCreateItemFromParsingName({path!r}) failed: hr=0x{hr & 0xFFFFFFFF:08X}")
    return ctypes.cast(ptr, POINTER(IShellItem))


def pick_folders_native(initial_dir: str | None, title: str) -> list[str]:
    """Opens the native multi-select folder dialog. Returns the list of
    picked folder paths (possibly more than one). Raises
    FolderPickerCancelled if the user cancels, FolderPickerUnavailable if
    the dialog itself couldn't be created/shown (caller should fall back)."""
    comtypes.CoInitialize()
    try:
        try:
            dlg = comtypes.client.CreateObject(_CLSID_FileOpenDialog, interface=IFileOpenDialog)
        except Exception as e:
            raise FolderPickerUnavailable(f"Could not create FileOpenDialog: {e}") from e

        try:
            opts = dlg.GetOptions()
            dlg.SetOptions(opts | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM
                            | _FOS_ALLOWMULTISELECT | _FOS_PATHMUSTEXIST)
            dlg.SetTitle(title)

            if initial_dir and Path(initial_dir).is_dir():
                try:
                    dlg.SetFolder(_shell_item_from_path(initial_dir))
                except OSError:
                    pass  # best-effort — fall back to Explorer's own remembered location

            dlg.Show(None)
        except comtypes.COMError as e:
            if e.hresult == _HRESULT_CANCELLED:
                raise FolderPickerCancelled() from None
            raise FolderPickerUnavailable(f"Dialog Show() failed: {e}") from e
        except Exception as e:
            raise FolderPickerUnavailable(f"Dialog setup failed: {e}") from e

        try:
            results = dlg.GetResults()
            count = results.GetCount()
            paths: list[str] = []
            for i in range(count):
                item = results.GetItemAt(i)
                # comtypes marshals the [out] LPWSTR into a Python str for us;
                # the underlying CoTaskMemAlloc'd buffer is a small, one-time
                # per-picked-folder leak we accept rather than freeing a
                # pointer comtypes no longer hands back after conversion.
                name = item.GetDisplayName(_SIGDN_FILESYSPATH)
                if name:
                    paths.append(name)
            return paths
        except Exception as e:
            raise FolderPickerUnavailable(f"Reading dialog results failed: {e}") from e
    finally:
        comtypes.CoUninitialize()
