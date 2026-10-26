import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from html import escape as html_escape
from urllib import request, error

import config
import mt5_connector as m5
import risk_manager as rm


_STATE_FILE = Path(__file__).parent / "logs" / "email_state.json"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


@dataclass(frozen=True)
class EmailConfig:
    api_key: str
    mail_from: str
    mail_to: str


def _get_email_config() -> EmailConfig | None:
    # Prefer process env vars, but also fallback to config's .env loader.
    api_key = (
        os.getenv("RESEND_API_KEY")
        or os.getenv("RESEND_KEY")
        or config._env("RESEND_API_KEY", "")
        or config._env("RESEND_KEY", "")
        or ""
    ).strip()
    mail_from = (
        os.getenv("RESEND_FROM")
        or os.getenv("EMAIL_FROM")
        or config._env("RESEND_FROM", "")
        or config._env("EMAIL_FROM", "")
        or ""
    ).strip()
    mail_to = (
        os.getenv("RESEND_TO")
        or os.getenv("EMAIL_TO")
        or config._env("RESEND_TO", "")
        or config._env("EMAIL_TO", "")
        or ""
    ).strip()
    if not (api_key and mail_from and mail_to):
        return None
    return EmailConfig(api_key=api_key, mail_from=mail_from, mail_to=mail_to)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Some gateways can block generic urllib signatures.
            # Provide an explicit agent string for better compatibility.
            "User-Agent": "TRADER-Bot/1.0 (+https://palaverafrica.org)",
            **headers,
        },
    )
    try:
        with request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return int(getattr(e, "code", 0) or 0), body
    except Exception as e:
        return 0, str(e)


