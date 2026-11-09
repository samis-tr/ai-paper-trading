"""Outbound-only Telegram notifications. Without credentials, messages go to a local outbox (dry run)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import DATA_DIR
from .ledger import Ledger

OUTBOX = DATA_DIR / "outbox.log"


def send(text: str, outbox: Path = OUTBOX) -> str:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        outbox.parent.mkdir(parents=True, exist_ok=True)
        with open(outbox, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n{text}\n")
        return "dry-run"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text}, timeout=15)
        r.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        return f"failed: {type(e).__name__}"  # never log the URL (contains the token)


STATE_AR = {"ACTIVE": "نشط", "NEW_TRADES_DISABLED": "الصفقات الجديدة معطّلة", "HALTED": "متوقف"}


def daily_summary(ledger: Ledger) -> str:
    snap = ledger.last_snapshot()
    c = ledger.total_costs()
    spent = sum(x["amount_eur"] for x in ledger.actual_costs())
    lines = ["📊 ملخص التداول الورقي اليومي (VWCE) — ليست أموالاً حقيقية"]
    if not snap:
        lines.append("لا توجد بيانات بعد.")
    else:
        e0 = ledger.initial_equity
        lines += [
            f"الجلسة: {snap['session']}",
            f"الحالة: {STATE_AR.get(snap['state'], snap['state'])}",
            f"حقوق الملكية الافتراضية: €{snap['equity']:.2f} ({snap['equity'] / e0 - 1:+.2%} منذ البداية)",
            f"النقد الافتراضي: €{snap['cash']:.2f} | الحصص: {snap['qty']:g} @ €{snap['close']:.2f}",
            f"تغير اليوم: €{snap['daily_pnl']:+.2f} ({snap['daily_ret']:+.2%})",
            f"التراجع من القمة: {snap['drawdown']:.2%} (حد الإيقاف 3%)",
            f"التكاليف المحاكاة: عمولات €{c['commission']:.2f} + فارق/انزلاق €{c['spread_slippage']:.2f}",
            f"الإنفاق الفعلي على البنية: €{spent:.2f}",
            f"مصدر البيانات: {snap['data_source']}",
        ]
    errs = [e for e in ledger.events(20) if e["level"] in ("WARN", "CRITICAL", "ERROR")][:3]
    for e in errs:
        lines.append(f"⚠️ {e['ts'][:16]} {e['kind']}: {e['message'][:120]}")
    lines.append("تنبيه: لا يوجد أي ضمان للربح.")
    return "\n".join(lines)
