"""Throwaway profiler for the pricing export: one line per stage, no writes."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\FarmAnalysis")

MARKS = []
T0 = time.perf_counter()


def mark(name, fn):
    started = time.perf_counter()
    out = fn()
    elapsed = time.perf_counter() - started
    MARKS.append({"stage": name, "seconds": round(elapsed, 2)})
    print(f"[{time.perf_counter() - T0:7.1f}s] {name:28s} {elapsed:7.2f}s", flush=True)
    return out


def main() -> int:
    from woweconomy.pricing.config import PricingConfig
    from woweconomy.pricing.engine import PricingEngine
    from woweconomy.pricing.dataset import build_dataset, validate_dataset

    config = PricingConfig()
    engine = mark("engine.load", lambda: PricingEngine(config).load())
    mark("fit_shipped_model", engine.fit_shipped_model)
    mark("calibrate_price_interval", engine.calibrate_price_interval)
    builder, notes = mark("build_dataset", lambda: build_dataset(engine, config))

    def index():
        from woweconomy.intel import store
        from woweconomy.pricing.realm_index import build_index
        con = store.connect(config.dfa_db)
        realms = sorted(builder.realm_groups) or None
        builder.realm_index = build_index(engine, builder.region_prices, con,
                                         realms=realms,
                                         horizon_h=config.horizon_h)
    mark("build_realm_index", index)
    text = mark("to_lua", builder.to_lua)
    mark("validate_dataset", lambda: validate_dataset(text))
    tmp = Path(r"C:\FarmAnalysis\data\_profile_export.lua")
    mark("write_file", lambda: tmp.write_text(text, encoding="utf-8", newline="\n"))
    if tmp.exists():
        tmp.unlink()

    out = {
        "total_seconds": round(time.perf_counter() - T0, 2),
        "stages": MARKS,
        "records": len(builder.records),
        "realms_with_records": len(builder.realms),
        "realm_index": len(builder.realm_index),
        "region_prices": len(builder.region_prices),
        "bytes": len(text.encode("utf-8")),
        "checksum": builder.checksum()[:16],
    }
    print(json.dumps(out, indent=2), flush=True)
    Path(r"C:\FarmAnalysis\data\_profile_export.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