def send_email(*, subject: str, html: str, text: str) -> bool:
    cfg = _get_email_config()
    if cfg is None:
        # Email not configured (env vars missing) -> skip with explicit log.
        print("[EMAIL] Skipped: missing RESEND_API_KEY/RESEND_FROM/RESEND_TO configuration.")
        return False

    payload = {
        "from": cfg.mail_from,
        "to": [cfg.mail_to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    status, body = _post_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        payload=payload,
    )

    ok = 200 <= status < 300
    if not ok:
        print(f"[EMAIL] Resend failed status={status} body={body}")
    return ok


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


def _account_snapshot() -> dict[str, Any]:
    acc = m5.get_account()
    if not acc:
        acc = {}
    hist = m5.get_trade_history()
    prop = rm.get_prop_status()

    trades = int(hist.get("trades") or 0)
    wins = int(hist.get("wins") or 0)
    win_rate = _safe_div(wins, trades) * 100.0

    return {
        "balance": float(acc.get("balance", 0.0)),
        "equity": float(acc.get("equity", 0.0)),
        "margin": float(acc.get("margin", 0.0)),
        "free_margin": float(acc.get("free_margin", 0.0)),
        "currency": acc.get("currency", ""),
        "trades_30d": trades,
        "wins_30d": wins,
        "losses_30d": int(hist.get("losses") or 0),
        "win_rate_30d": win_rate,
        "prop_profit_pct": float(prop.get("profit_pct") or 0.0),
        "prop_overall_dd_pct": float(prop.get("overall_dd_pct") or 0.0),
        "prop_daily_dd_pct": float(prop.get("daily_dd_pct") or 0.0),
        "phase": int(prop.get("phase") or 1),
    }


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def _fmt_money(x: float, ccy: str) -> str:
    return f"{x:,.2f} {ccy}".strip()


def _build_status_text() -> str:
    s = _account_snapshot()
    ccy = s["currency"]
    return (
        f"Time: {_now_utc_str()}\n"
        f"Balance: {_fmt_money(s['balance'], ccy)}\n"
        f"Equity: {_fmt_money(s['equity'], ccy)}\n"
        f"Margin: {_fmt_money(s['margin'], ccy)}\n"
        f"Free margin: {_fmt_money(s['free_margin'], ccy)}\n"
        f"Win rate (30d): {s['win_rate_30d']:.1f}% (Wins {s['wins_30d']}/{s['trades_30d']})\n"
        f"Prop phase: {s['phase']}\n"
        f"Prop profit: {_fmt_pct(s['prop_profit_pct'])}\n"
        f"Prop daily DD: {_fmt_pct(s['prop_daily_dd_pct'])}\n"
        f"Prop overall DD: {_fmt_pct(s['prop_overall_dd_pct'])}\n"
    )


def _build_status_kv() -> dict[str, str]:
    s = _account_snapshot()
    ccy = s["currency"]
    return {
        "Time (UTC)": _now_utc_str(),
        "Balance": _fmt_money(s["balance"], ccy),
        "Equity": _fmt_money(s["equity"], ccy),
        "Margin": _fmt_money(s["margin"], ccy),
        "Free margin": _fmt_money(s["free_margin"], ccy),
        "Win rate (30d)": f"{s['win_rate_30d']:.1f}% (Wins {s['wins_30d']}/{s['trades_30d']})",
        "Prop phase": str(s["phase"]),
        "Prop profit": _fmt_pct(s["prop_profit_pct"]),
        "Prop daily DD": _fmt_pct(s["prop_daily_dd_pct"]),
        "Prop overall DD": _fmt_pct(s["prop_overall_dd_pct"]),
    }


def _badge(text: str, *, tone: str) -> str:
    colors = {
        "green": ("#065f46", "#d1fae5"),
        "red": ("#991b1b", "#fee2e2"),
        "amber": ("#92400e", "#fef3c7"),
        "blue": ("#1e40af", "#dbeafe"),
        "gray": ("#374151", "#f3f4f6"),
    }
    fg, bg = colors.get(tone, colors["gray"])
    return (
        f"<span style=\"display:inline-block;padding:4px 10px;border-radius:999px;"
        f"font-size:12px;font-weight:600;color:{fg};background:{bg};\">{html_escape(text)}</span>"
    )


def _kv_table(rows: list[tuple[str, str]]) -> str:
    trows = []
    for k, v in rows:
        trows.append(
            "<tr>"
            f"<td style=\"padding:10px 12px;border-bottom:1px solid #eef2f7;color:#6b7280;width:170px;\">{html_escape(k)}</td>"
            f"<td style=\"padding:10px 12px;border-bottom:1px solid #eef2f7;color:#111827;font-weight:600;\">{html_escape(v)}</td>"
            "</tr>"
        )
    return (
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" "
        "style=\"border:1px solid #eef2f7;border-radius:12px;overflow:hidden;border-collapse:separate;\">"
        + "".join(trows)
        + "</table>"
    )


def _card(title: str, body_html: str) -> str:
    return (
        "<div style=\"background:#ffffff;border:1px solid #eef2f7;border-radius:14px;"
        "padding:16px 16px;margin-top:14px;\">"
        f"<div style=\"font-size:13px;font-weight:800;letter-spacing:.04em;color:#6b7280;text-transform:uppercase;\">{html_escape(title)}</div>"
        f"<div style=\"margin-top:10px;\">{body_html}</div>"
        "</div>"
    )


def _render_email(*, title: str, subtitle: str, badges: list[str], cards: list[str]) -> str:
    badges_html = " ".join(badges)
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\" /></head>",
        "<body style=\"margin:0;background:#f6f7fb;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;\">",
        "<div style=\"max-width:720px;margin:0 auto;padding:24px;\">",
        "<div style=\"background:#111827;border-radius:18px;padding:18px 18px;color:#fff;\">",
        f"<div style=\"font-size:18px;font-weight:800;\">{html_escape(title)}</div>",
        f"<div style=\"margin-top:6px;font-size:13px;opacity:.9;\">{html_escape(subtitle)}</div>",
        f"<div style=\"margin-top:12px;\">{badges_html}</div>",
        "</div>",
        "".join(cards),
        "<div style=\"margin-top:18px;color:#6b7280;font-size:12px;\">",
        "Sent by TRADER bot via Resend. Times shown in UTC.",
        "</div>",
        "</div></body></html>",
    ]
    return "".join(parts)


def notify_trade_taken(*, symbol: str, direction: str, entry: float, sl: float, tp: float,
                       lots: float, confidence: int, reasoning: str,
                       ticket: int | None = None) -> None:
    subj = f"PENDING ORDER PLACED: {direction} LIMIT {symbol}"
    text = (
        f"{subj}\n"
        f"{'-'*40}\n"
        f"Ticket: {ticket if ticket is not None else 'N/A'}\n"
        f"Limit Price (Zone): {entry}\n"
        f"Stop Loss: {sl}\n"
        f"Take Profit: {tp}\n"
        f"Lots: {lots}\n"
        f"Confidence: {confidence}%\n"
        f"AI Reason: {reasoning}\n"
        f"{'-'*40}\n"
        f"{_build_status_text()}"
    )
    html = _render_email(
        title=f"Pending limit order: {direction} {symbol}",
        subtitle=f"Ticket {ticket if ticket is not None else 'N/A'} • Confidence {confidence}%",
        badges=[
            _badge("PENDING PLACED", tone="blue"),
            _badge(direction, tone="blue"),
            _badge(symbol, tone="gray"),
        ],
        cards=[
            _card(
                "Trade parameters",
                _kv_table(
                    [
                        ("Entry", str(entry)),
                        ("Stop Loss", str(sl)),
                        ("Take Profit", str(tp)),
                        ("Lots", str(lots)),
                        ("Confidence", f"{confidence}%"),
                    ]
                ),
            ),
            _card(
                "AI reason",
                "<div style=\"white-space:pre-wrap;line-height:1.45;color:#111827;\">"
                + html_escape(reasoning or "").replace("\n", "<br/>")
                + "</div>",
            ),
            _card("Account status", _kv_table(list(_build_status_kv().items()))),
        ],
    )
    send_email(subject=subj, html=html, text=text)


