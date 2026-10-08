"""Stocks plugin for InkyPi.

Shows quotes for indices, stocks, ETFs, crypto or currencies with the change
over a chosen range and an optional sparkline.

Data comes from Twelve Data (https://twelvedata.com). The free plan needs an
API key stored as TWELVE_DATA_API_KEY (InkyPi web UI -> API Keys) and allows
8 symbols per minute / 800 per day.

Ticker syntax (comma or newline separated). Yahoo-style and Twelve Data
symbols both work:
    ^GSPC, ^DJI, ^IXIC, AAPL, BTC-USD      (or SPX, DJI, IXIC, AAPL, BTC/USD)
    AAPL=Apple                              <- optional custom label after "="
"""

import logging
import math
import re
import time
from datetime import datetime, timedelta

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

API_BASE = "https://api.twelvedata.com"
API_KEY_NAME = "TWELVE_DATA_API_KEY"

# range -> (interval, outputsize, label shown on screen)
RANGES = {
    "1d": ("5min", 400, "Today"),
    "5d": ("30min", 400, "5 Days"),
    "1mo": ("1day", 45, "1 Month"),
    "3mo": ("1day", 110, "3 Months"),
    "6mo": ("1day", 200, "6 Months"),
    "ytd": ("1day", 400, "YTD"),
    "1y": ("1day", 400, "1 Year"),
    "5y": ("1week", 300, "5 Years"),
}
RANGE_DAYS = {"1mo": 30, "3mo": 91, "6mo": 182, "1y": 365, "5y": 1826}

# Yahoo-style input -> (Twelve Data symbol, display name, ETF fallback if the
# index isn't available on the account's plan)
ALIASES = {
    "^GSPC": ("SPX", "S&P 500", "SPY"),
    "^SPX": ("SPX", "S&P 500", "SPY"),
    "SPX": ("SPX", "S&P 500", "SPY"),
    "^DJI": ("DJI", "Dow Jones", "DIA"),
    "DJI": ("DJI", "Dow Jones", "DIA"),
    "^IXIC": ("IXIC", "Nasdaq", "ONEQ"),
    "IXIC": ("IXIC", "Nasdaq", "ONEQ"),
    "^NDX": ("NDX", "Nasdaq 100", "QQQ"),
    "NDX": ("NDX", "Nasdaq 100", "QQQ"),
    "^RUT": ("RUT", "Russell 2000", "IWM"),
    "RUT": ("RUT", "Russell 2000", "IWM"),
    "^VIX": ("VIX", "VIX", None),
    "VIX": ("VIX", "VIX", None),
    "GC=F": ("XAU/USD", "Gold", None),
    "SI=F": ("XAG/USD", "Silver", None),
}
CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "DOGE": "Dogecoin"}

DEFAULT_TICKERS = "^GSPC, ^DJI, ^IXIC"
MAX_TICKERS = 8          # free plan: 8 API credits per minute, 1 per symbol
CACHE_SECONDS = 240      # reuse data fetched in the last few minutes
CLOSED_CACHE_SECONDS = 3600  # when markets are closed, refetch at most hourly
SPARK_W, SPARK_H = 200, 60   # SVG viewBox units; scaled to fit by CSS

_cache = {}  # (symbols, range, mode) -> (fetched_at, quotes)
_unavailable = set()  # index symbols the account's plan can't access; use their ETF directly


