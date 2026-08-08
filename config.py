"""Central configuration: credentials, collector cadences, seed data, flags.

Every tunable lives here so cadences and thresholds are changed in one place.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "data" / "trading.db"

# --------------------------------------------------------------------------
# Credentials (loaded from .env — never hardcode)
# --------------------------------------------------------------------------
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()

REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "trading-insights/0.1").strip()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Reddit apps go through manual approval now; keep the whole sentiment layer
# behind a flag so the rest of the dashboard ships while review is pending.
ENABLE_REDDIT = _flag("ENABLE_REDDIT")

# --------------------------------------------------------------------------
# Collector cadences, in seconds
# --------------------------------------------------------------------------
COLLECTOR_TICK = 5  # loop granularity; each job runs when its interval elapses

STOCK_QUOTE_INTERVAL = 60  # Finnhub /quote, one REST call per symbol
STOCK_VOLUME_INTERVAL = 900  # yfinance batch — /quote carries no volume field
STOCK_NEWS_INTERVAL = 900  # Finnhub /company-news, one call per symbol

# CoinGecko Demo allows ~100 calls/min but only 10k calls/month — the month cap
# is the binding constraint, so poll the whole watchlist as ONE batched call.
CRYPTO_PRICE_INTERVAL = 180
CRYPTO_NEWS_INTERVAL = 900  # keyless RSS, not CoinGecko

MEME_PAIR_INTERVAL = 90  # DexScreener, batched per chain
MEME_TRENDING_INTERVAL = 300  # DexScreener boosted-token discovery

SOCIAL_INTERVAL = 300  # Reddit scan window
PORTFOLIO_SNAPSHOT_INTERVAL = 180  # drives the equity curve

# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------
STARTING_BALANCE = 10_000.00

# --------------------------------------------------------------------------
# Meme-coin risk thresholds (see README — these are the numbers that separate a
# real move from an easily manipulated one)
# --------------------------------------------------------------------------
THIN_LIQUIDITY_USD = 50_000.0
NEW_TOKEN_AGE_HOURS = 24

# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
UI_AUTO_REFRESH_SECONDS = 45
UI_CACHE_TTL_SECONDS = 20

# --------------------------------------------------------------------------
# Seed watchlist: (symbol, asset_class, name, source_id)
#   source_id — CoinGecko coin id, or DexScreener "chainId:pairAddress",
#               or None for stocks (Finnhub/yfinance key off the ticker).
# DexScreener entries carry a "~<last4 of pair address>" symbol suffix because
# a dozen unrelated tokens all call themselves PEPE and `symbol` is the join key.
# --------------------------------------------------------------------------
SEED_WATCHLIST = [
    ("AAPL", "stock", "Apple Inc.", None),
    ("NVDA", "stock", "NVIDIA Corporation", None),
    ("TSLA", "stock", "Tesla, Inc.", None),
    ("SPY", "stock", "SPDR S&P 500 ETF Trust", None),

    ("BTC", "crypto", "Bitcoin", "bitcoin"),
    ("ETH", "crypto", "Ethereum", "ethereum"),
    ("SOL", "crypto", "Solana", "solana"),

    ("DOGE", "meme", "Dogecoin", "dogecoin"),
    ("SHIB", "meme", "Shiba Inu", "shiba-inu"),
    ("PEPE", "meme", "Pepe", "pepe"),

    # DexScreener-tracked pairs (no OHLC history — charted from our own
    # accumulated price_snapshots).
    ("PEPE~HhZn", "meme", "Pepe (Raydium, Solana)",
     "solana:FCEnSxyJfRSKsz6tASUENCsfGwKgkH6YuRn1AMmyHhZn"),
    ("PEPE~34c7", "meme", "BasedPepe (Uniswap v3, Base)",
     "base:0x0FB597D6cFE5bE0d5258A7f017599C2A4Ece34c7"),
]

ASSET_CLASSES = ("stock", "crypto", "meme")

# --------------------------------------------------------------------------
# News / sentiment sources
# --------------------------------------------------------------------------
# CoinGecko /news and CryptoCompare both return 401 without a paid plan, so
# crypto headlines come from keyless publisher RSS.
CRYPTO_NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]

REDDIT_SUBREDDITS = [
    "stocks",
    "wallstreetbets",
    "CryptoCurrency",
    "CryptoMoonShots",
    "SatoshiStreetBets",
]
REDDIT_POSTS_PER_SUB = 100

# Shared HTTP settings
HTTP_TIMEOUT = 20
HTTP_USER_AGENT = "trading-insights/0.1 (personal research dashboard)"