def notify_trade_failed(*, symbol: str, direction: str | None,
                        entry: float | None, sl: float | None, tp: float | None,
                        confidence: int | None, reasoning: str | None,
                        error: str) -> None:
    subj = f"TRADE FAILED: {symbol}"
    text = (
        f"{subj}\n"
        f"{'-'*40}\n"
        f"Direction: {direction}\n"
        f"Entry/SL/TP: {entry} / {sl} / {tp}\n"
        f"Confidence: {confidence}\n"
        f"AI Reason: {reasoning}\n"
        f"Error: {error}\n"
        f"{'-'*40}\n"
        f"{_build_status_text()}"
    )
    html = _render_email(
        title=f"Trade failed: {symbol}",
        subtitle="Order was rejected or not executed",
        badges=[
            _badge("TRADE FAILED", tone="red"),
            _badge(direction or "N/A", tone="blue" if direction else "gray"),
            _badge(symbol, tone="gray"),
        ],
        cards=[
            _card(
                "Attempted trade",
                _kv_table(
                    [
                        ("Direction", str(direction)),
                        ("Entry", str(entry)),
                        ("Stop Loss", str(sl)),
                        ("Take Profit", str(tp)),
                        ("Confidence", str(confidence)),
                    ]
                ),
            ),
            _card(
                "AI reason",
                "<div style=\"white-space:pre-wrap;line-height:1.45;color:#111827;\">"
                + html_escape(reasoning or "").replace("\n", "<br/>")
                + "</div>",
            ),
            _card(
                "Error details",
                "<div style=\"font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
                "font-size:12px;white-space:pre-wrap;color:#111827;background:#f9fafb;"
                "border:1px solid #eef2f7;border-radius:12px;padding:12px;\">"
                + html_escape(error)
                + "</div>",
            ),
            _card("Account status", _kv_table(list(_build_status_kv().items()))),
        ],
    )
    send_email(subject=subj, html=html, text=text)


def notify_sl_trailed(*, ticket: int, symbol: str, direction: str, old_sl: float, new_sl: float,
                      locked_r: int | None = None) -> None:
    state = _load_state()
    key = f"trail_sl_notified:{ticket}"
    last = state.get(key)
    if last is not None:
        try:
            if float(last) == float(new_sl):
                print(f"[EMAIL] Trailing SL dedup skip for {symbol} #{ticket} (SL unchanged at {new_sl}).")
                return
        except Exception:
            pass

    lr = f"{locked_r}R" if locked_r is not None else "N/A"
    subj = f"SL TRAILED: {direction} {symbol} (#{ticket})"
    text = (
        f"{subj}\n"
        f"{'-'*40}\n"
        f"Ticket: {ticket}\n"
        f"Old SL: {old_sl}\n"
        f"New SL: {new_sl}\n"
        f"Locked: +{lr}\n"
        f"{'-'*40}\n"
        f"{_build_status_text()}"
    )
    html = _render_email(
        title=f"Stop loss trailed: {direction} {symbol}",
        subtitle=f"Ticket {ticket} • Locked +{lr}",
        badges=[
            _badge("SL TRAILED", tone="amber"),
            _badge(direction, tone="blue"),
            _badge(symbol, tone="gray"),
        ],
        cards=[
            _card(
                "Stop loss update",
                _kv_table(
                    [
                        ("Ticket", str(ticket)),
                        ("Old SL", str(old_sl)),
                        ("New SL", str(new_sl)),
                        ("Locked", f"+{lr}"),
                    ]
                ),
            ),
            _card("Account status", _kv_table(list(_build_status_kv().items()))),
        ],
    )

    ok = send_email(subject=subj, html=html, text=text)
    if ok:
        state[key] = new_sl
        _save_state(state)
        print(f"[EMAIL] Trailing SL email sent for {symbol} #{ticket} ({old_sl} -> {new_sl}).")
    else:
        print(f"[EMAIL] Trailing SL email not sent for {symbol} #{ticket}.")


