from datetime import datetime, timedelta, timezone

from conftest import AUTH, READ_TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
GID = "inst:g1"


def seed(client, cid="c1"):
    snap = {"captured_at": T0.isoformat(), "device_id": "mac",
            "data": {"name": "小德", "level": 22, "class": "DRUID", "race": "NightElf"}}
    client.put(f"/api/v1/characters/{cid}/snapshot", json=snap, headers=AUTH)
    pos = {"map_id": 1436, "x": 40.0, "y": 50.0, "zone_name": "西部荒野"}
    rows = [
        (-10, "MOB_KILLED", {**pos, "monster_name": "組隊前的怪"}),
        (0, "GROUP_JOINED", {**pos, "group_id": GID, "size": 2, "leader": True, "raid": False,
                             "members": [{"name": "艾莉", "class": "PRIEST", "level": 23}]}),
        (1, "LOCATION_SAMPLE", pos),
        (2, "GROUP_MEMBER_JOINED", {"group_id": GID, "name": "波波", "class": "MAGE", "level": 21}),
        (3, "MOB_KILLED", {**pos, "monster_id": 1, "monster_name": "迪菲亞盜賊"}),
        (4, "MOB_KILLED", {**pos, "monster_id": 1, "monster_name": "迪菲亞盜賊", "killed_by": "波波"}),
        (5, "XP_GAINED", {"amount": 300}),
        (6, "ITEM_LOOTED", {"item_id": 2, "item_name": "亞麻布", "count": 3, "sell_price": 13}),
        (7, "CHAT_MESSAGE", {"message": "秘密", "channel_type": "party"}),
        (8, "INSTANCE_ENTERED", {"name": "死亡礦坑"}),
        (9, "PLAYER_DIED", pos),
        (10, "LOCATION_SAMPLE", {**pos, "x": 42.0}),
        (12, "GROUP_MEMBER_LEFT", {"group_id": GID, "name": "艾莉"}),
        (20, "GROUP_LEFT", {**pos, "group_id": GID, "duration": 1200}),
        (30, "MOB_KILLED", {**pos, "monster_name": "組隊後的怪"}),
    ]
    events = [{"event_id": f"{cid}:{i:04d}", "character_id": cid, "event_type": t,
               "timestamp": (T0 + timedelta(minutes=m)).isoformat(), "payload": p}
              for i, (m, t, p) in enumerate(rows)]
    r = client.post("/api/v1/events/batch", headers=AUTH,
                    json={"schema_version": 1, "device_id": "mac", "events": events})
    assert r.json()["accepted"] == len(rows)


def test_group_list_and_share_page(client):
    seed(client)
    items = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"]
    assert len(items) == 1
    g = items[0]
    assert (g["group_id"], g["ongoing"], g["duration"], g["kills"], g["xp"]) == (GID, False, 1200, 2, 300)
    assert [(m["name"], m["class"], m["left"]) for m in g["members"]] == [("艾莉", "PRIEST", True), ("波波", "MAGE", False)]
    tok = g["token"]
    assert len(tok) == 12
    assert client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]["token"] == tok   # stable

    d = client.get(f"/api/v1/shared/party/{tok}").json()          # no login needed with the link
    assert d["owner"] == {"name": "小德", "full_name": None, "class": "DRUID", "level": 22} and d["full_names"] == {}
    assert d["stats"]["kills"] == 2 and d["stats"]["xp"] == 300 and d["stats"]["loot_value"] == 39
    assert d["kills_by"] == [{"name": "小德", "kills": 1, "monsters": [{"name": "迪菲亞盜賊", "count": 1}]},
                             {"name": "波波", "kills": 1, "monsters": [{"name": "迪菲亞盜賊", "count": 1}]}]
    assert d["deaths"] == 1 and d["instances"] == ["死亡礦坑"] and d["zones"] == ["西部荒野"]
    assert d["maps"] == [{"map_id": 1436, "name": "西部荒野", "has_image": False, "members": {},
                          "points": [["2026-11-22T12:01:00Z", 40.0, 50.0], ["2026-11-22T12:10:00Z", 42.0, 50.0]]}]
    text = str(d)
    assert "組隊前的怪" not in text and "組隊後的怪" not in text       # only the session's own window
    assert "秘密" not in text and "CHAT_MESSAGE" not in text          # never any chat
    assert [e["type"] for e in d["timeline"]][:3] == ["GROUP_JOINED", "GROUP_MEMBER_JOINED", "MOB_KILLED"]

    page = client.get(f"/party/{tok}")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "小德" not in page.text
    assert client.get("/api/v1/shared/party/wrongwrong12").status_code == 404
    assert client.get(f"/api/v1/shared/party/{tok}/map/1436").status_code == 404      # no image uploaded
    # the party link opens nothing else
    assert client.get("/api/v1/events", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    assert client.get(f"/voice?t={tok}").status_code == 404


def test_shared_map_only_for_maps_of_that_session(client):
    seed(client)
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 64
    for m in (1436, 1429):
        client.put(f"/api/v1/maps/{m}/image", content=jpeg, headers=AUTH)
    tok = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]["token"]
    assert client.get(f"/api/v1/shared/party/{tok}/map/1436").content == jpeg
    assert client.get(f"/api/v1/shared/party/{tok}/map/1429").status_code == 404
    assert client.get(f"/api/v1/shared/party/{tok}").json()["maps"][0]["has_image"] is True


