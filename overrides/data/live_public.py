"""Live public spot candles for PAPER trading.

No exchange credentials are used. Candle data are fetched from public spot
endpoints and converted to Ternary's MarketData interface. The paper broker
remains simulated; this module only supplies market prices/bars.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from data.store import Bar, MarketData
from sim.simulator import Quote


_INTERVALS = {
    "1m": ("1", "1min", "1m"),
    "5m": ("5", "5min", "5m"),
    "15m": ("15", "15min", "15m"),
    "30m": ("30", "30min", "30m"),
    "1h": ("60", "1hour", "1h"),
    "4h": ("240", "4hour", "4h"),
    "1d": ("D", "1day", "1d"),
    "1w": ("W", "1week", "1w"),
}


def _json(url: str, timeout: int = 8):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ternary-live-paper/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(symbol: str) -> str:
    s = (symbol or "").upper().strip().replace("-", "/")
    if s.endswith("USDT") and "/" not in s:
        s = s[:-4] + "/USDT"
    if not s.endswith("/USDT"):
        raise ValueError("live PAPER currently supports USDT spot pairs")
    return s


def _bybit(symbol: str, interval: str, limit: int) -> list[Bar]:
    s = _norm(symbol)
    if interval not in _INTERVALS:
        raise ValueError("unsupported candle interval")
    q = urllib.parse.urlencode({
        "category": "spot",
        "symbol": s.replace("/", ""),
        "interval": _INTERVALS[interval][0],
        "limit": max(60, min(int(limit), 1000)),
    })
    d = _json("https://api.bybit.com/v5/market/kline?" + q)
    if int(d.get("retCode", -1)) != 0:
        raise RuntimeError("Bybit: " + str(d.get("retMsg") or "request failed"))
    raw = (d.get("result") or {}).get("list") or []
    bars = [Bar(int(k[0]) * 1_000_000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            for k in reversed(raw)]
    if not bars:
        raise RuntimeError("Bybit returned no candles")
    return bars


def _kucoin(symbol: str, interval: str, limit: int) -> list[Bar]:
    s = _norm(symbol)
    if interval not in _INTERVALS:
        raise ValueError("unsupported candle interval")
    q = urllib.parse.urlencode({"symbol": s.replace("/", "-"), "type": _INTERVALS[interval][1]})
    d = _json("https://api.kucoin.com/api/v1/market/candles?" + q)
    if str(d.get("code")) != "200000":
        raise RuntimeError("KuCoin request failed")
    raw = ((d.get("data") or [])[:max(60, min(int(limit), 1500))])
    bars = [Bar(int(k[0]) * 1_000_000_000, float(k[1]), float(k[3]), float(k[4]), float(k[2]), float(k[5]))
            for k in reversed(raw)]
    if not bars:
        raise RuntimeError("KuCoin returned no candles")
    return bars


def _binance(symbol: str, interval: str, limit: int) -> list[Bar]:
    s = _norm(symbol)
    if interval not in _INTERVALS:
        raise ValueError("unsupported candle interval")
    q = urllib.parse.urlencode({
        "symbol": s.replace("/", ""),
        "interval": _INTERVALS[interval][2],
        "limit": max(60, min(int(limit), 1000)),
    })
    raw = _json("https://api.binance.com/api/v3/klines?" + q)
    bars = [Bar(int(k[0]) * 1_000_000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]
    if not bars:
        raise RuntimeError("Binance returned no candles")
    return bars


_PROVIDERS = (("bybit", _bybit), ("kucoin", _kucoin), ("binance", _binance))


class PublicSpotMarketData(MarketData):
    """Cached live candle feed with provider fallback.

    `bars()` returns the latest public candles, including the currently-forming
    candle. `quote_at()` derives a deterministic top-of-book around that same
    candle close so PAPER fills and mark-to-market use the same price domain.
    """
    is_live = True
    source_name = "public_spot_candles"

    def __init__(self, symbols, interval="1h", limit=600, cache_seconds=20,
                 spread_bps=6.0, depth_frac=0.05, fetcher=None):
        if interval not in _INTERVALS:
            interval = "1h"
        self.symbols = tuple(dict.fromkeys(_norm(s) for s in symbols))
        self.interval = interval
        self.limit = max(80, min(int(limit), 1000))
        self.cache_ns = max(5, int(cache_seconds)) * 1_000_000_000
        self.spread_bps = float(spread_bps)
        self.depth_frac = float(depth_frac)
        self._fetcher = fetcher
        self._cache = {}       # symbol -> {bars, fetched_ns, provider, error}
        self._preferred = None
        self._lock = threading.RLock()

    def _fetch(self, symbol: str):
        if self._fetcher is not None:
            bars = list(self._fetcher(symbol, self.interval, self.limit))
            if not bars:
                raise RuntimeError("injected live fetcher returned no candles")
            return bars, "injected"
        providers = list(_PROVIDERS)
        if self._preferred:
            providers.sort(key=lambda x: 0 if x[0] == self._preferred else 1)
        errors = []
        for name, fn in providers:
            try:
                bars = fn(symbol, self.interval, self.limit)
                self._preferred = name
                return bars, name
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("all public candle providers failed — " + " | ".join(errors))

    def _refresh_symbol(self, symbol: str, force=False):
        now = time.time_ns()
        with self._lock:
            cur = self._cache.get(symbol)
            if cur and not force and now - int(cur.get("fetched_ns", 0)) < self.cache_ns:
                return cur
        try:
            bars, provider = self._fetch(symbol)
            row = {"bars": tuple(bars), "fetched_ns": time.time_ns(), "provider": provider, "error": None}
            with self._lock:
                self._cache[symbol] = row
            return row
        except Exception as exc:
            with self._lock:
                old = self._cache.get(symbol)
                if old:
                    old = dict(old)
                    old["error"] = str(exc)
                    self._cache[symbol] = old
                    return old
            raise

    def refresh(self, force=False):
        """Refresh the configured universe concurrently; returns a health summary."""
        ok, errors = 0, {}
        workers = max(1, min(8, len(self.symbols)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self._refresh_symbol, s, force): s for s in self.symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    row = fut.result()
                    if row.get("bars"):
                        ok += 1
                    if row.get("error"):
                        errors[sym] = row["error"]
                except Exception as exc:
                    errors[sym] = str(exc)
        return {"ok": ok, "total": len(self.symbols), "errors": errors, "provider": self._preferred}

    def bars(self, symbol: str):
        s = _norm(symbol)
        row = self._refresh_symbol(s)
        return row.get("bars", ())

    def quote_at(self, symbol: str, ts_ns: int):
        s = _norm(symbol)
        row = self._refresh_symbol(s)
        bars = row.get("bars") or ()
        if not bars:
            return None
        b = bars[-1]
        mid = float(b.close)
        if mid <= 0:
            return None
        half = mid * (self.spread_bps / 1e4) / 2
        size = max(1e-6, float(b.volume) * self.depth_frac)
        # Quote age is the age of the successful HTTP refresh, not candle-open age.
        # This lets the gateway reject genuinely stale network data.
        qts = int(row.get("fetched_ns") or ts_ns)
        return Quote(ts_ns=qts, bid=mid - half, ask=mid + half,
                     bid_size=size, ask_size=size)

    def health(self):
        now = time.time_ns()
        with self._lock:
            ages = {s: (now - int(r.get("fetched_ns", 0))) / 1e9 for s, r in self._cache.items()}
            errors = {s: r.get("error") for s, r in self._cache.items() if r.get("error")}
        return {"source": self.source_name, "interval": self.interval,
                "provider": self._preferred, "cached": len(ages),
                "max_age_s": max(ages.values()) if ages else None, "errors": errors}
