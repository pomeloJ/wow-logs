from datetime import datetime, timedelta, timezone

from conftest import AUTH, READ_TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def setup(client, cid="char-1"):
    snap = {"captured_at": NOW.isoformat(), "device_id": "mac", "data": {
        "name": "小德", "level": 22, "race": "NightElf", "class": "DRUID", "xp": 100, "xp_max": 400,
        "money": 30908, "zone_name": "西部荒野", "sub_zone": "哨兵嶺", "x": 53.3, "y": 52.5,
        "bags": {"slots": 48, "free": 6, "items": [
            {"item_id": 2318, "name": "輕皮", "count": 20}, {"item_id": 2318, "name": "輕皮", "count": 3},
            {"item_id": 6948, "name": "爐石", "count": 1}]},
        "quests": [{"quest_id": 1, "title": "自然屏障", "category": "戰歌峽谷", "level": 29,
                    "complete": False, "objectives": [{"text": "0/3 榮譽獎章"}]},
                   {"quest_id": 2, "title": "紅色亞麻布", "category": "西部荒野", "level": 12,
                    "complete": True, "objectives": []}],
        "equipment": [{"slot": 5, "item_id": 2041, "durability": 90, "durability_max": 90}],
        "flight_points": {"哨兵嶺，西部荒野": True, "zzOLD": False, "夜色鎮，暮色森林": False}}}
    assert client.put(f"/api/v1/characters/{cid}/snapshot", json=snap, headers=AUTH).status_code == 200

    def e(i, etype, payload, ago=5):
        return {"event_id": f"a:{i:010d}", "character_id": cid, "event_type": etype,
                "timestamp": (NOW - timedelta(minutes=ago)).isoformat(), "payload": payload}
    events = [e(1, "MOB_KILLED", {"monster_id": 547, "monster_name": "巨型血牙野豬", "zone_name": "西部荒野"}),
              e(2, "ITEM_LOOTED", {"item_id": 2318, "item_name": "輕皮", "count": 2}, 4),
              e(3, "XP_GAINED", {"amount": 150}, 3),
              e(4, "CHAT_MESSAGE", {"text": "秘密對話"}, 2),
              e(5, "LOCATION_SAMPLE", {"map_id": 1436, "x": 1, "y": 2}, 1),
              e(6, "LEVEL_UP", {"level": 22}, 60 * 24 * 3)]
    r = client.post("/api/v1/events/batch", headers=AUTH,
                    json={"schema_version": 1, "device_id": "mac", "events": events})
    assert r.json()["accepted"] == 6
    return client.post(f"/api/v1/characters/{cid}/voice-token", headers=AUTH).json()["token"]


def test_token_is_short_stable_and_needs_the_write_token(client):
    tok = setup(client)
    assert len(tok) == 12 and tok.isalnum()
    assert client.post("/api/v1/characters/char-1/voice-token", headers=AUTH).json()["token"] == tok
    assert client.post("/api/v1/characters/char-1/voice-token", headers=READ).status_code == 403
    assert client.post("/api/v1/characters/nobody/voice-token", headers=AUTH).status_code == 404
    new = client.post("/api/v1/characters/char-1/voice-token?rotate=1", headers=AUTH).json()["token"]
    assert new != tok
    assert client.get(f"/voice?t={tok}").status_code == 404      # the old one stops working
    assert client.get(f"/voice?t={new}").status_code == 200