def test_unfinished_group_runs_to_the_latest_data(client):
    seed(client, "c2")
    late = {"event_id": "c2:9000", "character_id": "c2", "event_type": "GROUP_JOINED",
            "timestamp": (T0 + timedelta(minutes=40)).isoformat(),
            "payload": {"group_id": "inst:g2", "members": [{"name": "新隊友"}]}}
    kill = {"event_id": "c2:9001", "character_id": "c2", "event_type": "MOB_KILLED",
            "timestamp": (T0 + timedelta(minutes=55)).isoformat(), "payload": {"monster_name": "野豬"}}
    client.post("/api/v1/events/batch", headers=AUTH,
                json={"schema_version": 1, "device_id": "mac", "events": [late, kill]})
    items = client.get("/api/v1/groups?character_id=c2", headers=READ).json()["items"]
    assert [i["group_id"] for i in items] == ["inst:g2", GID]              # newest first
    assert items[0]["ongoing"] is True and items[0]["duration"] == 900 and items[0]["kills"] == 1


def test_group_list_respects_the_character_share_token(client):
    seed(client, "c1")
    seed(client, "c2")
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert len(client.get("/api/v1/groups?character_id=c1", headers=S).json()["items"]) == 1
    assert client.get("/api/v1/groups?character_id=c2", headers=S).status_code == 403
    assert client.get("/api/v1/groups?character_id=c1").status_code == 401