class Stocks(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["ranges"] = [{"value": k, "label": v[2]} for k, v in RANGES.items()]
        return template_params

    def generate_image(self, settings, device_config):
        api_key = device_config.load_env_key(API_KEY_NAME)
        if not api_key:
            raise RuntimeError(f"Add a free Twelve Data API key as {API_KEY_NAME} on the API Keys page.")

        tickers = parse_tickers(settings.get("tickers") or DEFAULT_TICKERS)
        if not tickers:
            raise RuntimeError("Enter at least one ticker symbol.")
        if len(tickers) > MAX_TICKERS:
            raise RuntimeError(f"Up to {MAX_TICKERS} tickers are supported on Twelve Data's free plan.")

        chart_range = settings.get("chartRange", "1d")
        if chart_range not in RANGES:
            chart_range = "1d"
        show_chart = settings.get("showChart", "true") != "false"
        color_mode = settings.get("colorMode", "color")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz_name = device_config.get_config("timezone", default="America/New_York")
        try:
            tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone("America/New_York")

        quotes = get_quotes(api_key, tickers, chart_range, show_chart)

        if all(q.get("error") for q in quotes):
            raise RuntimeError(f"Could not load market data: {quotes[0]['error']}")

        latest = max((q["time"] for q in quotes if q.get("time")), default=None)
        updated = datetime.fromtimestamp(latest, tz).strftime("%b %-d, %-I:%M %p %Z") if latest else ""

        for q in quotes:
            if not q.get("error") and show_chart:
                q["sparkline"] = build_sparkline(q.get("points") or [], q["prev_close"])

        width, height = dimensions
        layout = "hero" if len(quotes) == 1 else ("grid" if should_grid(len(quotes), width, height) else "rows")

        template_params = {
            "title": settings.get("title", "").strip(),
            "quotes": quotes,
            "range_label": RANGES[chart_range][2],
            "updated": updated,
            "market_status": market_status(quotes),
            "show_chart": show_chart,
            "color_mode": color_mode,
            "layout": layout,
            "count": len(quotes),
            "grid_cols": 2 if layout == "grid" else 1,
            "plugin_settings": settings,
        }

        image = self.render_image(dimensions, "stocks.html", "stocks.css", template_params)
        if image is None:
            raise RuntimeError("Failed to render the stocks display.")
        return image


# --------------------------------------------------------------------------
# Tickers
# --------------------------------------------------------------------------

def parse_tickers(raw):
    """Returns a list of dicts: {input, symbol, name, fallback, label}."""
    tickers, seen = [], set()
    for part in re.split(r"[,\n;]+", raw):
        part = part.strip()
        if not part:
            continue
        label = None
        # "AAPL=Apple" sets a custom label; futures/forex like GC=F or EURUSD=X keep their "=".
        if "=" in part:
            sym, _, maybe_label = part.partition("=")
            if len(maybe_label.strip()) > 1:
                part, label = sym, maybe_label.strip()
        raw_sym = part.strip().upper()
        if not raw_sym or raw_sym in seen:
            continue
        seen.add(raw_sym)
        tickers.append(resolve_symbol(raw_sym, label))
    return tickers


def resolve_symbol(raw_sym, label=None):
    symbol, name, fallback = raw_sym, None, None
    if raw_sym in ALIASES:
        symbol, name, fallback = ALIASES[raw_sym]
    elif re.fullmatch(r"[A-Z0-9]{2,6}-(USD|EUR|GBP|USDT)", raw_sym):        # BTC-USD
        base, quote = raw_sym.split("-")
        symbol, name = f"{base}/{quote}", CRYPTO_NAMES.get(base)
    elif re.fullmatch(r"[A-Z]{6}=X", raw_sym):                             # EURUSD=X
        symbol = f"{raw_sym[:3]}/{raw_sym[3:6]}"
    elif "/" in raw_sym:
        name = CRYPTO_NAMES.get(raw_sym.split("/")[0])
    return {"input": raw_sym, "symbol": symbol, "name": label or name,
            "fallback": fallback, "label": label}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

class ApiError(Exception):
    pass


def api_get(endpoint, params, api_key):
    params = dict(params, apikey=api_key)
    try:
        resp = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=20)
    except requests.RequestException as e:
        raise ApiError(f"Network error reaching Twelve Data: {e}")
    try:
        data = resp.json()
    except ValueError:
        raise ApiError(f"Twelve Data returned HTTP {resp.status_code}")

    if isinstance(data, dict) and data.get("status") == "error" and "code" in data:
        code, msg = data.get("code"), data.get("message", "")
        logger.warning(f"Stocks: Twelve Data {endpoint} error {code}: {msg}")
        if code == 401:
            raise ApiError("Twelve Data rejected the API key. Check TWELVE_DATA_API_KEY.")
        if code == 429:
            raise ApiError("Twelve Data rate limit reached (free plan: 8 symbols/min, 800/day). "
                           "Use fewer tickers or refresh less often.")
        # A single-symbol request reports symbol errors at the top level.
        if len(params.get("symbol", "").split(",")) == 1:
            return {params["symbol"]: data}
        raise ApiError(msg or f"Twelve Data error {code}")
    if resp.status_code >= 400:
        raise ApiError(f"Twelve Data returned HTTP {resp.status_code}")
    return data


