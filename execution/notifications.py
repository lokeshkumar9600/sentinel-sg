"""Trade signal notifications — optional webhook + email delivery.

The advisory engine records every ENTER/EXIT/STOP/TAKE_PROFIT to
data/trade_history.json; this module mirrors the same events to a webhook
URL (Discord, Slack, Ntfy, generic) AND/OR an email address when configured.

Webhook URL comes from the TRADE_WEBHOOK_URL environment variable. Example
values:

    Discord:   https://discord.com/api/webhooks/<id>/<token>
    Slack:     https://hooks.slack.com/services/T00000000/B00000000/XXXX
    Ntfy:      https://ntfy.sh/mytopic
    Generic:   any endpoint that accepts a JSON POST

Email delivery uses SMTP. Configure with:

    NOTIFY_EMAIL_RECIPIENTS=you@example.com,another@example.com
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587              # or 465 for SSL
    SMTP_USER=you@gmail.com    # the authenticated sender
    SMTP_PASSWORD=app-password # Gmail App Password (NOT your login password)
    SMTP_USE_SSL=false         # true for port 465, false (STARTTLS) for 587

Gmail: enable 2-Step Verification, then create an App Password at
https://myaccount.google.com/apppasswords and use it here.

Delivery is fire-and-forget on a daemon thread — it must never block the
pipeline or fail it. Webhook/email errors are logged at WARNING, never raised.
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

logger = logging.getLogger("sentinel.notifications")

WEBHOOK_URL = os.getenv("TRADE_WEBHOOK_URL", "").strip()

# ── Email config ─────────────────────────────────────────────────────────────
EMAIL_RECIPIENTS = [
    r.strip()
    for r in os.getenv("NOTIFY_EMAIL_RECIPIENTS", "").split(",")
    if r.strip()
]
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "").strip().lower() == "true"
EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", SMTP_USER or "Sentinel <no-reply@localhost>").strip()

# We collapse repeated notifications for the same bracket+signal so a poll
# loop firing every few seconds doesn't spam a channel with duplicates.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()
_MIN_REPEAT_SECONDS = 300.0  # 5 minutes between identical notifications


def is_enabled() -> bool:
    return bool(WEBHOOK_URL or (EMAIL_RECIPIENTS and SMTP_HOST))


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


def _send_email(event: dict) -> None:
    """Send a trade-signal email to all configured recipients via SMTP."""
    if not EMAIL_RECIPIENTS or not SMTP_HOST or not SMTP_USER:
        return

    signal = event.get("signal", "")
    bracket = event.get("bracket", "")
    side = event.get("side", "")
    entry = event.get("entry_price")
    exit_price = event.get("exit_price")
    stake = event.get("stake_usd")
    edge = event.get("edge")
    pnl_pct = event.get("pnl_pct")
    reason = event.get("reason", "")

    subject = f"Sentinel {signal} {bracket or ''}".strip()
    body = f"Sentinel — trade signal\n{'=' * 48}\n\n"
    body += f"Signal: {signal}\n"
    body += f"Bracket: {bracket}\n"
    if side:
        body += f"Side: {side}\n"
    if entry is not None:
        body += f"Entry: ${float(entry):.2f}\n"
    if exit_price is not None:
        body += f"Exit: ${float(exit_price):.2f}\n"
    if stake is not None:
        body += f"Stake: ${float(stake):.2f}\n"
    if edge is not None:
        body += f"Edge: {float(edge):.1%}\n"
    if pnl_pct is not None:
        body += f"P&L: {float(pnl_pct):+.2%}\n"
    if reason:
        body += f"Reason: {reason}\n"
    body += f"\nSent at: {_now_sgt()}\n"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg.set_content(body)

    try:
        if SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with server:
            if not SMTP_USE_SSL:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — notifications must never throw
        logger.warning("Email delivery failed: %s", exc)


def send_test_email() -> tuple[bool, str]:
    """Send a test email to the configured recipients.

    Lets the owner verify SMTP credentials without waiting for a real trade
    signal. Returns (ok, message).
    """
    if not (SMTP_HOST and SMTP_USER and EMAIL_RECIPIENTS):
        return False, (
            "Email not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and "
            "NOTIFY_EMAIL_RECIPIENTS (see .env.example)."
        )
    try:
        _send_email({
            "signal": "TEST",
            "bracket": "—",
            "side": "",
            "entry_price": None,
            "exit_price": None,
            "stake_usd": None,
            "edge": None,
            "pnl_pct": None,
            "reason": "Sentinel email notifications are working.",
        })
        return True, f"Test email queued for delivery to {', '.join(EMAIL_RECIPIENTS)}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to queue test email: {exc}"


if __name__ == "__main__":
    ok, msg = send_test_email()
    print(msg)
    raise SystemExit(0 if ok else 1)


def _now_sgt() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d %H:%M:%S SGT")
    except Exception:  # noqa: BLE001
        import datetime as _dt
        return str(_dt.datetime.now())


def notify(event: dict) -> None:
    """Send a trade-signal notification to webhook and/or email (async).

    Muted entirely when no channel is configured (TRADE_WEBHOOK_URL and
    NOTIFY_EMAIL_RECIPIENTS both unset/smtp unconfigured). A duplicate event
    for the same bracket+signal within 5 minutes is suppressed.
    """
    if not event.get("signal"):
        return
    if not (WEBHOOK_URL or (SMTP_HOST and EMAIL_RECIPIENTS)):
        return
    if not _should_send(event):
        return

    payload = _build_payload(event)

    def _deliver():
        if WEBHOOK_URL:
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
            except Exception as exc:  # noqa: BLE001 — must never throw
                logger.warning("Webhook delivery failed: %s", exc)
        if EMAIL_RECIPIENTS and SMTP_HOST:
            _send_email(event)

    threading.Thread(target=_deliver, daemon=True, name="notify-signal").start()


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