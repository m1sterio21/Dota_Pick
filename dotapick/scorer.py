"""Оценка героев для текущего драфта.

Все составляющие считаются в процентных пунктах винрейта, чтобы их можно было складывать:
  итог = w_meta·мета + w_counter·контрпик + w_synergy·синергия + w_comfort·комфорт

Статистика — с хай-ммр: Dota2ProTracker (пабы 7000+) основной источник,
STRATZ (Divine/Immortal) и OpenDota (Divine/Immortal) — дополнительные.
"""
import math
import time
from dataclasses import dataclass, field

# Запасной вариант, если нет данных по позициям: грубо по ролям героя.
ROLE_FALLBACK = {
    1: lambda r: "Carry" in r and "Support" not in r,
    2: lambda r: ("Nuker" in r or "Carry" in r) and "Support" not in r,
    3: lambda r: ("Initiator" in r or "Durable" in r) and "Support" not in r,
    4: lambda r: "Support" in r or ("Initiator" in r and "Disabler" in r),
    5: lambda r: "Support" in r,
}

# Вес источников в контрпиках/синергии
SRC_WEIGHTS = {"d2pt": 0.6, "stratz": 0.3, "opendota": 0.1}


@dataclass
class Rec:
    hero_id: int
    total: float
    meta: float
    meta_wr: float
    counter: float
    synergy: float
    comfort: float
    games: int = 0
    wins: int = 0
    rating: float = None      # рейтинг протрекера /100
    matches: int = 0          # матчей на позиции на 7000+
    wr7k: float = None        # «сырой» винрейт на позиции на 7000+
    vs: list = field(default_factory=list)      # [(enemy_id, adv)]
    with_: list = field(default_factory=list)   # [(ally_id, adv)]


def _shrink(wins, n, k, prior=0.5):
    """Байесовское сглаживание: при малой выборке тянем винрейт к prior."""
    return (wins + k * prior) / (n + k)


def _fits_position(store, hid, position):
    if store.d2_meta:
        return hid in store.position_heroes(position)
    if store.sz_positions:
        counts = [store.sz_positions.get(p, {}).get(hid, (0, 0))[0] for p in range(1, 6)]
        here = counts[position - 1]
        return here >= 50 and here / max(1, sum(counts)) >= 0.12
    return ROLE_FALLBACK[position](store.heroes[hid]["roles"])


def _meta(store, hid, position):
    """(мета в п.п., сглаженный винрейт, рейтинг протрекера, матчей на 7000+)."""
    row = store.d2_meta.get(position, {}).get(hid)
    parts = []
    if row and row["matches"]:
        wr = _shrink(row["wins"], row["matches"], 1500)
        # рейтинг протрекера уже учитывает и винрейт, и популярность: 50/100 = 0, 100/100 = +5
        d2_meta = 0.6 * (row["rating"] - 50) / 10 + 0.4 * (wr - 0.5) * 100
        parts.append((d2_meta, wr, 0.75))
    others = []
    if store.sz_positions:
        m, w = store.sz_positions.get(position, {}).get(hid, (0, 0))
        if m:
            others.append(_shrink(w, m, 1000))
    od = store.od_stats.get(hid)
    if od:
        picks = (od.get("7_pick") or 0) + (od.get("8_pick") or 0)
        wins = (od.get("7_win") or 0) + (od.get("8_win") or 0)
        if picks:
            others.append(_shrink(wins, picks, 1000))
    if others:
        wr = sum(others) / len(others)
        parts.append(((wr - 0.5) * 100, wr, 0.25))
    if not parts:
        return 0.0, 0.5, None, 0
    wsum = sum(w for *_, w in parts)
    meta = sum(v * w for v, _, w in parts) / wsum
    wr = parts[0][1]
    return meta, wr, row["rating"] if row else None, row["matches"] if row else 0


def _pair(store, hid, other, kind, position):
    """Преимущество hid против/вместе с other в п.п. или None, если данных нет.
    position=None — взять матчапы протрекера с любой загруженной позиции героя."""
    vals = []
    d2 = row = None
    for p in ([position] if position else list(store.d2_matchups)):
        d2 = store.d2_matchups.get(p, {}).get(hid, {}).get(kind, {}).get(other)
        if d2:
            row = store.d2_meta.get(p, {}).get(hid)
            break
    if d2 and row and row["matches"]:
        n, w = d2
        base = row["wins"] / row["matches"]
        vals.append(((_shrink(w, n, 300, base) - base) * 100, SRC_WEIGHTS["d2pt"]))

    sz = store.sz_matchups.get(hid, {}).get(kind, {}).get(other)
    if sz is None:
        rev = store.sz_matchups.get(other, {}).get(kind, {}).get(hid)
        if rev is not None:
            sz = [-rev[0] if kind == "vs" else rev[0], rev[1]]
    if sz is not None and sz[0] is not None:
        adv, n = sz
        vals.append((adv * n / (n + 150), SRC_WEIGHTS["stratz"]))

    if kind == "vs":
        od = store.od_matchups.get(hid, {}).get(other)
        if od:
            games, wins = od
            base = store.od_base.get(hid, 0.5)
            # выборка OpenDota маленькая (десятки игр на пару) — сглаживаем сильно
            vals.append(((_shrink(wins, games, 150, base) - base) * 100, SRC_WEIGHTS["opendota"]))
    if not vals:
        return None
    wsum = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / wsum


