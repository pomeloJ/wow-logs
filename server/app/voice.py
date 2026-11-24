"""Plain-text character queries over a single GET URL, for voice assistants.

A voice assistant can only open a URL, so the access token travels in the query string.
That token is short, read-only and tied to one character. It also opens a read-only
dashboard for that same character (see can_read in main.py). It cannot write anything
and never returns chat messages.
"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .items import build as build_items, diff_text, stat_text

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (0/o, 1/l/i)
TOKEN_LEN = 12
SECTIONS = ["overview", "activity", "quests", "todo", "bags", "gear", "compare", "skills", "rep", "stats",
            "flights", "kills", "loot", "events", "where"]
ALIASES = {"summary": "overview", "today": "activity", "quest": "quests", "bag": "bags",
           "items": "bags", "equipment": "gear", "professions": "skills", "reputation": "rep",
           "attributes": "stats", "flight": "flights", "location": "where", "log": "events"}
CLASSES = {"WARRIOR": "戰士", "PALADIN": "聖騎士", "HUNTER": "獵人", "ROGUE": "盜賊", "PRIEST": "牧師",
           "SHAMAN": "薩滿", "MAGE": "法師", "WARLOCK": "術士", "DRUID": "德魯伊"}
RACES = {"Human": "人類", "Dwarf": "矮人", "NightElf": "夜精靈", "Gnome": "地精", "Orc": "獸人",
         "Scourge": "不死族", "Tauren": "牛頭人", "Troll": "食人妖"}
STANDING = {1: "仇恨", 2: "敵對", 3: "不友好", 4: "中立", 5: "友好", 6: "尊敬", 7: "崇敬", 8: "崇拜"}
SLOTS = {1: "頭部", 2: "頸部", 3: "肩部", 4: "襯衣", 5: "胸部", 6: "腰部", 7: "腿部", 8: "腳部", 9: "手腕",
         10: "手部", 11: "戒指一", 12: "戒指二", 13: "飾品一", 14: "飾品二", 15: "背部", 16: "主手",
         17: "副手", 18: "遠程", 19: "外袍"}
STAT_NAMES = {"strength": "力量", "agility": "敏捷", "stamina": "耐力", "intellect": "智力",
              "spirit": "精神", "armor": "護甲", "health_max": "生命上限", "power_max": "能量上限"}
RANGE_NAMES = {"today": "今天", "yesterday": "昨天", "week": "最近七天", "all": "全部紀錄"}

HELP = """# 冒險紀錄查詢說明
網址格式：/voice?t=<角色代碼>&show=<項目>&range=<時間>&q=<關鍵字>&limit=<筆數>
- show（可用逗號合併多項，預設 overview,activity）：
  overview 角色概況、activity 活動統計、quests 任務日誌、todo 目前所在區域還沒做的任務、
  bags 背包、gear 裝備（含屬性）、compare 背包裡的裝備和身上的比較、
  skills 專業技能、rep 聲望、stats 屬性、flights 飛行點、kills 擊殺排行、
  loot 拾取排行、events 事件紀錄、where 目前位置、all 全部
- range（影響 activity、kills、loot、events，預設 today）：
  today、yesterday、week、all，或 3h 這種「最近幾小時」
- q：名稱關鍵字，篩選任務、背包、聲望、擊殺、拾取、事件
- type：只用於 events，事件類型，例如 MOB_KILLED、ITEM_LOOTED、LEVEL_UP、
  QUEST_ACCEPTED、QUEST_COMPLETED、PLAYER_DIED、ZONE_CHANGED、SKILL_UP
