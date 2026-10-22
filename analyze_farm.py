import csv
import math
import sys
from collections import defaultdict

rows = []
with open(r"C:\FarmAnalysis\csv\items.csv", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for r in reader:
        mv = float(r["marketValue"]) if r["marketValue"] else 0
        hist = float(r["historical"]) if r["historical"] else 0
        asp = float(r["avgSalePrice"]) if r["avgSalePrice"] else 0
        sr = float(r["saleRate"]) if r["saleRate"] else 0
        spd = float(r["soldPerDay"]) if r["soldPerDay"] else 0
        rows.append({
            "itemId": int(r["itemId"]),
            "name": r["name"],
            "marketValue": mv,
            "historical": hist,
            "avgSalePrice": asp,
            "saleRate": sr,
            "soldPerDay": spd,
        })

print(f"Total items: {len(rows)}", file=sys.stderr)

# Filter: must have soldPerDay > 0 and saleRate > 0 and marketValue > 0
valid = [r for r in rows if r["soldPerDay"] > 0 and r["saleRate"] > 0 and r["marketValue"] > 0]
print(f"Items with sales data: {len(valid)}", file=sys.stderr)

if not valid:
    print("No items with sales data found.")
    sys.exit(1)

# ---- Cap extreme outliers ----
# Use 99th percentile cap
def cap_series(values, pct=99.0):
    s = sorted(values)
    idx = int(len(s) * pct / 100.0)
    cap = s[idx]
    return [min(v, cap) for v in values]

spd_vals = [r["soldPerDay"] for r in valid]
sr_vals = [r["saleRate"] for r in valid]
mv_vals = [r["marketValue"] for r in valid]
asp_vals = [r["avgSalePrice"] for r in valid]
hist_vals = [r["historical"] for r in valid]

capped_spd = cap_series(spd_vals, 99.0)
capped_sr = cap_series(sr_vals, 99.0)
capped_mv = cap_series(mv_vals, 99.0)
capped_asp = cap_series(asp_vals, 99.0)

# ---- Min-Max Normalize (0-1) ----
def normalize(vals):
    mn = min(vals)
    mx = max(vals)
    if mx == mn:
        return [0.5] * len(vals)
    return [(v - mn) / (mx - mn) for v in vals]

norm_spd = normalize(capped_spd)
norm_sr = normalize(capped_sr)
norm_mv = normalize(capped_mv)

# PriceTrend = marketValue / historical, capped and normalized
price_trends = []
for r in valid:
    if r["historical"] > 0:
        pt = r["marketValue"] / r["historical"]
    else:
        pt = 1.0
    price_trends.append(min(pt, 5.0))  # cap at 5x

norm_pt = normalize(price_trends)

# ---- FarmScore ----
# 40% SoldPerDay, 30% SaleRate, 20% MarketValue, 10% PriceTrend
results = []
for i, r in enumerate(valid):
    fs = (0.40 * norm_spd[i] + 0.30 * norm_sr[i] + 0.20 * norm_mv[i] + 0.10 * norm_pt[i]) * 100.0

    # LiquidityScore: how easy to sell (saleRate + soldPerDay combined)
    liq = (0.5 * norm_sr[i] + 0.5 * norm_spd[i]) * 100.0

    # ValueScore: market value + average sale price health
    norm_asp = normalize(capped_asp)
    vs = (0.6 * norm_mv[i] + 0.4 * norm_asp[i]) * 100.0

    # DemandScore: sold per day heavily weighted
    dm = norm_spd[i] * 100.0

    trues = r["marketValue"] / r["historical"] if r["historical"] > 0 else 1.0
    pt_display = trues

    # Explanation
    parts = []
    if norm_spd[i] > 0.8:
        parts.append("Extremely high turnover")
    elif norm_spd[i] > 0.5:
        parts.append("High turnover")
    elif norm_spd[i] > 0.2:
        parts.append("Moderate turnover")
    else:
        parts.append("Low turnover")

    if norm_sr[i] > 0.8:
        parts.append("exceptional sale rate")
    elif norm_sr[i] > 0.5:
        parts.append("strong sale rate")
    elif norm_sr[i] > 0.2:
        parts.append("ok sale rate")
    else:
        parts.append("weak sale rate")

    if norm_mv[i] > 0.8:
        parts.append("high value")
    elif norm_mv[i] > 0.5:
        parts.append("decent value")
    elif norm_mv[i] < 0.1:
        parts.append("low value")

    if pt_display > 1.2:
        parts.append("price rising")
    elif pt_display < 0.8:
        parts.append("price dropping")

    results.append({
        "name": r["name"],
        "FarmScore": round(fs, 2),
        "LiquidityScore": round(liq, 2),
        "ValueScore": round(vs, 2),
        "DemandScore": round(dm, 2),
        "MarketValue": r["marketValue"],
        "Historical": r["historical"],
        "AvgSalePrice": r["avgSalePrice"],
        "SaleRate": r["saleRate"],
        "SoldPerDay": r["soldPerDay"],
        "PriceTrend": round(pt_display, 4),
        "Explanation": "; ".join(parts),
    })

# Sort by FarmScore descending
results.sort(key=lambda x: x["FarmScore"], reverse=True)

# ---- Helper: name-based categorization ----
def categorize(name):
    nl = name.lower()
    if any(w in nl for w in ["herb", "felweed", "dreamfoil", "mountain silversage", "plaguebloom",
                               "icecap", "liferoot", "goldensansam", "nightmare vine",
                               "terocone", "ancient lichen", "ragveil", "flame cap",
                               "sorrowmoss", "netherbloom", "mana thistle", "dreaming glory",
                               "bloodthistle", "fel lotus", "black lotus", "swiftthistle",
                               "stranglekelp", "khadgar's whisker", "sungrass", "goldthorn",
                               "firebloom", "purple lotus", "wild steelbloom", "bruiseweed",
                               "briarthorn", "mageroyal", "earthroot", "silverleaf", "peacebloom",
                               "arbor's", "alchemy", "whiptail", "cinderbloom", "heartblossom",
                               "stormvine", "azshara's veil", "twilight jasmine"]):
        return "Herbs"
    if any(w in nl for w in ["ore", "bar", "copper", "tin", "silver", "gold", "iron",
                               "mithril", "truesilver", "thorium", "fel iron", "adamantite",
                               "khorium", "eternium", "cobalt", "saronite", "titanium",
                               "elementium", "pyrium", "ghost iron", "trillium",
                               "blackrock", "smelt", "gem", "sapphire", "ruby", "emerald",
                               "topaz", "diamond", "crystal", "stone", "mining"]):
        return "Ore"
    if any(w in nl for w in ["cloth", "linen", "wool", "silk", "mageweave", "runecloth",
                               "netherweave", "frostweave", "embercloth", "cloth scav",
                               "cloth bolt", "bolt of"]):
        return "Cloth"
    if any(w in nl for w in ["leather", "hide", "skin", "scale", "scorpid", "basilisk",
                               "clefthoof", "knothide", "rugged", "thick", "heavy",
                               "light", "medium", "whelp", "dragon", "primal bat",
                               "windscale", "wild drake", "fel hide", "zhevra"]):
        return "Leather"
    if any(w in nl for w in ["enchant", "formula", "wand", "dust", "essence", "shard",
                               "crystal", "eternal", "void crystal", "dream shard",
                               "abyss", "arcane dust", "soul dust", "vision dust",
                               "greater", "lesser", "magic wand", "mystic wand",
                               "brilliant", "gleaming", "radiant", "shimmering",
                               "runed", "enchant weapon", "enchant chest", "enchant cloak",
                               "enchant bracer", "enchant boots", "enchant gloves"]):
        return "Enchanting"
    if any(w in nl for w in ["cook", "food", "fish", "meat", "soup", "stew", "bread",
                               "cheese", "feast", "banquet", "delight", "bisque",
                               "omelet", "chop", "steak", "rib", "loaf", "muffin"]) and \
       any(w in nl for w in ["recipe", "cook", "food", "fish", "meat", "soup"]) or \
       any(w in nl for w in ["cooked", "seasoned", "grilled", "roasted"]):
        return "Cooking"
    if any(w in nl for w in ["recipe:", "plans:", "pattern:", "schematic:", "design:",
                            "formula: elixir", "formula: potion", "formula: flas",
                            "goggles", "spellfire", "imbued netherweave",
                            "mooncloth", "soulcloth", "arcane weave", "shadoweave",
                            "frozen shadoweave", "whitemend", "spellstrike", "battlecast",
                            "blackstrike", "unyeilding", "bramblewood", "ironvine",
                            "flamebane", "felsteel", "khorium", "eternium"]):
        return "Crafted Gear"
    if any(w in nl for w in ["bag", "pack", "sack", "pouch", "quiver", "ammo pouch"]):
        return "Bags"
    # Generic reagents
    if any(w in nl for w in ["reagent", "elemental", "primal", "eternal", "mote",
                               "void", "arcane", "mana", "spirit", "shadow", "flame",
                               "water", "air", "earth", "life", "death", "frost",
                               "fel", "holy", "nature", "fire", "crystal"]):
        return "Reagents"
    return "Other"

cats = defaultdict(list)
for r in results:
    cat = categorize(r["name"])
    cats[cat].append(r)

# -- Printing --
def print_table(items, title, limit=100):
    print(f"\n{'='*120}")
    print(f"  {title}")
    print(f"{'='*120}")
    header = f"{'Rank':<5} {'Item Name':<45} {'Score':<8} {'Mkt Val':<14} {'Hist':<14} {'AvgPrice':<12} {'SaleRt':<8} {'Sold/D':<8} {'Trend':<8} Explanation"
    print(header)
    print("-"*120)
    for idx, r in enumerate(items[:limit]):
        rank = idx + 1
        mv_str = f"{r['MarketValue']:,.0f}"
        hist_str = f"{r['Historical']:,.0f}"
        asp_str = f"{r['AvgSalePrice']:,.0f}"
        name = r['name'][:44]
        expl = r['Explanation'][:35]
        print(f"{rank:<5} {name:<45} {r['FarmScore']:<8.2f} {mv_str:<14} {hist_str:<14} {asp_str:<12} {r['SaleRate']:<8.4f} {r['SoldPerDay']:<8.1f} {r['PriceTrend']:<8.2f} {expl}")

# 1. Top 100 overall
print_table(results, "TOP 100 OVERALL FARMING TARGETS", 100)

# Category top lists
for cat_name in ["Herbs", "Ore", "Cloth", "Leather", "Enchanting", "Cooking"]:
    if cat_name in cats:
        print_table(cats[cat_name], f"TOP {cat_name.upper()}", 20)

if "Reagents" in cats:
    print_table(cats["Reagents"], "TOP RARE MATERIALS", 20)

# ---- Analysis ----
print(f"\n{'='*120}")
print("  ANALYSIS")
print(f"{'='*120}")

# Items ideal for consistent gold/hour (high FarmScore, mid-high SPD, stable trend)
print("\n--- ITEMS IDEAL FOR CONSISTENT GOLD/HOUR ---")
consistent = [r for r in results[:200] if r["SoldPerDay"] > 0.05 and 0.8 <= r["PriceTrend"] <= 1.5 and r["SaleRate"] > 0.02]
for r in consistent[:15]:
    print(f"  {r['name']:<50} Score={r['FarmScore']:.1f}  SPD={r['SoldPerDay']:.2f}  SR={r['SaleRate']:.3f}  Value={r['MarketValue']:,.0f}")

# Items with huge demand (highest sold per day)
print("\n--- ITEMS WITH HUGE DEMAND (Highest Sold/Day) ---")
huge_demand = sorted(results[:300], key=lambda x: x["SoldPerDay"], reverse=True)[:15]
for r in huge_demand:
    print(f"  {r['name']:<50} SPD={r['SoldPerDay']:.2f}  SR={r['SaleRate']:.3f}  Value={r['MarketValue']:,.0f}  Score={r['FarmScore']:.1f}")

# Expensive items to avoid (high market value but low SPD/sale rate)
print("\n--- EXPENSIVE ITEMS TO AVOID (High Value, Low Turnover) ---")
avoid = [r for r in results if r["MarketValue"] > 5000000 and r["SoldPerDay"] < 0.01 and r["SaleRate"] < 0.005]
avoid.sort(key=lambda x: x["MarketValue"], reverse=True)
for r in avoid[:15]:
    print(f"  {r['name']:<50} Value={r['MarketValue']:,.0f}  SPD={r['SoldPerDay']:.3f}  SR={r['SaleRate']:.4f}  Score={r['FarmScore']:.1f}")

# Cheap items that sell fast
print("\n--- CHEAP ITEMS THAT SELL FAST (Low Value, High Turnover) ---")
cheap_fast = [r for r in results if r["MarketValue"] < 500000 and r["SoldPerDay"] > 0.5]
cheap_fast.sort(key=lambda x: x["SoldPerDay"], reverse=True)
for r in cheap_fast[:15]:
    print(f"  {r['name']:<50} Value={r['MarketValue']:,.0f}  SPD={r['SoldPerDay']:.2f}  SR={r['SaleRate']:.3f}  Score={r['FarmScore']:.1f}")

# Strongest markets overall
print("\n--- STRONGEST MARKETS (by category average FarmScore) ---")
cat_scores = []
for c, items in cats.items():
    if len(items) >= 3:
        avg = sum(r["FarmScore"] for r in items) / len(items)
        cat_scores.append((c, avg, len(items)))
cat_scores.sort(key=lambda x: x[1], reverse=True)
for c, avg, n in cat_scores[:15]:
    print(f"  {c:<20} AvgScore={avg:.1f}  Items={n}")
