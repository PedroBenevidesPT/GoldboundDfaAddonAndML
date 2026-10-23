-- DFA Auction Intelligence - HUD and in-game action queue
--
-- Everything that needs the game client used to live behind a slash
-- command the player had to remember.  This is the same set of actions,
-- on screen, with one button each:
--
--   Snapshot  bags + bank + warband + posted listings, then export
--   Undercut  the native scan (no Auctionator needed)
--   Mail      read the inbox for buyer names
--   Export    write the SavedVariables export now
--   Estimate  open DFA Estimate's reading panel (when that addon is loaded)
--
-- Every press is recorded in DFA_AUCTION_INTELLIGENCE_DB.actions with its
-- result.  The client writes SavedVariables on /reload or logout, the
-- out-of-game daemon imports that table, and *reacts* to it: a finished
-- undercut scan is what triggers the relist advice and the alert.  That is
-- the link the player asked for: press in game -> reload/logout -> the
-- continuous script picks the action up and slots it into the pipeline.
--
-- The visual build is best-effort.  A client whose frame templates changed
-- must never cost the action queue, so the frame is built inside pcall and
-- the actions work headless (the Lua harness proves exactly that).

local _, addon = ...

DFA_AI = DFA_AI or {}
DFA_AI.HUD = {}
local HUD = DFA_AI.HUD

local MAX_ACTIONS = 100
local frame = nil

HUD.error = nil


local function DB()
    return _G.DFA_AUCTION_INTELLIGENCE_DB
end

local function ExportNow()
    if DFA_AI.Export and DFA_AI.Export.GenerateExport then
        pcall(function() DFA_AI.Export:GenerateExport() end)
    end
end

