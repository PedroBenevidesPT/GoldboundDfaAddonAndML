-- DFA Auction Intelligence - Event Normalization, Provenance & Native Capture
local _, addon = ...
DFA_AI = DFA_AI or {}
DFA_AI.AuctionEvents = {}
local AuctionEvents = DFA_AI.AuctionEvents

local function ExtractItemID(itemLink)
    if not itemLink or type(itemLink) ~= "string" then return nil end
    local itemId = itemLink:match("item:(%d+)")
    if itemId then return tonumber(itemId) end
    local petId = itemLink:match("battlepet:(%d+)")
    if petId then return tonumber(petId) end
    return nil
end

-- Journalator's invoices carry no item link, only a name; ask the client for
-- the id so a sale can still be priced and joined to a listing.
local function ResolveItemIDFromName(itemName)
    if not itemName or itemName == "" then return nil end
    if C_Item and C_Item.GetItemIDForItemInfo then
        local ok, id = pcall(C_Item.GetItemIDForItemInfo, itemName)
        if ok and type(id) == "number" and id > 0 then return id end
    end
    if C_Item and C_Item.GetItemInfo then
        local ok, _, link = pcall(C_Item.GetItemInfo, itemName)
        if ok and link then return ExtractItemID(link) end
    end
    return nil
end

-- TSM_API.GetCustomPriceValue only accepts a TSM item string ("i:12345"):
-- it validates with `^[ip]:%d+` and *errors* on a hyperlink, and the pcall here
-- turned that into a silent nil -- which is why every listing carried
-- marketRefCopper = 0 even with TSM installed.
local function GetTSMReference(itemId, itemLink)
    if not (TSM_API and TSM_API.GetCustomPriceValue) then return nil end
    local id = itemId or ExtractItemID(itemLink)
    if not id then return nil end
    local ok, val = pcall(TSM_API.GetCustomPriceValue, "dbmarket", "i:" .. id)
    if ok and type(val) == "number" and val > 0 then
        return math.floor(val)
    end
    return nil
end

local function GetCurrentContext()
    return {
        realm = GetRealmName() or "UnknownRealm",
        character = UnitName("player") or "UnknownPlayer",
        faction = UnitFactionGroup("player") or "Neutral",
    }
end

function AuctionEvents:NormalizeInvoice(invoice, sourceTag)
    local ctx = invoice.source or GetCurrentContext()
    local itemLink = invoice.itemLink
    local itemId = ExtractItemID(itemLink) or ResolveItemIDFromName(invoice.itemName)
    local qty = math.max(1, tonumber(invoice.count) or 1)
    local totalVal = math.max(0, tonumber(invoice.value) or 0)
    local unitPrice = math.floor(totalVal / qty)
    local deposit = math.max(0, tonumber(invoice.deposit) or 0)
    local consignment = math.max(0, tonumber(invoice.consignment) or 0)
    local ts = tonumber(invoice.time) or time()
    local invoiceType = invoice.invoiceType or "seller"
    local eventType = (invoiceType == "buyer") and "purchase" or "sale"

    local net = 0
    if eventType == "sale" then
        net = totalVal - consignment + deposit
    else
        net = -totalVal
    end

    local tsmRef = GetTSMReference(itemId, itemLink)

    return {
        eventType = eventType,
        timestamp = ts,
        itemId = itemId,
        itemName = invoice.itemName or (itemLink and itemLink:match("%[(.-)%]")) or "Unknown Item",
        itemLink = itemLink,
        quantity = qty,
        unitPriceCopper = unitPrice,
        totalPriceCopper = totalVal,
        depositCopper = deposit,
        consignmentFeeCopper = consignment,
        netProceedsCopper = net,
        realm = ctx.realm or GetRealmName(),
        character = ctx.character or UnitName("player"),
        faction = ctx.faction or "Neutral",
        counterparty = invoice.playerName or "",
        source = sourceTag or "journalator",
        provenance = "client_event_inbox",
        confidence = "verified",
        marketRefCopper = tsmRef,
        raw = {
            invoiceType = invoiceType,
            deposit = deposit,
            consignment = consignment,
            value = totalVal,
            count = qty,
        }
    }
end

