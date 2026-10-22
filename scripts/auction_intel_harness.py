"""Run DFA_AuctionIntelligence in a real Lua interpreter with a mocked client.

The question this answers is the one the addon exists for: *does it save
everything before the client goes away?*  The client writes SavedVariables
after PLAYER_LOGOUT, so whatever the addon does in that handler is what the
next session sees.  The harness therefore fires the real sequence --

    ADDON_LOADED -> PLAYER_LOGIN -> (auctions happen) -> PLAYER_LOGOUT

-- and then asserts on the tables the client would have written:

1. the login sync imports *every* Journalator section (invoices, postings,
   failures), not just the invoices;
2. an auction that Journalator archived **during** the session is picked up by
   the logout flush, not on the next login;
3. the export is rebuilt on logout and its ``total_events`` matches the DB;
4. a failing section is reported, not silently swallowed;
5. the slash command reports what the last sync read.

    python scripts/auction_intel_harness.py [addon-dir]

Exit status is 0 only when every check holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

from lupa import LuaError, LuaRuntime

ADDON = Path(sys.argv[1] if len(sys.argv) > 1 else "C:/FarmAnalysis/DFA_AuctionIntelligence")
TOC = ADDON / "DFA_AuctionIntelligence.toc"

MOCK = r"""
-- ---------------------------------------------------------------- the client
local printed = {}
PRINTED = printed

