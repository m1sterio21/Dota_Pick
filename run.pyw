import importlib.util
import subprocess
import sys
import traceback
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # запущено как собранный DotaPick.exe
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# модуль -> пакет pip
REQUIRED = {"mss": "mss", "cv2": "opencv-python", "numpy": "numpy", "requests": "requests", "PIL": "pillow"}


def ensure_dependencies():
    """На новом компьютере библиотек может не быть — ставим их сами при первом запуске."""
    if FROZEN:
        return  # в .exe все библиотеки уже внутри
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    import tkinter as tk
    from tkinter import messagebox

    win = tk.Tk()
    win.title("Dota Pick")
    tk.Label(win, text="Первый запуск: устанавливаю библиотеки\n" + ", ".join(missing) + "\n\nЭто один раз, 1–3 минуты…",
             padx=24, pady=18, font=("Segoe UI", 10)).pack()
    win.update()
    exe = Path(sys.executable)
    python = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    result = subprocess.run([str(python), "-m", "pip", "install", *missing],
                            capture_output=True, text=True, creationflags=0x08000000)  # без окна консоли
    win.destroy()
    if result.returncode != 0:
        (ROOT / "error.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        messagebox.showerror("Dota Pick", "Не получилось установить библиотеки.\n"
                                          "Подробности в error.log. Проверь интернет и что Python установлен "
                                          "с галочкой «Add Python to PATH».")
        sys.exit(1)


try:
    ensure_dependencies()
    from dotapick.app import main
    main()
except SystemExit:
    raise
except Exception:
    (ROOT / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    raise
