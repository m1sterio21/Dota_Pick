"""Глобальная горячая клавиша через WinAPI RegisterHotKey (обычный механизм Windows,
им же пользуются Discord/OBS; с игрой никак не взаимодействует)."""
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000

KEYS = {
    "scroll_lock": ("Scroll Lock", 0x91),
    "pause": ("Pause/Break", 0x13),
    "insert": ("Insert", 0x2D),
}


class GlobalHotkey:
    def __init__(self, key, callback):
        self.label, self.vk = KEYS[key]
        self.callback = callback
        self.error = None
        self._thread_id = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(2)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, self.vk):
            self.error = f"Клавиша {self.label} уже занята другой программой"
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._thread_id and not self.error:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