def test_party_page_data_from_members(client):
    seed(client)
    extra = [
        (3, "PARTY_LOOT", {"looter": "波波", "item_id": 9, "item_name": "綠色法杖", "count": 1, "sell_price": 500}),
        (3, "PARTY_LOOT", {"looter": "波波", "item_id": 2, "item_name": "亞麻布", "count": 4, "sell_price": 13}),
        (4, "PARTY_MEMBER_DIED", {"name": "艾莉", "map_id": 1436, "x": 41.0, "y": 50.0}),
        (5, "GROUP_METER", {"group_id": GID, "damage": {"": 300, "波波": 500}, "healing": {"艾莉": 250}}),
        (6, "GROUP_METER", {"group_id": GID, "damage": {"": 100}, "healing": {}, "taken": {"": 80, "波波": 20}, "fight": 40,
                            "spells": {"": {"月火術": 70, "近戰": 30}, "波波": {"射擊": 9}},
                            "best": {"": [70, "月火術", "迪菲亞盜賊", True], "波波": "bad"},
                            "hits": {"": [10, 4], "波波": [1, "x"]}, "cc": {"艾莉": [2, 1]}, "over": {"艾莉": 50},
                            "hurt": {"迪菲亞盜賊｜近戰": 70, "迪菲亞法師｜火球術": 30}}),
        (6, "PARTY_POSITIONS", {"group_id": GID, "map_id": 1436, "members": {"波波": {"x": 41.5, "y": 50.5}}}),
        (7, "PARTY_POSITIONS", {"group_id": GID, "map_id": 1436, "members": {"波波": {"x": 42.5, "y": 50.5}}}),
        (8, "GROUP_MEMBER_LEVEL_UP", {"group_id": GID, "name": "波波", "level": 22}),
        (9, "QUEST_COMPLETED", {"quest_id": 5, "quest_name": "清理荒野", "xp_reward": 900, "money_reward": 600}),
        (9, "SKILL_UP", {"text": "你的剝皮技能提高到了79。"}),
        (10, "SKILL_UP", {"text": "你的剝皮技能提高到了80。"}),
        (25, "PARTY_LOOT", {"looter": "波波", "item_id": 9, "item_name": "組隊後的東西", "count": 1}),
        (4, "PARTY_LOOT", {"looter": "波波", "item_id": 8, "item_name": "買來的子彈", "count": 200, "how": "received"}),
        (4, "ITEM_LOOTED", {"item_id": 7, "item_name": "自己做的繃帶", "count": 20, "sell_price": 10, "how": "created"}),
        (7, "GROUP_ENCOUNTER", {"name": "范克里夫", "success": True, "duration": 95,
                                "damage": {"": 40, "波波": 60}, "healing": {"艾莉": 30}, "taken": {"": 50}}),
        (4, "ITEM_LOOTED", {"item_id": 9, "item_name": "戰利品", "count": 1, "sell_price": 500}),      # a roll, not a pick-up
    ]
    events = [{"event_id": f"x:{i:04d}", "character_id": "c1", "event_type": t,
               "timestamp": (T0 + timedelta(minutes=m, seconds=i)).isoformat(), "payload": p}
              for i, (m, t, p) in enumerate(extra)]
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": events})
    # rows from the combat log file: one per hit or miss
    t = int((T0 + timedelta(minutes=5)).timestamp() * 1000)
    hit = lambda pos, who, spell, amount, crit=0, miss=None, pet=0, raw=None, at=t: [     # noqa: E731
        pos, at, "out", who, pet, "x", "迪菲亞盜賊", 589, 1, spell, 1, "range", amount, raw, -1, 0, 0, 0, crit, 0, 0, miss,
        None, None, None, None, None]
    rows = [hit(1, "", "月火術", 100, raw=125), hit(2, "", "月火術", 140, raw=175), hit(3, "", "月火術", 300, 1, raw=375),
            hit(4, "", "月火術", 0, miss="RESIST"), hit(5, "波波", "射擊", 80), hit(6, "波波", "爪擊", 30, pet=1),
            hit(7, "", "月火術", 999, at=t + 3600_000)]                               # after the group ended
    assert client.post("/api/v1/combat/hits", json={"character_id": "c1", "source": "log.txt", "rows": rows}).status_code == 401
    post = lambda r: client.post("/api/v1/combat/hits", headers=AUTH,                 # noqa: E731
                                 json={"character_id": "c1", "source": "log.txt", "rows": r})
    assert post(rows).json()["accepted"] == 7 and post(rows).json()["changes"] == 7      # sent again: replaced, not doubled
    assert post([rows[0][:5]]).status_code == 422
    tok = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]["token"]
    d = client.get(f"/api/v1/shared/party/{tok}").json()
    moon, shot, claw = d["skills"]
    assert (moon["who"], moon["spell"], moon["attempts"], moon["hits"], moon["missed"], moon["total"]) == ("小德", "月火術", 4, 3, 1, 540)
    assert (moon["min"], moon["max"], moon["avg"], moon["median"]) == (100, 300, 180.0, 140)
    assert moon["normal"] == {"n": 2, "min": 100, "max": 140, "avg": 120.0, "total": 240}
    assert moon["crit"] == {"n": 1, "min": 300, "max": 300, "avg": 300.0, "total": 300}
    assert moon["misses"] == [{"type": "RESIST", "name": "抵抗", "count": 1}] and moon["reduced"] == 0.2
    assert (shot["who"], shot["pet"], shot["reduced"], claw["spell"], claw["pet"]) == ("波波", False, None, "爪擊", True)
    base = f"/api/v1/shared/party/{tok}"
    assert post([hit(8, "", "月火術", 60) [:6] + ["迪菲亞法師", 590] + hit(8, "", "月火術", 60)[8:]]).json()["accepted"] == 1
    r = client.get(base + "/skills").json()
    assert [(e["name"], e["damage"], e["hits"]) for e in r["enemies"]] == [("迪菲亞盜賊", 650, 6), ("迪菲亞法師", 60, 1)]
    one = client.get(base + "/skills", params={"enemy": "迪菲亞法師"}).json()["skills"]
    assert [(k["who"], k["total"], k["hits"]) for k in one] == [("小德", 60, 1)]
    am = client.get(base + "/amounts", params={"spell": "月火術"}).json()["members"]
    assert am == [{"who": "小德", "normal": [100, 140, 60], "crit": [300]}]
    assert client.get(base + "/amounts", params={"spell": "爪擊", "pet": "true"}).json()["members"][0]["who"] == "波波"
    line = client.get(base + "/dps", params={"start": t, "end": t + 9000, "step": 5}).json()
    assert (line["start"], line["step"], line["count"]) == (t, 5, 2)
    assert line["series"]["小德"]["own"] == [600, 0] and line["series"]["波波"] == {"own": [80, 0], "pet": [30, 0]}
    assert client.get(base + "/dps", params={"step": 1}).json()["count"] <= 1501          # widened to stay drawable
    ev = client.get(base + "/hits", params={"limit": 4}).json()["items"]
    assert len(ev) == 4 and ev[3]["miss"] == "抵抗" and ev[0]["who"] == "小德" and ev[0]["spell"] == "月火術"
    assert post([hit(9, "=cmd()", "@x", 1)]).json()["accepted"] == 1               # a name shaped like a formula
    sheet = client.get(base + "/hits.csv")
    assert "'=cmd()" in sheet.text and "'@x" in sheet.text and ",-1.0," in sheet.text   # text is defused, numbers are not
    assert (sheet.headers["referrer-policy"], sheet.headers["x-content-type-options"], sheet.headers["x-frame-options"]) == \
        ("no-referrer", "nosniff", "DENY")
    assert sheet.headers["content-type"].startswith("text/csv") and len(sheet.text.strip().splitlines()) == 9
    assert "999" not in sheet.text                                # nothing from outside the session
    assert client.get("/api/v1/shared/party/nope/skills").status_code == 404

    # ---- the same session for a tool: asked for by character, with a read token, in small pieces
    mine = "/api/v1/characters/c1/groups"
    assert client.get(mine).status_code == 401
    listed = client.get(mine, headers=READ).json()["items"]
    gid = listed[0]["group_id"]
    assert listed[0]["page"] == f"/party/{tok}" and listed[0]["kills"] == d["stats"]["kills"]
    assert client.get(mine, params={"limit": 1}, headers=READ).json()["items"] == listed[:1]
    one = client.get(f"{mine}/{gid}", headers=READ).json()
    assert one["kills_by"] == d["kills_by"] and [m["name"] for m in one["meter"]["members"]] == [m["name"] for m in d["meter"]["members"]]
    assert one["omitted"] == ["route", "timeline", "skills", "charts", "screenshots"]
    assert not {"maps", "marks", "timeline", "skills", "xp_points", "damage_minutes", "kill_times", "screenshots"} & set(one)
    # per-second figures and shares are worked out: 40 seconds of fighting in this session
    top = one["meter"]["members"][0]
    assert (top["name"], top["damage"], top["dps"], top["damage_pct"]) == ("波波", 500, 12.5, 55.6) and one["meter"]["group_dps"] == 22.5
    assert one["meter"]["members"][1]["crit_pct"] == 40.0 and one["meter"]["members"][2]["hps"] == 6.2
    fight = one["encounters"][0]
    assert fight["group_dps"] == round(100 / 95, 1) and fight["damage"][0] == {"name": "波波", "amount": 60, "dps": 0.6, "pct": 60.0}
    assert client.get(f"{mine}/latest", headers=READ).json()["group_id"] == gid          # "latest" stands for the newest
    more = client.get(f"{mine}/{gid}", params={"include": "route,timeline"}, headers=READ).json()
    assert more["maps"] == d["maps"] and more["timeline"] == d["timeline"] and more["omitted"] == ["skills", "charts", "screenshots"]
    assert client.get(f"{mine}/{gid}", params={"include": "all"}, headers=READ).json()["omitted"] == []
    assert client.get(f"{mine}/{gid}", params={"include": "nonsense"}, headers=READ).status_code == 400
    assert client.get(f"{mine}/nope", headers=READ).status_code == 404
    sk = client.get(f"{mine}/{gid}/skills", headers=READ).json()
    assert [(k["who"], k["spell"]) for k in sk["skills"]][:3] == [("小德", "月火術"), ("波波", "射擊"), ("波波", "爪擊")]
    assert [e["name"] for e in sk["enemies"]] == ["迪菲亞盜賊", "迪菲亞法師"]
    assert [k["spell"] for k in client.get(f"{mine}/{gid}/skills", params={"member": "波波"}, headers=READ).json()["skills"]] == ["射擊", "爪擊"]
    assert len(client.get(f"{mine}/{gid}/skills", params={"spell": "月火術", "enemy": "迪菲亞法師"}, headers=READ).json()["skills"]) == 1
    assert len(client.get(f"{mine}/{gid}/skills", params={"limit": 1}, headers=READ).json()["skills"]) == 1
    assert (sk["skills"][0]["hit_pct"], sk["skills"][0]["crit_pct"], sk["skills"][0]["pct_of_member"]) == (80.0, 25.0, 100.0)
    # one boss fight up close, by number or by name; or the whole session
    boss = client.get(f"{mine}/{gid}/fight", params={"boss": "1", "log": 5}, headers=READ).json()
    assert boss["fight"]["boss"] == "范克里夫" and boss["fight"]["number"] == 1 and boss["fight"]["seconds"] == 95
    assert [(m["name"], m["damage"], m["dps"], m["damage_pct"]) for m in boss["members"]] == [("波波", 60, 0.6, 60.0), ("小德", 40, 0.4, 40.0), ("艾莉", 0, 0.0, None)]
    assert boss["members"][2]["healing"] == 30 and boss["dps_over_time"]["step_seconds"] == 5
    assert client.get(f"{mine}/{gid}/fight", params={"boss": "范克里夫"}, headers=READ).json()["fight"]["number"] == 1
    lost = client.get(f"{mine}/{gid}/fight", params={"boss": "不存在"}, headers=READ)
    assert lost.status_code == 404 and "1. 范克里夫" in lost.json()["detail"]
    whole = client.get(f"{mine}/{gid}/fight", params={"log": 3}, headers=READ).json()
    assert whole["fight"]["boss"] is None and whole["members"][0]["name"] == "波波" and len(whole["log"]) == 3
    assert whole["skills"][0]["spell"] == "月火術" and "小德" in whole["dps_over_time"]["series"] and "at" in whole["log"][0]
    an = client.get(f"{mine}/{gid}/analysis", headers=READ).json()
    assert set(an) == {"group_id", "defence", "level_gaps", "dots", "guard", "buffs", "heals", "activity", "pets", "autoshot", "casts"}
    assert an["defence"][0]["name"] == "迪菲亞盜賊"
    part = client.get(f"{mine}/{gid}/analysis", params={"parts": "defence,activity", "limit": 1}, headers=READ).json()
    assert set(part) == {"group_id", "defence", "activity"} and len(part["defence"]) == 1
    assert client.get(f"{mine}/{gid}/analysis", params={"parts": "magic"}, headers=READ).status_code == 400
    # combat outside a session: any stretch of time
    when = {"start_time": T0.isoformat(), "end_time": (T0 + timedelta(hours=3)).isoformat()}
    wide = client.get("/api/v1/characters/c1/combat/skills", params=when, headers=READ).json()["skills"]
    assert wide[0]["total"] == 999 + 600 and wide[0]["spell"] == "月火術"             # includes the hit after the group ended
    assert client.get("/api/v1/characters/c1/combat/skills", headers=READ).json()["skills"] == []   # nothing in the last day
    assert client.get("/api/v1/characters/c1/combat/skills", params={"start_time": "2026-11-22T12:00:00"}, headers=READ).status_code == 400
    assert client.get("/api/v1/characters/c1/combat/analysis", params={**when, "parts": "defence"}, headers=READ).json()["defence"][0]["hits"] >= 6
    # the description a tool reads: every endpoint filed and explained
    api = client.get("/openapi.json").json()
    assert "character_id" in api["info"]["description"] and [t["name"] for t in api["tags"]][1] == "組隊 Groups"
    ops = {(m, p): o for p, methods in api["paths"].items() for m, o in methods.items()}
    assert ops[("get", "/api/v1/characters/{character_id}/groups/{group_id}/analysis")]["tags"] == ["組隊 Groups"]
    assert ops[("get", "/api/v1/characters/{character_id}/combat/skills")]["tags"] == ["戰鬥分析 Combat"]
    assert ops[("post", "/api/v1/events/batch")]["tags"] == ["上傳 Uploads"] and ops[("get", "/api/v1/world/items")]["tags"] == ["世界資料 World"]
    assert not [key for key, o in ops.items() if key[0] == "get" and not o.get("description")]      # nothing left unexplained

    loot = d["member_loot"]
    assert [(m["name"], m["count"], m["value"]) for m in loot] == [("波波", 5, 552)]
    assert [i["name"] for i in loot[0]["items"]] == ["綠色法杖", "亞麻布"]            # most valuable first
    assert "組隊後的東西" not in str(d)
    assert "買來的子彈" not in str(d["member_loot"]) and d["stats"]["items_looted"] == 3    # bought or crafted is not loot
    assert [m["level"] for m in d["members"] if m["name"] == "波波"] == [22]                # the level they ended on
    assert d["damage"] == [{"name": "波波", "amount": 500}, {"name": "小德", "amount": 400}]   # "" is the recorder
    assert d["healing"] == [{"name": "艾莉", "amount": 250}]
    assert {x["name"]: x["deaths"] for x in d["deaths_by"]} == {"小德": 1, "艾莉": 1}
    assert d["maps"][0]["members"] == {"波波": [["2026-11-22T12:06:05Z", 41.5, 50.5], ["2026-11-22T12:07:06Z", 42.5, 50.5]]}
    assert d["quests"] == [{"name": "清理荒野", "xp": 900, "money": 600}]
    assert d["skill_ups"] == ["你的剝皮技能提高到了80。"]                           # the latest line per skill
    kinds = [h["type"] for h in d["highlights"]]
    assert kinds.count("PARTY_MEMBER_DIED") == 1 and "GROUP_MEMBER_LEVEL_UP" in kinds and "PLAYER_DIED" in kinds
    assert d["pace"]["kills_per_hour"] == 6.0 and d["pace"]["xp_per_hour"] == 900
    assert d["xp_points"] == [["2026-11-22T12:05:00Z", 300]]
    m = d["meter"]
    assert m["fight_seconds"] == 40 and [x["name"] for x in m["members"]] == ["波波", "小德", "艾莉"]
    me_row = m["members"][1]
    assert (me_row["damage"], me_row["taken"], me_row["spells"][0]) == (400, 80, {"name": "月火術", "amount": 70})
    assert me_row["best"] == {"amount": 70, "spell": "月火術", "target": "迪菲亞盜賊", "crit": True}
    assert m["members"][0]["best"] is None and m["members"][2]["healing"] == 250
    assert (me_row["hits"], me_row["crits"], m["members"][0]["hits"]) == (10, 4, 0)
    assert (m["members"][2]["dispels"], m["members"][2]["interrupts"], m["members"][2]["overheal"]) == (2, 1, 50)
    assert m["threats"] == [{"enemy": "迪菲亞盜賊", "spell": "近戰", "amount": 70}, {"enemy": "迪菲亞法師", "spell": "火球術", "amount": 30}]
    assert len(d["kill_times"]) == d["stats"]["kills"]
    assert [list(x[1].items()) for x in d["damage_minutes"]] == [[("小德", 300), ("波波", 500)], [("小德", 100)]]
    boss = d["encounters"][0]
    assert (boss["name"], boss["success"], boss["duration"]) == ("范克里夫", True, 95)
    assert boss["damage"] == [{"name": "波波", "amount": 60}, {"name": "小德", "amount": 40}]
    types = {e["type"] for e in d["timeline"]}
    assert "GROUP_ENCOUNTER" not in types and not [e for e in d["timeline"] if e.get("item_name") == "戰利品"]
    assert not types & {"PARTY_POSITIONS", "GROUP_METER", "PARTY_LOOT", "CHAT_MESSAGE"}     # too noisy for a timeline
    # the noisy kinds can be left out of event lists, several at once
    r = client.get("/api/v1/events?character_id=c1&exclude_type=LOCATION_SAMPLE,PARTY_POSITIONS,GROUP_METER&limit=1000",
                   headers=READ).json()["items"]
    assert not {e["event_type"] for e in r} & {"LOCATION_SAMPLE", "PARTY_POSITIONS", "GROUP_METER"}
    assert "PARTY_LOOT" in {e["event_type"] for e in r}
    # the player's own totals never include what members picked up
    act = client.get("/api/v1/stats/activity?character_id=c1", headers=READ).json()
    assert act["items_looted"] == 3


