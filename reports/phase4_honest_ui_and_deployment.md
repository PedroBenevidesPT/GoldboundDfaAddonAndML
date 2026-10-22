# Phase 4 — Honest UI and deployment reliability

Branch `fix/sentinel-probability`. **Not merged into `main`.**
HEAD at the time of writing: `3c8a94c`.

---

## 1. Changed files

| File | Change | Purpose |
| --- | --- | --- |
| `DFA_Estimate/UI/Tooltip.lua` | +29 / −5 | Render the record status as a player-facing sentence instead of the raw lifecycle token. |
| `tests/test_tooltip_status.py` | new, 164 lines, 14 tests | Cover every status the tooltip can display, plus the missing/unknown/empty cases. |

Commit: `3c8a94c Say what the number is, instead of printing a lifecycle token`.

No other file was modified, staged or committed by this phase. In particular the
model artifacts were not touched, and the model-selection metric was not changed.

### What the tooltip change actually is

The old render site printed the status token verbatim, so a player saw
`Prediction: baseline_only`.

A display-only table now maps each known token to a phrase:

```lua
local STATUS_TEXT = {
    ["baseline_only"]            = "item sale rate, no learned model",
    ["trained_no_measured_gain"] = "no better than a simple rate",
    ["validated_ranking_only"]   = "ranking only, no duration",
    ["unsupported_signal"]       = "no signal the data can support",
    ["insufficient_data"]        = "too little evidence",
    ["stale"]                    = "out of date",
    ["unknown"]                  = "not available",
}
```

The lookup falls back to the raw token (`STATUS_TEXT[token] or token`), so a status
added upstream later stays visible rather than being silently swallowed. `validated`
still produces no line, a record with no status still produces no line, and
`record.fallback` is still appended in parentheses. The status values themselves, their
semantics, and every prediction are untouched.

---

## 2. Exact test commands and results

Interpreter used for every run (the only one with `catboost` and `pytest`):

```
C:\Users\petru\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe
```

### Focused tests

```
python -m pytest tests/test_tooltip_status.py tests/test_sentinel_probability.py -v
```

→ **19 passed in 1.76s**

The 14 tooltip tests cover: `validated` shows no line; all seven known statuses render as
sentences (parametrised); `baseline_only` says what the number is; an unknown upstream
status stays visible; a missing status adds no line; an empty status adds no line; the
fallback reason survives beside the label; and one test reads the **real exported
dataset** and asserts no shipped record reaches the player as a token.

### Full regression suite

```
python -m pytest tests/ -q
```

→ **8 failed, 942 passed in 13.86s**

---

## 3. Failures: pre-existing vs regression

**Regressions introduced by this branch: 0.**