def test_bad_token_reveals_nothing(client):
    setup(client)
    for url in ("/voice", "/voice?t=", "/voice?t=wrongwrong12"):
        r = client.get(url)
        assert r.status_code == 404 and "小德" not in r.text
    # the character token can never write
    tok = client.post("/api/v1/characters/char-1/voice-token", headers=AUTH).json()["token"]
    assert client.post("/api/v1/events/batch", json={}, headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_default_view_is_plain_text_overview(client):
    tok = setup(client)
    r = client.get(f"/voice?t={tok}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "小德" in r.text and "22 級 夜精靈德魯伊" in r.text
    assert "3金9銀8銅" in r.text and "100/400（25%）" in r.text
    assert "擊殺 1 隻" in r.text and "獲得經驗 150" in r.text


def test_sections_and_filters(client):
    tok = setup(client)
    get = lambda q: client.get(f"/voice?t={tok}&{q}").text  # noqa: E731
    quests = get("show=quests")
    assert "自然屏障" in quests and "0/3 榮譽獎章" in quests and "已可交付" in quests
    assert "自然屏障" not in get("show=quests&q=亞麻") and "紅色亞麻布" in get("show=quests&q=亞麻")
    assert "輕皮 x23" in get("show=bags")                       # stacks are merged
    assert "胸部：物品編號 2041，耐久 90/90" in get("show=gear")
    flights = get("show=flights")
    assert "已開啟 1/2" in flights and "zzOLD" not in flights
    assert "巨型血牙野豬 1 隻" in get("show=kills") and "輕皮 2 件" in get("show=loot")
    assert "座標 53, 52" in get("show=where")
    both = get("show=quests,bags")
    assert "任務日誌" in both and "背包" in both and "角色概況" not in both
    assert "任務日誌" in get("show=all") and "事件紀錄" in get("show=all")


def test_events_ranges_and_privacy(client):
    tok = setup(client)
    get = lambda q: client.get(f"/voice?t={tok}&{q}").text  # noqa: E731
    recent = get("show=events&range=1h")
    assert "擊殺 巨型血牙野豬" in recent and "升到 22 級" not in recent
    assert "秘密對話" not in recent and "CHAT_MESSAGE" not in recent and "LOCATION_SAMPLE" not in recent
    assert "升到 22 級" in get("show=events&range=all")
    only = get("show=events&range=all&type=item_looted")
    assert "拾取 輕皮 x2" in only and "擊殺" not in only
    assert "秘密對話" not in get("show=events&range=all&type=CHAT_MESSAGE")
    assert "擊殺" in get("show=events&range=all&q=野豬") and "拾取" not in get("show=events&range=all&q=野豬")
    assert len([ln for ln in get("show=events&range=all&limit=2").splitlines() if ln.startswith("- ")]) == 2


def test_help_and_bad_parameters(client):
    tok = setup(client)
    assert "show" in client.get(f"/voice?t={tok}&show=help").text
    assert client.get(f"/voice?t={tok}&show=secrets").status_code == 400
    assert client.get(f"/voice?t={tok}&range=forever").status_code == 400
    assert client.get(f"/voice?t={tok}&limit=1000").status_code == 422


def test_each_token_sees_only_its_character(client):
    a = setup(client, "char-1")
    snap = {"captured_at": NOW.isoformat(), "device_id": "mac", "data": {"name": "小號", "level": 3}}
    client.put("/api/v1/characters/char-2/snapshot", json=snap, headers=AUTH)
    b = client.post("/api/v1/characters/char-2/voice-token", headers=AUTH).json()["token"]
    assert a != b
    text = client.get(f"/voice?t={b}&show=all&range=all").text
    assert "小號" in text and "小德" not in text and "巨型血牙野豬" not in text


def test_character_token_opens_a_one_character_dashboard(client):
    tok = setup(client, "char-1")
    snap = {"captured_at": NOW.isoformat(), "device_id": "mac", "data": {"name": "小號", "level": 3}}
    client.put("/api/v1/characters/char-2/snapshot", json=snap, headers=AUTH)
    other = {"event_id": "b:0000000001", "character_id": "char-2", "event_type": "MOB_KILLED",
             "timestamp": NOW.isoformat(), "payload": {"monster_name": "別人的怪"}}
    client.post("/api/v1/events/batch", headers=AUTH,
                json={"schema_version": 1, "device_id": "mac", "events": [other]})
    S = {"Authorization": f"Bearer {tok}"}
    get = lambda url: client.get("/api/v1" + url, headers=S)  # noqa: E731

    assert [c["id"] for c in get("/characters").json()["items"]] == ["char-1"]
    assert get("/characters/char-1/snapshot").json()["data"]["name"] == "小德"
    assert get("/characters/char-2/snapshot").status_code == 403
    assert get("/characters/char-2").status_code == 403
    # events are this character's only and never include chat, whatever is asked for
    for url in ("/events?limit=1000", "/events?character_id=char-1", "/events?event_type=CHAT_MESSAGE"):
        items = get(url).json()["items"]
        assert all(e["character_id"] == "char-1" and e["event_type"] != "CHAT_MESSAGE" for e in items)
    assert len(get("/events?limit=1000").json()["items"]) == 5
    assert get("/events?event_type=CHAT_MESSAGE").json()["items"] == []
    # asking about someone else's character is refused, never answered about this one
    for url in ("/events?character_id=char-2", "/locations?character_id=char-2", "/stats/activity?character_id=char-2"):
        r = get(url)
        assert r.status_code == 403 and r.json()["detail"] == {
            "error": "character_not_authorized", "character_id": "char-2", "message": r.json()["detail"]["message"]}, url
    act = get("/stats/activity").json()
    assert act["kills"] == 1 and act["top_monsters"][0]["name"] == "巨型血牙野豬"
    summary = get("/stats/summary").json()
    assert summary["characters"] == 1 and summary["events"] == 5 and "CHAT_MESSAGE" not in summary["by_type"]
    assert get("/maps").status_code == 200
    # it cannot write, and the full read token still sees everything
    assert client.put("/api/v1/characters/char-1/snapshot", json=snap, headers=S).status_code == 403
    assert client.post("/api/v1/characters/char-1/voice-token", headers=S).status_code == 403
    assert len(client.get("/api/v1/characters", headers=READ).json()["items"]) == 2
    assert client.get("/api/v1/events?event_type=CHAT_MESSAGE", headers=READ).json()["items"] != []
    # rotating the token closes the shared dashboard
    client.post("/api/v1/characters/char-1/voice-token?rotate=1", headers=AUTH)
    assert get("/characters").status_code == 403


def test_vendor_values(client):
    snap = {"captured_at": NOW.isoformat(), "device_id": "mac", "data": {"name": "小德", "bags": {
        "slots": 16, "free": 10, "items": [
            {"item_id": 1, "name": "破牙", "count": 4, "quality": 0, "sell_price": 25},
            {"item_id": 2, "name": "輕皮", "count": 10, "quality": 1, "sell_price": 15},
            {"item_id": 3, "name": "爐石", "count": 1, "quality": 1, "sell_price": 0}]}}}
    client.put("/api/v1/characters/v1/snapshot", json=snap, headers=AUTH)
    ev = [{"event_id": f"v:{i}", "character_id": "v1", "event_type": "ITEM_LOOTED",
           "timestamp": (NOW - timedelta(minutes=1)).isoformat(), "payload": p}
          for i, p in enumerate([{"item_id": 2, "item_name": "輕皮", "count": 3, "sell_price": 15},
                                 {"item_id": 9, "item_name": "舊物", "count": 2}])]
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": ev})
    act = client.get("/api/v1/stats/activity?character_id=v1", headers=READ).json()
    assert act["loot_value"] == 45 and {i["name"]: i["value"] for i in act["top_items"]} == {"輕皮": 45, "舊物": 0}
    tok = client.post("/api/v1/characters/v1/voice-token", headers=AUTH).json()["token"]
    text = client.get(f"/voice?t={tok}&show=bags,activity,loot&range=1h").text
    assert "全部賣給商店約值 2銀50銅，其中灰色垃圾 1銀" in text
    assert "輕皮 x10，值 1銀50銅" in text and "- 爐石 x1\n" in text
    assert "（賣給商店約值 45銅）" in text and "輕皮 3 件，值 45銅" in text and "舊物 2 件\n" in text