---Records one action, whether it ran or was refused.
---@param name string
---@param ok boolean
---@param detail any
function HUD:RecordAction(name, ok, detail)
    local db = DB()
    if type(db) ~= "table" then
        return nil
    end
    db.actions = type(db.actions) == "table" and db.actions or {}
    local entry = {
        name = tostring(name or ""),
        at = time(),
        ok = ok and true or false,
        detail = tostring(detail or ""),
    }
    db.actions[#db.actions + 1] = entry
    while #db.actions > MAX_ACTIONS do
        table.remove(db.actions, 1)
    end
    db.last_action = entry
    return entry
end

local function SnapshotAction()
    local snapshot, problem = DFA_AI.Inventory:Snapshot("hud")
    if not snapshot then
        return false, tostring(problem or "snapshot failed")
    end
    local stats = DFA_AI.Inventory:GetStats()
    return true, string.format("bags=%d bank=%d warband=%d posted=%d listings=%d",
        stats.locations.bags or 0, stats.locations.bank or 0,
        stats.locations.warband or 0, stats.locations.posted or 0,
        stats.listings or 0)
end

local function UndercutAction()
    DFA_AI.Undercut:InstallHooks()
    local ok, why = DFA_AI.Undercut:StartScan()
    if not ok then
        return false, tostring(why or "scan did not start")
    end
    return true, "scan started; keep the Auction House open"
end

local function MailAction()
    -- Guard: Mail module may not be initialised yet (load-order race).
    if type(DFA_AI.Mail) ~= "table" or type(DFA_AI.Mail.ScanInbox) ~= "function" then
        return false, "Mail module not ready"
    end
    local report = DFA_AI.Mail:ScanInbox("hud", true)
    if type(report) ~= "table" then
        return false, "Mail scan returned no report"
    end
    if report.mailbox_closed then
        return false, "open the mailbox first, then press Mail"
    end
    if not report.ok then
        return false, tostring(report.error or "scan failed")
    end
    return true, string.format("inbox=%d invoices=%d stored=%d without buyer=%d",
        report.inbox_items or 0, report.invoices or 0, report.stored or 0,
        report.without_buyer or 0)
end

local function ExportAction()
    DFA_AI.Export:GenerateExport()
    return true, "export written"
end

local ACTIONS = {
    snapshot = SnapshotAction,
    undercut = UndercutAction,
    mail = MailAction,
    export = ExportAction,
}

---Runs one HUD action, records it, and exports so the daemon sees it.
---@param name string
---@return boolean ok
---@return string detail
function HUD:RunAction(name)
    local handler = ACTIONS[tostring(name or ""):lower()]
    if not handler then
        local detail = "unknown action: " .. tostring(name)
        self:RecordAction(name, false, detail)
        return false, detail
    end
    -- pcall returns every value the handler returned: keep the detail, not
    -- just the boolean, or the log says "true" instead of what was read.
    local ok, first, second = pcall(handler)
    local success, detail
    if not ok then
        success, detail = false, "error: " .. tostring(first)
    elseif first == false then
        success, detail = false, tostring(second or "")
    else
        success, detail = true, tostring(second or first or "")
    end
    self:RecordAction(name, success, detail)
    ExportNow()
    if self.Refresh then
        pcall(function() self:Refresh() end)
    end
    print(string.format("%s%s: %s", (DFA_AI.L and DFA_AI.L.PREFIX or "DFA: "),
        tostring(name), detail))
    return success, detail
end

---The action names the HUD offers, for the daemon and for the tests.
function HUD:GetActions()
    local names = {}
    for name in pairs(ACTIONS) do
        names[#names + 1] = name
    end
    table.sort(names)
    return names
end

---What the daemon would find in the export right now.
function HUD:GetStats()
    local db = DB()
    local stats = { available = type(frame) == "table", actions = 0,
                    last_action = nil, error = self.error }
    if type(db) == "table" and type(db.actions) == "table" then
        stats.actions = #db.actions
        stats.last_action = db.last_action or db.actions[#db.actions]
    end
    return stats
end

local function StatusLines()
    local lines = {}
    local db = DB()
    local events = 0
    if DFA_AI.DataStore and DFA_AI.DataStore.GetCount then
        events = DFA_AI.DataStore:GetCount() or 0
    end
    local inventory = DFA_AI.Inventory:GetStats()
    if inventory.available then
        lines[#lines + 1] = string.format("Bags %d  Bank %d  War %d",
            inventory.locations.bags or 0, inventory.locations.bank or 0,
            inventory.locations.warband or 0)
        lines[#lines + 1] = string.format("Posted %d units / %d listings  Guild %d",
            inventory.locations.posted or 0, inventory.listings or 0,
            inventory.locations.guildbank or 0)
        if not inventory.readable.posted then
            lines[#lines + 1] = "|cffffd200posted: unknown|r (open the AH)"
        end
    else
        lines[#lines + 1] = "Inventory: no snapshot yet"
    end
    if db and type(db.last_action) == "table" then
        lines[#lines + 1] = string.format("Last: %s %s",
            tostring(db.last_action.name),
            db.last_action.ok and "ok" or "failed")
    end
    lines[#lines + 1] = string.format("Events %d  Actions %d", events,
        (db and type(db.actions) == "table") and #db.actions or 0)
    return lines
end

function HUD:Refresh()
    if not frame or not frame.lines then return end
    local lines = StatusLines()
    for index, label in ipairs(frame.lines) do
        label:SetText(lines[index] or "")
    end
end

local function Call(object, method, ...)
    if not object or type(object[method]) ~= "function" then
        return nil
    end
    local ok, value = pcall(object[method], object, ...)
    if ok then return value end
    return nil
end

-- The HUD's layout, as numbers rather than magic constants.  The frame used
-- to be a fixed 200 tall while its contents needed about 164, so the panel
-- carried a dead band under the buttons and sat taller than anything in it.
-- Height is now derived from what is actually placed: four status lines, and
-- one button every two per row.
local LAYOUT = {
    width = 236,
    pad = 8,             -- inner margin on every side
    title_top = 6,       -- the title's own inset
    title_h = 12,
    line_h = 13,         -- one status line
    line_gap = 6,        -- between the title and the first line
    block_gap = 8,       -- between the last line and the buttons
    button_w = 108,
    button_h = 20,
    button_step = 24,    -- vertical pitch, i.e. height + the gap
    button_cols = 2,
}

---The height the contents need, so the frame is never taller than its job.
---@param line_count number
---@param button_count number
---@return number
local function RequiredHeight(line_count, button_count)
    local rows = math.ceil(button_count / LAYOUT.button_cols)
    local lines_bottom = LAYOUT.title_top + LAYOUT.title_h + LAYOUT.line_gap
        + line_count * LAYOUT.line_h
    local buttons_top = rows > 0
        and (rows - 1) * LAYOUT.button_step + LAYOUT.button_h or 0
    return lines_bottom + LAYOUT.block_gap + buttons_top + LAYOUT.pad
end


local function Build()
    if frame then return frame end
    local ok, built = pcall(function()
        local f = CreateFrame("Frame", "DFA_AuctionIntelligenceHUD",
            _G.UIParent, "BackdropTemplate")
        if not f then return nil end
        local status_lines = 4
        local button_count = 5
        Call(f, "SetSize", LAYOUT.width, RequiredHeight(status_lines, button_count))
        Call(f, "SetPoint", "CENTER", _G.UIParent, "CENTER", 0, 0)
        Call(f, "SetFrameStrata", "LOW")
        Call(f, "SetClampedToScreen", true)
        if not Call(f, "SetBackdrop", {
            bgFile = "Interface\\Buttons\\WHITE8X8",
            edgeFile = "Interface\\Buttons\\WHITE8X8",
            edgeSize = 1,
        }) then
            -- A client without the backdrop mixin still gets the buttons;
            -- an invisible background is better than no HUD.
        end
        Call(f, "SetBackdropColor", 0.05, 0.06, 0.08, 0.92)
        Call(f, "SetBackdropBorderColor", 0.25, 0.6, 0.9, 0.9)
        Call(f, "SetMovable", true)
        Call(f, "EnableMouse", true)
        Call(f, "RegisterForDrag", "LeftButton")
        Call(f, "SetScript", "OnDragStart", function(self)
            if self.StartMoving then self:StartMoving() end
        end)
        Call(f, "SetScript", "OnDragStop", function(self)
            if self.StopMovingOrSizing then self:StopMovingOrSizing() end
        end)

        local title = Call(f, "CreateFontString", nil, "GameFontNormalSmall")
        Call(title, "SetPoint", "TOPLEFT", f, "TOPLEFT", 8, -6)
        Call(title, "SetText", "DFA Intelligence")
        if title then f.title = title end

        local close = CreateFrame("Button", nil, f, "UIPanelCloseButton")
        Call(close, "SetPoint", "TOPRIGHT", f, "TOPRIGHT", -2, -2)
        Call(close, "SetScript", "OnClick", function()
            f:Hide()
        end)

        f.lines = {}
        for index = 1, 4 do
            local label = Call(f, "CreateFontString", nil, "GameFontHighlightSmall")
            Call(label, "SetPoint", "TOPLEFT", f, "TOPLEFT", LAYOUT.pad,
                -(LAYOUT.title_top + LAYOUT.title_h + LAYOUT.line_gap)
                    - (index - 1) * LAYOUT.line_h)
            Call(label, "SetJustifyH", "LEFT")
            Call(label, "SetText", "")
            f.lines[index] = label
        end

        local buttons = {
            { key = "snapshot", label = "Snapshot" },
            { key = "undercut", label = "Undercut scan" },
            { key = "mail", label = "Mail" },
            { key = "export", label = "Export" },
            -- not an action-queue entry: this is the visible link to the
            -- other addon's reading panel, the one the player asked for
            { key = "estimate", label = "Estimate", panel = true },
        }
        for index, spec in ipairs(buttons) do
            local button = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
            Call(button, "SetSize", LAYOUT.button_w, LAYOUT.button_h)
            local column = (index - 1) % LAYOUT.button_cols
            local row = math.floor((index - 1) / LAYOUT.button_cols)
            Call(button, "SetPoint", "BOTTOMLEFT", f, "BOTTOMLEFT",
                LAYOUT.pad + column * (LAYOUT.button_w + 4),
                LAYOUT.pad + row * LAYOUT.button_step)
            if button.SetText then
                button:SetText(spec.label)
            end
            Call(button, "SetScript", "OnClick", function()
                if spec.panel then
                    local slash = _G.SlashCmdList
                        and _G.SlashCmdList["DFAESTIMATE"]
                    if type(slash) == "function" then
                        local ok, err = pcall(slash, "")
                        if not ok then
                            print("DFA: Estimate panel error: " .. tostring(err))
                        end
                    else
                        print("DFA: DFA Estimate is not loaded.")
                    end
                else
                    HUD:RunAction(spec.key)
                end
            end)
            f.buttons = f.buttons or {}
            f.buttons[spec.key] = button
        end

        Call(f, "SetScript", "OnUpdate", (function()
            local elapsed = 0
            return function(_, delta)
                elapsed = elapsed + (delta or 0)
                if elapsed >= 5 then
                    elapsed = 0
                    HUD:Refresh()
                end
            end
        end)())
        return f
    end)
    if not ok or not built then
        HUD.error = tostring(built)
        return nil
    end
    frame = built
    HUD.error = nil
    pcall(function() HUD:Refresh() end)
    return frame
end

---Shows/hides the HUD.  Builds it on first use.
---@param show boolean?
function HUD:Toggle(show)
    local visible = Build()
    if not visible then
        return false, "the HUD could not be built: " .. tostring(HUD.error)
    end
    if show == nil then
        show = not (visible.IsShown and visible:IsShown())
    end
    if show then
        Call(visible, "Show")
    else
        Call(visible, "Hide")
    end
    pcall(function() self:Refresh() end)
    return true, show and "shown" or "hidden"
end

---Starts the HUD once the client is ready.  Called from Core at login.
function HUD:Initialize()
    local db = DB()
    if type(db) ~= "table" then
        return false
    end
    local settings = db.hud
    if type(settings) == "table" and settings.hidden then
        return false
    end
    local shown = self:Toggle(true)
    if not shown then
        return false
    end
    if type(settings) ~= "table" then
        db.hud = { hidden = false }
    end
    return true
end
