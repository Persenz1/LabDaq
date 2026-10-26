"""Bundled resources and per-user writable locations for the installed app."""
import os
from pathlib import Path
import sys


def _documents_directory():
    """Resolve the current Windows user's Documents location, including redirection."""
    import ctypes
    from ctypes import wintypes
    from uuid import UUID

    folder_id = (ctypes.c_byte * 16).from_buffer_copy(
        UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
    shell = ctypes.WinDLL("shell32")
    shell.SHGetKnownFolderPath.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(wintypes.LPWSTR))
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole = ctypes.WinDLL("ole32")
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole.CoTaskMemFree.restype = None
    location = wintypes.LPWSTR()
    try:
        # KF_FLAG_DONT_VERIFY: obtain the path without creating or checking the folder.
        result = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0x4000, None,
                                            ctypes.byref(location))
        if result < 0:
            raise OSError(f"无法读取 Windows 文档目录（HRESULT 0x{result & 0xffffffff:08X}）")
        return Path(location.value)
    finally:
        ole.CoTaskMemFree(location)


ROOT = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "LabDAQ" if FROZEN else ROOT
RUNTIME_DIR = DATA_DIR / ".runtime"
RECORDING_ROOT = _documents_directory() / "LabDAQ" / "recordings" if FROZEN else ROOT / "recordings"
