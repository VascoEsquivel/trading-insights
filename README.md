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
**6.4%** of the time; a −20% move followed **10.1%**.

| Setup | n | Big up-move | vs base | Big drop | Typical |
|---|---|---|---|---|---|
| Six-month momentum | 47,539 | 15.1% | 2.38x | 14.6% | **+4.7%** |
| Coiling near the high | 45,468 | 3.1% | 0.48x | **4.7%** | +4.0% |
| Stage-2 breakout | 15,040 | 6.3% | 0.99x | 8.0% | +3.6% |
| New 52-week high | 39,325 | 5.0% | 0.79x | 6.7% | +3.5% |
| Volume shock | 8,494 | 11.2% | 1.76x | 16.2% | +2.5% |
| *Control: broken downtrend* | 214,786 | 7.3% | 1.15x | 13.5% | +2.8% |
| Recovering from a collapse | 9,949 | 14.0% | 2.20x | **32.2%** | **−5.5%** |

Two results are worth dwelling on, because they are why the table reports both
tails rather than a single score:

**The control condition beat the baseline.** A broken downtrend — below the
200-day, negative over six months — was followed by a +40% move 15% *more* often
than a random day. It was included expecting it to underperform. It does not,
and the reason is that a fixed percentage threshold is partly a volatility bet:
cheap, violent names clear ±40% more often whichever direction they are heading.

**"Recovering from a collapse" is a lottery ticket, not an edge.** It has the
second-highest big-up-move rate in the table, which on hit rate alone would make
it a top setup. Its drop rate is 32% against a 10% baseline and its median
outcome is −5.5%. Ranking by hit rate would have promoted the worst setup in the
study.

So the tab sorts by *typical* (median) outcome and always shows the drop rate
next to the headline number. Six-month momentum is the only condition with both
a strong upside lift and the best typical outcome — which is roughly what the
momentum literature would predict.

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