Evidence: the same suite was run against an immutable checkout of `c277036` (the commit
before any of this branch's work) in a throwaway `git worktree`:

```
git worktree add /tmp/base_c277036 c277036
cd /tmp/base_c277036 && python -m pytest tests/ -q
```

→ **10 failed, 879 passed**

The 8 failures in the current tree are a subset of those 10. The 2 extra are worktree
artifacts — `test_the_installed_copy_is_the_one_that_was_tested` in
`test_auction_intel_addon.py` and `test_zone_timer_addon.py` compare the repo against the
live WoW `AddOns` folder, which a worktree has no relationship to. They pass in the real
tree.

### The 8 pre-existing failures and their root causes

| Test | Root cause |
| --- | --- |
| `test_dfa_estimate_addon.py` × 5 | `DFA_Estimate/Localization.lua` does not exist — **never** in git history, and not in the installed addon either. |
| `test_dfa_pricing.py` × 2 | lupa harness: `attempt to call a nil value (global 'CreateFrame')`. |
| `test_auction_intel_addon.py` × 1 | lupa harness `AttributeError`. |

The missing `Localization.lua` is not a deployment defect: `DFA_Estimate.toc` does not
list it and no Lua file references it, so the addon loads without it. The tests assert on
a file that was never shipped.

### The test-count variance seen earlier, explained

Two intermediate runs reported `13 failed` and `27 failed`. These were **not** regressions:

- A concurrent process was writing `woweconomy/intel/ah.py`, `store.py`, `identity.py`,
  `prices.py`, `woweconomy/ml/dataset.py`, `linking.py`, `scripts/intel.py`,
  `scripts/watch.py` between 21:06 and 21:19 in this same working tree.
- Tests that import those modules while they are mid-write fail with `NameError` /
  `AttributeError` — exactly the `test_sentinel.py` and `test_intel_*` failures observed.
- `tests/test_sentinel.py` passes 21/21 in isolation, and passes alongside both newly
  added test files.
- Once the writes settled, three consecutive full runs produced **byte-identical** failure
  sets (diffed, no differences). The passing count still drifts (923 → 942) because the
  concurrent work keeps adding tests; the failing set is stable.

---

## 4. Legacy price-model lifecycle

### Exact training entry point

`DfaLearningEngine.fit_final()` — `woweconomy/ml/pipeline.py:672`.
Reachable only through `scripts/dfa_ml.py train` (or `all`), which is `cmd_train` →
`build_rows` → `validate` → `test_window` → `ablation` → `fit_final` → `evaluate_strategy`.

It writes 9 files into `models/dfa_ml/`: `price_model.cbm`, `price_model.q10.cbm`,
`price_model.q90.cbm`, `sale_probability_{24,48,168,336}h.cbm`, `time_to_sale.cbm`, plus
`.features.json` sidecars. Current mtime: **2026-10-21 20:58**, i.e. ~24.5 h old.

### Every consumer of `models/dfa_ml/*.cbm`

- `PricingEngine.load()` — `woweconomy/pricing/engine.py:302-308` reads
  `price_model.cbm`, `sale_probability_{horizon_h}h.cbm`, `time_to_sale.cbm` from
  `models_dir`, default `C:\FarmAnalysis\models\dfa_ml` (`woweconomy/pricing/config.py:33`).
- `scripts/dfa_pricing_export.py:62` constructs that engine unconditionally. **This is the
  only path that reaches the game.** It is a hard dependency: without
  `models/dfa_ml/price_model.cbm` the exporter aborts with *"no price model found … run
  `python scripts/dfa_ml.py train` first"*.
- `scripts/rank_farms.py`, `woweconomy/pricing/farm_ranking.py`, and
  `scripts/dfa_ml.py recommend`.
- `--realm-model` does **not** replace this. It layers `models/intel/<region>/<realm>/`
  on top via `engine.load_realm_models()` (`engine.py:254`, `dfa_pricing_export.py:69-70`).

### Is automatic retraining safe and idempotent?

**Safe in effect, not idempotent in bytes.**

- Two identical runs produced 9 files, `identical on rerun: 0/9`.
- A 3-run probe showed the CatBoost `.cbm` bytes differ run to run while the
  **predictions are bit-identical** (`max abs diff: 0.0`). The difference is internal
  metadata, not the fit.
- Cost: **~90 s** per legacy refit (89.7 s / 91.8 s measured).
- Writes are **not atomic** — `model.save(path)` writes in place. A crash mid-write leaves
  a `.cbm` without its `.features.json` sidecar, and `PricingEngine.load()` then raises
  `FileNotFoundError` (see `models.py::_read_sidecar`).

### Does the watcher already have a supported scheduling mechanism?

**Yes — and it is not wired to this model.**

`plan()` (`scripts/watch.py:244-247`) already implements exactly the right pattern: a
due-time gate (`train_every_h = 6.0`) combined with a change gate
(`changed.get("journal") or changed.get("addon")`), and `mark_task` persists `last["train"]`.

But `task_train` (`watch.py:483-489`) runs `scripts/intel.py realm-ml` — the **realm**
models under `models/intel/`, not `fit_final`. So:

- the legacy `models/dfa_ml/*.cbm` have **no scheduled refresh at all** — they only change
  on a manual `dfa_ml.py train`;
- the sentinel path *is* properly gated: `task_sentinel` → `dfa_ml.py sentinel --if-needed`
  every 24 h, and `retrain_decision` requires `max(20, 10% of n_train)` new confirmed
  labels (472 today) before it refits.

Per the constraint, **no second scheduler was added and training was not wired into every
watcher cycle.**

---

## 5. Watcher startup persistence

**There is none.** No Task Scheduler entry, no Startup-folder shortcut, no `HKCU\...\Run`
key.

The live process is PID **23040**, `python3.13.exe`, started **2026-10-22 14:35:44**,
parent `powershell.exe` (PID 24216) — a VS Code integrated terminal:

```
"C:\Users\petru\AppData\Local\Microsoft\WindowsApps\python.exe" scripts\watch.py \
  --realms "Silvermoon,Draenor,Auchindoun,Kazzak,Blackrock,Dragonmaw,Defias Brotherhood"
```

It is alive and ticking (last tick 21:25:45, ~2 min cadence). It will **not** survive a
reboot or closing that terminal.

### Runtime and dependencies (verified on the running interpreter)

- Interpreter: `C:\Users\petru\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe`
- Python **3.13.14**

| Package | Version |
| --- | --- |
| catboost | 1.2.10 |
| scikit-learn | 1.9.1 |
| pandas | 3.0.6 |
| numpy | 2.5.3 |
| scipy | 1.18.1 |
| joblib | 1.6.0 |
| lupa | 2.8 |
| requests | 2.34.2 |
| pytest | 9.1.1 |
| SQLAlchemy | 2.1.4 |
| pillow | 13.0.0 |
| matplotlib | 3.11.2 |

Missing but **not required** on any path exercised: `alembic`, `posthog`
(`opentelemetry` is present). Import smoke test for `woweconomy.ml.sentinel`,
`woweconomy.pricing.engine`, `woweconomy.pricing.deploy`, `woweconomy.intel.realm_ml`:
all **OK**.

---

## 6. The abandoned `.staging-*` artifact

`models/dfa_sentinel/.staging-20261021T195432Z-8e127d19`

- 22 files, schema `dfa-sentinel/1`, created `2026-10-21T19:55:00Z`.
- **All 21 checksums in the manifest verify** (0 mismatches, 0 missing).
- `manifest.status = "validated"` — this is the *pre-fix* artifact, from before the
  `predict_proba` correction, which is exactly why it looked good.
- It is **not** `CURRENT`. `CURRENT` = `20261022T195801Z-4e9a79ed`.

**Referenced anywhere?** No:

- The only code occurrences are the generic construction
  `staging = cfg.models_root / f".staging-{version}"` (`sentinel.py:1738`) and a docstring
  (`sentinel.py:1575`); plus a prose mention in `reports/dfa_sentinel_ml.md`.
- `load_artifact` skips dot-prefixed names (`sentinel.py:1567`).
- `_prune_versions` skips dot-prefixed directories (`sentinel.py:1591`) — which is why it
  is still there.

**Conclusion:** unreferenced, never loaded, never promoted, never pruned. Safe to remove.
It was **not deleted**, per instruction.

---

## 7. Deployment verification (what was actually checked)

- `deploy.discover_targets` covers only `Data/DFA_Estimate_Data.lua` — it never writes
  `UI/Tooltip.lua`. The UI change was therefore deployed by hand.
- `Tooltip.lua` copied to the installed addon; both copies share one SHA-256:
  `11ff8bd1e0a5b9b34ba5d9bb959fd8d766ecbd6b6bc8717d8977cbf4f2eadcc3`.
- Every file listed in the installed `DFA_Estimate.toc` is present (7/7 OK).
- The shipped dataset carries `status = "baseline_only"` on **237/237** records.
- Rendering the real deployed dataset through the real `Tooltip.lua` produces
  **`Prediction: item sale rate, no learned model`**.

The watcher is running and the addon files are in place, but the client has not been
reloaded, so this is verified at the data-and-Lua level, not in a live game session.

---

## 8. Remaining risks

1. **The legacy models are stale and nothing refreshes them.** `models/dfa_ml/*.cbm` are
   24.5 h old; the exporter still loads them as the base layer for every realm. This is
   the largest correctness risk in the deployment and it was deliberately left alone
   (fixing it means either wiring `fit_final` into `plan()` or adding a scheduler — both
   outside this phase's constraints).
2. **Every shipped record is `baseline_only`.** Honest, but blunt: the tooltip now tells
   every player "item sale rate, no learned model". This is the direct, intended
   consequence of the Phase 1 fix. `sale_48h` *is* validated (brier 0.0936 vs 0.1309), but
   the dataset's headline horizon is 24 h, which the baseline wins.
3. **Log-loss-only selection structurally favours under-confident models.** Untouched, per
   requirement 7.
4. **Non-atomic artifact writes in `fit_final`.** A crash mid-write leaves a `.cbm` without
   its sidecar, which the engine rejects and the exporter then fails on.
5. **No startup persistence.** A reboot silently stops all data collection, exports and
   retraining until someone restarts the terminal.
6. **A concurrent writer shares this working tree.** Any full-suite number is a snapshot of
   a moving target; that is what produced the transient 13- and 27-failure runs.

---

## 9. Manual setup required

- **To restore the watcher after a reboot** — from `C:\FarmAnalysis`:
  ```
  "C:\Users\petru\AppData\Local\Microsoft\WindowsApps\python.exe" scripts\watch.py ^
    --realms "Silvermoon,Draenor,Auchindoun,Kazzak,Blackrock,Dragonmaw,Defias Brotherhood"
  ```
  Making this durable needs a Task Scheduler entry (logon trigger, restart-on-failure).
  **Not created** — it changes machine state and was not approved.
- **In game** — `/reload` or restart the client for the new `Tooltip.lua` to take effect.
- **`DFA_Estimate/Localization.lua`** — still absent. The 5 addon tests will keep failing
  until the file is added or the tests stop requiring it. The addon itself does not need
  it.
- **Retraining the legacy models** — manual only:
  `python scripts/dfa_ml.py train --refresh-journal` (~90 s).

---

## 10. Deliberately not done

- Not merged into `main`.
- No model artifact deleted (including the `.staging-*` directory).
- No second scheduler, and no training triggered per watcher cycle.
- Model-selection metric unchanged.
- No unrelated feature touched.