def test_research_on_targets_pets_and_costs(client):
    client.put("/api/v1/characters/c1/snapshot", headers=AUTH, json={
        "captured_at": T0.isoformat(), "device_id": "mac", "data": {"name": "小德", "class": "DRUID", "level": 24}})
    ev = [(0, "GROUP_JOINED", {"group_id": GID, "members": [{"name": "波波", "class": "HUNTER", "level": 22}]}),
          (30, "GROUP_LEFT", {"group_id": GID, "duration": 1800})]
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": [
        {"event_id": f"r:{i}", "character_id": "c1", "event_type": t, "timestamp": (T0 + timedelta(minutes=m)).isoformat(), "payload": p}
        for i, (m, t, p) in enumerate(ev)]})
    t = int((T0 + timedelta(minutes=5)).timestamp() * 1000)
    n = [0]

    def row(way, who, kind, spell, amount, at, *, pet=0, crit=0, glancing=0, miss=None, raw=None, overkill=-1, blocked=0,
            armor=None, level=None, guid="Creature-0-1-0-1-589-AAA", enemy="迪菲亞盜賊", unit="x", spell_id=1, school=1):
        n[0] += 1
        return [n[0], t + at, way, who, pet, unit, enemy, 589, spell_id, spell, school, kind, amount, raw, overkill, 0, blocked, 0,
                crit, glancing, 0, miss, None, None, armor, level, None, guid]
    rows = [
        row("out", "波波", "range", "自動射擊", 80, 0, raw=100, armor=400, level=24, spell_id=75),
        row("out", "波波", "range", "自動射擊", 90, 2000, raw=100, armor=300, level=24, spell_id=75),
        row("out", "", "swing", "近戰", 40, 2500, raw=50),
        row("out", "", "swing", "近戰", 0, 3000, miss="DODGE"),
        row("out", "", "swing", "近戰", 30, 3500, raw=50, glancing=1),
        row("out", "", "swing", "近戰", 90, 4000, raw=100, crit=1, blocked=5, overkill=12),          # the killing blow
        row("out", "", "spell", "月火術", 60, 4100, raw=60, school=64, guid="Creature-0-1-0-1-589-BBB", level=25, armor=400),
        row("out", "波波", "swing", "近戰", 20, 1000, pet=1, unit="小白"),
        row("in", "波波", "swing", "近戰", 15, 1500, pet=1, unit="小白"),
        row("cast", "", "0", "月火術", 30, 4000, raw=1500),
        row("cast", "", "1", "槌擊", 150, 4000),
        # a bleed that was up for 3 of the 4 seconds its target lived, and ticked twice
        row("aura", "", "APPLIED", "掃擊", None, 1000, miss="DEBUFF"), row("aura", "", "REFRESH", "掃擊", None, 2000, miss="DEBUFF"),
        row("aura", "", "REMOVED", "掃擊", None, 4000, miss="DEBUFF"),
        row("out", "", "dot", "掃擊", 12, 2000), row("out", "", "dot", "掃擊", 14, 3000),
        # a buff on a member for 2 of the seconds they were seen, and one never seen to end
        row("aura", "波波", "APPLIED", "野性印記", None, 1000, miss="BUFF", enemy=None, unit="小德-伺服器"),
        row("aura", "波波", "REMOVED", "野性印記", None, 3000, miss="BUFF", enemy=None, unit="小德-伺服器"),
        row("aura", "波波", "APPLIED", "強擊光環", None, 2000, miss="BUFF", enemy=None, unit="波波-伺服器"),
        row("death", "波波", None, None, None, 5000, pet=1, unit="小白", enemy=None),
        row("in", "", "swing", "近戰", 50, 2000), row("in", "", "swing", "近戰", 0, 2100, miss="DODGE"),
        row("in", "", "swing", "近戰", 30, 2200, blocked=10), row("in", "", "spell", "火球術", 80, 2300, crit=1),
        row("heal", "", "heal", "治療之觸", 70, 3000, raw=100, crit=1), row("heal", "", "hot", "回春術", 20, 3500, raw=20),
    ]
    rows += [row("out", "波波", "range", "自動射擊", 80, 10000 + at, spell_id=75) for at in [k * 2000 for k in range(22)] + [44600]]
    rows += [row("out", "波波", "swing", "近戰", 20, 20000 + i * 1500, pet=1, unit="小白") for i in range(20)]
    assert client.post("/api/v1/combat/hits", headers=AUTH, json={"character_id": "c1", "source": "l", "rows": rows}).status_code == 200
    assert client.post("/api/v1/combat/hits", headers=AUTH,                     # the shape older agents send
                       json={"character_id": "c2", "source": "old", "rows": [rows[0][:27]]}).status_code == 200
    tok = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]["token"]
    r = client.get(f"/api/v1/shared/party/{tok}/research").json()
    foe, = r["defence"]
    assert (foe["name"], foe["level"], foe["units"], foe["kills"], foe["ttk"]["avg"]) == ("迪菲亞盜賊", "24–25", 2, 1, 4.0)
    assert foe["armor"] == {"min": 300, "max": 400, "median": 400, "n": 3}
    melee = {x["key"]: x for x in foe["types"]["melee"]["results"]}
    assert foe["types"]["melee"]["attempts"] == 4 and melee["DODGE"]["pct"] == 25.0 and melee["DODGE"]["ci"][0] < 25 < melee["DODGE"]["ci"][1]
    assert {x["key"] for x in foe["types"]["melee"]["results"]} == {"normal", "crit", "glancing", "DODGE"}
    assert foe["types"]["pet"]["attempts"] == 21 and foe["block"] == {"n": 1, "of": 29, "pct": 3.4, "ci": foe["block"]["ci"]}
    physical = next(x for x in foe["schools"] if x["school"] == "物理")
    assert physical["reduced"] == round(1 - (80 + 90 + 40) / 250, 3) and physical["n"] == 3     # glancing and critical hits are left out
    assert [m["who"] for m in foe["members"]] == ["波波", "小德"]
    gap = {(g["type"], g["gap"]): g for g in r["level_gaps"]}
    assert gap[("ranged", 2)]["attempts"] == 25 and gap[("melee", 0)]["dodge"]["n"] == 1 and gap[("spell", 1)]["attempts"] == 1
    pet, = r["pets"]
    assert (pet["who"], pet["pet"], pet["damage"], pet["taken"], pet["swing_seconds"], pet["melee"]["avg"]) == ("波波", "小白", 420, 15, 1.5, 20.0)
    shots, = r["autoshot"]
    assert (shots["who"], shots["cycle"], shots["late"]["n"]) == ("波波", 2.0, 1) and 0.98 < shots["efficiency"] < 1
    bleed, = r["dots"]
    assert (bleed["who"], bleed["spell"], bleed["applied"], bleed["targets"], bleed["up_seconds"], bleed["ticks"], bleed["tick_damage"]) == \
        ("小德", "掃擊", 2, 1, 3, 2, 26)
    assert bleed["coverage"] == round(3000 / 54600, 3) and bleed["per_application"] == 13.0
    on, = r["buffs"]
    mark = {b["spell"]: b for b in on["buffs"]}
    assert on["who"] == "波波" and (mark["野性印記"]["up_seconds"], mark["野性印記"]["by"], mark["野性印記"]["applied"]) == (2, "小德", 1)
    assert mark["強擊光環"]["up_seconds"] > 40                      # still up when the member was last seen
    me_guard, = r["guard"]
    assert (me_guard["who"], me_guard["attacks"], me_guard["damage"], me_guard["avg_hit"]) == ("小德", 4, 160, 53.3)
    assert (me_guard["dodge"]["n"], me_guard["block"]["n"], me_guard["crit"]["n"], me_guard["hit"]["pct"]) == (1, 1, 1, 75.0)
    touch, rejuv = r["heals"]
    assert (touch["spell"], touch["healing"], touch["overheal"], touch["crit"]["n"], rejuv["periodic"], rejuv["crit"]) == ("治療之觸", 70, 0.3, 1, True, None)
    busy = {x["who"]: x for x in r["activity"]}
    assert busy["波波"]["share"] == 1.0 and busy["小德"]["active_seconds"] == 5 and busy["小德"]["active_dps"] == round((40 + 30 + 90 + 60 + 26) / 5, 1)
    cost, = r["casts"]
    assert (cost["cast_seconds"], cost["per_cast_second"]) == (1.5, 40.0) and pet["deaths"] == 1
    log = client.get(f"/api/v1/shared/party/{tok}/hits", params={"limit": 2000}).json()["items"]
    assert {e["way"] for e in log} == {"out", "in", "heal", "aura", "death"}
    dump = client.get(f"/api/v1/shared/party/{tok}/hits.json").json()
    assert dump["columns"][0] == "ts" and len(dump["rows"]) == len(rows) and len(dump["rows"][0]) == len(dump["columns"])
    boss = client.get(f"/api/v1/shared/party/{tok}/amounts", params={"spell": "自動射擊", "start": t, "end": t + 2500}).json()["members"]
    assert boss == [{"who": "波波", "normal": [80, 90], "crit": []}]
    assert (cost["spell"], cost["casts"], cost["damage"], cost["cost"], cost["per_point"], cost["power"]) == ("月火術", 1, 60, 30.0, 2.0, "法力")
    ac = client.get(f"/api/v1/shared/party/{tok}/across", params={"who": "波波", "spell": "自動射擊"}).json()["targets"]
    assert (ac[0]["enemy"], ac[0]["n"], ac[0]["armor"], ac[0]["reduced"]) == ("迪菲亞盜賊", 25, 400, 0.15) and ac[0]["ci"][0] < ac[0]["avg"] < ac[0]["ci"][1]
    db = client.get("/api/v1/combat/npcs?character_id=c1", headers=READ).json()["items"]
    assert db[0]["name"] == "迪菲亞盜賊" and client.get("/api/v1/combat/npcs?character_id=c1").status_code == 401


