# Strategy Evidence Brief & Spec Audit

Purpose: canonical-rule audit of the seven frozen backtest specs against the published
versions, with the performance evidence as actually reported in the literature.
Researched 2026-10-21. Severity flags: **MATERIAL** = changes what the strategy is or
what the evidence supports; **MODERATE** = changes fills/selection noticeably;
**MINOR** = close enough; **MATCHES** = as published.

**Cross-cutting warning for this build:** in a 120-day window, the two daily strategies
cannot validate — Donchian on one market yields roughly 2–6 trades, RSI(2) roughly
8–15. Their published evidence is portfolio-scale and decade-scale. The five intraday
strategies will produce statistically usable trade counts (100+); the two daily ones
will produce anecdotes. Rank accordingly when reading results.

---

## 1. Opening Range Breakout (ORB)

**Canonical versions (two different animals share this name):**

- **Toby Crabel, *Day Trading with Short Term Price Patterns and Opening Range
  Breakout* (1990).** Crabel's ORB is *not* a fixed-time range. His trigger levels are
  the day's open ± the **Stretch** — the 10-day average of the smaller of (high − open)
  and (open − low). A buy stop sits at open + Stretch, a sell stop at open − Stretch
  (OCO); the first stop hit is the trade, the unfilled stop becomes the protective
  stop, and the standard exit is the close. Crabel's own research found **the earlier
  in the session the stop triggers, the more likely the trade is profitable at the
  close**. His famous pattern filters (NR4/NR7, inside days) condition the setup.
- **Modern fixed-range ORB, quantified by Carlo Zarattini & Andrew Aziz, "Can Day
  Trading Really Be Profitable?" (2023, rev. 2024).** On QQQ: if the first 5-minute
  candle closes up (down), buy (short) at the start of the second interval — i.e.,
  enter at 9:35 in the first candle's direction. Stop at the opposite extreme of the
  first candle. **Profit target 10R**, else exit at the close. One trade/day, 1% risk,
  up to 4× leverage.

**Spec audit:**
- **MATERIAL — mislabeled.** The frozen spec (fixed 15/30-min range) is the modern
  generic ORB, not Crabel's. Nothing in it uses the Stretch. Rename it in code/results
  ("fixed-range ORB") so the evidence trail stays honest.
- **MATERIAL — 2R target.** The only modern quantified version with strong published
  results uses a 10R target / exit-at-close. Its edge is payoff asymmetry — a minority
  of trades run all day. A 2R cap amputates exactly the right tail that carries the
  published P&L. Expect the tested version to behave materially worse than the paper.
- **MODERATE — close-confirmed entry.** Both canonical versions enter on stop orders
  *at* the level, accepting breakout-fill slippage in exchange for immediacy, and
  Crabel's central finding (earlier = better) argues against waiting for a 1-min close
  beyond a 15/30-min range and for a noon entry window full of late, weaker triggers.
- **MATCHES — stop at the opposite edge** is exactly Zarattini & Aziz's stop.

**Evidence:** Zarattini & Aziz, QQQ 2016–2023 (two bear markets): ~33% annualized
alpha **net of commissions but with no slippage/spread modeled** (fills at recorded
prices); a TQQQ implementation returned +1,484% total vs +169% buy-and-hold. Follow-up
(Zarattini, Barbon & Aziz, Swiss Finance Institute, 2024) on ~7,000 US stocks
filtered to "stocks in play": top-20 portfolio > +1,600% net performance, Sharpe 2.81,
36% annualized alpha, stated net of transaction costs; 15/30/60-min ranges were also
tested, 5-min strongest. ORB on index futures specifically has also been examined
(Syu, Lin & Wu, 2019) with results sensitive to entry timing and costs. Win rate is a
minority by construction (10R target); the published edge is avg-win/avg-loss, not
accuracy. All of it is one modern sample (2016–2023), mostly US equities/ETFs.