def fetch_batch(api_key, symbols, chart_range, use_series):
    """Returns {symbol: raw_response_for_symbol}."""
    joined = ",".join(symbols)
    if use_series:
        interval, size, _ = RANGES[chart_range]
        data = api_get("time_series", {"symbol": joined, "interval": interval,
                                       "outputsize": size, "order": "asc"}, api_key)
    else:
        data = api_get("quote", {"symbol": joined}, api_key)
    if len(symbols) == 1 and symbols[0] not in data:
        data = {symbols[0]: data}
    return data


def get_quotes(api_key, tickers, chart_range, show_chart):
    # The quote endpoint has exact daily change and names, but no history. Use
    # it when no chart is needed for "Today"; otherwise one time_series call.
    use_series = show_chart or chart_range != "1d"
    key = (tuple(t["input"] for t in tickers), chart_range, use_series)
    cached = _cache.get(key)
    if cached:
        age = time.time() - cached[0]
        all_closed = not any(q.get("market_open") for q in cached[1] if not q.get("error"))
        if age < CACHE_SECONDS or (all_closed and age < CLOSED_CACHE_SECONDS):
            logger.info(f"Stocks: using cached data ({int(age)}s old)")
            return [dict(q) for q in cached[1]]

    # Indices already known to be off-plan go straight to their ETF.
    tickers = [dict(t, symbol=t["fallback"], proxied=True) if t["symbol"] in _unavailable and t["fallback"] else t
               for t in tickers]
    try:
        data = fetch_batch(api_key, [t["symbol"] for t in tickers], chart_range, use_series)
    except ApiError as e:
        if cached:  # show slightly stale data instead of failing outright
            logger.warning(f"Stocks: {e}; showing cached data")
            return [dict(q) for q in cached[1]]
        raise RuntimeError(str(e))

    quotes = [parse_entry(t, data.get(t["symbol"]), chart_range, use_series) for t in tickers]
    for t, q in zip(tickers, quotes):
        if t.get("proxied") and not q.get("error"):
            q["sub"] = f"via {t['symbol']}"

    # Indices may not be on the account's plan; retry those with their ETF.
    retry = [(i, t) for i, t in enumerate(tickers)
             if quotes[i].get("error") and t["fallback"] and not t.get("proxied")]
    if retry:
        proxies = [dict(t, symbol=t["fallback"]) for _, t in retry]
        try:
            pdata = fetch_batch(api_key, [p["symbol"] for p in proxies], chart_range, use_series)
            for (i, _), p in zip(retry, proxies):
                q = parse_entry(p, pdata.get(p["symbol"]), chart_range, use_series)
                if not q.get("error"):
                    q["sub"] = f"via {p['symbol']}"
                    quotes[i] = q
                    # The index failed but its ETF worked: skip the index next time.
                    _unavailable.add(tickers[i]["symbol"])
        except ApiError as e:
            logger.warning(f"Stocks: ETF fallback failed: {e}")

    if any(not q.get("error") for q in quotes):
        _cache[key] = (time.time(), [dict(q) for q in quotes])
    return quotes


def error_quote(t, message):
    return {"symbol": t["symbol"], "name": t["name"] or t["input"], "error": message}


def parse_entry(t, entry, chart_range, use_series):
    if not entry:
        return error_quote(t, "No data")
    if entry.get("status") == "error":
        msg = entry.get("message", "")
        logger.warning(f"Stocks: {t['symbol']}: {msg}")
        if entry.get("code") == 404 or "not found" in msg.lower() or "invalid" in msg.lower():
            return error_quote(t, "Unknown symbol")
        if "plan" in msg.lower() or entry.get("code") == 403:
            return error_quote(t, "Not on free plan")
        return error_quote(t, "Unavailable")
    try:
        return parse_series(t, entry, chart_range) if use_series else parse_quote(t, entry)
    except (KeyError, ValueError, TypeError, IndexError) as e:
        logger.warning(f"Stocks: could not parse {t['symbol']}: {e}")
        return error_quote(t, "Unavailable")


def parse_quote(t, q):
    price = float(q["close"])
    prev = float(q.get("previous_close") or price)
    ts = q.get("last_quote_at") or q.get("timestamp")
    return build_quote(t, price, prev, points=[], name=q.get("name"),
                       ts=int(ts) if ts else None, market_open=bool(q.get("is_market_open")),
                       instrument=q.get("type", ""))


