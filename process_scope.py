"""Identify only bot processes belonging to this exact source directory."""
import ctypes
from pathlib import Path


def split_windows_command_line(command):
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def owns_process(row, base):
    base = Path(base).resolve()
    actual = row.get('ExecutablePath')
    if not actual:
        return False
    executable = Path(actual)
    if not executable.is_absolute():
        return False
    if executable.name.lower() == 'tlbbbotengine.exe':
        return executable.resolve() == (base / 'TLBBBotEngine.exe').resolve()
    if not executable.name.lower().startswith('python'):
        return False
    command = row.get('CommandLine')
    if not isinstance(command, str) or not command:
        return False
    arguments = split_windows_command_line(command)
    for argument in arguments[1:]:
        if argument in ('-u', '-B'):
            continue
        if argument.startswith('-'):
            return False
        script = Path(argument)
        return script.is_absolute() and script.resolve() == (base / 'simple_bot.py').resolve()
    return False
