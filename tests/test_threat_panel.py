from pathlib import Path

from wowmock import Game


PANEL = Path(__file__).resolve().parents[1] / "addon/AdventureCollector/ThreatPanel.lua"


def game():
    g = Game()
    g.login()
    g.lua.execute('''
        MOCK.threat = {
            player = {false, 0, 88, 96, 8200},
            pet = {true, 3, 255, 255, 8500},
            party1 = {false, 0, 40, 49, 4200}
        }
        function UnitCanAttack() return true end
        function UnitExists(u) return MOCK.target and (u == "target" or MOCK.threat[u] ~= nil) end
        function UnitGUID(u) return u == "raid1" and "player" or u end
        function UnitName(u) return u end
        function UnitDetailedThreatSituation(u) return unpack(MOCK.threat[u] or {}) end
        MOCK.target = true
        MOCK.grouped = true
    ''')
    chunk = g.lua.eval("loadstring")(PANEL.read_text(encoding="utf-8"), "ThreatPanel.lua")
    chunk("AdventureCollector", g.ns)
    g.ns.initThreatPanel()
    return g


def test_live_ranking_and_player_warning():
    g = game()
    d = g.ns.collectLiveThreat()
    assert [r.value for r in d.rows.values()] == [8500, 8200, 4200]
    assert d.me.percent == 88 and d.me.rank == 2
    assert d.message == "接近 OT・注意仇恨"
    assert d.rows[1].holding and d.rows[1].percent == 100


def test_target_change_and_restricted_values_never_show_stale_safety():
    g = game()
    g.mock.secret = 8200
    assert g.ns.collectLiveThreat().message == "無自身仇恨資料"
    g.mock.secret = None
    g.lua.execute('MOCK.threat.player = {false, 0, 232, 255, 0}')
    assert g.ns.collectLiveThreat().me.percent is None
    g.mock.target = False
    d = g.ns.collectLiveThreat()
    assert len(d.rows) == 0 and d.me is None
    assert d.message == "請選取敵對目標"
    g.mock.target = True
    g.lua.execute('UnitDetailedThreatSituation = nil')
    assert g.ns.collectLiveThreat().message == "此版本無仇恨 API"


def test_combat_visibility_settings_and_warning_threshold():
    g = game()
    p = g.ns.threatPanel
    assert not p.shown
    g.mock.combat = True
    g.ns.refreshThreatPanel()
    assert p.shown
    command = g.lua.globals().SlashCmdList.ADVENTURETHREAT
    command("warn 95")
    assert g.ns.collectLiveThreat().message == "仇恨低於警示門檻"
    command("off")
    assert not p.shown
    command("show")
    g.mock.combat = False
    g.ns.refreshThreatPanel()
    assert p.shown
    command("scale 100")
    assert g.db.settings.threatPanel.scale == 1.6
    command("lock")
    assert g.db.settings.threatPanel.locked
    command("reset")
    assert g.db.settings.threatPanel.scale == 1
    command("auto")
    assert not p.shown
    # The separate driver's update must work while the panel is hidden.
    g.mock.combat = True
    g.mock.frame.scripts.OnUpdate(g.mock.frame, 0.2)
    assert p.shown


def test_raid_deduplicates_player_and_keeps_own_row_visible():
    g = game()
    g.lua.execute('''
        function IsInRaid() return true end
        function GetNumGroupMembers() return 10 end
        for i = 1, 10 do MOCK.threat["raid" .. i] = {false, 0, 80, 80, 20000 - i} end
    ''')
    d = g.ns.collectLiveThreat()
    assert len(d.rows) == 11  # self, pet, nine other raiders; raid1 is self
    assert sum(1 for r in d.rows.values() if r.key == "player") == 1
    g.mock.combat = True
    g.ns.refreshThreatPanel()  # exercises overflow and own-row substitution
    assert d.me.rank > 8


def test_toc_module_loads_before_login_and_core_initializes_panel():
    g = Game()
    g.load_addon()
    chunk = g.lua.eval("loadstring")(PANEL.read_text(encoding="utf-8"), "ThreatPanel.lua")
    chunk("AdventureCollector", g.ns)
    g.fire("PLAYER_LOGIN")
    assert g.ns.threatPanel is not None
