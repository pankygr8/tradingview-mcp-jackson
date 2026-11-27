"""
nse_mcp.py — minimal client for NSE's official MCP server (end-of-day bhavcopy).

    https://mcp.nseindia.in/bhavcopy/cm/mcp      streamable HTTP, no login, ~5 years of daily data

MCP is JSON-RPC over HTTP: initialize → notifications/initialized → tools/call.
The server answers in plain JSON or as server-sent events ("data: {...}" lines);
both are handled. Tool results are JSON text inside result.content[].text.

Tools used here (argument names as the server publishes them):
    get_stock_history     {symbol, months (max 3), endDate "yyyy-MM-dd" | "today"}
                          → {data: [{date, series, open, high, low, close, prevClose, volume, ...}],
                             from_date, to_date, next_end_date}
    get_bulk_quote        {symbols: [... max 50]} → latest EOD bar per symbol
    get_corporate_actions {symbol, fromDate, toDate} → {actions: [{exDate, actionType, purpose,
                          adjustmentFactor}]}  — factor multiplies prices BEFORE the ex-date
Prices from the server are NOT adjusted for splits/bonus; data.py applies the factors.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request

URL = os.environ.get("NSE_MCP_URL", "https://mcp.nseindia.in/bhavcopy/cm/mcp")
PAUSE = float(os.environ.get("NSE_MCP_PAUSE", "0.25"))     # politeness gap per call, per session


class MCPError(RuntimeError):
    """Transport / protocol problem — worth retrying."""


class ToolError(MCPError):
    """The tool itself refused (unknown symbol, bad date) — retrying will not help."""


class NSEMCP:
    def __init__(self, url: str = URL, timeout: int = 90):
        self.url = url
        self.timeout = timeout
        self.sid: str | None = None
        self.calls = 0
        self._id = 0
        self._last = 0.0

    # ── transport ────────────────────────────────────────────────────────────
    def _post(self, body: dict) -> bytes:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "gzip",
            "User-Agent": "gann-square-agent/1.0",
        }
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        wait = PAUSE - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.sid = sid
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):            # expired / unknown session → reconnect
                self.sid = None
            raise MCPError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        finally:
            self._last = time.time()
        return raw

    @staticmethod
    def _envelope(raw: bytes) -> dict:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return {}
        if text.startswith("{"):
            return json.loads(text)
        found = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:") and line[5:].strip():
                found = json.loads(line[5:].strip())
        if found is None:
            raise MCPError("unreadable response: " + text[:200])
        return found

    def _next(self) -> int:
        self._id += 1
        return self._id

    def connect(self) -> dict:
        self.sid = None
        self.calls += 1
        env = self._envelope(self._post({
            "jsonrpc": "2.0", "id": self._next(), "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "gann-square-agent", "version": "1.0"}}}))
        if "error" in env:
            raise MCPError(json.dumps(env["error"])[:300])
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return (env.get("result") or {}).get("serverInfo") or {}

    def list_tools(self) -> list[dict]:
        if self.sid is None:
            self.connect()
        self.calls += 1
        env = self._envelope(self._post({"jsonrpc": "2.0", "id": self._next(), "method": "tools/list", "params": {}}))
        return (env.get("result") or {}).get("tools") or []

    def call(self, tool: str, args: dict, retries: int = 4):
        last = None
        for attempt in range(1, retries + 1):
            try:
                if self.sid is None:
                    self.connect()
                self.calls += 1
                env = self._envelope(self._post({
                    "jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                    "params": {"name": tool, "arguments": args}}))
                if "error" in env:
                    self.sid = None
                    raise MCPError(json.dumps(env["error"])[:300])
                result = env.get("result") or {}
                if isinstance(result.get("structuredContent"), (dict, list)) and not result.get("isError"):
                    return result["structuredContent"]
                text = "\n".join(c.get("text") or "" for c in result.get("content") or [] if c.get("type") == "text")
                if result.get("isError"):
                    raise ToolError(f"{tool}: {text[:300]}")
                try:
                    return json.loads(text) if text else {}
                except json.JSONDecodeError:
                    return {"text": text}
            except ToolError:
                raise
            except (MCPError, urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
                last = exc
                self.sid = None
            if attempt < retries:
                time.sleep(2.0 * attempt)
        raise MCPError(f"{tool} failed after {retries} attempts: {last}")


# One session per thread (the session id is per connection).
_local = threading.local()


def client() -> NSEMCP:
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = NSEMCP()
    return c


def rows_of(payload) -> list[dict]:
    """The list of bar/quote dicts inside a tool payload, whatever key it sits under."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "quotes", "results", "stocks", "records", "items", "actions"):
        v = payload.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
        if isinstance(v, dict):                     # {symbol: {...}} shape
            vals = [dict(x, symbol=x.get("symbol", k)) for k, x in v.items() if isinstance(x, dict)]
            if vals:
                return vals
    vals = [v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
    return vals[0] if vals else []


def pick(row: dict, *names, default=None):
    """Field lookup that tolerates camelCase / snake_case / upper-case variants."""
    low = {str(k).lower().replace("_", ""): v for k, v in row.items()}
    for n in names:
        v = low.get(n.lower().replace("_", ""))
        if v not in (None, ""):
            return v
    return default
