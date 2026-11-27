from datetime import datetime, timezone

from conftest import AUTH, READ_TOKEN

READ = {"Authorization": f"Bearer {READ_TOKEN}"}


def snapshot(client, cid="c1", **extra):
    data = {"name": "小德", "level": 22, "class": "DRUID", "money": 30908, "equipment": [
        {"slot": 5, "item_id": 2041, "name": "西部荒野外套", "quality": 2, "item_level": 20, "req_level": 15,
         "item_type": "護甲", "item_subtype": "皮甲", "equip_loc": "INVTYPE_CHEST", "sell_price": 300,
         "stats": {"armor": 70, "stamina": 3, "spirit": 5}, "durability": 90, "durability_max": 90},
        {"slot": 11, "item_id": 50, "name": "舊戒指", "item_level": 10, "equip_loc": "INVTYPE_FINGER",
         "stats": {"stamina": 1}}],
        "bags": {"slots": 16, "free": 9, "items": [
            {"item_id": 3000, "name": "流浪者外衣", "count": 1, "quality": 2, "item_level": 24, "req_level": 19,
             "item_type": "護甲", "item_subtype": "皮甲", "equip_loc": "INVTYPE_CHEST", "sell_price": 410,
             "stats": {"armor": 88, "stamina": 5, "agility": 2}},
            {"item_id": 3001, "name": "鎖甲胸鎧", "count": 1, "quality": 2, "item_level": 26, "req_level": 20,
             "item_subtype": "鎖甲", "equip_loc": "INVTYPE_CHEST", "sell_price": 500, "stats": {"armor": 160}},
            {"item_id": 3002, "name": "高等手套", "count": 1, "item_level": 30, "req_level": 25,
             "item_subtype": "皮甲", "equip_loc": "INVTYPE_HAND", "stats": {"armor": 50}},
            {"item_id": 3003, "name": "新戒指", "count": 1, "item_level": 18, "equip_loc": "INVTYPE_FINGER",
             "stats": {"stamina": 3}},
            {"item_id": 3004, "name": "木盾", "count": 1, "item_subtype": "盾牌", "equip_loc": "INVTYPE_SHIELD"},
            {"item_id": 3005, "name": "短劍", "count": 1, "item_level": 20, "equip_loc": "INVTYPE_WEAPON",
             "stats": {"damage_per_second": 9.5}},
            {"item_id": 1, "name": "破牙", "count": 4, "quality": 0, "sell_price": 25},
            {"item_id": 2, "name": "輕皮", "count": 10, "quality": 1, "sell_price": 15}]}}
    data.update(extra)
    r = client.put(f"/api/v1/characters/{cid}/snapshot", headers=AUTH,
                   json={"captured_at": datetime.now(timezone.utc).isoformat(), "device_id": "mac", "data": data})
    assert r.status_code == 200


def test_items_endpoint_has_stats_values_and_comparisons(client):
    snapshot(client)
    r = client.get("/api/v1/characters/c1/items", headers=READ).json()
    assert (r["level"], r["class"], r["bag_free"], r["has_stats"]) == (22, "DRUID", 9, True)
    assert r["bag_value"] == 410 + 500 + 100 + 150 and r["junk_value"] == 100
    chest = r["equipment"][0]
    assert (chest["slot_name"], chest["name"], chest["item_level"], chest["subtype"], chest["sell_price"]) == \
        ("胸部", "西部荒野外套", 20, "皮甲", 300)
    assert chest["stats"] == {"armor": 70, "stamina": 3, "spirit": 5}
    assert [b["name"] for b in r["bags"]][:2] == ["鎖甲胸鎧", "流浪者外衣"]            # most valuable first
    by = {c["item"]["name"]: c for c in r["comparisons"]}
    up = by["流浪者外衣"]
    assert up["usable"] is True and up["slot_name"] == "胸部" and up["equipped"]["name"] == "西部荒野外套"
    assert up["stat_diff"] == {"armor": 18, "stamina": 2, "agility": 2, "spirit": -5} and up["item_level_diff"] == 4
    assert by["鎖甲胸鎧"]["usable"] is False and "鎖甲" in by["鎖甲胸鎧"]["note"]          # druids wear leather
    assert by["高等手套"]["usable"] is False and by["高等手套"]["note"] == "需要 25 級"
    assert by["新戒指"]["slot_name"] == "戒指二" and by["新戒指"]["equipped"] is None      # the empty ring slot
    assert by["木盾"]["usable"] is False
    assert by["短劍"]["usable"] is None and by["短劍"]["equipped"] is None                 # weapon skill unknown
    assert "破牙" not in by and "輕皮" not in by                                          # not wearable