- limit：清單筆數上限，預設 20，最多 100
- tz：時區與 UTC 的小時差，預設 8
"""


def money(c: Any) -> str:
    c = int(c or 0)
    sign, c = ("-" if c < 0 else ""), abs(c)
    parts = [(c // 10000, "金"), (c // 100 % 100, "銀"), (c % 100, "銅")]
    return sign + ("".join(f"{n}{u}" for n, u in parts if n) or "0銅")


def duration(sec: Any) -> str:
    sec = int(sec or 0)
    d, h, m = sec // 86400, sec // 3600 % 24, sec // 60 % 60
    if d:
        return f"{d}天{h}小時"
    if h:
        return f"{h}小時{m}分"
    return f"{m}分鐘" if m else f"{sec}秒"


def describe(kind: str, p: Dict[str, Any]) -> str:
    """One spoken-friendly line for an event."""
    where = f"（{p['zone_name']}）" if p.get("zone_name") else ""
    if kind == "MOB_KILLED":
        who = f"隊友 {p['killed_by']} " if p.get("killed_by") else ""
        return f"{who}擊殺 {p.get('monster_name') or '未知怪物'}{where}"
    if kind == "GROUP_JOINED":
        names = "、".join(m.get("name", "") for m in p.get("members") or [] if isinstance(m, dict))
        return f"開始組隊（{names or '隊友'}）"
    if kind == "GROUP_LEFT":
        return f"組隊結束，共 {duration(p.get('duration'))}"
    if kind == "PARTY_LOOT":
        return f"隊友 {p.get('looter')} 拾取 {p.get('item_name') or p.get('item_id')} x{p.get('count') or 1}"
    if kind == "PARTY_MEMBER_DIED":
        return f"隊友 {p.get('name')} 死亡{where}"
    if kind == "GROUP_MEMBER_LEVEL_UP":
        return f"隊友 {p.get('name')} 升到 {p.get('level')} 級"
    if kind in ("GROUP_MEMBER_JOINED", "GROUP_MEMBER_LEFT"):
        return f"隊友 {p.get('name')} " + ("加入" if kind == "GROUP_MEMBER_JOINED" else "離隊")
    if kind == "ITEM_LOOTED":
        return f"拾取 {p.get('item_name') or p.get('item_id')} x{p.get('count') or 1}{where}"
    if kind == "XP_GAINED":
        return f"獲得經驗 {p.get('amount')}"
    if kind == "MONEY_CHANGED":
        d = p.get("delta") or 0
        return ("獲得 " if d >= 0 else "花費 ") + money(abs(d))
    if kind == "LEVEL_UP":
        return f"升到 {p.get('level')} 級"
    if kind in ("QUEST_ACCEPTED", "QUEST_COMPLETED", "QUEST_REMOVED"):
        verb = {"QUEST_ACCEPTED": "接受任務", "QUEST_COMPLETED": "完成任務", "QUEST_REMOVED": "移除任務"}[kind]
        return f"{verb} {p.get('quest_name') or p.get('quest_id')}{where}"
    if kind == "PLAYER_DIED":
        return f"死亡{where}"
    if kind == "ZONE_CHANGED":
        return f"進入 {p.get('zone_name') or p.get('map_id')}"
    if kind == "SKILL_UP":
        return str(p.get("text") or "技能提升")
    if kind == "COMBAT":
        return f"戰鬥 {duration(p.get('duration'))}{where}"
    if kind == "NPC_INTERACTION":
        return f"與 {p.get('npc_name') or 'NPC'} 互動{where}"
    if kind in ("INSTANCE_ENTERED", "INSTANCE_LEFT"):
        return ("進入副本 " if kind == "INSTANCE_ENTERED" else "離開副本 ") + str(p.get("name") or "")
    if kind == "ACHIEVEMENT_EARNED":
        return f"獲得成就 {p.get('name') or p.get('achievement_id')}"
    names = {"SESSION_START": "登入遊戲", "FLIGHT_STARTED": "開始飛行", "FLIGHT_ENDED": "飛行結束",
             "ENCOUNTER_END": "首領戰結束", "MAP_SCREENSHOT": "地圖截圖"}
    return names.get(kind, kind)


def register(app, db, can_write, activity: Callable, world=None, claim=lambda writer, character_id: None) -> None:
    """`world` (optional) supplies item names and quest turn-in places from the world database."""
    @app.post("/api/v1/characters/{character_id}/voice-token")
    def voice_token(character_id: str, rotate: bool = False, writer: Optional[str] = Depends(can_write)):
        """Return this character's voice token, creating it on first use. rotate=1 replaces it."""
        claim(writer, character_id)
        with db() as conn:
            if not conn.execute("SELECT 1 FROM characters WHERE id=?", (character_id,)).fetchone():
                raise HTTPException(404, "character not found")
            row = conn.execute("SELECT token FROM voice_tokens WHERE character_id=?",
                               (character_id,)).fetchone()
            if row and not rotate:
                return {"token": row["token"], "created": False}
            token = "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LEN))
            conn.execute("INSERT OR REPLACE INTO voice_tokens(token, character_id, created_at) "
                         "VALUES(?,?,?)", (token, character_id,
                                           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        return {"token": token, "created": True}

    def text(body: str, status: int = 200) -> PlainTextResponse:
        return PlainTextResponse(body, status_code=status, media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @app.get("/voice", include_in_schema=False)
    def voice(t: str = "", show: str = "overview,activity", range: str = "today",
              q: Optional[str] = None, type: Optional[str] = None,
              limit: int = Query(20, ge=1, le=100), tz: float = Query(8, ge=-12, le=14)):
        with db() as conn:
            tok = conn.execute("SELECT character_id FROM voice_tokens WHERE token=?", (t,)).fetchone()
            if not tok:
                return text("代碼無效。請確認網址裡的 t 參數。", 404)
            cid = tok["character_id"]
            snap = conn.execute("SELECT captured_at, payload_json FROM character_snapshots "
                                "WHERE character_id=?", (cid,)).fetchone()
        d: Dict[str, Any] = json.loads(snap["payload_json"]) if snap else {}
        zone = timezone(timedelta(hours=tz))
        now = datetime.now(zone)

        def local(ts: Optional[str], fmt: str = "%m月%d日 %H:%M") -> str:
            if not ts:
                return "未知"
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
                .astimezone(zone).strftime(fmt)

        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start: Optional[datetime]
        end: Optional[datetime] = None
        r = range.strip().lower()
        if r == "today":
            start = midnight
        elif r == "yesterday":
            start, end = midnight - timedelta(days=1), midnight
        elif r == "week":
            start = midnight - timedelta(days=6)
        elif r == "all":
            start = None
        elif r.endswith("h") and r[:-1].isdigit() and 0 < int(r[:-1]) <= 24 * 30:
            start = now - timedelta(hours=int(r[:-1]))
        else:
            return text("range 參數無效。\n\n" + HELP, 400)
        range_name = RANGE_NAMES.get(r, f"最近 {r[:-1]} 小時")

        wanted: List[str] = []
        for s in show.lower().replace(" ", "").split(","):
            s = ALIASES.get(s, s)
            if s == "help":
                return text(HELP)
            for one in (SECTIONS if s == "all" else [s]):
                if one not in SECTIONS:
                    return text(f"show 參數無效：{one}\n\n" + HELP, 400)
                if one not in wanted:
                    wanted.append(one)

        kw = (q or "").strip().lower()
        match = lambda *names: not kw or any(kw in str(n).lower() for n in names if n)  # noqa: E731
        act: Dict[str, Any] = {}
        if {"activity", "kills", "loot"} & set(wanted):
            act = activity(cid, start, end)

        name = d.get("name") or "角色"
        out = [f"# {name} 的冒險紀錄",
               f"資料更新時間：{local(snap['captured_at']) if snap else '尚無資料'}"
               f"（查詢時間 {now.strftime('%m月%d日 %H:%M')}）。資料只在遊戲存檔後才會更新。"]

        def section(title: str, lines: List[str], empty: str = "沒有資料。") -> None:
            out.append(f"\n## {title}")
            out.extend(lines or [empty])

        for s in wanted:
            if s == "overview":
                xp, xp_max = d.get("xp"), d.get("xp_max")
                lines = [f"- {d.get('level', '?')} 級 {RACES.get(d.get('race'), d.get('race') or '')}"
                         f"{CLASSES.get(d.get('class'), d.get('class') or '')}"]
                if xp is not None and xp_max:
                    lines.append(f"- 經驗 {xp}/{xp_max}（{xp * 100 // xp_max}%），距離升級還差 {xp_max - xp}"
                                 + (f"，休息加成 {d['rested_xp']}" if d.get("rested_xp") else ""))
                if d.get("zone_name"):
                    lines.append(f"- 位置：{d['zone_name']}" + (f" {d['sub_zone']}" if d.get("sub_zone") else ""))
                if d.get("money") is not None:
                    lines.append(f"- 金錢：{money(d['money'])}")
                bags = d.get("bags") or {}
                if bags.get("slots"):
                    lines.append(f"- 背包：空位 {bags.get('free')}/{bags['slots']}")
                if d.get("bind_location"):
                    lines.append(f"- 爐石：{d['bind_location']}")
                if d.get("played_total"):
                    lines.append(f"- 總遊戲時間：{duration(d['played_total'])}")
                section("角色概況", lines)
            elif s == "where":
                lines = []
                if d.get("zone_name"):
                    pos = f"，座標 {d['x']:.0f}, {d['y']:.0f}" if isinstance(d.get("x"), (int, float)) else ""
                    lines.append(f"- {d['zone_name']}" + (f" {d['sub_zone']}" if d.get("sub_zone") else "") + pos)
                    lines.append("- 狀態：" + ("休息區" if d.get("resting") else "野外")
                                 + ("、騎乘中" if d.get("mounted") else ""))
                section("目前位置", lines)
            elif s == "activity":
                lines = []
                if act.get("first_event_at"):
                    lines = [f"- 活動時間：{duration(act['active_seconds'])}"
                             f"（{local(act['first_event_at'])} 到 {local(act['last_event_at'])}）",
                             f"- 擊殺 {act['kills']} 隻，戰鬥 {act['fights']} 場共 {duration(act['combat_seconds'])}",
                             f"- 獲得經驗 {act['xp']}，拾取物品 {act['items_looted']} 件"
                             + (f"（賣給商店約值 {money(act['loot_value'])}）" if act.get("loot_value") else ""),
                             f"- 金錢收入 {money(act['money_in'])}，支出 {money(act['money_out'])}"]
                    c = act.get("counts") or {}
                    extra = [f"{label} {c[k]}" for k, label in (
                        ("LEVEL_UP", "升級"), ("QUEST_ACCEPTED", "接任務"), ("QUEST_COMPLETED", "完成任務"),
                        ("PLAYER_DIED", "死亡"), ("SKILL_UP", "技能提升")) if c.get(k)]
                    if extra:
                        lines.append("- " + "、".join(extra) + " 次")
                    xp, xp_max = d.get("xp"), d.get("xp_max")
                    if act["xp"] and act["active_seconds"] >= 300 and xp is not None and xp_max:
                        rate = act["xp"] * 3600 / act["active_seconds"]
                        lines.append(f"- 經驗效率每小時約 {rate:.0f}，照這速度約 "
                                     f"{duration((xp_max - xp) / rate * 3600)}後升級")
                section(f"活動統計（{range_name}）", lines, "這段時間沒有活動紀錄。")
            elif s == "quests":
                lines = []
                shown = []
                for qu in (d.get("quests") or []):
                    objs = [o.get("text") for o in (qu.get("objectives") or []) if o.get("text")]
                    if match(qu.get("title"), qu.get("category"), *objs):
                        shown.append((qu, objs))
                total, shown = len(shown), shown[:limit]
                turn_in = world.turn_in([qu["quest_id"] for qu, _ in shown
                                         if isinstance(qu.get("quest_id"), int)]) if world else {}
                for qu, objs in shown:
                    state = "已可交付" if qu.get("complete") else "進行中"
                    place = turn_in.get(qu.get("quest_id"))
                    lines.append(f"- {qu.get('title')}（{qu.get('level')} 級，{qu.get('category') or '未分類'}，"
                                 f"{state}）" + ("：" + "；".join(objs) if objs else "")
                                 + (f"。交給 {place}" if place else ""))
                section(f"任務日誌（{total} 個）", lines, "沒有符合的任務。")
            elif s == "todo":
                zone = None
                if world and world.available() and world.zone_quests and isinstance(d.get("map_id"), int):
                    zone = world.zone_quests(cid, d["map_id"])
                if not zone or not zone["items"]:
                    section("這一區還沒做的任務", [], "目前所在的區域沒有任務資料。")
                    continue
                items = zone["items"]
                done = sum(q["state"] == "completed" for q in items)
                todo = [q for q in items if q["state"] == "available" and match(q["name"])]
                lines = [f"- 這一區適合你的任務共 {len(items)} 個，已完成 {done} 個，"
                         f"進行中 {sum(q['state'] == 'in_log' for q in items)} 個，現在可接 {len(todo)} 個"]
                if not zone["known"]:
                    lines.append("- 注意：還沒有完整的已完成任務清單，下面可能包含你其實做過的任務。")
                for q in todo[:limit]:
                    start = q.get("start") or {}
                    lines.append(f"- {q['name']}（{q['level']} 級，約 {q['xp_now']} 經驗"
                                 + ("，因為等級差距只剩部分經驗" if q["xp_share"] < 1 else "")
                                 + (f"，找 {start['name']} 接" if start.get("name") else "") + "）")
                section(f"{zone['zone'] or '這一區'}還沒做的任務（經驗多的在前）", lines)
            elif s == "bags":
                merged: Dict[Any, Dict[str, Any]] = {}
                for it in ((d.get("bags") or {}).get("items") or []):
                    m = merged.setdefault(it.get("item_id"), {"name": it.get("name"), "count": 0, "value": 0,
                                                              "quality": it.get("quality")})
                    n = it.get("count") or 1
                    m["count"] += n
                    m["value"] += (it.get("sell_price") or 0) * n
                rows = sorted((m for m in merged.values() if match(m["name"])),
                              key=lambda m: (-m["value"], -m["count"]))
                bags = d.get("bags") or {}
                total = sum(m["value"] for m in rows)
                junk = sum(m["value"] for m in rows if m["quality"] == 0)
                lines = [f"- {m['name']} x{m['count']}" + (f"，值 {money(m['value'])}" if m["value"] else "")
                         for m in rows[:limit]]
                if total:      # vendor prices are only known once the updated addon has scanned the bags
                    lines.insert(0, f"- 全部賣給商店約值 {money(total)}"
                                 + (f"，其中灰色垃圾 {money(junk)}" if junk else ""))
                section(f"背包（空位 {bags.get('free', '?')}/{bags.get('slots', '?')}，{len(rows)} 種物品）",
                        lines, "沒有符合的物品。")
            elif s == "gear":
                eq = d.get("equipment") or []
                ids = [e["item_id"] for e in eq if isinstance(e.get("item_id"), int)]
                names = world.item_names(ids) if world else {}
                lines = []
                for e in sorted(eq, key=lambda e: e.get("slot") or 0):
                    label = e.get("name") or names.get(e.get("item_id")) or f"物品編號 {e.get('item_id')}"
                    more = [f"物品等級 {e['item_level']}" if e.get("item_level") else None,
                            e.get("item_subtype"), stat_text(e.get("stats")) or None,
                            f"耐久 {e['durability']}/{e['durability_max']}" if e.get("durability_max") else None,
                            f"賣店 {money(e['sell_price'])}" if e.get("sell_price") else None]
                    lines.append(f"- {SLOTS.get(e.get('slot'), e.get('slot'))}：{label}"
                                 + "".join("，" + m for m in more if m))
                if eq and not any(e.get("stats") for e in eq):
                    lines.append("- 還沒有屬性資料：更新插件後重新登入並存檔一次就會有。")
                section("裝備", lines)
            elif s == "compare":
                info = build_items(d, world.item_names(
                    [e["item_id"] for e in d.get("equipment") or [] if isinstance(e.get("item_id"), int)]) if world else {})
                lines = []
                for c in info["comparisons"]:
                    it, cur = c["item"], c["equipped"]
                    if not match(it["name"], c["slot_name"]):
                        continue
                    head = f"- {it['name']}（{c['slot_name']}" + (f"，物品等級 {it['item_level']}" if it.get("item_level") else "") \
                        + (f"，{it['subtype']}" if it.get("subtype") else "") + "）"
                    if c["usable"] is False:
                        lines.append(head + f"：不能裝備，{c['note']}")
                        continue
                    if not cur:
                        body = "這個部位目前是空的" + ("，屬性：" + stat_text(it["stats"]) if it.get("stats") else "")
                    elif c["stat_diff"] is None:
                        body = f"對比身上的 {cur['name']}：沒有屬性資料可以比較"
                    elif not c["stat_diff"]:
                        body = f"對比身上的 {cur['name']}：屬性相同"
                    else:
                        body = f"換掉身上的 {cur['name']} 會變成：{diff_text(c['stat_diff'])}"
                    lines.append(head + "：" + body + (f"。{c['note']}" if c["note"] else ""))
                if lines and not info["has_stats"]:
                    lines.append("- 還沒有屬性資料：更新插件後重新登入並存檔一次，才能比較屬性。")
                section("背包裡可以裝備的東西（和身上的比較）", lines[:limit], "背包裡沒有可以裝備的東西。")
            elif s == "skills":
                section("專業技能", [f"- {p.get('name')} {p.get('rank')}/{p.get('max_rank')}"
                                 for p in (d.get("professions") or []) if match(p.get("name"))])
            elif s == "rep":
                lines = []
                for f in (d.get("reputations") or []):
                    if not match(f.get("name")):
                        continue
                    span = (f.get("ceiling") or 0) - (f.get("floor") or 0)
                    prog = f" {(f.get('value') or 0) - (f.get('floor') or 0)}/{span}" if span > 0 else ""
                    lines.append(f"- {f.get('name')}：{STANDING.get(f.get('standing'), f.get('standing'))}{prog}")
                section("聲望", lines[:limit])
            elif s == "stats":
                st = d.get("stats") or {}
                section("屬性", [f"- {label} {st[k]}" for k, label in STAT_NAMES.items() if k in st])
            elif s == "flights":
                fp = {k: v for k, v in (d.get("flight_points") or {}).items()
                      if not k.startswith("zz") and match(k)}
                known = [k for k, v in fp.items() if v]
                section(f"飛行點（已開啟 {len(known)}/{len(fp)}）",
                        ["- 已開啟：" + "、".join(known)] if known else [],
                        "尚無紀錄。在遊戲中打開一次飛行地圖即可記錄。")
            elif s in ("kills", "loot"):
                rows = [x for x in act.get("top_monsters" if s == "kills" else "top_items", [])
                        if match(x.get("name"))][:limit]
                unit = "隻" if s == "kills" else "件"
                section(f"{'擊殺' if s == 'kills' else '拾取'}排行（{range_name}）",
                        [f"- {x.get('name') or x.get('id')} {x['count']} {unit}"
                         + (f"，值 {money(x['value'])}" if x.get("value") else "") for x in rows],
                        "這段時間沒有紀錄。")
            elif s == "events":
                where, args = ["character_id=?", "event_type NOT IN ('LOCATION_SAMPLE','CHAT_MESSAGE','PARTY_POSITIONS','GROUP_METER','GROUP_ENCOUNTER')"], [cid]
                if type:
                    where.append("event_type=?")
                    args.append(type.strip().upper())
                for col, val in ((">=", start), ("<", end)):
                    if val is not None:
                        where.append(f"timestamp{col}?")
                        args.append(val.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                with db() as conn:
                    rows = conn.execute(
                        "SELECT event_type, timestamp, payload_json FROM events WHERE "
                        + " AND ".join(where) + " ORDER BY timestamp DESC, event_id DESC LIMIT ?",
                        args + [limit if not kw else 1000]).fetchall()
                lines = []
                for row in rows:
                    line = describe(row["event_type"], json.loads(row["payload_json"]))
                    if match(line):
                        lines.append(f"- {local(row['timestamp'], '%m/%d %H:%M')} {line}")
                section(f"事件紀錄（{range_name}，由新到舊）", lines[:limit], "這段時間沒有符合的事件。")
        return text("\n".join(out) + "\n")