def notify_trade_exit_tp_sl(
    *,
    exit_kind: str,
    symbol: str,
    direction: str,
    ticket: int,
    entry: float,
    stop_loss: float,
    take_profit: float,
    lots: float,
    exit_price: float,
    profit: float,
    swap: float,
    commission: float,
    net_pnl: float,
) -> None:
    """exit_kind: 'TP' | 'SL' | 'STOP OUT' — broker exit reasons we notify for."""
    if exit_kind == "TP":
        subj_lead = "TP HIT"
    elif exit_kind == "SL":
        subj_lead = "SL HIT"
    else:
        subj_lead = exit_kind
    subj = f"{subj_lead}: {direction} {symbol} (#{ticket})"
    s = _account_snapshot()
    ccy = s["currency"]

    text = (
        f"{subj}\n"
        f"{'-' * 40}\n"
        f"Ticket (position): {ticket}\n"
        f"Direction: {direction}\n"
        f"Entry: {entry}\n"
        f"Exit: {exit_price}\n"
        f"Stop Loss (was): {stop_loss}\n"
        f"Take Profit (was): {take_profit}\n"
        f"Lots: {lots}\n"
        f"Trade profit: {profit:+.2f} {ccy}\n"
        f"Swap: {swap:+.2f} {ccy}\n"
        f"Commission: {commission:+.2f} {ccy}\n"
        f"Net P/L: {net_pnl:+.2f} {ccy}\n"
        f"{'-' * 40}\n"
        f"{_build_status_text()}"
    )
    if exit_kind == "TP":
        tone = "green"
    elif exit_kind == "STOP OUT":
        tone = "amber"
    else:
        tone = "red"
    title_exit = {"TP": "Take profit", "SL": "Stop loss", "STOP OUT": "Stop-out"}.get(
        exit_kind, exit_kind
    )
    html = _render_email(
        title=f"{title_exit} — {direction} {symbol}",
        subtitle=f"Position #{ticket} closed at {exit_price}",
        badges=[
            _badge(exit_kind, tone=tone),
            _badge(direction, tone="blue"),
            _badge(symbol, tone="gray"),
        ],
        cards=[
            _card(
                "Exit summary",
                _kv_table(
                    [
                        ("Position", str(ticket)),
                        ("Direction", direction),
                        ("Entry", str(entry)),
                        ("Exit price", str(exit_price)),
                        ("Stop loss", str(stop_loss)),
                        ("Take profit", str(take_profit)),
                        ("Lots", str(lots)),
                        ("Trade P/L", f"{profit:+.2f} {ccy}"),
                        ("Swap", f"{swap:+.2f} {ccy}"),
                        ("Commission", f"{commission:+.2f} {ccy}"),
                        ("Net P/L", f"{net_pnl:+.2f} {ccy}"),
                    ]
                ),
            ),
            _card("Account status", _kv_table(list(_build_status_kv().items()))),
        ],
    )
    send_email(subject=subj, html=html, text=text)


def notify_daily_status(*, kind: str) -> None:
    # kind: "START" | "END"
    state = _load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    key = f"daily:{kind}:{today}"
    if state.get(key):
        return

    subj = f"DAILY STATUS ({kind}): {today}"
    text = f"{subj}\n{'-'*40}\n{_build_status_text()}"
    html = _render_email(
        title=f"Daily account status ({kind})",
        subtitle=today,
        badges=[_badge(f"DAILY {kind}", tone="blue")],
        cards=[_card("Account status", _kv_table(list(_build_status_kv().items())))],
    )
    ok = send_email(subject=subj, html=html, text=text)
    if ok:
        state[key] = True
        _save_state(state)


def notify_news_alarm(*, symbol: str, reason: str, orders_cancelled: int) -> None:
    # Debounce news emails to avoid spamming every 20 seconds.
    # We will track "symbol+reason" and only email once per specific news event.
    state = _load_state()
    today_key = f"news_alarm:{symbol}:{reason.replace(' ', '_')}"
    if state.get(today_key):
        return

    subj = f"NEWS ALARM: {symbol} Trade Suspended"
    text = (
        f"{subj}\n"
        f"{'-'*40}\n"
        f"Symbol: {symbol}\n"
        f"Reason: {reason}\n"
        f"Action: Suspended AI scanning for {symbol}.\n"
        f"Orders cancelled: {orders_cancelled}\n"
        f"{'-'*40}\n"
        f"{_build_status_text()}"
    )
    html = _render_email(
        title=f"News Alarm triggering for {symbol}",
        subtitle=f"{orders_cancelled} pending orders cancelled to protect capital",
        badges=[
            _badge("NEWS ALARM", tone="red"),
            _badge(symbol, tone="gray"),
        ],
        cards=[
            _card(
                "Trigger Details",
                _kv_table(
                    [
                        ("Symbol", symbol),
                        ("Reason", reason),
                        ("Orders Cancelled", str(orders_cancelled)),
                    ]
                ),
            ),
            _card("Account status", _kv_table(list(_build_status_kv().items()))),
        ],
    )
    ok = send_email(subject=subj, html=html, text=text)
    if ok:
        state[today_key] = True
        _save_state(state)
