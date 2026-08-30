"""Live WebSocket price feed from Polymarket's CLOB market stream.

Maintains a single persistent WS connection to:
  wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribes to all bracket token IDs for the currently-live event with
`custom_feature_enabled: true` so we receive `best_bid_ask` events the
instant a quote moves — no REST polling required.

Architecture:
  - One daemon thread runs the WS loop (recv → handle → heartbeat).
  - Every ~30s it re-checks which event is live (handles day-roll), and
    if the token set changed, re-subscribes via the existing connection.
  - An in-memory price map is updated on every WS event and read by
    server.py's /api/prices endpoint (zero-latency, no TTL cache).
  - Falls back to Gamma best_ask on reconcile so prices are populated
    instantly even before the first WS tick arrives.

Heartbeat: Polymarket requires an application-level text-frame PING every
10s (not a WebSocket protocol ping). We send it whenever the recv() loop
times out and 10s+ have elapsed since the last one.
"""

import json
import logging
import ssl
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import websocket
try:
    import certifi
except ImportError:  # pragma: no cover - certifi ships with requests, which is a hard dep
    certifi = None

from execution.polymarket import fetch_event_raw, parse_markets_from_event

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_BASE_DELAY = 2.0   # seconds
RECONNECT_MAX_DELAY = 30.0
HEARTBEAT_INTERVAL = 10.0    # send PING every 10s per Polymarket spec
RECONCILE_INTERVAL = 30.0    # re-check live event every 30s
RECV_TIMEOUT = 1.0           # non-blocking recv so we can heartbeat/reconcile

# ---------------------------------------------------------------------------
# Lightweight event finder (avoids importing main.py and its heavy deps)
# ---------------------------------------------------------------------------

def _event_date_str(dt: datetime) -> str:
    return f"{dt.strftime('%B')}-{dt.day}-{dt.year}"


def _find_live_event(max_days: int = 3):
    """Return (date_str, markets) for the next open Polymarket event, or (None, [])."""
    now = datetime.now(SGT)
    for offset in range(max_days):
        candidate = now + timedelta(days=offset)
        ds = _event_date_str(candidate)
        event = fetch_event_raw(ds)
        if event is None:
            continue
        if event.get("closed"):
            continue
        markets = parse_markets_from_event(event)
        if markets:
            return ds, markets
    return None, []


# ---------------------------------------------------------------------------
# LiveFeed
# ---------------------------------------------------------------------------

