"""Dedicated paper ledger (SQLite). Fills, snapshots, events and costs are append-only (enforced by triggers).
Events form a SHA-256 hash chain so tampering is detectable."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ACTIVE, DISABLED, HALTED = "ACTIVE", "NEW_TRADES_DISABLED", "HALTED"
RESUME_PHRASE = "I APPROVE RESUMING PAPER TRADING"

_APPEND_ONLY = ["fills", "snapshots", "events", "actual_costs"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control(
  id INTEGER PRIMARY KEY CHECK(id=1),
  state TEXT NOT NULL CHECK(state IN ('{ACTIVE}','{DISABLED}','{HALTED}')),
  reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE, isin TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty REAL NOT NULL CHECK(qty>0),
  decision_session TEXT NOT NULL, exec_session TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','FILLED','CANCELLED','REJECTED')),
  reason TEXT, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS orders_immutable BEFORE UPDATE ON orders
  WHEN OLD.status != 'PENDING' OR NEW.idem_key IS NOT OLD.idem_key OR NEW.side IS NOT OLD.side
    OR NEW.qty IS NOT OLD.qty OR NEW.exec_session IS NOT OLD.exec_session OR NEW.isin IS NOT OLD.isin
  BEGIN SELECT RAISE(ABORT, 'orders are immutable once final'); END;
CREATE TRIGGER IF NOT EXISTS orders_nodelete BEFORE DELETE ON orders
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  session TEXT NOT NULL, isin TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL CHECK(qty>0),
  ref_open REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL,
  spread_slippage_cost REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(
  session TEXT PRIMARY KEY, close REAL NOT NULL, qty REAL NOT NULL, cash REAL NOT NULL,
  equity REAL NOT NULL, peak REAL NOT NULL, drawdown REAL NOT NULL, daily_pnl REAL NOT NULL,
  daily_ret REAL NOT NULL, state TEXT NOT NULL, data_source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL,
  message TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actual_costs(
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, vendor TEXT NOT NULL,
  amount_eur REAL NOT NULL CHECK(amount_eur>=0), note TEXT, created_at TEXT NOT NULL);
""" + "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_noupdate BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_nodelete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, 'append-only'); END;"""
    for t in _APPEND_ONLY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Order:
    id: int
    idem_key: str
    isin: str
    side: str
    qty: float
    decision_session: str
    exec_session: str
    status: str


