-- AdventureCollector (Phase 1): character snapshot + location events.
-- Data is only written to SavedVariables; the AdventureSync agent uploads it.
local ADDON_NAME, ns = ...
ADDON_NAME = ADDON_NAME or "AdventureCollector"
ns = ns or {}

local VERSION = "0.3.2"
local DEFAULTS = {
    enabled = true,
    locationEnabled = true,
    locationInterval = 15, -- seconds between position checks
    minMove = 0.5,         -- map percent points before a new sample is stored
    heartbeat = 60,        -- seconds; store a sample anyway after this long
    maxEvents = 5000,      -- oldest events are dropped beyond this
    showButton = true,     -- on-screen "save now" button
    requestPlayed = true,  -- ask for /played once per login (prints two chat lines)
    combatLogRemind = true, -- say so in chat when the game's combat log is off
    combatLogAuto = true,   -- switch the game's combat log on at login instead of only saying so
    aiShotDelay = 0.2,      -- seconds between showing the AI screenshot panel and taking the picture
    aiFontSize = 15,        -- panel text size (unscaled UI units; about 21 px at 1080p)
}

local db, charId, myGuid
-- Current group, rebuilt on every roster change (not saved): member guid -> name, member name
-- -> unit token, and pet guid -> owner name ("" for the player's own pet).
local groupGuids, groupUnits, petOwners = {}, {}, {}
local sessionEvents = 0
local last = { map = nil, x = nil, y = nil, t = 0 }
local ticker
local saveButton

local combatLogOn, combatLogPopup   -- defined with the combat log reminder below
local function updateButton()
    if saveButton then saveButton:SetText("存檔 (" .. sessionEvents .. ")") end
end

-- ------------------------------------------------------------ helpers
local function isSecret(v)
    return type(issecretvalue) == "function" and issecretvalue(v) or false
end

-- Call an API that may be missing, restricted or erroring. Returns nil on failure.
local function safe(fn, ...)
    if type(fn) ~= "function" then return nil end
    local r = { pcall(fn, ...) }
    if not r[1] then return nil end
    return unpack(r, 2, table.maxn(r))
end

-- Only plain, serialisable, non-secret scalars are ever stored.
local function plain(v)
    if v == nil or isSecret(v) then return nil end
    local t = type(v)
    if t == "string" or t == "number" or t == "boolean" then return v end
    return nil
end

local function now()
    local t = plain(safe(GetServerTime))
    if type(t) ~= "number" then t = time() end
    return t
end

local function say(msg)
    print("|cff33ff99AdventureCollector|r: " .. tostring(msg))
end

-- ------------------------------------------------------------ combat log reminder
-- Damage figures and kills inside dungeons come from the file the game writes while its own
-- combat log is on (/combatlog). Nothing else tells the player it is off, and a session
-- played without it has no figures at all, so say so on coming into the game.
-- Only then: seen in the game, a while after the log is switched on the client answers "off"
-- although the file is being written, so nothing may keep asking and nagging during play.
-- Saving reloads the interface, which is not coming into the game: no reminder after that.
local combatLogAsked, reloaded

-- true or false; nil when the client does not say (no such call, or it was refused).
function combatLogOn()
    if not db or db.settings.combatLogCheck == false or type(LoggingCombat) ~= "function" then return nil end
    local ok, on = pcall(LoggingCombat)
    if not ok or isSecret(on) then return nil end
    return (on or (combatLogAsked and now() - combatLogAsked < 10)) and true or false
end

-- Switch the game's combat log on. true: it is on. false: the game refused, the player has to
-- type the command. Seen in the game: the switch works, yet asking straight afterwards still
-- answers "off" -- and typing /combatlog then switches it off again. So a switch the game took
-- without complaint counts as on for the next few seconds, whatever it answers; after that the
-- game's own answer is believed again, which is how a log the player switches off shows up.
local function switchCombatLogOn()
    local ok, answer = pcall(LoggingCombat, true)
    local read = combatLogOn()
    if db and type(db.debug) == "table" then      -- what this client really answers, to read back later
        db.debug.combat_log_switch = { ok = ok, answer = isSecret(answer) and "secret" or tostring(answer),
                                       read = tostring(read), at = now() }
    end
    if not ok or db.settings.combatLogCheck == false then return false end
    combatLogAsked = now()
    return true
end

-- A line of chat scrolls away in a busy group, so the same thing is also put up as a dialog
-- with a button that switches the combat log on. Nothing here may stop the reminder itself.
local POPUP = "ADVENTURECOLLECTOR_COMBATLOG"
function combatLogPopup()
    if type(UIErrorsFrame) == "table" and type(UIErrorsFrame.AddMessage) == "function" then
        pcall(UIErrorsFrame.AddMessage, UIErrorsFrame, "請確認戰鬥紀錄已開啟", 1, 0.82, 0)
    end
    if type(StaticPopupDialogs) ~= "table" or type(StaticPopup_Show) ~= "function" then return end
    StaticPopupDialogs[POPUP] = StaticPopupDialogs[POPUP] or {
        -- Asked, not asserted: this client's answer is not always right, the log may well be on.
        -- The button only ever switches it on; the command would switch a running log off.
        text = "請確認戰鬥紀錄是否已開啟。\n\n傷害統計、技能分析與副本內的擊殺都靠它，沒開就不會記錄。"
            .. "\n\n按「確認開啟」就會開著，已經開著也沒關係。\n\n"
            .. "自己輸入 |cffffff00/combatlog|r 是切換：原本開著會變成關閉。",
        button1 = "確認開啟", button2 = "略過", button3 = "不再提醒",
        OnAccept = function()
            if switchCombatLogOn() then say("戰鬥紀錄已確認開啟，不用再輸入指令。") return end
            -- This client will not let an addon switch it on: put the command in the chat box,
            -- so that all that is left to do is press Enter.
            if type(ChatFrame_OpenChat) == "function" and pcall(ChatFrame_OpenChat, "/combatlog") then
                say("插件沒辦法代你開啟：指令已經放進聊天輸入框，|cffffff00按 Enter|r 就會開啟。")
            else
                say("插件沒辦法代你開啟，請在聊天輸入框輸入 |cffffff00/combatlog|r 再按 Enter。")
            end
        end,
        OnAlt = function()
            if db then db.settings.combatLogRemind = false end
            say("進遊戲時不再提醒確認戰鬥紀錄。要恢復：|cffffff00/adventure combatlog quiet|r")
        end,
        timeout = 0, whileDead = true, hideOnEscape = true, preferredIndex = 3,
    }
    pcall(StaticPopup_Show, POPUP)
end

local function checkCombatLog(again)
    local on = combatLogOn()
    -- Switched on every time, whatever the game answers: its answer is not always right, and
    -- switching on a log that is already on changes nothing (only the /combatlog command toggles).
    if db.settings.combatLogAuto and switchCombatLogOn() then
        if not reloaded or again then say("戰鬥紀錄已開啟（每次登入自動開），這次遊玩會有傷害統計。") end
        return true
    end
    if on ~= false then return on end
    if not db.settings.combatLogRemind or (reloaded and not again) then return false end
    say("|cffffd100請確認戰鬥紀錄是否已開啟|r，沒開就不會有傷害統計。按視窗上的「確認開啟」；"
        .. "想要每次登入自動開，輸入 |cffffff00/adventure combatlog on|r")
    combatLogPopup()
    return false
end
ns.checkCombatLog = checkCombatLog

local function newInstallationId()
    local function hex() return string.format("%08x", math.random(0, 2147483646)) end
    return string.format("%x", now()) .. hex() .. hex()
end

-- ------------------------------------------------------------ storage
local function initDB()
    if type(AdventureCollectorDB) ~= "table" then AdventureCollectorDB = {} end
    db = AdventureCollectorDB
    db.schemaVersion = 1
    -- A missing id or counter means we cannot continue the old sequence safely,
    -- so start a fresh namespace instead of risking duplicate event ids.
    if type(db.installationId) ~= "string" or db.installationId == ""
        or type(db.nextSequence) ~= "number" then
        db.installationId = newInstallationId()
        db.nextSequence = 1
    end
    if type(db.settings) ~= "table" then db.settings = {} end
    if type(db.characters) ~= "table" then db.characters = {} end
    if type(db.events) ~= "table" then db.events = {} end
    for k, v in pairs(DEFAULTS) do
        if type(db.settings[k]) ~= type(v) then db.settings[k] = v end
    end
    -- Up to 0.3.0 the switch was off by default, and that default was stored like a choice. A
    -- logout for being idle came back without the log, so now it is on for everyone; only a
    -- choice made with the command from now on (combatLogAutoChosen) is kept.
    if not db.settings.combatLogAutoChosen then db.settings.combatLogAuto = true end
    if type(db.settings.chat) ~= "table" then db.settings.chat = {} end -- per-channel, all off
end

local function trimEvents()
    local max = db.settings.maxEvents
    local n = #db.events
    if n <= max + 200 then return end
    local kept = {}
    for i = n - max + 1, n do kept[#kept + 1] = db.events[i] end
    db.events = kept
end

local function addEvent(eventType, payload)
    if not db or not charId or not db.settings.enabled then return nil end
    local seq = db.nextSequence
    db.nextSequence = seq + 1
    local ev = {
        event_id = db.installationId .. ":" .. string.format("%010d", seq),
        character_id = charId,
        event_type = eventType,
        ts = now(),
        payload = payload or {},
    }
    db.events[#db.events + 1] = ev
    sessionEvents = sessionEvents + 1
    trimEvents()
    updateButton()
    return ev
end

-- ------------------------------------------------------------ game data
-- Returns mapID, x, y where x/y are 0-100 percentages on that map (2 decimals).
-- x/y are nil where the client does not expose a position (e.g. instances).
local function getPosition()
    if type(C_Map) ~= "table" then return nil end
    local mapID = plain(safe(C_Map.GetBestMapForUnit, "player"))
    if type(mapID) ~= "number" then return nil end
    local pos = safe(C_Map.GetPlayerMapPosition, mapID, "player")
    local x, y
    if pos ~= nil and not isSecret(pos) and type(pos) == "table" and pos.GetXY then
        x, y = safe(pos.GetXY, pos)
        x, y = plain(x), plain(y)
    end
    if type(x) ~= "number" or type(y) ~= "number" then return mapID, nil, nil end
    return mapID, math.floor(x * 10000 + 0.5) / 100, math.floor(y * 10000 + 0.5) / 100
end

local function zoneName()
    return plain(safe(GetRealZoneText)) or plain(safe(GetZoneText))
end

-- GetRealmName() returns "" on some clients; never store an empty realm.
local function realmName()
    local realm = plain(safe(GetRealmName))
    if type(realm) ~= "string" or realm == "" then realm = plain(safe(GetNormalizedRealmName)) end
    if type(realm) ~= "string" or realm == "" then return nil end
    return realm
end

local function resolveCharacterId()
    local guid = plain(safe(UnitGUID, "player"))
    if type(guid) == "string" and guid ~= "" then return guid, guid end
    local name = plain(safe(UnitName, "player")) or "Unknown"
    local realm = realmName() or "UnknownRealm"
    return realm .. ":" .. name, nil
end

-- ------------------------------------------------------------ extended data
-- Bags, equipment, quests and professions are only rescanned after the game
-- reports a change (dirty flag), not on every snapshot.
local dirty = { equipment = true, bags = true, quests = true, professions = true, reputations = true, stats = true,
                quests_done = true }
local cache = {}
local questTitles = {}
local scanners = {}

local function itemName(link, id)
    if type(link) == "string" then
        local n = link:match("%[(.-)%]")
        if n and n ~= "" then return n end
    end
    if id and type(C_Item) == "table" then return plain(safe(C_Item.GetItemNameByID, id)) end
    return nil
end

-- What a vendor pays for one of this item, in copper (nil when the client has not loaded
-- the item yet, 0 for things that cannot be sold).
local function sellPrice(id)
    local fn = type(C_Item) == "table" and C_Item.GetItemInfo or GetItemInfo
    local price = plain((select(11, safe(fn, id))))
    return type(price) == "number" and price or nil
end

-- What the client knows about an item: vendor price, level, slot, type and, for gear, its
-- stats. `ref` is an item link when there is one (random-suffix gear needs the link to give
-- its real stats) or an item id. Everything is nil until the client has loaded the item.
local function itemDetails(ref)
    local fn = type(C_Item) == "table" and C_Item.GetItemInfo or GetItemInfo
    local _, _, _, ilvl, minLevel, itemType, subType, _, equipLoc, _, price = safe(fn, ref)
    local function num(v) v = plain(v) return type(v) == "number" and v or nil end
    local function str(v) v = plain(v) return type(v) == "string" and v ~= "" and v or nil end
    local out = { sell_price = num(price), item_level = num(ilvl), req_level = num(minLevel),
                  item_type = str(itemType), item_subtype = str(subType), equip_loc = str(equipLoc) }
    if out.equip_loc and type(ref) == "string" then
        local statsFn = type(C_Item) == "table" and C_Item.GetItemStats or GetItemStats
        local stats = safe(statsFn, ref)
        if type(stats) == "table" then
            local s, any = {}, false
            for key, value in pairs(stats) do
                value = plain(value)
                if type(key) == "string" and type(value) == "number" then
                    -- ITEM_MOD_STAMINA_SHORT -> stamina; RESISTANCE0_NAME is armor
                    local short = key == "RESISTANCE0_NAME" and "armor"
                        or key:gsub("^ITEM_MOD_", ""):gsub("_SHORT$", ""):gsub("_NAME$", ""):lower()
                    s[short] = math.floor(value * 100 + 0.5) / 100     -- weapon DPS has long fractions
                    any = true
                end
            end
            if any then out.stats = s end
        end
    end
    return out
end

function scanners.equipment()
    local out = {}
    for slot = 1, 19 do
        local id = plain(safe(GetInventoryItemID, "player", slot))
        if type(id) == "number" then
            local link = plain(safe(GetInventoryItemLink, "player", slot))
            local cur, max = safe(GetInventoryItemDurability, slot)
            local _, _, quality = safe(type(C_Item) == "table" and C_Item.GetItemInfo or nil, id)
            local e = itemDetails(link or id)
            e.slot, e.item_id, e.name, e.quality = slot, id, itemName(link, id), plain(quality)
            e.durability, e.durability_max = plain(cur), plain(max)
            out[#out + 1] = e
        end
    end
    return out
end

-- What is in these containers: stacks of one item added up.
local function scanContainers(first, final, list)
    if type(C_Container) ~= "table" then return nil end
    local byId, items, total, free = {}, {}, 0, 0
    for i = first, final do
        local bag = list and list[i] or i
        local n = plain(safe(C_Container.GetContainerNumSlots, bag))
        if type(n) == "number" then
            total = total + n
            local f = plain(safe(C_Container.GetContainerNumFreeSlots, bag))
            if type(f) == "number" then free = free + f end
            for slot = 1, n do
                local info = safe(C_Container.GetContainerItemInfo, bag, slot)
                local id = type(info) == "table" and plain(info.itemID) or nil
                if type(id) == "number" then
                    local e = byId[id]
                    if not e then
                        local link = plain(info.hyperlink)
                        e = itemDetails(type(link) == "string" and link or id)
                        e.item_id, e.count, e.quality = id, 0, plain(info.quality)
                        e.name = plain(info.itemName) or itemName(link, id)
                        byId[id] = e
                        items[#items + 1] = e
                    end
                    local c = plain(info.stackCount)
                    e.count = e.count + (type(c) == "number" and c or 1)
                end
            end
        end
    end
    return { slots = total, free = free, items = items }
end

function scanners.bags() return scanContainers(0, 4) end

-- Every quest this character has ever turned in (ids, sorted), including ones done before
-- the addon was installed. The client offers this under one of two names.
function scanners.quests_done()
    local out = {}
    local ids = type(C_QuestLog) == "table" and safe(C_QuestLog.GetAllCompletedQuestIDs) or nil
    if type(ids) == "table" then
        for _, id in ipairs(ids) do
            id = plain(id)
            if type(id) == "number" then out[#out + 1] = id end
        end
    else
        local done = safe(GetQuestsCompleted)        -- older form: { [questID] = true }
        if type(done) ~= "table" then return nil end
        for id, flag in pairs(done) do
            if type(id) == "number" and flag then out[#out + 1] = id end
        end
    end
    table.sort(out)
    return out
end

function scanners.quests()
    if type(C_QuestLog) ~= "table" then return nil end
    local n = plain(safe(C_QuestLog.GetNumQuestLogEntries))
    if type(n) ~= "number" then return nil end
    local out, header = {}, nil
    for i = 1, n do
        local info = safe(C_QuestLog.GetInfo, i)
        if type(info) == "table" then
            local title, id = plain(info.title), plain(info.questID)
            if info.isHeader then
                header = title
            elseif not info.isHidden and type(id) == "number" then
                questTitles[id] = title
                local objectives = {}
                local list = safe(C_QuestLog.GetQuestObjectives, id)
                if type(list) == "table" then
                    for _, o in ipairs(list) do
                        local text = plain(o.text)
                        if text then
                            objectives[#objectives + 1] = {
                                text = text, finished = plain(o.finished) and true or false,
                            }
                        end
                    end
                end
                out[#out + 1] = {
                    quest_id = id, title = title, level = plain(info.level), category = header,
                    complete = plain(safe(C_QuestLog.IsComplete, id)) and true or false,
                    objectives = objectives,
                }
            end
        end
    end
    return out
end

function scanners.professions()
    local out = {}
    local idx = { safe(GetProfessions) }
    for i = 1, table.maxn(idx) do
        local index = plain(idx[i])
        if type(index) == "number" then
            local name, _, rank, maxRank = safe(GetProfessionInfo, index)
            name = plain(name)
            if name then out[#out + 1] = { name = name, rank = plain(rank), max_rank = plain(maxRank) } end
        end
    end
    return out
end

function scanners.reputations()
    if type(C_Reputation) ~= "table" then return nil end
    local n = plain(safe(C_Reputation.GetNumFactions))
    if type(n) ~= "number" then return nil end
    local out = {}
    for i = 1, n do
        local f = safe(C_Reputation.GetFactionDataByIndex, i)
        if type(f) == "table" and (not f.isHeader or f.isHeaderWithRep) then
            local name = plain(f.name)
            if name then
                out[#out + 1] = {
                    faction_id = plain(f.factionID), name = name, standing = plain(f.reaction),
                    value = plain(f.currentStanding), floor = plain(f.currentReactionThreshold),
                    ceiling = plain(f.nextReactionThreshold), at_war = plain(f.atWarWith) and true or false,
                }
            end
        end
    end
    return out
end

-- Skipped in combat, where the client hides some of these values.
function scanners.stats()
    if type(InCombatLockdown) == "function" and InCombatLockdown() then return nil end
    local out = {}
    for i, key in ipairs({ "strength", "agility", "stamina", "intellect", "spirit" }) do
        local _, effective = safe(UnitStat, "player", i)
        out[key] = plain(effective)
    end
    local _, armor = safe(UnitArmor, "player")
    out.armor = plain(armor)
    out.health_max = plain(safe(UnitHealthMax, "player"))
    out.power_max = plain(safe(UnitPowerMax, "player"))
    return out
end

local function takeSnapshot(levelOverride)
    if not db or not charId then return end
    local _, classFile = safe(UnitClass, "player")
    local _, raceFile, raceId = safe(UnitRace, "player")
    local mapID, x, y = getPosition()
    db.characters[charId] = {
        captured_at = now(),
        data = {
            name = plain(safe(UnitName, "player")),
            realm = realmName(),
            guid = plain(safe(UnitGUID, "player")),
            race = plain(raceFile),
            race_id = plain(raceId),
            class = plain(classFile),
            faction = plain(safe(UnitFactionGroup, "player")),
            level = levelOverride or plain(safe(UnitLevel, "player")),
            money = plain(safe(GetMoney)),
            map_id = mapID,
            zone_name = zoneName(),
            sub_zone = plain(safe(GetSubZoneText)),
            x = x,
            y = y,
            addon_version = VERSION,
            xp = plain(safe(UnitXP, "player")),
            xp_max = plain(safe(UnitXPMax, "player")),
            rested_xp = plain(safe(GetXPExhaustion)),
            resting = plain(safe(IsResting)),
            bind_location = plain(safe(GetBindLocation)),
            guild = plain(safe(GetGuildInfo, "player")),
            mounted = plain(safe(IsMounted)),
        },
    }
    local data = db.characters[charId].data
    for key, scan in pairs(scanners) do
        if dirty[key] then
            -- A failed scan (restricted or secret data) keeps the previous result.
            local result = safe(scan)
            if result ~= nil then
                cache[key] = result
                dirty[key] = false
            end
        end
        data[key] = cache[key]
    end
    if type(db.extra) == "table" and type(db.extra[charId]) == "table" then
        for key, value in pairs(db.extra[charId]) do data[key] = value end
    end
end

-- Maps 1411-1461 have backgrounds from the wiki map set; anything else (new
-- Forever zones) needs a one-off /adventure mapshot. Remind once per session.
local reminded = {}

local function remindMapBackground(mapID)
    if (mapID >= 1411 and mapID <= 1461) or reminded[mapID] then return end
    reminded[mapID] = true
    if type(db.mapShots) == "table" and db.mapShots[mapID] then return end
    say("這張地圖（" .. tostring(zoneName() or mapID) .. "）在 Dashboard 還沒有底圖，輸入 /adventure mapshot 可以補上")
end

-- Where a map lies in its continent's world coordinates (yards), asked of the client once
-- per map. With it the server turns map percent into real distances and joins zones.
local function recordMapRect(mapID)
    if type(db.mapRects) ~= "table" then db.mapRects = {} end
    if db.mapRects[mapID] or type(C_Map) ~= "table" or type(C_Map.GetWorldPosFromMapPos) ~= "function"
        or type(CreateVector2D) ~= "function" then return end
    local function corner(u, v)
        local continent, pos = safe(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(u, v))
        if type(pos) ~= "table" or isSecret(pos) then return nil end
        local wx, wy
        if pos.GetXY then wx, wy = safe(pos.GetXY, pos) else wx, wy = pos.x, pos.y end
        wx, wy = plain(wx), plain(wy)
        if type(wx) ~= "number" or type(wy) ~= "number" then return nil end
        return plain(continent), math.floor(wx * 100 + 0.5) / 100, math.floor(wy * 100 + 0.5) / 100
    end
    local c0, x0, y0 = corner(0, 0)
    local c1, x1, y1 = corner(1, 1)
    if type(c0) ~= "number" or c0 ~= c1 or not x1 or x0 == x1 or y0 == y1 then return end
    local info = safe(C_Map.GetMapInfo, mapID)
    info = type(info) == "table" and info or {}
    if addEvent("MAP_RECT", { map_id = mapID, map_name = plain(info.name), parent_map_id = plain(info.parentMapID),
            map_type = plain(info.mapType), continent_id = c0,
            world_x0 = x0, world_y0 = y0, world_x1 = x1, world_y1 = y1 }) then
        db.mapRects[mapID] = true
    end
end

local function samplePosition(force)
    if not db or not db.settings.locationEnabled then return end
    local mapID, x, y = getPosition()
    if not mapID or not x then return end
    local t = now()
    local s = db.settings
    local moved = last.map ~= mapID or not last.x
        or math.abs(x - last.x) >= s.minMove or math.abs(y - last.y) >= s.minMove
    if not (force or moved or (t - last.t) >= s.heartbeat) then return end
    if addEvent("LOCATION_SAMPLE", { map_id = mapID, x = x, y = y, zone_name = zoneName() }) then
        last.map, last.x, last.y, last.t = mapID, x, y, t
        takeSnapshot() -- keep the snapshot current while position data is valid
        remindMapBackground(mapID)
        pcall(recordMapRect, mapID)
    end
end

local function startTicker()
    if ticker or type(C_Timer) ~= "table" or type(C_Timer.NewTicker) ~= "function" then return end
    ticker = C_Timer.NewTicker(db.settings.locationInterval, function()
        samplePosition(false)
        if ns.onTick then ns.onTick() end
    end)
end

-- ------------------------------------------------------------ events
local handlers = {}

function handlers.ADDON_LOADED(name)
    if name == ADDON_NAME then initDB() end
end

function handlers.PLAYER_LOGIN()
    if not db then initDB() end
    charId, myGuid = resolveCharacterId()
    takeSnapshot()
    if type(db.debug) ~= "table" then db.debug = {} end
    db.debug.unregistered_events = ns.unregistered or {}      -- saved for diagnosis
    db.debug.version_loaded = now()                           -- when this copy of the addon started
    if next(ns.blocked or {}) then                            -- calls refused before the saved table existed
        if type(db.debug.blocked) ~= "table" then db.debug.blocked = {} end
        for key, n in pairs(ns.blocked) do db.debug.blocked[key] = (db.debug.blocked[key] or 0) + n end
        ns.blocked = {}
    end
    local _, build, _, toc = safe(GetBuildInfo)
    addEvent("SESSION_START", {
        level = plain(safe(UnitLevel, "player")),
        addon_version = VERSION,
        build = plain(build),
        interface = plain(toc),
    })
    -- Right at login the client still reports the continent map; wait for the zone.
    if type(C_Timer) == "table" and type(C_Timer.After) == "function" then
        -- Not forced: skipped when the zone-change handler already stored this position.
        C_Timer.After(3, function()
            samplePosition(false)
            ns.initCounters()
            ns.syncGroup()          -- still grouped after a reload, or left while offline
            if db.settings.requestPlayed then safe(RequestTimePlayed) end
            checkCombatLog()
            pcall(ns.bindAIShot)
        end)
    else
        samplePosition(true)
        checkCombatLog()
    end
    startTicker()
    pcall(ns.createButton) -- UI problems must never stop data collection
end

function handlers.ZONE_CHANGED_NEW_AREA()
    if not charId then return end
    local mapID, x, y = getPosition()
    addEvent("ZONE_CHANGED", { map_id = mapID, x = x, y = y, zone_name = zoneName() })
    if ns.checkInstance then ns.checkInstance() end
    takeSnapshot()
    samplePosition(true)
end

function handlers.PLAYER_LEVEL_UP(level)
    if not charId then return end
    level = plain(level)
    addEvent("LEVEL_UP", { level = level })
    dirty.stats = true
    takeSnapshot(type(level) == "number" and level or nil)
end

-- No snapshot at PLAYER_LOGOUT: verified in game that position (and likely other
-- values) are already unavailable then and would overwrite good data.

-- Position fields shared by gameplay events.
local function here(payload)
    local mapID, x, y = getPosition()
    payload.map_id, payload.x, payload.y, payload.zone_name = mapID, x, y, zoneName()
    return payload
end

-- The client loads item data lazily; rescan once it arrives so prices and stats fill in.
function handlers.GET_ITEM_INFO_RECEIVED() dirty.equipment, dirty.bags = true, true end
function handlers.PLAYER_EQUIPMENT_CHANGED() dirty.equipment, dirty.stats = true, true end
function handlers.QUEST_LOG_UPDATE() dirty.quests = true end
function handlers.SKILL_LINES_CHANGED() dirty.professions = true end

local function questTitle(id)
    local title = type(C_QuestLog) == "table" and plain(safe(C_QuestLog.GetTitleForQuestID, id)) or nil
    if type(title) == "string" and title ~= "" then questTitles[id] = title end
    return questTitles[id]
end

function handlers.QUEST_ACCEPTED(questID)
    questID = plain(questID)
    if not charId or type(questID) ~= "number" then return end
    addEvent("QUEST_ACCEPTED", here({ quest_id = questID, quest_name = questTitle(questID) }))
    dirty.quests = true
end

local turnedIn = {}

function handlers.QUEST_TURNED_IN(questID, xpReward, moneyReward)
    questID = plain(questID)
    if not charId or type(questID) ~= "number" then return end
    turnedIn[questID] = true
    addEvent("QUEST_COMPLETED", here({
        quest_id = questID, quest_name = questTitle(questID),
        xp_reward = plain(xpReward), money_reward = plain(moneyReward),
    }))
    dirty.quests, dirty.quests_done = true, true
end

-- QUEST_REMOVED fires for turn-ins as well as abandons. Only a removal with no
-- matching turn-in is stored, and it is not labelled "abandoned": the client
-- does not say whether the quest was abandoned or failed.
function handlers.QUEST_REMOVED(questID)
    questID = plain(questID)
    if not charId or type(questID) ~= "number" then return end
    local function finish()
        if turnedIn[questID] then
            turnedIn[questID] = nil
            return
        end
        addEvent("QUEST_REMOVED", here({ quest_id = questID, quest_name = questTitles[questID] }))
        dirty.quests = true
    end
    if type(C_Timer) == "table" and type(C_Timer.After) == "function" then
        C_Timer.After(1, finish) -- the turn-in event may arrive just after this one
    else
        finish()
    end
end

function handlers.PLAYER_DEAD()
    if not charId then return end
    addEvent("PLAYER_DIED", here({ level = plain(safe(UnitLevel, "player")) }))
end

-- The loot channel also announces things that were not picked up: crafted items, and items
-- handed over by a vendor or a quest. The client's own message formats tell them apart, in
-- whatever language it runs: the longest fixed piece of each format is looked for in the text.
local LOOT_FORMATS = {
    created = { "LOOT_ITEM_CREATED_SELF", "LOOT_ITEM_CREATED_SELF_MULTIPLE", "TRADESKILL_LOG_FIRSTPERSON",
                "TRADESKILL_LOG_THIRDPERSON" },
    received = { "LOOT_ITEM_PUSHED_SELF", "LOOT_ITEM_PUSHED_SELF_MULTIPLE", "LOOT_ITEM_PUSHED",
                 "LOOT_ITEM_PUSHED_MULTIPLE" },
}
local lootMarks
local function lootKind(text)
    if not lootMarks then
        lootMarks = {}
        for kind, names in pairs(LOOT_FORMATS) do
            for _, name in ipairs(names) do
                local fmt = _G[name]
                if type(fmt) == "string" then
                    local best = ""
                    for piece in fmt:gsub("%%%d*%$?[sd]", "\0"):gmatch("[^%z]+") do
                        if #piece > #best then best = piece end
                    end
                    if #best >= 4 then lootMarks[#lootMarks + 1] = { kind, best } end
                end
            end
        end
    end
    for _, mark in ipairs(lootMarks) do
        if text:find(mark[2], 1, true) then return mark[1] end
    end
    return nil                                     -- an ordinary pick-up
end

-- Loot lines are localised text; only the item link inside is parsed.
function handlers.CHAT_MSG_LOOT(text, ...)
    text = plain(text)
    if not charId or type(text) ~= "string" then return end
    -- Group loot rolls ("[Loot]: X selected Greed for [item]", "... won [item]") start with a
    -- link to the roll window. They announce choices, not pick-ups: the pick-up has its own line.
    local firstLink = text:match("|H(%a+):")
    if firstLink and firstLink ~= "item" then return end
    local guid = plain((select(11, ...)))
    local id = tonumber(text:match("|Hitem:(%d+)"))
    local name = text:match("|Hitem:[^|]*|h%[(.-)%]|h") or text:match("%[(.-)%]")
    if type(guid) == "string" and guid ~= "" and guid ~= myGuid then
        -- Someone else's loot: kept only for members of the current group, under their name.
        local mate = groupGuids[guid]
        if mate and id then
            addEvent("PARTY_LOOT", {
                looter = mate, item_id = id, item_name = name,
                count = tonumber(text:match("|h|rx(%d+)")) or 1, sell_price = sellPrice(id),
                how = lootKind(text),
            })
        end
        return
    end
    if not id then return end
    addEvent("ITEM_LOOTED", here({
        item_id = id,
        item_name = name,
        count = tonumber(text:match("|h|rx(%d+)")) or 1,
        sell_price = sellPrice(id),
        how = lootKind(text),                      -- "created" / "received"; nil when picked up
    }))
end

-- Names of recently targeted units, so a kill can be given a name.
local seenNames, seenOrder = {}, {}

function handlers.PLAYER_TARGET_CHANGED()
    local guid, name = plain(safe(UnitGUID, "target")), plain(safe(UnitName, "target"))
    if type(guid) ~= "string" or type(name) ~= "string" then return end
    if not seenNames[guid] then
        seenOrder[#seenOrder + 1] = guid
        if #seenOrder > 100 then seenNames[table.remove(seenOrder, 1)] = nil end
    end
    seenNames[guid] = name
end

-- ------------------------------------------------------------ groups
-- A group session runs from joining a party or raid until leaving it. Its id and roster are
-- kept in the saved table so a /reload or a disconnect in the middle does not split it.

local function savedGroup()
    if type(db.groups) ~= "table" then db.groups = {} end
    return db.groups[charId]
end

-- Current members other than the player, and whether every name is known yet.
local function roster()
    if not plain(safe(IsInGroup)) then return nil end
    local raid = plain(safe(IsInRaid)) and true or false
    local n = plain(safe(GetNumGroupMembers))
    n = type(n) == "number" and n or 0
    local out, me = {}, plain(safe(UnitName, "player"))
    groupGuids, groupUnits, petOwners = {}, {}, {}
    local myPet = plain(safe(UnitGUID, "pet"))
    if type(myPet) == "string" then petOwners[myPet] = "" end
    for i = 1, raid and n or 4 do
        local unit = (raid and "raid" or "party") .. i
        local name = plain(safe(UnitName, unit))
        if type(name) == "string" and name ~= "" and name ~= me and name ~= UNKNOWNOBJECT then
            local _, class = safe(UnitClass, unit)
            local guid = plain(safe(UnitGUID, unit))
            out[#out + 1] = { name = name, class = plain(class), level = plain(safe(UnitLevel, unit)) }
            if type(guid) == "string" then groupGuids[guid] = name end
            groupUnits[name] = unit
            local pet = plain(safe(UnitGUID, (raid and "raidpet" or "partypet") .. i))
            if type(pet) == "string" then petOwners[pet] = name end
        end
    end
    return out, raid, #out >= n - 1
end

local function syncGroup()
    if not charId or not db then return end
    local members, raid, complete = roster()
    local g = savedGroup()
    if members and not g then
        g = { id = db.installationId .. ":g" .. now(), started = now(), members = {} }
        for _, m in ipairs(members) do g.members[m.name] = (tonumber(m.level) or 0) > 0 and m.level or true end
        db.groups[charId] = g
        addEvent("GROUP_JOINED", here({
            group_id = g.id, members = members, size = #members + 1, raid = raid,
            leader = plain(safe(UnitIsGroupLeader, "player")) and true or false,
        }))
    elseif members and g then
        if type(g.members) ~= "table" then g.members = {} end
        local present = {}
        for _, m in ipairs(members) do
            present[m.name] = true
            local known = (tonumber(m.level) or 0) > 0
            if not g.members[m.name] then
                g.members[m.name] = known and m.level or true
                addEvent("GROUP_MEMBER_JOINED", { group_id = g.id, name = m.name, class = m.class, level = m.level })
            elseif g.members[m.name] == true and known then
                -- A member too far away reports level 0; fill it in once they come into range.
                g.members[m.name] = m.level
                addEvent("GROUP_MEMBER_JOINED",
                    { group_id = g.id, name = m.name, class = m.class, level = m.level, update = true })
            elseif type(g.members[m.name]) == "number" and known and m.level > g.members[m.name] then
                g.members[m.name] = m.level
                addEvent("GROUP_MEMBER_LEVEL_UP", { group_id = g.id, name = m.name, level = m.level })
            end
        end
        if complete then    -- names still loading must not look like people leaving
            for name in pairs(g.members) do
                if not present[name] then
                    g.members[name] = nil
                    addEvent("GROUP_MEMBER_LEFT", { group_id = g.id, name = name })
                end
            end
        end
    elseif not members and g then
        db.groups[charId] = nil
        groupGuids, groupUnits, petOwners = {}, {}, {}
        addEvent("GROUP_LEFT", here({ group_id = g.id, duration = now() - (tonumber(g.started) or now()) }))
    end
end
ns.syncGroup = syncGroup

function handlers.GROUP_ROSTER_UPDATE() syncGroup() end

local function isGroupUnit(unit)
    unit = plain(unit)
    return type(unit) == "string" and (unit:find("^party%d") or unit:find("^raid%d")) and true or false
end

function handlers.UNIT_LEVEL(unit) if isGroupUnit(unit) then syncGroup() end end
function handlers.UNIT_PET() syncGroup() end          -- a pet was called or dismissed

-- A member dying: seen as their unit turning dead, since the combat log may be restricted.
local deadMates = {}
local function checkDeaths()
    if not charId or not next(groupUnits) then return end
    for name, unit in pairs(groupUnits) do
        local dead = plain(safe(UnitIsDeadOrGhost, unit)) and true or false
        if dead and not deadMates[name] then
            addEvent("PARTY_MEMBER_DIED", here({ group_id = (savedGroup() or {}).id, name = name }))
        end
        deadMates[name] = dead or nil
    end
end
function handlers.UNIT_HEALTH(unit) if isGroupUnit(unit) then checkDeaths() end end

-- Where the other members are, for those on the player's own map (the client gives no
-- position for anyone elsewhere or inside an instance). Only members who moved are stored.
local lastMate = {}
local function sampleGroup()
    if not charId or not db or not db.settings.locationEnabled or not next(groupUnits) then return end
    if db.settings.matePositions == false then return end
    local g = savedGroup()
    local mapID = getPosition()
    if not g or not mapID then return end
    local moved, any = {}, false
    for name, unit in pairs(groupUnits) do
        local pos = safe(C_Map.GetPlayerMapPosition, mapID, unit)
        if pos ~= nil and not isSecret(pos) and type(pos) == "table" and pos.GetXY then
            local x, y = safe(pos.GetXY, pos)
            x, y = plain(x), plain(y)
            if type(x) == "number" and type(y) == "number" and (x ~= 0 or y ~= 0) then
                x, y = math.floor(x * 10000 + 0.5) / 100, math.floor(y * 10000 + 0.5) / 100
                local l = lastMate[name]
                if not l or l.map ~= mapID or math.abs(x - l.x) >= db.settings.minMove
                    or math.abs(y - l.y) >= db.settings.minMove then
                    lastMate[name] = { map = mapID, x = x, y = y }
                    moved[name] = { x = x, y = y }
                    any = true
                end
            end
        end
    end
    if any then addEvent("PARTY_POSITIONS", { group_id = g.id, map_id = mapID, members = moved }) end
end

-- No damage or healing meter: this client does not let addons read the combat log, and
-- even registering for it brings up the game's "addon blocked" warning.
function ns.groupTick()
    sampleGroup()
    checkDeaths()
end
function handlers.GROUP_JOINED() syncGroup() end
function handlers.GROUP_LEFT() syncGroup() end

function handlers.PARTY_KILL(attackerGUID, targetGUID)
    attackerGUID, targetGUID = plain(attackerGUID), plain(targetGUID)
    if not charId or type(targetGUID) ~= "string" then return end
    local mate = nil
    if attackerGUID ~= myGuid then                 -- a member, or a pet (credited to its owner)
        mate = groupGuids[attackerGUID] or petOwners[attackerGUID]
        if not mate then return end
        if mate == "" then mate = nil end          -- the player's own pet
    end
    local kind, npcId = targetGUID:match("^(%a+)%-%d+%-%d+%-%d+%-%d+%-(%d+)%-%x+$")
    if kind ~= "Creature" and kind ~= "Vehicle" then return end
    addEvent("MOB_KILLED", here({
        monster_id = tonumber(npcId), monster_name = seenNames[targetGUID],
        monster_guid = targetGUID, player_level = plain(safe(UnitLevel, "player")),
        killed_by = mate,                          -- a group member's kill; nil for the player's own
    }))
end

-- ------------------------------------------------------------ more data
-- Values learned from events (played time, flight points) live in the saved
-- table so they survive a reload and stay in every snapshot.
local function extra()
    if type(db.extra) ~= "table" then db.extra = {} end
    if type(db.extra[charId]) ~= "table" then db.extra[charId] = {} end
    return db.extra[charId]
end

-- ------------------------------------------------------------ bank
-- The game only says what is in the bank while its window is open. So it is read then, on
-- every change while it stays open, and kept in the saved table with the time it was seen:
-- every snapshot carries the bank as it was the last time the player stood at a banker.
local bankOpen, bankSaid

-- The bank itself and the bags bought for it. Their numbers differ between clients, so they
-- are taken by name where the client names them; a number that is no container has no slots.
local function bankContainers()
    local list, seen = {}, {}
    local function add(bag)
        if type(bag) == "number" and (bag < 0 or bag > 4) and not seen[bag] then seen[bag] = true list[#list + 1] = bag end
    end
    local index = type(Enum) == "table" and Enum.BagIndex
    if type(index) == "table" then
        add(index.Bank)
        for i = 1, 8 do add(index["BankBag_" .. i]) add(index["CharacterBankTab_" .. i]) end
    end
    if #list == 0 then
        add(-1)
        for bag = 5, 11 do add(bag) end
    end
    return list
end

local function scanBank()
    if not bankOpen or not db or not charId then return end
    local list = bankContainers()
    local bank = safe(scanContainers, 1, #list, list)
    -- Nothing readable (the window closed meanwhile, or this client keeps the bank elsewhere):
    -- keep what was seen before instead of recording an empty bank.
    if type(bank) ~= "table" or bank.slots == 0 then
        if type(db.debug) == "table" then db.debug.bank = "no slots in: " .. table.concat(list, ",") end
        return
    end
    bank.seen_at = now()
    extra().bank = bank
    if not bankSaid then
        bankSaid = true
        say("已記錄銀行：" .. #bank.items .. " 種物品，空位 " .. bank.free .. "/" .. bank.slots .. "。按「存檔」後會上傳。")
    end
end

function handlers.BANKFRAME_CLOSED() bankOpen = nil end
function handlers.PLAYERBANKSLOTS_CHANGED() scanBank() end
function handlers.PLAYERBANKBAGSLOTS_CHANGED() scanBank() end
function handlers.BAG_UPDATE_DELAYED() dirty.bags = true scanBank() end
-- Item names and prices arrive late: read the open bank again when they do.
local itemInfoArrived = handlers.GET_ITEM_INFO_RECEIVED
function handlers.GET_ITEM_INFO_RECEIVED() itemInfoArrived() scanBank() end

function handlers.UPDATE_FACTION() dirty.reputations = true end

function handlers.UNIT_STATS(unit)
    if plain(unit) == "player" then dirty.stats = true end
end

function handlers.TIME_PLAYED_MSG(total, thisLevel)
    if not charId then return end
    local e = extra()
    e.played_total, e.played_level, e.played_at = plain(total), plain(thisLevel), now()
end

-- Money and experience are tracked as differences from the last known value.
local lastMoney, lastXP

function ns.initCounters()
    lastMoney = plain(safe(GetMoney))
    lastXP = plain(safe(UnitXP, "player"))
end

function handlers.PLAYER_MONEY()
    local money = plain(safe(GetMoney))
    if not charId or type(money) ~= "number" then return end
    if type(lastMoney) == "number" and money ~= lastMoney then
        addEvent("MONEY_CHANGED", here({ delta = money - lastMoney, total = money }))
    end
    lastMoney = money
end

function handlers.PLAYER_XP_UPDATE()
    local xp = plain(safe(UnitXP, "player"))
    if not charId or type(xp) ~= "number" then return end
    -- After a level-up the counter restarts lower; the gain cannot be derived then.
    if type(lastXP) == "number" and xp > lastXP then
        addEvent("XP_GAINED", here({ amount = xp - lastXP, xp = xp, xp_max = plain(safe(UnitXPMax, "player")) }))
    end
    lastXP = xp
end

function handlers.CHAT_MSG_SKILL(text)
    text = plain(text)
    if not charId or type(text) ~= "string" then return end
    addEvent("SKILL_UP", { text = text, value = tonumber(text:match("(%d+)%D*$")) })
    dirty.professions = true
end

local combatStart

function handlers.PLAYER_REGEN_DISABLED() combatStart = now() end

function handlers.PLAYER_REGEN_ENABLED()
    if not charId or not combatStart then return end
    addEvent("COMBAT", here({ duration = now() - combatStart }))
    combatStart = nil
end

local currentInstance, currentInstanceName

local function checkInstance()
    if not charId then return end
    local id, name, kind, difficulty
    if plain(safe(IsInInstance)) then
        local n, t, _, diffName, _, _, _, instanceID = safe(GetInstanceInfo)
        name, kind, difficulty = plain(n), plain(t), plain(diffName)
        id = plain(instanceID) or name
    end
    if id == currentInstance then return end
    if currentInstance then
        addEvent("INSTANCE_LEFT", { instance_id = currentInstance, name = currentInstanceName })
    end
    if id then
        addEvent("INSTANCE_ENTERED", { instance_id = id, name = name, instance_type = kind, difficulty = difficulty })
    end
    currentInstance, currentInstanceName = id, name
end
ns.checkInstance = checkInstance

function handlers.PLAYER_ENTERING_WORLD(_, reloading)
    if reloading == true then reloaded = true end
    checkInstance()
end

function handlers.ENCOUNTER_END(encounterID, name, _, groupSize, success)
    if not charId then return end
    addEvent("ENCOUNTER_END", {
        encounter_id = plain(encounterID), name = plain(name), group_size = plain(groupSize),
        success = plain(success) == 1,
    })
end

function handlers.ACHIEVEMENT_EARNED(achievementID)
    achievementID = plain(achievementID)
    if not charId or type(achievementID) ~= "number" then return end
    local _, name = safe(GetAchievementInfo, achievementID)
    addEvent("ACHIEVEMENT_EARNED", { achievement_id = achievementID, name = plain(name) })
end

-- Opening a merchant, trainer, mailbox... is stored once per NPC per minute.
local lastInteraction = {}

local function interaction(kind)
    if not charId then return end
    local guid, name = plain(safe(UnitGUID, "npc")), plain(safe(UnitName, "npc"))
    local key, t = kind .. tostring(guid), now()
    if lastInteraction[key] and t - lastInteraction[key] < 60 then return end
    lastInteraction[key] = t
    local npcId = type(guid) == "string" and tonumber(guid:match("^%a+%-%d+%-%d+%-%d+%-%d+%-(%d+)%-%x+$"))
    addEvent("NPC_INTERACTION", here({ kind = kind, npc_name = name, npc_id = npcId or nil }))
end

function handlers.MERCHANT_SHOW() interaction("merchant") end
function handlers.TRAINER_SHOW() interaction("trainer") end
function handlers.MAIL_SHOW() interaction("mailbox") end
function handlers.BANKFRAME_OPENED()
    interaction("bank")
    bankOpen, bankSaid = true, nil
    scanBank()
end
function handlers.AUCTION_HOUSE_SHOW() interaction("auction") end

function handlers.TAXIMAP_OPENED()
    interaction("flightmaster")
    if not charId or type(C_TaxiMap) ~= "table" then return end
    local mapID = getPosition()
    local nodes = mapID and safe(C_TaxiMap.GetAllTaxiNodes, mapID)
    if type(nodes) ~= "table" then return end
    local unreachable = type(Enum) == "table" and type(Enum.FlightPathState) == "table"
        and Enum.FlightPathState.Unreachable or 2
    local e = extra()
    if type(e.flight_points) ~= "table" then e.flight_points = {} end
    for _, node in ipairs(nodes) do
        local name, state = plain(node.name), plain(node.state)
        if type(name) == "string" and state ~= nil then e.flight_points[name] = state ~= unreachable end
    end
end

local onTaxi

function ns.onTick()
    if ns.groupTick then ns.groupTick() end
    local flying = plain(safe(UnitOnTaxi, "player")) and true or false
    if onTaxi ~= nil and flying ~= onTaxi and charId then
        addEvent(flying and "FLIGHT_STARTED" or "FLIGHT_ENDED", here({}))
    end
    onTaxi = flying
end

-- Chat is off for every channel until switched on with /adventure chat.
local CHAT_KEYS = { "say", "yell", "party", "raid", "guild", "whisper", "general", "trade", "other_channels" }
local CHAT_EVENTS = {
    CHAT_MSG_SAY = "say", CHAT_MSG_YELL = "yell", CHAT_MSG_PARTY = "party", CHAT_MSG_PARTY_LEADER = "party",
    CHAT_MSG_RAID = "raid", CHAT_MSG_RAID_LEADER = "raid", CHAT_MSG_GUILD = "guild",
    CHAT_MSG_WHISPER = "whisper", CHAT_MSG_WHISPER_INFORM = "whisper", CHAT_MSG_CHANNEL = "channel",
}

-- Counters saved with the data, to diagnose why chat lines were or were not stored.
local function chatCount(field)
    if type(db.debug) ~= "table" then db.debug = {} end
    if type(db.debug.chat) ~= "table" then db.debug.chat = {} end
    db.debug.chat[field] = (db.debug.chat[field] or 0) + 1
end

for event, kind in pairs(CHAT_EVENTS) do
    handlers[event] = function(text, sender, _, _, _, _, zoneChannelID, _, channelBaseName)
        if not charId or not db then return end
        chatCount("received")
        local key = kind
        if kind == "channel" then
            local zone = plain(zoneChannelID)
            key = (zone == 1 and "general") or (zone == 2 and "trade") or "other_channels"
        end
        if not db.settings.chat[key] then
            chatCount("channel_off")
            return
        end
        if isSecret(text) then chatCount("secret_text") end
        text = plain(text)
        if type(text) ~= "string" then
            chatCount("unreadable")
            return
        end
        chatCount("stored")
        addEvent("CHAT_MESSAGE", {
            channel_type = key, channel_name = plain(channelBaseName), sender = plain(sender),
            message = text, outgoing = event == "CHAT_MSG_WHISPER_INFORM" or nil,
            map_id = (getPosition()),
        })
    end
end

function ns.chatCommand(msg)
    if not db then return end
    local key, switch = msg:match("^chat%s+(%S+)%s+(%S+)")
    local value = switch == "on"
    if key and (switch == "on" or switch == "off") then
        for _, k in ipairs(CHAT_KEYS) do
            if key == "all" or key == k then db.settings.chat[k] = value end
        end
    end
    local on = {}
    for _, k in ipairs(CHAT_KEYS) do
        if db.settings.chat[k] then on[#on + 1] = k end
    end
    say("聊天紀錄：" .. (#on > 0 and table.concat(on, ", ") or "全部關閉"))
    local c = type(db.debug) == "table" and type(db.debug.chat) == "table" and db.debug.chat or {}
    say("收到 " .. (c.received or 0) .. " 則，已記錄 " .. (c.stored or 0) .. "，頻道關閉 " .. (c.channel_off or 0)
        .. "，遊戲隱藏 " .. (c.unreadable or 0))
    local refused = {}
    for _, e in ipairs(ns.unregistered or {}) do
        if e:find("^CHAT_MSG_") and e ~= "CHAT_MSG_LOOT" then refused[#refused + 1] = e end
    end
    if #refused > 0 then say("這個遊戲版本不提供這些聊天事件：" .. table.concat(refused, ", ")) end
    say("用法：/adventure chat <頻道|all> on|off　頻道：" .. table.concat(CHAT_KEYS, " "))
end

-- ------------------------------------------------------------ save now
-- The game only writes SavedVariables on logout or UI reload, so "save" = reload.
local function saveNow()
    if type(InCombatLockdown) == "function" and InCombatLockdown() then
        say("戰鬥中無法存檔，請脫離戰鬥後再試")
        return false
    end
    local reload = (type(C_UI) == "table" and C_UI.Reload) or ReloadUI
    if type(reload) ~= "function" then
        say("此客戶端無法由插件重載介面，請手動輸入 /reload")
        return false
    end
    say("存檔中（重新載入介面）… 若畫面沒有重載，請手動輸入 /reload")
    reload()
    return true
end

function ns.createButton()
    if saveButton or not db or type(CreateFrame) ~= "function" or not UIParent then return end
    local b = CreateFrame("Button", "AdventureCollectorSaveButton", UIParent, "UIPanelButtonTemplate")
    b:SetSize(96, 24)
    b:ClearAllPoints()
    local pos = db.settings.buttonPos
    if type(pos) == "table" and type(pos.point) == "string" then
        b:SetPoint(pos.point, UIParent, pos.point, pos.x or 0, pos.y or 0)
    else
        b:SetPoint("TOP", UIParent, "TOP", 0, -40)
    end
    b:SetMovable(true)
    b:SetClampedToScreen(true)
    b:RegisterForDrag("LeftButton")
    b:SetScript("OnDragStart", function(self) self:StartMoving() end)
    b:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local point, _, _, x, y = self:GetPoint()
        if type(point) == "string" then db.settings.buttonPos = { point = point, x = x, y = y } end
    end)
    b:SetScript("OnClick", saveNow)
    saveButton = b
    updateButton()
    if not db.settings.showButton then b:Hide() end
end

-- ------------------------------------------------------------ AI screenshot
-- One key (F12 unless the player already uses it; changeable under Key Bindings > AddOns)
-- shows a panel with what the addon can read right now, takes an ordinary game screenshot
-- with the panel on it, and hides the panel again once the game says the picture is taken.
-- The sync program uploads screenshots within seconds, so an assistant reading the newest
-- one sees the live position, target, mouse-over and quest progress, with no /reload.
-- Values the client does not give are written "unavailable", never guessed.
local AI = { busy = nil, serial = 0, messages = {} }
local NA = "unavailable"
local LABEL = "|cff8fd3ff%s|r  "

local function fmt(v) if v == nil then return NA end return tostring(v) end

-- Position to three decimals (getPosition keeps two for the stored samples).
local function precisePosition()
    if type(C_Map) ~= "table" then return nil end
    local mapID = plain(safe(C_Map.GetBestMapForUnit, "player"))
    if type(mapID) ~= "number" then return nil end
    local pos = safe(C_Map.GetPlayerMapPosition, mapID, "player")
    local x, y
    if pos ~= nil and not isSecret(pos) and type(pos) == "table" and pos.GetXY then x, y = safe(pos.GetXY, pos) end
    x, y = plain(x), plain(y)
    if type(x) ~= "number" or type(y) ~= "number" then return mapID end
    return mapID, math.floor(x * 100000 + 0.5) / 1000, math.floor(y * 100000 + 0.5) / 1000
end

-- The game's facing is radians counter-clockwise from north; shown as a compass bearing.
local COMPASS = { "北", "東北", "東", "東南", "南", "西南", "西", "西北" }
local function facing()
    local r = plain(safe(GetPlayerFacing))
    if type(r) ~= "number" then return nil end
    local bearing = (360 - math.deg(r)) % 360
    return math.floor(bearing + 0.5) % 360, COMPASS[math.floor((bearing + 22.5) / 45) % 8 + 1]
end

local function spellName(id)
    if type(id) ~= "number" then return nil end
    if type(C_Spell) == "table" and type(C_Spell.GetSpellInfo) == "function" then
        local info = safe(C_Spell.GetSpellInfo, id)
        if type(info) == "table" and plain(info.name) then return plain(info.name) end
    end
    return plain((safe(GetSpellInfo, id)))
end

local function formName()
    local i = plain(safe(GetShapeshiftForm))
    if type(i) ~= "number" then return nil end
    if i == 0 then return "一般" end
    local _, second, _, fourth = safe(GetShapeshiftFormInfo, i)
    local spell = plain(fourth)
    local name = type(second) == "string" and plain(second) or spellName(spell)
    return (name or ("形態 " .. i)) .. (type(spell) == "number" and (" (spell " .. spell .. ")") or "")
end

local function bar(cur, max)
    cur, max = plain(cur), plain(max)
    if type(cur) ~= "number" then return NA end
    if type(max) == "number" and max > 0 then
        return string.format("%d/%d (%d%%)", cur, max, math.floor(cur * 100 / max + 0.5))
    end
    return tostring(cur)
end

local POWER = { [0] = "法力", [1] = "怒氣", [2] = "集中值", [3] = "能量" }

local function guidInfo(guid)
    guid = plain(guid)
    if type(guid) ~= "string" then return nil end
    local kind, id = guid:match("^(%a+)%-%d+%-%d+%-%d+%-%d+%-(%d+)%-%x+$")
    if kind then return kind, tonumber(id) end
    return guid:match("^(%a+)%-"), nil
end

local CLASSIFY = { elite = "精英", rareelite = "稀有精英", rare = "稀有", worldboss = "首領", normal = "普通",
                   trivial = "低等", minus = "雜兵" }

-- One line about a unit (target or mouse-over) and its NPC id.
local function unitLine(unit)
    if not plain(safe(UnitExists, unit)) then return nil end
    local kind, id = guidInfo(safe(UnitGUID, unit))
    local level = plain(safe(UnitLevel, unit))
    if level == -1 then level = "??" end
    local out = { plain(safe(UnitName, unit)) or NA }
    if kind == "Player" then out[#out + 1] = "玩家"
    else out[#out + 1] = "NPC ID " .. fmt(id) end
    out[#out + 1] = "等級 " .. fmt(level)
    local class = CLASSIFY[plain(safe(UnitClassification, unit)) or ""]
    if class then out[#out + 1] = class end
    local creature = plain(safe(UnitCreatureType, unit))
    if creature then out[#out + 1] = creature end
    local reaction = plain(safe(UnitReaction, unit, "player"))
    out[#out + 1] = type(reaction) ~= "number" and "關係 " .. NA
        or reaction <= 3 and "敵對" or reaction == 4 and "中立" or "友善"
    out[#out + 1] = "生命 " .. bar(safe(UnitHealth, unit), (safe(UnitHealthMax, unit)))
    if plain(safe(UnitIsDeadOrGhost, unit)) then out[#out + 1] = "已死亡" end
    return table.concat(out, " · "), kind ~= "Player" and id or nil, out[1]
end

-- What the mouse points at, read from the game tooltip before the panel appears.
local function tooltipInfo()
    local tip = GameTooltip
    if type(tip) ~= "table" or not plain(safe(tip.IsShown, tip)) then return nil end
    local out = { lines = {} }
    local _, link = safe(tip.GetItem, tip)
    link = plain(link)
    if type(link) == "string" then
        out.item, out.item_name = tonumber(link:match("item:(%d+)")), link:match("%[(.-)%]")
    end
    local spell = { safe(tip.GetSpell, tip) }
    for i = table.maxn(spell), 1, -1 do
        local v = plain(spell[i])
        if type(v) == "number" then out.spell, out.spell_name = v, plain(spell[1]) break end
    end
    local n = plain(safe(tip.NumLines, tip))
    for i = 1, math.min(type(n) == "number" and n or 0, 10) do
        local left, right = _G["GameTooltipTextLeft" .. i], _G["GameTooltipTextRight" .. i]
        local l = type(left) == "table" and plain(safe(left.GetText, left)) or nil
        local r = type(right) == "table" and plain(safe(right.GetText, right)) or nil
        if l or r then out.lines[#out.lines + 1] = (l or "") .. (r and ("    " .. r) or "") end
    end
    return out
end

local function trackedQuestIds()
    local ids = {}
    if type(C_QuestLog) == "table" and type(C_QuestLog.GetNumQuestWatches) == "function"
        and type(C_QuestLog.GetQuestIDForQuestWatchIndex) == "function" then
        for i = 1, plain(safe(C_QuestLog.GetNumQuestWatches)) or 0 do
            local id = plain(safe(C_QuestLog.GetQuestIDForQuestWatchIndex, i))
            if type(id) == "number" then ids[#ids + 1] = id end
        end
    end
    if #ids == 0 and type(GetNumQuestWatches) == "function" and type(GetQuestIndexForWatch) == "function"
        and type(C_QuestLog) == "table" then
        for i = 1, plain(safe(GetNumQuestWatches)) or 0 do
            local index = plain(safe(GetQuestIndexForWatch, i))
            local info = type(index) == "number" and safe(C_QuestLog.GetInfo, index) or nil
            local id = type(info) == "table" and plain(info.questID) or nil
            if type(id) == "number" then ids[#ids + 1] = id end
        end
    end
    return ids
end

-- The tracked quests, or (none tracked) the unfinished ones in the log, with their progress.
local function questLines(lines, data)
    local log = safe(scanners.quests) or {}
    local byId = {}
    for _, q in ipairs(log) do byId[q.quest_id] = q end
    local ids, note = trackedQuestIds(), "追蹤中"
    if #ids == 0 then
        note = "沒有追蹤任務，列出日誌中未完成的"
        for _, q in ipairs(log) do if not q.complete then ids[#ids + 1] = q.quest_id end end
    end
    lines[#lines + 1] = LABEL:format("任務") .. note .. (#ids == 0 and "：無" or "")
    data.quests = {}
    for n, id in ipairs(ids) do
        if n > 4 then lines[#lines + 1] = "    …另有 " .. (#ids - 4) .. " 個" break end
        data.quests[#data.quests + 1] = id
        local q = byId[id] or {}
        local done = q.complete
        if done == nil and type(C_QuestLog) == "table" then done = plain(safe(C_QuestLog.IsComplete, id)) and true or false end
        lines[#lines + 1] = string.format("    [%d] %s — %s", id, fmt(q.title or questTitle(id)),
            done and "|cff66ff66已完成，可交付|r" or "進行中")
        for i, o in ipairs(q.objectives or {}) do
            if i > 3 then break end
            lines[#lines + 1] = "        " .. (o.finished and "✓ " or "· ") .. o.text
        end
    end
end

local RECENT = {
    ITEM_LOOTED = function(p) return "拾取 " .. fmt(p.item_name) .. " x" .. fmt(p.count) .. " (item " .. fmt(p.item_id) .. ")" end,
    MOB_KILLED = function(p) return "擊殺 " .. fmt(p.monster_name) .. " (NPC " .. fmt(p.monster_id) .. ")" end,
    QUEST_ACCEPTED = function(p) return "接受任務 [" .. fmt(p.quest_id) .. "] " .. fmt(p.quest_name) end,
    QUEST_COMPLETED = function(p) return "交付任務 [" .. fmt(p.quest_id) .. "] " .. fmt(p.quest_name) end,
    QUEST_REMOVED = function(p) return "移除任務 [" .. fmt(p.quest_id) .. "] " .. fmt(p.quest_name) end,
    LEVEL_UP = function(p) return "升到 " .. fmt(p.level) .. " 級" end,
    PLAYER_DIED = function() return "死亡" end,
    COMBAT = function(p) return "戰鬥 " .. fmt(p.duration) .. " 秒" end,
}

local function ago(ts, t)
    local s = math.max(0, t - (tonumber(ts) or t))
    return s < 60 and (s .. " 秒前") or (math.floor(s / 60) .. " 分前")
end

local function recentLines(lines)
    local t, got = now(), {}
    for i = #db.events, math.max(1, #db.events - 300), -1 do
        local e = db.events[i]
        if type(e) == "table" and (t - (tonumber(e.ts) or 0)) > 900 then break end
        local describe = type(e) == "table" and RECENT[e.event_type]
        if describe then
            got[#got + 1] = "    " .. ago(e.ts, t) .. "  " .. describe(type(e.payload) == "table" and e.payload or {})
            if #got >= 6 then break end
        end
    end
    lines[#lines + 1] = LABEL:format("近期") .. (#got == 0 and "15 分鐘內沒有紀錄" or "")
    for _, l in ipairs(got) do lines[#lines + 1] = l end
    if #AI.messages > 0 then
        lines[#lines + 1] = LABEL:format("系統提示")
        for i = #AI.messages, 1, -1 do
            lines[#lines + 1] = "    " .. ago(AI.messages[i].ts, t) .. "  " .. AI.messages[i].text
        end
    end
end

local function remember(text)
    text = plain(text)
    if type(text) ~= "string" or text == "" then return end
    AI.messages[#AI.messages + 1] = { ts = now(), text = text }
    if #AI.messages > 3 then table.remove(AI.messages, 1) end
end
function handlers.UI_INFO_MESSAGE(a, b) remember(type(b) == "string" and b or a) end
function handlers.UI_ERROR_MESSAGE(a, b) remember(type(b) == "string" and b or a) end

-- The panel's text, and the same facts as data for the AI_SCREENSHOT event.
local function aiText(mouse, mouseUnit, mouseId, mouseName)
    local lines, data = {}, { local_ts = time() }
    local t = now()
    local name, level = plain(safe(UnitName, "player")), plain(safe(UnitLevel, "player"))
    lines[1] = "|cffffd100AdventureCollector AI 截圖|r  v" .. VERSION
    lines[#lines + 1] = LABEL:format("時間") .. date("%Y-%m-%d %H:%M:%S", time()) .. "（本機）  "
        .. date("!%H:%M:%S", t) .. " UTC"
    lines[#lines + 1] = LABEL:format("角色") .. fmt(name) .. "  等級 " .. fmt(level)
    local mapID, x, y = precisePosition()
    local info = type(mapID) == "number" and safe(C_Map.GetMapInfo, mapID) or nil
    local sub = plain(safe(GetSubZoneText))
    lines[#lines + 1] = LABEL:format("地圖") .. fmt(type(info) == "table" and plain(info.name) or zoneName())
        .. (type(sub) == "string" and sub ~= "" and (" / " .. sub) or "") .. "  UiMapID " .. fmt(mapID)
    lines[#lines + 1] = LABEL:format("座標") .. (x and string.format("%.3f, %.3f", x, y) or NA)
        .. "  (地圖百分比，左上 0,0)"
    local deg, dir = facing()
    lines[#lines + 1] = LABEL:format("面向") .. (deg and (deg .. "° " .. dir) or NA) .. "  (北 0°，順時針)"
    local combat = plain(safe(UnitAffectingCombat, "player"))
    lines[#lines + 1] = LABEL:format("狀態") .. "形態 " .. fmt(formName()) .. "  ·  "
        .. (combat == nil and "戰鬥 " .. NA or combat and "|cffff5555戰鬥中|r" or "非戰鬥")
    local res = { "生命 " .. bar(safe(UnitHealth, "player"), (safe(UnitHealthMax, "player"))) }
    local kind = plain(safe(UnitPowerType, "player"))
    if type(kind) == "number" then
        res[#res + 1] = (POWER[kind] or "能量") .. " " .. bar(safe(UnitPower, "player", kind), (safe(UnitPowerMax, "player", kind)))
    end
    if kind ~= 0 and select(2, safe(UnitClass, "player")) == "DRUID" then
        res[#res + 1] = "法力 " .. bar(safe(UnitPower, "player", 0), (safe(UnitPowerMax, "player", 0)))
    end
    lines[#lines + 1] = LABEL:format("資源") .. table.concat(res, "  ·  ")
    local target, targetId, targetName = unitLine("target")
    lines[#lines + 1] = LABEL:format("目標") .. (target or "無")
    if target then
        local combo = plain(safe(GetComboPoints, "player", "target"))
        if type(combo) == "number" and combo > 0 then lines[#lines + 1] = "    連擊點數 " .. combo end
    end
    if mouseUnit then
        lines[#lines + 1] = LABEL:format("滑鼠") .. mouseUnit
    elseif mouse and (mouse.item or mouse.spell) then
        lines[#lines + 1] = LABEL:format("滑鼠") .. (mouse.item and ("物品 " .. fmt(mouse.item_name) .. " (item " .. mouse.item .. ")")
            or ("技能 " .. fmt(mouse.spell_name) .. " (spell " .. mouse.spell .. ")"))
    else
        lines[#lines + 1] = LABEL:format("滑鼠") .. (mouse and "提示框內容如下" or "沒有指向")
    end
    if mouse and #mouse.lines > 0 then
        for i, l in ipairs(mouse.lines) do
            if i > 8 then break end
            lines[#lines + 1] = "    |cffbbbbbb" .. l .. "|r"
        end
    end
    questLines(lines, data)
    recentLines(lines)
    data.map_id, data.x, data.y, data.facing = mapID, x, y, deg
    data.in_combat, data.target_id, data.target_name = combat, targetId, targetName
    data.mouse_npc_id, data.mouse_name = mouseId, mouseName
    data.mouse_item_id, data.mouse_spell_id = mouse and mouse.item, mouse and mouse.spell
    return table.concat(lines, "\n"), data
end

local function aiPanel()
    if AI.frame then return AI.frame end
    if type(CreateFrame) ~= "function" or not UIParent then return nil end
    local f = CreateFrame("Frame", "AdventureCollectorAIPanel", UIParent)
    safe(f.SetIgnoreParentScale, f, true)       -- the same size in the picture whatever the UI scale
    f:SetFrameStrata("DIALOG")
    f:SetClampedToScreen(true)
    f:SetWidth(600)
    local bg = f:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(f)
    bg:SetColorTexture(0.02, 0.03, 0.06, 0.9)
    local text = f:CreateFontString(nil, "OVERLAY")
    text:SetFont(STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", db.settings.aiFontSize, "")
    text:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -12)
    text:SetWidth(572)
    text:SetJustifyH("LEFT")
    text:SetJustifyV("TOP")
    safe(text.SetSpacing, text, 3)
    f.text = text
    f:SetMovable(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local point, _, _, px, py = self:GetPoint()
        if type(point) == "string" then db.settings.aiPanelPos = { point = point, x = px, y = py } end
    end)
    f:Hide()
    AI.frame = f
    return f
end

local function aiPlace(f)
    f:ClearAllPoints()
    local pos = db.settings.aiPanelPos
    if type(pos) == "table" and type(pos.point) == "string" then
        f:SetPoint(pos.point, UIParent, pos.point, pos.x or 0, pos.y or 0)
    else
        f:SetPoint("LEFT", UIParent, "LEFT", 16, 40)   -- left edge, clear of the unit frames and the chat
    end
end

local function aiHide()
    AI.busy = nil
    if AI.frame then AI.frame:EnableMouse(false) AI.frame:Hide() end
end

-- test: shown for a while without a screenshot, and draggable to where it should sit
local function aiShot(test)
    if not db or AI.busy then return end
    if not test and type(Screenshot) ~= "function" then say("此客戶端無法截圖") return end
    local mouse = tooltipInfo()                     -- before anything on screen changes
    local mouseUnit, mouseId, mouseName = unitLine("mouseover")
    local text, data = aiText(mouse, mouseUnit, mouseId, mouseName)
    AI.lastText = text
    AI.serial = AI.serial + 1
    local serial = AI.serial
    local f = aiPanel()
    if f then
        aiPlace(f)
        f.text:SetText(text)
        local h = plain(safe(f.text.GetStringHeight, f.text))
        f:SetHeight((type(h) == "number" and h or 480) + 24)
        f:Show()
    end
    local after = type(C_Timer) == "table" and C_Timer.After or nil
    if test then
        AI.busy = "test"
        if f then f:EnableMouse(true) end
        say("AI 截圖面板測試：顯示 10 秒，不會截圖，可以用滑鼠拖曳調整位置（/adventure aishot reset 回到預設）")
        if after then after(10, function() if AI.serial == serial then aiHide() end end) else aiHide() end
        return
    end
    AI.busy = "shot"
    addEvent("AI_SCREENSHOT", data)
    if not after then Screenshot() return end
    -- give the panel a frame or two to be drawn before the picture is taken
    after(db.settings.aiShotDelay, function() if AI.serial == serial and AI.busy == "shot" then Screenshot() end end)
    after(db.settings.aiShotDelay + 4, function()
        if AI.serial == serial and AI.busy == "shot" then
            aiHide()
            say("截圖沒有回應，面板已隱藏")
        end
    end)
end

function AdventureCollector_AIShot() pcall(aiShot, false) end
BINDING_HEADER_ADVENTURECOLLECTOR = "AdventureCollector"
BINDING_NAME_ADVENTURECOLLECTOR_AISHOT = "AI 增強截圖"

-- F12 once, at the first login with this version, if neither the key nor the action is taken.
-- Asked only once, so a player who later unbinds it is never overruled.
function ns.bindAIShot()
    if not db or db.settings.aiKeyOffered or type(SetBinding) ~= "function" or type(GetBindingKey) ~= "function" then return end
    if plain(safe(InCombatLockdown)) then return end
    db.settings.aiKeyOffered = true
    if plain(safe(GetBindingKey, "ADVENTURECOLLECTOR_AISHOT")) then return end
    local taken = plain(safe(GetBindingAction, "F12"))
    if type(taken) == "string" and taken ~= "" then
        say("AI 增強截圖：F12 已經用於「" .. taken .. "」，請到 選單 > 按鍵設定 > 插件 自訂按鍵，或輸入 /adventure aishot")
        return
    end
    if safe(SetBinding, "F12", "ADVENTURECOLLECTOR_AISHOT") then
        safe(SaveBindings, plain(safe(GetCurrentBindingSet)) or 1)
        say("按 F12 拍 AI 增強截圖（可在 選單 > 按鍵設定 > 插件 更改）")
    end
end

ns.aiShot = aiShot
ns.aiState = AI

-- ------------------------------------------------------------ map screenshot
-- Takes a screenshot with the world map open and records where the map sits on
-- screen (as fractions), so the agent can crop it into a dashboard background.
local pendingShot

-- Returns the map canvas edges and the screen width, all in root UI units
-- (origin at the bottom-left of the screen). The agent converts them to pixels
-- using the screenshot's real size: UIParent can be shorter than the screen
-- (e.g. below a MacBook notch), so screen fractions cannot be computed here.
local function mapRect()
    local container = WorldMapFrame.ScrollContainer
    local canvas = type(container) == "table" and (container.Child or container) or WorldMapFrame
    local scale = canvas:GetEffectiveScale()
    local screenW = UIParent:GetWidth() * UIParent:GetEffectiveScale()
    if type(WorldFrame) == "table" then
        local ok, w = pcall(function() return WorldFrame:GetWidth() * WorldFrame:GetEffectiveScale() end)
        if ok and type(w) == "number" and w > screenW then screenW = w end
    end
    local left, right = canvas:GetLeft() * scale, canvas:GetRight() * scale
    local top, bottom = canvas:GetTop() * scale, canvas:GetBottom() * scale
    -- A zoomed-in map extends past the screen edges.
    if left < -2 or right > screenW + 2 or bottom < -2 or right - left < screenW * 0.1 then return nil end
    return left, top, right, bottom, screenW
end

local function mapShot()
    if not charId or type(Screenshot) ~= "function" or type(WorldMapFrame) ~= "table"
        or type(C_Timer) ~= "table" then
        say("此客戶端無法使用地圖截圖")
        return
    end
    if not WorldMapFrame:IsShown() then
        if type(ToggleWorldMap) == "function" then ToggleWorldMap() else WorldMapFrame:Show() end
    end
    say("1 秒後截圖：請把地圖縮到最小（看得到整張），並把滑鼠移出地圖")
    C_Timer.After(1, function()
        local ok, left, top, right, bottom, screenW = pcall(mapRect)
        if not ok or not left then
            say("無法取得地圖範圍：請確認世界地圖已開啟且沒有放大")
            return
        end
        local mapID = plain(safe(WorldMapFrame.GetMapID, WorldMapFrame))
        if type(mapID) ~= "number" then mapID = getPosition() end
        if type(mapID) ~= "number" then
            say("無法判斷目前顯示的是哪張地圖")
            return
        end
        local info = safe(C_Map.GetMapInfo, mapID)
        pendingShot = {
            map_id = mapID, map_name = type(info) == "table" and plain(info.name) or nil,
            left_u = left, top_u = top, right_u = right, bottom_u = bottom, screen_w_u = screenW,
            local_ts = time(), -- the PC clock, to match the screenshot file's timestamp
        }
        Screenshot()
    end)
end

function handlers.SCREENSHOT_SUCCEEDED()
    if AI.busy == "shot" then aiHide() end
    if not pendingShot then return end
    local shot = pendingShot
    pendingShot = nil
    if addEvent("MAP_SCREENSHOT", shot) then
        if type(db.mapShots) ~= "table" then db.mapShots = {} end
        db.mapShots[shot.map_id] = true
        say("地圖截圖完成（" .. tostring(shot.map_name or shot.map_id) .. "），按「存檔」後會上傳")
    end
end

function handlers.SCREENSHOT_FAILED()
    if AI.busy == "shot" then
        aiHide()
        say("AI 截圖失敗")
    end
    if pendingShot then
        pendingShot = nil
        say("截圖失敗")
    end
end

-- ------------------------------------------------------------ event wiring
ns.unregistered = {}
local frame = type(CreateFrame) == "function" and CreateFrame("Frame") or nil

-- The game reports when it stops an addon from calling something it protects. Remember
-- which call it was (also when it happens while the file is still loading, before the
-- saved table exists), and switch off the optional feature that made it.
ns.blocked = {}
local function onBlocked(kind, addon, func)
    addon, func = plain(addon), plain(func)
    if addon ~= ADDON_NAME then return end
    local key = kind .. ":" .. tostring(func)
    if not db then                                 -- still loading: kept here, written out at login
        ns.blocked[key] = (ns.blocked[key] or 0) + 1
        return
    end
    if type(db.debug) ~= "table" then db.debug = {} end
    if type(db.debug.blocked) ~= "table" then db.debug.blocked = {} end
    db.debug.blocked[key] = (db.debug.blocked[key] or 0) + 1
    if type(func) ~= "string" or func:find("MapPosition") then
        if db.settings.matePositions ~= false then
            say("遊戲不允許插件讀取隊友位置，已自動停用這項功能。其他紀錄不受影響。")
        end
        db.settings.matePositions = false
    end
    if type(func) == "string" and func:find("LoggingCombat") then
        db.settings.combatLogCheck = false         -- this client refuses the question: never ask again
    end
end
function handlers.ADDON_ACTION_BLOCKED(addon, func) onBlocked("blocked", addon, func) end
function handlers.ADDON_ACTION_FORBIDDEN(addon, func) onBlocked("forbidden", addon, func) end
if frame then
    -- These two first, so a refusal while registering the rest is still seen.
    pcall(frame.RegisterEvent, frame, "ADDON_ACTION_FORBIDDEN")
    pcall(frame.RegisterEvent, frame, "ADDON_ACTION_BLOCKED")
    for event in pairs(handlers) do
        -- Registering an unknown event throws; never let that abort the file. Remember which
        -- ones the client refused, so missing data can be told apart from "nothing happened".
        if not pcall(frame.RegisterEvent, frame, event) then
            ns.unregistered[#ns.unregistered + 1] = event
        end
    end
    table.sort(ns.unregistered)
    frame:SetScript("OnEvent", function(_, event, ...)
        local h = handlers[event]
        if h then
            local ok, err = pcall(h, ...)
            if not ok then say("error in " .. event .. ": " .. tostring(err)) end
        end
    end)
end

-- ------------------------------------------------------------ name probe
-- What this client's name calls really answer, every value as it comes back (type and text):
-- the full name (main and second name) is to be taken from what is seen here, not assumed.
-- Printed, and kept in the saved file (debug.names) to be read back.
local function shown(v)
    if isSecret(v) then return "<secret>" end
    if type(v) == "string" then return '"' .. v .. '"' end
    return type(v) .. ":" .. tostring(v)
end

local function counted(...) return select("#", ...), { ... } end

local function every(f, ...)
    if type(f) ~= "function" then return "（沒有這個函式）" end
    local n, packed = counted(pcall(f, ...))
    if not packed[1] then return "（錯誤：" .. tostring(packed[2]) .. "）" end
    local out = {}
    for i = 2, n do out[#out + 1] = shown(packed[i]) end
    return #out > 0 and table.concat(out, ", ") or "（沒有回傳值）"
end

local function nameProbe()
    local lines = {
        "GetRealmName() = " .. every(GetRealmName),
        "GetNormalizedRealmName() = " .. every(GetNormalizedRealmName),
    }
    for _, unit in ipairs({ "player", "target", "party1", "party2", "party3", "party4" }) do
        local ok, exists = pcall(UnitExists, unit)
        if unit == "player" or (ok and exists and not isSecret(exists)) then
            local guid = plain(safe(UnitGUID, unit))
            lines[#lines + 1] = "[" .. unit .. "] UnitGUID = " .. shown(guid)
            lines[#lines + 1] = "  UnitName = " .. every(UnitName, unit)
            lines[#lines + 1] = "  UnitFullName = " .. every(UnitFullName, unit)
            lines[#lines + 1] = "  GetUnitName(true) = " .. every(GetUnitName, unit, true)
            lines[#lines + 1] = "  GetUnitName(false) = " .. every(GetUnitName, unit, false)
            if type(guid) == "string" then
                lines[#lines + 1] = "  GetPlayerInfoByGUID = " .. every(GetPlayerInfoByGUID, guid)
            end
        end
    end
    if db then
        if type(db.debug) ~= "table" then db.debug = {} end
        db.debug.names = { at = now(), version = VERSION, lines = lines }
    end
    say("名稱測試（也存進存檔 debug.names；選一個目標、組隊時再跑一次會更完整）：")
    for _, line in ipairs(lines) do say(line) end
end
ns.nameProbe = nameProbe

-- ------------------------------------------------------------ slash
local function status()
    if not db then say("not initialised yet") return end
    local mapID, x, y = getPosition()
    say("v" .. VERSION .. (db.settings.enabled and " (collecting)" or " (PAUSED)"))
    say("character: " .. tostring(plain(safe(UnitName, "player")))
        .. "  level " .. tostring(plain(safe(UnitLevel, "player"))))
    say("zone: " .. tostring(zoneName()) .. "  map " .. tostring(mapID)
        .. "  pos " .. tostring(x) .. ", " .. tostring(y))
    say("events this session: " .. sessionEvents .. "  stored: " .. #db.events
        .. "  next seq: " .. db.nextSequence)
    say("character id: " .. tostring(charId))
    local logging = combatLogOn()
    if logging ~= nil then say("戰鬥紀錄 (/combatlog): " .. (logging and "已開啟" or "可能沒有開（遊戲的回報不一定準）")) end
end

SLASH_ADVENTURECOLLECTOR1 = "/adventure"
SLASH_ADVENTURECOLLECTOR2 = "/advc"
SlashCmdList = SlashCmdList or {}
SlashCmdList.ADVENTURECOLLECTOR = function(msg)
    msg = string.lower(tostring(msg or "")):match("^%s*(.-)%s*$")
    if msg:match("^chat") then
        ns.chatCommand(msg)
    elseif msg == "names" then
        nameProbe()
    elseif msg == "on" or msg == "off" then
        if db then
            db.settings.enabled = (msg == "on")
            say(db.settings.enabled and "collecting enabled" or "collecting paused")
        end
    elseif msg == "save" then
        saveNow()
    elseif msg == "mapshot" then
        mapShot()
    elseif msg:match("^aishot") then
        local what = msg:match("^aishot%s+(%S+)")
        if what == "reset" then
            if db then db.settings.aiPanelPos = nil end
            say("AI 截圖面板回到預設位置")
        else
            aiShot(what == "test")
        end
    elseif msg:match("^combatlog") then
        if not db then return end
        local what = msg:match("^combatlog%s+(%S+)")
        if what == "on" then          -- only ever on, now and at every login: safe to type whatever the state
            db.settings.combatLogAuto, db.settings.combatLogAutoChosen = true, true
            checkCombatLog(true)
        elseif what == "auto" then
            db.settings.combatLogAuto = not db.settings.combatLogAuto
            db.settings.combatLogAutoChosen = true
            say(db.settings.combatLogAuto and "之後每次登入會自動開啟戰鬥紀錄" or "不再自動開啟戰鬥紀錄（要恢復：/adventure combatlog on）")
            if db.settings.combatLogAuto then checkCombatLog() end
        elseif what == "quiet" then
            db.settings.combatLogRemind = not db.settings.combatLogRemind
            say(db.settings.combatLogRemind and "進遊戲時會提醒確認戰鬥紀錄" or "進遊戲時不再提醒確認戰鬥紀錄")
        else
            local on = combatLogOn()
            if on == nil then say("這個遊戲版本不讓插件知道戰鬥紀錄有沒有開，請自己輸入 /combatlog 確認")
            elseif on then say("戰鬥紀錄已開啟")
            else checkCombatLog(true) end
        end
    elseif msg == "button" then
        if db then
            db.settings.showButton = not db.settings.showButton
            if saveButton then
                if db.settings.showButton then saveButton:Show() else saveButton:Hide() end
            end
            say(db.settings.showButton and "存檔按鈕已顯示" or "存檔按鈕已隱藏")
        end
    elseif msg == "help" then
        say("/adventure [status] - 狀態;  /adventure save - 立即存檔;  /adventure mapshot - 截目前地圖當底圖;  /adventure aishot [test|reset] - AI 增強截圖 (預設 F12);  /adventure chat - 聊天紀錄設定;  "
            .. "/adventure combatlog - 確認戰鬥紀錄 (on: 開啟並每次登入自動開, auto: 切換自動開 (預設開), quiet: 不提醒);  "
            .. "/adventure button - 顯示/隱藏存檔按鈕;  "
            .. "/adventure on|off - 啟用/暫停")
    else
        status()
    end
end

-- Exposed for automated tests (not used in game).
ns.handlers = handlers
ns.samplePosition = samplePosition
ns.addEvent = addEvent
ns.getPosition = getPosition
ns.saveNow = saveNow