def test_items_without_stats_from_an_old_addon(client):
    data_eq = [{"slot": 5, "item_id": 2041, "durability": 90, "durability_max": 90}]
    snapshot(client, "old", equipment=data_eq, bags={"slots": 16, "free": 15, "items": [
        {"item_id": 2, "name": "輕皮", "count": 10, "quality": 1}]})
    r = client.get("/api/v1/characters/old/items", headers=READ).json()
    assert r["has_stats"] is False and r["bag_value"] is None and r["comparisons"] == []
    assert r["equipment"][0]["stats"] is None and r["equipment"][0]["name"] is None


def test_items_endpoint_access(client):
    snapshot(client, "c1")
    snapshot(client, "c2")
    assert client.get("/api/v1/characters/c1/items").status_code == 401
    assert client.get("/api/v1/characters/nobody/items", headers=READ).status_code == 404
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/characters/c1/items", headers=S).status_code == 200
    assert client.get("/api/v1/characters/c2/items", headers=S).status_code == 403


def test_voice_gear_and_compare(client):
    snapshot(client)
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    text = client.get(f"/voice?t={tok}&show=gear,compare").text
    assert "胸部：西部荒野外套，物品等級 20，皮甲，護甲 70、耐力 3、精神 5，耐久 90/90，賣店 3銀" in text
    assert "流浪者外衣（胸部，物品等級 24，皮甲）：換掉身上的 西部荒野外套 會變成：護甲 +18、敏捷 +2、耐力 +2、精神 -5" in text
    assert "鎖甲胸鎧（胸部，物品等級 26，鎖甲）：不能裝備，這個職業現在不能穿鎖甲" in text
    assert "新戒指（戒指二，物品等級 18）：這個部位目前是空的，屬性：耐力 3" in text
    assert "短劍" in text and "武器能不能用要看是否學過該武器技能" in text
    only = client.get(f"/voice?t={tok}&show=compare&q=戒指").text
    assert "新戒指" in only and "流浪者外衣" not in only


def test_two_handed_main_hand_blocks_the_off_hand(client):
    snapshot(client, "th", equipment=[
        {"slot": 16, "item_id": 9, "name": "橡木槌", "item_level": 21, "equip_loc": "INVTYPE_2HWEAPON",
         "stats": {"damage_per_second": 13.71, "strength": 5}}],
        bags={"slots": 16, "free": 10, "items": [
            {"item_id": 20, "name": "粉塵包", "count": 1, "equip_loc": "INVTYPE_HOLDABLE", "stats": {"spirit": 1}},
            {"item_id": 21, "name": "短劍", "count": 1, "item_level": 20, "equip_loc": "INVTYPE_WEAPON",
             "stats": {"damage_per_second": 9.5}}]})
    by = {c["item"]["name"]: c for c in client.get("/api/v1/characters/th/items", headers=READ).json()["comparisons"]}
    assert by["粉塵包"]["slot_name"] == "副手" and "雙手武器" in by["粉塵包"]["note"]
    sword = by["短劍"]                              # a one-hander is compared with the main hand
    assert sword["slot_name"] == "主手" and sword["equipped"]["name"] == "橡木槌"
    assert sword["stat_diff"] == {"damage_per_second": -4.21, "strength": -5}


