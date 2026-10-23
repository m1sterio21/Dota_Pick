"""Распознавание героев на скриншоте драфта сравнением с портретами (OpenCV).

Работает только с картинкой экрана — к файлам и процессу игры не обращается.
Ищем портреты в верхней полосе экрана на нескольких масштабах; найденное слева от
центра — Свет, справа — Тьма.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import mss
import numpy as np

TEMPLATE_H = 32      # высота портрета в пикселях, к которой приводится полоса экрана
# В рейтинге нижнюю часть портрета закрывает плашка ранга («Титан», «Божество III») и медаль,
# а края портрета скошены. Поэтому сравниваем только верхнюю чистую часть без краёв.
KEEP_TOP = (0.05, 0.58)   # какую полосу портрета по высоте берём (доли сверху)
SIDE_CROP = 0.13          # сколько срезаем слева и справа

UNKNOWN = -1  # слот занят, но героя не узнали (например, у героя другой портрет из-за скина)
# Центры слотов верхней панели драфта: расстояние от центра экрана в долях высоты экрана
# (измерено на реальном рейтинговом драфте; уточняется по найденным героям).
SLOT_OFFSETS = (0.179, 0.2945, 0.4115, 0.528, 0.644)


@dataclass
class Detection:
    hero_id: int
    score: float
    box: tuple  # x, y, w, h в пикселях экрана


def read_image(path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


def write_image(path, img):
    ok, buf = cv2.imencode(Path(path).suffix or ".png", img)
    if ok:
        buf.tofile(str(path))


def grab_monitors():
    """Снимки всех мониторов: [(номер монитора, картинка)]."""
    with getattr(mss, "MSS", mss.mss)() as sct:
        return [(i, np.ascontiguousarray(np.array(sct.grab(m))[:, :, :3]))
                for i, m in enumerate(sct.monitors[1:], 1)]


def grab_screen(monitor=0):
    with getattr(mss, "MSS", mss.mss)() as sct:
        mons = sct.monitors[1:]
        if 1 <= monitor <= len(mons):
            mon = mons[monitor - 1]
        else:
            mon = next((m for m in mons if m["left"] == 0 and m["top"] == 0), mons[0])
        shot = np.array(sct.grab(mon))
    return np.ascontiguousarray(shot[:, :, :3])


def _make_template(img, aspect):
    h, w = img.shape[:2]
    if w / h > aspect:
        nw = int(h * aspect)
        img = img[:, (w - nw) // 2:(w - nw) // 2 + nw]
    else:
        nh = int(w / aspect)
        img = img[(h - nh) // 2:(h - nh) // 2 + nh]
    tw = round(TEMPLATE_H * aspect)
    tpl = cv2.resize(img, (tw, TEMPLATE_H), interpolation=cv2.INTER_AREA)
    y0, y1 = round(TEMPLATE_H * KEEP_TOP[0]), round(TEMPLATE_H * KEEP_TOP[1])
    dx = round(tw * SIDE_CROP)
    return np.ascontiguousarray(tpl[y0:y1, dx:tw - dx])


def _brightness(img):
    return float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].mean())


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter) if inter else 0.0


class Recognizer:
    def __init__(self, images, rc):
        """images: {hero_id: путь к портрету}, rc: секция "recognition" конфига."""
        self.rc = rc
        self.templates = {}
        self.brightness = {}
        for hid, path in images.items():
            img = read_image(path)
            if img is not None:
                tpl = _make_template(img, rc["portrait_aspect"])
                self.templates[hid] = tpl
                self.brightness[hid] = _brightness(tpl)
        self._pool = ThreadPoolExecutor(max_workers=8)

    def _scan(self, strip, x0, screen_h, scales):
        rc = self.rc
        by_scale = {}
        for s in scales:
            # f — во сколько раз ужать экран, чтобы портрет стал высотой TEMPLATE_H
            f = TEMPLATE_H / (rc["portrait_height"] * screen_h * s)
            small = cv2.resize(strip, None, fx=f, fy=f,
                               interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_LINEAR)

            def match(item):
                hid, tpl = item
                th, tw = tpl.shape[:2]
                if small.shape[0] < th or small.shape[1] < tw:
                    return None
                res = cv2.matchTemplate(small, tpl, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx < rc["threshold"]:
                    return None
                return Detection(hid, float(mx), (x0 + loc[0] / f, loc[1] / f, tw / f, th / f))

            by_scale[s] = [d for d in self._pool.map(match, self.templates.items()) if d]
        return by_scale

    def detect(self, img):
        """Возвращает (список Detection, использованный масштаб или None)."""
        rc = self.rc
        H, W = img.shape[:2]
        half = rc["half_width"] * H
        x0, x1 = max(0, int(W / 2 - half)), min(W, int(W / 2 + half))
        strip = img[:max(1, int(H * rc["strip_height"])), x0:x1]

        learned = rc.get("learned_scale")
        scales = [learned * k for k in (0.94, 1.0, 1.06)] if learned else list(rc["scales"])
        by_scale = self._scan(strip, x0, H, scales)
        if learned and not any(by_scale.values()):
            by_scale = self._scan(strip, x0, H, list(rc["scales"]))
        if not any(by_scale.values()):
            return [], None

        # Все портреты на экране одного размера — берём масштаб с самыми уверенными совпадениями.
        best_s = max(by_scale, key=lambda s: sum(sorted((d.score for d in by_scale[s]), reverse=True)[:10]))
        cands = sorted(by_scale[best_s], key=lambda d: -d.score)
        # настоящие портреты совпадают примерно одинаково хорошо; сильно более слабые — случайный фон
        floor = max(rc["threshold"], cands[0].score * rc.get("relative_threshold", 0.78))
        portrait_h = rc["portrait_height"] * H * best_s
        portrait_w = portrait_h * rc["portrait_aspect"]
        row_y = cands[0].box[1]  # все портреты стоят в одну линию — по самому уверенному совпадению
        accepted, used = [], set()
        for d in cands:
            if d.score < floor:
                break
            if d.hero_id in used or abs(d.box[1] - row_y) > 0.15 * portrait_h:
                continue
            # в одном слоте может быть только один герой
            cx = d.box[0] + d.box[2] / 2
            if any(abs(cx - (a.box[0] + a.box[2] / 2)) < 0.6 * portrait_w for a in accepted):
                continue
            # Наведённый, но не выбранный герой рисуется затемнённым — это не пик.
            x, y, w, h = map(int, d.box)
            region = img[max(0, y):y + h, max(0, x):x + w]
            if region.size and _brightness(region) < rc.get("min_brightness_ratio", 0.75) * self.brightness[d.hero_id]:
                continue
            accepted.append(d)
            used.add(d.hero_id)
            if len(accepted) == 10:
                break
        return accepted + self._unknown_slots(img, accepted), best_s

    def _unknown_slots(self, img, dets):
        """Слоты, где явно стоит герой (яркий цветной портрет), но шаблон не совпал.
        Возвращаем их как Detection(UNKNOWN), чтобы в программе был «?», а не пустое место."""
        if not dets:
            return []
        H, W = img.shape[:2]
        cx0 = W / 2
        centers = [d.box[0] + d.box[2] / 2 for d in dets]
        # уточняем масштаб сетки по найденным героям
        ratios = []
        for c in centers:
            off = abs(c - cx0) / H
            nearest = min(SLOT_OFFSETS, key=lambda o: abs(o - off))
            ratios.append(off / nearest)
        k = float(np.median(ratios))
        if not 0.75 < k < 1.33:
            return []
        bw = float(np.median([d.box[2] for d in dets]))
        bh = float(np.median([d.box[3] for d in dets]))
        y = int(np.median([d.box[1] for d in dets]))
        out = []
        for sign in (-1, 1):
            for o in SLOT_OFFSETS:
                c = cx0 + sign * o * k * H
                if any(abs(c - dc) < 0.5 * bw for dc in centers):
                    continue
                x = int(c - bw / 2)
                region = img[max(0, y):y + int(bh), max(0, x):x + int(bw)]
                if not region.size:
                    continue
                hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
                colorful = float(((hsv[..., 1] > 90) & (hsv[..., 2] > 70)).mean())
                if colorful >= self.rc.get("unknown_colorful", 0.3):
                    out.append(Detection(UNKNOWN, 0.0, (x, y, bw, bh)))
        return out

    @staticmethod
    def split_teams(dets, screen_w):
        dets = sorted(dets, key=lambda d: d.box[0])
        radiant = [d for d in dets if d.box[0] + d.box[2] / 2 < screen_w / 2]
        dire = [d for d in dets if d.box[0] + d.box[2] / 2 >= screen_w / 2]
        return radiant, dire


def draw_debug(img, dets, names, rc):
    out = img.copy()
    H, W = out.shape[:2]
    half = rc["half_width"] * H
    cv2.rectangle(out, (int(W / 2 - half), 0), (int(W / 2 + half), int(H * rc["strip_height"])), (255, 200, 0), 2)
    for d in dets:
        x, y, w, h = map(int, d.box)
        color = (0, 200, 255) if d.hero_id == UNKNOWN else (0, 255, 0)
        label = "?" if d.hero_id == UNKNOWN else f"{names.get(d.hero_id, d.hero_id)} {d.score:.2f}"
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        cv2.putText(out, label, (x, y + h + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return out


def main(argv):
    """Проверка на сохранённом скриншоте: python -m dotapick.recognize screens/last.png"""
    from .config import load_config
    from .data import DataStore, _cached, HOUR
    from . import opendota

    cfg = load_config()
    store = DataStore()
    store._set_heroes(_cached("od_herostats", 12 * HOUR, opendota.hero_stats))
    images = {hid: store.image_path(hid) for hid in store.heroes if store.image_path(hid).exists()}
    rec = Recognizer(images, cfg["recognition"])
    img = read_image(argv[0])
    t = time.time()
    dets, scale = rec.detect(img)
    print(f"{len(dets)} героев за {time.time() - t:.2f} c, масштаб {scale}")
    rad, dire = rec.split_teams(dets, img.shape[1])
    print("Свет:", [f"{store.name(d.hero_id)} {d.score:.2f}" for d in rad])
    print("Тьма:", [f"{store.name(d.hero_id)} {d.score:.2f}" for d in dire])
    names = {hid: h["loc"] for hid, h in store.heroes.items()}
    out = Path(argv[0]).with_name(Path(argv[0]).stem + "_debug.png")
    write_image(out, draw_debug(img, dets, names, cfg["recognition"]))
    print("Отладочная картинка:", out)


if __name__ == "__main__":
    main(sys.argv[1:])
