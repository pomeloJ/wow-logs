"""Run the addon under Lua 5.1 with a mocked WoW API, and write SavedVariables
the way the game does. This is a mock: it proves logic, not in-game behaviour."""
from pathlib import Path

import lupa.lua51 as lupa

CORE = Path(__file__).resolve().parent.parent / "addon" / "AdventureCollector" / "Core.lua"

PRELUDE = r"""
MOCK = { time = 1795000000, map = 1439, x = 0.538, y = 0.751, level = 24,
         guid = "Player-1234-00ABCDEF", zone = "黑海岸", out = {} }
time = function() return MOCK.time end
function GetServerTime() return MOCK.time end
LOOT_ITEM_CREATED_SELF = "你製造了：%s。"
LOOT_ITEM_PUSHED_SELF = "你收到了物品：%s。"
LOOT_ITEM_PUSHED = "%s收到了物品：%s。"
MOCK.party = {}                -- other members: { name, class, level, guid }
local function mate(unit)
    local i = type(unit) == "string" and tonumber(unit:match("^party(%d)$") or unit:match("^raid(%d+)$"))
    return i and MOCK.party[i] or nil
end
function UnitGUID(unit)
    if unit == "pet" then return MOCK.myPet end
    local i = type(unit) == "string" and tonumber(unit:match("^partypet(%d)$"))
    if i then return MOCK.party[i] and MOCK.party[i].pet or nil end
    local m = mate(unit)
    if m then return m.guid end
    return MOCK.guid
end
function UnitName(unit)
    if type(unit) == "string" and unit ~= "player" and unit ~= "target" then
        local m = mate(unit)
        return m and m.name or nil
    end
    return "測試德魯伊", nil
end
function UnitIsDeadOrGhost(unit) local m = mate(unit) return m and m.dead or false end
function CombatLogGetCurrentEventInfo() return unpack(MOCK.cleu or {}, 1, 16) end
function IsInGroup() return MOCK.grouped or false end
-- The game's own combat log: asked with no argument, switched with one.
function LoggingCombat(on)
    if MOCK.nologging then error("refused") end
    if on ~= nil then MOCK.logging = on and true or false end
    return MOCK.logging or false
end
function IsInRaid() return false end
function GetNumGroupMembers() return MOCK.grouped and (MOCK.groupSize or #MOCK.party + 1) or 0 end
function UnitIsGroupLeader() return MOCK.leader or false end
UNKNOWNOBJECT = "未知"
function GetRealmName() return MOCK.realm or "ExampleRealm" end
function GetNormalizedRealmName() return "NormalizedRealm" end
function UnitClass(unit) local m = mate(unit) if m then return m.class, m.class, 1 end return "德魯伊", "DRUID", 11 end
function UnitRace() return "夜精靈", "NightElf", 4 end
function UnitFactionGroup() return "Alliance", "聯盟" end
function UnitLevel(unit) local m = mate(unit) if m then return m.level end return MOCK.level end
function GetMoney() return 123456 end
function GetRealZoneText() return MOCK.zone end
function GetZoneText() return MOCK.zone end
function GetSubZoneText() return "" end
function GetBuildInfo() return "1.60.1", "71037", "Nov 2026", 16001 end
function issecretvalue(v) return MOCK.secret ~= nil and v == MOCK.secret end
C_Map = {
    GetBestMapForUnit = function() return MOCK.map end,
    GetPlayerMapPosition = function(_, unit)
        if MOCK.nopos then return nil end
        local i = type(unit) == "string" and tonumber(unit:match("^party(%d)$"))
        if i then
            local m = MOCK.party[i]
            if not m or not m.x then return nil end
            return { x = m.x, y = m.y, GetXY = function(s) return s.x, s.y end }
        end
        return { x = MOCK.x, y = MOCK.y, GetXY = function(s) return s.x, s.y end }
    end,
}
C_Timer = { NewTicker = function(_, f) MOCK.tick = f return {} end,
            After = function(_, f) f() end }
C_Reputation = {
    GetNumFactions = function() return 3 end,
    GetFactionDataByIndex = function(i)
        return ({ { name = "聯盟", isHeader = true },
                  { factionID = 72, name = "暴風城", reaction = 5, currentStanding = 4200,
                    currentReactionThreshold = 3000, nextReactionThreshold = 9000 },
                  { factionID = 21, name = "藏寶海灣", reaction = 4, currentStanding = 500,
                    currentReactionThreshold = 0, nextReactionThreshold = 3000, atWarWith = false } })[i]
    end,
}
function UnitStat(_, i) return 10 * i, 10 * i + 5 end
function UnitArmor() return 300, 420 end
function UnitHealthMax() return 980 end
function UnitPowerMax() return 640 end
function IsMounted() return false end
function RequestTimePlayed() MOCK.playedRequests = (MOCK.playedRequests or 0) + 1 end
function IsInInstance() return MOCK.instance ~= nil, "party" end
function GetInstanceInfo() return MOCK.instance, "party", 1, "普通", 5, 0, false, 36 end
function GetAchievementInfo(id) return id, "成就名稱" end
function UnitOnTaxi() return MOCK.taxi or false end
C_TaxiMap = { GetAllTaxiNodes = function() return {
    { nodeID = 1, name = "哨兵嶺", state = 0 }, { nodeID = 2, name = "暴風城", state = 1 },
    { nodeID = 3, name = "夜色鎮", state = 2 } } end }
UIParent = { GetWidth = function() return 1000 end, GetHeight = function() return 500 end,
             GetEffectiveScale = function() return 1 end }
MOCK.canvas = { 200, 800, 450, 50 }   -- left, right, top, bottom in UI units (origin bottom-left)
WorldMapFrame = {
    shown = false,
    IsShown = function(self) return self.shown end,
    GetMapID = function() return 1436 end,
    ScrollContainer = { Child = {
        GetEffectiveScale = function() return 1 end,
        GetLeft = function() return MOCK.canvas[1] end, GetRight = function() return MOCK.canvas[2] end,
        GetTop = function() return MOCK.canvas[3] end, GetBottom = function() return MOCK.canvas[4] end,
    } },
}
function ToggleWorldMap() WorldMapFrame.shown = true end
C_Map.GetMapInfo = function() return { name = "西部荒野" } end
function Screenshot() MOCK.shots = (MOCK.shots or 0) + 1 fire("SCREENSHOT_SUCCEEDED") end
function UnitXP() return 4200 end
function UnitXPMax() return 21000 end
function GetXPExhaustion() return 1500 end
function IsResting() return false end
function GetBindLocation() return "暴風城" end
function GetGuildInfo() return nil end
MOCK.equip = { [16] = { 2140, "|cff1eff00|Hitem:2140::::::::22:::::|h[雕花匕首]|h|r" } }
function GetInventoryItemID(_, slot) local e = MOCK.equip[slot] return e and e[1] end
function GetInventoryItemLink(_, slot) local e = MOCK.equip[slot] return e and e[2] end
function GetInventoryItemDurability() return 30, 35 end
MOCK.prices = { [2589] = 13, [2140] = 250, [6948] = 0 }      -- vendor sell price per item, in copper
-- Gear known to the mock client: slot, type and stats by item id.
MOCK.gear = { [2140] = { loc = "INVTYPE_WEAPON", type = "武器", sub = "匕首", ilvl = 21, req = 16,
                         stats = { ITEM_MOD_AGILITY_SHORT = 3, ITEM_MOD_DAMAGE_PER_SECOND_SHORT = 12.5 } },
              [3000] = { loc = "INVTYPE_CHEST", type = "護甲", sub = "皮甲", ilvl = 24, req = 19,
                         stats = { RESISTANCE0_NAME = 88, ITEM_MOD_STAMINA_SHORT = 5, ITEM_MOD_SPIRIT_SHORT = 4 } } }
local function itemId(ref) return type(ref) == "number" and ref or tonumber(tostring(ref):match("item:(%d+)")) end
C_Item = { GetItemInfo = function(ref)
               if MOCK.itemInfoBroken then error("restricted") end
               local id = itemId(ref)
               local g = MOCK.gear[id]
               if MOCK.notLoaded and MOCK.notLoaded[id] then return nil end
               return "x", "x", 2, g and g.ilvl or 1, g and g.req or 1, g and g.type or "t", g and g.sub or "s", 20,
                      g and g.loc or "", 1, MOCK.prices[id]
           end,
           GetItemStats = function(ref)
               local g = MOCK.gear[itemId(ref)]
               return g and g.stats or {}
           end,
           GetItemNameByID = function() return "名稱" end }
MOCK.bags = { [0] = { { itemID = 2589, stackCount = 12, quality = 1, itemName = "亞麻布" },
                      { itemID = 2589, stackCount = 8, quality = 1, itemName = "亞麻布" }, false,
                      { itemID = 6948, stackCount = 1, quality = 1, hyperlink = "|Hitem:6948|h[爐石]|h" } } }
C_Container = {
    GetContainerNumSlots = function(bag) return MOCK.bags[bag] and #MOCK.bags[bag] or 0 end,
    GetContainerNumFreeSlots = function(bag) return MOCK.bags[bag] and 1 or 0 end,
    GetContainerItemInfo = function(bag, slot)
        if MOCK.bagsBroken then error("restricted") end
        return MOCK.bags[bag][slot] or nil
    end,
}
MOCK.done = { 783, 7, 15 }
MOCK.quests = { { title = "西部荒野", isHeader = true },
                { title = "人民軍", questID = 12, level = 12 },
                { title = "隱藏", questID = 99, isHidden = true } }
C_QuestLog = {
    GetNumQuestLogEntries = function() return #MOCK.quests end,
    GetInfo = function(i) return MOCK.quests[i] end,
    GetQuestObjectives = function() return { { text = "迪菲亞盜賊 3/15", finished = false } } end,
    IsComplete = function() return false end,
    GetAllCompletedQuestIDs = function()
        if MOCK.doneBroken then error("restricted") end
        return MOCK.done
    end,
    GetTitleForQuestID = function(id) return id == 12 and "人民軍" or nil end,
}
function GetProfessions() return 1, nil, nil, 4 end
function GetProfessionInfo(i) if i == 1 then return "草藥學", 0, 75, 150 end return "釣魚", 0, 20, 75 end
function InCombatLockdown() return MOCK.combat or false end
function ReloadUI() MOCK.reloads = (MOCK.reloads or 0) + 1 end
function CreateFrame(kind, name)
    -- Unknown widget methods are no-ops, like a permissive stand-in for the real UI.
    -- What they return (a texture, a font string, an animation) is as permissive again.
    local part
    part = setmetatable({}, { __index = function() return function() return part end end })
    local f = setmetatable({ events = {}, scripts = {}, shown = true },
        { __index = function() return function() return part end end })
    function f:RegisterEvent(e)
        if MOCK.refuse and MOCK.refuse[e] then error("unknown event " .. e) end
        self.events[e] = true
        MOCK.registered = MOCK.registered or {}
        MOCK.registered[e] = true
    end
    function f:UnregisterEvent(e) self.events[e] = nil end
    function f:SetText(t) self.text = t end
    function f:Show() self.shown = true end
    function f:Hide() self.shown = false end
    function f:SetScript(name, fn)
        self.scripts[name] = fn
        if name == "OnEvent" then MOCK.onEvent = fn end
    end
    if name == "AdventureCollectorCombatLogSign" then MOCK.sign = f
    elseif name == "AdventureCollectorAIPanel" then MOCK.aipanel = f
    elseif name == "AdventureCollectorAICode" then MOCK.aicode = f
    elseif kind == "Button" then MOCK.button = f else MOCK.frame = f end
    return f
end
function fire(e, ...) MOCK.onEvent(MOCK.frame, e, ...) end
-- what the AI screenshot panel reads
function GetPlayerFacing() return MOCK.facing end
function UnitHealth(unit) return unit == "target" and 40 or 900 end
function UnitPowerType() return 0 end
function UnitPower() return 320 end
function UnitAffectingCombat() return MOCK.combat or false end
function GetShapeshiftForm() return 0 end
function UnitExists(unit) return unit == "target" and MOCK.target or unit == "player" end
function UnitClassification() return "elite" end
function UnitCreatureType() return "人型生物" end
function UnitReaction() return 2 end
function GetComboPoints() return 3 end
date = os.date
-- map rects and key bindings (the AI screenshot tests switch these on: Game(world=True))
WORLD_MOCK = [[
C_Map.GetWorldPosFromMapPos = function(_, v) return 0, { x = -9000 + v.x * 3000, y = 500 - v.y * 2000 } end
function CreateVector2D(x, y) return { x = x, y = y } end
MOCK.bindings = {}
function GetBindingKey(action) for k, a in pairs(MOCK.bindings) do if a == action then return k end end end
function GetBindingAction(key) return MOCK.bindings[key] or "" end
function SetBinding(key, action) MOCK.bindings[key] = action return true end
function GetCurrentBindingSet() return 2 end
function SaveBindings(set) MOCK.savedBindings = set end
]]
print = function(s) MOCK.out[#MOCK.out + 1] = s end
"""