**Top gotcha:** cost/fill realism. The headline study charges commissions only. A
close-confirmed entry in a fast break can fill many ticks past the level, and with a
2R target the payoff distribution no longer resembles the validated one.

---

## 2. Previous-Day Sweep ("Turtle Soup")

**Canonical version: Laurence Connors & Linda Raschke, *Street Smarts* (1995).**
A **daily-bar** pattern, and — as suspected — it is keyed to **20-day extremes, not
the prior day**. Setup: price makes a new 20-bar high/low, and the *previous* 20-bar
extreme must have formed **at least 3 bars earlier**; the setup day closes at/through
the old extreme. Entry: **next session**, a stop order placed at the old extreme,
filled as price re-enters the range (the "Turtle Soup Plus One" variant waits one
additional day). Stop just beyond the new extreme. Exits are quick — first profitable
close / 2–3 days, trailed. It was designed to fade the Turtle trend system's entries.

**Spec audit — MATERIAL on every axis:**
- Reference level: prior-day RTH high/low instead of a 20-day extreme — a different,
  much shallower liquidity pool, restocked daily.
- No ≥3-day age requirement on the reference extreme (canonical, to ensure the level
  is stale/loaded with stops).
- Timing: canonical entry is next-day via stop order; the spec fades intraday on a
  1-min close back inside, the same day.
- Exit: fixed 2R target vs the book's rapid profit-taking.

**Verdict:** the frozen spec is a *prior-day liquidity sweep* (the modern ICT-style
pattern), not Turtle Soup. That's a legitimate thing to test — but it must not inherit
Turtle Soup's name or reputation. (If the published pattern is wanted, it is a daily
strategy: 20-day extreme, next-day stop entry — and in a 120-day window it will
generate very few trades.)

**Evidence:** thin. *Street Smarts* offers no audited statistics; the pattern's fame
rests on Raschke's live-trading reputation. The main independent verification
(OxfordStrat: 42 futures markets, 1980–2011, including a run with $100 round-turn
costs) assigns the mechanical Turtle Soup its lowest robustness rating (D).

**Top gotcha:** fading breakouts is short-volatility. The sweeps that *don't* reverse
are genuine breakouts and run immediately; the obvious stop (just beyond the extreme)
sits exactly where the crowd's stops sit.

---

## 3. VWAP Mean Reversion (±2σ fade)