def test_the_bank_as_it_was_last_seen(client):
    snapshot(client, "c2")
    assert client.get("/api/v1/characters/c2/bank", headers=READ).json()["bank"] is None      # never opened
    assert client.get("/api/v1/characters/c2/items", headers=READ).json()["bank"] is None
    snapshot(client, bank={"slots": 30, "free": 27, "seen_at": 1795000000, "items": [
        {"item_id": 2, "name": "輕皮", "count": 40, "quality": 1, "sell_price": 15},
        {"item_id": 3000, "name": "流浪者外衣", "count": 1, "quality": 2, "item_level": 24, "sell_price": 410,
         "stats": {"armor": 88}},
        {"item_id": 9, "name": "任務信件", "count": 1, "quality": 1}]})
    r = client.get("/api/v1/characters/c1/bank", headers=READ).json()
    bank = r["bank"]
    assert (bank["slots"], bank["free"], bank["value"], bank["seen_at"]) == (30, 27, 1010, "2026-11-18T11:06:40Z")
    assert [(i["name"], i["count"], i["value"]) for i in bank["items"]] == [
        ("流浪者外衣", 1, 410), ("輕皮", 40, 600), ("任務信件", 1, 0)]                      # best quality first
    assert client.get("/api/v1/characters/c1/items", headers=READ).json()["bank"] == bank
    # limited to the character a token is for, like everything else about a character
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    S = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/characters/c1/bank", headers=S).status_code == 200
    assert client.get("/api/v1/characters/c2/bank", headers=S).status_code == 403
    assert client.get("/api/v1/characters/c1/bank").status_code == 401
    assert "/api/v1/characters/{character_id}/bank" in client.get("/openapi.json").json()["paths"]


def test_trades_are_listed_with_both_sides(client):
    snapshot(client)
    snapshot(client, "c2")
    def trade(n, minute, who, gave, got):
        return {"event_id": f"c1:t{n}", "character_id": "c1", "event_type": "TRADE_COMPLETED",
                "timestamp": f"2026-11-20T12:{minute:02d}:00Z",
                "payload": {"with": who, "with_class": "WARRIOR", "with_level": 24, "gave": gave, "got": got, "zone_name": "赤脊山"}}
    client.post("/api/v1/events/batch", headers=AUTH, json={"schema_version": 1, "device_id": "mac", "events": [
        trade(1, 0, "波波", {"money": 150, "items": [{"item_id": 2589, "name": "亞麻布", "count": 20}]}, {"money": 0, "items": []}),
        trade(2, 5, "艾莉", {"money": 0, "items": []}, {"money": 500, "items": [{"item_id": 2770, "name": "銅礦石", "count": 5}]})]})
    r = client.get("/api/v1/characters/c1/trades", headers=READ).json()["trades"]
    assert [t["with"] for t in r] == ["艾莉", "波波"] and r[1]["gave"]["items"][0]["name"] == "亞麻布"
    assert [t["with"] for t in client.get("/api/v1/characters/c1/trades?with=波波", headers=READ).json()["trades"]] == ["波波"]
    tok = client.post("/api/v1/characters/c1/voice-token", headers=AUTH).json()["token"]
    assert client.get("/api/v1/characters/c2/trades", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    from app.voice import describe
    assert describe("TRADE_COMPLETED", r[0]).startswith("和 艾莉 交易：給出 沒有東西，收到 銅礦石 x5、5銀")


def test_an_enchanting_trade_is_said_as_such():
    from app.voice import describe
    p = {"with": "白河", "gave": {"money": 0, "items": [], "not_traded": {"name": "臂環", "enchantment": "附魔護腕 - 初級耐力"}},
         "got": {"money": 0, "items": []}}
    assert describe("TRADE_COMPLETED", p) == "和 白河 交易：請對方附魔我的 臂環（附魔護腕 - 初級耐力）"
    p["got"]["money"] = 50
    assert describe("TRADE_COMPLETED", p) == "和 白河 交易：給出 沒有東西，收到 50銅；請對方附魔我的 臂環（附魔護腕 - 初級耐力）"