class Game:
    def __init__(self, world=False):
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(PRELUDE)
        if world:
            self.lua.execute(self.lua.globals().WORLD_MOCK)
        self.mock = self.lua.globals().MOCK
        self.ns = None

    def load_addon(self):
        """Like a fresh UI load (login or /reload): runs the file, fires ADDON_LOADED."""
        chunk = self.lua.eval("loadstring")(CORE.read_text(encoding="utf-8"), "Core.lua")
        assert chunk is not None, "Core.lua has a syntax error"
        self.ns = self.lua.table()
        chunk("AdventureCollector", self.ns)
        self.fire("ADDON_LOADED", "AdventureCollector")

    def login(self):
        self.load_addon()
        self.fire("PLAYER_LOGIN")

    def fire(self, event, *args):
        self.lua.globals().fire(event, *args)

    def tick(self, seconds=15):
        self.mock.time += seconds
        self.mock.tick()

    def slash(self, msg=""):
        self.lua.globals().SlashCmdList.ADVENTURECOLLECTOR(msg)
        return list(self.mock.out.values())

    @property
    def db(self):
        return self.lua.globals().AdventureCollectorDB

    def events(self):
        return [dict(e) for e in self.db.events.values()]

    def saved_variables(self) -> str:
        return "\nAdventureCollectorDB = " + _dump(self.db, 0) + "\n"

    def logout(self, path: Path):
        # Verified in game: at logout the client no longer reports a position.
        self.mock.nopos = True
        self.fire("PLAYER_LOGOUT")
        self.mock.nopos = False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.saved_variables(), encoding="utf-8")


def _dump(v, depth):
    if lupa.lua_type(v) == "table":
        n = len(v)
        lines = []
        for i in range(1, n + 1):
            lines.append(f"{_dump(v[i], depth + 1)}, -- [{i}]")
        for k, val in v.items():
            if isinstance(k, int) and 1 <= k <= n:
                continue
            key = f'["{_esc(k)}"]' if isinstance(k, str) else f"[{k}]"
            lines.append(f"{key} = {_dump(val, depth + 1)},")
        return "{\n" + "\n".join(lines) + "\n}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return f'"{_esc(v)}"'


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
