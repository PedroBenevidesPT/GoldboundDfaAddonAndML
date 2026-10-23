-- DFA Auction Intelligence - Auction House mail invoices
--
-- The chat/invoice stream sometimes records a sale without the buyer
-- name.  The mailbox invoice does carry it: GetInboxInvoiceInfo(index)
-- returns invoiceType, itemName, playerName, bid, buyout, deposit,
-- consignment, ..., count.  This module reads that invoice without
-- opening each mail and stores it as evidence.  It never creates a sale
-- event: the Python linker attaches the buyer to the sale that already
-- exists in the journal.
--
-- Three details matter in the live client:
--   * the inbox count is available before the invoice cache is populated, so
--     a read with no invoice data is retried a few times instead of being
--     treated as an empty mailbox;
--   * the mailbox is read **on demand only**.  Scanning it from MAIL_SHOW
--     means the addon reads restricted data while Blizzard is opening a
--     frame it owns, and this client answers that with "action blocked by
--     Blizzard" -- the mailbox became unusable.  Auto-scan is permanently
--     off; the manual `/dfaai mail` path is the only supported one;
--   * invoice fields can be secret values (issecretvalue).  They are read
--     through Number()/Trim(), which turn a secret into nil or "", so a
--     restricted invoice is skipped instead of raising inside the scan.

local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.Mail = {}
local Mail = DFA_AI.Mail

MAX_INVOICES = 20000
MIN_SCAN_INTERVAL = 2
MAX_RETRIES = 3
AH_MAIL_LIFETIME_DAYS = 30

lastScanAt = 0
lastInboxCount = 0
MailboxPadding = 22

function Trim(text)
    -- a secret value cannot be matched or concatenated
    if type(text) ~= "string" then
        return ""
    end
    if DFA_AI.IsSecret(text) then
        return ""
    end
    return (text or ""):match("^%s*(.-)%s*$")
end

-- Helpers ------------------------------------------------------------------

-- A restricted ("secret") value cannot be read: not even compared.  Every
-- field that comes from the mailbox is tested before it is used.
local function IsRestricted(value)
    return DFA_AI.IsSecret ~= nil and DFA_AI.IsSecret(value) == true
end

local function Number(value)
    if value == nil or IsRestricted(value) then
        return nil
    end
    return tonumber(value)
end

-- The first positive amount among the invoice's money fields.  ``0`` is a
-- valid Lua value, so a plain ``a or b`` kept a zero bid and dropped the
-- buyout that actually identified the sale.
local function FirstPositive(...)
    for i = 1, select("#", ...) do
        local value = Number((select(i, ...)))
        if value and value > 0 then
            return value
        end
    end
    return 0
end

