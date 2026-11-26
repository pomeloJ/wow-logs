"""The AI screenshot key: a panel of live facts, a screenshot, the panel gone again."""
from wowmock import Game

GUID = "Creature-0-4378-0-12-1726-0000ABCD"


def setup(**mock):
    g = Game(world=True)
    for k, v in mock.items():
        setattr(g.mock, k, v)
    g.login()
    return g


def test_panel_holds_position_facing_target_and_quests_then_hides():
    g = setup(facing=1.5707963, target=True)       # facing west
    g.lua.execute("local old = UnitGUID; UnitGUID = function(u) if u == 'target' then return '%s' end return old(u) end" % GUID)
    shots = g.mock.shots or 0
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "UiMapID 1439" in text
    assert "53.800, 75.100" in text
    assert "270° 西" in text                          # bearing clockwise from north
    assert "NPC ID 1726" in text and "精英" in text and "敵對" in text and "連擊點數 3" in text
    assert "[12] 人民軍" in text and "迪菲亞盜賊 3/15" in text
    assert "法力 320/640" in text
    assert (g.mock.shots or 0) == shots + 1
    assert g.ns.aiState.busy is None and g.mock.aipanel.shown is False
    ev = [e for e in g.events() if e["event_type"] == "AI_SCREENSHOT"]
    assert len(ev) == 1 and dict(ev[0]["payload"])["target_id"] == 1726


def test_unavailable_values_are_said_not_guessed():
    g = setup()
    g.lua.execute("GetPlayerFacing = nil; C_Map.GetPlayerMapPosition = function() return nil end")
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "座標|r  unavailable" in text
    assert "面向|r  unavailable" in text
    assert "目標|r  無" in text


def test_recent_events_and_system_messages_appear():
    g = setup()
    g.fire("CHAT_MSG_LOOT", "你拾取了物品：|cffffffff|Hitem:2589::::::::|h[亞麻布]|h|r。")
    g.fire("UI_INFO_MESSAGE", 0, "迪菲亞盜賊已殺死：4/15")
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "拾取 亞麻布 x1 (item 2589)" in text
    assert "迪菲亞盜賊已殺死：4/15" in text


def test_test_mode_takes_no_screenshot_and_failure_hides():
    g = setup()
    shots = g.mock.shots or 0
    g.slash("aishot test")
    assert (g.mock.shots or 0) == shots
    g.lua.execute("Screenshot = function() MOCK.shots = (MOCK.shots or 0) + 1 fire('SCREENSHOT_FAILED') end")
    out = g.slash("aishot")
    assert any("AI 截圖失敗" in str(line) for line in out)
    assert g.ns.aiState.busy is None


def test_f12_is_bound_once_and_only_when_free():
    g = setup()
    assert g.mock.bindings["F12"] == "ADVENTURECOLLECTOR_AISHOT" and g.mock.savedBindings == 2
    g.mock.bindings["F12"] = None
    g.login()                                       # offered once: an unbound key stays unbound
    assert g.mock.bindings["F12"] is None
    h = Game(world=True)
    h.mock.bindings["F12"] = "TOGGLEFPS"
    h.login()
    assert h.mock.bindings["F12"] == "TOGGLEFPS"


def test_map_rect_recorded_once_per_map():
    g = setup()
    rects = [dict(e["payload"]) for e in g.events() if e["event_type"] == "MAP_RECT"]
    assert len(rects) == 1
    r = rects[0]
    assert r["map_id"] == 1439 and r["continent_id"] == 0
    assert (r["world_x0"], r["world_y0"], r["world_x1"], r["world_y1"]) == (-9000, 500, -6000, -1500)
    g.mock.x = 0.6
    g.tick(15)
    assert sum(e["event_type"] == "MAP_RECT" for e in g.events()) == 1
