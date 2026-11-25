from wowmock import Game


def types(game):
    return [e["event_type"] for e in game.events()]


def test_login_creates_snapshot_and_first_events():
    g = Game()
    g.login()
    assert types(g) == ["SESSION_START", "LOCATION_SAMPLE"]
    snap = g.db.characters["Player-1234-00ABCDEF"]
    assert snap.captured_at == 1795000000
    assert snap.data.name == "測試德魯伊"
    assert snap.data["class"] == "DRUID" and snap.data.level == 24
    loc = g.events()[1]
    assert dict(loc["payload"])["map_id"] == 1439
    assert dict(loc["payload"])["x"] == 53.8 and dict(loc["payload"])["y"] == 75.1


def test_location_sampling_rules():
    g = Game()
    g.login()
    g.tick(15)                      # no movement, under heartbeat
    assert types(g).count("LOCATION_SAMPLE") == 1
    g.mock.x = 0.560                # moved 2.2 points
    g.tick(15)
    assert types(g).count("LOCATION_SAMPLE") == 2
    g.mock.x = 0.561                # 0.1 points: ignored
    g.tick(15)
    assert types(g).count("LOCATION_SAMPLE") == 2
    g.tick(60)                      # heartbeat
    assert types(g).count("LOCATION_SAMPLE") == 3
    g.mock.map = 1440               # same x/y on a different map is a new place
    g.tick(15)
    assert types(g).count("LOCATION_SAMPLE") == 4


def test_zone_change_and_level_up():
    g = Game()
    g.login()
    g.mock.map, g.mock.zone = 1440, "梣谷"
    g.fire("ZONE_CHANGED_NEW_AREA")
    g.fire("PLAYER_LEVEL_UP", 25)
    assert types(g)[2:] == ["ZONE_CHANGED", "LOCATION_SAMPLE", "LEVEL_UP"]
    assert g.db.characters["Player-1234-00ABCDEF"].data.level == 25


def test_event_ids_unique_and_continue_after_reload():
    g = Game()
    g.login()
    g.tick(60)
    first = [e["event_id"] for e in g.events()]
    g.login()                       # /reload keeps AdventureCollectorDB
    ids = [e["event_id"] for e in g.events()]
    assert ids[:len(first)] == first            # old ids never change
    assert len(set(ids)) == len(ids)
    assert len({i.split(":")[0] for i in ids}) == 1


def test_lost_saved_variables_get_a_new_id_namespace():
    g = Game()
    g.login()
    old = g.db.installationId
    g.lua.globals().AdventureCollectorDB = None  # cold start that failed to load data
    g.mock.time += 100
    g.login()
    assert g.db.installationId != old
    assert g.events()[0]["event_id"].startswith(g.db.installationId + ":")


def test_no_position_and_secret_values_do_not_break_or_leak():
    g = Game()
    g.mock.nopos = True
    g.mock.secret = 123456          # pretend money is a secret value
    g.login()
    assert types(g) == ["SESSION_START"]
    data = g.db.characters["Player-1234-00ABCDEF"].data
    assert data.money is None and data.x is None and data.map_id == 1439


def test_trim_keeps_newest_events():
    g = Game()
    g.login()
    g.db.settings.maxEvents = 50
    for _ in range(400):
        g.ns.addEvent("TEST", None)
    ev = g.events()
    assert 50 <= len(ev) <= 250
    assert ev[-1]["event_id"].endswith(f"{g.db.nextSequence - 1:010d}")


def test_slash_commands_and_pause():
    g = Game()
    g.login()
    out = g.slash("status")
    assert any("測試德魯伊" in line for line in out)
    g.slash("off")
    n = len(g.events())
    g.mock.x = 0.9
    g.tick(15)
    assert len(g.events()) == n
    g.slash("on")
    g.tick(15)
    assert len(g.events()) == n + 1


def test_empty_realm_falls_back_and_logout_keeps_position(tmp_path):
    g = Game()
    g.mock.realm = ""               # seen in the real client
    g.login()
    g.logout(tmp_path / "sv.lua")
    data = g.db.characters["Player-1234-00ABCDEF"].data
    assert data.realm == "NormalizedRealm"
    assert data.map_id == 1439 and data.x == 53.8


def test_save_now_button_and_command():
    g = Game()
    g.login()
    b = g.mock.button
    assert b.text == f"存檔 ({len(g.events())})"      # shows events since last save
    g.tick(60)
    assert b.text == f"存檔 ({len(g.events())})"
    b.scripts.OnClick(b)
    assert g.mock.reloads == 1
    g.slash("save")
    assert g.mock.reloads == 2
    g.mock.combat = True                              # never reload mid-combat
    g.slash("save")
    assert g.mock.reloads == 2
    g.slash("button")
    assert b.shown is False and g.db.settings.showButton is False
    g.slash("button")
    assert b.shown is True