def parse_series(t, entry, chart_range):
    meta = entry.get("meta", {})
    tz = pytz.timezone(meta.get("exchange_timezone") or "UTC")
    bars = []
    for v in entry["values"]:
        fmt = "%Y-%m-%d %H:%M:%S" if " " in v["datetime"] else "%Y-%m-%d"
        bars.append((datetime.strptime(v["datetime"], fmt), float(v["close"])))
    bars.sort(key=lambda b: b[0])
    if not bars:
        raise ValueError("empty series")

    points, prev = split_range(bars, chart_range)
    last_dt = tz.localize(bars[-1][0])
    intraday = RANGES[chart_range][0].endswith("min")
    is_crypto = "/" in t["symbol"] and meta.get("type", "").lower().startswith("digital")
    # Intraday bars are stamped with their start time; treat a recent bar as "market open".
    market_open = intraday and (datetime.now(pytz.utc) - last_dt).total_seconds() < 20 * 60 and not is_crypto
    ts = int(last_dt.timestamp()) if intraday else None
    return build_quote(t, points[-1], prev, points, name=None, ts=ts,
                       market_open=market_open, instrument=meta.get("type", ""))


def split_range(bars, chart_range):
    """bars: [(naive datetime in exchange tz, close)] oldest first.
    Returns (points within the range, close just before the range began)."""
    last = bars[-1][0]
    if chart_range == "1d":
        start = datetime(last.year, last.month, last.day)
    elif chart_range == "5d":
        dates = sorted({b[0].date() for b in bars})
        d = dates[-5] if len(dates) >= 5 else dates[0]
        start = datetime(d.year, d.month, d.day)
    elif chart_range == "ytd":
        start = datetime(last.year, 1, 1)
    else:
        start = last - timedelta(days=RANGE_DAYS[chart_range])

    inside = [c for dt, c in bars if dt >= start]
    before = [c for dt, c in bars if dt < start]
    if not inside:
        inside = [bars[-1][1]]
    prev = before[-1] if before else inside[0]
    return inside, prev


def build_quote(t, price, prev, points, name, ts, market_open, instrument):
    change = price - prev
    pct = (change / prev * 100) if prev else 0.0
    display_name = t["name"] or clean_name(name or "") or t["input"].lstrip("^")
    sub = None
    if display_name.upper() != t["symbol"] and not t["fallback"] and t["input"] not in ALIASES:
        sub = t["symbol"]
    return {
        "symbol": t["symbol"],
        "name": display_name,
        "sub": sub,
        "price": format_price(price),
        "change": f"{change:+,.4f}" if price < 1 else f"{change:+,.2f}",
        "pct": f"{pct:+.2f}%",
        "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
        "points": points,
        "prev_close": prev,
        "time": ts,
        "market_open": market_open,
        "instrument": instrument,
    }


def clean_name(name):
    name = re.sub(r",?\s+(Inc\.?|Corporation|Corp\.?|Incorporated|Ltd\.?|plc|Co\.)$", "", name.strip())
    return name[:28]


def format_price(v):
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def market_status(quotes):
    equities = [q for q in quotes if not q.get("error")
                and "digital" not in q.get("instrument", "").lower()
                and "/" not in q["symbol"]]
    if not equities:
        return ""
    return "Market open" if any(q["market_open"] for q in equities) else "Market closed"


def should_grid(n, width, height):
    """Two columns of cards when the screen is wide and there are enough tickers."""
    return width > height * 1.2 and n >= 6


# --------------------------------------------------------------------------
# Sparkline
# --------------------------------------------------------------------------

def build_sparkline(points, prev_close, max_points=120):
    if len(points) < 2:
        return None
    if len(points) > max_points:
        step = len(points) / max_points
        sampled = [points[int(i * step)] for i in range(max_points)]
        sampled[-1] = points[-1]
        points = sampled

    lo, hi = min(points + [prev_close]), max(points + [prev_close])
    if math.isclose(lo, hi):
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    def y(v):
        return SPARK_H - (v - lo) / (hi - lo) * SPARK_H

    n = len(points)
    coords = [(i / (n - 1) * SPARK_W, y(v)) for i, v in enumerate(points)]
    line = " ".join(f"{x:.1f},{yv:.1f}" for x, yv in coords)
    area = f"0,{SPARK_H} " + line + f" {SPARK_W},{SPARK_H}"
    return {"line": line, "area": area, "baseline": round(y(prev_close), 1),
            "w": SPARK_W, "h": SPARK_H}