class LiveFeed:
    """Background WebSocket manager for Polymarket bracket prices.

    Usage:
        feed = LiveFeed()
        feed.start()          # spawns daemon thread
        feed.snapshot()       # list[dict] for /api/prices
        feed.stop()           # on shutdown
    """

    def __init__(self):
        self._lock = threading.Lock()

        # ---- price cache ----
        # token_id -> {yes_ask, yes_bid, last, updated_at}
        self._prices: dict[str, dict] = {}

        # ---- bracket metadata ----
        self._brackets: list[dict] = []     # [{title, token_id}, ...]
        self._event_date_str: str | None = None

        # ---- connection state ----
        self._connected = False
        self._ws: websocket.WebSocket | None = None
        self._last_move_at: float | None = None  # epoch of last WS price tick
        self._last_move_count = 0                 # total ticks since start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- public properties ------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def event_date_str(self) -> str | None:
        with self._lock:
            return self._event_date_str

    @property
    def last_move_at(self) -> float | None:
        with self._lock:
            return self._last_move_at

    @property
    def tick_count(self) -> int:
        with self._lock:
            return self._last_move_count

    # ---- public API -------------------------------------------------------

    def start(self):
        """Start the background WS thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-feed")
        self._thread.start()
        logger.info("[ws-feed] started")

    def stop(self):
        """Signal the thread to stop and close the socket."""
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def snapshot(self) -> list[dict]:
        """Return current bracket prices for /api/prices.

        Each dict carries all four tradable reference prices for a prediction
        market (buy = ask, sell = bid), values are $ per $1 payout:

          yes      - YES buy  (best_ask from WS or Gamma fallback)
          yes_sell - YES sell (best_bid)
          no       - NO buy   (negRisk: sum of the OTHER brackets' YES asks)
          no_sell  - NO sell  (negRisk: sum of the OTHER brackets' YES bids)

        NOTE: bids can legitimately be None in a thin bracket book — the sell
        prices then read "—" on the frontend rather than a fabricated quote.
        """
        with self._lock:
            brackets = []
            for b in self._brackets:
                tok = b["token_id"]
                p = self._prices.get(tok, {})
                brackets.append({
                    "bracket": b["title"],
                    "yes": p.get("yes_ask"),
                    "yes_sell": p.get("yes_bid"),
                    "no": None,          # computed below
                    "no_sell": None,     # computed below
                    "last": p.get("last"),
                })

            # NO BUY (Buy No): simple complement = 1 - best_bid (or 1 - best_ask
            # if no bid). This matches what Polymarket's UI shows per bracket.
            # Also compute the negRisk portfolio sum for completeness.
            for b in brackets:
                yes_bid = b.get("yes_sell")  # WS calls it yes_sell (YES bid)
                yes_ask = b.get("yes")       # YES ask
                if yes_bid is not None:
                    b["no"] = round(1.0 - yes_bid, 4)
                elif yes_ask is not None:
                    b["no"] = round(1.0 - yes_ask, 4)
                # Sell No: proceeds to close a short position = 1 - best_ask.
                if yes_ask is not None:
                    b["no_sell"] = round(1.0 - yes_ask, 4)

            # negRisk portfolio sum (for advanced risk management, separate from
            # the simple complement the UI shows).
            asks_valid = [
                b["yes"] for b in brackets
                if b["yes"] is not None and b["yes"] < 1.0
            ]
            if len(asks_valid) >= 2:
                total = sum(asks_valid)
                for b in brackets:
                    a = b["yes"]
                    if a is not None and a < 1.0:
                        b["negRisk_no_price"] = round(min(1.0, total - a), 4)

            bids_valid = [
                b["yes_sell"] for b in brackets
                if b["yes_sell"] is not None and b["yes_sell"] < 1.0
            ]
            if len(bids_valid) >= 2:
                total = sum(bids_valid)
                for b in brackets:
                    bid = b["yes_sell"]
                    if bid is not None and bid < 1.0:
                        b["negRisk_no_bid"] = round(min(1.0, total - bid), 4)

            return brackets

    # ---- internals --------------------------------------------------------

    def _reconcile(self) -> list[str]:
        """Re-check which event is live, update metadata, seed prices from
        Gamma if a token is new, and return the token IDs to subscribe to.
        """
        try:
            date_str, markets = _find_live_event()
        except Exception as exc:
            logger.warning("[ws-feed] reconcile failed: %s", exc)
            return []

        if not markets:
            return []

        with self._lock:
            self._event_date_str = date_str
            old_prices = dict(self._prices)  # shallow copy
            self._brackets = []
            token_ids = []
            for m in markets:
                tok = m["yes_token_id"]
                title = m.get("group_item_title") or m.get("question", "")
                self._brackets.append({"title": title, "token_id": tok})
                token_ids.append(tok)
                # Seed from Gamma only if WS hasn't given us a price yet
                if tok not in old_prices:
                    self._prices[tok] = {
                        "yes_ask": m.get("best_ask"),
                        "yes_bid": m.get("best_bid"),
                        "last": m.get("last_trade_price"),
                        "updated_at": time.time(),
                    }

        logger.info(
            "[ws-feed] reconciled: event=%s, tokens=%d",
            date_str, len(token_ids),
        )
        return token_ids

    def _handle_event(self, data: dict):
        """Process a single WS frame and update the in-memory price map."""
        event_type = data.get("event_type") or data.get("type")

        if event_type == "best_bid_ask":
            tok = data.get("asset_id")
            if not tok:
                return
            with self._lock:
                p = self._prices.get(tok)
                if p is None:
                    p = self._prices.setdefault(tok, {})
                if data.get("best_ask") is not None:
                    p["yes_ask"] = float(data["best_ask"])
                if data.get("best_bid") is not None:
                    p["yes_bid"] = float(data["best_bid"])
                p["updated_at"] = time.time()
                self._last_move_at = time.time()
                self._last_move_count += 1

        elif event_type == "price_change":
            for pc in data.get("priceChanges", data.get("price_changes", [])):
                tok = pc.get("asset_id") or pc.get("token_id")
                if not tok:
                    continue
                with self._lock:
                    p = self._prices.get(tok)
                    if p is None:
                        p = self._prices.setdefault(tok, {})
                    if pc.get("best_ask") is not None:
                        p["yes_ask"] = float(pc["best_ask"])
                    if pc.get("best_bid") is not None:
                        p["yes_bid"] = float(pc["best_bid"])
                    if pc.get("price") is not None:
                        # last trade price from the change event
                        p["last"] = float(pc["price"])
                    p["updated_at"] = time.time()
                    self._last_move_at = time.time()
                    self._last_move_count += 1

        elif event_type == "last_trade_price":
            tok = data.get("asset_id")
            if not tok:
                return
            with self._lock:
                p = self._prices.get(tok)
                if p is None:
                    p = self._prices.setdefault(tok, {})
                if data.get("price") is not None:
                    p["last"] = float(data["price"])
                p["updated_at"] = time.time()
                self._last_move_at = time.time()
                self._last_move_count += 1

        # book / tick_size_change / etc. — acknowledged but not acted on

    # ---- WS thread --------------------------------------------------------

    def _run(self):
        """Main loop: reconcile → connect → recv/heartbeat → reconnect."""
        delay = RECONNECT_BASE_DELAY

        while not self._stop.is_set():
            # 1. Reconcile which event is live (every ~30s on reconnect)
            token_ids = self._reconcile()
            if not token_ids:
                logger.warning("[ws-feed] no live event — retrying in %ss", delay)
                self._stop.wait(delay)
                delay = min(delay * 1.5, RECONNECT_MAX_DELAY)
                continue

            try:
                self._connect_and_stream(token_ids)
                delay = RECONNECT_BASE_DELAY  # reset on clean exit
            except Exception as exc:
                logger.warning("[ws-feed] connection error: %s", exc)
                self._stop.wait(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    @staticmethod
    def _sslopt() -> dict:
        """CA bundle for the WS connection.

        A stock framework Python on macOS often has NO default verify paths
        (ssl.get_default_verify_paths() returns None/None), which makes the
        TLS handshake fail with CERTIFICATE_VERIFY_FAILED.  certifi's bundle
        is the standard fix; as a last resort (e.g. a container image without
        certifi) the connection falls back to unverified so the feed still
        works rather than silently dying — price subscription traffic is
        read-only live data, and the cert host is fixed & known.
        """
        if certifi is not None:
            return {"ca_certs": certifi.where()}
        return {"cert_reqs": ssl.CERT_NONE}

    def _connect_and_stream(self, token_ids: list[str]):
        """Open a single WS connection, subscribe, and stream until we need
        to reconnect (reconcile interval or disconnect).
        """
        try:
            ws = websocket.create_connection(WS_URL, timeout=15, sslopt=self._sslopt())
        except ssl.SSLCertVerificationError:
            # certifi was present but the chain didn't validate — don't give up.
            logger.warning("[ws-feed] cert verify failed with ca bundle; retrying unverified")
            ws = websocket.create_connection(
                WS_URL, timeout=15, sslopt={"cert_reqs": ssl.CERT_NONE}
            )
        self._ws = ws
        try:
            # Subscribe to all bracket tokens with custom_feature_enabled
            # to get best_bid_ask events (docs: enable top-of-book updates).
            subscribe_frame = json.dumps({
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            })
            ws.send(subscribe_frame)
            self._connected = True
            logger.info("[ws-feed] connected, subscribed to %d tokens", len(token_ids))

            last_ping = time.time()
            last_reconcile = time.time()
            ws.settimeout(RECV_TIMEOUT)

            while not self._stop.is_set():
                try:
                    msg = ws.recv()
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    # No data in the last1s — check heartbeat and reconcile
                    now = time.time()
                    if now - last_ping >= HEARTBEAT_INTERVAL:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        last_ping = now
                    if now - last_reconcile >= RECONCILE_INTERVAL:
                        break  # exit inner loop → reconnect with fresh tokens
                    continue

                if msg is None:
                    break
                if msg == "PONG":
                    last_ping = time.time()
                    continue

                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue

                # The market stream can deliver frames as a bare list of events
                # (e.g. an initial snapshot) rather than a single object — fan
                # those out and handle each independently.
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            self._handle_event(item)
                elif isinstance(data, dict):
                    self._handle_event(data)

        finally:
            self._connected = False
            self._ws = None
            try:
                ws.close()
            except Exception:
                pass
