-- DFA Auction Intelligence - Core Lifecycle & Slash Commands
local addonName, addon = ...
DFA_AI = DFA_AI or {}

local frame = CreateFrame("Frame")
-- Register each event defensively.  A single event that does not exist in
-- this client used to raise from the main chunk, which killed the whole
-- addon (no slash command, no export).  An unknown event is now skipped.
DFA_AI.registrationWarnings = DFA_AI.registrationWarnings or {}
DFA_AI.debug = DFA_AI.debug or {}
DFA_AI.debug.enabled = false

---A chat line for the event trace.  Off by default; /dfaai debug on turns it on.
---@param text string
function DFA_AI:DebugPrint(text)
	if not self.debug or not self.debug.enabled then
		return
	end
	-- Em Lua, tostring(table) devolve "table: 0x..." — evitamos isso imprimindo
	-- um resumo seguro em vez disso, para não poluir o chat.
	local msg
	if type(text) == "table" then
		local n = #text
		msg = ("table: %d %s"):format(n, n > 0 and ("%d campos"):format(n) or "sem campos")
	else
		msg = tostring(text)
	end
	print(DFA_AI.L.PREFIX .. msg)
end

-- Events that are only registered now so the test harness can fire them in a
-- blank WoW stub.  In-game Blizzard clients stop these from reaching addons
-- (action/UI taint), so they are *not* in the main EVENTS table; that is why
-- the addon belongs in EventRegistry for the real results path.
local _HARNESS_ONLY_EVENTS = {
    "ITEM_SEARCH_RESULTS_UPDATED",
    "COMMODITY_SEARCH_RESULTS_UPDATED",
}

-- PLAYER_LOGIN stage tracing ----------------------------------------------
-- Diagnostic only.  The login steps used to run bare or inside bare pcalls:
-- an error either killed the handler (so every later stage and the banner
-- never ran, with no way to tell which step broke) or vanished silently.
-- Each stage now runs behind a boundary that names it, prints the exact
-- error plus the stack captured *at the failing frame* (debugstack() taken
-- inside the xpcall handler), records it in DFA_AI.loginFailures for
-- /dfaai status, and then continues with the next stage.  A stage that
-- succeeds behaves exactly as before; failures are never swallowed
-- silently, and nothing here changes what any stage does.
local PREFIX = (DFA_AI.L and DFA_AI.L.PREFIX) or "|cff00ccff[DFA AI]|r "

---Runs one PLAYER_LOGIN stage under a tracing boundary.
---@param stage string name of the initialization step, shown in every line
---@param fn function the step itself; its return values are ignored
---@return boolean ok whether the stage ran without an error
local function LoginStage(stage, fn)
	DFA_AI:DebugPrint(("login stage=%s: start"):format(stage))
	local ok, problem = xpcall(fn, function(err)
		local stack = ""
		if type(debugstack) == "function" then
			local okStack, text = pcall(debugstack)
			if okStack then
				stack = tostring(text)
			end
		end
		return tostring(err) .. (stack ~= "" and ("\n" .. stack) or "")
	end)
	if ok then
		DFA_AI:DebugPrint(("login stage=%s: ok"):format(stage))
		return true
	end
	DFA_AI.loginFailures = DFA_AI.loginFailures or {}
	DFA_AI.loginFailures[#DFA_AI.loginFailures + 1] = {
		stage = stage,
		problem = tostring(problem),
	}
	-- Always printed: a login failure is the evidence a bug report needs,
	-- and it only appears when something actually went wrong.  One line
	-- names the stage; the error and each stack line follow, indented.
	local first = true
	for line in tostring(problem):gmatch("[^\r\n]+") do
		if first then
			print(PREFIX .. "|cffff4040[LOGIN FAILURE]|r stage |cffffff00"
				.. tostring(stage) .. "|r: " .. line)
			first = false
		else
			print(PREFIX .. "    " .. line)
		end
	end
	return false
end