function AuctionEvents:NormalizePosting(post, sourceTag)
    local ctx = post.source or GetCurrentContext()
    local itemLink = post.itemLink
    local itemId = ExtractItemID(itemLink)
    local qty = math.max(1, tonumber(post.count) or 1)
    local buyout = math.max(0, tonumber(post.buyout) or 0)
    local bid = tonumber(post.bid) or 0
    local deposit = math.max(0, tonumber(post.deposit) or 0)
    local ts = tonumber(post.time) or time()

    local totalPrice = buyout * qty
    local tsmRef = GetTSMReference(itemId, itemLink)

    return {
        eventType = "listing",
        timestamp = ts,
        itemId = itemId,
        itemName = post.itemName or (itemLink and itemLink:match("%[(.-)%]")) or "Unknown Item",
        itemLink = itemLink,
        quantity = qty,
        unitPriceCopper = buyout,
        totalPriceCopper = totalPrice,
        depositCopper = deposit,
        consignmentFeeCopper = 0,
        netProceedsCopper = 0,
        realm = ctx.realm or GetRealmName(),
        character = ctx.character or UnitName("player"),
        faction = ctx.faction or "Neutral",
        counterparty = "",
        source = sourceTag or "journalator",
        provenance = "client_event_auction_post",
        confidence = "verified",
        marketRefCopper = tsmRef,
        raw = {
            buyout = buyout,
            bid = bid,
            deposit = deposit,
            count = qty,
        }
    }
end

function AuctionEvents:NormalizeFailure(failure, sourceTag)
    local ctx = failure.source or GetCurrentContext()
    local itemLink = failure.itemLink
    local itemId = ExtractItemID(itemLink) or ResolveItemIDFromName(failure.itemName)
    local qty = math.max(1, tonumber(failure.count) or 1)
    local failedType = failure.failedType or "expired"
    local eventType = (failedType == "cancelled") and "cancel" or "expire"
    local ts = tonumber(failure.time) or time()

    local tsmRef = GetTSMReference(itemId, itemLink)

    return {
        eventType = eventType,
        timestamp = ts,
        itemId = itemId,
        itemName = failure.itemName or (itemLink and itemLink:match("%[(.-)%]")) or "Unknown Item",
        itemLink = itemLink,
        quantity = qty,
        unitPriceCopper = 0,
        totalPriceCopper = 0,
        depositCopper = 0,
        consignmentFeeCopper = 0,
        netProceedsCopper = 0,
        realm = ctx.realm or GetRealmName(),
        character = ctx.character or UnitName("player"),
        faction = ctx.faction or "Neutral",
        counterparty = "",
        source = sourceTag or "journalator",
        provenance = "client_event_inbox_failure",
        confidence = "verified",
        marketRefCopper = tsmRef,
        raw = {
            failedType = failedType,
            count = qty,
        }
    }
end

-- Native Blizzard fallback listeners (active when Journalator is absent or disabled)
local nativeEnabled = false
local frame = CreateFrame("Frame", "DFA_AI_AuctionEventsFrame")

function AuctionEvents:EnableNativeCapture()
    if nativeEnabled then return end
    nativeEnabled = true

    -- Hook AH posting
    if C_AuctionHouse and C_AuctionHouse.PostItem then
        hooksecurefunc(C_AuctionHouse, "PostItem", function(location, duration, quantity, bid, buyout)
            local link = C_Container.GetContainerItemInfo(location:GetBagAndSlot()).hyperlink
            local deposit = C_AuctionHouse.CalculateItemDeposit(location, duration, quantity) or 0
            local post = {
                itemName = C_Item.GetItemName(location),
                buyout = buyout or 0,
                bid = bid or 0,
                count = quantity or 1,
                deposit = deposit,
                time = time(),
                itemLink = link,
                source = GetCurrentContext(),
            }
            local ev = AuctionEvents:NormalizePosting(post, "native_blizzard")
            DFA_AI.DataStore:StoreEvent(ev)
        end)
    end

    if C_AuctionHouse and C_AuctionHouse.PostCommodity then
        hooksecurefunc(C_AuctionHouse, "PostCommodity", function(location, duration, quantity, unitPrice)
            local link = C_Container.GetContainerItemInfo(location:GetBagAndSlot()).hyperlink
            local itemID = C_Item.GetItemInfoInstant(link)
            local deposit = C_AuctionHouse.CalculateCommodityDeposit(itemID, duration, quantity) or 0
            local post = {
                itemName = C_Item.GetItemName(location),
                buyout = unitPrice or 0,
                bid = 0,
                count = quantity or 1,
                deposit = deposit,
                time = time(),
                itemLink = link,
                source = GetCurrentContext(),
            }
            local ev = AuctionEvents:NormalizePosting(post, "native_blizzard")
            DFA_AI.DataStore:StoreEvent(ev)
        end)
    end
end