function Escaped(text)
    -- build a Lua-safe "quoted string" literal that prints back into chat
    -- without creating '/' slash commands or odd control characters.
    local out = {"["}
    for i = 1, #text do
        local byte = text:byte(i)
        if byte == 0x5c then
            -- backslash stays literal
            out[#out + 1] = "\\\\"
        elseif byte < 0x20 then
            -- control chars are never helpful
            out[#out + 1] = string.format("\\%03d", byte)
        else
            out[#out + 1] = text:sub(i, i)
        end
    end
    out[#out + 1] = "]"
    return table.concat(out)
end

function MailboxButton()
    local anchor = CreateFrame(
        "Button",
        "DFA_AI_MailboxAnchor",
        UIParent,
        "UIPanelButtonTemplate"
    )
    anchor:SetText("Open Mailbox")
    anchor:SetSize(140, 22)
    anchor:SetPoint("TOP", 0, -180)
    anchor:SetScript("OnClick", function()
        ClearCursor()
        
        -- Atualizado para o formato de caminhos moderno do WoW
        PlaySoundFile("Sound/Interface/RifleScopeTargetAcquired.wav", "SFX")
        
        -- Auto-scan is permanently disabled: read invoices only manually
        -- with /dfaai mail command
    end)
    return anchor
end


-- The item link of an invoice is optional evidence: the buyer name, the
-- item name and the prices already come from GetInboxInvoiceInfo.  The
-- retail client has no GetInboxInvoiceItemLink at all, so a missing link
-- API is never fatal -- the invoice is stored without the link.
local function InvoiceItemLink(index)
    -- GetInboxInvoiceItemLink was removed from the retail client API.
    -- GetInboxItemLink(index, attachmentIndex) is the surviving call; the
    -- old name stays as a fallback for clients that still expose it.  Both
    -- are optional evidence: a missing link never loses the invoice.
    for _, getter in ipairs({ GetInboxItemLink, GetInboxInvoiceItemLink }) do
        if type(getter) == "function" then
            local ok, link = pcall(getter, index, 1)
            if ok and type(link) == "string" and link ~= "" and not IsRestricted(link) then
                return link
            end
        end
    end
    return ""
end

-- Some clients leave the invoice count empty and report the stack size on
-- the attachment instead.  Reading it is optional: a missing API is 1 item.
local function AttachmentCount(index)
    if type(GetInboxItem) ~= "function" then
        return nil
    end
    local ok, _name, _id, _icon, count = pcall(GetInboxItem, index, 1)
    if not ok then
        return nil
    end
    local quantity = Number(count)
    if quantity and quantity >= 1 then
        return quantity
    end
    return nil
end

-- Helpers ------------------------------------------------------------------

local ESCAPE_MAP = {
    ["\\"] = "\\\\",
    ['"'] = '\\"',
    ["\n"] = "\\n",
    ["\r"] = "\\r",
    ["\t"] = "\\t",
}
function EscapeJSON(s)
    if type(s) ~= "string" then
        return '""'
    end
    local escaped = s:gsub(".", ESCAPE_MAP)
    return '"' .. escaped .. '"'
end

-- A stable identity for one invoice.  ``seenAt`` is deliberately absent:
-- the same mail is read again on every scan, so a key built from the scan
-- time made every read look new and nothing was ever a duplicate.  The
-- invoice type is absent too: the client rewrites a "Sale Pending" mail
-- (``seller_temp_invoice``) as the seller invoice for the same sale.
function MailKey(row)
    return table.concat({
        tostring(row.itemName or ""),
        tostring(row.playerName or ""),
        tostring(Number(row.quantity) or 1),
        tostring(Number(row.valueCopper) or 0),
        tostring(Number(row.depositCopper) or 0),
    }, "|")
end

function Mail:IndexHas(key)
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) ~= "table" or type(db.mail_index) ~= "table" then
        return false
    end
    return db.mail_index[key] == true
end

-- Persist one invoice.  The SavedVariables table is the only store the
-- exporter reads, so an invoice that is not appended here is lost even
-- when the scan counted it.
function Mail:Append(record)
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) ~= "table" and DFA_AI.DataStore and DFA_AI.DataStore.Initialize then
        db = DFA_AI.DataStore:Initialize()
    end
    if type(db) ~= "table" then
        return false, "store not initialized"
    end
    local key = MailKey(record)
    db.mail_invoices = db.mail_invoices or {}
    db.mail_index = db.mail_index or {}
    -- A DB written before this index existed must not be appended to again
    -- as if every row were new.
    if #db.mail_invoices > 0 and next(db.mail_index) == nil then
        for _, row in ipairs(db.mail_invoices) do
            db.mail_index[MailKey(row)] = true
        end
    end
    if db.mail_index[key] then
        return false, "duplicate"
    end
    table.insert(db.mail_invoices, record)
    db.mail_index[key] = true
    db.mail_stats = db.mail_stats or {}
    db.mail_stats.by_type = db.mail_stats.by_type or {}
    local kind = tostring(record.invoiceType or "unknown")
    db.mail_stats.by_type[kind] = (db.mail_stats.by_type[kind] or 0) + 1
    db.mail_stats.total = #db.mail_invoices
    db.mail_stats.last_capture = record.seenAt or time()
    return true
end

-- One JSON object per line, strings quoted and escaped.  The old code
-- called EscapeJSON() on the whole record, which stringified the table as
-- "table: 0x..." and the importer discarded every line.
local MAIL_FIELDS = {
    "seenAt", "estimatedSaleAt", "daysLeft", "realm", "character",
    "invoiceType", "itemId", "itemName", "playerName", "quantity",
    "valueCopper", "buyoutCopper", "depositCopper", "consignmentCopper",
    "subject", "sender", "itemLink", "source", "confidence",
}

function Mail:ToJSONLines()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local rows = db and db.mail_invoices or {}
    local lines = {}
    for _, row in ipairs(rows) do
        local parts = {}
        for _, key in ipairs(MAIL_FIELDS) do
            local value = row[key]
            local kind = type(value)
            if kind == "number" then
                parts[#parts + 1] = EscapeJSON(key) .. ":" .. tostring(value)
            elseif kind == "boolean" then
                parts[#parts + 1] = EscapeJSON(key) .. ":" .. (value and "true" or "false")
            elseif kind == "string" and value ~= "" then
                parts[#parts + 1] = EscapeJSON(key) .. ":" .. EscapeJSON(value)
            end
        end
        lines[#lines + 1] = "{" .. table.concat(parts, ",") .. "}"
    end
    return table.concat(lines, "\n")
end


function Mail:ScanInbox(reason, includeCurrent, attempt)
    --[[[
        Scan the inbox for invoices and store them as evidence.

        @param reason string   Why are we scanning? (e.g. "manual", "logout")
        @param includeCurrent boolean  Kept for the existing callers; the
               inbox count already covers every indexed mail, so this never
               invents an index of its own.
        @param attempt number  Retry counter used by the delayed retry.
    ]]--
    local now = time()
    attempt = Number(attempt) or 1

    -- The current client reports the inbox size through GetInboxNumItems().
    -- There is no GetInboxInfo() to read it from; the failed lookup left the
    -- count at 0 and the old fallback scanned exactly one index, which is
    -- why a mailbox with several invoices produced a single one.
    local inboxCount = 0
    if type(GetInboxNumItems) == "function" then
        local ok, value = pcall(GetInboxNumItems)
        if ok and type(value) == "number" and value > 0 then
            inboxCount = math.floor(value)
        end
    end

    local report = {
        ok = true,
        error = nil,
        reason = tostring(reason or "manual"),
        mailbox_closed = inboxCount == 0,
        inbox_items = 0,      -- mails read in this scan
        invoices = 0,         -- auction house invoices found in those mails
        duplicates = 0,       -- invoices already stored by an earlier scan
        without_buyer = 0,    -- invoices with no buyer name
        invalid = 0,          -- invoices missing an item or a price
        secret = 0,           -- invoices the client refuses to let addons read
        pending_invoice = 0,  -- invoices whose cache was not populated yet
        stored = 0,           -- invoices written to SavedVariables by this scan
        retry_scheduled = false,
    }

    local processCount = math.min(inboxCount, MAX_INVOICES)
    report.inbox_items = processCount

    for index = 1, processCount do
        local isInvoice = false
        if type(GetInboxText) == "function" then
            local okText, _t1, _t2, _t3, _t4, flag = pcall(GetInboxText, index)
            if okText then
                isInvoice = flag == true
            end
        end

        local header = {}
        if type(GetInboxHeaderInfo) == "function" then
            local okHeader, values = pcall(function() return { GetInboxHeaderInfo(index) } end)
            if okHeader and type(values) == "table" then
                header = values
            end
        end

        local invoice = {}
        if type(GetInboxInvoiceInfo) == "function" then
            local okInvoice, values = pcall(function() return { GetInboxInvoiceInfo(index) } end)
            if okInvoice and type(values) == "table" then
                invoice = values
            end
        end

        local invoiceType = invoice[1]
        local restricted = IsRestricted(invoiceType)
            or IsRestricted(invoice[2]) or IsRestricted(invoice[3])
            or IsRestricted(invoice[4]) or IsRestricted(invoice[5])
            or IsRestricted(invoice[6]) or IsRestricted(invoice[7])
            or IsRestricted(invoice[11])

        local hasInvoiceData = false
        if not restricted then
            hasInvoiceData = type(invoiceType) == "string" and invoiceType ~= ""
        end

        if restricted then
            -- A restricted field cannot be read, not even compared.  The
            -- mail is not evidence, so skip it instead of raising.
            report.secret = report.secret + 1
        elseif not hasInvoiceData and not isInvoice then
            -- Expired/cancelled auction or a plain letter: no buyer here.
        elseif not hasInvoiceData then
            -- The inbox lists an invoice before its cache is populated.
            report.pending_invoice = report.pending_invoice + 1
        else
            local trimmedName = Trim(invoice[2])
            local trimmedPlayer = Trim(invoice[3])
            local valueCopper = FirstPositive(invoice[4], invoice[5], invoice[6])
            local quantity = Number(invoice[11])
            if not quantity or quantity < 1 then
                -- Some clients leave the invoice count empty and put the
                -- stack size on the attachment instead.
                quantity = AttachmentCount(index) or 1
            end
            local itemLink = InvoiceItemLink(index)
            local daysLeft = Number(header[7])

            local estimatedSaleAt = now
            if daysLeft and daysLeft > 0 and daysLeft <= AH_MAIL_LIFETIME_DAYS then
                estimatedSaleAt = now
                    - math.floor((AH_MAIL_LIFETIME_DAYS - daysLeft) * 86400)
            end

            if trimmedPlayer == "" then
                report.without_buyer = report.without_buyer + 1
            elseif trimmedName == "" or valueCopper <= 0 then
                report.invalid = report.invalid + 1
            else
                local record = {
                    seenAt = now,
                    estimatedSaleAt = estimatedSaleAt,
                    daysLeft = daysLeft or 0,
                    realm = GetRealmName(),
                    character = UnitName("player"),
                    invoiceType = tostring(invoiceType),
                    itemId = tonumber(itemLink and itemLink:match("item:(%d+)")),
                    itemName = trimmedName,
                    playerName = trimmedPlayer,
                    quantity = quantity,
                    valueCopper = valueCopper,
                    buyoutCopper = Number(invoice[5]) or 0,
                    depositCopper = Number(invoice[6]) or 0,
                    consignmentCopper = Number(invoice[7]) or 0,
                    subject = Trim(header[4]),
                    sender = Trim(header[3]),
                    itemLink = itemLink,
                    source = "inbox_scan",
                    confidence = "invoice",
                }

                report.invoices = report.invoices + 1
                local key = MailKey(record)
                if Mail:IndexHas(key) then
                    report.duplicates = report.duplicates + 1
                    if DFA_AI.debug and DFA_AI.debug.enabled then
                        print(DFA_AI.L.PREFIX .. ("mail debug: index=%d dup=true name=%s"):format(
                            index, tostring(trimmedName)))
                    end
                else
                    local appended, why = Mail:Append(record)
                    if appended then
                        report.stored = report.stored + 1
                        if DFA_AI.debug and DFA_AI.debug.enabled then
                            print(DFA_AI.L.PREFIX .. ("mail debug: index=%d stored=true name=%s value=%d"):format(
                                index, tostring(trimmedName), valueCopper))
                        end
                    elseif why == "duplicate" then
                        report.duplicates = report.duplicates + 1
                    else
                        report.invalid = report.invalid + 1
                    end
                end
            end
        end
    end

    -- A non-zero inbox whose invoices have not been cached yet is not an
    -- empty mailbox.  Retry a bounded number of times instead of losing the
    -- buyers until the player opens the folder by hand.
    if report.pending_invoice > 0 and attempt < MAX_RETRIES
            and type(C_Timer) == "table" and type(C_Timer.After) == "function" then
        report.retry_scheduled = true
        C_Timer.After(1, function()
            if DFA_AI.Mail and DFA_AI.Mail.ScanInbox then
                DFA_AI.Mail:ScanInbox(report.reason, includeCurrent, attempt + 1)
            end
        end)
    end

    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) == "table" then
        db.mail_stats = db.mail_stats or {}
        local stats = db.mail_stats
        stats.total = (type(db.mail_invoices) == "table" and #db.mail_invoices) or 0
        stats.last_scan = now
        stats.last_reason = report.reason
        stats.last_inbox_items = report.inbox_items
        stats.last_invoices = report.invoices
        stats.last_stored = report.stored
        stats.last_duplicates = report.duplicates
        stats.last_secret = report.secret
    end

    -- One line, one scan: inbox = mails read, faturas = invoices found,
    -- guardadas = invoices written now, duplicadas = already stored.
    print(string.format(
        "[DFA AI] mail: inbox=%d faturas=%d guardadas=%d duplicadas=%d sem_comprador=%d",
        report.inbox_items, report.invoices, report.stored,
        report.duplicates, report.without_buyer))

    return report
end

-- Shortcut: open the mailbox manually once (or whenever a mail arrives),
-- run `/dfaai mail`, and leave auto-scan off.  That reads every invoice
-- without touching Blizzard's mailbox frames from an addon.
function Mail:IsAutoScanEnabled()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    return false  -- always off; the manual `/dfaai mail` path is the supported one
end

function Mail:SetAutoScan(enabled)
    local db = DFA_AUCTION_INTELLIGENCE_DB
    if type(db) ~= "table" then
        return false
    end
    if enabled then
        print("[DFA_AuctionIntelligence] Mail auto-scan is intentionally off.")
        print("[DFA_AuctionIntelligence] Listening for MAIL_SHOW / MAIL_INBOX_UPDATE")
        print("[DFA_AuctionIntelligence] and reading the inbox from there makes this")
        print("[DFA_AuctionIntelligence] client's mailbox answer")
        print('[DFA_AuctionIntelligence] "action blocked by Blizzard" and stop working')
        print("[DFA_AuctionIntelligence] until auto-scan is disabled again.")
        print("[DFA_AuctionIntelligence] Re-enabling it was blocked; auto-scan stays off.")
        print("[DFA_AuctionIntelligence] Instead: open the mailbox manually once and run")
        print("[DFA_AuctionIntelligence] `/dfaai mail` - that reads every invoice without")
        print("[DFA_AuctionIntelligence] touching Blizzard's mailbox frames from an addon.")
        return false
    end
    -- turning it off is a no-op because it is never on, but keep the DB in sync
    db.mail_autoscan = false
    return false
end

---The mailbox's own events.  Auto-scan is intentionally always off, so this
---never triggers an inbox read from MAIL_SHOW / MAIL_INBOX_UPDATE.  The only
---way invoices get read is the manual `/dfaai mail` path.
function Mail:OnMailboxEvent(reason)
    return nil
end

function Mail:GetStats()
    local db = DFA_AUCTION_INTELLIGENCE_DB
    local stats = db and db.mail_stats or {}
    return {
        total = (db and db.mail_invoices and #db.mail_invoices) or 0,
        duplicates = stats.duplicates or 0,
        by_type = stats.by_type or {},
        last_capture = stats.last_capture,
        last_scan = stats.last_scan,
        last_reason = stats.last_reason,
        last_inbox_items = stats.last_inbox_items,
        last_invoices = stats.last_invoices,
        last_stored = stats.last_stored,
        autoscan = self:IsAutoScanEnabled(),
    }
end

-- Chat command handler -------------------------------------------------------

function DFA_AI.HandleCommand(msg)
    local cmd, rest = strsplit(" ", msg, 2)
    cmd = cmd and cmd:trim() or ""
    rest = rest and rest:trim() or ""

    if cmd == "mail" then
        if rest == "auto" or rest == "auto on" then
            print("[DFA_AuctionIntelligence] Mail auto-scan is permanently disabled.")
            print("[DFA_AuctionIntelligence] Use `/dfaai mail` to scan manually.")
        elseif rest == "auto off" then
            -- already off, just confirm
        else
            -- Default: manual scan
            local report = DFA_AI.Mail:ScanInbox("manual", true)
            if not report.ok then
                print("[DFA_AuctionIntelligence] Mail scan failed: " .. tostring(report.error))
            else
                print(string.format("[DFA_AuctionIntelligence] Mail: inbox=%d invoices=%d stored=%d duplicates=%d without buyer=%d",
                    report.inbox_items or 0, report.invoices or 0,
                    report.stored or 0, report.duplicates or 0, report.without_buyer or 0))
                DFA_AI.Export:GenerateExport()
            end
        end
    elseif cmd == "auctionator" then
        -- DFA Estimate Auctionator bridge commands
        if rest == "fill" then
            DFA_Estimate:FillPrice()
        elseif rest == "debug" then
            local enabled = rest == "debug on" or rest == "debug true"
            DFA_Estimate.debug = DFA_Estimate.debug or {}
            DFA_Estimate.debug.enabled = enabled
            print("[DFA_Estimate] Debug " .. (enabled and "enabled" or "disabled"))
        end
    end
end

-- End of Mail module -------------------------------------------------------