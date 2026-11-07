"""Shared database-backed services used by the API and the dashboard (no hardcoded numbers)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time
from typing import Optional

import pandas as pd

from . import db
from .config import REFERENCE_DATE


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def latest_run(conn, run_type: str) -> Optional[dict]:
    return one(conn, "SELECT * FROM run_log WHERE run_type=? ORDER BY started_at DESC, run_id DESC LIMIT 1", (run_type,))


def recompute_case(conn, case_id: str, actor: str = "SYSTEM", reason: str = "Recomputed from source events", as_of: date = REFERENCE_DATE) -> Optional[str]:
    """Derive case_status / debt_confirmation_status from flags + promises; audit if changed. Call inside a transaction."""
    c = one(conn, "SELECT * FROM collection_case WHERE case_id=?", (case_id,))
    if c is None or c["case_status"] == "RESOLVED":
        return None
    ref_dt = datetime.combine(as_of, time(23, 59, 59)).isoformat(timespec="seconds")

    def active(ftype: str, key_col: str, key: str) -> bool:
        r = one(conn, f"SELECT event_type FROM flag_event WHERE flag_type=? AND {key_col}=? AND event_at<=? ORDER BY event_at DESC, flag_event_id DESC LIMIT 1", (ftype, key, ref_dt))
        return bool(r and r["event_type"] == "RAISED")

    dispute = active("DISPUTE", "loan_id", c["loan_id"])
    hardship = active("HARDSHIP", "customer_id", c["customer_id"])
    pending = one(conn, "SELECT 1 AS x FROM promise_to_pay WHERE case_id=? AND promise_status='PENDING' LIMIT 1", (case_id,))
    status = "DISPUTE_REVIEW" if dispute else "HARDSHIP_REVIEW" if hardship else "PROMISE_PENDING" if pending else "OPEN"
    cleared = one(conn, "SELECT 1 AS x FROM flag_event WHERE flag_type='DISPUTE' AND loan_id=? AND event_type='CLEARED' AND event_at<=? LIMIT 1", (c["loan_id"], ref_dt))
    debt = "DISPUTED" if dispute else "CONFIRMED" if cleared else "UNVERIFIED"
    if status != c["case_status"] or debt != c["debt_confirmation_status"]:
        conn.execute("UPDATE collection_case SET case_status=?, debt_confirmation_status=?, updated_at=? WHERE case_id=?", (status, debt, db.now_iso(), case_id))
        if status != c["case_status"]:
            db.write_audit(conn, "CASE", case_id, "STATUS_CHANGED", c["case_status"], status, actor, reason)
        if debt != c["debt_confirmation_status"]:
            db.write_audit(conn, "CASE", case_id, "DEBT_STATUS_CHANGED", c["debt_confirmation_status"], debt, actor, reason)
        return status
    return c["case_status"]


def raise_or_clear_flag(conn, customer_id: str, loan_id: Optional[str], flag_type: str, event_type: str, event_at: datetime,
                        source: str = "ANALYST", note: Optional[str] = None, actor: str = "SYSTEM") -> str:
    """Append a flag event (never edits history) and recompute affected open cases. Call inside a transaction."""
    from . import ids
    fid = ids.flag_event_id(db.next_counter(conn, "flag_event", "flag_event_id", "FLG-"))
    outcome = "DEBT_VALIDATED" if (flag_type == "DISPUTE" and event_type == "CLEARED") else None
    now = db.now_iso()
    conn.execute("INSERT INTO flag_event VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (fid, customer_id, loan_id if flag_type == "DISPUTE" else None, flag_type, event_type, event_at.isoformat(timespec="seconds"),
                  source, outcome, note, now, now))
    db.write_audit(conn, "FLAG", fid, f"{flag_type}_{event_type}", None, event_type, actor, note)
    q = "SELECT case_id FROM collection_case WHERE case_status<>'RESOLVED' AND " + ("loan_id=?" if flag_type == "DISPUTE" else "customer_id=?")
    for r in conn.execute(q, (loan_id if flag_type == "DISPUTE" else customer_id,)).fetchall():
        recompute_case(conn, r["case_id"], actor, f"{flag_type} {event_type}")
    return fid


# ---------------------------------------------------------------------------- metrics
def portfolio_metrics(conn, ref: date = REFERENCE_DATE) -> dict:
    iso = ref.isoformat()
    q = lambda sql, p=(): conn.execute(sql, p).fetchone()
    snap = q("SELECT COUNT(*) n, COALESCE(SUM(outstanding_balance),0) o, COALESCE(SUM(total_arrears),0) a, "
             "SUM(CASE WHEN total_arrears>0 THEN 1 ELSE 0 END) od, SUM(CASE WHEN loan_status<>'PAID_OFF' THEN 1 ELSE 0 END) active "
             "FROM delinquency_snapshot WHERE snapshot_date=?", (iso,))
    prom = {r["promise_status"]: r["n"] for r in conn.execute("SELECT promise_status, COUNT(*) n FROM promise_to_pay GROUP BY 1")}
    evaluated = sum(v for k, v in prom.items() if k != "PENDING")
    pay = {r["reconciliation_status"]: r["n"] for r in conn.execute("SELECT reconciliation_status, COUNT(*) n FROM payment GROUP BY 1")}
    npay = sum(pay.values())
    pri = latest_run(conn, "PRIORITIZATION")
    exc = latest_run(conn, "EXCEPTION_DETECTION")
    cases = {r["case_status"]: r["n"] for r in conn.execute("SELECT case_status, COUNT(*) n FROM collection_case GROUP BY 1")}
    return {
        "as_of_date": iso, "total_loans": conn.execute("SELECT COUNT(*) FROM loan").fetchone()[0], "total_installments": conn.execute("SELECT COUNT(*) FROM installment").fetchone()[0],
        "active_loans": snap["active"] or 0, "total_outstanding_balance": round(snap["o"], 2), "total_arrears": round(snap["a"], 2), "overdue_accounts": snap["od"] or 0,
        "open_cases": sum(v for k, v in cases.items() if k != "RESOLVED"), "cases_by_status": cases,
        "promises_by_status": prom, "promise_fulfillment_rate": round(prom.get("FULFILLED", 0) / evaluated, 4) if evaluated else None,
        "promise_fulfilment_or_partial_rate": round((prom.get("FULFILLED", 0) + prom.get("PARTIALLY_FULFILLED", 0)) / evaluated, 4) if evaluated else None,
        "payments_total": npay, "payments_by_reconciliation_status": pay,
        "payment_reconciliation_rate": round(pay.get("RECONCILED", 0) / npay, 4) if npay else None,
        "latest_prioritization_run": pri["run_id"] if pri else None, "latest_exception_run": exc["run_id"] if exc else None,
        "open_compliance_exceptions": (conn.execute("SELECT COUNT(*) FROM exception_record WHERE run_id=? AND status<>'RESOLVED'", (exc["run_id"],)).fetchone()[0] if exc else 0),
    }


def cancel_scheduled_attempts(conn, case_id: str, actor: str = "SYSTEM", reason: str = "Case resolved") -> int:
    """Cancel not-yet-completed contact tasks of a case (audited). Call inside a transaction."""
    pending = rows(conn, "SELECT attempt_id FROM contact_attempt WHERE case_id=? AND attempt_status='SCHEDULED'", (case_id,))
    for r in pending:
        conn.execute("UPDATE contact_attempt SET attempt_status='CANCELLED', updated_at=? WHERE attempt_id=?", (db.now_iso(), r["attempt_id"]))
        db.write_audit(conn, "CONTACT_ATTEMPT", r["attempt_id"], "CANCELLED", "SCHEDULED", "CANCELLED", actor, reason)
    return len(pending)