class Ledger:
    def __init__(self, path: str | Path = ":memory:", read_only: bool = False):
        self.path = str(path)
        if read_only:
            self.db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    # ---------- setup / meta ----------
    def init(self, initial_equity: float, isin: str) -> None:
        if self.meta("initial_equity") is not None:
            return
        with self.tx():
            self.db.execute("INSERT INTO meta VALUES('initial_equity', ?)", (repr(float(initial_equity)),))
            self.db.execute("INSERT INTO meta VALUES('isin', ?)", (isin,))
            self.db.execute("INSERT INTO meta VALUES('mode', 'PAPER_ONLY')")
            self.db.execute("INSERT INTO control VALUES(1, ?, 'initialised', ?)", (ACTIVE, _now()))
        self.event("INFO", "INIT", f"Paper ledger initialised with EUR {initial_equity:.2f}")

    def meta(self, key: str):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    @property
    def initial_equity(self) -> float:
        return float(self.meta("initial_equity"))

    def tx(self):
        return _Tx(self.db)

    # ---------- events (hash chain) ----------
    def event(self, level: str, kind: str, message: str) -> None:
        with self.tx():
            prev = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else "GENESIS"
            ts = _now()
            h = hashlib.sha256(f"{prev_hash}|{ts}|{level}|{kind}|{message}".encode()).hexdigest()
            self.db.execute("INSERT INTO events(ts,level,kind,message,prev_hash,hash) VALUES(?,?,?,?,?,?)",
                            (ts, level, kind, message, prev_hash, h))

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for r in self.db.execute("SELECT * FROM events ORDER BY id"):
            h = hashlib.sha256(f"{prev}|{r['ts']}|{r['level']}|{r['kind']}|{r['message']}".encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != h:
                return False
            prev = h
        return True

    def events(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # ---------- control state ----------
    def state(self) -> str:
        r = self.db.execute("SELECT state FROM control WHERE id=1").fetchone()
        if r is None:
            raise RuntimeError("Ledger not initialised")
        return r[0]

    def control(self) -> dict:
        return dict(self.db.execute("SELECT * FROM control WHERE id=1").fetchone())

    def set_state(self, state: str, reason: str, level: str = "WARN") -> None:
        cur = self.state()
        if cur == state:
            return
        if cur == HALTED and state != HALTED:
            raise PermissionError("HALTED can only be cleared with resume() and the approval phrase")
        if cur == DISABLED and state == ACTIVE:
            raise PermissionError("Re-enabling trading requires resume() and the approval phrase")
        self.db.execute("UPDATE control SET state=?, reason=?, updated_at=? WHERE id=1", (state, reason, _now()))
        self.event(level, f"STATE_{state}", reason)

    def resume(self, phrase: str, who: str) -> None:
        if phrase != RESUME_PHRASE:
            self.event("WARN", "RESUME_DENIED", f"Wrong approval phrase from {who}")
            raise PermissionError("Approval phrase mismatch")
        self.db.execute("UPDATE control SET state=?, reason=?, updated_at=? WHERE id=1",
                        (ACTIVE, f"resumed by {who}", _now()))
        self.event("WARN", "STATE_ACTIVE", f"Owner {who} resumed paper trading")

    # ---------- orders / fills ----------
    def place_order(self, idem_key: str, isin: str, side: str, qty: float,
                    decision_session: str, exec_session: str, reason: str) -> int | None:
        try:
            cur = self.db.execute(
                "INSERT INTO orders(idem_key,isin,side,qty,decision_session,exec_session,status,reason,created_at)"
                " VALUES(?,?,?,?,?,?,'PENDING',?,?)",
                (idem_key, isin, side, qty, decision_session, exec_session, reason, _now()))
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) and "idem_key" in str(e):
                self.event("WARN", "DUPLICATE_ORDER", f"Rejected duplicate order {idem_key}")
                return None
            raise
        self.event("INFO", "ORDER", f"{side} {qty:g} {isin} for {exec_session} ({reason})")
        return cur.lastrowid

    def pending_orders(self) -> list[Order]:
        return [Order(r["id"], r["idem_key"], r["isin"], r["side"], r["qty"], r["decision_session"],
                      r["exec_session"], r["status"])
                for r in self.db.execute("SELECT * FROM orders WHERE status='PENDING' ORDER BY id")]

    def close_order(self, order_id: int, status: str, reason: str) -> None:
        self.db.execute("UPDATE orders SET status=?, reason=? WHERE id=?", (status, reason, order_id))
        self.event("WARN" if status != "FILLED" else "INFO", f"ORDER_{status}", f"order {order_id}: {reason}")

    def record_fill(self, order: Order, session: str, qty: float, ref_open: float, price: float,
                    commission: float, note: str) -> None:
        with self.tx():
            self.db.execute(
                "INSERT INTO fills(order_id,session,isin,side,qty,ref_open,price,commission,"
                "spread_slippage_cost,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order.id, session, order.isin, order.side, qty, ref_open, price, commission,
                 abs(price - ref_open) * qty, _now()))
            self.db.execute("UPDATE orders SET status='FILLED', reason=? WHERE id=?", (note, order.id))
        self.event("INFO", "FILL", f"{order.side} {qty:g} @ {price:.4f} (open {ref_open:.4f}) fee {commission:.2f}")

    def fills(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM fills ORDER BY id")]

    def position(self) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(CASE side WHEN 'BUY' THEN qty ELSE -qty END),0) FROM fills").fetchone()
        return float(r[0])

    def cash(self) -> float:
        r = self.db.execute(
            "SELECT COALESCE(SUM(CASE side WHEN 'BUY' THEN -(qty*price) - commission"
            " ELSE qty*price - commission END),0) FROM fills").fetchone()
        return self.initial_equity + float(r[0])

    def total_costs(self) -> dict:
        r = self.db.execute("SELECT COALESCE(SUM(commission),0), COALESCE(SUM(spread_slippage_cost),0),"
                            " COUNT(*) FROM fills").fetchone()
        return {"commission": r[0], "spread_slippage": r[1], "fills": r[2]}

    # ---------- snapshots ----------
    def add_snapshot(self, **s) -> None:
        cols = ",".join(s.keys()) + ",created_at"
        self.db.execute(f"INSERT INTO snapshots({cols}) VALUES({','.join('?' * (len(s) + 1))})",
                        (*s.values(), _now()))

    def last_snapshot(self) -> dict | None:
        r = self.db.execute("SELECT * FROM snapshots ORDER BY session DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def snapshots(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM snapshots ORDER BY session")]

    # ---------- actual infra spending (entered from invoices by the owner) ----------
    def add_actual_cost(self, date: str, vendor: str, amount_eur: float, note: str = "") -> None:
        self.db.execute("INSERT INTO actual_costs(date,vendor,amount_eur,note,created_at) VALUES(?,?,?,?,?)",
                        (date, vendor, amount_eur, note, _now()))
        self.event("INFO", "ACTUAL_COST", f"{vendor} EUR {amount_eur:.2f} {date}")

    def actual_costs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM actual_costs ORDER BY date")]


class _Tx:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.own = False

    def __enter__(self):
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
            self.own = True
        return self.db

    def __exit__(self, exc_type, *_):
        if self.own:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
