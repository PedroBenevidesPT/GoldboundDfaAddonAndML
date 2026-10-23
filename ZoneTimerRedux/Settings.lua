-- ZoneTimerRedux/Settings_Mainline.lua
-- Settings panel using the native Settings API

if not (Settings and Settings.RegisterVerticalLayoutCategory) then return end

local ZTR = ZoneTimerRedux

-- Hook into Blizzard's button mixin so we can disable the Adopt button after use.
local _ztrAdoptButtonFrame = nil
if SettingsButtonElementMixin then
    hooksecurefunc(SettingsButtonElementMixin, "Init", function(self, data)
        if data and data._ztrDisable and self.Button then
            _ztrAdoptButtonFrame = self.Button
            self.Button:Disable()
        end
    end)
end

-- Delay all registration until PLAYER_LOGIN.
-- By that point SavedVariables are guaranteed loaded, the Settings UI is fully
-- initialised, and addon compartment frames are available.
local loader = CreateFrame("Frame")
loader:RegisterEvent("PLAYER_LOGIN")
loader:SetScript("OnEvent", function(self)
    self:UnregisterAllEvents()

    local category    = Settings.RegisterVerticalLayoutCategory("Zone Timer Redux")
    local addonLayout = SettingsPanel:GetLayout(category)

    -- Each proxy table holds a single key "v".  The Settings API reads/writes
    -- only these proxy tables, never ZoneTimerSettings directly.  Callbacks then
    -- sync the confirmed user change back into ZoneTimerSettings.
    local p = {
        theme       = { v = ZoneTimerSettings.theme or "gold" },
        windowVis   = { v = ZoneTimerSettings.windowVisible ~= false },
        showLabels  = { v = ZoneTimerSettings.showLabels ~= false },
        showAlerts  = { v = ZoneTimerSettings.showAlerts ~= false },
        trackGold   = { v = ZoneTimerSettings.trackGold ~= false },
        width       = { v = ZoneTimerSettings.width },
        opacity     = { v = ZoneTimerSettings.opacity },
        fontSize    = { v = ZoneTimerSettings.fontSize },
        tallyGold      = { v = ZoneTimerSettings.tallySort == "gold" },
        mainCharView   = { v = ZoneTimerSettings.mainPanelCharView == true },
    }

    -- Timer Window -------------------------------------------------------------
    -- Visibility, layout, and visual style of the main timer frame.

    addonLayout:AddInitializer(CreateSettingsListSectionHeaderInitializer("Timer Window"))

    local windowVisSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_WINDOW_VISIBLE", "v", p.windowVis,
        Settings.VarType.Boolean, "Show timer window", true)
    Settings.CreateCheckbox(category, windowVisSetting,
        "Show or hide the main timer window. You can also toggle it with /zt.")
    Settings.SetOnValueChangedCallback("ZTR_WINDOW_VISIBLE", function()
        ZoneTimerSettings.windowVisible = p.windowVis.v
        if ZoneTimerRedux.SetWindowVisible then
            ZoneTimerRedux.SetWindowVisible(p.windowVis.v)
        end
    end)

    -- Only the themes that suit every frame here: the timer window and the
    -- toasts have no title bar or close button, so the window-only Basic
    -- and Panel are left out (GetThemes("toast")).
    local THEME_DESCRIPTIONS = {
        gold     = "A gold dialog border with a title banner.",
        modern   = "The Game Menu's border and header.",
        standard = "A grey dialog border with a title banner.",
        tooltip  = "A tooltip's thin border.",
    }
    local function GetThemeOptions()
        local container = Settings.CreateControlTextContainer()
        for _, name in ipairs(AlnUI:GetThemes("toast")) do
            container:Add(name, name:sub(1, 1):upper() .. name:sub(2), THEME_DESCRIPTIONS[name])
        end
        return container:GetData()
    end

    local themeSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_THEME", "v", p.theme,
        Settings.VarType.String, "Window theme", "gold")
    Settings.CreateDropdown(category, themeSetting, GetThemeOptions,
        "The border style of every Zone Timer Redux window and of the milestone toasts.")
    Settings.SetOnValueChangedCallback("ZTR_THEME", function()
        ZoneTimerSettings.theme = p.theme.v
        if ZoneTimerRedux.ApplyWindowTheme then ZoneTimerRedux.ApplyWindowTheme() end
    end)

    local fontSizeSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_FONT_SIZE", "v", p.fontSize,
        Settings.VarType.Number, "Font size", 12)
    local fontOptions = Settings.CreateSliderOptions(8, 24, 1)
    fontOptions:SetLabelFormatter(MinimalSliderWithSteppersMixin.Label.Right, tostring)
    Settings.CreateSlider(category, fontSizeSetting, fontOptions,
        "Font size for text in the main timer window.")
    Settings.SetOnValueChangedCallback("ZTR_FONT_SIZE", function()
        ZoneTimerSettings.fontSize = p.fontSize.v
        if ZoneTimerRedux.SetFontSize then ZoneTimerRedux.SetFontSize(p.fontSize.v) end
    end)

    local showLabelsSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_SHOW_LABELS", "v", p.showLabels,
        Settings.VarType.Boolean, "Show labels", true)
    Settings.CreateCheckbox(category, showLabelsSetting,
        "Show 'Time' and 'Gold' labels next to the values. When off, values are centered.")
    Settings.SetOnValueChangedCallback("ZTR_SHOW_LABELS", function()
        ZoneTimerSettings.showLabels = p.showLabels.v
        if ZoneTimerRedux.SetShowLabels then ZoneTimerRedux.SetShowLabels(p.showLabels.v) end
    end)

    local mainCharViewSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_MAIN_CHAR_VIEW", "v", p.mainCharView,
        Settings.VarType.Boolean, "Show character data", false)
    Settings.CreateCheckbox(category, mainCharViewSetting,
        "Show per-character time and gold on the main timer window instead of account-wide totals.")
    Settings.SetOnValueChangedCallback("ZTR_MAIN_CHAR_VIEW", function()
        ZoneTimerSettings.mainPanelCharView = p.mainCharView.v
    end)

    local widthSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_WIDTH", "v", p.width,
        Settings.VarType.Number, "Window width", 220)
    local widthOptions = Settings.CreateSliderOptions(100, 400, 10)
    widthOptions:SetLabelFormatter(MinimalSliderWithSteppersMixin.Label.Right, tostring)
    Settings.CreateSlider(category, widthSetting, widthOptions,
        "Width of the main timer window in pixels.")
    Settings.SetOnValueChangedCallback("ZTR_WIDTH", function()
        ZoneTimerSettings.width = p.width.v
        if ZoneTimerRedux.LayoutMainFrame then ZoneTimerRedux.LayoutMainFrame() end
    end)

    local opacitySetting = Settings.RegisterAddOnSetting(
        category, "ZTR_OPACITY", "v", p.opacity,
        Settings.VarType.Number, "Window opacity", 1.0)
    local opacityOptions = Settings.CreateSliderOptions(0.1, 1.0, 0.1)
    opacityOptions:SetLabelFormatter(MinimalSliderWithSteppersMixin.Label.Right,
        function(v) return string.format("%.1f", v) end)
    Settings.CreateSlider(category, opacitySetting, opacityOptions,
        "Opacity of the main timer window.")
    Settings.SetOnValueChangedCallback("ZTR_OPACITY", function()
        ZoneTimerSettings.opacity = p.opacity.v
        ZoneTimerRedux.mainFrame:SetAlpha(p.opacity.v)
    end)

    -- Tracking -----------------------------------------------------------------
    -- What data to collect and how to display it.

    addonLayout:AddInitializer(CreateSettingsListSectionHeaderInitializer("Tracking"))

    local goldSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_TRACK_GOLD", "v", p.trackGold,
        Settings.VarType.Boolean, "Track gold", true)
    Settings.CreateCheckbox(category, goldSetting,
        "Track cash picked up per zone and the value of the loot, and display both on the timer.")
    -- two numbers, two meanings: cash is money in the bag, the loot line is
    -- what the items would fetch (vendor guaranteed, market estimated)
    Settings.SetOnValueChangedCallback("ZTR_TRACK_GOLD", function()
        ZoneTimerSettings.trackGold = p.trackGold.v
        if ZoneTimerRedux.SetGoldTracking then
            ZoneTimerRedux.SetGoldTracking(p.trackGold.v)
        end
    end)

    local alertsSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_SHOW_ALERTS", "v", p.showAlerts,
        Settings.VarType.Boolean, "Milestone alerts", true)
    Settings.CreateCheckbox(category, alertsSetting,
        "Show a toast notification when you reach a time or gold milestone in a zone.")
    Settings.SetOnValueChangedCallback("ZTR_SHOW_ALERTS", function()
        ZoneTimerSettings.showAlerts = p.showAlerts.v
    end)

    local tallySortSetting = Settings.RegisterAddOnSetting(
        category, "ZTR_TALLY_GOLD_SORT", "v", p.tallyGold,
        Settings.VarType.Boolean, "Sort tally by gold", false)
    Settings.CreateCheckbox(category, tallySortSetting,
        "Sort the tally window by gold earned instead of time spent.")
    Settings.SetOnValueChangedCallback("ZTR_TALLY_GOLD_SORT", function()
        -- the checkbox means "most gold first", or "most time first" when off
        ZoneTimerSettings.tallySort          = p.tallyGold.v and "gold" or "time"
        ZoneTimerSettings.tallySortAscending = false
        ZTR.sortMode      = ZoneTimerSettings.tallySort
        ZTR.sortAscending = false
        if ZoneTimerRedux.SyncTallySort then ZoneTimerRedux.SyncTallySort() end
    end)

    -- Data ---------------------------------------------------------------------
    -- Reset tools for recorded zone data.

    addonLayout:AddInitializer(CreateSettingsListSectionHeaderInitializer("Data"))

    addonLayout:AddInitializer(CreateSettingsButtonInitializer(
        "Reset current zone", "Reset",
        function()
            if ZoneTimerRedux.ResetCurrentZone then ZoneTimerRedux.ResetCurrentZone() end
        end,
        "Clears the recorded time for the zone you are currently in.", true))

    addonLayout:AddInitializer(CreateSettingsButtonInitializer(
        "Reset all zones", "Reset All",
        function()
            ZoneTimerSettings.times = {}
            ZoneTimerSettings.announcedMilestones = {}
        end,
        "Clears all recorded zone times and milestone data.", true))

    local adoptInit = CreateSettingsButtonInitializer(
        "Adopt global data as character data", "Adopt",
        function()
            if ZoneTimerSettings.globalDataAdopted then return end
            ZoneTimerRedux:AdoptGlobalData()
            if _ztrAdoptButtonFrame then _ztrAdoptButtonFrame:Disable() end
            print("|cff33ff99ZoneTimerRedux:|r Global data adopted into this character.")
        end,
        "Replaces this character's zone time and gold history with the account-wide data. "
        .. "Use this if you were tracking time before per-character tracking was added and want "
        .. "that history to count as this character's own. "
        .. "This overwrites any existing character data and cannot be undone. "
        .. "The button is permanently disabled after use.",
        false)
    if ZoneTimerSettings.globalDataAdopted then
        adoptInit.data._ztrDisable = true
    end
    addonLayout:AddInitializer(adoptInit)

    addonLayout:AddInitializer(CreateSettingsButtonInitializer(
        "Migrate data from the original ZoneTimer addon", "How to",
        function()
            if ZoneTimerRedux.ShowMigrationHelp then ZoneTimerRedux.ShowMigrationHelp() end
        end,
        "If you have history saved in the original ZoneTimer addon, click for step-by-step instructions on how to bring it into ZoneTimer Redux.",
        false))

    Settings.RegisterAddOnCategory(category)

    -- Re-apply all saved visual settings to the frames.  UI.lua built the frames
    -- at file-load time using placeholder defaults (SavedVariables weren't loaded
    -- yet); by PLAYER_LOGIN the real saved values are in ZoneTimerSettings.
    if ZoneTimerRedux.LayoutMainFrame  then ZoneTimerRedux.LayoutMainFrame()  end
    if ZoneTimerRedux.ApplyWindowTheme then ZoneTimerRedux.ApplyWindowTheme() end
    if ZoneTimerRedux.mainFrame then
        ZoneTimerRedux.mainFrame:SetAlpha(ZoneTimerSettings.opacity)
        if ZoneTimerSettings.windowVisible ~= false then
            ZoneTimerRedux.mainFrame:Show()
        else
            ZoneTimerRedux.mainFrame:Hide()
        end
    end

    -- AddOn Compartment --------------------------------------------------------

    if AddonCompartmentFrame then
        AddonCompartmentFrame:RegisterAddon({
            text                = "Zone Timer Redux",
            icon                = "Interface\\Icons\\inv_misc_pocketwatch_01",
            registerForAnyClick = true,
            func                = function()
                if GetMouseButtonClicked() == "RightButton" then
                    Settings.OpenToCategory(category.ID)
                elseif ZoneTimerRedux.mainFrame then
                    ZoneTimerRedux.SetWindowVisible(not ZoneTimerRedux.mainFrame:IsShown())
                end
            end,
            funcOnEnter = function(button)
                GameTooltip:SetOwner(button, "ANCHOR_LEFT")
                GameTooltip:AddLine("Zone Timer Redux", 1, 0.82, 0)
                GameTooltip:AddLine("Left-click: toggle main window", 1, 1, 1)
                GameTooltip:AddLine("Right-click: open settings", 1, 1, 1)
                GameTooltip:Show()
            end,
            funcOnLeave = function() GameTooltip:Hide() end,
        })
    end
end)
