import json
import sys
from pathlib import Path

# В собранном .exe (PyInstaller) настройки и кэш лежат рядом с DotaPick.exe, а не во временной папке.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
SCREENS_DIR = ROOT / "screens"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "account_id": "",
    "stratz_token": "",
    "position": 1,
    "side": "radiant",
    "hotkey": "scroll_lock",
    # 0 = авто: ищем драфт на всех мониторах; 1..N = только этот монитор
    "monitor": 0,
    "last_monitor": None,  # где драфт нашёлся в прошлый раз — в авто-режиме проверяем его первым
    "always_on_top": True,
    # True — окно всегда невидимо для скриншотов/Discord/OBS. False — видно, а прячется
    # только на долю секунды во время своего скана, чтобы не закрыть драфт на снимке.
    "hide_from_capture": False,
    "opacity": 1.0,
    # сворачивать окно при клике мышью вне программы (по Scroll Lock окно появляется снова);
    # по умолчанию включено только в собранном DotaPick.exe
    "minimize_on_outside_click": bool(getattr(sys, "frozen", False)),
    "window_pos": None,
    "window_size": None,  # [ширина, высота таблицы] — меняется уголком справа снизу
    "show_settings": True,
    # режим (какие части формулы включены), фильтры популярности и опыта
    "mode": "personal",
    "pick_rate": "any",
    "experience": "all",
    # контрпик и синергия — суммы по 5 парам, а попарные эффекты частично перекрываются,
    # поэтому их вес меньше единицы
    "weights": {"meta": 1.0, "counter": 0.6, "synergy": 0.5, "comfort": 1.0},
    # Геометрия задаётся в долях высоты экрана: интерфейс Доты масштабируется по высоте
    # и центрируется по горизонтали, поэтому одни числа работают и на 16:9, и на 21:9.
    "recognition": {
        "strip_height": 0.13,
        "half_width": 0.95,
        "portrait_height": 0.037,
        "portrait_aspect": 1.7778,
        # 1.65 — масштаб верхней панели реального рейтингового драфта
        "scales": [0.8, 1.0, 1.2, 1.4, 1.55, 1.65, 1.8],
        "threshold": 0.55,
        "relative_threshold": 0.78,
        # портрет темнее эталона в столько раз = наведён, но не выбран
        "min_brightness_ratio": 0.75,
        "learned_scale": None,
    },
}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            _merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