def _comfort(store, mine):
    if not store.player:
        return 0.0
    if not mine or not mine["games"]:
        return -2.0
    g, w = mine["games"], mine["wins"]
    experience = 4.0 * min(1.0, math.log1p(g) / math.log1p(150))
    personal_wr = max(-4.0, min(4.0, (_shrink(w, g, 10) - 0.5) * 100 * 0.4))
    recent = 1.0 if mine.get("last") and time.time() - mine["last"] < 60 * 86400 else 0.0
    return experience + personal_wr + recent


def recommend(store, position, allies, enemies, banned, weights, min_pick_share=0.0, min_games=0, limit=12):
    """min_pick_share — фильтр «Pick rate»: доля от числа матчей самого популярного героя позиции;
    min_games — фильтр «Опыт»: сколько своих рейтинговых игр нужно на герое."""
    taken = set(allies) | set(enemies) | set(banned)
    meta_rows = store.d2_meta.get(position, {})
    top_matches = max((r["matches"] for r in meta_rows.values()), default=0)
    out = []
    for hid in store.heroes:
        if hid in taken or not _fits_position(store, hid, position):
            continue
        if min_pick_share and top_matches and \
                meta_rows.get(hid, {}).get("matches", 0) < min_pick_share * top_matches:
            continue
        mine = store.player_hero(hid)
        games = mine["games"] if mine else 0
        if games < min_games:
            continue

        meta, meta_wr, rating, matches = _meta(store, hid, position)
        vs = [(e, a) for e in enemies if (a := _pair(store, hid, e, "vs", position)) is not None]
        with_ = [(a_id, a) for a_id in allies if (a := _pair(store, hid, a_id, "with", position)) is not None]
        counter = sum(a for _, a in vs)
        synergy = sum(a for _, a in with_)
        comfort = _comfort(store, mine)
        total = (weights["meta"] * meta + weights["counter"] * counter
                 + weights["synergy"] * synergy + weights["comfort"] * comfort)
        row = meta_rows.get(hid)
        wr7k = row["wins"] / row["matches"] if row and row["matches"] else None
        out.append(Rec(hid, total, meta, meta_wr, counter, synergy, comfort,
                       games, mine["wins"] if mine else 0, rating, matches, wr7k, vs, with_))
    out.sort(key=lambda r: r.total, reverse=True)
    return out[:limit]


def candidate_values(store, cand, radiant, dire, my_side, position):
    """Цифры под героями драфта для кандидата из списка:
    под врагами — его преимущество против каждого, под союзниками — синергия с каждым."""
    allies, enemies = (radiant, dire) if my_side == "radiant" else (dire, radiant)

    def value(h, kind):
        return _pair(store, cand, h, kind, position) if h else None

    vals = {"radiant": [], "dire": []}
    for side, team in (("radiant", radiant), ("dire", dire)):
        kind = "with" if team is allies else "vs"
        vals[side] = [value(h, kind) for h in team]
    return vals


def draft_values(store, team, enemies):
    """Для каждого героя команды — сумма его преимуществ против вражеских героев (п.п.)."""
    vals = []
    for h in team:
        advs = [a for e in enemies if e and h and (a := _pair(store, h, e, "vs", None)) is not None]
        vals.append(sum(advs) if advs else None)
    return vals


def explain(rec, store, position):
    lines = []
    good = sorted((x for x in rec.vs if x[1] >= 0.3), key=lambda x: -x[1])
    bad = sorted((x for x in rec.vs if x[1] <= -0.3), key=lambda x: x[1])
    if good:
        lines.append("Силён против: " + ", ".join(f"{store.name(e)} {a:+.1f}" for e, a in good))
    if bad:
        lines.append("Слаб против: " + ", ".join(f"{store.name(e)} {a:+.1f}" for e, a in bad))
    if rec.with_:
        lines.append("С союзниками: " + ", ".join(
            f"{store.name(a_id)} {a:+.1f}" for a_id, a in sorted(rec.with_, key=lambda x: -x[1])))
    if rec.rating is not None:
        lines.append(f"Протрекер 7000+, поз. {position}: рейтинг {rec.rating:.0f}/100, "
                     f"{rec.matches} матчей, винрейт {rec.meta_wr * 100:.1f}% (сглаженный)")
    else:
        lines.append(f"Мета: винрейт {rec.meta_wr * 100:.1f}% на хай-ммр")
    if store.player:
        if rec.games:
            lines.append(f"Твои игры: {rec.games}, винрейт {rec.wins / rec.games * 100:.0f}%")
        else:
            lines.append("Ты на нём не играл в рейтинге")
    return "\n".join(lines)
