# Trading Log Review: March 27 - April 3, 2026

## Logs Reviewed
| Log File | Period | Duration |
|---|---|---|
| `bot.2026-03-27_10-29-47_919436.log` | Mar 27 - Mar 31 | ~4 days |
| `bot.2026-03-31_07-09-21_922788.log` | Mar 31 - Apr 2 | ~2.5 days |
| `bot.2026-04-02_09-18-25_366154.log` | Apr 2 - Apr 3 | ~26.5 hours |

---

## Executive Summary

Across this week's logs, the bot executed trades on **8 unique tickers** (SGML, NNE, SPCE, IMSR, OMER, PLAY, RZLV, SIDU). The overall performance was poor: **only 2 winning trades vs. 5 losses**, with several positions still open/unresolved at log boundaries. More critically, **three bugs of increasing severity** were identified that undermine the bot's risk management and signal quality.

---

## Performance Breakdown

### Log 1: Mar 27-31

| Ticker | Action | Outcome |
|---|---|---|
| SGML | 3 partial take-profits (+30-33%), then re-entry | Stop-loss at -8.5% on accumulated 297-share position. **Net likely negative.** |
| IMSR | Buy 89 shares | Stop-loss at -8.0% |
| NNE | Buy 53 shares @ $25.05 | No exit (carried forward) |
| SPCE | Buy 244 + 236 shares | No exit (carried forward) |

**Verdict:** The SGML take-profits were excellent, but the bot gave back all gains (and more) by repeatedly re-entering the same fading trade. IMSR was a clean loss.

### Log 2: Mar 31 - Apr 2

| Ticker | Action | Outcome |
|---|---|---|
| SPCE | Carried position (480 shares) | Stop-loss at -8.3% |
| SGML | Carried position (74 shares) | Stop-loss at -9.4% |
| OMER | Buy 76 shares @ $12.76 | Stop-loss at -8.4% |
| PLAY | Buy 74 shares @ $13.02 | No exit (still held) |
| RZLV | Buy 208 shares @ $3.09 | No exit (still held) |

**Verdict:** Zero wins. Every closed trade hit a stop-loss. The bot hit its 5-position cap and was locked out of scanning for ~5 hours.

### Log 3: Apr 2-3

| Ticker | Action | Outcome |
|---|---|---|
| SIDU | Buy 214 shares @ $3.01 | No exit (log cut off) |

**Verdict:** Only 1 signal detected across 109 scans. However, the signal triggered **22 duplicate buy orders** due to a critical bug (see below).

---

## Bugs Found (Ranked by Severity)

### BUG 1 - CRITICAL: Duplicate Order Submission
**Log:** `bot.2026-04-02` | **Impact:** Extreme

The bot submitted **22 identical BUY orders** for SIDU (214 shares @ $3.01 each) over ~2 hours. Every 5-minute scan cycle re-detected the same signal and placed a new order without checking for existing open orders or positions.

**If all orders filled:** 4,708 shares (~$14,170) instead of the intended 214 shares (~$644). This would be **~22x the intended position size** and blow through every risk limit.

**Root cause:** The `should_trade()` / order-submission logic does not check whether a position or pending order already exists for the ticker before submitting.

**Fix:** Before placing any order, query open orders and current positions for that ticker. Skip if one already exists.

---

### BUG 2 - HIGH: Stale `prev_close` Reference Data
**Log:** `bot.2026-04-02` | **Impact:** High

On April 3, dozens of tickers showed current prices ~50% below their `prev_close` values (e.g., SPY $316 vs prev_close $655, OXY $29 vs $62). The `prev_close` data was never refreshed between trading days, causing the price-change % calculation to be completely wrong.

**Impact:** The +5% entry signal threshold becomes meaningless. The bot may either:
- Miss real signals (if prices are above stale close, the change looks negative)
- Generate false signals (if prices are below stale close, everything looks like a crash)

**Fix:** Ensure `prev_close` is refreshed at market open from the actual prior day's close, not carried from the initial watchlist load.

---

### BUG 3 - MEDIUM: Signal Re-Entry Chasing (SGML Pattern)
**Log:** `bot.2026-03-27` | **Impact:** Medium

After SGML's initial take-profit exits (+30-33%), the bot immediately re-entered because the signal remained "hot" (the stock was still up significantly on the day). It accumulated a 297-share position ($4,000 = 43% of equity) before hitting stop-loss at -8.5%. After that stop-loss, it **re-entered again**.

**Impact:** A single-stock position reached 43% of equity, far exceeding the 20% per-position cap. The multiple re-entries into a fading move turned a profitable trade into a net loss.

**Fix:** Implement a cooldown period after exiting a position (e.g., skip ticker for 2-4 hours after exit). Also enforce the 20% equity cap on accumulated position value, not just initial order size.

---

## Additional Issues

### Position Max-Out Lockout (Medium)
In the Mar 31 log, hitting the 5-position cap caused the bot to skip scanning entirely for **62 consecutive cycles (~5 hours)**. During this time, losing positions continued to bleed without the bot being able to rotate into better opportunities.

**Recommendation:** Allow scanning even at max positions so the bot can identify better setups. Consider closing the weakest position if a significantly stronger signal appears.

### OMER Double-Sell Race Condition (Low)
After selling 76 shares of OMER on stop-loss, the bot detected the stop trigger again and sold a residual 4 shares in separate orders. This suggests position reconciliation has a small timing gap.

### Fintel Access Denied (Low)
The Fintel Short Squeeze Leaderboard returned "access denied" during overnight scans (03:00 on Apr 1 and Apr 3). This means the squeeze screening data source was intermittently unavailable.

**Recommendation:** Verify subscription status. Add a fallback data source for short interest / squeeze signals.

---

## Key Metrics Summary

| Metric | Value |
|---|---|
| Total tickers traded | 8 |
| Closed winning trades | 2 (SGML partials) |
| Closed losing trades | 5 |
| Win rate (closed trades) | 29% |
| Average loss | -8.5% |
| Signals per scan cycle | 0-2 (very selective) |
| Scans per log file | ~110 |
| Critical bugs found | 1 |
| High-severity bugs | 1 |
| Medium-severity bugs | 1 |

---

## Recommendations (Priority Order)

1. **Fix duplicate order submission immediately** -- this is a portfolio-destroying bug
2. **Fix stale `prev_close` data refresh** -- signals are unreliable without correct reference prices
3. **Add re-entry cooldown + enforce position size cap on accumulation** -- prevent the SGML chasing pattern
4. **Allow scanning at max positions** -- don't lock out for hours when all slots are filled
5. **Verify Fintel subscription** -- ensure data source availability
6. **Add position reconciliation delay** -- prevent double-sell on residual shares
