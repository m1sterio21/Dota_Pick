import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from PIL import Image, ImageTk

from . import builds, scorer
from .config import SCREENS_DIR, load_config, save_config
from .data import DataStore, parse_account_id
from .hotkey import KEYS, GlobalHotkey
from .recognize import UNKNOWN, Recognizer, draw_debug, grab_monitors, grab_screen, write_image

C = {
    "border": "#243041", "bg": "#0e131a", "panel": "#141b24", "sel": "#22303f", "hover": "#18212c",
    "line": "#1c2633", "fg": "#e6edf3", "muted": "#7d8a99", "dim": "#4b5767",
    "cyan": "#38d0e0", "red": "#ff5a64", "accent": "#2fc3d8", "live": "#ff4d5a", "warn": "#e0a040",
}
FONT = "Segoe UI"
W = 600  # ширина окна
WDA_EXCLUDEFROMCAPTURE = 0x11

POSITIONS = [(p, str(p)) for p in range(1, 6)]
SIDES = [("radiant", "Radiant"), ("dire", "Dire")]
MODES = [("meta", "Мета"), ("matchup", "Матчап"), ("hybrid", "Гибрид"), ("personal", "Личный")]
# какие части формулы включает режим: мета, контрпик, синергия, комфорт
MODE_MULT = {"meta": (1, 0, 0, 0), "matchup": (0.3, 1, 1, 0), "hybrid": (1, 1, 1, 0), "personal": (1, 1, 1, 1)}
PICK_RATES = [("any", "Любой"), ("average", "Средний"), ("high", "Высокий")]
PICK_RATE_SHARE = {"any": 0.0, "average": 0.05, "high": 0.15}
EXPERIENCE = [("all", "Все"), ("familiar", "Знакомые"), ("mains", "Мейны")]
MIN_GAMES = {"all": 0, "familiar": 5, "mains": 30}
RANKS = ["?", "Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine", "Immortal"]

ROWS = 12
ROW_H = 30
HEAD_H = 22
# заголовки таблицы: (текст, x, привязка)
HEADERS = [("ГЕРОЙ", 10, "w"), ("МАТЧЕЙ", 236, "e"), ("TIER & RATING", 248, "w"), ("WR", 372, "e"),
           ("СИН", 414, "e"), ("КОНТР", 462, "e"), ("М", 498, "e"), ("ИГРОК", 546, "e"), ("ИТОГ", 592, "e")]
# сортировка по клику на заголовок: (ключ, левая и правая граница зоны клика по x)
SORT_ZONES = [("name", 0, 180), ("matches", 180, 242), ("rating", 242, 322), ("wr", 322, 376),
              ("synergy", 376, 418), ("counter", 418, 466), ("games", 466, 502), ("pwr", 502, 550),
              ("total", 550, 600)]
SORT_VALUE = {
    "matches": lambda r: r.matches or None,
    "rating": lambda r: r.rating,
    "wr": lambda r: r.wr7k,
    "synergy": lambda r: r.synergy,
    "counter": lambda r: r.counter,
    "games": lambda r: r.games or None,
    # винрейт игрока считаем только от 5 игр, иначе наверх вылезут герои с 1 победой из 1
    "pwr": lambda r: r.wins / r.games if r.games >= 5 else None,
}
# буквы тиров как на Dota2ProTracker: (минимальный рейтинг, буква, цвет)
TIERS = [(80, "S", "#ff7a85"), (62, "A", "#ffbf86"), (54, "B", "#f8ef86"), (45, "C", "#c6f59e"),
         (35, "D", "#8fe3cf"), (0, "F", "#8fb4ff")]
D2_GREEN, D2_RED, D2_YELLOW = "#3fe05a", "#ff4b55", "#f5c518"
PILL_BG, PILL_BORDER, PILL_NUM, PILL_SUB = "#1a1928", "#3a3352", "#c7a6ff", "#7c7396"


def _signed_color(v, eps=0.3):
    if v is None or abs(v) < eps:
        return C["muted"]
    return C["cyan"] if v > 0 else C["red"]


def _tier(rating):
    return next((letter, color) for floor, letter, color in TIERS if rating >= floor)


def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


class Segmented(tk.Frame):
    """Переключатель-«таблетка» как в оригинальном оверлее."""

    def __init__(self, master, options, value, command):
        super().__init__(master, bg=C["border"], padx=1, pady=1)
        self.value, self.command, self.labels = value, command, {}
        for key, text in options:
            lbl = tk.Label(self, text=text, font=(FONT, 8), padx=9, pady=2, cursor="hand2")
            lbl.pack(side="left", fill="both", expand=True, padx=(0, 1))
            lbl.bind("<Button-1>", lambda e, k=key: self.set(k, notify=True))
            self.labels[key] = lbl
        self._paint()

    def set(self, key, notify=False):
        changed = key != self.value
        self.value = key
        self._paint()
        if notify and changed:
            self.command(key)

    def _paint(self):
        for key, lbl in self.labels.items():
            sel = key == self.value
            lbl.config(bg=C["sel"] if sel else C["panel"], fg=C["fg"] if sel else C["muted"])


class HeroPicker(tk.Toplevel):
    """Всплывающий поиск героя для слота драфта или бана."""

    def __init__(self, app, x, y, on_pick, allow_clear=True):
        super().__init__(app.root)
        self.app, self.on_pick, self.items = app, on_pick, []
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=C["border"])
        inner = tk.Frame(self, bg=C["panel"], padx=6, pady=6)
        inner.pack(padx=1, pady=1)
        self.var = tk.StringVar()
        self.entry = tk.Entry(inner, textvariable=self.var, width=26, bg=C["bg"], fg=C["fg"],
                              insertbackground=C["fg"], relief="flat", font=(FONT, 9))
        self.entry.pack(fill="x", ipady=3)
        self.lb = tk.Listbox(inner, height=8, bg=C["bg"], fg=C["fg"], selectbackground=C["sel"],
                             selectforeground=C["fg"], relief="flat", highlightthickness=0,
                             font=(FONT, 9), activestyle="none")
        self.lb.pack(fill="x", pady=(4, 0))
        if allow_clear:
            clr = tk.Label(inner, text="✕  очистить слот", bg=C["panel"], fg=C["muted"],
                           font=(FONT, 8), cursor="hand2")
            clr.pack(anchor="w", pady=(4, 0))
            clr.bind("<Button-1>", lambda e: self._choose(None))
        self.var.trace_add("write", lambda *a: self._refresh())
        self.entry.bind("<Return>", lambda e: self._choose_selected())
        self.entry.bind("<Down>", lambda e: self._move(1))
        self.entry.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Escape>", lambda e: self.destroy())
        self.lb.bind("<ButtonRelease-1>", lambda e: self._choose_selected())
        self.bind("<FocusOut>", lambda e: self.after(150, self._check_focus))
        self._refresh()
        self.geometry(f"+{x}+{y}")
        self.after(20, self._grab_focus)

    def _grab_focus(self):
        self.focus_force()
        self.entry.focus_set()

    def _check_focus(self):
        try:
            f = self.focus_get()
            if f is None or not str(f).startswith(str(self)):
                self.destroy()
        except (KeyError, tk.TclError):
            pass

    def _refresh(self):
        text = self.var.get().strip().lower()
        names = self.app.hero_names
        if text:
            starts = [n for n in names if n.lower().startswith(text)]
            names = starts + [n for n in names if text in n.lower() and n not in starts]
        self.items = names
        self.lb.delete(0, "end")
        for n in names:
            self.lb.insert("end", n)
        if names:
            self.lb.selection_set(0)

    def _move(self, d):
        if not self.items:
            return
        cur = self.lb.curselection()
        i = max(0, min(len(self.items) - 1, (cur[0] if cur else 0) + d))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        self.lb.see(i)

    def _choose_selected(self):
        cur = self.lb.curselection()
        if self.items:
            self._choose(self.app.resolve_hero(self.items[cur[0] if cur else 0]))

    def _choose(self, hid):
        self.destroy()
        self.on_pick(hid)


