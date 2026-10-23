-- DFA Auction Intelligence - Localization & Constants
DFA_AI = DFA_AI or {}

DFA_AI.VERSION = "1.6.1"
DFA_AI.SCHEMA_VERSION = 1
DFA_AI.ADDON_NAME = "DFA_AuctionIntelligence"

-- Newer clients hand addons "secret" values for restricted data: they
-- look like ordinary strings and numbers, but any use -- matching,
-- comparing, doing arithmetic -- raises.  Every read of a loot window or a
-- mailbox has to go through this, or an error is thrown inside a frame
-- Blizzard considers its own.
function DFA_AI.IsSecret(value)
    if value == nil then return false end
    return type(issecretvalue) == "function" and issecretvalue(value) == true
end

DFA_AI.L = {
    TITLE = "DFA Auction Intelligence",
    PREFIX = "|cff00ccff[DFA AI]|r ",
    LOADED = "DFA Auction Intelligence v%s initialized (Schema v%d).",
    STATUS_HEADER = "--- DFA Auction Intelligence Status ---",
    JOURNALATOR_CONNECTED = "Journalator: Connected (Version %s, %d stores, %d events)",
    JOURNALATOR_MISSING = "Journalator: Not found or disabled (operating in native fallback mode)",
    TSM_CONNECTED = "TSM: Detected (pricing reference capture enabled)",
    TSM_MISSING = "TSM: Not active (native pricing capture only)",
    DB_STATS = "DataStore: %d events captured (%d sales, %d purchases, %d listings, %d cancels, %d expires).",
    EXPORT_READY = "Export ready: %d records in DFA_AUCTION_INTELLIGENCE_EXPORT.",
    LAST_EVENT = "Last event captured: %s (%s).",
    NO_EVENTS = "No events recorded yet.",
    DIAG_OK = "Diagnostics: All systems healthy. Data integrity verified.",
    DIAG_ISSUES = "Diagnostics: %d warnings detected. Use '/dfaai diag' for details.",
}