def test_fighting_alone_is_cut_into_sessions_with_the_same_pages_as_a_group(client):
    seed(client)                                                # a group session from T0 to T0+20 min
    at = lambda minutes: int((T0 + timedelta(minutes=minutes)).timestamp() * 1000)     # noqa: E731
    hit = lambda pos, minutes, amount, crit=0: [                                       # noqa: E731
        pos, at(minutes), "out", "", 0, "x", "迪菲亞盜賊", 589, 1, "月火術", 1, "range", amount, None, -1, 0, 0, 0, crit, 0, 0, None,
        None, None, None, None, None]
    rows = [hit(1, -60, 100), hit(2, -55, 120), hit(3, -50, 300, 1),          # alone, an hour before the group
            hit(4, -8, 50), hit(5, 5, 70), hit(6, 15, 80), hit(7, 28, 90), hit(8, 35, 110),   # into the group and out again
            hit(9, 200, 40),                                                  # one stray hit: too short to be a session
            hit(10, 300, 60), hit(11, 300.5, 75)]                             # short, but it killed something
    assert client.post("/api/v1/combat/hits", headers=AUTH, json={"character_id": "c1", "source": "l", "rows": rows}).status_code == 200
    kill = {"event_id": "c1:9100", "character_id": "c1", "event_type": "MOB_KILLED",
            "timestamp": (T0 + timedelta(minutes=300.5, seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "payload": {"monster_name": "野棘捕獵者"}}
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": [kill]})
    solo = client.get("/api/v1/characters/c1/solo", headers=READ).json()["items"]
    assert [(s["started_at"][11:16], s["ended_at"][11:16], s["damage"]) for s in solo] == [
        ("17:00", "17:00", 135),                                                          # thirty seconds and a kill
        ("12:20", "12:35", 200), ("11:52", "11:59", 50), ("11:00", "11:10", 520)]        # newest first; the group's part cut out
    assert all(s["group_id"].startswith("solo-") and s["members"] == [] and s["page"] == "/party/" + s["token"] for s in solo)
    assert solo[1]["zone_name"] == "西部荒野" and solo[1]["kills"] == 1               # 組隊後的怪, killed at T0+30
    # the group's own list is as before, and its numbers do not take in what was fought alone
    groups = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"]
    assert len(groups) == 1 and not groups[0]["group_id"].startswith("solo")
    # the page: the same answers as a group session's, with one member
    first = solo[-1]
    d = client.get(f"/api/v1/shared/party/{first['token']}").json()
    assert d["solo"] is True and d["members"] == [] and d["owner"]["name"] == "小德"
    moon = d["skills"][0]
    assert (moon["who"], moon["spell"], moon["hits"], moon["total"], moon["crit"]["n"]) == ("小德", "月火術", 3, 520, 1)
    assert client.get(f"/api/v1/shared/party/{first['token']}/skills").json()["skills"][0]["total"] == 520
    assert client.get(f"/api/v1/shared/party/{first['token']}/research").status_code == 200
    assert client.get(f"/api/v1/shared/party/{groups[0]['token']}").json()["solo"] is False
    # for tools: the same endpoints as a group session, by its id or as the newest
    base = "/api/v1/characters/c1/groups/"
    assert client.get(base + first["group_id"], headers=READ).json()["solo"] is True
    assert client.get(base + "solo-latest", headers=READ).json()["group_id"] == solo[0]["group_id"]
    assert client.get(base + first["group_id"] + "/skills", headers=READ).json()["skills"][0]["total"] == 520
    assert client.get(base + first["group_id"] + "/analysis?parts=defence", headers=READ).status_code == 200
    assert client.get(base + "latest", headers=READ).json()["solo"] is False
    assert client.get(base + "solo-1", headers=READ).status_code == 404
    # it keeps its link while it grows
    more = [hit(10, -45, 60)]
    client.post("/api/v1/combat/hits", headers=AUTH, json={"character_id": "c1", "source": "l", "rows": more})
    again = client.get("/api/v1/characters/c1/solo", headers=READ).json()["items"][-1]
    assert (again["token"], again["damage"], again["ended_at"][11:16]) == (first["token"], 580, "11:15")
    # someone else's token does not get in
    assert client.get("/api/v1/characters/c1/solo").status_code == 401
    assert "/api/v1/characters/{character_id}/solo" in client.get("/openapi.json").json()["paths"]


def test_full_names_come_from_the_combat_log_and_are_only_shown(client):
    """The log writes "小德-德魯-PvP-KR": shown as "小德 德魯"; everything stays keyed by "小德"."""
    seed(client)
    t = int((T0 + timedelta(minutes=5)).timestamp() * 1000)
    rows = [[i, t + i, "aura", who, 0, unit, None, None, 1, "增益", 1, "APPLIED", None, None, None, None, None, None,
             0, 0, 0, "BUFF", None, None, None, None, None, None]
            for i, (who, unit) in enumerate([("", "小德-德魯-PvP-KR"), ("波波", "波波-火法-PvP-KR"), ("艾莉", "艾莉-甲-PvP-KR"),
                                             ("艾莉", "艾莉-乙-PvP-KR"), ("", "x"), ("", "迪菲亞盜賊")])]
    assert client.post("/api/v1/combat/hits", headers=AUTH, json={"character_id": "c1", "source": "l", "rows": rows}).status_code == 200
    me = client.get("/api/v1/characters/c1", headers=READ).json()
    assert (me["name"], me["full_name"]) == ("小德", "小德 德魯")
    g = client.get("/api/v1/groups?character_id=c1", headers=READ).json()["items"][0]
    assert g["full_names"] == {"波波": "波波 火法"}                      # two 艾莉 seen: left short, never the wrong one
    assert [m["name"] for m in g["members"]] == ["艾莉", "波波"]
    d = client.get(f"/api/v1/shared/party/{g['token']}").json()
    assert d["full_names"] == {"小德": "小德 德魯", "波波": "波波 火法"} and d["owner"]["name"] == "小德"
    assert client.get("/api/v1/characters/c1/snapshot", headers=READ).json()["full_name"] == "小德 德魯"
    # the addon's own word wins, and a snapshot without it keeps what is known
    snap = lambda m, data: client.put("/api/v1/characters/c1/snapshot", headers=AUTH, json={      # noqa: E731
        "captured_at": (T0 + timedelta(minutes=m)).isoformat(), "device_id": "mac", "data": data})
    snap(60, {"name": "小德", "full_name": "小德 真名", "level": 22})
    assert client.get("/api/v1/characters/c1", headers=READ).json()["full_name"] == "小德 真名"
    snap(61, {"name": "小德", "level": 22})
    assert client.get("/api/v1/characters/c1", headers=READ).json()["full_name"] == "小德 真名"
    assert client.get("/api/v1/characters", headers=READ).json()["items"][0]["full_name"] == "小德 真名"
