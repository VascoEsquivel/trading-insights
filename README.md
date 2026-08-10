# Trading Insights

A personal market-research dashboard: near-live prices, news, and social
sentiment for stocks, major crypto, and meme coins in one place — plus a
paper-trading simulator with a virtual cash balance so trades and PnL can be
tracked without real money.

This is a decision-support and practice tool. It surfaces data; you make the
calls. It never renders a buy/sell recommendation, and it never connects to a
brokerage or exchange for order execution.

## Ground rules baked into the build

- **Paper trading only.** No account is ever linked for execution. Where a
  provider offers both data and trading endpoints, only the data endpoints are
  used.
- **Sentiment and news are inputs, not verdicts.** Mention counts, polarity
  scores, and trend direction render as raw numbers and badges — never as a
  recommendation.
- **Meme rows always show risk context.** Liquidity, market cap, and token age
  sit next to price. Anything under $50k liquidity is badged `thin`; anything
  under 24h old is badged `new`. Those are the numbers that separate a real move
  from an easily manipulated one.
- **Secrets never get committed.** Keys live in `.env` (gitignored since the
  first commit); `.env.example` documents the variable names.

## Setup

```bash
pip install -r requirements.txt
```

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Where to get it | Needed for |
|---|---|---|
| `FINNHUB_API_KEY` | free key at [finnhub.io](https://finnhub.io) | stock quotes + company news |
| `COINGECKO_API_KEY` | free Demo key from the CoinGecko developer dashboard | crypto prices + OHLC |
| `ENABLE_REDDIT` | `false` until your Reddit app is approved | sentiment layer |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | "script" app at [reddit.com/prefs/apps](https://reddit.com/prefs/apps) | sentiment layer |

DexScreener and yfinance need no credentials.

Reddit API apps now go through manual approval under the Responsible Builder
Policy, which can take weeks, and unauthenticated endpoints are blocked. The
entire sentiment layer sits behind `ENABLE_REDDIT` so everything else works
while that review is pending.

## Running it — two processes

The collector writes to SQLite; the Streamlit app only reads from it. They run
separately, in two terminals:

```bash
python -m collector.scheduler
```

```bash
streamlit run app.py
```

Note that Streamlit binds `0.0.0.0` by default, so the dashboard is reachable
from every device on your network and it has no login. Pass
`--server.address 127.0.0.1` to keep it on loopback — see the Tailscale section
for reaching it remotely without exposing it to the LAN.

The collector must be running for prices to update. `python -m
collector.scheduler --once` runs a single cycle of every job and exits, which is
the quickest way to confirm your keys work.

### Running it continuously

`CRYPTO_PRICE_INTERVAL` is set to 300s specifically so the collector *can* stay
up. The CoinGecko Demo tier caps at 10,000 calls **per month** and that cap, not
the per-minute limit, is what binds:

| Interval | Calls/day | Calls/month | Fits? |
|---|---|---|---|
| 180s | 480 | 14,400 | no — dies around day 20 |
| 300s | 288 | 8,640 | yes, with headroom for chart views |

Chart and signal-desk reads draw on the same quota, so treat 300s as a floor
rather than a target.

Measured footprint while running (32-core, 32 GB machine): the collector uses
about 0.02s of CPU per minute and 122 MB of RAM — it is network-bound and
asleep almost all the time. Streamlit uses roughly 2.7% of one core and 170 MB
while a browser tab is open and auto-refreshing, and close to nothing once the
tab is closed. Combined that is under 1% of memory.

## Tests

```bash
python -m tests.test_portfolio
```

17 tests over the paper engine — stdlib `unittest`, since pytest isn't
installed here. They run against a throwaway database and never touch
`data/trading.db`.

The coverage is aimed at the money math specifically, because that is where a
silent bug costs you trust in every number on the dashboard, and two real ones
already shipped this build: positions with no snapshot were valued at zero
(making a buy at the live price look like an instant total loss of the amount
spent), and the equity curve dropped those positions entirely so it stepped down
the moment a trade filled. Both are pinned by named regression tests.

Those two were checked by mutation: reintroducing the original bug fails the
tests, restoring the fix passes them. A regression test that cannot fail is
decoration.

## Reaching it from your phone (Tailscale)

The dashboard has no authentication of any kind, so it is served over a private
tailnet rather than the public internet.

After `tailscale up` on this machine and installing Tailscale on the phone under
the same account, there are two ways to reach it.

**Direct tailnet address** — works immediately, no extra setup:

```bash
streamlit run app.py
```

Reachable at `http://<tailnet-ip>:8501` from your own devices. The catch is that
this relies on Streamlit's default `0.0.0.0` bind, so the dashboard is also open
to anyone on the same Wi-Fi.

**Via `tailscale serve`** — better, but Serve has to be enabled once for the
tailnet from the Tailscale admin console:

```bash
tailscale serve --bg 8501
```

This gives an HTTPS `https://<machine>.<tailnet>.ts.net/` address and proxies
from localhost, so Streamlit can be bound to loopback and stop being visible on
the LAN at all:

```bash
streamlit run app.py --server.address 127.0.0.1
```

`tailscale serve status` shows what is exposed; `tailscale serve --https=443 off`
withdraws it.

Two things worth keeping straight:

- **`tailscale serve` is tailnet-only. `tailscale funnel` is the public
  internet.** Do not use `funnel` here — it would put an unauthenticated paper
  portfolio on the open web.
- The machine has to be awake. A tunnel does not help if the laptop is asleep,
  and the collector stops with it.

## Layout

```
trading-insights/
├── config.py            # cadences, starting balance, seed watchlist, flags
├── app.py               # Streamlit entrypoint, four tabs
├── collector/
│   ├── __init__.py      # shared HTTP session + per-source 429 backoff
│   ├── stocks.py        # Finnhub quotes + company news, yfinance candles
│   ├── crypto.py        # CoinGecko batched prices + OHLC, crypto news RSS
│   ├── memecoins.py     # DexScreener pairs + trending discovery
│   ├── social.py        # Reddit via PRAW (behind ENABLE_REDDIT)
│   └── scheduler.py     # the polling loop
├── trading/
│   ├── db.py            # schema + queries
│   └── portfolio.py     # paper engine, PnL math
└── data/trading.db      # gitignored
```

## Data sources

| Asset class | Live quotes | Chart history | News |
|---|---|---|---|
| Stocks | Finnhub `/quote` | yfinance | Finnhub `/company-news` |
| Crypto | CoinGecko `/simple/price` (batched) | CoinGecko `/coins/{id}/ohlc` | publisher RSS |
| Meme | DexScreener pairs + CoinGecko | CoinGecko for DOGE/SHIB/PEPE; own `price_snapshots` for DexScreener-only tokens | publisher RSS |

Verified against live responses in August 2026:

- **Finnhub `/stock/candle` returns 403** on free keys for US equities — hence
  yfinance for stock charts. `/quote` also carries **no volume field**, so daily
  volume is topped up by one batched yfinance call every 15 minutes.
- **CoinGecko `/news` and CryptoCompare's news API both return 401** without a
  paid plan, so crypto headlines come from keyless publisher RSS (CoinDesk,
  Cointelegraph, Decrypt).
- **DexScreener exposes no historical OHLC** — current and 24h stats only. Charts
  for DexScreener-only tokens are line charts built from our own accumulated
  `price_snapshots`, which is why that poll runs on a 90-second cadence.
- **Not used:** Binance (blocks US IPs) and X/Twitter (pay-per-use only since
  Feb 2026, no free tier).

## The signal desk

Each asset tab has a **Signal desk** that does the part that actually takes
work: it sizes today's move against the symbol's own recent behaviour, then
looks for what corroborates or undercuts it.

For the selected symbol it computes the move as a multiple of the symbol's
typical daily swing, volume against its 20-period average, distance from the
20-period average, RSI(14), position in the observed range, and — for meme
pairs — pool liquidity. It then pulls the headlines from the last 24h, scores
their tone with VADER, and checks whether coverage points the same way the
price moved. The result is sorted into **Supporting / Against / Context**, each
factor carrying the number it came from.

It deliberately produces no buy/sell label. A read like *"up 2.3%, only 0.9x its
typical swing, volume 0.81x average, 37 headlines at neutral tone"* tells you
there is probably nothing there — which is a genuinely useful answer, and a
different one from *"don't buy"*. None of these factors forecasts anything; a
well-explained move is still just a move that has been explained.

History comes from 6 months of yfinance dailies for stocks, 90 days of hourly
CoinGecko data resampled to daily closes for coins, and collected
`price_snapshots` for DexScreener-only tokens (where the read says so and marks
itself provisional).

## Paper trading — buying things to see how you'd do

You start with $10,000 of virtual cash. Nothing is ever sent to a broker.

There are two ways in:

- **Markets → Buy / sell**, directly under the watchlist table. Pick a symbol,
  a side and a quantity; it fills at the latest stored price. Fractional
  quantities are fine.
- **Ideas → Discover or Recommended**, on any candidate you've opened. The
  quick-buy sits under the evidence so you can act on a name while you're
  reading about it.

Buying from Ideas does two extra things, because those tickers are not on your
watchlist and so have no stored price: it adds the symbol to the watchlist (an
untracked holding could never be marked to market afterwards) and writes a
price snapshot at the fill price, so the position values correctly right away
rather than waiting for the collector's next cycle.

**Positions with no current price are held at cost, not at zero.** That
distinction matters more than it sounds: valuing them at zero made a
just-executed buy look like an instant total loss of the amount spent. The
Portfolio tab flags any line it can't price.

The Portfolio tab has a **Performance** section answering the question the
equity curve cannot: it compares your return against simply buying and holding
SPY over the same period. A portfolio up 4% is only good news if the index did
less.

Alongside it, closed-trade statistics — win rate, average win against average
loss, and profit factor (gross profit over gross loss). Those two together are
the point: a run of 67% wins with a profit factor of 0.97 is a *losing* record,
because the one loser was bigger than both winners. Win rate on its own hides
that. Open positions are excluded deliberately — an unrealised loss you are
still holding is not yet a losing trade, and counting it would let the numbers
be improved by refusing to sell.

The Portfolio tab is also where you see how you're doing — cash, total value,
realized and unrealized PnL, every position marked to market, the full trade
log, and an equity curve the collector extends each cycle. There's an
asset-class filter, and a reset if you want to start over.

## Recommended — measured base rates, not predictions

Build the study first (a couple of minutes, ~250 tickers, ~12 years of daily
bars):

```bash
python -m quant.study
```

For every ticker on every trading day it computes a feature set, labels whether
a large move followed, and reports how often each setup was *actually* followed
by one, against the unconditional rate over the same data. The **Recommended**
tab then runs the identical conditions against today's last bar and shows what
currently matches, with each setup's historical record attached.

### What the study found

Baseline: on a randomly chosen day, a +40% move within 60 trading days followed
**6.3%** of the time; a −20% move followed **10.1%**.

Each setup gets three lift numbers, and they disagree in ways that matter:

| Setup | n | Raw lift | Vol-adj | Out-of-sample | Big drop | Typical | Holds up |
|---|---|---|---|---|---|---|---|
| Six-month momentum | 47,540 | 2.38x | **1.30x** | **1.38x** | 14.6% | +4.7% | yes |
| Stage-2 breakout | 15,106 | 0.99x | **1.29x** | **1.40x** | 8.0% | +3.5% | yes |
| New 52-week high | 39,452 | 0.79x | **1.21x** | **1.40x** | 6.7% | +3.5% | yes |
| Volume shock | 8,606 | 1.74x | 1.15x | 1.03x | 16.6% | +2.3% | no |
| Oversold in an uptrend | 7,989 | 0.81x | 1.05x | 0.93x | 8.0% | +4.3% | no |
| Fresh golden cross | 25,410 | 1.06x | 0.99x | 0.98x | 10.1% | +3.0% | no |
| Coiling near the high | 45,724 | 0.48x | 0.95x | 1.07x | 4.8% | +3.9% | no |
| Squeeze into volume | 2,367 | 1.28x | 0.91x | 0.96x | 11.2% | +2.8% | no |
| *Control: broken downtrend* | 215,457 | 1.14x | **0.88x** | 0.90x | 13.7% | +2.7% | no |
| Recovering from a collapse | 9,991 | 2.19x | **0.74x** | 0.76x | 32.5% | −5.7% | no |

**Raw lift is actively misleading, and the control proves it.** A fixed +40%
threshold is partly a volatility bet — cheap, violent names clear any fixed
percentage more often whichever way they are heading. The control condition
(below the 200-day, falling) was included expecting it to underperform. On raw
lift it *beat* the baseline at 1.14x.

**Vol-adjusted fixes it** by comparing each setup against stocks in the same
trailing-volatility decile rather than against the universe. The control drops
to 0.88x, where it belongs. That correction reversed two rankings:

- *Recovering from a collapse* collapses from 2.19x to **0.74x**. Its entire
  apparent edge was volatility. Median outcome −5.7%, drop rate triple the
  baseline — on raw lift it ranked second, and it is the worst setup here.
- *Stage-2 breakout* and *New 52-week high* rise from 0.99x and 0.79x to
  **1.29x** and **1.21x**. Raw lift made two of the better setups look worthless
  because they select calm stocks, which clear a fixed percentage less often.

**Out-of-sample is the overfitting check.** Rates are computed on the first 60%
of the date range and re-measured on the last 40%, with a 90-day purge between
so no forward outcome straddles the boundary. Three conditions clear both bars
(≥1.10x adjusted and out-of-sample) — and notably the two breakout setups get
*stronger* out of sample, at 1.40x.

Six-month momentum surviving at 1.30x adjusted and 1.38x out-of-sample is
roughly what the momentum literature would predict, which is mild evidence the
pipeline is measuring something real rather than generating noise.

### Does it work when the market is falling?

"The sample is dominated by a bull market" was a listed bias; now it is a
measured column. Every row is tagged with whether SPY was above its own 200-day
that day (78% of the sample), and each setup is scored separately in each
regime.

| Setup | Bull | Bear |
|---|---|---|
| Six-month momentum | 1.34x | **1.38x** |
| Stage-2 breakout | 1.32x | **1.35x** |
| New 52-week high | 1.25x | **1.02x** |
| Volume shock | 1.11x | **1.32x** |
| Oversold in an uptrend | 1.00x | **1.36x** |
| Fresh golden cross | 1.06x | **0.71x** |
| Recovering from a collapse | 0.88x | **0.46x** |

Momentum and stage-2 breakouts hold in both directions, which is the strongest
result in the study. New 52-week high does not — it is a bull-market setup, and
it was one of the three that passed the earlier checks, so the regime column is
what catches it.

The mean-reversion setups invert: oversold-in-an-uptrend and volume shock are
*better* when the market is falling. And a fresh golden cross is actively
harmful in a downtrend at 0.71x — the classic whipsaw.

The Recommended tab reads the live regime from SPY on load, shows which column
applies today, and only badges a setup "holds up" if it clears 1.10x in the
*current* regime as well as overall and out of sample. Bear-regime samples are
roughly a fifth of the data, so those numbers are noisier.

### Trending news on the matches

Below the ranked list, **Load headlines** pulls company news for the top matches
— names that are not on your watchlist, so the collector has never fetched
anything for them. It shows a per-ticker chip row (headline count and average
tone), a filter, and a merged chronological feed with each headline's VADER
tone.

This is separate from, and does not replace, the per-tab news feed, which stays
scoped to symbols you actually track. The two answer different questions: what
is happening to things you hold, versus what the story is behind a name you have
never looked at.

One wrinkle worth knowing: `news_items` is unique on `(symbol, url)`, so a
market-wide story genuinely exists once per ticker it mentions. That is correct
in the table but reads as a duplicate in a merged feed, so the feed collapses by
URL and shows every ticker it touched as a combined tag (`ABNB · TWLO`).

### Biases, stated rather than corrected

- **Survivorship.** yfinance serves only tickers that still trade, so companies
  that went to zero are absent and every rate reads optimistic.
- **Overlapping windows.** Consecutive days are near-duplicates, so the true
  independent sample is far smaller than n, and the Wilson intervals are too
  tight.
- **Multiple testing.** Ten conditions were tried; some of the spread is chance.
- **Regime.** The window is dominated by a long bull market.
- No position sizing, costs, slippage, or exit rule is modelled. This is not a
  backtest of a strategy.

`quant/live.py` deliberately imports the same `CONDITIONS` and feature code the
study uses. If the two ever drifted apart, every number on the tab would quietly
stop meaning anything.

## Discover — candidates beyond the watchlist

The **Discover** tab scans the whole market instead of your watchlist, in two
stages.

First a cheap pass ranks a screen's worth of candidates on data the screen
already returns — volume against its 3-month average, position in the 52-week
range, distance from the 50-day average, market cap, and whether the move is so
large that most of it is already behind it. That ordering is deliberately not
"biggest gainer first".

Then **Analyse** runs the full read on one candidate: it fetches that symbol's
price history and headlines on demand (discovery candidates are off-watchlist,
so nothing has been collected for them yet) and produces the same
Supporting / Against / Context breakdown as the signal desk.

The two stages disagreeing is the point. A recent scan had AXTI ranked top at
+3/−0 on screen data; the deep read came back +1/−3 — extended 59% above its
20-day average, RSI 73, and no headline explaining an unusual move. That is the
catch a gainers list alone will not give you.

Sources: Yahoo's predefined screeners via yfinance for stocks (day gainers, most
active, small-cap gainers, growth tech, undervalued growth, aggressive small
caps, day losers); CoinGecko `/search/trending` and the `/coins/markets` 24h
leaderboard for crypto; DexScreener boosted tokens for memes. Every scan is a
live API call, so it is button-gated and cached for five minutes.

Finnhub's general news feed is deliberately not used for discovery: its items
come back with an empty `related` field, so headlines cannot be mapped to
tickers.

## Using it

Each asset tab has the same shape: watchlist table, add/remove controls, a
chart, an order ticket, and a news feed. The Meme tab adds two things —
liquidity/market-cap/age columns with `thin` and `new` badges, and a **Trending
on DexScreener** section that resolves currently-boosted tokens to their
deepest pool. Trending is button-gated rather than automatic, because resolving
each token costs its own request, and nothing is ever added to the watchlist
without you clicking Add.

The Portfolio tab shows cash, total value, PnL, positions marked to market, the
equity curve, and the trade log, with an asset-class filter. Cash is a single
shared balance, so it is reported unfiltered.

Charts pick their source automatically: yfinance candles for stocks, CoinGecko
OHLC for coins with a CoinGecko id, and a line chart built from collected
`price_snapshots` for DexScreener-only tokens.

## Known rough edges

- **Finnhub company news is broad.** Its `/company-news` feed tags general
  market-wire stories with a symbol, so an NVDA query returns items that only
  loosely concern NVDA. That is the source's tagging, not a filter bug.
- **Sentiment counts are per ticker, not per token.** Matching is on ticker,
  cashtag, and coin name, so the several tokens called PEPE share one count.
- **Liquidity and age are blank for CoinGecko-priced memes.** DOGE, SHIB and
  PEPE trade mainly on exchanges; a DEX pool number would misrepresent their
  real depth, so the columns are left empty rather than filled with something
  misleading.
- **Watchlist tables are static HTML, not `st.dataframe`.** Streamlit's
  dataframe is a canvas grid that measures zero width inside a tab hidden on
  first paint, so every tab but the first rendered collapsed. The cells are
  pre-formatted strings anyway, where the grid's sorting would order "$1.30T"
  before "$922.11K" lexicographically.

## Notes on the schema

`news_items` is uniquely keyed on `(symbol, url)` rather than `url` alone. One
article often mentions several watched coins, and a global unique constraint on
`url` would silently drop it from every feed but the first. The pairing still
prevents duplicate rows.

Watchlist symbols are the join key everywhere, so DexScreener entries carry a
`~<last4-of-pair-address>` suffix (`PEPE~HhZn`) — a dozen unrelated tokens all
call themselves PEPE.

All timestamps are stored as ISO-8601 UTC strings.

## Later, not v1

Price alerts, backtesting, CSV export of trade history, WebSocket feeds,
X/Twitter sentiment (paid), public deployment.