local function record(text)
    printed[#printed + 1] = tostring(text)
end

function print(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
    record(table.concat(parts, " "))
end

-- Newer clients mark restricted data as "secret": it looks like an
-- ordinary string or number, but any use raises.  These harnesses use this
-- to prove a restricted value is skipped instead of taking a handler down.
SECRETS = {}
function issecretvalue(value)
    return SECRETS[value] == true
end

FRAMES = {}
function CreateFrame(kind, name)
    local frame = {
        kind = kind, name = name, events = {}, scripts = {},
        RegisterEvent = function(self, event) self.events[#self.events + 1] = event end,
        UnregisterEvent = function() end,
        SetScript = function(self, script, fn) self.scripts[script] = fn end,
        GetScript = function(self, script) return self.scripts[script] end,
    }
    FRAMES[#FRAMES + 1] = frame
    return frame
end

NOW = 1792521600
function time() return NOW end
function date(fmt, ts) return tostring(ts) end
function GetRealmName() return "Dragonmaw" end
function UnitName(unit) return "Bullbound" end
function UnitFactionGroup(unit) return "Horde" end
function GetBuildInfo() return "12.0.5", "60000", "2026-10-01", 120005 end

HOOKED = {}
function hooksecurefunc(target, name, fn)
    target = target or _G
    if type(target[name]) ~= "function" then
        error(("attempt to hook a nonexistent function %s"):format(tostring(name)), 2)
    end
    HOOKED[#HOOKED + 1] = name
    -- the client replaces the field with a wrapper that calls the original *and*
    -- then the hook, returning the original's results.  Anything registered
    -- against the old function object is now stale -- which is exactly how
    -- hooking Journalator.AddToLogs broke its Auctionator EventBus source.
    local original = target[name]
    target[name] = function(...)
        local a, b, c, d = original(...)
        fn(...)
        return a, b, c, d
    end
end

SlashCmdList = {}
C_AddOns = { GetAddOnMetadata = function(addon, field) return "170" end }
C_Container = { GetContainerItemInfo = function() return { hyperlink = "|cff9d9d9d|Hitem:1001:::::::|h[Thing]|h|r" } end }
C_Item = {
    GetItemName = function() return "Thing" end,
    GetItemInfoInstant = function(link) return 1001 end,
    -- invoices carry no link: the id has to come from the name
    GetItemIDForItemInfo = function(name)
        if name == "Deepsea Sagefish" then return 13755 end
        return nil
    end,
}

-- TSM_API.GetCustomPriceValue, as TradeSkillMaster implements it: the item
-- argument must be a TSM item string ("i:12345"); a hyperlink raises, and a
-- pcall around it hides that as a silent nil.
TSM_DBMARKET = { [1008] = 12345, [13755] = 4200 }
TSM_API = {
    GetCustomPriceValue = function(customPriceStr, itemString)
        if type(itemString) ~= "string" or not string.match(itemString, "^[ip]:%d+") then
            error("Invalid 'itemString' argument type (must be a TSM item string): "
                .. tostring(itemString), 2)
        end
        if customPriceStr ~= "dbmarket" then return 0 end
        return TSM_DBMARKET[tonumber(string.match(itemString, "^[ip]:(%d+)"))] or 0
    end,
}

-- a Journalator whose archive grows during the session, exactly as the real
-- one does: the sections are read by name and the live hook feeds AddToLogs
ARCHIVE = {
    Invoices = {
        {
            invoiceType = "seller", itemName = "Deepsea Sagefish", playerName = "Buyer-A",
            value = 7600, count = 4, deposit = 100, consignment = 380,
            time = 1792447600, itemLink = nil,
        },
    },
    Posting = {
        {
            itemName = "Light Hide", itemLink = "|cff9d9d9d|Hitem:1008:::::::|h[Light Hide]|h|r",
            buyout = 29000, bid = 0, deposit = 100, count = 4, time = 1792457529,
        },
    },
    Failures = {
        { failedType = "expired", itemName = "Light Hide", count = 4,
          time = 1792457600, itemLink = nil },
    },
}
BROKEN_SECTIONS = {}

function REAL_GET_RANGE(dateFrom, section)
    if BROKEN_SECTIONS[section] then
        error("store is locked: " .. section)
    end
    local rows = {}
    for i, row in ipairs(ARCHIVE[section] or {}) do rows[i] = row end
    return rows
end

-- Auctionator's EventBus, as implemented in Auctionator 341: Fire() refuses a
-- source that was never registered.  This is what caught the addon replacing
-- Journalator.AddToLogs with hooksecurefunc: the new function object was not
-- the one Journalator had registered, so every archived sale raised
-- "All sources must be registered".
local EventBus = { sources = {}, listeners = {} }
function EventBus:RegisterSource(source, name)
    self.sources[source] = name
    return self
end
function EventBus:Register(listener, eventNames)
    for _, eventName in ipairs(eventNames) do
        self.listeners[eventName] = self.listeners[eventName] or {}
        table.insert(self.listeners[eventName], listener)
    end
    return self
end
function EventBus:Fire(source, eventName, ...)
    if self.sources[source] == nil then
        error("All sources must be registered (" .. eventName .. ")", 2)
    end
    for _, listener in ipairs(self.listeners[eventName] or {}) do
        listener:ReceiveEvent(eventName, ...)
    end
    return self
end
Auctionator = { EventBus = EventBus }

Journalator = {
    Events = { LogsUpdated = "journalator_logs_updated" },
    State = { Logs = {} },
    Archiving = { GetRange = REAL_GET_RANGE },
}

-- Journalator 171: registers the function object as the bus source, inserts the
-- rows into the live store, then fires through that same field.
function Journalator.AddToLogs(newEntries)
    for key, list in pairs(newEntries) do
        Journalator.State.Logs[key] = Journalator.State.Logs[key] or {}
        for _, value in ipairs(list) do
            table.insert(Journalator.State.Logs[key], value)
        end
    end
    Auctionator.EventBus:Fire(Journalator.AddToLogs, Journalator.Events.LogsUpdated, newEntries)
end
Auctionator.EventBus:RegisterSource(Journalator.AddToLogs, "Journalator.AddToLogs")

-- seed the live store the way Journalator.Archiving.SetState does at login
for section, rows in pairs(ARCHIVE) do
    local copy = {}
    for i, row in ipairs(rows) do copy[i] = row end
    Journalator.State.Logs[section] = copy
end

-- ------------------------------------------------------- kills, loot, copper
-- The client hands the combat log over through one function; the test sets
-- the tuple the client would return and fires the event.
Enum = { LootSlotType = { Item = 1, Money = 2, Currency = 3 } }

PLAYER_GUID = "Player-1465-0A1B2C3D"
PET_GUID = nil
MOB_GUID = "Creature-0-1465-2552-1234-224654-00004A2B1C"
MOB2_GUID = "Creature-0-1465-2552-1234-224654-00004A2B1D"
MOB_ITEM = 2589

function UnitGUID(unit)
    if unit == "player" then return PLAYER_GUID end
    if unit == "pet" then return PET_GUID end
    return nil
end

-- the tuple is positional: a table with holes loses everything after the
-- first nil under ``#``/``unpack``, which is not how the client returns it
CLEU = {}
function CombatLogGetCurrentEventInfo()
    local event = CLEU
    return 0, event[1] or "", false, event[2] or "", event[3] or "",
           0, 0, event[4] or "", event[5] or "", 0, 0
end

function SET_HIT(guid, name, sourceGuid)
    CLEU = { "SWING_DAMAGE", sourceGuid or PLAYER_GUID, "Bullbound", guid, name }
end

function SET_DIED(guid, name)
    CLEU = { "UNIT_DIED", "", "", guid, name }
end

function SET_PARTY_KILL(guid, name, killerGuid)
    CLEU = { "PARTY_KILL", killerGuid or PLAYER_GUID, "Bullbound", guid, name }
end

LOOT_ITEMS = {}
function LOOT_CLEAR() LOOT_ITEMS = {} end
function LOOT_ADD(guid, slotType, quantity, link)
    LOOT_ITEMS[#LOOT_ITEMS + 1] = { guid = guid, type = slotType,
                                    quantity = quantity, link = link }
end
function GetNumLootItems() return #LOOT_ITEMS end
function GetLootSourceInfo(slot) return LOOT_ITEMS[slot].guid, 1 end
function GetLootSlotType(slot) return LOOT_ITEMS[slot].type end
function GetLootSlotInfo(slot)
    local entry = LOOT_ITEMS[slot]
    return "Interface\\Icons\\INV_Misc_QuestionMark", "loot", entry.quantity or 0
end
function GetLootSlotLink(slot) return LOOT_ITEMS[slot].link end
function GetMoney() return 0 end

DFA_AUCTION_INTELLIGENCE_DB = nil
DFA_AUCTION_INTELLIGENCE_EXPORT = nil
"""


LIVE_SALE = r"""
    Journalator.AddToLogs({ Invoices = { {
        invoiceType = "seller", itemName = "Deepsea Sagefish", playerName = "Buyer-Live",
        value = 9000, count = 1, deposit = 0, consignment = 450, time = 1792458000,
    } } })
"""

# The Archivist tears its instance down on its own PLAYER_LOGOUT handler
# (``archive.sv = nil``), which can run before ours -- exactly what the saved
# sync log showed: every logout read failed with "attempt to index a nil
# value".  The row below is archived without going through the bus, so only
# the logout flush can save it.
SILENT_SALE_AND_TEARDOWN = r"""
    Journalator.Archiving.GetRange = function()
        error("Journalator/Embeds/Archivist/Archivist.lua:395: attempt to index a nil value", 2)
    end
    local row = {
        invoiceType = "seller", itemName = "Light Hide", playerName = "Buyer-Silent",
        value = 5000, count = 2, deposit = 0, consignment = 250, time = 1792458100,
    }
    table.insert(ARCHIVE.Invoices, row)
    table.insert(Journalator.State.Logs.Invoices, row)
"""


def main() -> int:
    if not TOC.exists():
        print(f"no TOC at {TOC}", file=sys.stderr)
        return 2

    client = [line.strip() for line in TOC.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("##")]

    run = LuaRuntime(unpack_returned_tuples=True)
    g = run.globals()
    run.execute(MOCK)

    def fire(event, arg1=None):
        frames = g.FRAMES
        for frame in frames.values():
            handler = frame.scripts["OnEvent"]
            if handler is not None and event in {e for e in frame.events.values()}:
                handler(frame, event, arg1)

    def slash(command):
        handler = g.SlashCmdList["DFAAI"]
        handler(command)

    try:
        for name in client:
            path = ADDON / name
            run.execute(path.read_text(encoding="utf-8"), name=str(path))
    except LuaError as exc:
        print(f"LOAD FAILED: {exc}", file=sys.stderr)
        return 1

    checks: list[tuple[str, bool, str]] = []
    MOB_ITEM = 2589
    try:
        fire("ADDON_LOADED", "DFA_AuctionIntelligence")
        fire("PLAYER_LOGIN")

        def count(table):
            return len(table) if table is not None else 0

        db = g.DFA_AUCTION_INTELLIGENCE_DB
        by_type = {}
        if db.events is not None:
            for event in db.events.values():
                by_type[event.eventType] = by_type.get(event.eventType, 0) + 1
        checks.append(("login imports the invoices", by_type.get("sale") == 1, str(by_type)))
        checks.append(("login imports the postings, not just the invoices",
                       by_type.get("listing") == 1, str(by_type)))
        checks.append(("login imports the failures",
                       by_type.get("expire") == 1, str(by_type)))
        listing_row = None
        sale_row = None
        if db.events is not None:
            for event in db.events.values():
                if event.eventType == "listing":
                    listing_row = event
                elif event.eventType == "sale":
                    sale_row = event
        checks.append(("a listing carries the market reference from TSM",
                       listing_row is not None and listing_row.marketRefCopper == 12345,
                       str(listing_row is not None and listing_row.marketRefCopper)))
        checks.append(("a sale without a link is priced by its item id",
                       sale_row is not None and sale_row.itemId == 13755
                       and sale_row.marketRefCopper == 4200,
                       f"id={sale_row.itemId if sale_row else None} "
                       f"ref={sale_row.marketRefCopper if sale_row else None}"))
        checks.append(("the login export is written",
                       g.DFA_AUCTION_INTELLIGENCE_EXPORT is not None
                       and g.DFA_AUCTION_INTELLIGENCE_EXPORT.total_events == count(db.events),
                       str(g.DFA_AUCTION_INTELLIGENCE_EXPORT.total_events)))

        # ---------------------------------------------- kills, loot, copper
        # one death, reported by UNIT_DIED and PARTY_KILL: one kill
        run.execute("SET_HIT(MOB_GUID, 'Bristleback Boar')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        run.execute("SET_DIED(MOB_GUID, 'Bristleback Boar')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        run.execute("SET_PARTY_KILL(MOB_GUID, 'Bristleback Boar')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        pending = db.kills.pending[224654]
        checks.append(("one death is one kill, however often it is reported",
                       pending is not None and pending.kills == 1,
                       str(pending.kills if pending else None)))

        # the pet is the killer: it still counts
        run.execute("PET_GUID = 'Pet-0-1465-2552-1234-12345-0000ABCDEF'")
        run.execute("SET_HIT(MOB2_GUID, 'Bristleback Scout', PET_GUID)")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        run.execute("SET_DIED(MOB2_GUID, 'Bristleback Scout')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        pet_row = db.kills.pending[224654]
        checks.append(("a kill landed by the pet counts",
                       pet_row is not None and pet_row.kills == 2,
                       str(pet_row.kills if pet_row else None)))

        # somebody else's kill: damage from a stranger, then the death
        run.execute("STRANGER = 'Creature-0-1465-2552-1234-224654-00004A2B1E'")
        run.execute("SET_HIT(STRANGER, 'Someone Else Mob', 'Player-1465-99999999')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        run.execute("SET_DIED(STRANGER, 'Someone Else Mob')")
        fire("COMBAT_LOG_EVENT_UNFILTERED")
        checks.append(("a mob this player never hit is not our kill",
                       db.kills.pending[224654].kills == 2
                       and db.kills.stats.kills == 2,
                       str(db.kills.stats.kills)))

        # the loot window: three cloth and 45 silver, on the corpse we killed
        run.execute("LOOT_CLEAR()")
        run.execute("LOOT_ADD(MOB_GUID, 1, 3, "
                    "'|cffffffff|Hitem:2589:::::::|h[Linen Cloth]|h|r')")
        run.execute("LOOT_ADD(MOB_GUID, 2, 4500, nil)")
        fire("LOOT_OPENED")
        fire("LOOT_CLOSED")
        loot_row = db.kills.pending[224654]
        checks.append(("loot is counted with its quantity and its copper",
                       loot_row is not None
                       and loot_row["items"][MOB_ITEM] == 3
                       and loot_row.copper == 4500 and loot_row.loots == 1,
                       f"items={loot_row['items'][MOB_ITEM] if loot_row else None} "
                       f"copper={loot_row.copper if loot_row else None}"))

        # reopening the same corpse for the leftovers is not a second drop
        fire("LOOT_OPENED")
        fire("LOOT_CLOSED")
        checks.append(("a reopened corpse is not a second drop",
                       loot_row.loots == 1 and loot_row.copper == 4500,
                       f"loots={loot_row.loots} copper={loot_row.copper}"))

        # a chest is a GameObject, not a mob, and is not stored
        run.execute("LOOT_CLEAR()")
        run.execute("LOOT_ADD('GameObject-0-1465-2552-1234-321654-00004A2B1F', 1, 1, "
                    "'|cffffffff|Hitem:2589:::::::|h[Linen Cloth]|h|r')")
        fire("LOOT_OPENED")
        fire("LOOT_CLOSED")
        checks.append(("a chest is not a mob kill", db.kills.stats.loots == 1,
                       str(db.kills.stats.loots)))

        # one export so far: the counters are pending, not yet a batch
        checks.append(("the pending counters are not yet exported",
                       count(db.kills.order) == 0, str(count(db.kills.order))))

        # a sale that happens after login: Journalator announces it on the bus
        before_live = count(db.events)
        bus_ok, bus_error = True, ""
        try:
            run.execute(LIVE_SALE)
        except LuaError as exc:
            bus_ok, bus_error = False, str(exc)
        live_added = count(db.events) - before_live
        checks.append(("Journalator's EventBus still accepts its own source",
                       bus_ok, bus_error))
        checks.append(("a live auction is captured exactly once",
                       live_added == 1, f"+{live_added}"))

        # a sale archived during the session, with the Archivist already gone
        run.execute(SILENT_SALE_AND_TEARDOWN)
        before = count(db.events)

        fire("PLAYER_LOGOUT")

        after = count(db.events)
        checks.append(("the logout flush captures what was archived during the session",
                       after == before + 1, f"{before} -> {after}"))
        fell_back = {value for value in (db.last_sync.fallback or {}).values()}
        checks.append(("logout says it read the live store after the teardown",
                       {"Invoices", "Posting", "Failures"} <= fell_back,
                       str(sorted(fell_back))))
        # put the archive back: the next checks are about a failing section
        run.execute("Journalator.Archiving.GetRange = REAL_GET_RANGE")
        exported = g.DFA_AUCTION_INTELLIGENCE_EXPORT
        checks.append(("the logout export is rebuilt and matches the DB",
                       exported is not None and exported.total_events == after,
                       f"export={exported.total_events} db={after}"))
        kill_rows = 0
        kill_json = ""
        if exported is not None and exported.kills_jsonl is not None:
            kill_json = exported.kills_jsonl
            for line in str(kill_json).splitlines():
                if line.strip():
                    kill_rows = kill_rows + 1
        checks.append(("the logout export carries the kill rows",
                       kill_rows == 1 and '"npcId":224654' in kill_json,
                       f"rows={kill_rows}"))
        checks.append(("the exported kill row carries kills, loot and copper",
                       '"kills":2' in kill_json and '"moneyCopper":4500' in kill_json
                       and '"batchId"' in kill_json,
                       kill_json[:120]))
        checks.append(("the export carries the sync report",
                       exported is not None and exported.last_sync is not None
                       and exported.last_sync.reason == "logout",
                       str(exported is not None and exported.last_sync is not None
                           and exported.last_sync.reason)))
        checks.append(("the sync report is persisted with the events",
                       db.last_sync is not None and db.last_sync.postings == 1
                       and db.last_sync.stored == 1,
                       f"read postings={db.last_sync.postings if db.last_sync else None} "
                       f"stored={db.last_sync.stored if db.last_sync else None}"))
        checks.append(("the report history is kept",
                       count(db.sync_log) == 2, str(count(db.sync_log))))

        # a section that cannot be read must be reported, not swallowed
        g.BROKEN_SECTIONS["Posting"] = True
        slash("status")
        slash("sync")
        last = db.last_sync
        reported = any("store is locked" in str(e) for e in last.errors.values())
        checks.append(("a failing section is reported", reported,
                       str([e for e in last.errors.values()])))
        checks.append(("a failing section does not stop the export",
                       g.DFA_AUCTION_INTELLIGENCE_EXPORT.total_events == count(db.events),
                       str(g.DFA_AUCTION_INTELLIGENCE_EXPORT.total_events)))

        printed = " || ".join(g.PRINTED.values())
        checks.append(("the slash command reports the last sync",
                       "Last sync:" in printed and "postings=" in printed, ""))
        checks.append(("the slash command names the capture path",
                       "capture: eventbus" in printed, printed[-160:]))
    except LuaError as exc:
        print(f"EVENT FAILED: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for label, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
