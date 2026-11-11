"""
A log file that several processes can write at once without losing lines.

Python's 'a' mode on Windows moves to the end of the file and then writes, in two steps.
When two IIS processes write at the same moment, one line can land on top of the other.
Measured: up to 72 of 600 lines lost on a local disk, and up to 18 on the shared drive.
A handle opened with FILE_APPEND_DATA and no FILE_WRITE_DATA makes Windows put each
write at the current end of the file in one step, and over SMB the file server does it.
"""
import logging
import os
import sys

FILE_APPEND_DATA = 0x0004
FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004  # read, write, delete
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80


def open_for_append(path, encoding='utf-8', errors=None):
    """Text file whose every write goes to the end of the file, whoever else is writing."""
    if sys.platform != 'win32':
        return open(path, 'a', encoding=encoding, errors=errors)  # O_APPEND is atomic here

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE

    handle = create_file(os.path.abspath(path), FILE_APPEND_DATA, FILE_SHARE_ALL, None,
                         OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return open(descriptor, 'a', encoding=encoding, errors=errors)


class AppendFileHandler(logging.FileHandler):
    """FileHandler whose writes are true appends, so IIS processes can share one file."""

    def _open(self):
        return open_for_append(self.baseFilename, encoding=self.encoding, errors=self.errors)
