"""Клиент публичного API OpenDota (без ключа: ~60 запросов в минуту)."""
import time

import requests

BASE = "https://api.opendota.com/api"

_session = requests.Session()
_session.headers["User-Agent"] = "dota-pick/1.0 (personal use)"


class ApiError(Exception):
    pass


def _get(path, params=None, retries=4):
    err = None
    for attempt in range(retries):
        try:
            r = _session.get(BASE + path, params=params, timeout=25)
        except requests.RequestException as e:
            err = e
        else:
            if r.ok:
                return r.json()
            if r.status_code != 429 and r.status_code < 500:
                raise ApiError(f"OpenDota {r.status_code}: {r.text[:200]}")
            err = f"HTTP {r.status_code}"
        time.sleep(3 * (attempt + 1))
    raise ApiError(f"OpenDota недоступен: {err}")


def hero_stats():
    return _get("/heroStats")


def hero_matchups(hero_id):
    return _get(f"/heroes/{hero_id}/matchups")


def item_constants():
    """{id предмета: {name, dname, cost, img}} — названия и картинки предметов."""
    ids = _get("/constants/item_ids")
    items = _get("/constants/items")
    out = {}
    for item_id, name in ids.items():
        it = items.get(name)
        if it:
            out[int(item_id)] = {"name": name, "dname": it.get("dname") or name,
                                 "cost": it.get("cost") or 0, "img": (it.get("img") or "").split("?")[0]}
    return out


def player(account_id):
    return _get(f"/players/{account_id}")


def request_refresh(account_id):
    """Просит OpenDota скачать историю матчей игрока (появится через несколько минут)."""
    try:
        _session.post(f"{BASE}/players/{account_id}/refresh", timeout=15)
    except requests.RequestException:
        pass


def player_heroes(account_id):
    # lobby_type 7 = рейтинговые игры
    return _get(f"/players/{account_id}/heroes", {"lobby_type": 7})
