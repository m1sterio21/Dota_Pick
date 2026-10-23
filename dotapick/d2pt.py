"""Статистика Dota2ProTracker (пабы 7000+ MMR) через внутренние запросы их сайта.

Официального API у сайта нет — это те же запросы, что делает их страница в браузере,
поэтому формат может поменяться. Ходим редко (кэш на сутки) и по одному запросу за раз.
"""
import time

import requests

BASE = "https://dota2protracker.com"
MMR = 7000

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": f"{BASE}/meta",
})


class D2ptError(Exception):
    pass


def _get(path, params=None, retries=3):
    err = None
    for attempt in range(retries):
        try:
            r = _session.get(BASE + path, params=params, timeout=40)
        except requests.RequestException as e:
            err = e
        else:
            if r.ok:
                return r.json()
            err = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code != 429:
                break
        time.sleep(3 * (attempt + 1))
    raise D2ptError(f"Dota2ProTracker недоступен: {err}")


def position_meta(position):
    """{hero_id: {matches, wins, rating, share}} для позиции 1..5 на пабах 7000+."""
    rows = _get("/api/heroes/stats", {
        "position": f"pos {position}", "order_by": "matches", "legacy": "false",
        "mmr": MMR, "min_matches": 1, "period": 8,
    })
    if not isinstance(rows, list):
        raise D2ptError("Dota2ProTracker: неожиданный ответ меты")
    return {
        r["hero_id"]: {
            "matches": r.get("matches") or 0,
            "wins": r.get("wins") or 0,
            "rating": float(r.get("meta_score") or 0),     # то самое «Tier & Rating /100» на сайте
            "share": float(r.get("role_pick_share") or 0),  # доля игр героя именно на этой позиции
        }
        for r in rows if r.get("hero_id")
    }


def _sum_by_hero(rows):
    out = {}
    for r in rows or []:
        hid = r.get("other_hero_id")
        if hid is None:
            continue
        m, w = out.get(hid, (0, 0))
        out[hid] = (m + (r.get("matches") or 0), w + (r.get("wins") or 0))
    return {h: list(v) for h, v in out.items()}


def matchups(hero_id, position):
    """{"vs": {id: [матчей, побед]}, "with": {...}} для героя на позиции против/с любыми ролями."""
    data = _get(f"/hero/x/api/matchup-payload", {"heroId": hero_id, "position": f"pos {position}"})
    return {"vs": _sum_by_hero(data.get("matchups")), "with": _sum_by_hero(data.get("synergies"))}


def _item_list(rows):
    """[{item, pr, win_rate, minute}] без повторов, из списков вида anchor_items2 / items_mid_late."""
    out, seen = [], set()
    for r in rows or []:
        item = r.get("raw_item_id")
        if item is None or item in seen:
            continue
        seen.add(item)
        out.append({"item": item, "pr": r.get("pr") or 0, "win_rate": r.get("win_rate"),
                    "minute": r.get("avg_minute")})
    return out


def hero_build(hero_id, position):
    """Актуальная сборка героя на позиции с пабов 7000+ (самый частый вариант аспекта)."""
    rows = _get(f"/api/hero/{hero_id}/builds", {"position": f"pos {position}"})
    if not isinstance(rows, list) or not rows:
        return None
    b = max(rows, key=lambda r: r.get("num_matches") or 0)
    bd = b.get("build_data") or {}
    anchor = bd.get("anchor_build") or [[], 0]
    return {
        "matches": b.get("num_matches") or 0,
        "wins": b.get("num_wins") or 0,
        # варианты стартового закупа: [[id предметов], {count, win_rate, lane_advantage}]
        "starting": [[ids, stats] for ids, stats in (bd.get("starting_items_new") or [])],
        "early": _item_list((bd.get("anchor_items") or []) + (bd.get("anchor_items2") or [])),
        # самые частые инвентари в долгих играх: {"30+": [id…], "45+": […], "55+": […]}
        "lategame": bd.get("anchor_lategame_inventories") or {},
        "core": {"items": anchor[0] or [], "count": anchor[1] if len(anchor) > 1 else 0},
        "late": _item_list(bd.get("items_mid_late")),
        "sixslot": [{"item": r["item_id"], "pr": r.get("pick_rate") or 0, "n": r.get("num_matches") or 0}
                    for r in bd.get("sixslot") or [] if r.get("item_id") is not None],
    }


def matchups_for(hero_ids, position, progress=None):
    out = {}
    for i, hid in enumerate(hero_ids, 1):
        if progress:
            progress(f"Протрекер: матчапы поз. {position} — {i}/{len(hero_ids)}")
        try:
            out[hid] = matchups(hid, position)
        except D2ptError:
            continue
        time.sleep(0.4)
    return out