CHAR = "Player-1234-00ABCDEF"
LOOT = "你獲得了物品：|cffffffff|Hitem:2589::::::::22:::::|h[亞麻布]|h|rx2。"


def lua_list(t):
    return [t[i] for i in range(1, len(t) + 1)]


def test_snapshot_has_extended_data_and_rescans_only_when_dirty():
    g = Game()
    g.login()
    d = g.db.characters[CHAR].data
    assert (d.xp, d.xp_max, d.rested_xp, d.bind_location) == (4200, 21000, 1500, "暴風城")
    eq = lua_list(d.equipment)
    assert (eq[0].slot, eq[0].item_id, eq[0].name, eq[0].quality, eq[0].durability) == (16, 2140, "雕花匕首", 2, 30)
    assert (d.bags.slots, d.bags.free) == (4, 1)
    items = {i.item_id: (i.name, i.count) for i in lua_list(d.bags["items"])}
    assert items == {2589: ("亞麻布", 20), 6948: ("爐石", 1)}       # stacks are merged
    prices = {i.item_id: i.sell_price for i in lua_list(d.bags["items"])}
    assert prices == {2589: 13, 6948: 0} and eq[0].sell_price == 250   # vendor value per item, copper
    q = lua_list(d.quests)
    assert len(q) == 1 and (q[0].quest_id, q[0].title, q[0].category) == (12, "人民軍", "西部荒野")
    assert lua_list(q[0].objectives)[0].text == "迪菲亞盜賊 3/15"
    assert [(p.name, p.rank, p.max_rank) for p in lua_list(d.professions)] == [("草藥學", 75, 150), ("釣魚", 20, 75)]

    g.lua.execute("MOCK.bags[0][1].stackCount = 1")
    g.mock.x = 0.60
    g.tick(15)                                   # new snapshot, but bags not flagged dirty
    assert lua_list(g.db.characters[CHAR].data.bags["items"])[0].count == 20
    g.fire("BAG_UPDATE_DELAYED")
    g.mock.x = 0.62
    g.tick(15)
    assert lua_list(g.db.characters[CHAR].data.bags["items"])[0].count == 9


def test_failed_scan_keeps_previous_data_and_collection_continues():
    g = Game()
    g.login()
    g.mock.bagsBroken = True
    g.fire("BAG_UPDATE_DELAYED")
    g.mock.x = 0.60
    g.tick(15)
    d = g.db.characters[CHAR].data
    assert d.bags.slots == 4 and d.x == 60.0


