"""Trade signal notifications — optional webhook delivery.

The advisory engine records every ENTER/EXIT/STOP/TAKE_PROFIT to
data/trade_history.json; this module mirrors the same events to a webhook
URL (Discord, Slack, Ntfy, generic) when configured.

Webhook URL comes from the TRADE_WEBHOOK_URL environment variable. Example
values:

    Discord:   https://discord.com/api/webhooks/<id>/<token>
    Slack:     https://hooks.slack.com/services/T00000000/B00000000/XXXX
    Ntfy:      https://ntfy.sh/mytopic
    Generic:   any endpoint that accepts a JSON POST

Delivery is fire-and-forget on a daemon thread — it must never block the
pipeline or fail it. Webhook errors are logged at WARNING, never raised.
"""

import logging
import os
import threading

logger = logging.getLogger("sentinel.notifications")

WEBHOOK_URL = os.getenv("TRADE_WEBHOOK_URL", "").strip()

# We collapse repeated notifications for the same bracket+signal so a poll
# loop firing every few seconds doesn't spam a channel with duplicates.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()
_MIN_REPEAT_SECONDS = 300.0  # 5 minutes between identical notifications


def is_enabled() -> bool:
    return bool(WEBHOOK_URL)


def _build_payload(event: dict) -> dict:
    signal = event.get("signal", "")
    bracket = event.get("bracket", "")
    side = event.get("side", "")
    stake = event.get("stake_usd")
    edge = event.get("edge")
    stock_price = event.get("entry_price")
    exit_price = event.get("exit_price")
    pnl_pct = event.get("pnl_pct")
    reason = event.get("reason", "")

    lines = [
        f"**{signal}** `{bracket}`" + (f" `({side})`" if side else ""),
    ]
    if stock_price is not None:
        lines.append(f"Entry: ${stock_price:.2f}")
    if exit_price is not None:
        lines.append(f"Exit: ${exit_price:.2f}")
    if stake is not None:
        lines.append(f"Stake: ${stake:.2f}")
    if edge is not None:
        lines.append(f"Edge: {edge:.1%}")
    if pnl_pct is not None:
        lines.append(f"P&L: {pnl_pct:+.2%}")
    if reason:
        lines.append(f"Reason: {reason}")

    return {
        "text": "Sentinel Trade Signal",
        "content": f"**Sentinel** — trade signal",
        "message": "\n".join(lines),
        "sentinel": {
            "type": "trade_signal",
            "signal": signal,
            "bracket": bracket,
            "side": side,
            "edge": edge,
            "stake_usd": stake,
            "entry_price": stock_price,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
        },
    }


def _dedupe_key(event: dict) -> str:
    return f"{event.get('signal')}|{event.get('bracket')}|{event.get('side', '')}"


def _should_send(event: dict) -> bool:
    import time as _time

    now = _time.time()
    key = _dedupe_key(event)
    with _lock:
        last = _last_sent.get(key, 0.0)
        if now - last < _MIN_REPEAT_SECONDS:
            return False
        _last_sent[key] = now
        return True


def notify(event: dict) -> None:
    """Send a trade-signal notification to the configured webhook (async).

    Muted entirely when TRADE_WEBHOOK_URL is unset. A duplicate event for the
    same bracket+signal within 5 minutes is suppressed.
    """
    if not WEBHOOK_URL or not event.get("signal"):
        return
    if not _should_send(event):
        return

    payload = _build_payload(event)

    def _deliver():
        try:
            import requests

            if "ntfy" in WEBHOOK_URL:
                requests.post(
                    WEBHOOK_URL,
                    data=payload["content"].encode("utf-8"),
                    headers={
                        "Title": payload["text"],
                        "Priority": "default",
                        "Tags": "thermometer",
                    },
                    timeout=5,
                )
            else:
                requests.post(
                    WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=5,
                )
        except Exception as exc:  # noqa: BLE001 — notifications must never throw
            logger.warning("Webhook delivery failed: %s", exc)

    threading.Thread(target=_deliver, daemon=True, name="notify-webhook").start()


def notify_entry(bracket: str, side: str, price: float, stake: float, edge: float) -> None:
    notify({
        "signal": f"ENTER_{side}",
        "bracket": bracket,
        "side": side,
        "entry_price": price,
        "stake_usd": stake,
        "edge": edge,
    })


def notify_exit(bracket: str, side: str, entry_price: float, exit_price: float,
                pnl_pct: float, signal: str) -> None:
    notify({
        "signal": signal,
        "bracket": bracket,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
    })