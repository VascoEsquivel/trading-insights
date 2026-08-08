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

The collector must be running for prices to update. `python -m
collector.scheduler --once` runs a single cycle of every job and exits, which is
the quickest way to confirm your keys work.

Don't leave the collector running 24/7 — the CoinGecko Demo tier caps at 10,000
calls **per month**, and that cap, not the per-minute limit, is what binds.

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