**Canonical status:** there is no single published system. The construction is
standard: session-anchored VWAP = Σ(typical price × volume) / Σvolume, with bands at
VWAP ± kσ for k = 1, 2, 3. **The frozen spec's σ definition — volume-weighted
standard deviation of price around VWAP — is the mainstream band construction** (the
other convention seen in some platforms, rolling σ of bar prices, is the outlier).
Fading 2σ extremes back toward VWAP on rotational days is institutional-desk lore,
formalized in practitioner writing (e.g., Brian Shannon's VWAP work) and always
paired with **day-type judgment**: fade on range days, never on trend days.

**Spec audit:**
- **MATCHES** on construction (session VWAP, ±2σ, deviation-based σ).
- **MODERATE — no day-type gate.** Canonical practice conditions the fade on a
  rotational day (flat VWAP, price oscillating across it). The spec's only concession
  is the 10:30 start (sensible — early σ is estimated off few bars and bands are
  unstable). On a one-way day this spec will repeatedly fade into a moving train.
- **MINOR/MODERATE — target = full VWAP.** Common practice takes partials at the 1σ
  band; demanding full reversion to VWAP lowers win rate for a better payoff. Defensible,
  but know it trades accuracy for R.
- Stop "beyond the extreme" is the usual convention (often the +3σ line or the sweep
  high/low) — implementable, but it makes R vary trade to trade.

**Evidence:** no peer-reviewed or audited performance record exists for this rule set.
Practitioner/blog win-rate claims of 60–70% are unverified. One large systematic
retail study of VWAP rules on ES ("Everyone Uses VWAP Wrong," 400k+ parameter
combinations, Bonferroni-corrected) found that band *breakouts* predict reversal
rather than continuation — directionally supportive of fading extremes — while most
other naive VWAP rules lost significantly. Treat as suggestive, single-source.

**Top gotcha:** adverse selection. Excursions beyond 2σ on genuine trend days extend
to 3σ+ and stay there; the strategy's bad days are concentrated, one-directional, and
tend to cluster on news days — precisely when 1-min fills are worst.

---

## 4. VWAP Trend Pullback

**Canonical status:** practitioner synthesis, mainly Brian Shannon (*Technical
Analysis Using Multiple Timeframes*, 2008; Alphatrends): on a trend day, price holds
above a rising VWAP; buy pullbacks to VWAP; the trade is wrong when price gains
acceptance back below VWAP. There is **no canonical quantified rule set and no
published win-rate/payoff statistics** — any number attached to this pattern in
marketing material is invented.

**Spec audit:** the frozen spec is a reasonable mechanical distillation (trend
declared at 10:30; rising = VWAP at 10:30 above VWAP at 10:00; first qualifying
pullback; trigger = first 1-min close above the prior bar's high — a standard trigger
bar). Two flags:
- **MODERATE — stop placement.** Stop = VWAP − 0.5×ATR(14, 1m) anchors risk to VWAP,
  not to the entry. Risk per trade therefore varies with how far above VWAP the
  trigger fires. That is a defensible design (the level is the thesis), but R is not
  constant by construction — size accordingly.
- **Evidence flag (not a rule error):** the large ES statistical study cited in §3
  found the naive version of this exact idea — long while price is above a rising
  VWAP — had a **statistically significant negative edge** in its sample. The spec's
  extra structure (single first pullback, trigger bar, morning-only) may rescue it,
  but the prior from the only big-sample test is negative.

**Top gotcha:** crowded level. VWAP touches on trend days are where every intraday
desk is bidding; fills at the touch are competitive, and the first pullback is the
good one — quality degrades fast on the second and third, which the spec correctly
excludes by taking only the first.

---

## 5. Initial Balance Breakout

**Canonical version: J. Peter Steidlmayer's Market Profile; Jim Dalton, *Mind Over
Markets* (1993) and *Markets in Profile* (2007).** The Initial Balance is the range of
the **first hour** of the session — the first two 30-min TPO brackets (9:30–10:30 ET
for equity index futures). **Spec MATCHES on the IB definition.** But in the canon the
IB is *context*, not a system: Dalton classifies day types (normal, normal variation,
trend, neutral, nontrend) and reads range extension beyond the IB as evidence the
longer-timeframe trader is active. No mechanical "break IB, stop at midpoint, target
2R" system exists in Steidlmayer or Dalton.

**Measured statistics (the real canonical evidence):** NQ Stats (10 years of NQ):
one IB extreme is broken on ~96% of days (≈83% before noon), **both** extremes on
only ~30%; and there is a documented directional tilt the spec ignores — when the IB
closes in its upper half, the IB *high* breaks ~82% of the time; lower-half close →
low breaks ~76%. ES counts compiled by practitioners (MicrosTrader) are near-identical
(~96% one side, ~30% both, ~2% neither).

**Spec audit:**
- **MODERATE — symmetric first-break entry.** The published statistics support a
  *directionally conditioned* break (favor the side the IB closed toward), not
  whichever side breaks first. As specified, expect ~30% of days (both-sides days) to
  whipsaw the first break.
- **MINOR — stop at IB midpoint, 2R target.** Midpoint stop is a practitioner
  convention, and 2R from the IB edge with a half-IB stop ≈ the 100% IB extension,
  which is the canonical first target zone. Coherent, just not from the books.
- **MINOR — break-bar range ≥ 0.5×ATR filter and close-confirmed entry** are
  pragmatic additions; canonical range extension is a trade *through* the level.
  Note what the spec omits from canon: **IB width conditioning** — Dalton's framework
  expects narrow IBs to produce range-expansion days and wide IBs rotation. The spec
  trades both identically.
- **Context flag:** 9:30–10:30 ET is the equity-index IB. If this is ever run on gold,
  the Market Profile tradition anchors to the (old) COMEX floor morning, not the equity
  open — 9:30 ET IB on GC is a modern convention, not canon.

**Evidence:** the break-frequency statistics above are genuinely measured and
replicated across NQ/ES — but "one side breaks 96% of days" is not itself an edge;
direction and follow-through are the trade, and no published P&L study exists for a
mechanical IB breakout with these rules.

**Top gotcha:** both-sides days (~30%) plus wide-IB days, where a midpoint stop is an
enormous risk unit. Without width conditioning, position size must absorb IBs of very
different widths.

---

## 6. Donchian Trend (daily, 20/10)

**Canonical version: Richard Donchian's channel breakout, as operationalized by the
Turtle program — *The Original Turtle Trading Rules* (Curtis Faith, 2003), System 1.**
Enter when price exceeds by **one tick** the high/low of the preceding **20 days**;
exit when price touches the opposite **10-day** channel; initial stop **2N** where N is
the 20-day ATR; units sized off N; **add (pyramid) one unit every ½N** up to 4 units,
stops ratcheted; and System 1 **skips** a breakout if the previous System 1 breakout
in that market was a winner (the 55-day System 2 breakout is the failsafe so big moves
aren't missed entirely).

**Spec audit:**
- **MATCHES:** 20-day entry channel, 10-day opposite exit channel, 2×ATR stop
  (ATR(14) vs the Turtles' 20-day N — MINOR difference).
- **MATERIAL — entry timing.** The Turtles entered **intraday, the moment the level
  traded through** — the rules explicitly say they did *not* wait for the close or the
  next open. The spec (enter next open after a *close* beyond the channel) is a
  close-confirmed variant: it pays the overnight gap, forfeits the intraday portion of
  every real breakout, and silently drops gap-through breakouts' best fills. Test it if
  desired, but it is not the published entry.
- **MODERATE — exit on close beyond the 10-day channel** vs canonical intraday touch:
  slower exits, systematically larger give-back on reversals.
- **MODERATE — omissions:** the winner-skip rule and pyramiding are absent. Pyramiding
  in particular is load-bearing in the Turtle track record — most annual profit came
  from a few fully-pyramided trends.

**Evidence — the strongest in this document.** The Turtle experiment itself
(1984–1988, real money, futures incl. gold and equity indexes): Dennis reported the
group earned on the order of $175M in aggregate (as recounted by Faith; unaudited).
Character: low win rate — Dennis's own framing was "we're going to lose money 60% of
the time" — with payoff carried by avg-win/avg-loss well above 1 and a handful of huge
trends. The academic anchor is time-series momentum, the same phenomenon at measured
horizons: Moskowitz, Ooi & Pedersen (*Journal of Financial Economics*, 2012) found
significant trend profits in **all 58 liquid futures** (equity index, currency,
commodity, bond), 1985–2009; Hurst, Ooi & Pedersen ("A Century of Evidence on
Trend-Following Investing," 2017) found the effect positive in **every decade since
1880** across 67 markets and strongest in crises. Caveat: the academic evidence is
built mostly on 1–12-month signals and diversified portfolios; the 20-day channel is
its fastest classic implementation, and post-2009 trend returns at short horizons have
been choppier. Results in the literature are gross-to-lightly-costed; diversified
trend at realistic costs remains positive in the century study's fee simulations.

**Top gotcha (for this build):** sample size and concentration. Canonical returns come
from ~dozens of markets over decades, with most profit from 2–3 trades a year *per
portfolio*. One market, 120 days: expect 2–6 trades and a coin flip. Do not let this
backtest's Donchian result — good or bad — revise the strategy's standing.

---

## 7. Connors RSI(2) (daily)

**Canonical version: Larry Connors & Cesar Alvarez, *Short Term Trading Strategies
That Work* (2008).** **Long only.** (1) Close above the 200-day SMA. (2) **Buy on the
close** when RSI(2) closes **below 5** — the book's threshold research tested entries
from RSI(2) < 10 down and found returns per trade rose as the threshold fell; < 5 is
the published flagship rule (scale-in variants take a first tranche below 10).
(3) Exit at the close of the first day that closes **above the 5-day SMA**.
(4) **No stop — explicitly.** Connors reports that in his testing, stops *reduced*
performance on stock indexes.

**Spec audit:**
- **MATERIAL — entry timing.** Canonical entry is **on the signal close** (MOC). The
  spec enters at the **next open**. Index mean reversion is fastest in the first
  hours after the signal; buying the next open systematically donates the overnight
  snap-back portion of the edge and is the single largest deviation from the tested
  system.
- **MATCHES:** RSI(2) < 5 threshold ✓ (the stricter published flagship, not the looser
  < 10 research variant); close > 200-day SMA filter ✓; exit at the first close above
  the 5-day SMA ✓ — yes, that is Connors' actual exit; long-only ✓; no stop ✓
  (faithful to canon, with the risk noted below).

**Evidence:** the best-documented high-win-rate system in this brief. Connors &
Alvarez's book tests (SPY and stock portfolios, ~1995–2007, before costs) and
independent replications since: an EasyLanguageMastery replication of the exact book
rules on the S&P 500 index (from 1983) produced 129 trades, **82% profitable, profit
factor 2.97** (no commissions/slippage, fixed $2,000 risk sizing); a recent large
parameter-grid study (StatOasis) found long RSI(2) configurations profitable in 100%
of 96 long variants on SPY **and ES futures**, median profit factor 1.79 and **69.7%
win rate**; long-run SPY tracking (Quantified Strategies, 1993–present) shows ~+0.9%
average per trade with a ~34% max drawdown. Payoff ratio is below 1 (many small wins,
occasional large loss) — the mirror image of Donchian. Post-publication decay has been
debated (2011–2014 drawdown era), with later updates showing new equity highs by 2016.

**Top gotcha:** the no-stop design on a leveraged futures account. The published
drawdowns (max ~34% on unleveraged SPY) come from holding through cascading declines
(2008, 2011) until the 5-SMA exit triggers. Faithful replication means accepting that
tail; adding a stop changes the system into something the evidence doesn't describe.

---

## Ranking

Of these seven, the two with the strongest published evidence **on index and gold
futures specifically** are **(1) Donchian trend following** and **(2) the Opening
Range Breakout**. Donchian/trend stands alone at the top: it is the only approach here
validated by a real-money, multi-year experiment on futures themselves (the Turtles,
trading gold and index contracts among others) *and* by peer-reviewed,
cross-century evidence — Moskowitz, Ooi & Pedersen's 58-futures study and Hurst, Ooi &
Pedersen's 1880–2016, 67-market study, positive in every decade and asset class that
includes both equity-index and gold futures — with the honest caveat that the academic
results are strongest at slower horizons and portfolio scale, so the 20-day
single-market version inherits only part of that strength. ORB takes second on the
Zarattini & Aziz body of work: a fully specified, rules-exact study on QQQ (2016–2023)
with ~33% annualized alpha net of commissions, extended by the stocks-in-play study
net of transaction costs — modern, single-era, equity/ETF-based rather than
futures-based, and (critically for this build) validated with a 10R/close exit that
the frozen 2R spec does not replicate. Connors RSI(2) is a close third — its win-rate
evidence (≈70–82%) is arguably the most replicated here, including tests on ES — but
it is a daily equity-index mean-reversion system whose no-stop drawdown profile fits a
$500 leveraged account worst, and its canonical entry (on the close) is the one the
frozen spec changes.
