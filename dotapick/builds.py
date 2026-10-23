"""Подстройка сборки под вражеский пик.

У Dota2ProTracker нет данных «какие предметы собирать против конкретных героев», поэтому:
берём предметы, которые на этом герое реально собирают на 7000+, и отмечаем те из них,
что закрывают угрозы из вражеского драфта (магия, невидимость, иллюзии, уклонение и т.д.).
"""

INVIS = {"riki", "clinkz", "bounty_hunter", "nyx_assassin", "weaver", "invoker", "mirana",
         "sand_king", "treant", "templar_assassin"}
ILLUSIONS = {"phantom_lancer", "naga_siren", "chaos_knight", "terrorblade", "meepo", "arc_warden"}
EVASION = {"phantom_assassin", "windrunner", "brewmaster"}
HEAL = {"huskar", "necrolyte", "alchemist", "morphling", "abaddon", "oracle", "dazzle", "omniknight",
        "chen", "wisp", "witch_doctor", "undying", "life_stealer", "warlock", "enchantress", "treant", "pugna"}
ESCAPE = {"storm_spirit", "queenofpain", "antimage", "puck", "ember_spirit", "void_spirit", "weaver",
          "faceless_void", "slark", "pangolier", "kez", "morphling", "mirana", "earth_spirit", "monkey_king"}
PASSIVES = {"bristleback", "phantom_assassin", "spectre", "huskar", "slardar", "medusa", "faceless_void",
            "ursa", "drow_ranger", "viper", "sniper", "chaos_knight", "troll_warlord"}
TARGETED = {"lion", "lina", "doom_bringer", "legion_commander", "bane", "shadow_shaman", "batrider", "pudge",
            "beastmaster", "sniper", "necrolyte", "ogre_magi", "rubick", "sven", "chaos_knight", "vengefulspirit",
            "dragon_knight"}

# (угроза, проверка героя, сколько таких врагов нужно, предметы-ответы, подходит ли всем героям)
THREATS = [
    ("магический урон и контроль", lambda h: "Nuker" in h["roles"], 3,
     ["black_king_bar", "pipe", "glimmer_cape", "eternal_shroud", "aeon_disk", "mage_slayer"], False),
    ("невидимость", lambda h: h["short"] in INVIS, 1, ["dust", "ward_sentry", "gem"], True),
    ("иллюзии и клоны", lambda h: h["short"] in ILLUSIONS, 1,
     ["mjollnir", "maelstrom", "gungir", "bfury", "radiance", "shivas_guard", "crimson_guard"], False),
    ("уклонение", lambda h: h["short"] in EVASION, 1, ["monkey_king_bar", "bloodthorn", "javelin"], False),
    ("лечение и реген", lambda h: h["short"] in HEAL, 1, ["spirit_vessel", "shivas_guard", "skadi"], False),
    ("мобильные герои", lambda h: h["short"] in ESCAPE, 2,
     ["orchid", "bloodthorn", "abyssal_blade", "sheepstick", "gungir"], False),
    ("сильные пассивки (нужен Break)", lambda h: h["short"] in PASSIVES, 1, ["silver_edge"], False),
    ("точечные заклинания", lambda h: h["short"] in TARGETED, 2, ["lotus_orb", "sphere"], False),
    ("физический урон керри", lambda h: "Carry" in h["roles"] and "Nuker" not in h["roles"], 2,
     ["ghost", "blade_mail", "assault", "solar_crest", "heavens_halberd", "shivas_guard", "crimson_guard",
      "butterfly"], False),
]


def item_pool(build, items):
    """{name предмета: насколько часто его собирают} — предметы, реально встречающиеся на герое."""
    pool = {}
    for r in build.get("late", []) + build.get("early", []):
        if r["pr"] >= 0.03 and r["item"] in items:
            pool[items[r["item"]]["name"]] = max(pool.get(items[r["item"]]["name"], 0), r["pr"])
    for r in build.get("sixslot", []):
        if r["pr"] >= 0.02 and r["item"] in items:
            pool[items[r["item"]]["name"]] = max(pool.get(items[r["item"]]["name"], 0), r["pr"])
    return pool


def situational(store, build, items, enemies, position):
    """[{item_id, reason, pr}] — предметы из пула героя против угроз во вражеском пике."""
    pool = item_pool(build, items)
    by_name = {it["name"]: item_id for item_id, it in items.items()}
    picks = {}
    for threat, test, minimum, answers, for_everyone in THREATS:
        who = [e for e in enemies if e in store.heroes and test(store.heroes[e])]
        if len(who) < minimum:
            continue
        if for_everyone:
            # дешёвая детекция — любому; поддержке варды, кору пыль
            chosen = ["ward_sentry", "dust"] if position >= 4 else ["dust"]
        else:
            chosen = sorted((a for a in answers if a in pool), key=lambda a: -pool[a])[:2]
        reason = f"{threat}: " + ", ".join(store.name(e) for e in who)
        for name in chosen:
            if name not in by_name:
                continue
            p = picks.setdefault(name, {"item": by_name[name], "reasons": [], "pr": pool.get(name, 0)})
            p["reasons"].append(reason)
    out = sorted(picks.values(), key=lambda p: (-len(p["reasons"]), -p["pr"]))
    return [{"item": p["item"], "reason": "; ".join(p["reasons"]), "pr": p["pr"]} for p in out[:6]]