class BuildWindow(tk.Toplevel):
    """Окно «Сборка»: стартовый закуп, ранние предметы, самая популярная сборка, предметы дальше
    по игре (всё — Dota2ProTracker, пабы 7000+) и предметы против текущего вражеского пика."""

    ICON_W, ICON_H = 42, 31

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=C["border"])
        self._icons = {}
        self._drag = None
        self._request = 0
        frame = tk.Frame(self, bg=C["bg"])
        frame.pack(padx=1, pady=1, fill="both", expand=True)
        head = tk.Frame(frame, bg=C["panel"])
        head.pack(fill="x")
        self.hero_img = tk.Label(head, bg=C["panel"])
        self.hero_img.pack(side="left", padx=(10, 6), pady=6)
        self.title_lbl = tk.Label(head, text="", bg=C["panel"], fg=C["accent"], font=(FONT, 11, "bold"))
        self.title_lbl.pack(side="left")
        close = app._icon_button(head, "✕", self.withdraw)
        close.pack(side="right", padx=(0, 4))
        for w in (head, self.title_lbl, self.hero_img):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self.sub_lbl = tk.Label(frame, text="", bg=C["bg"], fg=C["muted"], font=(FONT, 8), anchor="w")
        self.sub_lbl.pack(fill="x", padx=10, pady=(6, 0))
        self.body = tk.Frame(frame, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self.info_lbl = tk.Label(frame, text="Наведи на предмет — название, минута и статистика",
                                 bg=C["panel"], fg=C["muted"], font=(FONT, 8), anchor="w", width=1)
        self.info_lbl.pack(fill="x", pady=(8, 0), ipady=4, ipadx=8)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def show(self, hero_id, position, enemies):
        app = self.app
        self._request += 1
        request = self._request
        self.title_lbl.config(text=f"{app.store.name(hero_id).upper()}  ·  поз. {position}")
        self.hero_img.config(image=app._icon(hero_id, 54, 30))
        self.sub_lbl.config(text="Загружаю сборку с Dota2ProTracker…")
        for w in self.body.winfo_children():
            w.destroy()
        if not self.winfo_viewable():
            app.root.update_idletasks()
            x = app.root.winfo_rootx() + app.root.winfo_width() + 8
            self.geometry(f"+{x}+{app.root.winfo_rooty()}")
            self.deiconify()
        self.lift()
        app._exclude_from_capture(self)

        def work():
            try:
                items = app.store.ensure_items()
                build = app.store.hero_build(hero_id, position)
                if build:
                    ids = [r["item"] for r in build["early"] + build["late"]] + build["core"]["items"]
                    ids += [i for variant, _ in build["starting"] for i in variant]
                    ids += [i for inv in (build.get("lategame") or {}).values() for i in inv]
                    app.store.download_item_icons(ids)
                    extra = builds.situational(app.store, build, items, enemies, position)
                    app.store.download_item_icons([s["item"] for s in extra])
                else:
                    extra = []
                app.post(self._render, request, hero_id, position, build, items, extra)
            except Exception as e:
                app.post(self.sub_lbl.config, {"text": f"Не удалось загрузить сборку: {e}"})

        threading.Thread(target=work, daemon=True).start()

    # --- отрисовка (в стиле карточек Dota2ProTracker) ---

    CARD = "#111821"

    def _card(self, title, desc=None):
        outer = tk.Frame(self.body, bg=C["border"])
        outer.pack(fill="x", pady=(8, 0))
        card = tk.Frame(outer, bg=self.CARD)
        card.pack(fill="x", padx=1, pady=1)
        tk.Label(card, text=title, bg=self.CARD, fg=C["fg"], font=(FONT, 10, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(8, 0))
        if desc:
            tk.Label(card, text=desc, bg=self.CARD, fg=C["muted"], font=(FONT, 8), anchor="w", justify="left",
                     wraplength=520).pack(fill="x", padx=12)
        tk.Frame(card, bg=C["border"], height=1).pack(fill="x", pady=(8, 0))
        body = tk.Frame(card, bg=self.CARD)
        body.pack(fill="x", padx=12, pady=10)
        return body

    def _columns(self, parent, n):
        """n равных колонок с вертикальными разделителями между ними."""
        cols = []
        for k in range(n):
            if k:
                tk.Frame(parent, bg=C["border"], width=1).grid(row=0, column=2 * k - 1, sticky="ns", padx=8)
            col = tk.Frame(parent, bg=self.CARD)
            col.grid(row=0, column=2 * k, sticky="n")
            parent.columnconfigure(2 * k, weight=1)
            cols.append(col)
        return cols

    def _grid(self, parent, item_ids, per_row=3, stack=False):
        """Предметы сеткой по per_row в ряд; stack — одинаковые схлопнуть в один с числом в углу."""
        if stack:
            counts = {}
            for i in item_ids:
                counts[i] = counts.get(i, 0) + 1
            entries = list(counts.items())
        else:
            entries = [(i, 1) for i in item_ids]
        grid = tk.Frame(parent, bg=self.CARD)
        grid.pack(pady=(6, 0))
        for k, (item_id, n) in enumerate(entries):
            self._item(grid, item_id, n).grid(row=k // per_row, column=k % per_row, padx=3, pady=3)
        return grid

    def _icon(self, item_id):
        if item_id not in self._icons:
            path = self.app.store.item_icon_path(item_id)
            try:
                img = Image.open(path).convert("RGB").resize((self.ICON_W, self.ICON_H), Image.LANCZOS)
                self._icons[item_id] = ImageTk.PhotoImage(img)
            except (OSError, TypeError, AttributeError):
                self._icons[item_id] = None
        return self._icons[item_id]

    def _item(self, parent, item_id, count=1, stats=None):
        """Иконка предмета в рамке; число в углу — сколько штук; при наведении — подробности внизу окна."""
        it = self.items.get(item_id, {})
        w, h = self.ICON_W, self.ICON_H
        cv = tk.Canvas(parent, width=w + 2, height=h + 2, bg=C["border"], highlightthickness=0)
        img = self._icon(item_id)
        if img:
            cv.create_image(1, 1, anchor="nw", image=img)
        else:
            cv.create_text(w / 2 + 1, h / 2 + 1, text=it.get("dname", "?")[:6], fill=C["fg"], font=(FONT, 6))
        if count > 1:
            cv.create_text(w, h + 1, text=str(count), anchor="se", fill="#000000", font=(FONT, 9, "bold"))
            cv.create_text(w - 1, h, text=str(count), anchor="se", fill="#ffffff", font=(FONT, 9, "bold"))
        text = (f"{it.get('dname', item_id)}" + (f" ×{count}" if count > 1 else "")
                + f" · {it.get('cost', 0)} золота" + (f" · {stats}" if stats else ""))
        cv.bind("<Enter>", lambda e: self.info_lbl.config(text=text, fg=C["fg"]))
        return cv

    def _render(self, request, hero_id, position, build, items, extra):
        if request != self._request:
            return  # пока грузили, открыли сборку другого героя
        self.items = items
        for w in self.body.winfo_children():
            w.destroy()
        if not build or not build["matches"]:
            self.sub_lbl.config(text="На протрекере нет сборки для этого героя на этой позиции")
            return
        wr = build["wins"] / build["matches"] * 100
        self.sub_lbl.config(text=f"Dota2ProTracker · пабы 7000+ · {build['matches']} матчей · WR {wr:.1f}%")
        card = self.CARD

        def lbl(parent, text, fg, size=8, bold=False, **kw):
            return tk.Label(parent, text=text, bg=card, fg=fg, font=(FONT, size, "bold") if bold else (FONT, size), **kw)

        def stats(r):
            return (f"~{r['minute']:.0f} мин · покупают в {r['pr'] * 100:.0f}% матчей"
                    + (f" · WR {r['win_rate'] * 100:.1f}%" if r.get("win_rate") else ""))

        # все предметы с временем покупки, без повторов
        timed = {}
        for r in build["early"] + build["late"]:
            if r["minute"] is not None and (r["item"] not in timed or r["pr"] > timed[r["item"]]["pr"]):
                timed[r["item"]] = r
        timed = sorted(timed.values(), key=lambda r: r["minute"])

        # 1. Стартовые предметы — три варианта рядом
        body = self._card("Стартовые предметы", "Самые частые стартовые наборы предметов")
        variants = build["starting"][:3]
        if not variants:
            lbl(body, "Недостаточно данных.", C["muted"]).pack(anchor="w")
        for col, (variant, st) in zip(self._columns(body, len(variants)), variants):
            vwr = (st.get("win_rate") or 0) * 100
            head = tk.Frame(col, bg=card)
            head.pack()
            lbl(head, f"Матчей: {st.get('count', 0)}", C["fg"]).pack(side="left")
            lbl(head, f" · {vwr:.1f}%", D2_GREEN if vwr >= 50 else D2_RED, bold=True).pack(side="left")
            lbl(head, " Винрейт", C["muted"]).pack(side="left")
            lane = st.get("lane_advantage")
            if lane is not None:
                color = D2_GREEN if lane >= 3 else "#f0a030" if lane >= 0 else D2_RED
                lbl(col, f"{lane:+.1f}% линия", color, bold=True).pack()
            self._grid(col, variant, 3)  # каждый предмет отдельной иконкой: 2 ветки = 2 иконки

        # 2. Основные предметы — покупаются больше чем в половине матчей, по порядку
        core = [r for r in timed if r["pr"] >= 0.5][:11]
        if core:
            body = self._card("Основные предметы",
                              "Покупаются более чем в половине матчей этого билда, в порядке приобретения")
            row = tk.Frame(body, bg=card)
            row.pack(anchor="w")
            for k, r in enumerate(core):
                cell = tk.Frame(row, bg=card)
                cell.grid(row=0, column=k, padx=2, sticky="n")
                lbl(cell, f"{r['minute']:.0f}m", C["muted"]).pack()
                self._item(cell, r["item"], stats=stats(r)).pack()
                if r["pr"] >= 0.75:
                    tk.Label(cell, text="ОСН.", bg="#0f2a33", fg=C["accent"], font=(FONT, 6, "bold"),
                             padx=4).pack(pady=(3, 0))

        # 3. Поздняя игра — самые частые инвентари в долгих играх
        late = {k: v for k, v in (build.get("lategame") or {}).items() if v}
        if late:
            body = self._card("Поздняя игра", "Самые частые наборы предметов в долгих играх")
            for col, (key, inv) in zip(self._columns(body, len(late)), late.items()):
                lbl(col, f"МИНУТА {key}", C["dim"], bold=True).pack(anchor="w")
                self._grid(col, inv, 3)

        # 4. Ситуативные предметы — под этот вражеский пик
        body = self._card("Ситуативные предметы под этот пик",
                          "Из того, что на этом герое собирают на 7000+, — предметы против угроз вражеского драфта")
        if extra:
            for s in extra:
                line = tk.Frame(body, bg=card)
                line.pack(fill="x", pady=2)
                self._item(line, s["item"], stats=s["reason"]).pack(side="left")
                text_col = tk.Frame(line, bg=card)
                text_col.pack(side="left", fill="x", padx=(8, 0))
                lbl(text_col, self.items.get(s["item"], {}).get("dname", "?"), C["fg"], 9, True, anchor="w").pack(fill="x")
                lbl(text_col, s["reason"], C["muted"], anchor="w", justify="left", wraplength=440).pack(fill="x")
        else:
            lbl(body, "Врагов ещё нет или особых угроз не видно — бери основные предметы.", C["muted"]).pack(anchor="w")
        situational = [r for r in timed if 0.08 <= r["pr"] < 0.5][:10]
        if situational:
            lbl(body, "Ещё ситуативно собирают на этом герое:", C["dim"], 7, True).pack(anchor="w", pady=(10, 0))
            row = tk.Frame(body, bg=card)
            row.pack(anchor="w")
            for k, r in enumerate(situational):
                cell = tk.Frame(row, bg=card)
                cell.grid(row=0, column=k, padx=2)
                self._item(cell, r["item"], stats=stats(r)).pack()
                lbl(cell, f"{r['pr'] * 100:.0f}%", C["muted"], 7).pack()
        self.info_lbl.config(text="Наведи на предмет — название, минута и статистика", fg=C["muted"])
        self._fit_to_screen()

    def _fit_to_screen(self):
        """Окно высокое — если низ уезжает за экран, поднимаем его."""
        try:
            self.update_idletasks()
            wa = self.app._work_area(self)
        except Exception:
            return
        y, h = self.winfo_y(), self.winfo_height()
        if y + h > wa.b:
            self.geometry(f"+{self.winfo_x()}+{max(wa.t, wa.b - h)}")


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.store = DataStore()
        self.recognizer = None
        self.hotkey = None
        self.hero_names = []
        self.radiant = [None] * 5
        self.dire = [None] * 5
        self.banned = []
        self.manual_ids = set()  # герои, вписанные руками: повторный скан их не стирает
        self.recs = []          # строки таблицы в показанном порядке
        self._base_recs = []    # те же строки в обычном порядке (по итогу)
        self.sort_key = None    # по какой колонке отсортировано (None — по итогу)
        self.selected = 0
        self.hover_row = None
        self._events = queue.Queue()
        self._loading = False
        self._recognizing = False
        self._icons = {}
        self._drag = None
        self._resize_start = None
        size = self.cfg.get("window_size") or [W, ROWS * ROW_H]
        if size and not self.cfg.get("details_tooltip"):
            # блок пояснений под таблицей заменили подсказкой — отдаём его место таблице
            size = [size[0], size[1] + 105]
            self.cfg["details_tooltip"] = True
            self.cfg["window_size"] = size
            save_config(self.cfg)
        self.w, self.table_h = self._clamp_size(*size)

        self._build_ui()
        self._install_clipboard_fixes()
        self.root.after(50, self._poll)
        self.root.after(100, self._apply_window_flags)
        self.root.after(400, self._fit_to_screen)
        self._mouse_down = False
        self.root.after(500, self._watch_outside_click)
        self._start_hotkey()
        self.load_data()

    # ---------- интерфейс ----------

    def _build_ui(self):
        r = self.root
        r.title("Dota Pick")
        r.configure(bg=C["border"])
        r.overrideredirect(True)
        pos = self.cfg.get("window_pos")
        r.geometry(f"+{pos[0]}+{pos[1]}" if pos else "+200+150")
        self.frame = tk.Frame(r, bg=C["bg"])
        self.frame.pack(padx=1, pady=1, fill="both", expand=True)
        self.name_font = tkfont.Font(family=FONT, size=9)
        self.badge_font = tkfont.Font(family=FONT, size=7, weight="bold")

        self._build_header()
        self._build_draft()
        self._build_controls()
        self._build_settings_panel()
        self._build_table()
        self._build_footer()
        if not self.cfg.get("show_settings", True):
            self.settings_panel.pack_forget()

    def _icon_button(self, parent, text, command, size=10):
        b = tk.Label(parent, text=text, bg=parent["bg"], fg=C["muted"], font=(FONT, size), padx=6, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.config(fg=C["fg"]))
        b.bind("<Leave>", lambda e: b.config(fg=C["muted"]))
        return b

    def _build_header(self):
        h = tk.Frame(self.frame, bg=C["panel"])
        h.pack(fill="x")
        title = tk.Label(h, text="DOTA PICK", bg=C["panel"], fg=C["accent"], font=(FONT, 11, "bold"))
        title.pack(side="left", padx=(10, 8), pady=5)
        self.live_lbl = tk.Label(h, text="● LIVE DRAFT", bg="#3a141b", fg=C["live"], font=(FONT, 7, "bold"), padx=6)
        self.live_lbl.pack(side="left")
        self._icon_button(h, "✕", self._on_close).pack(side="right", padx=(0, 4))
        self._icon_button(h, "—", self.minimize, 9).pack(side="right")
        self._icon_button(h, "⚙", self.toggle_settings, 11).pack(side="right")
        for w in (h, title):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

    def _build_draft(self):
        self.draft_cv = tk.Canvas(self.frame, width=self.w, height=54, bg=C["bg"], highlightthickness=0)
        self.draft_cv.pack(fill="x", pady=(8, 0))
        self.draft_cv.bind("<Button-1>", self._draft_click)
        self.draft_cv.bind("<Button-3>", self._draft_right_click)
        self.draft_cv.bind("<Motion>", self._draft_motion)
        self.draft_cv.bind("<Leave>", lambda e: self._show_tip(""))

        bans = tk.Frame(self.frame, bg=C["bg"])
        bans.pack(fill="x", padx=10, pady=(2, 0))
        tk.Label(bans, text="БАНЫ", bg=C["bg"], fg=C["dim"], font=(FONT, 7, "bold")).pack(side="left")
        self.bans_cv = tk.Canvas(bans, width=self.w - 120, height=18, bg=C["bg"], highlightthickness=0)
        self.bans_cv.pack(side="left", padx=6)
        self.bans_cv.bind("<Button-1>", self._ban_click)
        add = self._icon_button(bans, "+ бан", self._add_ban_dialog, 8)
        add.config(bg=C["bg"])
        add.pack(side="right")
        self._ban_add_btn = add

    def _build_controls(self):
        row = tk.Frame(self.frame, bg=C["bg"])
        row.pack(fill="x", padx=10, pady=(8, 0))
        self.side_seg = Segmented(row, SIDES, self.cfg["side"], self.on_side)
        self.side_seg.pack(side="left")
        self.pos_seg = Segmented(row, POSITIONS, self.cfg["position"], self.on_position)
        self.pos_seg.pack(side="right")

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=C["dim"], font=(FONT, 7, "bold"))

    def _build_settings_panel(self):
        p = tk.Frame(self.frame, bg=C["panel"], padx=10, pady=8)
        p.pack(fill="x", padx=10, pady=(8, 0))
        self.settings_panel = p

        self._label(p, "STEAM ID").grid(row=0, column=0, sticky="w")
        self.profile_lbl = tk.Label(p, text="", bg=C["panel"], fg=C["muted"], font=(FONT, 8), width=1, anchor="e")
        self.profile_lbl.grid(row=0, column=1, columnspan=2, sticky="we")
        idrow = tk.Frame(p, bg=C["panel"])
        idrow.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 6))
        self.account_var = tk.StringVar(value=self.cfg["account_id"])
        ent = tk.Entry(idrow, textvariable=self.account_var, bg=C["bg"], fg=C["fg"], insertbackground=C["fg"],
                       relief="flat", font=(FONT, 9))
        ent.pack(side="left", fill="x", expand=True, ipady=3)
        ent.bind("<Return>", lambda e: self.on_account())
        go = tk.Label(idrow, text="⟳", bg=C["accent"], fg=C["bg"], font=(FONT, 10, "bold"), padx=8, cursor="hand2")
        go.pack(side="left", padx=(4, 0), fill="y")
        go.bind("<Button-1>", lambda e: self.on_account())
        self.account_var.trace_add("write", lambda *a: self._save_account())

        self._label(p, "РЕЖИМ").grid(row=2, column=0, columnspan=3, sticky="w")
        self.mode_seg = Segmented(p, MODES, self.cfg["mode"], lambda v: self._set_cfg("mode", v))
        self.mode_seg.grid(row=3, column=0, columnspan=3, sticky="we", pady=(2, 6))

        self._label(p, "ПОПУЛЯРНОСТЬ НА 7000+").grid(row=4, column=0, sticky="w")
        self._label(p, "ТВОЙ ОПЫТ").grid(row=4, column=2, sticky="w")
        self.rate_seg = Segmented(p, PICK_RATES, self.cfg["pick_rate"], lambda v: self._set_cfg("pick_rate", v))
        self.rate_seg.grid(row=5, column=0, sticky="w", pady=(2, 6))
        self.exp_seg = Segmented(p, EXPERIENCE, self.cfg["experience"], lambda v: self._set_cfg("experience", v))
        self.exp_seg.grid(row=5, column=2, sticky="e", pady=(2, 6))
        p.columnconfigure(1, weight=1)

        self._label(p, "ПРОЗРАЧНОСТЬ ОКНА").grid(row=6, column=0, sticky="w")
        self.opacity_cv = tk.Canvas(p, width=self.w - 40, height=18, bg=C["panel"], highlightthickness=0, cursor="hand2")
        self.opacity_cv.grid(row=7, column=0, columnspan=3, sticky="we", pady=(2, 6))
        self.opacity_cv.bind("<Button-1>", self._opacity_drag)
        self.opacity_cv.bind("<B1-Motion>", self._opacity_drag)
        self.opacity_cv.bind("<ButtonRelease-1>", lambda e: save_config(self.cfg))
        self._draw_opacity()

        self._label(p, "ПРЯТАТЬ ОКНО ОТ DISCORD-СТРИМА И СКРИНШОТОВ").grid(row=8, column=0, columnspan=3, sticky="w")
        self.capture_seg = Segmented(p, [("no", "Нет, показывать"), ("yes", "Да, всегда прятать")],
                                     "yes" if self.cfg.get("hide_from_capture") else "no", self._on_capture_mode)
        self.capture_seg.grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 6))

        self._label(p, "СВОРАЧИВАТЬ ПРИ КЛИКЕ ВНЕ ОКНА (Scroll Lock покажет снова)").grid(
            row=10, column=0, columnspan=3, sticky="w")
        self.autohide_seg = Segmented(p, [("no", "Нет"), ("yes", "Да")],
                                      "yes" if self.cfg.get("minimize_on_outside_click") else "no",
                                      lambda v: self._set_option("minimize_on_outside_click", v == "yes"))
        self.autohide_seg.grid(row=11, column=0, columnspan=3, sticky="w", pady=(2, 6))

        links = tk.Frame(p, bg=C["panel"])
        links.grid(row=12, column=0, columnspan=3, sticky="we")
        more = self._icon_button(links, "Токен STRATZ, веса, клавиша…", self.open_settings, 8)
        more.pack(side="left")
        refresh = self._icon_button(links, "Обновить статистику", lambda: self.load_data(force=True), 8)
        refresh.pack(side="right")

    def _build_table(self):
        # шапка таблицы отдельно — она не прокручивается
        self.head_cv = tk.Canvas(self.frame, width=self.w, height=HEAD_H, bg=C["bg"], highlightthickness=0,
                                 cursor="hand2")
        self.head_cv.pack(fill="x", pady=(8, 0))
        self.head_cv.bind("<Button-1>", self._head_click)
        body = tk.Frame(self.frame, bg=C["bg"])
        body.pack(fill="x")
        self.table_cv = tk.Canvas(body, width=self.w - 6, height=self.table_h, bg=C["bg"], highlightthickness=0,
                                  yscrollincrement=ROW_H)
        self.table_cv.pack(side="left")
        self.scroll_cv = tk.Canvas(body, width=6, height=self.table_h, bg=C["bg"], highlightthickness=0)
        self.scroll_cv.pack(side="left")
        self.table_cv.bind("<Motion>", self._table_motion)
        self.table_cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.table_cv.bind("<Button-1>", self._table_click)
        self.table_cv.bind("<Button-3>", self._table_right_click)
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self._row_bg = []
        # объяснение по герою — всплывающая подсказка у курсора, а не блок под таблицей
        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.configure(bg=C["border"])
        self.tip_lbl = tk.Label(self.tip, bg=C["panel"], fg=C["fg"], font=(FONT, 8), justify="left",
                                padx=9, pady=7, wraplength=440)
        self.tip_lbl.pack(padx=1, pady=1)
        self.tip.withdraw()
        self._tip_job = None

        # плашка «Показать сборку» — появляется у курсора после клика по герою
        self.plaque = tk.Toplevel(self.root)
        self.plaque.overrideredirect(True)
        self.plaque.attributes("-topmost", True)
        plaque_btn = tk.Label(self.plaque, text="▶  Показать сборку", bg=C["accent"], fg=C["bg"],
                              font=(FONT, 9, "bold"), padx=12, pady=5, cursor="hand2")
        plaque_btn.pack()
        plaque_btn.bind("<Button-1>", lambda e: self._open_build())
        self.plaque.bind("<Enter>", lambda e: self._cancel_plaque_timer())
        # «Leave» в Tk приходит и при переходе курсора внутрь плашки (на надпись) — поэтому
        # прячем, только если курсор действительно ушёл за её границы
        self.plaque.bind("<Leave>", lambda e: self.root.after(150, self._plaque_leave_check))
        self.plaque.withdraw()
        self._plaque_rec = None
        self._plaque_job = None
        self.build_win = None

    def _build_footer(self):
        self.notes_lbl = tk.Label(self.frame, text="", bg=C["bg"], fg=C["warn"], font=(FONT, 8), width=1,
                                  justify="left", anchor="w", wraplength=self.w - 20)
        self.notes_lbl.pack(fill="x", padx=10)
        f = tk.Frame(self.frame, bg=C["panel"])
        f.pack(fill="x", pady=(6, 0))
        self.status_lbl = tk.Label(f, text="", bg=C["panel"], fg=C["muted"], font=(FONT, 8), anchor="w", width=1)
        self.status_lbl.pack(side="left", padx=8, pady=4, fill="x", expand=True)
        # уголок для изменения размера окна (у окна без рамки своего нет)
        grip = tk.Canvas(f, width=14, height=22, bg=C["panel"], highlightthickness=0, cursor="size_nw_se")
        grip.pack(side="right", fill="y")
        for i in range(3):
            for j in range(i + 1):
                x, y = 11 - (i - j) * 4, 18 - j * 4
                grip.create_rectangle(x - 1, y - 1, x + 1, y + 1, fill=C["muted"], outline="")
        grip.bind("<ButtonPress-1>", self._resize_begin)
        grip.bind("<B1-Motion>", self._resize_move)
        grip.bind("<ButtonRelease-1>", self._resize_end)
        self.scan_btn = tk.Label(f, text="СКАН", bg=C["accent"], fg=C["bg"], font=(FONT, 8, "bold"),
                                 padx=10, cursor="hand2")
        self.scan_btn.pack(side="right", fill="y")
        self.scan_btn.bind("<Button-1>", lambda e: self.analyze_screen())
        clr = self._icon_button(f, "очистить", self.clear_draft, 8)
        clr.pack(side="right", padx=4)

    # ---------- окно: перетаскивание, свернуть, флаги ----------

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, e):
        self._drag = None
        self._fit_to_screen()
        self.cfg["window_pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        save_config(self.cfg)

    MIN_W, MAX_W = 560, 1100
    MIN_TABLE_H, MAX_TABLE_H = 4 * ROW_H, 40 * ROW_H

    def _clamp_size(self, w, table_h):
        return (int(min(max(w, self.MIN_W), self.MAX_W)),
                int(min(max(table_h, self.MIN_TABLE_H), self.MAX_TABLE_H)))

    def _resize_begin(self, e):
        self._resize_start = (e.x_root, e.y_root, self.w, self.table_h)

    def _resize_move(self, e):
        if not self._resize_start:
            return
        x0, y0, w0, h0 = self._resize_start
        w, h = self._clamp_size(w0 + e.x_root - x0, h0 + e.y_root - y0)
        if (w, h) != (self.w, self.table_h):
            self._apply_size(w, h)

    def _resize_end(self, e):
        self._resize_start = None
        self._fit_to_screen()
        self.cfg["window_size"] = [self.w, self.table_h]
        save_config(self.cfg)

    def _apply_size(self, w, table_h):
        """Ширина растягивает колонку с именами, высота — число видимых строк таблицы."""
        self.w, self.table_h = w, table_h
        self.draft_cv.config(width=w)
        self.bans_cv.config(width=w - 120)
        self.opacity_cv.config(width=w - 40)
        self.head_cv.config(width=w)
        self.table_cv.config(width=w - 6, height=table_h)
        self.scroll_cv.config(height=table_h)
        self.notes_lbl.config(wraplength=w - 20)
        self._draw_draft()
        self._draw_opacity()
        self._draw_table(keep_scroll=True)

    def _work_area(self, win=None):
        """Рабочая область (без панели задач) монитора, на котором сейчас окно."""
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        hwnd = user32.GetAncestor((win or self.root).winfo_id(), 2)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
        return mi.rcWork

    def _fit_to_screen(self, _retry=True):
        """Если окно не влезает по высоте в свой монитор: сдвигаем вверх, потом уменьшаем таблицу,
        а если и этого мало — сворачиваем панель настроек. Иначе кнопка «СКАН» и уголок размера
        уезжают за край экрана."""
        try:
            self.root.update_idletasks()
            wa = self._work_area()
        except Exception:
            return
        y, h = self.root.winfo_y(), self.root.winfo_height()
        over = y + h - wa.b
        if over <= 0:
            return
        new_y = max(wa.t, y - over)
        if new_y != y:
            self.root.geometry(f"+{self.root.winfo_x()}+{new_y}")
            over -= y - new_y
        if over <= 0:
            return
        if self.settings_panel.winfo_ismapped() and _retry:
            self.settings_panel.pack_forget()
            want = (self.cfg.get("window_size") or [W, ROWS * ROW_H])[1]
            self._apply_size(self.w, max(self.table_h, min(want, self.MAX_TABLE_H)))
            self._fit_to_screen(_retry=False)
            return
        shrink = min(over, self.table_h - self.MIN_TABLE_H)
        if shrink > 0:
            self._apply_size(self.w, self.table_h - shrink)

    def _hwnd(self, win=None):
        return ctypes.windll.user32.GetAncestor((win or self.root).winfo_id(), 2)

    def _watch_outside_click(self):
        """Режим «сворачивать при клике вне окна»: смотрим, нажата ли кнопка мыши и над чьим окном курсор.
        Это обычный запрос состояния мыши у Windows — с игрой никак не связано."""
        try:
            if self.cfg.get("minimize_on_outside_click"):
                user32 = ctypes.windll.user32
                down = (user32.GetAsyncKeyState(0x01) | user32.GetAsyncKeyState(0x02)) & 0x8000
                if down and not self._mouse_down and not user32.IsIconic(self._hwnd()):
                    pt = wt.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    user32.WindowFromPoint.argtypes = [wt.POINT]
                    user32.WindowFromPoint.restype = ctypes.c_void_p
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(ctypes.c_void_p(user32.WindowFromPoint(pt)), ctypes.byref(pid))
                    if pid.value != os.getpid():  # кликнули не по окну программы
                        self.minimize()
                self._mouse_down = bool(down)
        except Exception:
            pass
        self.root.after(50, self._watch_outside_click)

    def _restore_after_scan(self):
        """В режиме автосворачивания — после скана показываем окно, не забирая фокус у игры."""
        if self.cfg.get("minimize_on_outside_click") and ctypes.windll.user32.IsIconic(self._hwnd()):
            ctypes.windll.user32.ShowWindow(self._hwnd(), 4)  # SW_SHOWNOACTIVATE

    def minimize(self):
        """Сворачиваем на панель задач средствами Windows (у окна без рамки нет своей кнопки);
        клик по значку на панели задач возвращает окно."""
        self._hide_tip()
        self._hide_plaque()
        if self.build_win is not None:
            self.build_win.withdraw()
        ctypes.windll.user32.ShowWindow(self._hwnd(), 6)  # SW_MINIMIZE

    def _apply_window_flags(self):
        r = self.root
        r.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        r.attributes("-alpha", float(self.cfg.get("opacity", 0.96)))
        try:
            user32 = ctypes.windll.user32
            hwnd = self._hwnd()
            # окно без рамки само по себе не сворачивается — разрешаем (WS_MINIMIZEBOX)
            style = user32.GetWindowLongW(hwnd, -16)
            if not style & 0x00020000:
                user32.SetWindowLongW(hwnd, -16, style | 0x00020000)
            # и по умолчанию пропадает с панели задач — возвращаем (APPWINDOW вместо TOOLWINDOW)
            ex = user32.GetWindowLongW(hwnd, -20)
            new = (ex & ~0x80) | 0x40000
            if new != ex:
                user32.SetWindowLongW(hwnd, -20, new)
                r.withdraw()
                r.after(10, r.deiconify)
        except Exception:
            pass
        self._exclude_from_capture(r)

    def _set_capture_hidden(self, win, hidden):
        """Скрыть окно от захвата экрана (скриншоты, Discord, OBS) или вернуть видимость."""
        try:
            user32 = ctypes.windll.user32
            win.update_idletasks()
            user32.SetWindowDisplayAffinity(user32.GetAncestor(win.winfo_id(), 2),
                                            WDA_EXCLUDEFROMCAPTURE if hidden else 0)
        except Exception:
            pass

    def _exclude_from_capture(self, win):
        """Применить настройку «прятать от скриншотов и стрима» к окну."""
        self._set_capture_hidden(win, bool(self.cfg.get("hide_from_capture")))

    def _hide_for_scan(self, hidden):
        """На время своего скана окно всегда прячется, чтобы не закрыть драфт на скриншоте."""
        if self.cfg.get("hide_from_capture"):
            return
        for win in self._all_windows():
            self._set_capture_hidden(win, hidden)

    def _all_windows(self):
        return [w for w in (self.root, self.tip, self.plaque, self.build_win) if w is not None]

    OPACITY_MIN = 0.4
    _OP_X0 = 8  # левый край полосы ползунка

    @property
    def _OP_X1(self):
        return self.w - 90

    def _draw_opacity(self):
        cv, x0, x1 = self.opacity_cv, self._OP_X0, self._OP_X1
        v = float(self.cfg.get("opacity", 1.0))
        x = x0 + (x1 - x0) * (v - self.OPACITY_MIN) / (1 - self.OPACITY_MIN)
        cv.delete("all")
        cv.create_line(x0, 9, x1, 9, fill=C["bg"], width=4, capstyle="round")
        cv.create_line(x0, 9, x, 9, fill=C["accent"], width=4, capstyle="round")
        cv.create_oval(x - 7, 2, x + 7, 16, fill=C["accent"], outline=C["fg"])
        cv.create_text(x1 + 20, 9, text=f"{v * 100:.0f}%", anchor="w", fill=C["fg"], font=(FONT, 8, "bold"))

    def _opacity_drag(self, e):
        x0, x1 = self._OP_X0, self._OP_X1
        t = min(1.0, max(0.0, (e.x - x0) / (x1 - x0)))
        self._on_opacity(round((self.OPACITY_MIN + t * (1 - self.OPACITY_MIN)) * 100))

    def _set_option(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def _on_capture_mode(self, value):
        self.cfg["hide_from_capture"] = value == "yes"
        save_config(self.cfg)
        for win in self._all_windows():
            self._exclude_from_capture(win)

    def _on_opacity(self, value):
        self.cfg["opacity"] = int(float(value)) / 100
        self.root.attributes("-alpha", self.cfg["opacity"])
        self._draw_opacity()

    def toggle_settings(self):
        shown = bool(self.settings_panel.winfo_ismapped())
        if shown:
            self.settings_panel.pack_forget()
        else:
            self.settings_panel.pack(fill="x", padx=10, pady=(8, 0), before=self.head_cv)
            self._fit_to_screen(_retry=False)  # открыли сами — не сворачиваем, только ужимаем таблицу
        self.cfg["show_settings"] = not shown
        save_config(self.cfg)

    def _install_clipboard_fixes(self):
        # Tk понимает Ctrl+V/C/X/A только в английской раскладке — ловим по физической клавише.
        keys = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "select_all"}

        def on_ctrl(event):
            action = keys.get(event.keycode)
            if not action or event.keysym.lower() in ("v", "c", "x", "a"):
                return None
            w = event.widget
            if action == "select_all":
                try:
                    w.select_range(0, "end")
                    w.icursor("end")
                except (AttributeError, tk.TclError):
                    pass
            else:
                w.event_generate(action)
            return "break"

        menu = tk.Menu(self.root, tearoff=0)

        def on_right_click(event):
            w = event.widget
            w.focus_set()
            menu.delete(0, "end")
            menu.add_command(label="Вставить", command=lambda: w.event_generate("<<Paste>>"))
            menu.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
            menu.add_command(label="Вырезать", command=lambda: w.event_generate("<<Cut>>"))
            menu.tk_popup(event.x_root, event.y_root)

        for cls in ("TEntry", "TCombobox", "TSpinbox", "Entry"):
            self.root.bind_class(cls, "<Control-KeyPress>", on_ctrl, add="+")
            self.root.bind_class(cls, "<Button-3>", on_right_click)
        # окно без рамки само не берёт фокус клавиатуры — отдаём его полю по клику
        self.root.bind_class("Entry", "<Button-1>",
                             lambda e: (e.widget.winfo_toplevel().focus_force(), e.widget.focus_set()), add="+")

    def _save_account(self):
        self.cfg["account_id"] = self.account_var.get().strip()
        save_config(self.cfg)

    def _on_close(self):
        self._save_account()
        if self.hotkey:
            self.hotkey.stop()
        self.root.destroy()

    # ---------- события из фоновых потоков ----------

    def post(self, fn, *args):
        self._events.put((fn, args))

    def _poll(self):
        try:
            while True:
                fn, args = self._events.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def set_status(self, text):
        self.status_lbl.config(text=text)

    def _show_tip(self, text):
        self.status_lbl.config(text=text or self._status_base)

    _status_base = ""

    def _set_base_status(self, text):
        self._status_base = text
        self.status_lbl.config(text=text)

    # ---------- данные ----------

    def load_data(self, force=False, force_player=False):
        if self._loading:
            return
        self._loading = True
        account = parse_account_id(self.account_var.get())
        token = self.cfg["stratz_token"].strip()

        def work():
            recognizer = None
            try:
                self.store.load(account, token, self.pos_seg.value,
                                lambda s: self.post(self.set_status, s), force, force_player)
                images = {hid: self.store.image_path(hid) for hid in self.store.heroes
                          if self.store.image_path(hid).exists()}
                recognizer = Recognizer(images, self.cfg["recognition"])
            except Exception as e:
                self.post(self.set_status, f"Ошибка загрузки: {e}")
            finally:
                self._loading = False
            if recognizer:
                self.post(self._on_loaded, recognizer)

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, recognizer):
        self.recognizer = recognizer
        self.hero_names = sorted(h["loc"] for h in self.store.heroes.values())
        p = self.store.player
        if p:
            games = sum(h["games"] for h in p["heroes"].values())
            self.profile_lbl.config(text=f"{p.get('name') or '?'} · {RANKS[self.store.tier or 0]} · {games} игр")
        else:
            self.profile_lbl.config(text="")
        self.notes_lbl.config(text="\n".join(dict.fromkeys(self.store.notes)))
        self._set_base_status(f"Статистика: пабы 7000+ · {KEYS[self.cfg['hotkey']][0]} — скан драфта")
        self.recalc()
        self._ensure_position()
        self._fit_to_screen()

    def _ensure_position(self):
        pos = self.pos_seg.value
        if self.store.d2_meta and pos not in self.store.d2_matchups:
            self._load_position(pos)

    def _load_position(self, pos):
        if self._loading:
            return
        self._loading = True

        def work():
            try:
                self.store.load_position(pos, lambda s: self.post(self.set_status, s))
            finally:
                self._loading = False
            self.post(self._set_base_status, f"Статистика: пабы 7000+ · {KEYS[self.cfg['hotkey']][0]} — скан драфта")
            self.post(self.recalc)
            self.post(self._ensure_position)  # если пока грузили, позицию успели сменить

        threading.Thread(target=work, daemon=True).start()

    def on_account(self):
        self._save_account()
        self.load_data(force_player=True)

    def resolve_hero(self, text):
        text = (text or "").strip().lower()
        if not text:
            return None
        by_name = {h["loc"].lower(): hid for hid, h in self.store.heroes.items()}
        if text in by_name:
            return by_name[text]
        for name, hid in sorted(by_name.items()):
            if name.startswith(text):
                return hid
        for name, hid in sorted(by_name.items()):
            if text in name:
                return hid
        return None

    # ---------- переключатели ----------

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        self.recalc()

    def on_side(self, side):
        self._set_cfg("side", side)

    def on_position(self, pos):
        self.cfg["position"] = pos
        save_config(self.cfg)
        self.recalc()
        self._ensure_position()

    def allies_enemies(self):
        # нераспознанные слоты («?») в расчётах не участвуют
        r = [h for h in self.radiant if h and h != UNKNOWN]
        d = [h for h in self.dire if h and h != UNKNOWN]
        return (r, d) if self.side_seg.value == "radiant" else (d, r)

    # ---------- драфт: слоты и баны ----------

    SLOT_W, SLOT_H, SLOT_GAP = 40, 23, 3

    def _slot_boxes(self):
        boxes = []
        step = self.SLOT_W + self.SLOT_GAP
        for i in range(5):
            x = 10 + i * step
            boxes.append(("radiant", i, x, 2))
        for i in range(5):
            x = self.w - 10 - (5 - i) * step + self.SLOT_GAP
            boxes.append(("dire", i, x, 2))
        return boxes

    def _slot_at(self, x, y):
        for team, i, bx, by in self._slot_boxes():
            if bx <= x <= bx + self.SLOT_W and by <= y <= by + self.SLOT_H + 16:
                return team, i
        return None

    def _focus_rec(self):
        """Герой из списка, для которого показываем цифры под драфтом: под курсором, иначе выбранный."""
        i = self.hover_row if self.hover_row is not None else self.selected
        return self.recs[i] if self.recs and 0 <= i < len(self.recs) else None

    def _draw_draft(self):
        cv = self.draft_cv
        cv.delete("all")
        focus = self._focus_rec()
        vals = (scorer.candidate_values(self.store, focus.hero_id, self.radiant, self.dire,
                                        self.side_seg.value, self.pos_seg.value)
                if focus else {"radiant": [None] * 5, "dire": [None] * 5})
        rad_vals, dire_vals = vals["radiant"], vals["dire"]
        for team, i, x, y in self._slot_boxes():
            hid = (self.radiant if team == "radiant" else self.dire)[i]
            mine = (team == self.side_seg.value)
            border = C["accent"] if mine else C["red"]
            if hid == UNKNOWN:
                cv.create_rectangle(x, y, x + self.SLOT_W, y + self.SLOT_H, fill="#3a2a10", outline=C["warn"])
                cv.create_text(x + self.SLOT_W / 2, y + self.SLOT_H / 2, text="?", fill=C["warn"],
                               font=(FONT, 11, "bold"))
            elif hid:
                cv.create_image(x, y, anchor="nw", image=self._icon(hid, self.SLOT_W, self.SLOT_H))
            else:
                cv.create_rectangle(x, y, x + self.SLOT_W, y + self.SLOT_H, fill=C["panel"], outline="")
                cv.create_text(x + self.SLOT_W / 2, y + self.SLOT_H / 2, text="+", fill=C["dim"], font=(FONT, 9))
            cv.create_line(x, y + self.SLOT_H + 1, x + self.SLOT_W, y + self.SLOT_H + 1, fill=border, width=2)
            v = (rad_vals if team == "radiant" else dire_vals)[i]
            if v is not None:
                cv.create_text(x + self.SLOT_W / 2, y + self.SLOT_H + 11, text=f"{v:+.1f}%",
                               fill=_signed_color(v), font=(FONT, 7, "bold"))
        # в центре — для какого героя из списка цифры и их сумма (контрпик + синергия),
        # а под ней — у какой команды драфт сильнее
        cx = self.w / 2
        shown = [v for v in rad_vals + dire_vals if v is not None]
        team = self._team_advantage()
        if not (focus and shown) and team is None:
            cv.create_text(cx, 18, text="vs", fill=C["dim"], font=(FONT, 10, "bold"))
            return
        cv.create_rectangle(cx - 42, 2, cx + 42, 50, fill=C["panel"], outline="")
        if focus and shown:
            total = sum(shown)
            name = self.store.name(focus.hero_id).upper()
            while name and self.badge_font.measure(name) > 78:
                name = name[:-1]
            cv.create_text(cx, 10, text=name, fill=C["fg"], font=self.badge_font)
            cv.create_text(cx, 24, text=f"{total:+.1f}%", fill=_signed_color(total), font=(FONT, 10, "bold"))
        if team is not None:
            leader, adv = team
            ours = leader.lower() == self.side_seg.value
            cv.create_line(cx - 34, 35, cx + 34, 35, fill=C["line"])
            cv.create_text(cx, 43, text=f"{leader} {adv:+.1f}%", fill=C["cyan"] if ours else C["red"],
                           font=self.badge_font)

    def _team_advantage(self):
        """(какая сторона сильнее по контрпикам драфта, на сколько) или None, если пиков ещё нет."""
        if self.radiant.count(None) == 5 or self.dire.count(None) == 5:
            return None
        rad = scorer.draft_values(self.store, self.radiant, [h for h in self.dire if h])
        dire = scorer.draft_values(self.store, self.dire, [h for h in self.radiant if h])
        adv = (sum(v for v in rad if v is not None) - sum(v for v in dire if v is not None)) / 10
        return ("RADIANT" if adv >= 0 else "DIRE"), abs(adv)

    def _draft_motion(self, e):
        hit = self._slot_at(e.x, e.y)
        if hit:
            hid = (self.radiant if hit[0] == "radiant" else self.dire)[hit[1]]
            team = "Свет" if hit[0] == "radiant" else "Тьма"
            focus = self._focus_rec()
            if hid == UNKNOWN:
                self._show_tip(f"{team}: тут выбран герой, но я его не узнал (скин?) — кликни и выбери")
            elif hid and focus:
                ally = hit[0] == self.side_seg.value
                what = "синергия" if ally else "против"
                self._show_tip(f"{self.store.name(focus.hero_id)} {what} {self.store.name(hid)} · "
                               f"клик: изменить, ПКМ: убрать")
            else:
                self._show_tip(f"{team}: {self.store.name(hid)} — клик: изменить, ПКМ: убрать" if hid
                               else f"{team}: пустой слот — клик, чтобы вписать героя")
        else:
            self._show_tip("")

    def _draft_click(self, e):
        hit = self._slot_at(e.x, e.y)
        if not hit or not self.hero_names:
            return
        team, i = hit
        HeroPicker(self, e.x_root - 20, e.y_root + 14, lambda hid: self._set_slot(team, i, hid))

    def _draft_right_click(self, e):
        hit = self._slot_at(e.x, e.y)
        if hit:
            self._set_slot(*hit, None)

    def _set_slot(self, team, i, hid):
        if hid:
            for lst in (self.radiant, self.dire):
                for j, h in enumerate(lst):
                    if h == hid:
                        lst[j] = None
            if hid in self.banned:
                self.banned.remove(hid)
            self.manual_ids.add(hid)
        (self.radiant if team == "radiant" else self.dire)[i] = hid
        self.recalc()

    def _add_ban_dialog(self):
        if not self.hero_names:
            return
        x = self._ban_add_btn.winfo_rootx() - 150
        y = self._ban_add_btn.winfo_rooty() + 18
        HeroPicker(self, x, y, self._add_ban, allow_clear=False)

    def _add_ban(self, hid):
        if hid and hid not in self.banned:
            self.banned.append(hid)
        self.recalc()

    def _draw_bans(self):
        cv = self.bans_cv
        cv.delete("all")
        if not self.banned:
            cv.create_text(0, 9, text="нет", anchor="w", fill=C["dim"], font=(FONT, 8))
        for i, hid in enumerate(self.banned):
            x = i * 33
            cv.create_image(x, 0, anchor="nw", image=self._icon(hid, 31, 17, gray=True))

    def _ban_click(self, e):
        i = int(e.x // 33)
        if 0 <= i < len(self.banned):
            self.banned.pop(i)
            self.recalc()

    def clear_draft(self):
        self.radiant, self.dire = [None] * 5, [None] * 5
        self.banned, self.manual_ids = [], set()
        self.live_lbl.config(text="● LIVE DRAFT")
        self.recalc()

    # ---------- распознавание ----------

    def _start_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        self.hotkey = GlobalHotkey(self.cfg["hotkey"], lambda: self.post(self.analyze_screen))
        self.scan_btn.config(text=f"СКАН · {KEYS[self.cfg['hotkey']][0]}")
        if self.hotkey.error:
            self.set_status(self.hotkey.error)

    def analyze_screen(self):
        if self.recognizer is None:
            self.set_status("Подожди, данные ещё загружаются…")
            return
        if self._recognizing:
            return
        self._recognizing = True
        self.set_status("Распознаю…")
        rc = self.cfg["recognition"]
        self._hide_for_scan(True)

        def work():
            try:
                t = time.time()
                time.sleep(0.08)  # даём Windows применить скрытие окна
                try:
                    monitor = self.cfg.get("monitor") or 0
                    shots = [(monitor, grab_screen(monitor))] if monitor else grab_monitors()
                finally:
                    self.post(self._hide_for_scan, False)
                shots = [(i, im) for i, im in shots if im.mean() >= 3]
                if not shots:
                    self.post(self.set_status, "Скриншот чёрный — переключи Доту в режим «Окно без рамки»")
                    return
                # авто: ищем драфт на всех мониторах, начиная с того, где он был в прошлый раз
                last = self.cfg.get("last_monitor")
                shots.sort(key=lambda s: s[0] != last)
                best = None
                for idx, shot in shots:
                    found, sc = self.recognizer.detect(shot)
                    real = [d for d in found if d.hero_id != UNKNOWN]
                    total = sum(d.score for d in real)
                    if best is None or total > best[0]:
                        best = (total, idx, shot, found, sc)
                    if len(real) >= 3 and idx == last:
                        break  # драфт там же, где в прошлый раз — другие мониторы не смотрим
                total, idx, img, dets, scale = best
                if total > 0 and idx != last:
                    self.post(self._set_option, "last_monitor", idx)
                radiant, dire = Recognizer.split_teams(dets, img.shape[1])
                SCREENS_DIR.mkdir(exist_ok=True)
                write_image(SCREENS_DIR / "last.png", img)
                names = {hid: h["loc"] for hid, h in self.store.heroes.items()}
                write_image(SCREENS_DIR / "last_debug.png", draw_debug(img, dets, names, rc))
                self.post(self._apply_detection, [d.hero_id for d in radiant], [d.hero_id for d in dire],
                          scale, len(dets), time.time() - t)
            except Exception as e:
                self.post(self.set_status, f"Ошибка распознавания: {e}")
            finally:
                self._recognizing = False

        threading.Thread(target=work, daemon=True).start()

    def _apply_detection(self, radiant, dire, scale, count, elapsed):
        self._restore_after_scan()
        if not count:
            self.set_status(f"Героев не нашёл ({elapsed:.1f} c) — можно вписать кликом по слоту")
            return
        # прошлый скан заменяется целиком, а вписанное вручную сохраняется
        found = set(radiant) | set(dire)
        keep_r = [h for h in self.radiant if h in self.manual_ids and h not in found]
        keep_d = [h for h in self.dire if h in self.manual_ids and h not in found]

        def merge(detected, kept):
            # вписанный руками герой обычно стоит как раз на месте «?» — убираем по одному «?» на каждого
            detected = list(detected)
            for _ in kept:
                if UNKNOWN in detected:
                    detected.remove(UNKNOWN)
            return (detected + kept + [None] * 5)[:5]

        self.radiant = merge(radiant, keep_r)
        self.dire = merge(dire, keep_d)
        unknown = (radiant + dire).count(UNKNOWN)
        known = count - unknown
        self.set_status(f"Найдено героев: {known} ({elapsed:.1f} c)"
                        + (f" · не узнал: {unknown} — кликни по «?» и выбери" if unknown else ""))
        if scale and count >= 4 and scale != self.cfg["recognition"].get("learned_scale"):
            self.cfg["recognition"]["learned_scale"] = scale
            save_config(self.cfg)
        self.recalc()

    # ---------- рекомендации ----------

    def weights(self):
        base = self.cfg["weights"]
        mult = MODE_MULT[self.mode_seg.value]
        return {k: base[k] * m for k, m in zip(("meta", "counter", "synergy", "comfort"), mult)}

    def recalc(self):
        self._draw_bans()
        if self.store.heroes:
            allies, enemies = self.allies_enemies()
            self._base_recs = scorer.recommend(self.store, self.pos_seg.value, allies, enemies, self.banned,
                                               self.weights(), min_pick_share=PICK_RATE_SHARE[self.rate_seg.value],
                                               min_games=MIN_GAMES[self.exp_seg.value], limit=200)
            self.recs = self._sorted(self._base_recs)
            self.selected = 0
            self.hover_row = None
            self._hide_tip()
            self._hide_plaque()
            self._draw_table()
        self._draw_draft()

    def _sorted(self, recs):
        if self.sort_key == "name":
            return sorted(recs, key=lambda r: self.store.name(r.hero_id))
        if self.sort_key in SORT_VALUE:
            get = SORT_VALUE[self.sort_key]
            # больше — выше; у кого значения нет — в конец; при равенстве остаётся порядок по итогу
            return sorted(recs, key=lambda r: (get(r) is None, -(get(r) or 0)))
        return list(recs)

    def _head_click(self, e):
        dx = self.w - W
        for key, x0, x1 in SORT_ZONES:
            lo, hi = (x0, x1 + dx) if key == "name" else (x0 + dx, x1 + dx)
            if lo <= e.x < hi:
                # повторный клик по той же колонке (или клик по «Итог») — обычный порядок
                self.sort_key = None if key in (self.sort_key, "total") else key
                break
        else:
            return
        self.recs = self._sorted(self._base_recs)
        self.selected, self.hover_row = 0, None
        self._hide_tip()
        self._draw_table()
        self._draw_draft()

    def _draw_table(self, keep_scroll=False):
        dx = self.w - W  # лишняя ширина окна уходит в колонку с именами
        head = self.head_cv
        head.delete("all")
        active = self.sort_key or "total"
        for k, (title, x, anchor) in enumerate(HEADERS):
            on = SORT_ZONES[k][0] == active
            arrow = ("▲ " if active == "name" else "▼ ") if on else ""
            if anchor == "e":
                title = arrow + title
            elif on:
                title = title + (" ▲" if active == "name" else " ▼")
            head.create_text(x + (dx if k else 0), HEAD_H / 2, text=title, anchor=anchor,
                             fill=(C["accent"] if on else C["dim"]), font=(FONT, 7, "bold"))
        head.create_line(0, HEAD_H - 1, self.w, HEAD_H - 1, fill=C["line"])

        cv = self.table_cv
        first = cv.yview()[0]
        cv.delete("all")
        self._row_bg = []
        cv.configure(scrollregion=(0, 0, self.w - 6, max(self.table_h, len(self.recs) * ROW_H)))
        cv.yview_moveto(first if keep_scroll else 0)
        if not self.recs:
            msg = ("Нет героев под фильтры — укажи Steam ID или поменяй «Твой опыт»"
                   if self.store.heroes else "Загружаю данные…")
            cv.create_text(self.w / 2, 40, text=msg, fill=C["muted"], font=(FONT, 9))
            self._draw_scrollbar()
            return
        rows = self.store.d2_meta.get(self.pos_seg.value, {})
        top_matches = max((r["matches"] for r in rows.values()), default=0) or 1
        for i, rec in enumerate(self.recs):
            y = i * ROW_H
            cy = y + ROW_H / 2
            self._row_bg.append(cv.create_rectangle(0, y, self.w, y + ROW_H - 1, fill=C["bg"], outline=""))
            cv.create_image(10, cy, anchor="w", image=self._icon(rec.hero_id, 36, 20))
            cv.create_text(54, cy, text=self._fit(self.store.name(rec.hero_id), 124 + dx), anchor="w",
                           fill=C["fg"], font=self.name_font)
            self._draw_matches(cv, rec.matches, top_matches, y, dx)
            self._draw_tier(cv, rec.rating, cy, dx)

            wr = "—" if rec.wr7k is None else f"{rec.wr7k * 100:.1f}%"
            wr_color = C["muted"] if rec.wr7k is None else (D2_GREEN if rec.wr7k >= 0.5 else D2_RED)
            pwr = rec.wins / rec.games if rec.games else None
            cells = [
                (372, wr, wr_color, True),
                (414, f"{rec.synergy:+.1f}", _signed_color(rec.synergy), False),
                (462, f"{rec.counter:+.1f}", _signed_color(rec.counter), False),
                (498, str(rec.games) if rec.games else "—", C["muted"], False),
                (546, "—" if pwr is None else f"{pwr * 100:.0f}%",
                 C["muted"] if pwr is None else _signed_color((pwr - 0.5) * 100, 2.0), False),
                (592, f"{rec.total:+.1f}", _signed_color(rec.total, 0.05), True),
            ]
            for x, text, color, bold in cells:
                cv.create_text(x + dx, cy, text=text, anchor="e", fill=color,
                               font=(FONT, 9, "bold") if bold else (FONT, 9))
            cv.create_line(0, y + ROW_H - 1, self.w, y + ROW_H - 1, fill=C["line"])
        self._paint_rows()
        self._draw_scrollbar()

    def _on_wheel(self, e):
        # колесо работает, когда курсор над таблицей
        w = self.root.winfo_containing(e.x_root, e.y_root)
        if w is not self.table_cv and w is not self.scroll_cv:
            return
        self.table_cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
        self._draw_scrollbar()
        self._hide_plaque()
        self.hover_row = None  # под курсором теперь другой герой — подсказку покажем заново
        self._set_hover(self._row_at(e.y_root - self.table_cv.winfo_rooty()))

    def _draw_scrollbar(self):
        cv = self.scroll_cv
        cv.delete("all")
        first, last = self.table_cv.yview()
        if last - first >= 0.999:
            return
        h = self.table_h
        cv.create_rectangle(2, 0, 5, h, fill=C["line"], outline="")
        cv.create_rectangle(1, first * h, 6, last * h, fill=C["dim"], outline="")

    def _draw_matches(self, cv, matches, top, y, dx=0):
        """Число матчей с жёлтой полоской под ним — как на протрекере."""
        x0, x1 = 184 + dx, 236 + dx
        if not matches:
            cv.create_text(x1, y + ROW_H / 2, text="—", anchor="e", fill=C["muted"], font=(FONT, 8))
            return
        cv.create_text(x1, y + 11, text=str(matches), anchor="e", fill=C["fg"], font=(FONT, 8))
        cv.create_rectangle(x0, y + 20, x1, y + 23, fill="#2a2f3a", outline="")
        cv.create_rectangle(x0, y + 20, x0 + (x1 - x0) * min(1.0, matches / top), y + 23,
                            fill=D2_YELLOW, outline="")

    def _draw_tier(self, cv, rating, cy, dx=0):
        """Значок тира (S/A/B…) и плашка «91 /100» — как «Tier & Rating» на протрекере."""
        x = 248 + dx
        if rating is None:
            cv.create_text(x, cy, text="—", anchor="w", fill=C["muted"], font=(FONT, 9))
            return
        letter, color = _tier(rating)
        _round_rect(cv, x, cy - 8, x + 16, cy + 8, 4, fill=color, outline="")
        cv.create_text(x + 8, cy, text=letter, fill="#1a1a1a", font=(FONT, 7, "bold"))
        _round_rect(cv, x + 19, cy - 8, x + 62, cy + 8, 4, fill=PILL_BG, outline=PILL_BORDER)
        num = cv.create_text(x + 24, cy, text=f"{rating:.0f}", anchor="w", fill=PILL_NUM, font=(FONT, 8, "bold"))
        cv.create_text(cv.bbox(num)[2] + 1, cy + 1, text="/100", anchor="w", fill=PILL_SUB, font=(FONT, 6))

    def _paint_rows(self):
        for i, rid in enumerate(self._row_bg):
            color = C["sel"] if i == self.selected else C["hover"] if i == self.hover_row else C["bg"]
            self.table_cv.itemconfig(rid, fill=color)

    def _fit(self, text, px):
        if self.name_font.measure(text) <= px:
            return text
        while text and self.name_font.measure(text + "…") > px:
            text = text[:-1]
        return text + "…"

    def _row_at(self, y):
        """y — координата внутри видимой части таблицы; учитываем прокрутку."""
        cy = self.table_cv.canvasy(y)
        i = int(cy // ROW_H)
        return i if cy >= 0 and 0 <= i < len(self.recs) else None

    def _set_hover(self, i):
        if i != self.hover_row:
            self.hover_row = i
            self._paint_rows()
            self._draw_draft()  # цифры под драфтом — для героя под курсором
            self._hide_tip()
            if i is not None:
                self._tip_job = self.root.after(450, lambda: self._show_row_tip(i))

    def _table_motion(self, e):
        self._set_hover(self._row_at(e.y))

    def _table_click(self, e):
        i = self._row_at(e.y)
        if i is not None:
            self.selected = i
            self._paint_rows()
            self._draw_draft()
            self._hide_plaque()
            self._show_row_tip(i)

    def _table_right_click(self, e):
        """Правый клик по герою — плашка «Показать сборку» прямо под курсором."""
        i = self._row_at(e.y)
        if i is not None:
            self.selected = i
            self._paint_rows()
            self._draw_draft()
            self._show_build_plaque(i)

    def _show_build_plaque(self, i):
        self._hide_tip()
        self._plaque_rec = self.recs[i]
        # плашка появляется прямо под курсором: второй клик в то же место сразу открывает сборку
        px, py = self.root.winfo_pointerxy()
        self.plaque.geometry(f"+{px - 14}+{py - 12}")
        self.plaque.deiconify()
        self.plaque.lift()
        self._exclude_from_capture(self.plaque)
        self._cancel_plaque_timer()
        self._plaque_job = self.root.after(5000, self._hide_plaque)  # не трогали — сама исчезнет

    def _cancel_plaque_timer(self):
        if self._plaque_job:
            self.root.after_cancel(self._plaque_job)
            self._plaque_job = None

    def _hide_plaque(self):
        self._cancel_plaque_timer()
        self.plaque.withdraw()

    def _plaque_leave_check(self):
        if not self.plaque.winfo_viewable():
            return
        px, py = self.root.winfo_pointerxy()
        x, y = self.plaque.winfo_rootx(), self.plaque.winfo_rooty()
        inside = x - 2 <= px <= x + self.plaque.winfo_width() + 2 and y - 2 <= py <= y + self.plaque.winfo_height() + 2
        if not inside:
            self._hide_plaque()

    def _open_build(self):
        rec = self._plaque_rec
        self._hide_plaque()
        if not rec:
            return
        if self.build_win is None:
            self.build_win = BuildWindow(self)
        allies, enemies = self.allies_enemies()
        self.build_win.show(rec.hero_id, self.pos_seg.value, enemies)

    def _hide_tip(self):
        if self._tip_job:
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        self.tip.withdraw()

    def _show_row_tip(self, i):
        if self._tip_job:  # если подсказка уже ждала по таймеру — отменяем, чтобы не всплыла потом
            self.root.after_cancel(self._tip_job)
        self._tip_job = None
        if i >= len(self.recs) or i != self.hover_row:
            return
        rec = self.recs[i]
        self.tip_lbl.config(text=f"{self.store.name(rec.hero_id)} — итог {rec.total:+.1f}: "
                                 f"мета {rec.meta:+.1f}, контрпик {rec.counter:+.1f}, "
                                 f"синергия {rec.synergy:+.1f}, комфорт {rec.comfort:+.1f}\n"
                                 + scorer.explain(rec, self.store, self.pos_seg.value))
        px, py = self.root.winfo_pointerxy()
        self.tip.geometry(f"+{px + 18}+{py + 14}")
        self.tip.deiconify()
        self.tip.lift()
        self._exclude_from_capture(self.tip)

    def _icon(self, hid, w, h, gray=False):
        key = (hid, w, h, gray)
        if key not in self._icons:
            try:
                img = Image.open(self.store.image_path(hid)).convert("RGB").resize((w, h), Image.LANCZOS)
                if gray:
                    img = img.convert("L").convert("RGB")
                self._icons[key] = ImageTk.PhotoImage(img)
            except (OSError, KeyError):
                self._icons[key] = ""
        return self._icons[key]

    # ---------- настройки ----------

    def open_settings(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["panel"],
                     bordercolor=C["border"])
        st.configure("TLabel", background=C["bg"], foreground=C["fg"])
        st.configure("Head.TLabel", font=(FONT, 9, "bold"))
        st.configure("TButton", background=C["sel"], foreground=C["fg"], padding=(10, 4))
        st.map("TButton", background=[("active", C["hover"])])
        st.configure("TCombobox", foreground=C["fg"], arrowcolor=C["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", C["panel"])])
        self.root.option_add("*TCombobox*Listbox.background", C["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["fg"])

        win = tk.Toplevel(self.root)
        win.title("Настройки")
        win.configure(bg=C["bg"])
        win.attributes("-topmost", True)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Токен STRATZ (бесплатно: stratz.com/api → войти через Steam):").grid(row=0, column=0, columnspan=2, sticky="w")
        old_token = self.cfg["stratz_token"]
        token_var = tk.StringVar(value=old_token)
        ttk.Entry(frm, textvariable=token_var, width=60, show="•").grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))

        def on_token(*_):
            self.cfg["stratz_token"] = token_var.get().strip()
            save_config(self.cfg)

        def on_close():
            win.destroy()
            if self.cfg["stratz_token"] != old_token:
                self.load_data(force_player=True)

        token_var.trace_add("write", on_token)
        win.protocol("WM_DELETE_WINDOW", on_close)

        ttk.Label(frm, text="Базовые веса формулы (режим включает/выключает части):", style="Head.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        weight_vars = {}
        for i, (key, label) in enumerate((("meta", "Мета"), ("counter", "Контрпик"),
                                          ("synergy", "Синергия"), ("comfort", "Комфорт (твои игры)"))):
            ttk.Label(frm, text=label).grid(row=3 + i, column=0, sticky="w")
            var = tk.DoubleVar(value=self.cfg["weights"][key])
            ttk.Spinbox(frm, from_=0, to=3, increment=0.1, textvariable=var, width=6).grid(row=3 + i, column=1, sticky="w")
            weight_vars[key] = var

        ttk.Label(frm, text="Горячая клавиша:").grid(row=7, column=0, sticky="w", pady=(10, 0))
        key_names = {v[0]: k for k, v in KEYS.items()}
        hk_var = tk.StringVar(value=KEYS[self.cfg["hotkey"]][0])
        ttk.Combobox(frm, values=list(key_names), textvariable=hk_var, state="readonly", width=14).grid(row=7, column=1, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="Монитор с игрой (0 = авто — ищет драфт на всех):").grid(row=8, column=0, sticky="w", pady=(6, 0))
        mon_var = tk.IntVar(value=self.cfg["monitor"])
        ttk.Spinbox(frm, from_=0, to=6, textvariable=mon_var, width=6).grid(row=8, column=1, sticky="w", pady=(6, 0))

        def save():
            self.cfg["stratz_token"] = token_var.get().strip()
            token_changed = self.cfg["stratz_token"] != old_token
            for key, var in weight_vars.items():
                try:
                    self.cfg["weights"][key] = round(float(var.get()), 2)
                except (tk.TclError, ValueError):
                    pass
            self.cfg["hotkey"] = key_names[hk_var.get()]
            self.cfg["monitor"] = mon_var.get()
            save_config(self.cfg)
            self._start_hotkey()
            self._apply_window_flags()
            win.destroy()
            if token_changed:
                self.load_data(force_player=True)
            else:
                self.recalc()

        ttk.Button(frm, text="Сохранить", command=save).grid(row=9, column=0, columnspan=2, pady=(14, 0))


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
