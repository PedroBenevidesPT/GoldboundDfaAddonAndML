"""Run the inventory and seller modules in a real Lua interpreter.

The existing ``auction_intel_harness.py`` proves the journal sync.  This
one proves the two new capture paths:

1. an inventory snapshot reads bags and posted auctions;
2. it marks the bank unreadable instead of writing zeros;
3. a tooltip ``Seller: Name-Realm`` becomes one observation, with its
   raw line kept;
4. a tooltip without a seller line stores nothing;
5. the player's own name is not stored as a competitor;
6. the export carries ``inventory_json`` and ``sellers_jsonl`` that the
   Python parsers can read.

    python scripts/intel_addon_harness.py [addon-dir]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from lupa import LuaError, LuaRuntime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.auction_intel_harness import MOCK  # noqa: E402

ADDON = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "DFA_AuctionIntelligence")
TOC = ADDON / "DFA_AuctionIntelligence.toc"

CAPTURE_MOCK = r"""
-- ------------------------------------------------------- inventory / sellers
C_Container = {
    GetContainerNumSlots = function(bag)
        if bag == 0 then return 2 end
        if bag == 20 then return 1 end   -- character bank tab
        if bag == 21 then return 1 end   -- warband tab
        return 0  -- not at the bank: unknown, not empty
    end,
    GetContainerItemInfo = function(bag, slot)
        if bag == 20 and slot == 1 then
            return { itemID = 3001, itemName = "Bank Thing", stackCount = 3 }
        elseif bag == 21 and slot == 1 then
            return { itemID = 3002, itemName = "Warband Thing", stackCount = 9 }
        elseif bag ~= 0 then
            return nil
        end
        if slot == 1 then
            return { itemID = 1001, itemName = "Thing", stackCount = 5,
                     hyperlink = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r" }
        elseif slot == 2 then
            return { itemID = 1002, itemName = "Other", stackCount = 2 }
        end
        return nil
    end,
}

-- Bank tabs changed in 11.2: purchased tab *containers* are the truth.
-- They are empty until the bank/warband is opened, exactly like the client.
local characterBankOpen, warbandOpen = false, false
function SetCharacterBankOpen(value) characterBankOpen = value end
function SetWarbandOpen(value) warbandOpen = value end
Enum = Enum or {}
Enum.BankType = { Character = 1, Account = 2, Guild = 3 }
C_Bank = {
    FetchPurchasedBankTabIDs = function(kind)
        if kind == 1 and characterBankOpen then return { 20 } end
        if kind == 2 and warbandOpen then return { 21 } end
        return {}
    end,
}
C_AuctionHouse = {
    PostItem = function() end,
    PostCommodity = function() end,
    GetNumOwnedAuctions = function() return 1 end,
    GetOwnedAuctionInfo = function(index)
        return {
            auctionID = 6001, quantity = 4, buyoutAmount = 40000,
            itemKey = { itemID = 1003, itemLevel = 0 }, bidder = nil,
            itemLink = "|cff9d9d9d|Hitem:1003:::::::|h[Posted]|h|r",
            searchName = "Posted", timeLeft = 7200,
        }
    end,
    QueryOwnedAuctions = function(sorts)
        NativeOwnedQueries = (NativeOwnedQueries or 0) + 1
    end,
    SendSearchQuery = function(key, sorts, split)
        NativeSearches = (NativeSearches or 0) + 1
        NativeLastSorts = sorts
        NativeCurrentSearchKey = key
    end,
}

GameTooltip = {
    regions = {},
    scripts = {},
    HookScript = function(self, event, fn) self.scripts[event] = fn end,
    GetRegions = function(self) return table.unpack(self.regions) end,
    GetItem = function(self) return "Thing", "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r" end,
    SetRegions = function(self, regions) self.regions = regions end,
}

function SetSellerTooltip(line)
    GameTooltip:SetRegions({ { GetText = function() return line end } })
end

function ReadStat(stat, key)
    return stat[key]
end

-- Timers are queued, not immediate: the undercut scan waits for the
-- results event, and a mock that fired the timeout first would hide that.
C_Timer = { queue = {} }
function C_Timer.After(delay, fn) table.insert(C_Timer.queue, fn) end
function RunTimers()
    local guard = 0
    while #C_Timer.queue > 0 and guard < 50 do
        local pending = C_Timer.queue
        C_Timer.queue = {}
        for _, fn in ipairs(pending) do fn() end
        guard = guard + 1
    end
end

-- ------------------------------------------------------- guild bank
local guildOpen = false
function SetGuildBankOpen(value) guildOpen = value end
function GetNumGuildBankTabs()
    if not guildOpen then return 0 end
    return 1
end
function GetGuildBankTabInfo(tab)
    return "Tab 1", 1, true, true, 0, 0
end
function GetGuildBankItemInfo(tab, slot)
    if tab == 1 and slot == 1 then
        return 1, 7, false, false, 2, 2001, false,
            "|cff9d9d9d|Hitem:2001:::::::|h[Guild Thing]|h|r"
    end
    return nil
end

-- ------------------------------------------------------- Auctionator scan
AuctionatorUndercutScanMixin = {}
function AuctionatorUndercutScanMixin:StartScan() end
function AuctionatorUndercutScanMixin:ProcessSearchResults(auctionInfo) end
function AuctionatorUndercutScanMixin:ProcessUndercutResult(auctionInfo) end

-- The hook must check for StartOpening, not for a particular container type.
-- Use a userdata proxy here so a `type(button) == "table"` regression fails.
OpenAllMail = io.tmpfile()
debug.setmetatable(OpenAllMail, {
    __index = {
        StartOpening = function(self)
            OpenAllMailCalls = (OpenAllMailCalls or 0) + 1
        end,
    },
})

Auctionator = {
    Selling = {},
    Constants = {
        CommodityResultsSorts = { sortOrder = 0, reverseSort = false },
        ItemResultsSorts = { sortOrder = 4, reverseSort = false },
    },
}
function Auctionator.Selling.DoesItemMatchFromKey(ownKey, ownLink, key, link)
    return true
end

function RejectItemLevelFive()
    Auctionator.Selling.DoesItemMatchFromKey = function(ownKey, ownLink, key, link)
        return not (key and key.itemLevel == 5)
    end
end

local itemResults = {}
local commodityResults = {}
local currentOwned = nil
local nativeOwnedCommodity = nil

function C_AuctionHouse.GetNumItemSearchResults(key) return #itemResults end
function C_AuctionHouse.GetItemSearchResultInfo(key, index) return itemResults[index] end
function C_AuctionHouse.GetNumCommoditySearchResults(itemID)
    return #commodityResults
end
function C_AuctionHouse.GetCommoditySearchResultInfo(itemID, index)
    return commodityResults[index]
end
function C_AuctionHouse.GetItemKeyInfo(key)
    if key and key.itemID == 1004 then
        return { isCommodity = true, itemName = "Commodity Thing" }
    end
    return { isCommodity = false, itemName = "Thing" }
end

function BeginUndercutScan() itemResults = {}; commodityResults = {} end

function SetOwnedAuction(id, price, quantity)
    currentOwned = {
        auctionID = id, quantity = quantity or 1, buyoutAmount = price,
        itemKey = { itemID = 1001 },
        itemLink = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
        searchName = "Thing", timeLeft = 3600,
    }
    AuctionatorUndercutScanMixin.currentAuction = currentOwned
    AuctionatorUndercutScanMixin.expectedItemKey = currentOwned.itemKey
end

function SetOwnedCommodity(id, unitPrice, quantity)
    currentOwned = {
        auctionID = id, quantity = quantity or 1,
        buyoutAmount = (unitPrice or 0) * (quantity or 1),
        itemKey = { itemID = 1004, itemLevel = 0, itemSuffix = 0,
                    battlePetSpeciesID = 0 },
        itemLink = "|cff9d9d9d|Hitem:1004:::::::|h[Commodity Thing]|h|r",
        searchName = "Commodity Thing", timeLeftSeconds = 3600,
    }
    AuctionatorUndercutScanMixin.currentAuction = currentOwned
    AuctionatorUndercutScanMixin.expectedItemKey = currentOwned.itemKey
end

function AddListing(seller, unitPrice, quantity, itemLevel)
    local qty = quantity or 1
    itemResults[#itemResults + 1] = {
        auctionID = 9000 + #itemResults, quantity = qty,
        buyoutAmount = unitPrice * qty, owners = { seller },
        timeLeft = 3600,
        itemKey = { itemID = 1001, itemLevel = itemLevel or 0 },
        itemLink = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
    }
end

function AddCommodityListing(seller, unitPrice, quantity)
    local qty = quantity or 1
    commodityResults[#commodityResults + 1] = {
        auctionID = 9500 + #commodityResults, quantity = qty,
        unitPrice = unitPrice, owners = { seller },
        timeLeftSeconds = 3600, itemID = 1004,
    }
end

-- Auctionator's scan, reduced to the two calls that matter here.
function RunSearchStep()
    AuctionatorUndercutScanMixin.ProcessSearchResults(
        AuctionatorUndercutScanMixin, currentOwned)
end

-- NextStep has already moved currentAuction on by the time our post-hook
-- runs.  Keep the correct auctionInfo argument in the call and move the field
-- to a decoy, so reading currentAuction would record the wrong auction id.
function RunCommoditySearchStep()
    local auction = currentOwned
    AuctionatorUndercutScanMixin.currentAuction = {
        auctionID = 9999, quantity = 1, buyoutAmount = 1,
        itemKey = { itemID = 9999 },
        itemLink = "|cff9d9d9d|Hitem:9999:::::::|h[Next Thing]|h|r",
        searchName = "Next Thing", timeLeft = 1,
    }
    AuctionatorUndercutScanMixin.ProcessSearchResults(
        AuctionatorUndercutScanMixin, auction)
end

function RunUndercutStep()
    AuctionatorUndercutScanMixin.ProcessUndercutResult(
        AuctionatorUndercutScanMixin, currentOwned)
end

-- Native scan uses GetOwnedAuctionInfo, not Auctionator's scan fields.
function SetNativeOwnedCommodity(id, unitPrice, quantity)
    nativeOwnedCommodity = {
        auctionID = id, quantity = quantity or 1,
        buyoutAmount = (unitPrice or 0) * (quantity or 1),
        itemKey = { itemID = 1004, itemLevel = 0, itemSuffix = 0,
                    battlePetSpeciesID = 0 },
        itemLink = "|cff9d9d9d|Hitem:1004:::::::|h[Commodity Thing]|h|r",
        status = 0, bidder = nil, timeLeftSeconds = 3600,
    }
    C_AuctionHouse.GetNumOwnedAuctions = function() return 1 end
    C_AuctionHouse.GetOwnedAuctionInfo = function(index)
        return nativeOwnedCommodity
    end
end

function hooksecurefunc(target, key, fn)
    local holder = target
    if type(target) == "userdata" then
        local mt = debug.getmetatable(target)
        holder = mt and mt.__index or nil
    end
    if type(holder) == "table" and type(holder[key]) == "function" then
        local original = holder[key]
        holder[key] = function(...)
            local result = original(...)
            fn(...)
            return result
        end
    end
end

-- ------------------------------------------------------- mail invoices
local unpackValues = table.unpack or unpack
local inbox = {
    {
        header = { n = 12, false, false, "Auction House",
                   "Auction successful: [Thing]", 0, 0, 29.5,
                   false, true, false, false, false },
        invoice = { "seller", "Thing", "Buyer-Silvermoon", 9000, 9000,
                    100, 450, 0, 0, 0, 2, 0 },
        link = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
    },
}

local inboxHidden = false
function SetInboxHidden(value) inboxHidden = value end
function GetInboxNumItems()
    if inboxHidden then return 0 end
    return #inbox
end
-- The 5th return is true for an invoice, even before its cache is filled.
function GetInboxText(index)
    local entry = inbox[index]
    if not entry or not entry.invoice then return "", "", "", "", false end
    return "", "", "", "", true
end
function GetInboxHeaderInfo(index)
    local entry = inbox[index]
    if not entry then return nil end
    local header = entry.header
    return unpackValues(header, 1, header.n)
end
function GetInboxInvoiceInfo(index)
    local entry = inbox[index]
    if not entry or not entry.invoice or entry.invoice[1] == nil then
        return nil
    end
    return unpackValues(entry.invoice, 1, 12)
end
function GetInboxInvoiceItemLink(index)
    local entry = inbox[index]
    return entry and entry.link or nil
end
function GetInboxItemLink(index, attachment)
    local entry = inbox[index]
    return entry and entry.link or nil
end
function GetInboxItem(index, attachment)
    local row = inbox[index]
    if row and row.attachment then
        return row.attachment.name, row.attachment.id, "Interface\\Icons\\INV_Misc_QuestionMark",
            row.attachment.count, 1, true
    end
    return nil
end
function AddSecretInvoice()
    -- the buyer is a value the client refuses to let addons read
    SECRETS["SecretBuyer-Silvermoon"] = true
    inbox[#inbox + 1] = {
        header = { n = 12, false, false, "Auction House",
                   "Auction successful: [Secret Thing]", 0, 0, 29.5,
                   false, true, false, false, false },
        invoice = { "seller", "Secret Thing", "SecretBuyer-Silvermoon",
                    6000, 6000, 100, 300, 0, 0, 0, 1, 0 },
        link = "|cff9d9d9d|Hitem:1003:::::::|h[Secret Thing]|h|r",
    }
end
function AddInboxInvoice(buyer)
    inbox[#inbox + 1] = {
        header = { n = 12, false, false, "Auction House",
                   "Auction successful: [Other]", 0, 0, 29.5,
                   false, true, false, false, false },
        invoice = { "seller", "Other", buyer, 5000, 5000, 100, 250,
                    0, 0, 0, 1, 0 },
        link = "|cff9d9d9d|Hitem:1002:::::::|h[Other]|h|r",
    }
end
function AddAttachmentCountInvoice(buyer)
    inbox[#inbox + 1] = {
        header = { n = 12, false, false, "Auction House",
                   "Auction successful: [Stack]", 0, 0, 29.5,
                   false, true, false, false, false },
        -- field 11 (count) is intentionally missing on this client
        invoice = { "seller", "Stack", buyer, 7000, 7000, 100, 350,
                    0, 0, 0 },
        link = "|cff9d9d9d|Hitem:1002:::::::|h[Stack]|h|r",
        attachment = { name = "Stack", id = 1002, count = 7 },
    }
end
function ReplaceInboxWithUncachedInvoice()
    inbox = {
        {
            header = { n = 12, false, false, "Auction House",
                       "Auction successful: [Thing]", 0, 0, 29.5,
                       false, true, false, false, false },
            invoice = { nil },
            link = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
        },
    }
end
function ReplaceUncachedInvoiceWithValid()
    inbox[1] = {
        header = { n = 12, false, false, "Auction House",
                   "Auction successful: [Thing]", 0, 0, 29.5,
                   false, true, false, false, false },
        invoice = { "seller", "Thing", "Buyer-Retry", 9000, 9000,
                    100, 450, 0, 0, 0, 2, 0 },
        link = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
    }
end

-- The same mail an hour earlier: "Sale Pending" still says
-- seller_temp_invoice, then the client rewrites it as a seller invoice.
function AddPendingCopyOfFirstMail()
    inbox[#inbox + 1] = {
        header = { n = 12, false, false, "Auction House",
                   "Sale Pending: [Thing]", 0, 0, 29.5,
                   false, false, false, false, false },
        invoice = { "seller_temp_invoice", "Thing", "Buyer-Silvermoon",
                    9000, 9000, 100, 450, 0, 0, 0, 2, 0 },
        link = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r",
    }
end
"""


def main() -> int:
    if not TOC.exists():
        print(f"no TOC at {TOC}", file=sys.stderr)
        return 2
    client = [line.strip() for line in TOC.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("##")]

    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(CAPTURE_MOCK)
    for name in client:
        lua.execute((ADDON / name).read_text(encoding="utf-8"))

    g = lua.globals()
    checks = []

    def capture():
        """Record returns one value on success and two on failure."""
        result = g.DFA_AI.Sellers.CaptureTooltip(
            g.DFA_AI.Sellers, g.GameTooltip, "manual")
        if isinstance(result, tuple):
            return bool(result[0]), (result[1] if len(result) > 1 else "")
        return bool(result), ""

    def fire(event, arg1="DFA_AuctionIntelligence"):
        frame = None
        for candidate in g.FRAMES.values():
            if event in dict(candidate.events).values():
                frame = candidate
                break
        if frame is None:
            raise RuntimeError(f"no frame registered for {event}")
        frame.scripts["OnEvent"](frame, event, arg1)

    try:
        fire("ADDON_LOADED")
        fire("PLAYER_LOGIN")

        db = g.DFA_AUCTION_INTELLIGENCE_DB
        latest = db.inventory_latest
        checks.append(("login writes an inventory snapshot", latest is not None, ""))
        locations = dict(latest.locations) if latest else {}
        bags = {(int(r.itemId), int(r.quantity)) for r in (locations.get("bags") or {}).values()}
        posted = {(int(r.itemId), int(r.quantity)) for r in (locations.get("posted") or {}).values()}
        checks.append(("bags are read", bags == {(1001, 5), (1002, 2)}, str(bags)))
        checks.append(("posted stock is read", posted == {(1003, 4)}, str(posted)))
        checks.append(("an unreadable bank is not written as empty",
                       bool(latest.available.bank) is False
                       and locations.get("bank") is None, ""))

        stats = g.DFA_AI.Inventory.GetStats(g.DFA_AI.Inventory)
        checks.append(("the stats say bank was unreadable", not stats.readable.bank, ""))

        # With the Auction House closed the owned list is unloaded and the
        # API answers 0.  That is unknown, not an empty shelf.
        lua.execute('C_AuctionHouse.HasFullOwnedAuctionResults = function() return false end')
        lua.execute('C_AuctionHouse.GetNumOwnedAuctions = function() return 0 end')
        unloaded = g.DFA_AI.Inventory.Snapshot(g.DFA_AI.Inventory, "unloaded")
        checks.append(("an unloaded owned-auction list is unknown, not empty",
                       unloaded.available.posted is False
                       and unloaded.locations.posted is None, ""))

        lua.execute('C_AuctionHouse.HasFullOwnedAuctionResults = function() return true end')
        completed = g.DFA_AI.Inventory.Snapshot(g.DFA_AI.Inventory, "completed")
        checks.append(("a completed empty owned list is recorded as empty",
                       completed.available.posted is True
                       and len(completed.locations.posted) == 0, ""))

        # --- guild bank and posted refresh ----------------------------------
        checks.append(("an unopened guild bank is unknown at login",
                       bool(latest.available.guildbank) is False
                       and latest.locations.guildbank is None, ""))

        lua.execute('SetGuildBankOpen(true)')
        # A modern client opens the guild bank through the interaction
        # manager, and the legacy GUILDBANKFRAME_OPENED is protected there:
        # registering it raises ADDON_ACTION_FORBIDDEN.  Core.lua therefore
        # picks the interaction-manager event whenever the client has one,
        # and this is the payload that opens the guild bank.
        guild_banker = int(lua.globals().Enum.PlayerInteractionType.GuildBanker)
        fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", guild_banker)
        lua.execute('RunTimers()')
        guild = db.inventory_latest
        guild_rows = dict(guild.locations.guildbank or {})
        checks.append(("opening the guild bank records its items",
                       guild.reason == "guildbank"
                       and guild.available.guildbank is True
                       and any(int(r.itemId) == 2001 and int(r.quantity) == 7
                               for r in guild_rows.values()),
                       str(guild.reason)))

        lua.execute('C_AuctionHouse.GetNumOwnedAuctions = function() return 1 end')
        fire("OWNED_AUCTIONS_UPDATED")
        lua.execute('RunTimers()')
        auctions = db.inventory_latest
        posted_rows = dict(auctions.locations.posted or {})
        checks.append(("an owned-auction update makes posted readable",
                       auctions.reason == "auctions"
                       and auctions.available.posted is True
                       and any(int(r.itemId) == 1003 and int(r.quantity) == 4
                               for r in posted_rows.values()),
                       str(auctions.reason)))
        listing = (auctions.postedListings or [None])[1]
        checks.append(("every posted listing carries its auction id and unit price",
                       listing is not None
                       and int(listing.auctionId) == 6001
                       and int(listing.itemId) == 1003
                       and int(listing.unitPriceCopper) == 10000
                       and int(listing.timeLeft) == 7200,
                       str(listing and listing.unitPriceCopper)))

        # --- character bank and warband bank (11.2 tab containers) ----------
        lua.execute('SetCharacterBankOpen(true)')
        bank = g.DFA_AI.Inventory.Snapshot(g.DFA_AI.Inventory, "bank")
        bank_rows = dict(bank.locations.bank or {})
        checks.append(("opening the character bank reads its purchased tabs",
                       bank.available.bank is True
                       and any(int(r.itemId) == 3001 and int(r.quantity) == 3
                               for r in bank_rows.values()),
                       str(len(bank_rows))))
        checks.append(("an unopened warband is unknown, not empty",
                       bool(bank.available.warband) is False
                       and bank.locations.warband is None, ""))
        lua.execute('SetWarbandOpen(true)')
        warband = g.DFA_AI.Inventory.Snapshot(g.DFA_AI.Inventory, "warband")
        warband_rows = dict(warband.locations.warband or {})
        checks.append(("the warband bank is read and kept apart",
                       warband.available.warband is True
                       and any(int(r.itemId) == 3002 and int(r.quantity) == 9
                               for r in warband_rows.values())
                       and not any(int(r.itemId) == 3002
                                   for r in dict(warband.locations.bank or {}).values()),
                       str(len(warband_rows))))

        # --- seller capture -------------------------------------------------
        lua.execute('SetSellerTooltip("Seller: Tavil-Sylvanas")')
        ok, why = capture()
        observations = db.seller_observations or {}
        checks.append(("a seller line is captured", bool(ok) and len(observations) == 1,
                       str(why)))
        if len(observations) == 1:
            obs = list(dict(observations).values())[0]
            checks.append(("the seller is split into name and realm",
                           obs.sellerName == "Tavil" and obs.sellerRealm == "Sylvanas",
                           f"{obs.sellerName}/{obs.sellerRealm}"))
            checks.append(("the raw line is kept",
                           obs.rawLine == "Seller: Tavil-Sylvanas", str(obs.rawLine)))

        lua.execute('SetSellerTooltip("Commodity: 20")')
        ok2, _ = capture()
        checks.append(("a tooltip without a seller line stores nothing",
                       not ok2 and len(db.seller_observations) == 1, ""))

        lua.execute('SetSellerTooltip("Seller: Bullbound-Dragonmaw")')
        ok3, why3 = capture()
        checks.append(("the player's own auction is not stored",
                       not ok3 and "own auction" in str(why3),
                       str(why3)))
        checks.append(("no duplicate observation was created",
                       len(db.seller_observations) == 1,
                       str(len(db.seller_observations))))

        # The client labels our own auction "You", not with the character
        # name; that line was being stored as a competitor.
        lua.execute('SetSellerTooltip("Seller: You")')
        ok4, why4 = capture()
        checks.append(("the literal You line is also an own auction",
                       not ok4 and "own auction" in str(why4),
                       str(why4)))
        checks.append(("You never reaches the observation store",
                       len(db.seller_observations) == 1,
                       str(len(db.seller_observations))))

        # --- mail invoices --------------------------------------------------
        report = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        invoices = db.mail_invoices or {}
        checks.append(("a mail invoice stores the buyer",
                       report.stored == 1 and len(invoices) == 1,
                       str(report.stored)))
        if len(invoices) == 1:
            row = list(dict(invoices).values())[0]
            checks.append(("the invoice keeps the item, quantity and value",
                           row.itemName == "Thing" and row.quantity == 2
                           and row.valueCopper == 9000,
                           f"{row.itemName}/{row.quantity}/{row.valueCopper}"))
        again = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        checks.append(("a repeated inbox scan does not duplicate",
                       again.duplicates == 1 and len(db.mail_invoices) == 1,
                       str(again.duplicates)))
        lua.execute('AddPendingCopyOfFirstMail()')
        pending = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        checks.append(("a pending invoice is the mail that becomes the seller one",
                       pending.duplicates >= 1 and len(db.mail_invoices) == 1,
                       str(pending.duplicates)))
        # A scan that runs before the inbox has loaded answers 0 items.
        # That must not throttle the scan MAIL_INBOX_UPDATE triggers.
        lua.execute('SetInboxHidden(true)')
        preload = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "mail_show", True)
        lua.execute('SetInboxHidden(false)')
        loaded = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "inbox_update", False)
        checks.append(("an empty answer before load does not throttle the next scan",
                       preload.inbox_items == 0 and loaded.skipped is None,
                       str(loaded.skipped)))
        lua.execute('AddSecretInvoice()')
        secret = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        checks.append(("a restricted invoice is skipped, not raised on",
                       (secret.secret or 0) >= 1
                       and len(db.mail_invoices) == 1,
                       str(secret.secret)))
        lua.execute('AddInboxInvoice("")')
        no_buyer = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        checks.append(("an invoice without a buyer is not stored",
                       no_buyer.without_buyer >= 1
                       and len(db.mail_invoices) == 1,
                       str(no_buyer.without_buyer)))

        lua.execute('AddAttachmentCountInvoice("Buyer-Stack")')
        counted = g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "manual", True)
        stack_row = next((row for row in dict(db.mail_invoices).values()
                          if row.playerName == "Buyer-Stack"), None)
        checks.append(("the attachment count is the quantity when the invoice has none",
                       counted.stored == 1 and stack_row is not None
                       and int(stack_row.quantity) == 7,
                       str(stack_row and stack_row.quantity)))
        checks.append(("the mail carries the attachment item id",
                       stack_row is not None and int(stack_row.itemId) == 1002,
                       str(stack_row and stack_row.itemId)))

        # An inbox update often arrives before the invoice cache.  The first
        # read must not throttle the retry that actually sees the buyer.
        lua.execute('ReplaceInboxWithUncachedInvoice()')
        uncached = g.DFA_AI.Mail.ScanInbox(
            g.DFA_AI.Mail, "inbox_update", False)
        checks.append(("a non-zero inbox with no invoices is retried",
                       uncached.inbox_items == 1 and uncached.invoices == 0,
                       str(uncached.invoices)))
        lua.execute('ReplaceUncachedInvoiceWithValid(); RunTimers()')
        retry_row = next((row for row in dict(db.mail_invoices).values()
                          if row.playerName == "Buyer-Retry"), None)
        checks.append(("the delayed retry stores the buyer",
                       retry_row is not None, str(retry_row is not None)))

        # --- undercut scan (Auctionator) ------------------------------------
        undercut = g.DFA_AI.Undercut
        checks.append(("the undercut hook attaches to Auctionator's scan",
                       bool(undercut.GetStats(undercut).hooked), ""))
        lua.execute('AuctionatorUndercutScanMixin.StartScan(AuctionatorUndercutScanMixin)')
        lua.execute('BeginUndercutScan(); SetOwnedAuction(501, 10000)')
        lua.execute('AddListing("player", 12000, 1)')
        lua.execute('AddListing("Rival-Stormscale", 8000, 1)')
        lua.execute('AddListing("Under-Stormscale", 7000, 2)')
        lua.execute('RunSearchStep(); RunUndercutStep()')
        rows = db.undercut_observations or {}
        checks.append(("one scan records the undercutting seller by name",
                       len(rows) == 1, str(len(rows))))
        if len(rows) == 1:
            row = list(dict(rows).values())[0]
            checks.append(("the lowest seller, price and gap are stored",
                           row.lowestSeller == "Under"
                           and int(row.lowestUnitPriceCopper) == 7000
                           and int(row.undercutCopper) == 3000
                           and int(row.sellersBelow) == 2
                           and int(row.itemsAhead) == 3,
                           f"{row.lowestSeller}/{row.lowestUnitPriceCopper}"
                           f"/{row.sellersBelow}"))

        # A second stack of the same item reuses Auctionator's cached results.
        lua.execute('SetOwnedAuction(502, 7500); RunUndercutStep()')
        checks.append(("a second own auction of the same item is its own row",
                       len(db.undercut_observations) == 2, ""))
        if len(db.undercut_observations) == 2:
            second = list(dict(db.undercut_observations).values())[1]
            checks.append(("its gap is computed against its own price",
                           int(second.undercutCopper) == 500
                           and int(second.sellersBelow) == 1,
                           f"{second.undercutCopper}/{second.sellersBelow}"))

        lua.execute('AuctionatorUndercutScanMixin.StartScan(AuctionatorUndercutScanMixin)')
        lua.execute('BeginUndercutScan(); SetOwnedAuction(503, 5000)')
        lua.execute('AddListing("Rival-Stormscale", 8000, 1); RunSearchStep()')
        stats = undercut.GetStats(undercut)
        checks.append(("a listing nobody undercuts is counted, not stored",
                       len(db.undercut_observations) == 2
                       and int(stats.not_undercut) == 1,
                       str(stats.not_undercut)))

        lua.execute('AuctionatorUndercutScanMixin.StartScan(AuctionatorUndercutScanMixin)')
        lua.execute('BeginUndercutScan(); SetOwnedAuction(504, 1000); RunSearchStep()')
        checks.append(("an item with no results is skipped without an error",
                       len(db.undercut_observations) == 2
                       and int(undercut.GetStats(undercut).errors) == 0, ""))

        # A cheaper listing of another item level must not look like an
        # undercut of the same item.
        lua.execute('AuctionatorUndercutScanMixin.StartScan(AuctionatorUndercutScanMixin)')
        lua.execute('RejectItemLevelFive(); BeginUndercutScan()')
        lua.execute('SetOwnedAuction(505, 9000)')
        lua.execute('AddListing("OtherLevel", 6000, 1, 5)')
        lua.execute('AddListing("Under-Stormscale", 8000, 1, 0)')
        lua.execute('RunSearchStep()')
        checks.append(("a listing of another item level is not an undercut",
                       len(db.undercut_observations) == 3,
                       str(len(db.undercut_observations))))
        third = next((row for row in dict(db.undercut_observations).values()
                      if int(row.ownAuctionId) == 505), None)
        checks.append(("the matched seller is the one that counts",
                       third is not None
                       and third.lowestSeller == "Under"
                       and int(third.lowestUnitPriceCopper) == 8000
                       and int(third.sellersBelow) == 1,
                       f"{third.lowestSeller}/{third.sellersBelow}" if third else "missing"))
        lua.execute('Auctionator.Selling.DoesItemMatchFromKey = function() return true end')

        # Commodities use a different result API and a unit price.  The hook
        # must read that API, and must use the auctionInfo argument rather than
        # currentAuction, which NextStep may already have moved on.
        lua.execute('AuctionatorUndercutScanMixin.StartScan(AuctionatorUndercutScanMixin)')
        lua.execute('BeginUndercutScan(); SetOwnedCommodity(506, 7000, 10)')
        lua.execute('AddCommodityListing("player", 900, 2)')
        lua.execute('AddCommodityListing("CommodityUnder-Stormscale", 500, 5)')
        lua.execute('RunCommoditySearchStep()')
        commodity_row = next((row for row in dict(db.undercut_observations).values()
                              if int(row.ownAuctionId) == 506), None)
        checks.append(("a commodity undercut is read from the commodity API",
                       commodity_row is not None
                       and commodity_row.lowestSeller == "CommodityUnder"
                       and int(commodity_row.lowestUnitPriceCopper) == 500
                       and int(commodity_row.itemsAhead) == 5,
                       f"{commodity_row.lowestSeller}/{commodity_row.lowestUnitPriceCopper}"
                       if commodity_row else "missing"))
        checks.append(("the hook records this auction, not the next one",
                       commodity_row is not None
                       and int(commodity_row.ownAuctionId) != 9999,
                       str(commodity_row.ownAuctionId) if commodity_row else "missing"))
        checks.append(("the commodity row names its item and source",
                       commodity_row is not None
                       and commodity_row.itemName == "Commodity Thing"
                       and commodity_row.source == "auctionator_undercut_scan",
                       f"{commodity_row.itemName}/{commodity_row.source}"
                       if commodity_row else "missing"))

        # --- our own scan (no Auctionator) ----------------------------------
        lua.execute('AddListing("NativeUnder-Stormscale", 3000, 1)')
        lua.execute('AddListing("player", 5000, 1)')
        started, why = undercut.StartScan(undercut)
        fire("OWNED_AUCTIONS_UPDATED")
        checks.append(("/dfaai undercut starts its own owned-auction query",
                       bool(started) and g.NativeOwnedQueries == 1,
                       str(why)))
        checks.append(("the scan asks the AH for the item's listings",
                       g.NativeSearches == 1, str(g.NativeSearches)))
        checks.append(("an item search uses the item sort order",
                       g.NativeLastSorts[1].sortOrder == 4,
                       str(g.NativeLastSorts[1].sortOrder)))
        fire("ITEM_SEARCH_RESULTS_UPDATED", lua.table(itemID=9999))
        checks.append(("a result for another item does not consume the scan",
                       bool(undercut.NativeState(undercut).awaitingResults),
                       str(undercut.NativeState(undercut).awaitingResults)))
        fire("ITEM_SEARCH_RESULTS_UPDATED", g.NativeCurrentSearchKey)
        lua.execute('RunTimers()')
        native_row = None
        for value in dict(db.undercut_observations or {}).values():
            if int(value.ownAuctionId) == 6001:
                native_row = value
        checks.append(("the Blizzard results carry the seller name",
                       native_row is not None
                       and native_row.lowestSeller == "NativeUnder",
                       str(native_row.lowestSeller) if native_row else "missing"))
        checks.append(("the scan ends itself after the last auction",
                       undercut.NativeState(undercut).scanning is False, ""))

        # The native scan used to skip every item with itemLevel == nil, which
        # is exactly what a commodity key looks like.
        lua.execute('BeginUndercutScan(); SetNativeOwnedCommodity(6002, 7000, 10)')
        lua.execute('AddCommodityListing("player", 900, 2)')
        lua.execute('AddCommodityListing("NativeCommodity-Stormscale", 500, 5)')
        started, why = undercut.StartScan(undercut)
        fire("OWNED_AUCTIONS_UPDATED")
        checks.append(("a commodity search uses the commodity sort order",
                       g.NativeLastSorts[1].sortOrder == 0,
                       str(g.NativeLastSorts[1].sortOrder)))
        fire("COMMODITY_SEARCH_RESULTS_UPDATED",
             g.NativeCurrentSearchKey.itemID)
        lua.execute('RunTimers()')
        native_commodity = next(
            (row for row in dict(db.undercut_observations or {}).values()
             if int(row.ownAuctionId) == 6002), None)
        checks.append(("the native scan reads commodity results",
                       bool(started) and native_commodity is not None
                       and native_commodity.lowestSeller == "NativeCommodity"
                       and int(native_commodity.lowestUnitPriceCopper) == 500,
                       str(why)))
        checks.append(("the native commodity row keeps the item name and source",
                       native_commodity is not None
                       and native_commodity.itemName == "Commodity Thing"
                       and native_commodity.source == "native_undercut_scan",
                       f"{native_commodity.itemName}/{native_commodity.source}"
                       if native_commodity else "missing"))

        # --- the HUD action queue ------------------------------------------
        hud = g.DFA_AI.HUD
        offered = hud.GetActions(hud)
        action_names = [offered[index] for index in range(1, len(offered) + 1)]
        checks.append(("the HUD offers the four in-game actions",
                       action_names == ["export", "mail", "snapshot", "undercut"],
                       str(action_names)))
        ok_snap, detail_snap = hud.RunAction(hud, "snapshot")
        checks.append(("the snapshot button takes a picture and says what it read",
                       bool(ok_snap) and "posted=" in str(detail_snap)
                       and len(db.actions) == 1,
                       str(detail_snap)))
        ok_export, _ = hud.RunAction(hud, "export")
        checks.append(("the export button writes the export",
                       bool(ok_export) and len(db.actions) == 2, ""))
        ok_bad, why_bad = hud.RunAction(hud, "nope")
        checks.append(("an unknown action is recorded as a failure",
                       not ok_bad and "unknown action" in str(why_bad)
                       and len(db.actions) == 3,
                       str(why_bad)))

        # --- Blizzard's mailbox -------------------------------------------
        # Reading the mailbox from Blizzard's own event/frame is what this
        # client answers with "action blocked by Blizzard", so the addon must
        # not touch it on its own.
        lua.execute('OpenAllMail.StartOpening(OpenAllMail)')
        checks.append(("Open All still runs Blizzard's own code",
                       g.OpenAllMailCalls == 1, str(g.OpenAllMailCalls)))
        checks.append(("Open All is not hooked by the addon",
                       g.DFA_AI.Mail.openAllHooked is None,
                       str(g.DFA_AI.Mail.openAllHooked)))
        # clear the scan throttle so a scan *would* run if one were attempted
        lua.execute('SetInboxHidden(true)')
        g.DFA_AI.Mail.ScanInbox(g.DFA_AI.Mail, "reset", True)
        lua.execute('SetInboxHidden(false)')
        before_reason = db.mail_stats.last_reason
        mailbox_event_handled = True
        try:
            fire("MAIL_SHOW")
        except RuntimeError:
            mailbox_event_handled = False
        lua.execute('RunTimers()')
        checks.append(("the addon does not handle MAIL_SHOW",
                       not mailbox_event_handled, str(mailbox_event_handled)))
        checks.append(("opening the mailbox does not read it",
                       db.mail_stats.last_reason == before_reason,
                       f"{before_reason} -> {db.mail_stats.last_reason}"))
        lua.execute('SlashCmdList["DFAAI"]("mail auto")')
        checks.append(("mail auto is refused and never enables a scan",
                       not db.mail_autoscan, str(db.mail_autoscan)))
        g.DFA_AI.Mail.SetAutoScan(g.DFA_AI.Mail, True)
        checks.append(("setting auto-scan on is refused",
                       not db.mail_autoscan, str(db.mail_autoscan)))

        fire("PLAYER_LOGOUT")
        export = g.DFA_AUCTION_INTELLIGENCE_EXPORT
        inventory = json.loads(export.inventory_json)
        checks.append(("the export carries parseable inventory JSON",
                       inventory["locations"]["bags"][0]["quantity"] in (5, 2)
                       and inventory["available"]["bank"] is True
                       and len(inventory.get("postedListings") or []) == 1, ""))
        lines = [line for line in str(export.sellers_jsonl).splitlines() if line.strip()]
        parsed = [json.loads(line) for line in lines]
        checks.append(("the export carries the seller JSONL",
                       len(parsed) == 1 and parsed[0]["seller"] == "Tavil-Sylvanas",
                       str(parsed)))
        checks.append(("the export keeps a Lua sellers table too",
                       len(export.sellers or {}) == 1, ""))
        mail_lines = [line for line in str(export.mail_jsonl).splitlines()
                      if line.strip()]
        mail_parsed = [json.loads(line) for line in mail_lines]
        checks.append(("the export carries the mail invoice JSONL",
                       len(mail_parsed) == 3
                       and mail_parsed[0]["playerName"] == "Buyer-Silvermoon"
                       and any(row["playerName"] == "Buyer-Stack"
                               and row["quantity"] == 7 for row in mail_parsed),
                       str(mail_parsed)))
        undercut_lines = [line for line in str(export.undercuts_jsonl).splitlines()
                          if line.strip()]
        undercut_parsed = [json.loads(line) for line in undercut_lines]
        checks.append(("the export carries the undercut JSONL with sellers",
                       len(undercut_parsed) == 6
                       and undercut_parsed[0]["lowestSeller"] == "Under"
                       and len(undercut_parsed[0]["sellers"]) == 2
                       and any(row["itemName"] == "Commodity Thing"
                               and row["source"] in ("auctionator_undercut_scan",
                                                     "native_undercut_scan")
                               for row in undercut_parsed),
                       str(len(undercut_parsed))))
        checks.append(("logout writes a second inventory snapshot",
                       len(db.inventory_snapshots) >= 2,
                       str(len(db.inventory_snapshots))))
        action_lines = [line for line in str(export.actions_jsonl).splitlines()
                        if line.strip()]
        action_parsed = [json.loads(line) for line in action_lines]
        checks.append(("the export carries the HUD actions for the daemon",
                       len(action_parsed) == 3
                       and action_parsed[0]["name"] == "snapshot"
                       and action_parsed[0]["ok"] is True
                       and action_parsed[2]["ok"] is False,
                       str(action_parsed)))
    except LuaError as exc:
        import traceback; traceback.print_exc()
        return 1
    except Exception as exc:  # pragma: no cover - harness diagnostics
        import traceback; traceback.print_exc()
        return 1

    failed = 0
    for label, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
