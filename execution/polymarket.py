import json

import numpy as np
import requests

from data.config import GAMMA_API_URL, CLOB_API_URL


def fetch_event_raw(target_date_str: str) -> dict | None:
    """
    Fetch the raw event object for a given date (e.g. "August-29-2026"), or None if
    it doesn't exist (404) / the request fails. Kept separate from parsing so callers
    can inspect event-level fields like "closed" before deciding whether to use it -
    that's what lets the bot detect a resolved market and roll forward automatically.
    """
    event_slug = f"highest-temperature-in-singapore-on-{target_date_str.lower()}"
    url = f"{GAMMA_API_URL}/events/slug/{event_slug}"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"[Error] Polymarket Event Slug '{event_slug}': {e}")
        return None


def parse_markets_from_event(event: dict) -> list[dict]:
    """Extract tradeable (non-closed) brackets with valid CLOB token ids from a raw event."""
    markets_info = []
    for m in event.get("markets", []):
        if m.get("closed"):
            continue

        raw_tokens = m.get("clobTokenIds", "[]")
        try:
            clob_tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        except (json.JSONDecodeError, TypeError):
            clob_tokens = []

        if not clob_tokens:
            continue

        # BUG FIX: clobTokenIds[0] is NOT guaranteed to be the "Yes" token - Gamma
        # returns a parallel "outcomes" array (also JSON-encoded), and the two are
        # matched by position, not by a fixed Yes-first convention. Blindly taking
        # index 0 previously grabbed whichever token happened to be listed first,
        # which for these bracket markets was silently the "No" token - so every
        # price we fetched was actually the cost of betting AGAINST each bracket,
        # not for it (this is what caused the inverted prices you saw live).
        raw_outcomes = m.get("outcomes", "[]")
        try:
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        except (json.JSONDecodeError, TypeError):
            outcomes = []

        yes_index = 0  # fallback if "outcomes" is missing/malformed
        for i, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == "yes":
                yes_index = i
                break

        if yes_index >= len(clob_tokens):
            continue

        # Price source: Gamma's per-market metadata. These bracket markets are
        # negRisk (all brackets resolve against ONE temperature), so their
        # individual CLOB order books are thin/unreliable - the /book endpoint
        # returns 0.99/0.01 garbage for them. Gamma's bestAsk is the real ask
        # price (what we'd pay to buy Yes) and matches what the website shows.
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        markets_info.append({
            "question": m.get("question", ""),
            "yes_token_id": clob_tokens[yes_index],
            "group_item_title": m.get("groupItemTitle", ""),
            # YES side: bestAsk = buy price, bestBid = sell price.
            "best_ask": _f(m.get("bestAsk")),
            "best_bid": _f(m.get("bestBid")),
            "last_trade_price": _f(m.get("lastTradePrice")),
            # NO side: NOT a negRisk portfolio sum — the website shows the simple
            # complement (1 - YES_bid, or 1 - YES_ask if no bid). This is what the
            # UI calls "Buy No" and is the price you'd pay to short a bracket.
            "no_price": None,   # filled below (Buy No)
            "no_bid": None,     # filled below (Sell No)
        })

    # Now compute NO side using the simple complement rule (what Polymarket UI shows).
    for m in markets_info:
        yes_bid = m.get("best_bid")
        yes_ask = m.get("best_ask")
        # Buy No = 1 - best_bid (the price you'd pay to short this bracket).
        # Fall back to 1 - best_ask if there's no bid yet.
        if yes_bid is not None:
            m["no_price"] = round(1.0 - yes_bid, 4)
        elif yes_ask is not None:
            m["no_price"] = round(1.0 - yes_ask, 4)
        # Sell No = 1 - best_ask (the proceeds if you close the NO position).
        if yes_ask is not None:
            m["no_bid"] = round(1.0 - yes_ask, 4)

    # negRisk NO SELL price for bracket i = proceeds of selling YES on every OTHER
    # bracket (the mirror of the NO BUY portfolio sum, but for the bid side). This
    # is used when managing an open NO position: you close it by selling YES on all
    # other brackets and receive the sum of their bids. Keep this for position P&L.
    asks = [_f(m.get("best_ask")) for m in markets_info]
    bids = [_f(m.get("best_bid")) for m in markets_info]

    # NO BUY price for bracket i = cost of buying YES on every OTHER bracket
    # (the negRisk closing portfolio). a < 1.0 filters the "1.0 = no liquidity"
    # sentinel so it doesn't distort the group sum.
    if sum(1 for a in asks if a is not None and a < 1.0) >= 2:
        for i, m in enumerate(markets_info):
            other_sum = sum(
                a for j, a in enumerate(asks) if j != i and a is not None and a < 1.0
            )
            m["negRisk_no_price"] = round(min(1.0, other_sum), 4)
    else:
        for m in markets_info:
            m["negRisk_no_price"] = None

    # NO SELL price for bracket i = proceeds of selling YES on every OTHER
    # bracket (mirror of the above on the bid side). None bids (illiquid) are
    # simply excluded from the group sum.
    if sum(1 for b in bids if b is not None) >= 2:
        for i, m in enumerate(markets_info):
            other_sum = sum(
                b for j, b in enumerate(bids) if j != i and b is not None
            )
            m["negRisk_no_bid"] = round(min(1.0, other_sum), 4)
    else:
        for m in markets_info:
            m["negRisk_no_bid"] = None

    return markets_info


def get_polymarket_event_data(target_date_str: str) -> list[dict]:
    """Convenience wrapper: fetch + parse in one call. Returns [] if the event is missing."""
    event = fetch_event_raw(target_date_str)
    if not event:
        return []
    return parse_markets_from_event(event)


def get_live_clob_price(token_id: str) -> float:
    url = f"{CLOB_API_URL}/book?token_id={token_id}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        body = res.json()
        asks = body.get("asks", [])
        if asks:
            return float(asks[0].get("price", 1.0))
        # BUG FIX: previously this silently fell through to the 1.0 fallback with no
        # distinction between "genuinely no liquidity" and "the API returned something
        # unexpected" (e.g. an error payload with no 'asks' key). Surfacing it here
        # makes a real problem visible instead of looking identical to thin liquidity.
        print(f"[Warning] No asks in order book for token {token_id} (empty/no liquidity).")
    except Exception as e:
        print(f"[Error] CLOB Token {token_id}: {e}")
    # Defaulting to 1.0 (a "certain" price) is intentional: downstream, compute_kelly_trade
    # treats market_price >= 1.0 as SKIP, so a failed/illiquid lookup safely blocks a trade
    # instead of manufacturing a fake edge.
    return 1.0


def parse_temperature_bounds(title: str) -> tuple[float, float]:
    title = title.lower().strip()
    if "or lower" in title or "or below" in title:
        val = float(''.join(filter(lambda x: x.isdigit() or x == '.', title)))
        # BUG FIX: brackets are continuity-corrected (a "28C or below" bucket really means
        # temp <= 28.5 once you round to the nearest whole degree). This previously returned
        # the bare integer bound, which disagreed with main.py's hardcoded 28.5/37.5 boundaries.
        return -np.inf, val + 0.5
    elif "or higher" in title or "or above" in title:
        val = float(''.join(filter(lambda x: x.isdigit() or x == '.', title)))
        return val - 0.5, np.inf
    elif "°c" in title or "c" in title:
        val = float(''.join(filter(lambda x: x.isdigit() or x == '.', title)))
        return val - 0.5, val + 0.5
    return -np.inf, np.inf