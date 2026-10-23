"""Загрузка и кэширование статистики из OpenDota и STRATZ."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import d2pt, opendota, stratz
from .config import CACHE_DIR

CDN = "https://cdn.cloudflare.steamstatic.com"
HOUR = 3600
STEAM64_BASE = 76561197960265728


def parse_account_id(text):
    """Принимает Friend ID, Steam64 или ссылку на профиль stratz/dotabuff/opendota."""
    digits = re.findall(r"\d+", text or "")
    if not digits:
        return None
    n = int(max(digits, key=len))
    if n > STEAM64_BASE:
        n -= STEAM64_BASE
    return n or None


def _cached(name, ttl, fetch, force=False):
    """Берёт из кэша, если он свежий; иначе качает. При ошибке сети — старый кэш."""
    path = CACHE_DIR / f"{name}.json"
    fresh = path.exists() and time.time() - path.stat().st_mtime < ttl
    if fresh and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        data = fetch()
    except Exception:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _int_keys(d):
    return {int(k): v for k, v in d.items()}


def _parse_matchups(mu):
    return {int(h): {k: _int_keys(v) for k, v in m.items()} for h, m in mu.items()}


# Герой считается «своим» для позиции на протрекере при таком объёме игр на ней.
D2PT_MIN_MATCHES = 200
D2PT_MIN_SHARE = 0.10


class DataStore:
    def __init__(self):
        self.heroes = {}        # id -> {name, short, loc, roles, img}
        self.od_stats = {}      # id -> строка /heroStats (винрейты по брекетам)
        self.od_matchups = {}   # id -> {enemy_id: [games, wins]}
        self.od_base = {}       # id -> средний винрейт героя в выборке матчапов
        self.sz_positions = {}  # позиция -> {id: [матчей, побед]}
        self.sz_matchups = {}   # id -> {"vs": {id: [adv, n]}, "with": {...}}
        self.d2_meta = {}       # позиция -> {id: {matches, wins, rating, share}}
        self.d2_matchups = {}   # позиция -> {id: {"vs": {id: [матчей, побед]}, "with": {...}}}
        self.player = None      # {account_id, name, tier, heroes: {id: {games, wins, last}}}
        self.tier = None        # ранг игрока — только для показа, статистика с хай-ммр
        self.notes = []

    # --- справочное ---

    def name(self, hid):
        return self.heroes.get(hid, {}).get("loc", f"#{hid}")

    def image_path(self, hid):
        return CACHE_DIR / "heroes" / f"{self.heroes[hid]['short']}.png"

    def player_hero(self, hid):
        if not self.player:
            return None
        return self.player["heroes"].get(hid)

    # --- загрузка ---

    def load(self, account_id, token, position, status, force=False, force_player=False):
        self.notes = []
        status("Загружаю список героев…")
        self._set_heroes(_cached("od_herostats", 12 * HOUR, opendota.hero_stats, force))
        self._download_images(status)

        if account_id:
            status("Загружаю профиль…")
            self.player = self._load_player(account_id, token, force or force_player)
            tier = self.player.get("tier")
            self.tier = min(max(int(tier), 1), 8) if tier else None
        else:
            self.player, self.tier = None, None
            self.notes.append("Не указан Steam ID — личная статистика не учитывается.")

        try:
            status("Протрекер: мета 7000+ MMR…")
            meta = _cached("d2pt_meta", 12 * HOUR,
                           lambda: {p: d2pt.position_meta(p) for p in range(1, 6)}, force)
            self.d2_meta = {int(p): _int_keys(d) for p, d in meta.items()}
        except Exception as e:
            self.notes.append(str(e))
        self.load_position(position, status, force)

        if token:
            try:
                status("STRATZ: мета по позициям…")
                pos = _cached("sz_positions_high", 12 * HOUR, lambda: stratz.win_by_position(token), force)
                self.sz_positions = {int(p): _int_keys(d) for p, d in pos.items()}
                mu = _cached("sz_matchups_high", 24 * HOUR,
                             lambda: stratz.matchups(token, sorted(self.heroes), status), force)
                self.sz_matchups = _parse_matchups(mu)
            except Exception as e:
                self.notes.append(str(e))
        else:
            self.sz_positions, self.sz_matchups = {}, {}
            self.notes.append("Нет токена STRATZ — считаю по протрекеру и OpenDota.")

        try:
            mu = _cached("od_matchups", 72 * HOUR, lambda: self._fetch_od_matchups(status), force)
            self._set_od_matchups(mu)
        except Exception as e:
            self.notes.append(f"Матчапы OpenDota: {e}")
        status("Данные загружены · статистика с пабов 7000+ MMR")

    # --- сборки ---

    def ensure_items(self):
        """Справочник предметов (названия, цены, картинки) — грузится при первом открытии сборки."""
        if not getattr(self, "items", None):
            self.items = _int_keys(_cached("od_items", 7 * 24 * HOUR, opendota.item_constants))
        return self.items

    def hero_build(self, hero_id, position, force=False):
        return _cached(f"d2pt_build2_{hero_id}_pos{position}", 12 * HOUR,
                       lambda: d2pt.hero_build(hero_id, position), force)

    def item_icon_path(self, item_id):
        it = self.ensure_items().get(item_id)
        return CACHE_DIR / "items" / f"{it['name']}.png" if it else None

    def download_item_icons(self, item_ids):
        folder = CACHE_DIR / "items"
        folder.mkdir(parents=True, exist_ok=True)
        todo = [i for i in set(item_ids)
                if i in self.items and self.items[i]["img"] and not self.item_icon_path(i).exists()]

        def fetch(item_id):
            try:
                r = requests.get(CDN + self.items[item_id]["img"], timeout=20)
                if r.ok:
                    self.item_icon_path(item_id).write_bytes(r.content)
            except requests.RequestException:
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, todo))

    def position_heroes(self, position):
        """Герои, которых на хай-ммр реально играют на этой позиции (по протрекеру)."""
        meta = self.d2_meta.get(position, {})
        out = {hid for hid, r in meta.items()
               if r["matches"] >= D2PT_MIN_MATCHES and r["share"] >= D2PT_MIN_SHARE}
        # плюс свои коронные герои, если их на этой позиции хоть иногда играют
        if self.player:
            out |= {hid for hid, h in self.player["heroes"].items()
                    if h["games"] >= 30 and meta.get(hid, {}).get("share", 0) >= 0.05}
        return out

    def load_position(self, position, status, force=False):
        """Матчапы протрекера для героев позиции (по запросу на героя, поэтому только нужная позиция)."""
        if not self.d2_meta:
            return
        heroes = sorted(self.position_heroes(position))
        try:
            mu = _cached(f"d2pt_matchups_pos{position}", 24 * HOUR,
                         lambda: d2pt.matchups_for(heroes, position, status), force)
            self.d2_matchups = {**self.d2_matchups, position: _parse_matchups(mu)}
        except Exception as e:
            self.d2_matchups = {**self.d2_matchups, position: {}}  # не повторять до перезапуска
            self.notes.append(str(e))

    def _set_heroes(self, stats):
        heroes, od = {}, {}
        for h in stats:
            img = (h.get("img") or "").rstrip("?")
            heroes[h["id"]] = {
                "name": h["name"],
                "short": h["name"].replace("npc_dota_hero_", ""),
                "loc": h["localized_name"],
                "roles": h.get("roles") or [],
                "img": CDN + img if img else None,
            }
            od[h["id"]] = h
        self.heroes, self.od_stats = heroes, od

    def _download_images(self, status):
        folder = CACHE_DIR / "heroes"
        folder.mkdir(parents=True, exist_ok=True)
        missing = [hid for hid, h in self.heroes.items()
                   if h["img"] and not (folder / f"{h['short']}.png").exists()]
        if not missing:
            return
        status(f"Качаю портреты героев ({len(missing)})…")

        def fetch(hid):
            h = self.heroes[hid]
            try:
                r = requests.get(h["img"], timeout=20)
                if r.ok:
                    (folder / f"{h['short']}.png").write_bytes(r.content)
            except requests.RequestException:
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, missing))

    def _fetch_od_matchups(self, status):
        out = {}
        ids = sorted(self.heroes)
        for i, hid in enumerate(ids, 1):
            status(f"OpenDota: матчапы {i}/{len(ids)}")
            rows = opendota.hero_matchups(hid)
            out[hid] = {r["hero_id"]: [r["games_played"], r["wins"]] for r in rows}
            time.sleep(1.1)  # лимит бесплатного API — 60 запросов в минуту
        return out

    def _set_od_matchups(self, mu):
        matchups, base = {}, {}
        for h, rows in mu.items():
            rows = _int_keys(rows)
            games = sum(g for g, _ in rows.values())
            wins = sum(w for _, w in rows.values())
            matchups[int(h)] = rows
            base[int(h)] = wins / games if games else 0.5
        self.od_matchups, self.od_base = matchups, base

    def _load_player(self, account_id, token, force):
        def fetch():
            p = {"account_id": account_id, "name": None, "tier": None, "heroes": {}, "notes": []}
            sz_heroes, od_heroes = {}, {}
            if token:
                try:
                    name, rank = stratz.player_rank(token, account_id)
                    p["name"] = name
                    if rank:
                        p["tier"] = rank // 10
                    sz_heroes = stratz.player_heroes(token, account_id)
                except stratz.StratzError as e:
                    p["notes"].append(str(e))
            try:
                od = opendota.player(account_id)
                p["name"] = p["name"] or (od.get("profile") or {}).get("personaname")
                if not p["tier"] and od.get("rank_tier"):
                    p["tier"] = od["rank_tier"] // 10
                for r in opendota.player_heroes(account_id):
                    if r.get("games"):
                        od_heroes[int(r["hero_id"])] = {"games": r["games"], "wins": r["win"],
                                                        "last": r.get("last_played") or 0}
                if not od_heroes:
                    opendota.request_refresh(account_id)
                    p["notes"].append("OpenDota ещё не знает твоих матчей — запросил обновление, "
                                      "через 10–15 минут нажми «Загрузить» ещё раз.")
            except opendota.ApiError as e:
                p["notes"].append(str(e))
            # Оба источника считают одни и те же рейтинговые матчи — берём тот, где игр больше.
            for hid in set(sz_heroes) | set(od_heroes):
                a, b = sz_heroes.get(hid), od_heroes.get(hid)
                best = max((x for x in (a, b) if x), key=lambda x: x["games"])
                best = dict(best, last=max((x or {}).get("last", 0) or 0 for x in (a, b)))
                p["heroes"][hid] = best
            return p

        p = _cached(f"player_{account_id}", 1 * HOUR, fetch, force)
        p["heroes"] = _int_keys(p["heroes"])
        self.notes.extend(p.get("notes", []))
        return p
