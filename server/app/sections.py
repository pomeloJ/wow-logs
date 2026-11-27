"""What a sharing token shows: the parts of a person's record its maker chose.

A sharing token (see auth.py) may be limited to some of these parts. Its maker picks them in
the dashboard; a token with no choice stored shows everything, which is what every token made
before this existed has. The limit is enforced here, on the server, not just by the page:

* each read route belongs to one part (ROUTES), and is refused outright when that part is not
  shown; a few routes are always open (ALWAYS: who the characters are, and static game data),
  and any route not listed here is refused, so a new one never shows something by accident;
* the answers that mix parts are trimmed: the character snapshot loses the fields of a hidden
  part (FIELDS), the list of events loses the kinds of a hidden part (EVENTS), and positions
  are taken out of what remains when where the character goes is hidden.
"""
from typing import Any, Dict, FrozenSet, Iterable, Optional

# (key, what the dashboard calls it), in the order it lists them
PARTS = [
    ("location", "位置與移動路線"),
    ("activity", "最近動態與統計"),
    ("items", "裝備、背包、銀行與金錢"),
    ("quests", "任務"),
    ("character", "屬性、專業與聲望"),
    ("groups", "組隊與戰鬥紀錄"),
    ("screenshots", "遊戲截圖"),
]
KEYS = frozenset(k for k, _ in PARTS)
# what a new sharing token shows until its maker changes it: not group sessions or screenshots
SHARE_DEFAULT = [k for k, _ in PARTS if k not in ("groups", "screenshots")]

P = "/api/v1"
ALWAYS = {
    f"{P}/characters", f"{P}/characters/default", f"{P}/characters/{{character_id}}", f"{P}/characters/{{character_id}}/snapshot",
    f"{P}/maps", f"{P}/maps/{{map_id}}/image",
    f"{P}/world/sources", f"{P}/world/maps", f"{P}/world/quests", f"{P}/world/quests/{{quest_id}}", f"{P}/world/npcs",
    f"{P}/world/npcs/{{npc_id}}", f"{P}/world/names", f"{P}/world/items", f"{P}/world/items/{{item_id}}",
    f"{P}/world/spawns", f"{P}/world/nearby", f"{P}/world/map",
}
ROUTES = {
    f"{P}/locations": "location", f"{P}/world/route": "location", f"{P}/world/route/image": "location",
    f"{P}/events": "activity", f"{P}/stats/activity": "activity", f"{P}/stats/summary": "activity",
    f"{P}/characters/{{character_id}}/items": "items", f"{P}/characters/{{character_id}}/bank": "items",
    f"{P}/characters/{{character_id}}/quests": "quests", f"{P}/world/quest-pins": "quests",
    f"{P}/world/quest-zones": "quests", f"{P}/world/zone-quests": "quests",
    f"{P}/groups": "groups", f"{P}/characters/{{character_id}}/groups": "groups", f"{P}/characters/{{character_id}}/solo": "groups",
    f"{P}/characters/{{character_id}}/groups/{{group_id}}": "groups", f"{P}/characters/{{character_id}}/groups/{{group_id}}/fight": "groups",
    f"{P}/characters/{{character_id}}/groups/{{group_id}}/skills": "groups",
    f"{P}/characters/{{character_id}}/groups/{{group_id}}/analysis": "groups",
    f"{P}/characters/{{character_id}}/combat/skills": "groups", f"{P}/characters/{{character_id}}/combat/analysis": "groups",
    f"{P}/combat/npcs": "groups", f"{P}/characters/{{character_id}}/threat": "groups",
    f"{P}/screenshots": "screenshots", f"{P}/screenshots/{{shot_id}}/image": "screenshots",
    f"{P}/screenshots/{{shot_id}}/thumb": "screenshots", f"{P}/characters/{{character_id}}/screenshots/latest": "screenshots",
    f"{P}/characters/{{character_id}}/screenshots/latest/image": "screenshots",
}
# the character snapshot's fields that belong to a part; the rest (name, class, level, ...) always show
FIELDS = {
    "location": {"map_id", "x", "y", "zone_name", "sub_zone", "bind_location", "resting", "mounted", "flight_points"},
    "items": {"equipment", "bags", "bank", "money"},
    "quests": {"quests", "quests_done"},
    "character": {"stats", "professions", "reputations"},
    "activity": {"played_total", "played_level", "played_at"},
}
# the kinds of event that belong to a part other than "activity"
EVENTS = {
    "location": {"LOCATION_SAMPLE", "ZONE_CHANGED", "FLIGHT_STARTED", "FLIGHT_ENDED", "PARTY_POSITIONS"},
    "items": {"ITEM_LOOTED", "MONEY_CHANGED", "PARTY_LOOT"},
    "quests": {"QUEST_ACCEPTED", "QUEST_COMPLETED", "QUEST_REMOVED"},
    "groups": {"GROUP_JOINED", "GROUP_LEFT", "GROUP_MEMBER_JOINED", "GROUP_MEMBER_LEFT", "PARTY_MEMBER_DIED",
               "GROUP_MEMBER_LEVEL_UP", "GROUP_METER", "GROUP_ENCOUNTER", "ENCOUNTER_END", "PARTY_LOOT", "PARTY_POSITIONS",
               "THREAT"},
    "screenshots": {"MAP_SCREENSHOT"},
}
PLACE = ("map_id", "x", "y", "zone_name", "sub_zone")      # taken out of event payloads when location is hidden


def parse(text: Optional[str]) -> Optional[FrozenSet[str]]:
    """The stored choice: None (everything) or the parts shown."""
    if text is None:
        return None
    return frozenset(p for p in text.split(",") if p in KEYS)


def dump(parts: Optional[Iterable[str]]) -> Optional[str]:
    """For storing: None when every part is shown, so it reads like a token with no choice."""
    if parts is None:
        return None
    chosen = [k for k, _ in PARTS if k in set(parts)]
    return None if len(chosen) == len(PARTS) else ",".join(chosen)


def route_part(path: str) -> Optional[str]:
    """'always', a part's key, or None for a route this module does not know (refused)."""
    return "always" if path in ALWAYS else ROUTES.get(path)


def shows(sections: Optional[FrozenSet[str]], part: str) -> bool:
    return sections is None or part in sections


def hidden_events(sections: Optional[FrozenSet[str]]) -> set:
    if sections is None:
        return set()
    return set().union(*(kinds for part, kinds in EVENTS.items() if part not in sections))


def trim_snapshot(data: Dict[str, Any], sections: Optional[FrozenSet[str]]) -> Dict[str, Any]:
    if sections is None:
        return data
    gone = set().union(*(f for part, f in FIELDS.items() if part not in sections))
    return {k: v for k, v in data.items() if k not in gone}


def trim_payload(payload: Dict[str, Any], sections: Optional[FrozenSet[str]]) -> Dict[str, Any]:
    if shows(sections, "location") or not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if k not in PLACE}
