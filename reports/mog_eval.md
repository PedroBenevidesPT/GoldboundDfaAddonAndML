# Transmog model: what was measured, and what was not

## 1. Cost model

- Deposit formula `max(0.15 x vendor x qty, 100c)` explains **343/404** (84.9%) real sale rows.
- The 5% auction cut explains **339/577** (58.8%).
- Trustworthy: **True**.

The previous constants (`5% of price`, `1g` floor, `300% of vendor` cap) explain **0 of 404** rows.

## 2. Price forecast, chronological

Baseline is the last observed market value; the model is the last observed wall (`min_buyout`), which was the valuation's fallback before this measurement. Both scored on the same target row.

**This is why the fallback changed.** The wall loses on almost every item, so it is no longer used as a value; the market average is, and the wall is kept as a competition signal only.

| subset | n | baseline median err | model median err | model wins |
|---|---|---|---|---|
| all items (6 realms) | 59151 | 0.4% | 11.9% | 2.8% |
| transmog only (6 realms) | 41777 | 0.4% | 11.3% | 2.9% |

## 3. Strategy net gold

**Status: not_measurable.** no listing-level outcome feed for transmog: neither the account's sales history nor the TSM snapshot series records whether a listing at a given price sold.

Would require:
- per-listing outcomes for transmog items (posted, sold or expired, and at what price)
- at least several weeks of history so the slow tail is observed rather than censored
- both sides of each sale, so the clearing price is a transaction price and not an ask

## Account sale census

- Distinct items sold: **215**
- Of which transmog: **31**
- Categories: collectible=22, niche=9, plain=0, restricted=0, unknown=184