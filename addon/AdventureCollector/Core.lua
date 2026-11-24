-- AdventureCollector (Phase 1): character snapshot + location events.
-- Data is only written to SavedVariables; the AdventureSync agent uploads it.
local ADDON_NAME, ns = ...
ADDON_NAME = ADDON_NAME or "AdventureCollector"
ns = ns or {}

local VERSION = "0.1.0"
local DEFAULTS = {
    enabled = true,
    locationEnabled = true,
    locationInterval = 15, -- seconds between position checks
    minMove = 0.5,         -- map percent points before a new sample is stored
    heartbeat = 60,        -- seconds; store a sample anyway after this long
    maxEvents = 5000,      -- oldest events are dropped beyond this
    showButton = true,     -- on-screen "save now" button
    requestPlayed = true,  -- ask for /played once per login (prints two chat lines)
}

local db, charId, myGuid
-- Current group, rebuilt on every roster change (not saved): member guid -> name, member name
-- -> unit token, and pet guid -> owner name ("" for the player's own pet).
local groupGuids, groupUnits, petOwners = {}, {}, {}
local sessionEvents = 0
local last = { map = nil, x = nil, y = nil, t = 0 }
local ticker
local saveButton

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

function scanners.bags()
    if type(C_Container) ~= "table" then return nil end
    local byId, items, total, free = {}, {}, 0, 0
    for bag = 0, 4 do
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
        end)
    else
        samplePosition(true)
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

function handlers.BAG_UPDATE_DELAYED() dirty.bags = true end
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

function handlers.PLAYER_ENTERING_WORLD() checkInstance() end

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
function handlers.BANKFRAME_OPENED() interaction("bank") end
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
end

SLASH_ADVENTURECOLLECTOR1 = "/adventure"
SLASH_ADVENTURECOLLECTOR2 = "/advc"
SlashCmdList = SlashCmdList or {}
SlashCmdList.ADVENTURECOLLECTOR = function(msg)
    msg = string.lower(tostring(msg or "")):match("^%s*(.-)%s*$")
    if msg:match("^chat") then
        ns.chatCommand(msg)
    elseif msg == "on" or msg == "off" then
        if db then
            db.settings.enabled = (msg == "on")
            say(db.settings.enabled and "collecting enabled" or "collecting paused")
        end
    elseif msg == "save" then
        saveNow()
    elseif msg == "mapshot" then
        mapShot()
    elseif msg == "button" then
        if db then
            db.settings.showButton = not db.settings.showButton
            if saveButton then
                if db.settings.showButton then saveButton:Show() else saveButton:Hide() end
            end
            say(db.settings.showButton and "存檔按鈕已顯示" or "存檔按鈕已隱藏")
        end
    elseif msg == "help" then
        say("/adventure [status] - 狀態;  /adventure save - 立即存檔;  /adventure mapshot - 截目前地圖當底圖;  /adventure chat - 聊天紀錄設定;  "
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
