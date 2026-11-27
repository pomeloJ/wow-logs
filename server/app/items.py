"""A character's gear and bags as structured data, with stats, value and gear comparisons.

The stats, item level, slot and vendor price come from the game client through the addon,
so they are the real values for this game version. Meant for tools (voice, MCP, scripts)
that need to answer "what am I wearing", "what is it worth" and "which one is better".
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query

SLOT_NAMES = {1: "頭部", 2: "頸部", 3: "肩部", 4: "襯衣", 5: "胸部", 6: "腰部", 7: "腿部", 8: "腳部", 9: "手腕",
              10: "手部", 11: "戒指一", 12: "戒指二", 13: "飾品一", 14: "飾品二", 15: "背部", 16: "主手",
              17: "副手", 18: "遠程", 19: "外袍"}
# Where an item can go, by the client's equip location. Two slots = either of them.
EQUIP_SLOTS = {
    "INVTYPE_HEAD": [1], "INVTYPE_NECK": [2], "INVTYPE_SHOULDER": [3], "INVTYPE_BODY": [4],
    "INVTYPE_CHEST": [5], "INVTYPE_ROBE": [5], "INVTYPE_WAIST": [6], "INVTYPE_LEGS": [7], "INVTYPE_FEET": [8],
    "INVTYPE_WRIST": [9], "INVTYPE_HAND": [10], "INVTYPE_FINGER": [11, 12], "INVTYPE_TRINKET": [13, 14],
    "INVTYPE_CLOAK": [15], "INVTYPE_WEAPON": [16], "INVTYPE_2HWEAPON": [16], "INVTYPE_WEAPONMAINHAND": [16],
    "INVTYPE_WEAPONOFFHAND": [17], "INVTYPE_SHIELD": [17], "INVTYPE_HOLDABLE": [17], "INVTYPE_RANGED": [18],
    "INVTYPE_RANGEDRIGHT": [18], "INVTYPE_THROWN": [18], "INVTYPE_RELIC": [18], "INVTYPE_TABARD": [19],
}
STAT_NAMES = {"armor": "護甲", "strength": "力量", "agility": "敏捷", "stamina": "耐力", "intellect": "智力",
              "spirit": "精神", "damage_per_second": "每秒傷害", "attack_power": "攻擊強度",
              "spell_power": "法術強度", "spell_damage_done": "法術傷害", "spell_healing_done": "治療效果",
              "crit_rating": "致命一擊", "hit_rating": "命中", "dodge_rating": "閃躲", "parry_rating": "招架",
              "block_rating": "格擋", "defense_skill_rating": "防禦", "mana_regeneration": "法力回復",
              "health_regeneration": "生命回復", "ranged_attack_power": "遠程攻擊強度",
              "feral_attack_power": "野性攻擊強度", "block_value": "格擋值"}
# Armor each class can wear (40+ unlocks the heavier kind for some). Names as the client
# reports them, in zhTW and enUS.
ARMOR_RANK = {"布甲": 1, "Cloth": 1, "皮甲": 2, "Leather": 2, "鎖甲": 3, "Mail": 3, "鎧甲": 4, "Plate": 4}
CLASS_ARMOR = {"MAGE": (1, 1), "PRIEST": (1, 1), "WARLOCK": (1, 1), "DRUID": (2, 2), "ROGUE": (2, 2),
               "HUNTER": (2, 3), "SHAMAN": (2, 3), "WARRIOR": (3, 4), "PALADIN": (3, 4)}
SHIELD_CLASSES = {"WARRIOR", "PALADIN", "SHAMAN"}


def stat_text(stats: Optional[Dict[str, Any]]) -> str:
    """'護甲 88、耐力 5' for a stats table."""
    order = list(STAT_NAMES)
    keys = sorted(stats or {}, key=lambda k: (order.index(k) if k in order else 99, k))
    return "、".join(f"{STAT_NAMES.get(k, k)} {stats[k]:g}" for k in keys)


def diff_text(diff: Dict[str, float]) -> str:
    order = list(STAT_NAMES)
    keys = sorted(diff, key=lambda k: (order.index(k) if k in order else 99, k))
    return "、".join(f"{STAT_NAMES.get(k, k)} {diff[k]:+g}" for k in keys)


def build(d: Dict[str, Any], names: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    """Structured gear, bags and comparisons from a character snapshot."""
    names = names or {}
    level, cls = d.get("level"), d.get("class")

    def gear(e: Dict[str, Any]) -> Dict[str, Any]:
        return {"item_id": e.get("item_id"), "name": e.get("name") or names.get(e.get("item_id")),
                "quality": e.get("quality"), "item_level": e.get("item_level"), "req_level": e.get("req_level"),
                "type": e.get("item_type"), "subtype": e.get("item_subtype"),
                "stats": e.get("stats") if isinstance(e.get("stats"), dict) else None,
                "sell_price": e.get("sell_price")}

    equipment = []
    worn: Dict[int, Dict[str, Any]] = {}
    for e in sorted(d.get("equipment") or [], key=lambda e: e.get("slot") or 0):
        g = {"slot": e.get("slot"), "slot_name": SLOT_NAMES.get(e.get("slot")), **gear(e),
             "durability": e.get("durability"), "durability_max": e.get("durability_max")}
        equipment.append(g)
        worn[e.get("slot")] = g
    two_hander = any(e.get("slot") == 16 and e.get("equip_loc") == "INVTYPE_2HWEAPON" for e in d.get("equipment") or [])

    def usable(e: Dict[str, Any]) -> (Optional[bool], Optional[str]):
        if isinstance(e.get("req_level"), int) and isinstance(level, int) and e["req_level"] > level:
            return False, f"需要 {e['req_level']} 級"
        sub = e.get("item_subtype")
        if e.get("equip_loc") == "INVTYPE_SHIELD":
            return (True, None) if cls in SHIELD_CLASSES else (False, "這個職業不能用盾牌")
        if sub in ARMOR_RANK and cls in CLASS_ARMOR and e.get("equip_loc") != "INVTYPE_CLOAK":
            low, high = CLASS_ARMOR[cls]
            can = high if isinstance(level, int) and level >= 40 else low
            if ARMOR_RANK[sub] > can:
                return False, f"這個職業現在不能穿{sub}"
            return True, None
        if (e.get("equip_loc") or "").startswith(("INVTYPE_WEAPON", "INVTYPE_2HWEAPON", "INVTYPE_RANGED",
                                                    "INVTYPE_THROWN")):
            return None, "武器能不能用要看是否學過該武器技能"
        return True, None

    bags, comparisons = [], []
    bag_items = (d.get("bags") or {}).get("items") or []
    total = junk = 0
    priced = False
    for it in bag_items:
        n = it.get("count") or 1
        price = it.get("sell_price")
        priced = priced or isinstance(price, (int, float))
        value = (price or 0) * n
        total += value
        junk += value if it.get("quality") == 0 else 0
        slots = EQUIP_SLOTS.get(it.get("equip_loc") or "")
        row = {**gear(it), "count": n, "value": value, "equip_slots": [SLOT_NAMES[s] for s in slots] if slots else None}
        row["name"] = it.get("name") or names.get(it.get("item_id"))
        bags.append(row)
        if not slots:
            continue
        ok, note = usable(it)
        # Compare with what is worn there; with two possible slots, an empty one or the weaker piece.
        current = None
        slot = slots[0]
        for s in slots:
            if s not in worn:
                current, slot = None, s
                break
            if current is None or (worn[s].get("item_level") or 0) < (current.get("item_level") or 0):
                current, slot = worn[s], s
        if slot == 17 and two_hander and not note:
            note = "主手現在是雙手武器，要換成單手武器才能用副手"
        elif it.get("equip_loc") == "INVTYPE_2HWEAPON" and 17 in worn:
            note = (note + "；" if note else "") + "這是雙手武器，換上會卸下副手"
        new, old = row["stats"] or {}, (current or {}).get("stats") or {}
        diff = {k: round(new.get(k, 0) - old.get(k, 0), 2) for k in set(new) | set(old)
                if round(new.get(k, 0) - old.get(k, 0), 2) != 0}
        comparisons.append({
            "item": {k: row[k] for k in ("item_id", "name", "quality", "item_level", "req_level", "subtype", "stats",
                                         "sell_price")},
            "slot": slot, "slot_name": SLOT_NAMES.get(slot),
            "equipped": {k: current[k] for k in ("item_id", "name", "quality", "item_level", "subtype", "stats",
                                                 "sell_price")} if current else None,
            "usable": ok, "note": note,
            "stat_diff": diff if (row["stats"] is not None or current is None) else None,
            "item_level_diff": (row["item_level"] - current["item_level"])
            if current and isinstance(row["item_level"], (int, float))
            and isinstance(current.get("item_level"), (int, float)) else None,
        })
    has_stats = any(g["stats"] for g in equipment) or any(b["stats"] for b in bags)
    return {
        "bank": bank(d, names),
        "level": level, "class": cls, "money": d.get("money"),
        "bag_slots": (d.get("bags") or {}).get("slots"), "bag_free": (d.get("bags") or {}).get("free"),
        "bag_value": total if priced else None, "junk_value": junk if priced else None,
        "has_stats": has_stats, "equipment": equipment,
        "bags": sorted(bags, key=lambda b: -b["value"]), "comparisons": comparisons,
    }


def bank(d: Dict[str, Any], names: Optional[Dict[int, str]] = None) -> Optional[Dict[str, Any]]:
    """What is in the bank, as it was the last time its window was open. None when never seen."""
    b = d.get("bank")
    if not isinstance(b, dict):
        return None
    names = names or {}
    rows, total, priced = [], 0, False
    for it in b.get("items") or []:
        n = it.get("count") or 1
        price = it.get("sell_price")
        priced = priced or isinstance(price, (int, float))
        value = (price or 0) * n
        total += value
        rows.append({"item_id": it.get("item_id"), "name": it.get("name") or names.get(it.get("item_id")),
                     "quality": it.get("quality"), "count": n, "item_level": it.get("item_level"),
                     "req_level": it.get("req_level"), "type": it.get("item_type"), "subtype": it.get("item_subtype"),
                     "stats": it.get("stats") if isinstance(it.get("stats"), dict) else None,
                     "sell_price": price, "value": value})
    seen = b.get("seen_at")
    return {"seen_at": datetime.fromtimestamp(seen, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(seen, (int, float)) else None,
            "slots": b.get("slots"), "free": b.get("free"), "value": total if priced else None,
            "items": sorted(rows, key=lambda r: (-(r["quality"] or 0), -r["value"], -r["count"]))}


def register(app, db, can_read, world=None) -> None:
    def snapshot(character_id: str, scope: Optional[str]):
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        with db() as conn:
            snap = conn.execute("SELECT captured_at, payload_json FROM character_snapshots "
                                "WHERE character_id=?", (character_id,)).fetchone()
        if not snap:
            raise HTTPException(404, {"error": "character_not_synced", "message": "this character has no data on the server yet: it shows up once it is played with the sync program running", "character_id": character_id})
        return snap["captured_at"], json.loads(snap["payload_json"])

    @app.get("/api/v1/characters/{character_id}/bank")
    def character_bank(character_id: str, scope: Optional[str] = Depends(can_read)):
        """What the character keeps in the bank: every item with its count and vendor value.
        The game only shows the bank while its window is open, so this is the bank as it was at
        `seen_at`, not now. `bank` is null when the character has not opened the bank since the
        addon learned to record it."""
        captured, d = snapshot(character_id, scope)
        ids: List[int] = [e["item_id"] for e in (d.get("bank") or {}).get("items") or []
                          if not e.get("name") and isinstance(e.get("item_id"), int)]
        return {"character_id": character_id, "captured_at": captured,
                "bank": bank(d, world.item_names(ids) if world and ids else {})}

    @app.get("/api/v1/characters/{character_id}/trades")
    def character_trades(character_id: str, limit: int = Query(50, ge=1, le=500), with_name: Optional[str] = Query(None, alias="with"),
                         scope: Optional[str] = Depends(can_read)):
        """Trades with other players that went through, newest first (addon 0.3.6 and later).

        Each: `with` (the other player, with `with_class` and `with_level`), `gave` and `got`
        (`money` in copper and `items` with item id, name, count and quality), and `not_traded`
        on a side when an item was handed over only to be enchanted (it comes back), with the
        `enchantment`. `with` narrows to one player. Cancelled trades are not recorded."""
        if scope is not None and character_id != scope:
            raise HTTPException(403, "this link only covers one character")
        where, args = "character_id=? AND event_type='TRADE_COMPLETED'", [character_id]
        with db() as conn:
            rows = conn.execute(f"SELECT event_id, timestamp, payload_json FROM events WHERE {where} "
                                "ORDER BY timestamp DESC, event_id DESC", args).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload_json"])
            if not isinstance(p, dict) or (with_name and p.get("with") != with_name):
                continue
            out.append({"event_id": r["event_id"], "timestamp": r["timestamp"],
                        **{k: p.get(k) for k in ("with", "with_class", "with_level", "zone_name", "gave", "got")}})
            if len(out) >= limit:
                break
        return {"character_id": character_id, "trades": out}

    @app.get("/api/v1/characters/{character_id}/items")
    def character_items(character_id: str, scope: Optional[str] = Depends(can_read)):
        """Gear and bags with stats, vendor value, and how each wearable bag item compares
        with what is worn in its slot. `has_stats` is false until a recent addon has run.
        `bank` is the bank as last seen (see `/bank`), or null."""
        captured, d = snapshot(character_id, scope)
        ids: List[int] = [e["item_id"] for e in d.get("equipment") or []
                          if not e.get("name") and isinstance(e.get("item_id"), int)]
        names = world.item_names(ids) if world and ids else {}
        return {"character_id": character_id, "captured_at": captured, **build(d, names)}