def test_quest_death_loot_and_kill_events():
    g = Game()
    g.login()
    n = len(g.events())
    g.fire("QUEST_ACCEPTED", 12)
    g.fire("QUEST_TURNED_IN", 12, 900, 250)
    g.fire("QUEST_REMOVED", 12)                  # part of the turn-in: not a separate event
    g.fire("QUEST_REMOVED", 34)                  # no turn-in: abandoned or failed
    g.fire("PLAYER_DEAD")
    g.fire("CHAT_MSG_LOOT", LOOT, "", "", "", "", "", 0, 0, "", 0, 0, CHAR)
    g.fire("CHAT_MSG_LOOT", LOOT, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-OTHER")
    g.fire("CHAT_MSG_LOOT", "你得到了一些錢")
    mob = "Creature-0-3783-0-12-589-0000123ABC"
    g.lua.execute(f'UnitGUID = function(u) if u == "target" then return "{mob}" end return MOCK.guid end')
    g.lua.execute('UnitName = function(u) if u == "target" then return "迪菲亞盜賊" end return "測試德魯伊" end')
    g.fire("PLAYER_TARGET_CHANGED")
    g.fire("PARTY_KILL", CHAR, mob)
    g.fire("PARTY_KILL", "Player-1-OTHER", mob)                    # someone else's kill
    g.fire("PARTY_KILL", CHAR, "Player-1-ENEMY")                   # not a creature
    ev = [(e["event_type"], dict(e["payload"])) for e in g.events()[n:]]
    assert [t for t, _ in ev] == ["QUEST_ACCEPTED", "QUEST_COMPLETED", "QUEST_REMOVED",
                                  "PLAYER_DIED", "ITEM_LOOTED", "MOB_KILLED"]
    assert ev[0][1]["quest_name"] == "人民軍" and ev[0][1]["map_id"] == 1439
    assert (ev[1][1]["xp_reward"], ev[1][1]["money_reward"]) == (900, 250)
    assert ev[2][1]["quest_id"] == 34
    assert (ev[4][1]["item_id"], ev[4][1]["item_name"], ev[4][1]["count"]) == (2589, "亞麻布", 2)
    assert (ev[5][1]["monster_id"], ev[5][1]["monster_name"]) == (589, "迪菲亞盜賊")


def test_map_screenshot_records_map_rect():
    g = Game()
    g.login()
    g.slash("mapshot")
    assert g.lua.globals().WorldMapFrame.shown and g.mock.shots == 1
    e = g.events()[-1]
    p = dict(e["payload"])
    assert e["event_type"] == "MAP_SCREENSHOT" and p["map_id"] == 1436 and p["map_name"] == "西部荒野"
    assert [p[k] for k in ("left_u", "right_u", "top_u", "bottom_u", "screen_w_u")] == [200, 800, 450, 50, 1000]
    assert p["local_ts"] == g.mock.time
    g.lua.execute("MOCK.canvas = { -900, 1900, 450, 50 }")    # map zoomed in: off screen
    g.slash("mapshot")
    assert g.mock.shots == 1 and g.events()[-1]["event_id"] == e["event_id"]
    g.fire("SCREENSHOT_SUCCEEDED")                            # the player's own screenshot
    assert g.events()[-1]["event_id"] == e["event_id"]


def test_reminds_once_about_maps_without_background():
    g = Game()
    g.login()                                    # map 1439 is covered by the wiki set
    assert not any("底圖" in line for line in g.mock.out.values())
    g.mock.map, g.mock.zone = 2501, "河原地"
    g.tick(15)
    g.mock.x = 0.7
    g.tick(15)
    assert sum("還沒有底圖" in line for line in g.mock.out.values()) == 1
    g.lua.execute("WorldMapFrame.GetMapID = function() return 2501 end")
    g.slash("mapshot")
    g.login()                                    # next session: already has a screenshot
    g.tick(15)
    assert sum("還沒有底圖" in line for line in g.mock.out.values()) == 1


def new_events(g, n):
    return [(e["event_type"], dict(e["payload"])) for e in g.events()[n:]]


def test_reputation_stats_played_and_flight_points_in_snapshot():
    g = Game()
    g.login()
    assert g.mock.playedRequests == 1
    g.fire("TIME_PLAYED_MSG", 360000, 7200)
    g.fire("TAXIMAP_OPENED")
    g.mock.x = 0.6
    g.tick(15)
    d = g.db.characters[CHAR].data
    reps = [(r.name, r.standing, r.value, r.ceiling) for r in lua_list(d.reputations)]
    assert reps == [("暴風城", 5, 4200, 9000), ("藏寶海灣", 4, 500, 3000)]      # header skipped
    assert (d.stats.strength, d.stats.spirit, d.stats.armor, d.stats.health_max) == (15, 55, 420, 980)
    assert (d.played_total, d.played_level) == (360000, 7200)
    assert dict(d.flight_points) == {"哨兵嶺": True, "暴風城": True, "夜色鎮": False}
    g.login()                                   # values learned from events survive a reload
    assert g.db.characters[CHAR].data.played_total == 360000


def test_money_xp_combat_instance_and_misc_events():
    g = Game()
    g.login()
    n = len(g.events())
    g.lua.execute("GetMoney = function() return 123456 + 250 end")
    g.fire("PLAYER_MONEY")
    g.fire("PLAYER_MONEY")                      # no change: nothing stored
    g.lua.execute("UnitXP = function() return 4500 end")
    g.fire("PLAYER_XP_UPDATE", "player")
    g.lua.execute("UnitXP = function() return 100 end")     # level-up wrap: gain unknown
    g.fire("PLAYER_XP_UPDATE", "player")
    g.fire("CHAT_MSG_SKILL", "你的剝皮技能提升到了76。")
    g.fire("PLAYER_REGEN_DISABLED")
    g.mock.time += 12
    g.fire("PLAYER_REGEN_ENABLED")
    g.mock.instance = "死亡礦坑"
    g.fire("PLAYER_ENTERING_WORLD")
    g.fire("PLAYER_ENTERING_WORLD")             # still inside: no duplicate
    g.fire("ENCOUNTER_END", 1144, "拉克佐", 1, 5, 1)
    g.mock.instance = None
    g.fire("ZONE_CHANGED_NEW_AREA")
    g.fire("MERCHANT_SHOW")
    g.fire("MERCHANT_SHOW")                     # same NPC within a minute
    g.fire("ACHIEVEMENT_EARNED", 6)
    g.tick(15)                                  # first tick only learns the current state
    g.mock.taxi = True
    g.tick(15)
    g.mock.taxi = False
    g.tick(15)
    ev = [e for e in new_events(g, n) if e[0] not in ("LOCATION_SAMPLE", "ZONE_CHANGED")]
    assert [t for t, _ in ev] == ["MONEY_CHANGED", "XP_GAINED", "SKILL_UP", "COMBAT", "INSTANCE_ENTERED",
                                  "ENCOUNTER_END", "INSTANCE_LEFT", "NPC_INTERACTION",
                                  "ACHIEVEMENT_EARNED", "FLIGHT_STARTED", "FLIGHT_ENDED"]
    p = dict(ev)
    assert (p["MONEY_CHANGED"]["delta"], p["MONEY_CHANGED"]["total"]) == (250, 123706)
    assert p["XP_GAINED"]["amount"] == 300 and p["SKILL_UP"]["value"] == 76
    assert p["COMBAT"]["duration"] == 12
    assert (p["INSTANCE_ENTERED"]["name"], p["INSTANCE_ENTERED"]["instance_id"]) == ("死亡礦坑", 36)
    assert p["ENCOUNTER_END"]["success"] is True and p["ENCOUNTER_END"]["name"] == "拉克佐"
    assert p["NPC_INTERACTION"]["kind"] == "merchant" and p["ACHIEVEMENT_EARNED"]["name"] == "成就名稱"


def test_chat_is_off_by_default_and_per_channel():
    g = Game()
    g.login()
    n = len(g.events())
    say = ("大家好", "路人甲", "", "", "", "", 0, 0, "")
    g.fire("CHAT_MSG_SAY", *say)
    g.fire("CHAT_MSG_WHISPER", "私下說", "朋友", "", "", "", "", 0, 0, "")
    assert new_events(g, n) == []
    g.slash("chat say on")
    g.slash("chat trade on")
    g.fire("CHAT_MSG_SAY", *say)
    g.fire("CHAT_MSG_WHISPER", "私下說", "朋友", "", "", "", "", 0, 0, "")     # still off
    g.fire("CHAT_MSG_CHANNEL", "賣東西", "商人", "", "", "", "", 2, 2, "交易")
    g.fire("CHAT_MSG_CHANNEL", "閒聊", "路人", "", "", "", "", 1, 1, "綜合")    # general is off
    ev = new_events(g, n)
    assert [(t, p["channel_type"], p["sender"], p["message"]) for t, p in ev] == [
        ("CHAT_MESSAGE", "say", "路人甲", "大家好"), ("CHAT_MESSAGE", "trade", "商人", "賣東西")]
    assert ev[1][1]["channel_name"] == "交易"
    g.slash("chat all off")
    g.fire("CHAT_MSG_SAY", *say)
    assert len(new_events(g, n)) == 2
    g.login()                                    # setting persists
    g.slash("chat say on")
    assert g.db.settings.chat.say is True and not g.db.settings.chat.whisper


def test_sell_price_is_optional():
    g = Game()
    g.mock.itemInfoBroken = True                 # the client refuses GetItemInfo: record the rest anyway
    g.login()
    items = lua_list(g.db.characters[CHAR].data.bags["items"])
    assert [i.item_id for i in items] == [2589, 6948] and all(i.sell_price is None for i in items)


def test_refused_events_are_recorded_for_diagnosis():
    g = Game()
    g.lua.execute('MOCK.refuse = { CHAT_MSG_SAY = true, CHAT_MSG_CHANNEL = true }')
    g.login()                                     # the addon still loads and works
    assert list(g.db.debug.unregistered_events.values()) == ["CHAT_MSG_CHANNEL", "CHAT_MSG_SAY"]
    assert g.db.characters[CHAR].data.level is not None
    ok = Game()
    ok.login()
    assert len(ok.db.debug.unregistered_events) == 0


def test_completed_quests_are_recorded_and_refreshed_on_turn_in():
    g = Game()
    g.login()
    d = g.db.characters[CHAR].data
    assert lua_list(d.quests_done) == [7, 15, 783] and d.race_id == 4       # sorted ids
    g.lua.execute("MOCK.done = { 783, 7, 15, 12 }")
    g.mock.x = 0.61
    g.tick(15)                                   # nothing was turned in: not rescanned
    assert lua_list(g.db.characters[CHAR].data.quests_done) == [7, 15, 783]
    g.fire("QUEST_TURNED_IN", 12, 500, 0)
    g.mock.x = 0.63
    g.tick(15)
    assert lua_list(g.db.characters[CHAR].data.quests_done) == [7, 12, 15, 783]


def test_completed_quests_fall_back_to_the_older_api_or_stay_absent():
    g = Game()
    g.lua.execute("C_QuestLog.GetAllCompletedQuestIDs = nil; function GetQuestsCompleted() return { [40] = true, [9] = true } end")
    g.login()
    assert lua_list(g.db.characters[CHAR].data.quests_done) == [9, 40]
    none = Game()
    none.mock.doneBroken = True                  # neither works: everything else is still recorded
    none.login()
    d = none.db.characters[CHAR].data
    assert d.quests_done is None and len(lua_list(d.quests)) == 1


class Ev:
    def __init__(self, e):
        self.type, self.payload = e["event_type"], e["payload"]


def group_events(g):
    return [Ev(e) for e in g.events() if e["event_type"].startswith("GROUP_") or e["event_type"] == "MOB_KILLED"]


def test_group_session_from_join_to_leave():
    g = Game()
    g.login()
    assert not group_events(g)
    g.lua.execute('MOCK.grouped = true; MOCK.leader = true; '
                  'MOCK.party = { { name = "艾莉", class = "PRIEST", level = 23, guid = "Player-1-A" } }')
    g.fire("GROUP_ROSTER_UPDATE")
    g.fire("GROUP_ROSTER_UPDATE")                                  # repeated updates add nothing
    ev = group_events(g)
    assert [e.type for e in ev] == ["GROUP_JOINED"]
    joined = ev[0].payload
    gid = joined.group_id
    assert joined.size == 2 and joined.leader is True and joined.raid is False and joined.map_id == 1439
    m = lua_list(joined.members)
    assert (m[0].name, m[0]["class"], m[0].level) == ("艾莉", "PRIEST", 23)

    g.lua.execute('MOCK.party[2] = { name = "波波", class = "MAGE", level = 21, guid = "Player-1-B" }')
    g.fire("GROUP_ROSTER_UPDATE")
    mob = "Creature-0-1-0-1-547-000ABC"
    g.fire("PARTY_KILL", "Player-1-B", mob)                        # a group member's kill counts, with their name
    g.fire("PARTY_KILL", "Player-1-STRANGER", mob)                 # an outsider's does not
    g.mock.time += 600
    g.lua.execute('table.remove(MOCK.party, 1)')
    g.fire("GROUP_ROSTER_UPDATE")
    g.lua.execute('MOCK.grouped = false; MOCK.party = {}')
    g.fire("GROUP_ROSTER_UPDATE")
    ev = group_events(g)
    assert [e.type for e in ev] == ["GROUP_JOINED", "GROUP_MEMBER_JOINED", "MOB_KILLED", "GROUP_MEMBER_LEFT", "GROUP_LEFT"]
    assert ev[1].payload.name == "波波" and ev[1].payload.group_id == gid
    assert ev[2].payload.killed_by == "波波" and ev[2].payload.monster_id == 547
    assert ev[3].payload.name == "艾莉"
    assert ev[4].payload.group_id == gid and ev[4].payload.duration == 600
    g.fire("PARTY_KILL", "Player-1-B", mob)                        # no longer a group member
    assert len(group_events(g)) == 5


def test_group_survives_reload_and_names_still_loading():
    g = Game()
    g.login()
    g.lua.execute('MOCK.grouped = true; MOCK.party = { { name = "艾莉", class = "PRIEST", level = 23, guid = "Player-1-A" }, '
                  '{ name = "波波", class = "MAGE", level = 21, guid = "Player-1-B" } }')
    g.fire("GROUP_ROSTER_UPDATE")
    gid = group_events(g)[0].payload.group_id
    # right after a reload the client knows the group size but not every name yet
    g.lua.execute('MOCK.groupSize = 3; MOCK.party = { { name = "艾莉", class = "PRIEST", level = 23, guid = "Player-1-A" } }')
    g.load_addon()
    g.fire("PLAYER_LOGIN")
    assert [e.type for e in group_events(g)] == ["GROUP_JOINED"]   # same session, nobody "left"
    g.lua.execute('MOCK.party[2] = { name = "波波", class = "MAGE", level = 21, guid = "Player-1-B" }')
    g.fire("GROUP_ROSTER_UPDATE")
    assert [e.type for e in group_events(g)] == ["GROUP_JOINED"]
    # the group ended while logged out: closed at the next login
    g.lua.execute('MOCK.grouped = false; MOCK.party = {}; MOCK.groupSize = nil')
    g.load_addon()
    g.fire("PLAYER_LOGIN")
    ev = group_events(g)
    assert [e.type for e in ev] == ["GROUP_JOINED", "GROUP_LEFT"] and ev[1].payload.group_id == gid


def test_gear_details_and_stats():
    g = Game()
    g.lua.execute('MOCK.prices[3000] = 410; MOCK.bags[0][3] = { itemID = 3000, stackCount = 1, quality = 2, '
                  'hyperlink = "|Hitem:3000::::::::22|h[流浪者外衣]|h" }')
    g.login()
    d = g.db.characters[CHAR].data
    w = lua_list(d.equipment)[0]
    assert (w.slot, w.item_level, w.req_level, w.equip_loc, w.item_subtype, w.sell_price) == \
        (16, 21, 16, "INVTYPE_WEAPON", "匕首", 250)
    assert dict(w.stats) == {"agility": 3, "damage_per_second": 12.5}
    bag = {i.item_id: i for i in lua_list(d.bags["items"])}
    chest = bag[3000]
    assert (chest.name, chest.equip_loc, chest.item_level, chest.sell_price) == ("流浪者外衣", "INVTYPE_CHEST", 24, 410)
    assert dict(chest.stats) == {"armor": 88, "stamina": 5, "spirit": 4}
    assert bag[2589].equip_loc is None and bag[2589].stats is None and bag[2589].sell_price == 13   # not gear


def test_item_data_that_loads_late_is_picked_up():
    g = Game()
    g.lua.execute("MOCK.notLoaded = { [2140] = true }")
    g.login()
    w = lua_list(g.db.characters[CHAR].data.equipment)[0]
    assert w.item_id == 2140 and w.sell_price is None and w.stats is None      # recorded, details still missing
    g.lua.execute("MOCK.notLoaded = {}")
    g.fire("GET_ITEM_INFO_RECEIVED", 2140, True)
    g.mock.x = 0.61
    g.tick(15)
    w = lua_list(g.db.characters[CHAR].data.equipment)[0]
    assert w.sell_price == 250 and dict(w.stats)["agility"] == 3


def test_far_away_member_level_is_filled_in_later():
    g = Game()
    g.login()
    g.lua.execute('MOCK.grouped = true; MOCK.party = { { name = "鈴", class = "HUNTER", level = 0, guid = "Player-1-A" } }')
    g.fire("GROUP_ROSTER_UPDATE")
    g.lua.execute("MOCK.party[1].level = 24")
    g.fire("GROUP_ROSTER_UPDATE")
    g.fire("GROUP_ROSTER_UPDATE")                 # once is enough
    ev = [e for e in group_events(g) if e.type != "MOB_KILLED"]
    assert [e.type for e in ev] == ["GROUP_JOINED", "GROUP_MEMBER_JOINED"]
    assert (ev[1].payload.name, ev[1].payload.level, ev[1].payload.update) == ("鈴", 24, True)


def in_group(g, extra=""):
    g.login()
    g.lua.execute('MOCK.grouped = true; MOCK.party = { '
                  '{ name = "鈴", class = "HUNTER", level = 24, guid = "Player-1-A", pet = "Pet-0-1-A", x = 0.40, y = 0.50 }, '
                  '{ name = "波波", class = "MAGE", level = 21, guid = "Player-1-B" } }; MOCK.myPet = "Pet-0-1-ME"; ' + extra)
    g.fire("GROUP_ROSTER_UPDATE")
    return [e["payload"] for e in g.events() if e["event_type"] == "GROUP_JOINED"][0].group_id


def of(g, kind):
    return [e["payload"] for e in g.events() if e["event_type"] == kind]


def test_party_loot_deaths_and_level_ups():
    g = Game()
    gid = in_group(g)
    loot = "鈴獲得了物品: |cffffffff|Hitem:2589::::::::22:::::|h[亞麻布]|h|rx3。"
    g.fire("CHAT_MSG_LOOT", loot, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-A")
    g.fire("CHAT_MSG_LOOT", loot, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-STRANGER")     # not in the group
    pl = of(g, "PARTY_LOOT")
    assert len(pl) == 1 and (pl[0].looter, pl[0].item_id, pl[0].item_name, pl[0].count, pl[0].sell_price) == \
        ("鈴", 2589, "亞麻布", 3, 13)
    assert not of(g, "ITEM_LOOTED")                      # never mixed into the player's own loot

    g.lua.execute("MOCK.party[2].dead = true")
    g.fire("UNIT_HEALTH", "party2")
    g.fire("UNIT_HEALTH", "party2")                      # still dead: one event
    g.fire("UNIT_HEALTH", "target")                      # not a group unit
    died = of(g, "PARTY_MEMBER_DIED")
    assert [(d.name, d.group_id, d.map_id) for d in died] == [("波波", gid, 1439)]
    g.lua.execute("MOCK.party[2].dead = false")
    g.fire("UNIT_HEALTH", "party2")
    g.lua.execute("MOCK.party[2].dead = true")
    g.fire("UNIT_HEALTH", "party2")                      # died again after being revived
    assert len(of(g, "PARTY_MEMBER_DIED")) == 2

    g.lua.execute("MOCK.party[1].level = 25")
    g.fire("UNIT_LEVEL", "party1")
    g.fire("UNIT_LEVEL", "party1")
    up = of(g, "GROUP_MEMBER_LEVEL_UP")
    assert [(u.name, u.level) for u in up] == [("鈴", 25)]


def test_member_positions_only_when_they_move():
    g = Game()
    gid = in_group(g)
    g.tick(15)
    pos = of(g, "PARTY_POSITIONS")
    assert len(pos) == 1 and (pos[0].group_id, pos[0].map_id) == (gid, 1439)
    assert dict(pos[0].members["鈴"]) == {"x": 40.0, "y": 50.0} and pos[0].members["波波"] is None   # no position known
    g.tick(15)                                           # nobody moved
    assert len(of(g, "PARTY_POSITIONS")) == 1
    g.lua.execute("MOCK.party[1].x = 0.42")
    g.tick(15)
    assert len(of(g, "PARTY_POSITIONS")) == 2 and of(g, "PARTY_POSITIONS")[1].members["鈴"].x == 42.0
    g.lua.execute("MOCK.grouped = false; MOCK.party = {}")
    g.fire("GROUP_ROSTER_UPDATE")
    g.tick(15)
    assert len(of(g, "PARTY_POSITIONS")) == 2            # not while alone


def test_combat_log_is_not_used_and_pet_kills_count():
    g = Game()
    in_group(g)
    assert not g.lua.eval("MOCK.registered.COMBAT_LOG_EVENT_UNFILTERED")   # never registered: the client forbids it
    assert g.lua.eval("MOCK.registered.GROUP_ROSTER_UPDATE")
    g.fire("PLAYER_REGEN_DISABLED")
    g.fire("PLAYER_REGEN_ENABLED")
    assert not of(g, "GROUP_METER")
    mob = "Creature-0-1-0-1-547-000ABC"
    g.fire("PARTY_KILL", "Pet-0-1-A", mob)
    g.fire("PARTY_KILL", "Pet-0-1-ME", mob)
    assert [k.killed_by for k in of(g, "MOB_KILLED")] == ["鈴", None]


def test_says_when_the_combat_log_is_off_on_coming_into_the_game_and_only_then():
    said = lambda g: [line for line in g.mock.out.values() if "戰鬥紀錄" in line]   # noqa: E731
    box = "StaticPopupDialogs = {} SHOWN = {} function StaticPopup_Show(which) SHOWN[#SHOWN + 1] = which end "
    g = Game()
    g.lua.execute(box)
    g.login()
    dialog = g.lua.eval("StaticPopupDialogs.ADVENTURECOLLECTOR_COMBATLOG")
    assert len(said(g)) == 1 and "/combatlog" in said(g)[0]              # a line of chat
    assert g.lua.eval("#SHOWN") == 1 and "/combatlog" in dialog.text     # and a dialog, with the command on it
    # Seen in the game: a while after the log is on the client answers "off" all the same.
    # So nothing asks again during play, whatever happens.
    g.mock.time += 3600
    g.fire("GROUP_JOINED")
    g.tick(60)
    assert len(said(g)) == 1 and g.lua.eval("#SHOWN") == 1 and g.mock.button.text.startswith("存檔")
    # saving reloads the interface: that is not coming into the game
    g = Game()
    g.lua.execute(box)
    g.load_addon()
    g.fire("PLAYER_ENTERING_WORLD", False, True)
    g.fire("PLAYER_LOGIN")
    assert not said(g) and g.lua.eval("#SHOWN") == 0
    assert "沒有開" in g.slash("combatlog")[-1]                           # asked for: answered all the same
    # on already, told to keep quiet, or a client that will not say: nothing is said
    for prepare in ("MOCK.logging = true", "AdventureCollectorDB = { settings = { combatLogRemind = false } }", "MOCK.nologging = true"):
        g = Game()
        g.lua.execute(box + prepare)
        g.login()
        assert not said(g) and g.lua.eval("#SHOWN") == 0, prepare


def test_the_dialogs_button_switches_the_combat_log_on_and_never_off():
    box = "StaticPopupDialogs = {} function StaticPopup_Show() end function ChatFrame_OpenChat(t) TYPED = t end "
    last = lambda g: list(g.mock.out.values())[-1]                       # noqa: E731
    g = Game()
    g.lua.execute(box)
    g.login()
    dialog = g.lua.eval("StaticPopupDialogs.ADVENTURECOLLECTOR_COMBATLOG")
    dialog.OnAccept()
    assert g.mock.logging is True and "不用再輸入" in last(g) and g.lua.eval("TYPED") is None
    assert g.db.debug.combat_log_switch.answer == "true"
    # Seen in the game: the switch worked, the game still answered "off", the command was put in
    # the chat box, and pressing Enter switched the log off again. A switch the game took is on.
    g = Game()
    g.lua.execute(box + "LoggingCombat = function(on) if on then SWITCHED = true end return false end")
    g.login()
    g.lua.eval("StaticPopupDialogs.ADVENTURECOLLECTOR_COMBATLOG").OnAccept()
    assert g.lua.eval("SWITCHED") is True and g.lua.eval("TYPED") is None and "不用再輸入" in last(g)
    # only a client that refuses gets the command typed in for it
    g = Game()
    g.lua.execute(box + "LoggingCombat = function(on) if on ~= nil then error('refused') end return false end")
    g.login()
    dialog = g.lua.eval("StaticPopupDialogs.ADVENTURECOLLECTOR_COMBATLOG")
    dialog.OnAccept()
    assert g.lua.eval("TYPED") == "/combatlog" and "Enter" in last(g)
    dialog.OnAlt()                                                       # "do not remind me again"
    assert g.db.settings.combatLogRemind is False


def test_the_combat_log_can_be_switched_on_at_every_login():
    g = Game()
    g.login()
    g.slash("combatlog auto")
    assert g.mock.logging is True and g.db.settings.combatLogAuto is True
    g = Game()                                                           # the next time the game is started
    g.lua.execute("AdventureCollectorDB = { settings = { combatLogAuto = true } }")
    g.login()
    assert g.mock.logging is True and "已替你開啟" in list(g.mock.out.values())[-1]


def test_blocked_calls_are_recorded_even_before_login():
    g = Game()
    g.load_addon()
    g.fire("ADDON_ACTION_FORBIDDEN", "AdventureCollector", "Frame:RegisterEvent()")    # during loading
    g.fire("ADDON_ACTION_FORBIDDEN", "SomeOtherAddon", "Whatever()")                   # not ours
    g.fire("PLAYER_LOGIN")
    assert dict(g.db.debug.blocked) == {"forbidden:Frame:RegisterEvent()": 1}
    assert g.db.settings.matePositions is not False      # an unrelated call switches nothing off
    g.lua.execute('MOCK.grouped = true; MOCK.party = { { name = "鈴", class = "HUNTER", level = 24, '
                  'guid = "Player-1-A", x = 0.40, y = 0.50 } }')
    g.fire("GROUP_ROSTER_UPDATE")
    g.tick(15)
    assert len(of(g, "PARTY_POSITIONS")) == 1
    g.fire("ADDON_ACTION_BLOCKED", "AdventureCollector", "C_Map.GetPlayerMapPosition()")
    g.lua.execute("MOCK.party[1].x = 0.45")
    g.tick(15)
    assert g.db.settings.matePositions is False and len(of(g, "PARTY_POSITIONS")) == 1
    assert len(dict(g.db.debug.blocked)) == 2


def test_loot_messages_say_how_the_item_arrived():
    g = Game()
    in_group(g)
    link = "|cffffffff|Hitem:2589::::::::22:::::|h[亞麻布]|h|rx5。"
    g.fire("CHAT_MSG_LOOT", "你拾取了物品：" + link, "", "", "", "", "", 0, 0, "", 0, 0, CHAR)
    g.fire("CHAT_MSG_LOOT", "你製造了：" + link, "", "", "", "", "", 0, 0, "", 0, 0, CHAR)
    g.fire("CHAT_MSG_LOOT", "你收到了物品：" + link, "", "", "", "", "", 0, 0, "", 0, 0, CHAR)
    assert [e.how for e in of(g, "ITEM_LOOTED")] == [None, "created", "received"]
    g.fire("CHAT_MSG_LOOT", "鈴收到了物品：" + link, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-A")
    g.fire("CHAT_MSG_LOOT", "鈴拾取了物品：" + link, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-A")
    assert [e.how for e in of(g, "PARTY_LOOT")] == ["received", None]
    # a roll for group loot is a choice, not a pick-up, whoever makes it
    roll = "|HlootHistory:3|h[戰利品]|h: 你選擇了貪婪：" + link
    g.fire("CHAT_MSG_LOOT", roll, "", "", "", "", "", 0, 0, "", 0, 0, CHAR)
    g.fire("CHAT_MSG_LOOT", roll, "", "", "", "", "", 0, 0, "", 0, 0, "Player-1-A")
    g.fire("CHAT_MSG_LOOT", roll)
    assert len(of(g, "ITEM_LOOTED")) == 3 and len(of(g, "PARTY_LOOT")) == 2
