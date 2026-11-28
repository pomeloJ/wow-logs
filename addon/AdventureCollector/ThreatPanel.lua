-- Live display: reads the current target directly, independently of saved combat samples.
local _, ns = ...
local panel, settings, options
local editing = false
local MAX_ROWS = 8
local WHITE = "Interface\\Buttons\\WHITE8X8"
local GREEN, AMBER, RED, GREY = {0.35, 0.85, 0.5}, {1, 0.72, 0.2}, {1, 0.3, 0.25}, {0.65, 0.65, 0.65}

local function secret(v)
    return type(issecretvalue) == "function" and issecretvalue(v)
end

local function read(fn, ...)
    if type(fn) ~= "function" then return nil end
    local out = {pcall(fn, ...)}
    if not out[1] then return nil end
    for i = 2, table.maxn(out) do if secret(out[i]) then return nil end end
    return unpack(out, 2, table.maxn(out))
end

local function number(v)
    return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge
end

-- A fresh model each refresh prevents stale values after target changes or restricted reads.
local function collect()
    local model = {rows = {}, target = read(UnitName, "target") or "未選取目標"}
    if not read(UnitExists, "target") then model.message = "請選取敵對目標"; return model end
    if read(UnitCanAttack, "player", "target") == false then model.message = "請選取敵對目標"; return model end
    if type(UnitDetailedThreatSituation) ~= "function" then model.message = "此版本無仇恨 API"; return model end
    local units = {"player", "pet"}
    if read(IsInRaid) then
        local count = read(GetNumGroupMembers)
        for i = 1, number(count) and math.min(40, count) or 0 do
            units[#units + 1], units[#units + 2] = "raid" .. i, "raidpet" .. i
        end
    elseif read(IsInGroup) then
        for i = 1, 4 do units[#units + 1], units[#units + 2] = "party" .. i, "partypet" .. i end
    end
    local seen = {}
    for _, unit in ipairs(units) do
        if read(UnitExists, unit) then
            local guid = read(UnitGUID, unit)
            local key = type(guid) == "string" and guid or unit
            if not seen[key] then
                seen[key] = true
                local tank, status, pct, _, value = read(UnitDetailedThreatSituation, unit, "target")
                if number(value) and value >= 0 then
                    local pet = unit:find("pet") ~= nil
                    local name = unit == "player" and "你" or read(UnitName, unit) or (pet and "寵物" or unit)
                    local holding = value > 0 and (tank == true or (number(status) and status >= 2))
                    -- Zero-threat/holder percentages can be bogus in this client. Unknown stays unknown.
                    if value <= 0 then pct = nil
                    elseif holding then pct = 100
                    elseif not number(pct) or pct < 0 then pct = nil end
                    local row = {name = name .. (pet and "（寵物）" or ""), value = value,
                                 percent = pct, holding = holding, pet = pet, mine = unit == "player", key = key}
                    model.rows[#model.rows + 1] = row
                    if row.mine then model.me = row end
                    if holding then
                        model.holder = model.holder and (model.holder .. "、" .. row.name) or row.name
                    end
                end
            end
        end
    end
    table.sort(model.rows, function(a, b)
        if a.value == b.value then return a.key < b.key end
        return a.value > b.value
    end)
    for i, row in ipairs(model.rows) do row.rank = i end
    local me = model.me
    if not me or not me.percent then model.message, model.color = "無自身仇恨資料", GREY
    elseif me.holding then model.message, model.color = "正在坦怪", RED
    elseif me.percent >= (settings and settings.warn or 85) then model.message, model.color = "接近 OT・注意仇恨", AMBER
    else model.message, model.color = "仇恨低於警示門檻", GREEN end
    if #model.rows == 0 then model.message = "無仇恨資料" end
    return model
end

local function text(parent, size, x, y, width)
    local font = parent:CreateFontString(nil, "OVERLAY")
    font:SetFont(STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", size, "OUTLINE")
    font:SetPoint("TOPLEFT", parent, "TOPLEFT", x, y)
    font:SetWidth(width)
    font:SetJustifyH("LEFT")
    font:SetWordWrap(false)
    return font
end

local function bar(parent, x, y, width, height)
    local b = CreateFrame("StatusBar", nil, parent)
    b:SetPoint("TOPLEFT", parent, "TOPLEFT", x, y)
    b:SetSize(width, height)
    b:SetStatusBarTexture(WHITE)
    b:SetMinMaxValues(0, 100)
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(); bg:SetColorTexture(0.13, 0.13, 0.13, 0.95)
    return b
end

local function position()
    panel:ClearAllPoints()
    local at = settings.at
    if type(at) == "table" and type(at.point) == "string" and type(at.relative) == "string" and number(at.x) and number(at.y) then
        panel:SetPoint(at.point, UIParent, at.relative, at.x, at.y)
    else panel:SetPoint("RIGHT", UIParent, "RIGHT", -40, 0) end
    panel:SetScale(settings.scale)
    panel:SetAlpha(settings.alpha)
    panel.background:SetColorTexture(0.035, 0.03, 0.02, settings.backgroundAlpha)
end

local function refresh()
    if not panel then return end
    if not editing and (not settings.enabled or (settings.combatOnly and not read(UnitAffectingCombat, "player"))) then
        panel:Hide(); return
    end
    panel:Show()
    local model = collect()
    panel.target:SetText("目標：" .. model.target)
    panel.holder:SetText("目前坦怪：" .. (model.holder or "未知"))
    local maxValue = math.max(1, model.rows[1] and model.rows[1].value or 0)
    local visible = math.min(MAX_ROWS, #model.rows)
    for i, b in ipairs(panel.rows) do
        local row = model.rows[i]
        -- In raids keep the player's row visible, with its true rank.
        if i == MAX_ROWS and model.me and model.me.rank > MAX_ROWS then row = model.me end
        if row then
            b:Show(); b:SetValue(row.value / maxValue * 100)
            local c = row.mine and AMBER or row.pet and {0.3, 0.6, 1} or GREEN
            b:SetStatusBarColor(unpack(c))
            b.label:SetText(row.rank .. "  " .. row.name)
            b.amount:SetText(string.format("%.0f", row.value))
        else b:Hide() end
    end
    local y = -82 - visible * 25
    panel.riskLabel:ClearAllPoints(); panel.riskLabel:SetPoint("TOPLEFT", panel, "TOPLEFT", 12, y)
    panel.riskLabel:SetText("你的拉怪門檻比例：" .. (model.me and model.me.percent and string.format("%.0f%%", model.me.percent) or "—"))
    panel.risk:ClearAllPoints(); panel.risk:SetPoint("TOPLEFT", panel, "TOPLEFT", 12, y - 24)
    panel.risk:SetValue(math.min(100, model.me and model.me.percent or 0))
    local color = model.color or GREY
    panel.risk:SetStatusBarColor(unpack(color))
    panel.state:ClearAllPoints(); panel.state:SetPoint("TOPLEFT", panel, "TOPLEFT", 12, y - 44)
    panel.state:SetText(model.message); panel.state:SetTextColor(unpack(color))
    panel:SetHeight(170 + visible * 25)
end

local function openOptions()
    if not options then
        -- Independent parent keeps controls readable even when the panel is faint or tiny.
        options = CreateFrame("Frame", "AdventureCollectorThreatOptions", UIParent)
        options:SetSize(310, 300); options:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
        options:SetFrameStrata("DIALOG"); options:EnableMouse(true)
        options:SetClampedToScreen(true); options:SetMovable(true); options:RegisterForDrag("LeftButton")
        options:SetScript("OnDragStart", function(self) self:StartMoving() end)
        options:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
        local bg = options:CreateTexture(nil, "BACKGROUND")
        bg:SetAllPoints(); bg:SetColorTexture(0.06, 0.06, 0.06, 1)
        text(options, 17, 16, -16, 280):SetText("仇恨面板設定")
        text(options, 11, 16, -42, 280):SetText("即時預覽・自動保存・關閉後恢復顯示模式")
        options.sliders = {}
        local function slider(key, title, y, low, high)
            local label = text(options, 13, 16, y, 280)
            local s = CreateFrame("Slider", nil, options)
            s:SetPoint("TOPLEFT", options, "TOPLEFT", 20, y - 23)
            s:SetSize(270, 18); s:SetOrientation("HORIZONTAL")
            s:SetMinMaxValues(low, high); s:SetValueStep(1); s:EnableMouse(true)
            local track = s:CreateTexture(nil, "BACKGROUND")
            track:SetPoint("LEFT", s, "LEFT", 0, 0); track:SetPoint("RIGHT", s, "RIGHT", 0, 0)
            track:SetHeight(5); track:SetColorTexture(0.3, 0.3, 0.3, 1)
            local thumb = s:CreateTexture(nil, "OVERLAY")
            thumb:SetSize(12, 20); thumb:SetColorTexture(1, 0.72, 0.2, 1)
            s:SetThumbTexture(thumb)
            s:SetScript("OnValueChanged", function(_, value)
                settings[key] = math.floor(value + 0.5) / 100
                label:SetText(title .. "：" .. math.floor(value + 0.5) .. "%")
                position()
            end)
            options.sliders[key] = s
        end
        slider("scale", "大小", -70, 50, 200)
        slider("alpha", "整體不透明度（越低越透明）", -130, 20, 100)
        slider("backgroundAlpha", "背景不透明度", -190, 0, 100)
        local close = CreateFrame("Button", nil, options, "UIPanelButtonTemplate")
        close:SetSize(90, 24); close:SetPoint("BOTTOMRIGHT", options, "BOTTOMRIGHT", -16, 12)
        close:SetText("完成"); close:SetScript("OnClick", function() options:Hide() end)
        local reset = CreateFrame("Button", nil, options, "UIPanelButtonTemplate")
        reset:SetSize(120, 24); reset:SetPoint("BOTTOMLEFT", options, "BOTTOMLEFT", 16, 12)
        reset:SetText("重設外觀")
        reset:SetScript("OnClick", function()
            options.sliders.scale:SetValue(100)
            options.sliders.alpha:SetValue(100)
            options.sliders.backgroundAlpha:SetValue(90)
        end)
        options:SetScript("OnHide", function() editing = false; refresh() end)
        ns.threatOptions = options
    end
    for key, s in pairs(options.sliders) do s:SetValue(settings[key] * 100) end
    editing = true; options:Show(); refresh()
end

function ns.initThreatPanel()
    if panel or not AdventureCollectorDB then return end
    local saved = AdventureCollectorDB.settings
    if type(saved.threatPanel) ~= "table" then saved.threatPanel = {} end
    settings = saved.threatPanel
    if settings.enabled == nil then settings.enabled = true end
    if settings.combatOnly == nil then settings.combatOnly = true end
    if settings.locked == nil then settings.locked = false end
    settings.scale = number(settings.scale) and math.max(0.5, math.min(2, settings.scale)) or 1
    settings.alpha = number(settings.alpha) and math.max(0.2, math.min(1, settings.alpha)) or 1
    settings.backgroundAlpha = number(settings.backgroundAlpha) and math.max(0, math.min(1, settings.backgroundAlpha)) or 0.9
    settings.warn = number(settings.warn) and math.max(1, math.min(100, settings.warn)) or 85
    panel = CreateFrame("Frame", "AdventureCollectorThreatPanel", UIParent)
    panel:SetSize(320, 220); panel:SetFrameStrata("MEDIUM")
    panel:SetClampedToScreen(true); panel:SetMovable(true); panel:EnableMouse(true)
    panel:RegisterForDrag("LeftButton")
    local bg = panel:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(); bg:SetColorTexture(0.035, 0.03, 0.02, 0.9)
    panel.background = bg
    text(panel, 17, 12, -12, 210):SetText("仇恨監控")
    local config = CreateFrame("Button", nil, panel, "UIPanelButtonTemplate")
    config:SetSize(60, 22); config:SetPoint("TOPRIGHT", panel, "TOPRIGHT", -10, -8)
    config:SetText("設定"); config:SetScript("OnClick", openOptions)
    panel.target = text(panel, 13, 12, -38, 296)
    panel.holder = text(panel, 12, 12, -58, 296)
    panel.rows = {}
    for i = 1, MAX_ROWS do
        local b = bar(panel, 12, -82 - (i - 1) * 25, 296, 21)
        b.label = text(b, 12, 4, -3, 215)
        b.amount = text(b, 12, 220, -3, 72); b.amount:SetJustifyH("RIGHT")
        panel.rows[i] = b
    end
    panel.riskLabel = text(panel, 13, 12, 0, 296)
    panel.risk = bar(panel, 12, 0, 296, 12)
    panel.state = text(panel, 14, 12, 0, 296)
    local foot = text(panel, 10, 12, 0, 296)
    foot:ClearAllPoints(); foot:SetPoint("BOTTOMLEFT", panel, "BOTTOMLEFT", 12, 10)
    foot:SetText("目前目標・100% 為拉怪門檻・/athreat 設定")
    panel:SetScript("OnDragStart", function(self) if not settings.locked then self:StartMoving() end end)
    panel:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local point, _, relative, x, y = self:GetPoint(1)
        settings.at = {point = point, relative = relative, x = x, y = y}
        position()
    end)
    position()
    -- Separate always-visible driver: a hidden panel's OnUpdate would not run.
    local driver, elapsed = CreateFrame("Frame", nil, UIParent), 0
    driver:SetScript("OnUpdate", function(_, dt)
        elapsed = elapsed + dt
        if elapsed >= 0.2 then elapsed = 0; refresh() end
    end)
    ns.threatPanel = panel
    refresh()
end

-- One change of a setting; false when the command is not one. Shared by /athreat and the
-- gear menu next to the save button.
local function apply(cmd, arg)
    if not settings then return false end
    if cmd == "" or cmd == "config" then openOptions(); return true
    elseif cmd == "on" then settings.enabled = true
    elseif cmd == "off" then settings.enabled = false
    elseif cmd == "show" then settings.enabled, settings.combatOnly = true, false
    elseif cmd == "auto" then settings.enabled, settings.combatOnly = true, true
    elseif cmd == "lock" then settings.locked = true
    elseif cmd == "unlock" then settings.locked = false
    elseif cmd == "position" then settings.at = nil; position()
    elseif cmd == "reset" then
        settings.at, settings.scale, settings.alpha, settings.backgroundAlpha = nil, 1, 1, 0.9; position()
    elseif cmd == "scale" and number(tonumber(arg)) then
        settings.scale = math.max(0.5, math.min(2, tonumber(arg))); position()
    elseif cmd == "alpha" and number(tonumber(arg)) then
        settings.alpha = math.max(0.2, math.min(1, tonumber(arg))); position()
    elseif cmd == "bg" and number(tonumber(arg)) then
        settings.backgroundAlpha = math.max(0, math.min(1, tonumber(arg))); position()
    elseif cmd == "warn" and number(tonumber(arg)) then settings.warn = math.max(1, math.min(100, tonumber(arg)))
    else return false end
    if editing then openOptions() end
    refresh()
    return true
end

SLASH_ADVENTURETHREAT1 = "/athreat"
SlashCmdList.ADVENTURETHREAT = function(msg)
    if not settings then return end
    local cmd, arg = tostring(msg or ""):lower():match("^%s*(%S*)%s*(.-)%s*$")
    if not apply(cmd, arg) then
        print("|cff33ff99仇恨面板|r /athreat show 常駐；auto 戰鬥中顯示；on/off 開關；lock/unlock 鎖定/拖曳；config 大小與透明度；scale 0.5–2；alpha 0.2–1；bg 0–1；warn 85；reset 重設外觀與位置（存檔按鈕旁的齒輪也能設定）")
        return
    end
    print("|cff33ff99仇恨面板|r 設定已更新。")
end

-- For the gear menu: "off", "auto" (in combat) or "show" (always), and whether it is locked.
function ns.threatPanelMode()
    if not settings then return nil end
    return not settings.enabled and "off" or settings.combatOnly and "auto" or "show", settings.locked
end
ns.threatPanelSet = apply

ns.collectLiveThreat, ns.refreshThreatPanel = collect, refresh
