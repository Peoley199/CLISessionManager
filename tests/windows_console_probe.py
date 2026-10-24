"""Read and drive only a test-owned Windows console, using the standard library."""

import ctypes as C
from ctypes import wintypes as W
import time


class Coord(C.Structure):
    _fields_ = [("x", W.SHORT), ("y", W.SHORT)]


class Rect(C.Structure):
    _fields_ = [("left", W.SHORT), ("top", W.SHORT), ("right", W.SHORT), ("bottom", W.SHORT)]


class Info(C.Structure):
    _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", W.WORD),
                ("window", Rect), ("maximum", Coord)]


class Key(C.Structure):
    _fields_ = [("down", W.BOOL), ("repeat", W.WORD), ("vk", W.WORD),
                ("scan", W.WORD), ("char", W.WCHAR), ("control", W.DWORD)]


class Event(C.Union):
    _fields_ = [("key", Key), ("padding", C.c_byte * 16)]


class Record(C.Structure):
    _fields_ = [("type", W.WORD), ("event", Event)]


k = C.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
k.CreateFileW.restype = W.HANDLE
k.CloseHandle.argtypes = [W.HANDLE]
k.GetConsoleScreenBufferInfo.argtypes = [W.HANDLE, C.POINTER(Info)]
k.ReadConsoleOutputCharacterW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, Coord, C.POINTER(W.DWORD)]
k.WriteConsoleInputW.argtypes = [W.HANDLE, C.POINTER(Record), W.DWORD, C.POINTER(W.DWORD)]


class Console:
    def __init__(self, pid):
        k.FreeConsole()
        if not k.AttachConsole(pid):
            raise C.WinError(C.get_last_error())

    def handle(self, name):
        handle = k.CreateFileW(name, 0xC0000000, 3, None, 3, 0, None)
        if handle == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        return handle

    def read(self):
        handle = self.handle("CONOUT$")
        try:
            info = Info()
            if not k.GetConsoleScreenBufferInfo(handle, C.byref(info)):
                raise C.WinError(C.get_last_error())
            bottom = min(info.size.y, max(info.cursor.y, info.window.bottom) + 1)
            top = max(0, bottom - 160)
            count = info.size.x * (bottom - top)
            buffer = C.create_unicode_buffer(count + 1)
            read = W.DWORD()
            if not k.ReadConsoleOutputCharacterW(handle, buffer, count, Coord(0, top), C.byref(read)):
                raise C.WinError(C.get_last_error())
            return "\n".join(buffer.value[i:i + info.size.x].rstrip()
                             for i in range(0, read.value, info.size.x)).rstrip()
        finally:
            k.CloseHandle(handle)

    def send(self, text):
        handle = self.handle("CONIN$")
        try:
            for char in text:
                record = Record()
                record.type = 1
                record.event.key = Key(True, 1, 13 if char == "\r" else 0, 0, char, 0)
                written = W.DWORD()
                if not k.WriteConsoleInputW(handle, C.byref(record), 1, C.byref(written)):
                    raise C.WinError(C.get_last_error())
        finally:
            k.CloseHandle(handle)

    def close(self):
        k.FreeConsole()
