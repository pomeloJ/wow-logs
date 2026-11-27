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
    assert "人民軍" not in text                       # quests and history are left to the server
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


def test_system_messages_appear_but_not_the_history():
    g = setup()
    g.fire("CHAT_MSG_LOOT", "你拾取了物品：|cffffffff|Hitem:2589::::::::|h[亞麻布]|h|r。")
    for msg in ("一", "二", "迪菲亞盜賊已殺死：4/15"):
        g.fire("UI_INFO_MESSAGE", 0, msg)
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "亞麻布" not in text
    assert "迪菲亞盜賊已殺死：4/15" in text and "二" in text and "提示|r  一" not in text   # the last two


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


def decode(bits):
    """What the sync program reads back from the squares (see adventuresync.extras.read_code)."""
    def num(a, n):
        return int("".join(str(b) for b in bits[a:a + n]), 2)
    assert bits[:8] == [1, 0, 1, 1, 0, 0, 1, 0]
    m, x, y, f, check = num(8, 16), num(24, 17), num(41, 17), num(58, 9), num(67, 5)
    assert (m + x + y + f) % 32 == check
    return m, x, y, f


def test_the_position_in_the_panel_is_the_one_coded_into_the_picture():
    g = setup(facing=1.5707963)
    g.slash("aishot")
    bits = [int(b) for b in g.ns.aiState.lastCode.values()]
    assert len(bits) == 72 and decode(bits) == (1439, 53800, 75100, 270)
    ev = [dict(e["payload"]) for e in g.events() if e["event_type"] == "AI_SCREENSHOT"][-1]
    assert (ev["map_id"], ev["x"], ev["y"], ev["facing"]) == (1439, 53.8, 75.1, 270)
    assert g.mock.aicode.shown is False                 # hidden with the panel once the picture is taken
    # not known: all ones, and still a valid code
    assert decode([int(b) for b in g.ns.shotCode(None, None, None, None).values()]) == (0, 131071, 131071, 511)


def test_secret_health_is_said_hidden_and_a_percent_is_used_when_given():
    g = setup()
    g.lua.execute('MOCK.secret = "S" UnitHealth = function() return "S" end '
                  'UnitPowerType = function() return 1 end UnitPower = function(_, k) return k == 1 and 35 or "S" end '
                  'UnitPowerMax = function(_, k) return k == 1 and 100 or 640 end '
                  'UnitPowerPercent = function(_, k) return k == 0 and 0.4 or nil end')
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "生命 遊戲隱藏" in text and "怒氣 35/100 (35%)" in text and "法力 40%" in text   # a bear
    assert g.db.debug.resources["生命"] == "secret" and g.db.debug.resources["生命_pct"] == "missing"
    # what the game hides is handed back to it to write on the panel, never kept
    res = g.ns.aiState.res
    assert res.fmt.endswith("生命 %d/%d · 怒氣 35/100 (35%%) · 法力 40%%") and list(res.args.values()) == ["S", 980]
    ev = [dict(e["payload"]) for e in g.events() if e["event_type"] == "AI_SCREENSHOT"][-1]
    assert ev.get("health") is None and ev["power"] == 35 and "S" not in str(ev)


def test_minimap_and_the_panel_size():
    g = setup()
    g.lua.execute('Minimap = { GetZoom = function() return 2 end, GetZoomLevels = function() return 6 end } '
                  'C_CVar = { GetCVar = function(k) return k == "rotateMinimap" and "0" or nil end } '
                  'function IsIndoors() return false end C_Map.GetMapWorldSize = function() return 3000, 2000 end '
                  'function GetScreenWidth() return 1920 end function GetScreenHeight() return 1080 end')
    g.slash("aishot")
    text = g.ns.aiState.lastText
    assert "縮放 2/5 · 不旋轉（上方=北） · 戶外 · 寬約 333 碼（地圖寬 11.1%）" in text
    ev = [dict(e["payload"]) for e in g.events() if e["event_type"] == "AI_SCREENSHOT"][-1]
    assert dict(ev["minimap"])["yards"] == 333 and dict(ev["minimap"])["rotate"] is False
