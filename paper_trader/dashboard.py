"""Arabic, mobile-first, read-only dashboard + owner emergency stop.
Run: python -m uvicorn paper_trader.dashboard:app --host 127.0.0.1 --port 8765
(On the VPS it binds to the Tailscale interface only.)"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .calendar_xetra import TradingCalendar
from .config import DEFAULT_DB, Settings
from .jobs import emergency_halt
from .ledger import Ledger
from .notifier import STATE_AR

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _ro() -> Ledger:
    return Ledger(DEFAULT_DB, read_only=True)


def status_data(led: Ledger, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = Settings()
    snap = led.last_snapshot()
    e0 = led.initial_equity
    costs = led.total_costs()
    try:
        expected = TradingCalendar().expected_latest_session(now, s.data.close_buffer_hours).date().isoformat()
    except Exception as e:  # calendar failure is shown, not hidden
        expected = f"خطأ في التقويم: {e}"
    return {
        "state": led.state(), "control": led.control(), "initial": e0,
        "cash": led.cash(), "qty": led.position(), "snapshot": snap,
        "equity": snap["equity"] if snap else e0,
        "net_pnl": (snap["equity"] if snap else e0) - e0,
        "costs": costs, "fills": led.fills()[-20:][::-1],
        "events": led.events(15), "actual_costs": led.actual_costs(),
        "expected_session": expected,
        "fresh": bool(snap and snap["session"] == expected),
        "chain_ok": led.verify_chain(),
    }


@app.get("/api/status")
def api_status():
    return JSONResponse(status_data(_ro()))


@app.post("/halt")
def halt(token: str = Form(...)):
    owner = os.environ.get("DASHBOARD_OWNER_TOKEN", "")
    if not owner:
        raise HTTPException(503, "Emergency stop via web disabled: DASHBOARD_OWNER_TOKEN not set. Use CLI `halt`.")
    if not hmac.compare_digest(token.encode(), owner.encode()):
        raise HTTPException(403, "Invalid owner token")
    emergency_halt(Ledger(DEFAULT_DB), "owner-dashboard")
    return RedirectResponse("/", status_code=303)


def _card(label: str, value: str, cls: str = "", numeric: bool = True) -> str:
    v = f'<span dir="ltr">{value}</span>' if numeric else value
    return f'<div class="card {cls}"><div class="lbl">{escape(label)}</div><div class="val">{v}</div></div>'


@app.get("/", response_class=HTMLResponse)
def index():
    d = status_data(_ro())
    snap = d["snapshot"] or {}
    st = d["state"]
    st_cls = {"ACTIVE": "ok", "NEW_TRADES_DISABLED": "warn", "HALTED": "bad"}.get(st, "")
    dd = snap.get("drawdown", 0.0)
    spent = sum(c["amount_eur"] for c in d["actual_costs"])
    fills = "".join(
        f"<tr><td>{escape(f['session'])}</td><td>{'شراء' if f['side']=='BUY' else 'بيع'}</td>"
        f"<td>{f['qty']:g}</td><td>€{f['price']:.2f}</td><td>€{f['commission']:.2f}</td></tr>"
        for f in d["fills"]) or '<tr><td colspan="5">لا صفقات بعد</td></tr>'
    events = "".join(
        f"<li class='{escape(e['level'].lower())}'><b>{escape(e['ts'][:16])}</b> {escape(e['kind'])}: "
        f"{escape(e['message'][:160])}</li>" for e in d["events"])
    pos = (f"{d['qty']:g} حصة VWCE @ €{snap.get('close', 0):.2f} = €{d['qty'] * snap.get('close', 0):.2f}"
           if d["qty"] else "لا توجد مراكز مفتوحة")
    html = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>لوحة التداول الورقي</title><style>
:root{{--bg:#f6f7f9;--fg:#15181d;--card:#fff;--mut:#5d6573;--ok:#1f7a4d;--warn:#a86400;--bad:#b3261e;--bd:#e2e5ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--fg:#e8eaee;--card:#181b21;--mut:#9aa3b2;--ok:#4cc38a;--warn:#f0a63a;--bad:#ff6b5f;--bd:#2a2f38}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Segoe UI",Tahoma,sans-serif;padding:16px;max-width:900px;margin-inline:auto}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px}}
.lbl{{color:var(--mut);font-size:12px}}.val{{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums}}
.ok .val{{color:var(--ok)}}.warn .val{{color:var(--warn)}}.bad .val{{color:var(--bad)}}
section{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;margin-top:12px;overflow-x:auto}}
h2{{font-size:15px;margin:0 0 8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{padding:6px;border-bottom:1px solid var(--bd);text-align:right}}
ul{{margin:0;padding-inline-start:18px;font-size:13px}}li.critical,li.error{{color:var(--bad)}}li.warn{{color:var(--warn)}}
.stop{{border-color:var(--bad)}}.stop input{{width:100%;padding:10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--fg);margin:6px 0}}
.stop button{{width:100%;padding:14px;border:0;border-radius:10px;background:var(--bad);color:#fff;font-size:17px;font-weight:700}}
</style></head><body>
<h1>لوحة التداول الورقي — VWCE</h1>
<div class="sub">أموال افتراضية فقط · لا يوجد اتصال بوسيط حقيقي · لا يوجد ضمان للربح</div>
{'<div class="card bad" style="margin-bottom:10px"><div class="val">⚠️ بيانات اصطناعية للاختبار فقط — لا تعكس أي أداء حقيقي</div></div>' if 'SYNTHETIC' in str(snap.get('data_source', '')) else ''}
<div class="grid">
{_card("حالة النظام", STATE_AR.get(st, st), st_cls, numeric=False)}
{_card("حقوق الملكية الافتراضية", f"€{d['equity']:.2f}")}
{_card("النقد الافتراضي", f"€{d['cash']:.2f}")}
{_card("صافي الربح/الخسارة بعد التكاليف", f"€{d['net_pnl']:+.2f}", "ok" if d['net_pnl'] >= 0 else "bad")}
{_card("التراجع من القمة (حد 3%)", f"{dd:.2%}", "bad" if dd >= 0.03 else "warn" if dd >= 0.02 else "ok")}
{_card("تغير آخر جلسة (حد 1%)", f"{snap.get('daily_ret', 0):+.2%}")}
{_card("حداثة البيانات", ("محدّثة ✓ " if d['fresh'] else "متأخرة ✗ ") + escape(str(snap.get('session', '—'))), "ok" if d['fresh'] else "bad", numeric=False)}
{_card("التكاليف المحاكاة", f"€{d['costs']['commission'] + d['costs']['spread_slippage']:.2f}")}
{_card("الإنفاق الفعلي على البنية/API", f"€{spent:.2f}")}
{_card("سلامة سجل الأحداث", "سليم ✓" if d['chain_ok'] else "تلاعب محتمل ✗", "ok" if d['chain_ok'] else "bad", numeric=False)}
</div>
<section><h2>المراكز المفتوحة</h2>{escape(pos)}<div class="sub">الجلسة المتوقعة: {escape(d['expected_session'])} · المصدر: {escape(str(snap.get('data_source', '—')))}</div></section>
<section><h2>الصفقات المنفذة (ورقية)</h2><table><tr><th>الجلسة</th><th>النوع</th><th>الكمية</th><th>السعر</th><th>العمولة</th></tr>{fills}</table></section>
<section><h2>حالة الوكيل والأخطاء</h2><ul>{events}</ul></section>
<section class="stop"><h2>⛔ إيقاف الطوارئ</h2>
<form method="post" action="/halt" onsubmit="return confirm('تأكيد إيقاف كل التداول الورقي؟')">
<input type="password" name="token" placeholder="رمز المالك" autocomplete="current-password" required>
<button type="submit">إيقاف فوري</button></form>
<div class="sub">الاستئناف ممكن فقط من سطر الأوامر بعبارة الموافقة.</div></section>
</body></html>"""
    return HTMLResponse(html)