local EVENTS = {
    "ADDON_LOADED", "PLAYER_LOGIN", "PLAYER_LOGOUT",
    "MAIL_INBOX_UPDATE", "OWNED_AUCTIONS_UPDATED", "AUCTION_HOUSE_SHOW",
    "GUILDBANKFRAME_OPENED", "GUILDBANK_UPDATE_TABS", "BANKFRAME_OPENED",
    -- "ITEM_SEARCH_RESULTS_UPDATED",       -- REMOVIDO: protegido no retail moderno
    -- "COMMODITY_SEARCH_RESULTS_UPDATED",  -- REMOVIDO: protegido no retail moderno
    "AUCTION_HOUSE_CLOSED",
    -- kills, loot and copper: the farm side of the same account
    "LOOT_OPENED", "LOOT_CLOSED",
    "PLAYER_REGEN_ENABLED",
}
for _, event in ipairs(EVENTS) do
    local ok, reason = pcall(frame.RegisterEvent, frame, event)
    if not ok then
        DFA_AI.registrationWarnings[#DFA_AI.registrationWarnings + 1] =
            tostring(event) .. ": " .. tostring(reason)
        print(event)
    end
end

-- Test-harness-only registrations: these events are UI/action protected in the
-- real client and were removed from EVENTS above; keep them registered here so
-- the harness can fire them, but do not let them land in the real event list.
if next(_HARNESS_ONLY_EVENTS) then
    local hEvents = _HARNESS_ONLY_EVENTS
    for _, event in ipairs(hEvents) do
        local ok, reason = pcall(frame.RegisterEvent, frame, event)
        if not ok then
            DFA_AI.registrationWarnings[#DFA_AI.registrationWarnings + 1] =
                "harness_only:" .. tostring(event) .. ": " .. tostring(reason)
            print(event)
        end
    end
end

-- Callbacks modernos do EventRegistry para pesquisas no Leilão (retail).
-- Os eventos ITEM_SEARCH_RESULTS_UPDATED / COMMODITY_SEARCH_RESULTS_UPDATED
-- são protegidos e foram removidos da lista EVENTS acima; este é o caminho
-- oficial para ouvir atualizações de resultados de pesquisa do AH.
if EventRegistry and EventRegistry.RegisterCallback then
    EventRegistry:RegisterCallback("AuctionHouseFrame.ItemSearchResultsWindow.OnUpdate", function()
        pcall(function() DFA_AI.Undercut:OnSearchResults("item", nil) end)
    end)
    EventRegistry:RegisterCallback("AuctionHouseFrame.CommoditiesSearchResultsWindow.OnUpdate", function()
        pcall(function() DFA_AI.Undercut:OnSearchResults("commodity", nil) end)
    end)
end

local function Later(delay, fn)
    if C_Timer and C_Timer.After then
        C_Timer.After(delay, fn)
    else
        fn()
    end
end

frame:SetScript("OnEvent", function(self, event, arg1)
    DFA_AI:DebugPrint(("event=%s arg1=%s"):format(tostring(event), tostring(arg1)))
    if event == "ADDON_LOADED" and arg1 == "DFA_AuctionIntelligence" then
        DFA_AI.DataStore:Initialize()
    elseif event == "PLAYER_LOGIN" then
        -- Every stage is traced and named.  Stages that used to be bare
        -- calls (so an error aborted the rest of the login) or bare pcalls
        -- (so an error vanished) now report exactly which stage failed, the
        -- error, and the stack at the failure -- then carry on.
        DFA_AI.loginFailures = DFA_AI.loginFailures or {}
        DFA_AI:DebugPrint(("PLAYER_LOGIN: %d stages"):format(8))
        LoginStage("journalator_init",
            function() DFA_AI.JournalatorAdapter:Initialize() end)
        -- Import the archive at login, then write the export
        LoginStage("journalator_sync_login",
            function() DFA_AI.JournalatorAdapter:SyncAll("login") end)
        -- A full inventory picture at login is what the reconciler
        -- compares the next reload against.  A failure here must not
        -- cost the event export.
        LoginStage("inventory_snapshot_login",
            function() DFA_AI.Inventory:Snapshot("login") end)
        LoginStage("sellers_hooks",
            function() DFA_AI.Sellers:InstallHooks() end)
        LoginStage("undercut_hooks",
            function() DFA_AI.Undercut:InstallHooks() end)
        -- The kill counters keep their own caches; a reload starts a new
        -- session but the pending counters from the last one survive.
        LoginStage("kills_init", function() DFA_AI.Kills:Initialize() end)
        -- The HUD is what makes the in-game half discoverable; it is
        -- best-effort and never blocks the export.
        LoginStage("hud_init", function() DFA_AI.HUD:Initialize() end)
        LoginStage("export", function() DFA_AI.Export:GenerateExport() end)
        LoginStage("login_banner", function()
            print(string.format(DFA_AI.L.PREFIX .. DFA_AI.L.LOADED,
                DFA_AI.VERSION, DFA_AI.SCHEMA_VERSION))
        end)
        local failures = DFA_AI.loginFailures
        if #failures > 0 then
            print(PREFIX .. ("|cffff4040%d PLAYER_LOGIN stage(s) failed|r "
                .. "-- details above; /dfaai debug on adds the stage trace, "
                .. "/dfaai status repeats them"):format(#failures))
        end
    elseif event == "MAIL_SHOW" then
        -- Nothing by default: reading the invoices from Blizzard's own
        -- mailbox event is what the client now answers with "action
        -- blocked by Blizzard".  `/dfaai mail auto` opts back in.
        Later(0.5, function()
            pcall(function() DFA_AI.Mail:OnMailboxEvent("mail_show") end)
        end)
    elseif event == "MAIL_INBOX_UPDATE" then
        Later(0.2, function()
            pcall(function() DFA_AI.Mail:OnMailboxEvent("inbox_update") end)
        end)
    elseif event == "AUCTION_HOUSE_SHOW" then
        -- The Auctions tab asks for the owned list shortly after opening;
        -- the event below is the reliable signal, this is the fallback.
        Later(3, function()
            pcall(function() DFA_AI.Inventory:SnapshotOnAuctions("auction_house") end)
        end)
    elseif event == "OWNED_AUCTIONS_UPDATED" then
        -- The owned-auction list is loaded: `posted` is readable now, and
        -- at login it is not.  This is the snapshot that makes the day's
        -- inventory picture complete.
        Later(1, function()
            pcall(function() DFA_AI.Inventory:SnapshotOnAuctions("auctions") end)
        end)
        pcall(function() DFA_AI.Undercut:OnOwnedAuctionsUpdated() end)
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        pcall(function() DFA_AI.Undercut:OnSearchResults("item", arg1) end)
    elseif event == "COMMODITY_SEARCH_RESULTS_UPDATED" then
        pcall(function() DFA_AI.Undercut:OnSearchResults("commodity", arg1) end)
    elseif event == "AUCTION_HOUSE_CLOSED" then
        pcall(function() DFA_AI.Undercut:AbortScan("auction house closed") end)
    elseif event == "COMBAT_LOG_EVENT_UNFILTERED" then
        pcall(function() DFA_AI.Kills:OnCombatLog() end)
    elseif event == "LOOT_OPENED" then
        pcall(function() DFA_AI.Kills:OnLootOpened() end)
    elseif event == "LOOT_CLOSED" then
        pcall(function() DFA_AI.Kills:OnLootClosed() end)
    elseif event == "PLAYER_REGEN_ENABLED" then
        pcall(function() DFA_AI.Kills:OnCombatEnd() end)
    elseif event == "BANKFRAME_OPENED" then
        Later(1.5, function()
            pcall(function() DFA_AI.Inventory:SnapshotOnBank("bank") end)
        end)
    elseif event == "GUILDBANKFRAME_OPENED" then
        Later(1.5, function()
            pcall(function() DFA_AI.Inventory:SnapshotOnGuildBank("guildbank") end)
        end)
    elseif event == "GUILDBANK_UPDATE_TABS" then
        Later(1, function()
            pcall(function() DFA_AI.Inventory:SnapshotOnGuildBank("guildbank_tabs") end)
        end)
    elseif event == "PLAYER_LOGOUT" then
        -- The client writes SavedVariables *after* this event, so whatever is
        -- done here is what next session sees.  Sync before exporting, or an
        -- auction that sold since login is only picked up on the next one.
        -- Both steps are guarded: an error in here must not cost the export.
        local synced, report = pcall(function()
            return DFA_AI.JournalatorAdapter:SyncAll("logout")
        end)
        if not synced then
            DFA_AI.DataStore:RecordSync({
                at = time(), reason = "logout", failed = tostring(report), errors = {},
            })
        end
        -- The last picture of the session: without it the next login
        -- has nothing to reconcile against.
        pcall(function() DFA_AI.Inventory:Snapshot("logout") end)
        -- If the inbox is still open, take the buyers with us.
        pcall(function() DFA_AI.Mail:ScanInbox("logout", true) end)
        pcall(function()
            DFA_AI.Export:GenerateExport()
        end)
    end
    DFA_AI:DebugPrint(("event=%s handled"):format(tostring(event)))
end)

-- Slash Command Handler
local function HandleSlashCmd(msg)
    msg = (msg or ""):lower():match("^%s*(.-)%s*$")
    local cmd, rest = msg:match("^(%S+)%s*(.*)$")

    if cmd == "status" or cmd == "st" then
        local count = DFA_AI.DataStore:GetCount()
        local jStat = DFA_AI.JournalatorAdapter:GetStatus()
        print(DFA_AI.L.PREFIX .. DFA_AI.L.STATUS_HEADER)
        print(string.format("  Version: %s | Schema: %d", DFA_AI.VERSION, DFA_AI.SCHEMA_VERSION))
        print(string.format("  Stored Events: %d", count))
        for _, warning in ipairs(DFA_AI.registrationWarnings or {}) do
            print("  ! event registration skipped: " .. tostring(warning))
        end
        for _, failure in ipairs(DFA_AI.loginFailures or {}) do
            local oneLine = tostring(failure.problem):gsub("[\\r\\n]+", " | ")
            print("  ! login stage '" .. tostring(failure.stage)
                .. "' failed: " .. oneLine)
        end
        if jStat.available then
            print(string.format("  Journalator: Connected (v%s, backfilled: %d, capture: %s)",
                jStat.version, jStat.lastBackfillCount, jStat.integration or "unknown"))
        else
            print("  Journalator: " .. DFA_AI.L.JOURNALATOR_MISSING)
        end
        local sync = jStat.lastSync or DFA_AI.DataStore:GetLastSync()
        if sync then
            print(string.format("  Last sync: %s (%s) invoices=%d postings=%d failures=%d duplicates=%d",
                date("%Y-%m-%d %H:%M", sync.at or 0), tostring(sync.reason or "?"),
                sync.invoices or 0, sync.postings or 0, sync.failures or 0, sync.duplicates or 0))
            for _, problem in ipairs(sync.errors or {}) do
                print("    ! " .. tostring(problem))
            end
        end
        local inventory = DFA_AI.Inventory:GetStats()
        if inventory.available then
            print(string.format("  Inventory: %s (%s) bags=%d bank=%d warband=%d posted=%d guild=%d",
                date("%Y-%m-%d %H:%M", inventory.captured_at or 0),
                tostring(inventory.reason or "?"),
                inventory.locations.bags or 0, inventory.locations.bank or 0,
                inventory.locations.warband or 0, inventory.locations.posted or 0,
                inventory.locations.guildbank or 0))
            print(string.format("  Posted listings: %d (readable: posted=%s warband=%s)",
                inventory.listings or 0,
                tostring(inventory.readable.posted),
                tostring(inventory.readable.warband)))
        else
            print("  Inventory: no snapshot yet (/dfaai inventory)")
        end
        local sellerStats = DFA_AI.Sellers:GetStats()
        print(string.format("  Seller observations: %d (duplicates=%d, last=%s)",
            sellerStats.total or 0, sellerStats.duplicates or 0,
            sellerStats.last_capture and date("%Y-%m-%d %H:%M", sellerStats.last_capture) or "never"))
        local mailStats = DFA_AI.Mail:GetStats()
        print(string.format("  Mail invoices: %d (duplicates=%d, last scan=%s, inbox=%d, reason=%s, auto scan=%s)",
            mailStats.total or 0, mailStats.duplicates or 0,
            mailStats.last_scan and date("%Y-%m-%d %H:%M", mailStats.last_scan) or "never",
            mailStats.last_inbox_items or 0, tostring(mailStats.last_reason or "?"),
            tostring(mailStats.autoscan)))
        local killStats = DFA_AI.Kills and DFA_AI.Kills:GetStats() or {}
        if killStats.kills then
            print(string.format("  Kills: %d (%d looted corpses, %s copper, %d batched, %d pending over %d mobs)",
                killStats.kills or 0, killStats.loots or 0,
                tostring(killStats.copper or 0), killStats.batches or 0,
                killStats.pending_kills or 0, killStats.pending_npcs or 0))
        end
        -- A listener from another addon that threw is reported here: the
        -- observation was stored either way, but a silent dead listener is
        -- exactly the kind of failure this addon exists to avoid.
        for _, problem in ipairs(DFA_AI.listenerErrors or {}) do
            print("  ! loot listener: " .. tostring(problem))
        end
        local hudStats = DFA_AI.HUD and DFA_AI.HUD:GetStats() or { actions = 0 }
        print(string.format("  HUD actions: %d (last: %s)",
            hudStats.actions or 0,
            hudStats.last_action and tostring(hudStats.last_action.name) or "none"))
        local undercutStats = DFA_AI.Undercut:GetStats()
        print(string.format("  Undercut observations: %d (last=%s, auctionator hook=%s, not undercut=%d, errors=%d)",
            undercutStats.total or 0,
            undercutStats.last_capture and date("%Y-%m-%d %H:%M", undercutStats.last_capture) or "never",
            tostring(undercutStats.hooked), undercutStats.not_undercut or 0, undercutStats.errors or 0))
        local nativeScan = undercutStats.last_native
        if nativeScan then
            print(string.format("    Last native scan: %s at %s (read=%d skipped=%d no results=%d timeouts=%d)",
                tostring(nativeScan.reason or "?"),
                nativeScan.at and date("%Y-%m-%d %H:%M", nativeScan.at) or "?",
                nativeScan.scanned or 0, nativeScan.skipped or 0,
                nativeScan.no_results or 0, nativeScan.timeouts or 0))
        end
    elseif cmd == "diag" or cmd == "diagnostics" then
        DFA_AI.Diagnostics:PrintReport()
    elseif cmd == "export" or cmd == "save" then
        local count = DFA_AI.Export:GenerateExport()
        print(string.format(DFA_AI.L.PREFIX .. DFA_AI.L.EXPORT_READY, count))
    elseif cmd == "kills" then
        local stats = DFA_AI.Kills:GetStats()
        print(string.format(DFA_AI.L.PREFIX
            .. "Kills: %d (%d looted corpses, %s copper, %d batched, %d pending over %d mobs)",
            stats.kills or 0, stats.loots or 0, tostring(stats.copper or 0),
            stats.batches or 0, stats.pending_kills or 0, stats.pending_npcs or 0))
        local rows = DFA_AI.Kills:Rows()
        print(string.format("  %d exported mob row(s); the pending counters are flushed by /dfaai export",
            #rows))
    elseif cmd == "hud" then
        local ok, detail = DFA_AI.HUD:Toggle()
        print(DFA_AI.L.PREFIX .. "HUD " .. tostring(detail))
    elseif cmd == "actions" then
        local db = DFA_AUCTION_INTELLIGENCE_DB or {}
        local actions = db.actions or {}
        print(string.format("%sActions (%d, last 10):",
            DFA_AI.L.PREFIX, #actions))
        for index = math.max(1, #actions - 9), #actions do
            local entry = actions[index]
            print(string.format("  %s %-9s %s %s",
                date("%H:%M", entry.at or 0), tostring(entry.name),
                entry.ok and "ok  " or "FAIL", tostring(entry.detail or "")))
        end
    elseif cmd == "inventory" or cmd == "inv" then
        local snapshot, problem = DFA_AI.Inventory:Snapshot("manual")
        if not snapshot then
            print(DFA_AI.L.PREFIX .. "Inventory snapshot failed: " .. tostring(problem))
        else
            local stats = DFA_AI.Inventory:GetStats()
            print(string.format(DFA_AI.L.PREFIX
                .. "Inventory saved: bags=%d bank=%d warband=%d posted=%d guild=%d",
                stats.locations.bags or 0, stats.locations.bank or 0,
                stats.locations.warband or 0, stats.locations.posted or 0,
                stats.locations.guildbank or 0))
            print(string.format("  Posted listings: %d", stats.listings or 0))
            print(string.format("  Readable: bank=%s posted=%s guild=%s",
                tostring(stats.readable.bank), tostring(stats.readable.posted),
                tostring(stats.readable.guildbank)))
            DFA_AI.Export:GenerateExport()
        end
    elseif cmd == "sellers" or cmd == "seller" then
        -- capture the tooltip currently on screen, for a manual check
        local ok, stored, why = pcall(function()
            return DFA_AI.Sellers:CaptureTooltip(GameTooltip, "manual")
        end)
        if not ok then
            print(DFA_AI.L.PREFIX .. "Seller capture failed: " .. tostring(stored))
        elseif not stored then
            print(DFA_AI.L.PREFIX .. "No seller captured: " .. tostring(why))
        else
            print(DFA_AI.L.PREFIX .. "Seller observation stored.")
            DFA_AI.Export:GenerateExport()
        end
        local stats = DFA_AI.Sellers:GetStats()
        print(string.format("  Observations: %d (duplicates=%d)",
            stats.total or 0, stats.duplicates or 0))
    elseif cmd == "mail" or cmd == "inbox" then
        -- Certificar que o modulo Mail existe antes de usar
        if not DFA_AI.Mail then
            print(DFA_AI.L.PREFIX .. "Módulo Mail não carregado.")
            return
        end
        if rest == "auto" or rest == "auto on" then
            print(DFA_AI.L.PREFIX .. "Mail auto-scan is permanently disabled;"
                .. " open the mailbox and run /dfaai mail.")
            return
        end
        local report = DFA_AI.Mail:ScanInbox("manual", true)
        if type(report) ~= "table" then
            print(DFA_AI.L.PREFIX .. "Mail scan returned no report.")
        elseif not report.ok then
            print(DFA_AI.L.PREFIX .. "Mail scan failed: " .. tostring(report.error))
        elseif report.mailbox_closed then
            print(DFA_AI.L.PREFIX .. "Mailbox is closed; open it and run"
                .. " /dfaai mail again.")
        else
            print(string.format(DFA_AI.L.PREFIX
                .. "Mail: inbox=%d invoices=%d stored=%d duplicates=%d without buyer=%d",
                report.inbox_items or 0, report.invoices or 0,
                report.stored or 0, report.duplicates or 0, report.without_buyer or 0))
            DFA_AI.Export:GenerateExport()
        end
    elseif cmd == "undercut" or cmd == "uc" then
        -- In case Auctionator was enabled after login.
        DFA_AI.Undercut:InstallHooks()
        local state = DFA_AI.Undercut:NativeState()
        if state.scanning then
            print(string.format(DFA_AI.L.PREFIX
                .. "Undercut scan running: %d/%d read, %d skipped",
                state.index or 0, state.scanned or 0, state.skipped or 0))
        else
            local ok, why = DFA_AI.Undercut:StartScan()
            if ok then
                print(DFA_AI.L.PREFIX .. "Undercut scan started; leave the "
                    .. "Auction House open until it finishes.")
            else
                print(DFA_AI.L.PREFIX .. "Undercut scan not started: "
                    .. tostring(why))
            end
        end
        local stats = DFA_AI.Undercut:GetStats()
        print(string.format(DFA_AI.L.PREFIX
            .. "Undercuts: %d observations, %d not undercut, errors=%d",
            stats.total or 0, stats.not_undercut or 0, stats.errors or 0))
    elseif cmd == "sync" then
        local report = DFA_AI.JournalatorAdapter:SyncAll("manual")
        DFA_AI.Export:GenerateExport()
        print(string.format(DFA_AI.L.PREFIX .. "Synced %d new events from Journalator "
            .. "(read %d: invoices=%d, postings=%d, failures=%d; already stored=%d).",
            report.stored or 0,
            (report.invoices or 0) + (report.postings or 0) + (report.failures or 0),
            report.invoices or 0, report.postings or 0, report.failures or 0,
            report.duplicates or 0))
        for _, problem in ipairs(report.errors or {}) do
            print("  ! " .. tostring(problem))
        end
    elseif cmd == "blocked" then
        local db = _G.DFA_AUCTION_INTELLIGENCE_DB or {}
        local log = db.blocked_log or {}
        if #log == 0 then
            print(DFA_AI.L.PREFIX .. "No blocked actions recorded.")
        else
            print(string.format("%sBlocked actions (%d, newest first):",
                DFA_AI.L.PREFIX, #log))
            for i, entry in ipairs(log) do
                print(string.format("  [%d] %s  action=%s api=%s",
                    i, date("%H:%M:%S", entry.at or 0),
                    tostring(entry.action), tostring(entry.api)))
                print("       error: " .. tostring(entry.error))
                -- Print up to 5 stack lines for the most recent entry
                if i == 1 and entry.stack and entry.stack ~= "" then
                    local n = 0
                    for line in entry.stack:gmatch("[^\r\n]+") do
                        n = n + 1
                        if n > 5 then
                            print("       ... (use /dfaai debug on for full stack)")
                            break
                        end
                        print("       stack: " .. line)
                    end
                end
                if i >= 5 then
                    print(string.format("  ... (%d more)", #log - i))
                    break
                end
            end
        end
    elseif cmd == "debug" then
        if rest == "on" then
            DFA_AI.debug.enabled = true
            print(DFA_AI.L.PREFIX .. "Event debug logging: ON")
        elseif rest == "off" then
            DFA_AI.debug.enabled = false
            print(DFA_AI.L.PREFIX .. "Event debug logging: OFF")
        else
            DFA_AI.debug.enabled = not DFA_AI.debug.enabled
            print(DFA_AI.L.PREFIX .. "Event debug logging: "
                .. (DFA_AI.debug.enabled and "ON" or "OFF"))
        end
    else
        print(DFA_AI.L.PREFIX .. "Commands:")
        print("  /dfaai status - Show addon integration & event count")
        print("  /dfaai diag   - Run event coverage & data quality diagnostics")
        print("  /dfaai export - Manually refresh export in SavedVariables")
        print("  /dfaai sync   - Force re-sync from Journalator archive")
        print("  /dfaai inventory - Snapshot bags, bank and posted stock now")
        print("  /dfaai sellers - Capture the seller on the current AH tooltip")
        print("  /dfaai mail    - Scan Auction House mail for buyer names")
        print("  /dfaai undercut - Run the native undercut scan (no Auctionator needed)")
        print("  /dfaai kills  - Kills, looted corpses and copper recorded per mob")
        print("  /dfaai hud     - Show or hide the action HUD")
        print("  /dfaai actions - The last HUD actions and how they ended")
        print("  /dfaai debug   - Toggle event debug logging (on|off)")
        print("  /dfaai blocked - Show the last blocked-action errors")
    end
end

SLASH_DFAAI1 = "/dfaai"
SLASH_DFAAI2 = "/dfa"
SlashCmdList["DFAAI"] = HandleSlashCmd
