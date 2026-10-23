"""Клиент GraphQL API STRATZ. Нужен бесплатный токен: https://stratz.com/api"""
import socket

import requests
import urllib3.util.connection

URL = "https://api.stratz.com/graphql"

# STRATZ пускает токен не более чем с 2 IP за 15 минут. Windows чередует IPv4/IPv6
# (а IPv6-адреса ещё и временные), поэтому ходим только по IPv4 и одним соединением.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
_session = requests.Session()
_session.headers.update({"User-Agent": "STRATZ_API", "Content-Type": "application/json"})

# Статистику берём с хай-ммр, а не с ранга игрока.
HIGH_BRACKETS = "DIVINE, IMMORTAL"
HIGH_BASIC_BRACKET = "DIVINE_IMMORTAL"


class StratzError(Exception):
    pass


def query(token, q):
    try:
        r = _session.post(URL, json={"query": q}, timeout=60, headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as e:
        raise StratzError(f"STRATZ недоступен: {e}") from e
    if r.status_code == 403 and "IP Address" in r.text:
        raise StratzError("STRATZ: лимит — токен можно использовать максимум с 2 IP за 15 минут "
                          "(VPN/прокси?). Подожди 15 минут и нажми «Обновить статистику».")
    if r.status_code in (401, 403):
        raise StratzError("STRATZ: токен не принят (проверь его в настройках)")
    if not r.ok:
        raise StratzError(f"STRATZ {r.status_code}: {r.text[:150]}")
    body = r.json()
    if body.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in body["errors"])
        raise StratzError(f"STRATZ: {msgs[:150]}")
    return body["data"]


def player_rank(token, account_id):
    q = "{ player(steamAccountId: %d) { steamAccount { name seasonRank } } }" % account_id
    acc = (query(token, q).get("player") or {}).get("steamAccount") or {}
    return acc.get("name"), acc.get("seasonRank")


def player_heroes(token, account_id):
    q = """{ player(steamAccountId: %d) {
        heroesPerformance(request: {take: 10000, gameModeIds: [22]}, take: 300) {
            heroId matchCount winCount lastPlayedDateTime
        }
    } }""" % account_id
    rows = (query(token, q).get("player") or {}).get("heroesPerformance") or []
    return {r["heroId"]: {"games": r["matchCount"] or 0, "wins": r["winCount"] or 0,
                          "last": r.get("lastPlayedDateTime") or 0} for r in rows}


def win_by_position(token):
    """{позиция: {hero_id: [матчей, побед]}} за последние 2 недели на Divine/Immortal."""
    parts = [
        f"p{pos}: winWeek(bracketIds: [{HIGH_BRACKETS}], positionIds: [POSITION_{pos}], "
        f"gameModeIds: [ALL_PICK_RANKED]) {{ week heroId winCount matchCount }}"
        for pos in range(1, 6)
    ]
    data = query(token, "{ heroStats { %s } }" % " ".join(parts))["heroStats"]
    result = {}
    for pos in range(1, 6):
        rows = data.get(f"p{pos}") or []
        weeks = sorted({r["week"] for r in rows}, reverse=True)[:2]
        agg = {}
        for r in rows:
            if r["week"] in weeks:
                m, w = agg.get(r["heroId"], (0, 0))
                agg[r["heroId"]] = (m + r["matchCount"], w + r["winCount"])
        result[pos] = {hid: list(v) for hid, v in agg.items()}
    return result


def matchups(token, hero_ids, progress=None):
    """{hero_id: {"vs": {id: [advantage%, матчей]}, "with": {...}}} на Divine/Immortal."""
    basic = HIGH_BASIC_BRACKET
    out = {}
    hero_ids = list(hero_ids)
    for i in range(0, len(hero_ids), 10):
        chunk = hero_ids[i:i + 10]
        parts = [
            f"h{h}: heroVsHeroMatchup(heroId: {h}, bracketBasicIds: [{basic}]) "
            f"{{ advantage {{ heroId with {{ heroId2 synergy matchCount }} vs {{ heroId2 synergy matchCount }} }} }}"
            for h in chunk
        ]
        data = query(token, "{ heroStats { %s } }" % " ".join(parts))["heroStats"]
        for h in chunk:
            adv = (data.get(f"h{h}") or {}).get("advantage") or []
            if not adv:
                continue
            a = adv[0]
            out[h] = {
                "vs": {r["heroId2"]: [r["synergy"], r["matchCount"]] for r in a.get("vs") or []},
                "with": {r["heroId2"]: [r["synergy"], r["matchCount"]] for r in a.get("with") or []},
            }
        if progress:
            progress(f"STRATZ: матчапы {min(i + 10, len(hero_ids))}/{len(hero_ids)}")
    return out
