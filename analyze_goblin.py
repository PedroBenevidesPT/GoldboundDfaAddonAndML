import csv
import math
import sys
from collections import defaultdict
from statistics import geometric_mean

# ─── LOAD ITEMS ───
items = []
with open(r"C:\FarmAnalysis\csv\items.csv", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        try:
            mv = float(r["marketValue"] or 0)
            hist = float(r["historical"] or 0)
            asp = float(r["avgSalePrice"] or 0)
            sr = float(r["saleRate"] or 0)
            spd = float(r["soldPerDay"] or 0)
            if spd > 0 and sr > 0 and mv > 0:
                items.append({
                    "name": r["name"],
                    "marketValue": mv, "historical": hist,
                    "avgSalePrice": asp, "saleRate": sr, "soldPerDay": spd,
                })
        except (ValueError, KeyError):
            pass

print(f"[ITEMS] {len(items)} with sales data", file=sys.stderr)

# ─── LOAD COMMODITIES ───
comms = []
with open(r"C:\FarmAnalysis\csv\commodities.csv", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        try:
            mv = float(r["marketValue"] or 0)
            bo = float(r["minBuyout"] or 0)
            rec = float(r["recent"] or 0)
            hist = float(r["historical"] or 0)
            if mv > 0 and bo > 0 and rec > 0:
                comms.append({
                    "name": r["name"],
                    "marketValue": mv, "minBuyout": bo,
                    "recent": rec, "historical": hist,
                })
        except (ValueError, KeyError):
            pass

print(f"[COMMODITIES] {len(comms)} items", file=sys.stderr)

# ─── HELPER: robust log-normalize (0-1) ───
def log_normalize(values, eps=1.0):
    """Log-transform then min-max normalize."""
    logs = [math.log(v + eps) for v in values]
    mn, mx = min(logs), max(logs)
    if mx == mn:
        return [0.5] * len(logs)
    return [(v - mn) / (mx - mn) for v in logs]

def robust_normalize(values):
    """99th-percentile cap then log-normalize."""
    s = sorted(values)
    cap = s[int(len(s) * 0.99)]
    capped = [min(v, cap) for v in values]
    return log_normalize(capped)

# ─── COMPUTE ITEMS FARMSCORE ───
# Goblin dimensions:
# 1. TurnoverLiquidity = log(1+SPD) x log(2+SR*100)  — sell speed
# 2. GoldThroughput = log(1 + SPD * avgSalePrice)     — actual gold/day
# 3. PriceStability = 1 / (1 + |log(market/hist)|)    — stable = 1, volatile = 0
# 4. TrendDirection = clamp(market/hist, 0.5, 2.0)    — penalty for crashing or spiking
# 5. MarginHealth = avgSalePrice / marketValue         — realistic pricing

liquidity_raw = []
throughput_raw = []
stability_raw = []
trend_raw = []
margin_raw = []

for r in items:
    spd = r["soldPerDay"]
    sr = r["saleRate"]
    mv = r["marketValue"]
    asp = r["avgSalePrice"]
    hist = r["historical"]

    # Liquidity: SPD x SaleRate in log space
    liq = math.log(1 + spd) * math.log(2 + sr * 100)
    liquidity_raw.append(liq)

    # Gold throughput
    thr = math.log(1 + spd * asp)
    throughput_raw.append(thr)

    # Price stability (1 = perfectly stable)
    if hist > 0:
        ratio = mv / hist
    else:
        ratio = 1.0
    stab = 1.0 / (1.0 + abs(math.log(max(ratio, 0.0001))))
    stability_raw.append(stab)

    # Trend direction (penalize both crashing and spiking)
    t = max(0.5, min(2.0, ratio))
    trend_raw.append(t)

    # Margin health
    if mv > 0:
        m = min(asp / mv, 1.0)
    else:
        m = 0.5
    margin_raw.append(m)

# Normalize each dimension
n_liq = robust_normalize(liquidity_raw)
n_thr = robust_normalize(throughput_raw)
n_stab = robust_normalize(stability_raw)
n_trend = robust_normalize(trend_raw)
n_margin = robust_normalize(margin_raw)

results = []
for i, r in enumerate(items):
    # Geometric mean of 5 dimensions → 0-100
    gm = geometric_mean([
        max(n_liq[i], 0.001),
        max(n_thr[i], 0.001),
        max(n_stab[i], 0.001),
        max(n_trend[i], 0.001),
        max(n_margin[i], 0.001),
    ])
    farmscore = gm * 100.0

    ratio = r["marketValue"] / max(r["historical"], 1)

    parts = []
    p_liq = n_liq[i]
    p_thr = n_thr[i]
    p_stab = n_stab[i]

    if p_liq > 0.7:
        parts.append("Extremely liquid")
    elif p_liq > 0.4:
        parts.append("Liquid")
    elif p_liq < 0.15:
        parts.append("Illiquid")

    if p_thr > 0.7:
        parts.append("high gold throughput")
    elif p_thr > 0.4:
        parts.append("decent gold throughput")
    elif p_thr < 0.15:
        parts.append("low gold throughput")

    if p_stab > 0.8:
        parts.append("very stable price")
    elif p_stab < 0.4:
        parts.append("volatile price")

    if ratio > 1.1:
        parts.append("trending up")
    elif ratio < 0.85:
        parts.append("trending down")

    results.append({
        "name": r["name"],
        "FarmScore": round(farmscore, 2),
        "Liquidity": round(p_liq * 100, 2),
        "GoldThroughput": round(p_thr * 100, 2),
        "Stability": round(p_stab * 100, 2),
        "TrendDirection": round(n_trend[i] * 100, 2),
        "MarketValue": r["marketValue"],
        "Historical": r["historical"],
        "AvgSalePrice": r["avgSalePrice"],
        "SaleRate": r["saleRate"],
        "SoldPerDay": r["soldPerDay"],
        "PriceTrend": round(ratio, 4),
        "Explanation": "; ".join(parts),
    })

results.sort(key=lambda x: x["FarmScore"], reverse=True)

# ─── COMPUTE COMMODITIES SCORE ───
# Dimensions:
# 1. SpreadTightness = 1 / (1 + |log(recent/minBuyout)|)  — tight = active
# 2. TrendStability = clamp(recent/historical, 0.5, 2.0)   — not crashing
# 3. ValueScore = log(1 + marketValue)                      — absolute value
# 4. BuyoutConfidence = minBuyout / marketValue             — realistic pricing

c_spread = []
c_trend = []
c_val = []
c_confidence = []

for r in comms:
    rec = r["recent"]
    bo = r["minBuyout"]
    mv = r["marketValue"]
    hist = r["historical"]

    # Spread tightness
    if bo > 0:
        spread = 1.0 / (1.0 + abs(math.log(max(rec / bo, 0.0001))))
    else:
        spread = 0.5
    c_spread.append(spread)

    # Trend stability
    if hist > 0:
        ratio = rec / hist
    else:
        ratio = 1.0
    t = max(0.5, min(2.0, ratio))
    c_trend.append(t)

    # Log value
    c_val.append(math.log(1 + mv))

    # Buyout confidence
    if mv > 0:
        conf = min(bo / mv, 1.0)
    else:
        conf = 0.5
    c_confidence.append(conf)

n_cspread = robust_normalize(c_spread)
n_ctrend = robust_normalize(c_trend)
nc_val = robust_normalize(c_val)
nc_conf = robust_normalize(c_confidence)

com_results = []
for i, r in enumerate(comms):
    gm = geometric_mean([
        max(n_cspread[i], 0.001),
        max(n_ctrend[i], 0.001),
        max(nc_val[i], 0.001),
        max(nc_conf[i], 0.001),
    ])
    cs = gm * 100.0

    ratio = r["recent"] / max(r["historical"], 1)
    parts = []
    if n_cspread[i] > 0.7:
        parts.append("Tight spread, active market")
    elif n_cspread[i] < 0.2:
        parts.append("Wide spread, thin market")

    if n_ctrend[i] > 0.7:
        parts.append("stable trend")
    elif n_ctrend[i] < 0.3:
        parts.append("unstable trend")

    if ratio > 1.1:
        parts.append("trending up")
    elif ratio < 0.85:
        parts.append("trending down")

    com_results.append({
        "name": r["name"],
        "CommodityScore": round(cs, 2),
        "SpreadTightness": round(n_cspread[i] * 100, 2),
        "TrendStability": round(n_ctrend[i] * 100, 2),
        "ValueScore": round(nc_val[i] * 100, 2),
        "BuyoutConfidence": round(nc_conf[i] * 100, 2),
        "MarketValue": r["marketValue"],
        "MinBuyout": r["minBuyout"],
        "Recent": r["recent"],
        "Historical": r["historical"],
        "PriceTrend": round(ratio, 4),
        "Explanation": "; ".join(parts),
    })

com_results.sort(key=lambda x: x["CommodityScore"], reverse=True)

# ─── CATEGORIZATION ───
def categorize(name):
    nl = name.lower()
    if any(w in nl for w in ["herb", "felweed", "dreamfoil", "silversage", "plaguebloom",
        "icecap", "liferoot", "goldensansam", "nightmare vine", "terocone", "ancient lichen",
        "ragveil", "flame cap", "sorrowmoss", "netherbloom", "mana thistle", "dreaming glory",
        "bloodthistle", "fel lotus", "black lotus", "swiftthistle", "stranglekelp",
        "khadgar's whisker", "sungrass", "goldthorn", "firebloom", "purple lotus",
        "wild steelbloom", "bruiseweed", "briarthorn", "mageroyal", "earthroot",
        "silverleaf", "peacebloom", "whiptail", "cinderbloom", "heartblossom",
        "stormvine", "azshara's veil", "twilight jasmine", "mycobloom", "sanguithorn",
        "argentleaf", "nightshade", "starflower", "fjarnskaggl", "rising glory",
        "azeroot", "marrowroot", "talador orchid", "bismuth", "briarthorn"]):
        return "Herbs"
    if any(w in nl for w in ["ore", "bar", "copper", "tin", "silver", "gold", "iron",
        "mithril", "truesilver", "thorium", "fel iron", "adamantite", "khorium", "eternium",
        "cobalt", "saronite", "titanium", "elementium", "pyrium", "ghost iron", "trillium",
        "blackrock", "smelt", "gem", "sapphire", "ruby", "emerald", "topaz", "diamond",
        "crystal", "stone", "mining", "osmenite", "laestrite", "umbral tin", "refulgent",
        "sin'dorei", "felslate", "leystone", "kyparite"]):
        return "Ore"
    if any(w in nl for w in ["cloth", "linen", "wool", "silk", "mageweave", "runecloth",
        "netherweave", "frostweave", "embercloth", "weavercloth", "windwool", "wildercloth",
        "bright linen", "embersilk", "shrouded cloth", "tidespray linen", "lightless silk",
        "sunfire silk", "shal'dorei silk"]):
        return "Cloth"
    if any(w in nl for w in ["leather", "hide", "scale", "scorpid", "basilisk", "clefthoof",
        "knothide", "rugged", "thick leather", "void-tempered", "shimmerscale",
        "stormcharged leather", "dredged leather", "desolate leather", "fiendish leather",
        "tempest hide", "raw beast hide", "mist-touched"]):
        return "Leather"
    if any(w in nl for w in ["enchant", "formula: enchant", "wand", "dust", "essence", "shard",
        "crystal", "eternal", "void crystal", "dream shard", "arcane dust", "soul dust",
        "vision dust", "eversinging dust", "radiant shard", "infinite dust", "storm dust",
        "greater planar", "greater eternal", "lesser magic", "soul essence", "strange dust"]):
        return "Enchanting"
    if any(w in nl for w in ["meat", "fish", "food", "soup", "stew", "feast", "banquet",
        "delight", "bisque", "omelet", "steak", "rib", "roast", "mystery meat",
        "tender wolf", "raptor egg", "clam meat", "white spider", "bristle whisker",
        "gamy rib", "lean shank", "catfish", "savory deviate", "deviate fish",
        "maybe meat", "questionable meat", "crescent saberfish", "cooking spirit",
        "chunk of boar", "bear meat", "tiger meat", "meaty haunch", "lynxfish",
        "arcan wyrmfish", "raw sagefish", "red wolf meat", "shimmer spinefish"]):
        return "Cooking"
    if any(w in nl for w in ["potion", "flask", "elixir", "phial", "cauldron"]):
        return "Potions"
    if any(w in nl for w in ["bag", "pack", "sack", "pouch", "quiver", "ammo pouch",
        "netherweave bag", "runecloth bag", "frostweave bag", "embercloth bag"]):
        return "Bags"
    if any(w in nl for w in ["recipe:", "plans:", "pattern:", "schematic:", "design:",
        "formula:", "codex:", "libram:", "tome:", "book:", "crafted", "impossible",
        "goggles", "spellfire", "mooncloth", "soulcloth", "shadoweave", "whitemend",
        "spellstrike", "felsteel", "khorium", "eternium", "imbued netherweave"]):
        return "Crafted Gear"
    if any(w in nl for w in ["reagent", "elemental", "primal", "eternal", "mote",
        "void", "arcane", "mana", "spirit", "shadow", "flame", "water", "air",
        "earth", "life", "death", "frost", "fel", "holy", "nature", "fire",
        "rousing", "volatile", "sorcerous", "earth garnish", "mana lily",
        "saltwater potion", "death blossom", "augment rune", "spirit of harmony"]):
        return "Reagents"
    if any(w in nl for w in ["glyph", "ink", "pigment", "parchment", "scroll", "off-hand",
        "book", "tome"]):
        return "Glyphs/Inscription"
    if any(w in nl for w in ["gem", "ruby", "sapphire", "emerald", "topaz", "diamond",
        "zephyrite", "noxious seed"]):
        return "Gems"
    return "Other"

cats = defaultdict(list)
for r in results:
    cat = categorize(r["name"])
    cats[cat].append(r)

# ─── PRINTING ───
def print_table(items, title, score_key="FarmScore", limit=100):
    print(f"\n{'='*120}")
    print(f"  {title}")
    print(f"{'='*120}")
    score_lbl = "Score"
    hdr = f"{'Rank':<5} {'Item Name':<45} {score_lbl:<8} {'Mkt Val':<14} {'Hist':<14} {'AvgPrice':<12} {'SaleRt':<8} {'Sold/D':<8} {'Trend':<8} Explanation"
    print(hdr)
    print("-"*120)
    for idx, r in enumerate(items[:limit]):
        rank = idx+1
        mv = f"{r['MarketValue']:,.0f}"
        hist = f"{r['Historical']:,.0f}"
        asp = f"{r['AvgSalePrice']:,.0f}"
        name = r['name'][:44]
        expl = r['Explanation'][:40]
        print(f"{rank:<5} {name:<45} {r[score_key]:<8.2f} {mv:<14} {hist:<14} {asp:<12} {r['SaleRate']:<8.4f} {r['SoldPerDay']:<8.1f} {r['PriceTrend']:<8.2f} {expl}")

# ─── 1. TOP 100 ITEMS ───
print_table(results, "TOP 100 FARMING TARGETS [log + geometric mean]", "FarmScore", 100)

# ─── CATEGORY RANKINGS ───
for cat_name in ["Herbs", "Ore", "Cloth", "Leather", "Enchanting", "Cooking", "Potions", "Bags", "Gems"]:
    if cat_name in cats:
        print_table(cats[cat_name], f"TOP {cat_name.upper()}", "FarmScore", 15)

if "Reagents" in cats:
    print_table(cats["Reagents"], "TOP RARE MATERIALS", "FarmScore", 15)

# ─── 2. TOP COMMODITIES ───
print(f"\n{'='*120}")
print(f"  TOP 50 COMMODITIES [log + geometric mean]")
print(f"{'='*120}")
hdr = f"{'Rank':<5} {'Item Name':<45} {'Score':<8} {'Mkt Val':<14} {'MinBuyout':<14} {'Recent':<12} {'Spread':<8} {'Trend':<8} Explanation"
print(hdr)
print("-"*120)
for idx, r in enumerate(com_results[:50]):
    rank = idx+1
    mv = f"{r['MarketValue']:,.0f}"
    bo = f"{r['MinBuyout']:,.0f}"
    rec = f"{r['Recent']:,.0f}"
    name = r['name'][:44]
    expl = r['Explanation'][:40]
    print(f"{rank:<5} {name:<45} {r['CommodityScore']:<8.2f} {mv:<14} {bo:<14} {rec:<12} {r['SpreadTightness']:<8.1f} {r['PriceTrend']:<8.2f} {expl}")

# ─── ANALYSIS ───
print(f"\n{'='*120}")
print("  ANALYSIS")
print(f"{'='*120}")

# Items ideal for consistent gold/hour
print("\n--- BEST FOR CONSISTENT GOLD/HOUR (high liquidity + throughput + stability) ---")
best_consistent = [r for r in results[:300] if r["Liquidity"] > 40 and r["Stability"] > 40 and r["GoldThroughput"] > 30]
for r in best_consistent[:15]:
    print(f"  {r['name']:<50} Score={r['FarmScore']:.1f}  Liq={r['Liquidity']:.0f}  Thr={r['GoldThroughput']:.0f}  Stab={r['Stability']:.0f}  SPD={r['SoldPerDay']:.1f}  MV={r['MarketValue']:,.0f}")

# Items with huge demand
print("\n--- HUGE DEMAND (highest gold throughput) ---")
big_demand = sorted([r for r in results if r["SoldPerDay"] > 1000], key=lambda x: x["GoldThroughput"], reverse=True)[:15]
for r in big_demand:
    gpd = r["SoldPerDay"] * r["AvgSalePrice"]
    print(f"  {r['name']:<50} Score={r['FarmScore']:.1f}  SPD={r['SoldPerDay']:.1f}  x {r['AvgSalePrice']:,.0f} = {gpd:,.0f} g/d  Liq={r['Liquidity']:.0f}")

# Expensive items to avoid
print("\n--- EXPENSIVE ITEMS TO AVOID (high MV, illiquid, unstable) ---")
avoid = sorted([r for r in results if r["MarketValue"] > 10000000 and r["Liquidity"] < 20], key=lambda x: x["MarketValue"], reverse=True)[:15]
for r in avoid:
    print(f"  {r['name']:<50} MV={r['MarketValue']:,.0f}  Liq={r['Liquidity']:.0f}  Stab={r['Stability']:.0f}  Score={r['FarmScore']:.1f}")

# Cheap fast sellers
print("\n--- CHEAP ITEMS THAT SELL FAST (low MV, extreme liquidity) ---")
cheap_fast = sorted([r for r in results if r["MarketValue"] < 20000 and r["SoldPerDay"] > 500], key=lambda x: x["Liquidity"], reverse=True)[:15]
for r in cheap_fast:
    print(f"  {r['name']:<50} MV={r['MarketValue']:,.0f}  SPD={r['SoldPerDay']:.1f}  Liq={r['Liquidity']:.0f}  Score={r['FarmScore']:.1f}")

# Strongest markets
print("\n--- STRONGEST MARKETS (by median FarmScore, min 5 items) ---")
cat_stats = []
for c, items_list in cats.items():
    if len(items_list) >= 5:
        scores = [r["FarmScore"] for r in items_list]
        scores.sort()
        median = scores[len(scores)//2]
        avg = sum(scores)/len(scores)
        top10_avg = sum(r["FarmScore"] for r in items_list[:10]) / min(10, len(items_list))
        cat_stats.append((c, avg, median, top10_avg, len(items_list)))
cat_stats.sort(key=lambda x: x[3], reverse=True)
print(f"  {'Category':<25} {'Avg':<8} {'Median':<8} {'Top10Avg':<10} {'Count':<6}")
print(f"  {'-'*60}")
for c, avg, med, t10, n in cat_stats:
    print(f"  {c:<25} {avg:<8.2f} {med:<8.2f} {t10:<10.2f} {n:<6}")

# ─── COMMODITIES ANALYSIS ───
print(f"\n--- TOP COMMODITIES BY MARKET POTENTIAL ---")
top_coms = sorted(com_results[:100], key=lambda x: x["MarketValue"] * (x["SpreadTightness"]/100), reverse=True)[:15]
for r in top_coms:
    print(f"  {r['name']:<50} Score={r['CommodityScore']:.1f}  MV={r['MarketValue']:,.0f}  Spread={r['SpreadTightness']:.0f}  Trend={r['PriceTrend']:.2f}")
