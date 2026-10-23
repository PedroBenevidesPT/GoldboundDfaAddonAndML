"""Run ZoneTimerRedux's gold calculation in a real Lua interpreter.

The addon used to know one number: the client's "you loot N gold" message.
It now also values the items a corpse gave, from the dataset the DfA pricing
engine ships.  That raises the questions this harness answers, in the order
they can go wrong:

1. is the market value the dataset's *net* money (after the 5 % cut) of the
   realm's own record, and the regional net price x the realm factor when
   there is no record -- and nil, never a made-up zero, when there is no
   price at all?
2. is the vendor value kept separate, and does the client's sell price win
   over the dataset's copy of the same number?
3. is cash still what the player picked up, counted once, even though the
   loot window carries copper of its own?
4. do the session totals, the tally rows and the per-hour numbers agree?
5. does an old dataset say it is stale instead of being trusted silently?
6. is the loot feed subscribed once, so a reload cannot double the ledger?
7. does MobInfo2's documented API supply the per-kill denominator, and does
   the addon survive its absence?

    python scripts/zone_timer_harness.py [zone-timer-dir]

Exit status is 0 only when every check holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

from lupa import LuaError, LuaRuntime

ROOT = Path(__file__).resolve().parent.parent
ZTR = Path(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "ZoneTimerRedux"))
ESTIMATE = ROOT / "DFA_Estimate"
INTEL = ROOT / "DFA_AuctionIntelligence"

MOCK = r"""
-- ---------------------------------------------------------------- the client
PRINTED = {}
function print(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
    PRINTED[#PRINTED + 1] = table.concat(parts, " ")
end

-- Just after the shipped dataset was generated, so "not stale" means
-- something on any day.  This used to be a hardcoded epoch, which pinned
-- the harness to one build of the dataset: after the next export the
-- clock sat *before* the data and "an old dataset is reported stale"
-- passed 15 days into a dataset that was already 14 days ahead of it.
-- The clock now reads the dataset's own timestamp the first time it is
-- asked, and falls back to the old epoch when there is no dataset.
NOW = nil
function time()
    if NOW == nil then
        NOW = (DFA_ESTIMATE and DFA_ESTIMATE.generatedAt or 1792653821)
            + 2 * 3600
    end
    return NOW
end
function date(fmt, ts) return tostring(ts) end

local function Widget(kind, name)
    -- dialogs are created hidden in the client; the TOC shows the main
    -- one explicitly and the tally only through /ztt
    local w = { kind = kind, name = name, scripts = {}, events = {}, text = "",
                shown = false }
    w.SetScript = function(self, script, fn) self.scripts[script] = fn end
    w.GetScript = function(self, script) return self.scripts[script] end
    w.HookScript = function(self, script, fn) self.scripts[script] = fn end
    w.RegisterEvent = function(self, event) self.events[#self.events + 1] = event end
    w.UnregisterEvent = function() end
    w.UnregisterAllEvents = function(self) self.events = {} end
    w.Show = function(self) self.shown = true end
    w.Hide = function(self) self.shown = false end
    w.IsShown = function(self) return self.shown end
    w.SetText = function(self, text) self.text = tostring(text or "") end
    w.GetText = function(self) return self.text end
    w.SetJustifyH = function() end
    w.CreateFontString = function(self, name, layer, template)
        local child = Widget("FontString", name)
        return child
    end
    -- a client stub has hundreds of setters and none of them change what
    -- is being tested; the chain is kept so one call is one call
    return setmetatable(w, {
        __index = function(t, key)
            local fn = function(first) return first end
            rawset(t, key, fn)
            return fn
        end,
    })
end

FRAMES = {}
function CreateFrame(kind, name, parent, template)
    local frame = Widget(kind, name)
    frame.parent = parent
    FRAMES[#FRAMES + 1] = frame
    return frame
end

function FIRE(event, ...)
    for _, frame in ipairs(FRAMES) do
        local registered = false
        for _, e in ipairs(frame.events) do
            if e == event then registered = true break end
        end
        local handler = frame.scripts.OnEvent
        if registered and handler then handler(frame, event, ...) end
    end
end

UIParent = CreateFrame("Frame", "UIParent")
GameTooltip = CreateFrame("GameTooltip", "GameTooltip")

ZONE = "Ashenvale"
function GetRealZoneText() return ZONE end
function GetLocale() return "enUS" end
function GetRealmName() return "Dragonmaw" end
function UnitName() return "Bullbound" end
function UnitGUID() return "Player-1465-00000001" end
function UnitIsAFK() return false end

-- the client's item table: the sell price per item, when it has it cached
-- The client marks restricted data as "secret": normal-looking, but any
-- use raises.  A handler that reads it must skip it, not take the client's
-- own event down with it.
SECRETS = {}
function issecretvalue(value)
    return SECRETS[value] == true
end

SELL = {}
function GetItemInfo(itemId)
    itemId = tonumber(itemId) or itemId
    return "Item " .. tostring(itemId),
        "|cffffffff|Hitem:" .. tostring(itemId) .. ":::::::|h[Item]|h|r",
        1, 1, 1, "Misc", "Misc", 20, "", 1, SELL[itemId]
end
C_Item = { GetItemInfo = GetItemInfo, GetItemInfoInstant = function(id) return id end }

SlashCmdList = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) print(msg) end }

-- the loot window --------------------------------------------------------
LOOT = {}
function LOOT_CLEAR() LOOT = {} end
function LOOT_ADD(guid, slotType, quantity, link)
    LOOT[#LOOT + 1] = { guid = guid, slotType = slotType,
                        quantity = quantity, link = link }
end
function GetNumLootItems() return #LOOT end
function GetLootSourceInfo(slot) return LOOT[slot] and LOOT[slot].guid end
function GetLootSlotType(slot) return LOOT[slot] and LOOT[slot].slotType end
function GetLootSlotLink(slot) return LOOT[slot] and LOOT[slot].link end
function GetLootSlotInfo(slot)
    local entry = LOOT[slot]
    return "icon", "Loot", entry and entry.quantity or 0
end

-- AlnUI, recorded rather than loaded.  Its two thousand lines render a
-- window; they are not what the valuation has to get right, and the labels
-- and the row tooltips are all this harness reads.
AlnUI = {}
LABELS = {}
LAST_DIALOG = nil
LAST_LIST = nil
TOASTS = {}
function AlnUI:CreateDialog(opts)
    local frame = Widget("Dialog", opts and opts.name)
    frame.opts = opts
    LAST_DIALOG = frame
    return frame
end
function AlnUI:CreateSeparator(parent, opts) return Widget("Separator") end
function AlnUI:CreateLabel(parent, opts)
    local label = Widget("FontString")
    label:SetText(opts and opts.text or "")
    LABELS[#LABELS + 1] = label
    return label
end
function AlnUI:CreateSortHeader(parent, opts, cols)
    local header = Widget("SortHeader")
    header.cols = cols
    header.SetSort = function() end
    return header
end
function AlnUI:CreateScrollList(parent, opts)
    local list = { alnRows = {} }
    LAST_LIST = list
    function list:SetData(rows)
        self.alnRows = rows
        self.rows = {}
        for index, data in ipairs(rows) do
            local row = { cols = {}, data = data }
            for i, value in ipairs(data) do
                row.cols[i] = Widget("FontString")
                row.cols[i]:SetText(value)
            end
            if opts and opts.onRowInit then opts.onRowInit(row) end
            self.rows[index] = row
        end
    end
    function list:GetNumRows() return #self.alnRows end
    list:SetData(opts and opts.data or {})
    return list
end
function AlnUI:HasTabs() return false end
function AlnUI:CreateButton(parent, opts)
    local button = Widget("Button")
    button:SetText(opts and opts.text or "")
    return button
end
function AlnUI:AddTooltip(frame, title, text)
    frame.alnTooltip = { title = title, text = text }
    return frame
end
function AlnUI:ShowToast(opts)
    TOASTS[#TOASTS + 1] = opts and opts.text or ""
    return Widget("Toast")
end
function AlnUI:GetThemes() return { "gold" } end

-- the same contract AlnUI:AddTooltip gives a reader: title(frame), text
function TOOLTIP_OF(frame)
    local tip = frame and frame.alnTooltip
    if not tip then return nil, nil end
    local title, text = tip.title, tip.text
    if type(title) == "function" then title, text = title(frame) end
    return title, text
end

-- MobInfo2's documented API (ReadMe_MobInfo_API.txt), and nothing else
MI2_DATA = {}
function MI2_GetMobData(nameOrId, level, unit, combine)
    return MI2_DATA[tonumber(nameOrId) or nameOrId]
end
"""


def toc_files(addon_dir: Path, toc_name: str) -> list[str]:
    toc = addon_dir / toc_name
    return [line.strip() for line in toc.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("##")]


def load_files(lua, addon_dir: Path, files, addon_name: str, addon_table,
               skip=()) -> None:
    loader = lua.eval("loadfile")
    for name in files:
        if name in skip:
            continue
        path = addon_dir / name.replace("\\", "/")
        if not path.exists():
            raise SystemExit(f"the TOC lists a missing file: {name}")
        loaded = loader(str(path).replace("\\", "/"))
        # loadfile returns the chunk (and, on some runtimes, a nil error)
        chunk = loaded[0] if isinstance(loaded, tuple) else loaded
        if not callable(chunk):
            raise SystemExit(f"the TOC file does not compile: {name}")
        chunk(addon_name, addon_table)


def main() -> int:
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        checks.append((name, bool(ok), str(detail)))

    def run(code: str):
        return lua.execute(code)

    def ev(code: str):
        return lua.eval(code)

    estimate_addon = lua.table()
    intel_addon = lua.table()
    ztr_addon = lua.table()

    try:
        load_files(lua, ESTIMATE, toc_files(ESTIMATE, "DFA_Estimate.toc"),
                   "DFA_Estimate", estimate_addon)
        # The kill/loot observer is the single capture path; load its file
        # the way the addon does, then the zone timer on top of it.
        load_files(lua, INTEL, ["Localization.lua", "Kills.lua"],
                   "DFA_AuctionIntelligence", intel_addon)
        ztr_files = toc_files(ZTR, "ZoneTimerRedux.toc")
        # AlnUI is replaced by the recording mock above; the TOC still has
        # to list it, and the check below enforces that.
        load_files(lua, ZTR, ztr_files, "ZoneTimerRedux", ztr_addon,
                   skip=("Libs/AlnUI.lua",))
    except LuaError as exc:
        print(f"FAIL the addons do not load: {exc}")
        return 1

    check("the TOC lists the AlnUI library the harness replaces",
          "Libs/AlnUI.lua" in ztr_files, ", ".join(ztr_files))
    check("the TOC loads pricing before the UI",
          ztr_files.index("Pricing.lua") < ztr_files.index("UI.lua"),
          ", ".join(ztr_files))
    check("the TOC loads the loot feed before the UI",
          ztr_files.index("Loot.lua") < ztr_files.index("UI.lua"),
          ", ".join(ztr_files))

    lua.globals().ESTADDON = estimate_addon
    run("FIRE('ADDON_LOADED', 'DFA_Estimate')")
    run("FIRE('ADDON_LOADED', 'DFA_AuctionIntelligence')")
    run("FIRE('ADDON_LOADED', 'ZoneTimerRedux')")
    run("FIRE('PLAYER_LOGIN')")
    run("FIRE('PLAYER_ENTERING_WORLD')")

    if ev("DFA_ESTIMATE_API") is None:
        print("FAIL the pricing API is not published")
        return 1
    check("DFA Estimate publishes one API table for other addons",
          ev("DFA_ESTIMATE_API") is not None
          and ev("DFA_ESTIMATE_API.Load") is not None)
    check("DFA Estimate and the API are the same reader",
          bool(ev("DFA_ESTIMATE_API == ESTADDON.DFAEstimate")))
    check("the zone timer sees the current zone", ev("ZoneTimerRedux.currentZone") == "Ashenvale",
          str(ev("ZoneTimerRedux.currentZone")))

    # --- the pricing API -------------------------------------------------
    max_age = ev("DFA_ESTIMATE_API.GetMaxAgeHours()")
    check("the staleness limit defaults to the engine's 14 days",
          int(max_age) == 24 * 14, str(max_age))
    age = float(ev("DFA_ESTIMATE_API.GetAgeHours()"))
    check("a two-hour-old dataset is not stale",
          age < 3 and ev("DFA_ESTIMATE_API.IsStale()") is False, f"{age:.1f}h")

    picked = ev("""(function()
        local block = DFA_ESTIMATE_API.data.realms[GetRealmName()]
        for id, record in pairs(block.items or {}) do
            if record.net and record.net > 0 and record.vendor
                    and record.vendor > 0 and record.price
                    and record.price ~= record.net then
                return id, record.net, record.vendor, record.price
            end
        end
        return nil
    end)()""")
    if not picked:
        print("FAIL the shipped dataset has no realm record with a net price")
        return 1
    if not isinstance(picked, tuple):
        picked = (picked,)
    record_item = int(picked[0])
    record_net = int(picked[1])
    record_vendor = int(picked[2])
    record_price = int(picked[3])

    second = ev("""(function()
        local block = DFA_ESTIMATE_API.data.realms[GetRealmName()]
        for id, record in pairs(block.items or {}) do
            if id ~= %d and record.net and record.net > 0 then
                return id, record.net
            end
        end
        return nil
    end)()""" % record_item)
    if not isinstance(second, tuple):
        second = (second,)
    client_item = int(second[0])
    client_net = int(second[1])
    run(f"SELL[{client_item}] = 700")

    regional = ev("""(function()
        local data = DFA_ESTIMATE_API.data
        local block = data.realms[GetRealmName()]
        for id, value in pairs(data.nets or {}) do
            if not (block.items and block.items[id]) and value > 0 then
                return id, value
            end
        end
        return nil
    end)()""")
    if not isinstance(regional, tuple):
        regional = (regional,)
    regional_item = int(regional[0])
    regional_net = int(regional[1])
    factor = float(ev("DFA_ESTIMATE_API.GetRealmEntry().factor"))
    regional_expected = max(1, int(regional_net * factor + 0.5))

    unpriced_item = 999999
    check("the unpriced probe is absent from every price table",
          ev(f"DFA_ESTIMATE_API.GetLootValues({unpriced_item})") is None)

    values = ev(f"DFA_ESTIMATE_API.GetLootValues({record_item})")
    check("the realm record is the first source for the market value",
          values is not None and int(values["market"]) == record_net
          and values["source"] == "record",
          f"market={values and values['market']} net={record_net}")
    check("the market value is the net money, not the asking price",
          record_net != record_price
          and int(values["market"]) != record_price,
          f"net={record_net} ask={record_price}")
    check("the vendor value travels separately",
          int(values["vendor"]) == record_vendor
          and int(values["vendor"]) != int(values["market"]),
          f"vendor={values['vendor']}")
    check("a fresh record is not marked stale", values["stale"] is False or
          values["stale"] is None, str(values["stale"]))

    regional_values = ev(f"DFA_ESTIMATE_API.GetLootValues({regional_item})")
    check("without a record the regional net is scaled by the realm factor",
          regional_values is not None
          and int(regional_values["market"]) == regional_expected
          and regional_values["source"] == "regional",
          f"market={regional_values and regional_values['market']} "
          f"expected={regional_expected}")

    # --- the loot feed ---------------------------------------------------
    guid = "Creature-0-1465-2552-1234-224654-00004A2B1C"
    check("the zone timer subscribed to the loot feed",
          ev("ZoneTimerRedux.SubscribeLootFeed()") is True)
    run(f"DFA_AI.Kills:RecordKill('{guid}', 'Bristleback Boar')")
    check("a kill reaches the session ledger",
          int(ev(f"ZoneTimerRedux.mobs[224654].kills")) == 1,
          str(ev(f"ZoneTimerRedux.mobs[224654].kills")))

    def loot(guid: str, rows: list[tuple[int, int, int | None]]) -> None:
        run("LOOT_CLEAR()")
        for slot_type, quantity, item in rows:
            link = (f"'|cffffffff|Hitem:{item}:::::::|h[Item]|h|r'"
                    if item is not None else "nil")
            run(f"LOOT_ADD('{guid}', {slot_type}, {quantity}, {link})")
        # The event wiring lives in the addon's Core.lua, which the intel
        # harness already exercises; this one drives the observer itself.
        run("DFA_AI.Kills:OnLootOpened()")
        run("DFA_AI.Kills:OnLootClosed()")

    loot(guid, [(1, 1, record_item)])
    check("loot lands in the account ledger",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 1)
    check("loot lands in the character ledger too",
          int(ev(f"ZoneTimerCharDB.loot.Ashenvale[{record_item}]")) == 1)

    # a reload re-fires ADDON_LOADED on every frame; the subscription must
    # not become two listeners, or the next corpse counts twice
    for _ in range(3):
        run("FIRE('ADDON_LOADED', 'DFA_AuctionIntelligence')")
        run("FIRE('PLAYER_LOGIN')")
    loot("Creature-0-1465-2552-1234-224654-00004A2B1D", [(1, 1, record_item)])
    check("a reload does not subscribe the feed twice",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 2,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))

    # --- ZoneTimer's own capture -----------------------------------------
    # DFA_AuctionIntelligence's feed answered for only a handful of corpses in
    # a long session, so the addon captures the loot window itself.  These
    # checks fire the client events rather than driving the observer.
    run("ZoneLootDB = {}")
    run("ZoneTimerCharDB.loot = {}")
    run("ZoneTimerRedux.mobs = {}")
    run("ZoneTimerRedux:TouchLedger()")

    # a GUID the observer ignores (not a creature) proves the capture stands
    # on its own: the feed never sees this corpse
    own_guid = "GameObject-0-1465-2552-1234-99999-00004A2C00"
    run("LOOT_CLEAR()")
    run(f"LOOT_ADD('{own_guid}', 0, 2, "
        f"'|cffffffff|Hitem:{record_item}:::::::|h[Item]|h|r')")
    run("FIRE('LOOT_OPENED')")
    run("FIRE('LOOT_CLOSED')")
    check("the addon captures loot without the feed",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 2,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))

    # the same corpse again, inside the dedupe window: a reopened loot window
    # for leftovers, not a second kill
    run("FIRE('LOOT_OPENED')")
    run("FIRE('LOOT_CLOSED')")
    check("a reopened corpse is counted once",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 2,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))

    # the same spawn killed later: the client hands a respawn the same GUID,
    # so a permanent memo would swallow this drop
    run("NOW = NOW + 300")
    run("FIRE('LOOT_OPENED')")
    run("FIRE('LOOT_CLOSED')")
    check("a respawned mob is a new drop",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 4,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))
    run("NOW = NOW - 300")

    # a creature corpse seen by both paths is still one drop
    shared_guid = "Creature-0-1465-2552-1234-224654-00004A2C01"
    run("ZoneLootDB = {}")
    run("ZoneTimerCharDB.loot = {}")
    run("ZoneTimerRedux:TouchLedger()")
    run("LOOT_CLEAR()")
    run(f"LOOT_ADD('{shared_guid}', 0, 1, "
        f"'|cffffffff|Hitem:{record_item}:::::::|h[Item]|h|r')")
    run("FIRE('LOOT_OPENED')")
    run("FIRE('LOOT_CLOSED')")
    check("a corpse both paths see is one drop",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 1,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))

    # a zone with loot but no saved time must still get a tally row, or its
    # loot is invisible everywhere.  Both ledgers get it, exactly as
    # RecordDrop writes them.
    run(f"ZoneLootDB['Loot Only Zone'] = {{ [{record_item}] = 1 }}")
    run(f"ZoneTimerCharDB.loot['Loot Only Zone'] = {{ [{record_item}] = 1 }}")
    run("ZoneTimerCharDB.times['Loot Only Zone'] = nil")
    run("ZoneTimerSettings.times['Loot Only Zone'] = nil")
    listed = [str(row["zone"]) for row in ev("ZoneTimerRedux:GetSortedZones()").values()]
    check("a zone with loot but no time gets a tally row",
          "Loot Only Zone" in listed, str(listed))
    run("ZoneLootDB['Loot Only Zone'] = nil")
    run("ZoneTimerCharDB.loot['Loot Only Zone'] = nil")

    # a clean ledger for the arithmetic below
    run("ZoneLootDB = {}")
    run("ZoneTimerCharDB.loot = {}")
    run("ZoneTimerRedux.mobs = {}")
    run("ZoneTimerRedux:TouchLedger()")
    run("ZoneTimerRedux._valuations = {}")

    main_guid = "Creature-0-1465-2552-1234-224654-00004A2B1E"
    loot(main_guid, [(1, 3, record_item), (1, 2, client_item),
                (1, 1, regional_item), (1, 1, unpriced_item),
                (2, 4500, None)])
    check("a reopened corpse is not a second drop",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 3,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))
    check("the loot feed records the mob's copper for gold per kill",
          int(ev("ZoneTimerRedux.mobs[224654].copper")) == 4500)

    valuation = ev("ZoneTimerRedux:Valuation('Ashenvale', false)")
    expected_vendor = 3 * record_vendor + 2 * 700
    expected_market = 3 * record_net + 2 * client_net + regional_expected
    check("cash is untouched by the loot window",
          int(valuation["cash"]) == 0, str(valuation["cash"]))
    check("vendor value sums the client price and the dataset's",
          int(valuation["vendor"]) == expected_vendor,
          f"{valuation['vendor']} expected {expected_vendor}")
    check("market value sums net prices, item by item",
          int(valuation["market"]) == expected_market,
          f"{valuation['market']} expected {expected_market}")
    check("vendor and market are not mixed",
          int(valuation["vendor"]) != int(valuation["market"]))
    check("an unpriced item is counted, not valued",
          int(valuation["unpriced"]) == 1
          and int(valuation["priced"]) == 3
          and int(valuation["quantity"]) == 7,
          f"unpriced={valuation['unpriced']} priced={valuation['priced']} "
          f"qty={valuation['quantity']}")

    run("FIRE('CHAT_MSG_MONEY', 'You receive 45 Silver.')")
    valuation = ev("ZoneTimerRedux:Valuation('Ashenvale', false)")
    check("cash comes from the money message, once",
          int(valuation["cash"]) == 4500, str(valuation["cash"]))

    # A restricted message or corpse cannot be read: the addon must skip it
    # rather than raise inside the client's own event.
    run("SECRETS['You receive 99 Gold.'] = true")
    run("FIRE('CHAT_MSG_MONEY', 'You receive 99 Gold.')")
    valuation = ev("ZoneTimerRedux:Valuation('Ashenvale', false)")
    check("a restricted money message is ignored, not crashed on",
          int(valuation["cash"]) == 4500, str(valuation["cash"]))
    secret_guid = "Creature-0-1465-2552-1234-224654-00004A2B1F"
    run(f"SECRETS['{secret_guid}'] = true")
    loot(secret_guid, [(1, 5, record_item)])
    check("a restricted corpse is not attributed",
          int(ev(f"ZoneLootDB.Ashenvale[{record_item}]")) == 3,
          str(ev(f"ZoneLootDB.Ashenvale[{record_item}]")))

    # --- what the player sees -------------------------------------------
    # half an hour in the zone, so the per-hour line has a denominator
    run("ZoneTimerRedux.enteredTime = NOW - 1800")
    run("ZoneTimerRedux.mainFrame.scripts.OnUpdate(ZoneTimerRedux.mainFrame)")
    cash_text = str(ev("ZoneTimerRedux.mainText.cash:GetText()"))
    loot_text = str(ev("ZoneTimerRedux.mainText.loot:GetText()"))
    check("the main window shows cash", "45s" in cash_text, cash_text)
    check("the main window shows the vendor value", "g" in loot_text, loot_text)
    check("the main window marks the market value as an estimate",
          "~" in loot_text, loot_text)

    title, body = ev("TOOLTIP_OF(ZoneTimerRedux.mainText.loot)")
    body = str(body or "")
    check("the loot tooltip names the vendor value", "Vendor:" in body, body[:80])
    check("the loot tooltip names the market value as estimated",
          "Market:" in body and "estimated" in body, body[:120])
    check("the loot tooltip gives the per-hour numbers", "Per hour" in body)
    check("the loot tooltip says where the prices came from",
          "DFA Estimate" in body, body[-120:])
    cash_title, cash_body = ev("TOOLTIP_OF(ZoneTimerRedux.mainText.cash)")
    check("the cash tooltip calls cash actual money",
          "actual" in str(cash_body or ""), str(cash_body))

    run("SlashCmdList['ZONETIMERTALLY']()")
    tally_cash = str(ev("ZoneTimerRedux.tallyText.cash():GetText()"))
    tally_loot = str(ev("ZoneTimerRedux.tallyText.loot():GetText()"))
    check("the tally total is cash", "Cash:" in tally_cash and "45s" in tally_cash,
          tally_cash)
    check("the tally total keeps vendor and market apart",
          "vendor" in tally_loot and "market" in tally_loot
          and "~" in tally_loot, tally_loot)
    check("the tally mentions the unpriced item",
          "unpriced" in tally_loot, tally_loot)

    row_cash = str(ev("LAST_LIST.rows[1].cols[3]:GetText()"))
    check("the tally row shows the same cash as the totals",
          "45s" in row_cash and str(valuation["cash"]) != "0", row_cash)
    row_title, row_body = ev("TOOLTIP_OF(LAST_LIST.rows[1])")
    check("a tally row tooltip carries the split",
          row_title == "Ashenvale -- gold" and "Vendor:" in str(row_body),
          f"{row_title} / {str(row_body)[:60]}")

    # --- per hour --------------------------------------------------------
    rates = ev("ZoneTimerRedux:ZoneRates('Ashenvale', false)")
    check("half an hour doubles the hourly market run rate",
          abs(float(rates["market"]) - 2 * expected_market) < 1.0,
          f"{rates['market']} vs {2 * expected_market}")
    check("the per-hour cash is the picked-up money",
          abs(float(rates["cash"]) - 2 * 4500) < 1.0, str(rates["cash"]))

    # --- an old dataset --------------------------------------------------
    run("NOW = NOW + 15 * 24 * 3600")
    stale = ev("ZoneTimerRedux:Valuation('Ashenvale', false)")
    check("an old dataset is reported stale",
          stale["stale"] is True, str(stale["stale"]))
    run("ZoneTimerRedux.SyncTallySort()")
    tally_loot = str(ev("ZoneTimerRedux.tallyText.loot():GetText()"))
    check("the tally says the prices are stale", "stale" in tally_loot, tally_loot)
    run("NOW = NOW - 15 * 24 * 3600")

    # --- no pricing engine ----------------------------------------------
    run("DFA_ESTIMATE_API = nil")
    run("ZoneTimerRedux._valuations = {}")
    bare = ev("ZoneTimerRedux:Valuation('Ashenvale', false)")
    check("without the dataset nothing is estimated",
          int(bare["market"]) == 0, str(bare["market"]))
    check("without the dataset the client's vendor price still counts",
          int(bare["vendor"]) == 2 * 700, str(bare["vendor"]))
    check("without the dataset unpriced items are still counted, not zeroed",
          int(bare["unpriced"]) == 3, str(bare["unpriced"]))
    run("ZoneTimerRedux.SyncTallySort()")
    tally_loot = str(ev("ZoneTimerRedux.tallyText.loot():GetText()"))
    check("the tally says prices are missing", "no prices" in tally_loot, tally_loot)
    run("DFA_ESTIMATE_API = ESTADDON.DFAEstimate")
    run("ZoneTimerRedux._valuations = {}")
    check("the dataset is back after the outage",
          ev("DFA_ESTIMATE_API") is not None
          and int(ev("DFA_ESTIMATE_API.GetPriceCount()")) > 1000)

    # --- MobInfo2 --------------------------------------------------------
    run(f"""MI2_DATA[224654] = {{
        name = 'Bristleback Boar',
        kills = 40, loots = 38, emptyLoots = 2, copper = 12000,
        itemList = {{ [{record_item}] = 120 }},
    }}""")
    run("ZoneTimerRedux._mobInfo = nil")
    mob = ev("ZoneTimerRedux:GetMobGold(224654)")
    expected_mob_market = 120 * record_net
    check("MobInfo2's kills are the gold-per-kill denominator",
          mob is not None and mob["source"] == "mobinfo"
          and int(mob["kills"]) == 40, f"{mob and mob['source']} {mob and mob['kills']}")
    check("the mob's loot is valued with the pricing engine",
          int(mob["market"]) == expected_mob_market,
          f"{mob['market']} expected {expected_mob_market}")
    check("the mob's copper stays separate from its item value",
          int(mob["copper"]) == 12000 and int(mob["market"]) != int(mob["copper"]))
    check("the per-kill figure divides by MobInfo2's kills",
          abs(float(mob["marketPerKill"]) - expected_mob_market / 40) < 0.01,
          str(mob["marketPerKill"]))

    run("MI2_GetMobData = nil")
    run("ZoneTimerRedux._mobInfo = nil")
    session = ev("ZoneTimerRedux:GetMobGold(224654)")
    check("without MobInfo2 the session ledger answers instead",
          session is not None and session["source"] == "session"
          and int(session["loots"]) == 1 and int(session["kills"]) == 0,
          f"{session and session['source']} "
          f"loots={session and session['loots']} kills={session and session['kills']}")
    check("the session ledger values the same loot the same way",
          int(session["market"]) == expected_market
          and int(session["vendor"]) == expected_vendor,
          f"{session['market']} vs {expected_market}")
    check("a mob nobody recorded has no gold figure",
          ev("ZoneTimerRedux:GetMobGold(424242)") is None)

    # --- the text report -------------------------------------------------
    run("PRINTED = {}")
    run("SlashCmdList['ZONETIMEREDUX']('gold')")
    printed = " || ".join(str(text) for text in ev("PRINTED").values())
    check("/zt gold reports the zones", "Ashenvale" in printed, printed[:160])
    check("/zt gold names cash, vendor and market",
          "cash" in printed and "vendor" in printed and "market" in printed)
    check("/zt gold says where the prices came from",
          "DFA Estimate" in printed, printed[:200])

    run("PRINTED = {}")
    run("SlashCmdList['ZONETIMEREDUX']('loot')")
    printed = " || ".join(str(text) for text in ev("PRINTED").values())
    check("/zt loot reports the capture", "capture" in printed or "feed" in printed,
          printed[:160])
    check("/zt loot names the loot-window calls it depends on",
          "GetLootSourceInfo" in printed and "GetLootSlotLink" in printed,
          printed[:220])

    failed = [item for item in checks if not item[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
