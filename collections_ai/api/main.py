"""FastAPI backend. Run: uvicorn collections_ai.api.main:app --reload  (Swagger UI at /docs).

Every endpoint reads/writes the SQLite database (COLLECTIONS_DB env var, default data/collections.db).
Business operations use the SAME engines as the batch pipeline (delinquency, contact eligibility,
prioritization, reconciliation, detection). Business date defaults to the fixed reference date
(2026-10-22) so results are reproducible; pass as_of where supported to override.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import __version__, db, ids, pipeline, services
from ..config import REFERENCE_DATE, default_db_path, ROOT
from ..contact import ContactContext, ContactRequest, effective_window
from ..delinquency import Ledger, fifo_allocate, from_minor, to_minor, bucket_for
from ..detection import evaluate_against_ground_truth
from ..prioritization import prioritize_cases
from ..reconciliation import evaluate_promise, reconcile_payments
from ..rules import load_rules
from ..spec import CASE_STATUSES, PROMISE_STATUSES
from . import schemas as S

app = FastAPI(title="Ethical Loan Collections Intelligence API", version=__version__,
              description="Prioritize overdue accounts, schedule permissible contact tasks and monitor commitments - without aggressive or unlawful practices. "
                          "All data is synthetic. Contact/jurisdiction rules are illustrative, configurable placeholders - NOT legal advice.")
RULES = load_rules()
GRACE = RULES["case_policy"]["promise_grace_days"]


def get_conn():
    path = os.environ.get("COLLECTIONS_DB", str(default_db_path()))
    if not Path(path).exists():
        raise HTTPException(503, f"Database not found at {path}. Run: python -m collections_ai.cli init-db")
    conn = db.connect(path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Depends(get_conn)


@app.exception_handler(sqlite3.IntegrityError)
async def _integrity(_, exc):
    return JSONResponse(status_code=409, content={"detail": f"Integrity constraint violated: {exc}"})


def _page(conn, table, where=(), params=(), limit=100, offset=0, order="1"):
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM {table} {w}", params).fetchone()[0]
    data = services.rows(conn, f"SELECT * FROM {table} {w} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset))
    return data


def _get_or_404(conn, table, col, val, label):
    r = services.one(conn, f"SELECT * FROM {table} WHERE {col}=?", (val,))
    if not r:
        raise HTTPException(404, f"{label} {val!r} not found")
    return r


def _ledger_for_loan(conn, loan_id: str):
    t = db.load_slice(conn, [loan_id])
    return t, Ledger(t["loan"], t["installment"], t["payment_allocation"], RULES["case_policy"]["default_dpd"])


# ============================================================================ health / metrics
@app.get("/health", response_model=S.Health, tags=["System"])
def health(conn=Conn):
    n = conn.execute("SELECT COUNT(*) FROM loan").fetchone()[0]
    return S.Health(status="ok", database="connected", loans=n, reference_date=REFERENCE_DATE.isoformat(), version=__version__)


@app.get("/metrics/summary", tags=["System"], summary="Portfolio KPIs computed from the database")
def metrics(conn=Conn):
    return services.portfolio_metrics(conn)


# ============================================================================ master data
@app.get("/customers", response_model=list[S.CustomerOut], tags=["Customers"])
def list_customers(risk_category: Optional[str] = None, jurisdiction_profile: Optional[str] = None, q: Optional[str] = Query(None, description="Match customer_id or name"),
                   limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    if risk_category:
        where.append("risk_category=?"); params.append(risk_category)
    if jurisdiction_profile:
        where.append("jurisdiction_profile=?"); params.append(jurisdiction_profile)
    if q:
        where.append("(customer_id LIKE ? OR full_name LIKE ?)"); params += [f"%{q}%"] * 2
    return _page(conn, "customer", where, params, limit, offset, "customer_id")


@app.get("/customers/{customer_id}", response_model=S.CustomerOut, tags=["Customers"])
def get_customer(customer_id: str, conn=Conn):
    return _get_or_404(conn, "customer", "customer_id", customer_id, "Customer")


@app.get("/loans", response_model=list[S.LoanOut], tags=["Loans"])
def list_loans(loan_status: Optional[str] = None, customer_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    if loan_status:
        where.append("loan_status=?"); params.append(loan_status)
    if customer_id:
        where.append("customer_id=?"); params.append(customer_id)
    return _page(conn, "loan", where, params, limit, offset, "loan_id")


@app.get("/loans/{loan_id}", response_model=S.LoanOut, tags=["Loans"])
def get_loan(loan_id: str, conn=Conn):
    return _get_or_404(conn, "loan", "loan_id", loan_id, "Loan")


@app.get("/loans/{loan_id}/delinquency", response_model=S.DelinquencyOut, tags=["Delinquency"], summary="Live delinquency calculation from instalments and allocations")
def loan_delinquency(loan_id: str, as_of: Optional[date] = None, conn=Conn):
    _get_or_404(conn, "loan", "loan_id", loan_id, "Loan")
    _, ledger = _ledger_for_loan(conn, loan_id)
    d = ledger.state(loan_id, as_of or REFERENCE_DATE).as_dict_money()
    d["days_past_due"] = d.pop("days_past_due")
    return S.DelinquencyOut(**d)


@app.get("/loans/{loan_id}/installments", response_model=list[S.InstallmentOut], tags=["Installments"])
def loan_installments(loan_id: str, conn=Conn):
    _get_or_404(conn, "loan", "loan_id", loan_id, "Loan")
    return services.rows(conn, "SELECT * FROM installment WHERE loan_id=? ORDER BY installment_number", (loan_id,))


@app.get("/installments", response_model=list[S.InstallmentOut], tags=["Installments"])
def list_installments(loan_id: Optional[str] = None, installment_status: Optional[str] = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    if loan_id:
        where.append("loan_id=?"); params.append(loan_id)
    if installment_status:
        where.append("installment_status=?"); params.append(installment_status)
    return _page(conn, "installment", where, params, limit, offset, "installment_id")


@app.get("/delinquency-snapshots", response_model=list[S.SnapshotOut], tags=["Delinquency"])
def list_snapshots(loan_id: Optional[str] = None, snapshot_date: Optional[date] = None, delinquency_bucket: Optional[str] = None,
                   limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    if loan_id:
        where.append("loan_id=?"); params.append(loan_id)
    if snapshot_date:
        where.append("snapshot_date=?"); params.append(snapshot_date.isoformat())
    if delinquency_bucket:
        where.append("delinquency_bucket=?"); params.append(delinquency_bucket)
    return _page(conn, "delinquency_snapshot", where, params, limit, offset, "snapshot_id")


# ============================================================================ collection cases
@app.get("/collection-cases", response_model=list[S.CaseOut], tags=["Collection cases"])
def list_cases(case_status: Optional[str] = None, loan_id: Optional[str] = None, customer_id: Optional[str] = None,
               limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("case_status", case_status), ("loan_id", loan_id), ("customer_id", customer_id)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "collection_case", where, params, limit, offset, "case_id")


@app.get("/collection-cases/{case_id}", response_model=S.CaseOut, tags=["Collection cases"])
def get_case(case_id: str, conn=Conn):
    return _get_or_404(conn, "collection_case", "case_id", case_id, "Case")


@app.post("/collection-cases", response_model=S.CaseOut, status_code=201, tags=["Collection cases"],
          summary="Open a case (rejected for paid-off loans or loans with an open case)")
def create_case(body: S.CaseCreate, conn=Conn):
    loan = _get_or_404(conn, "loan", "loan_id", body.loan_id, "Loan")
    _, ledger = _ledger_for_loan(conn, body.loan_id)
    st = ledger.state(body.loan_id, REFERENCE_DATE)
    if st.outstanding <= 0:
        raise HTTPException(422, "Loan is fully paid; a collection case cannot be opened (PAID_OFF).")
    if st.dpd < RULES["case_policy"]["case_open_dpd"]:
        raise HTTPException(422, f"Loan DPD {st.dpd} is below the case-opening threshold of {RULES['case_policy']['case_open_dpd']} days.")
    if services.one(conn, "SELECT 1 x FROM collection_case WHERE loan_id=? AND case_status<>'RESOLVED'", (body.loan_id,)):
        raise HTTPException(409, "An open case already exists for this loan.")
    now = db.now_iso()
    with db.transaction(conn):
        cid = ids.case_id(db.next_counter(conn, "collection_case", "case_id", "COL-"))
        conn.execute("INSERT INTO collection_case VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, body.loan_id, loan["customer_id"], REFERENCE_DATE.isoformat(), None, "OPEN", "UNVERIFIED", st.dpd, from_minor(st.arrears), body.assigned_analyst, now, now))
        db.write_audit(conn, "CASE", cid, "CASE_OPENED", None, "OPEN", body.actor, "Case opened via API")
        services.recompute_case(conn, cid, body.actor, "Initial status derivation")
    return services.one(conn, "SELECT * FROM collection_case WHERE case_id=?", (cid,))


ALLOWED_TRANSITIONS = {s: set(CASE_STATUSES) - {s} for s in CASE_STATUSES if s != "RESOLVED"}
ALLOWED_TRANSITIONS["RESOLVED"] = set()


@app.patch("/collection-cases/{case_id}/status", response_model=S.CaseOut, tags=["Collection cases"],
           summary="Lifecycle transition with business-rule guards (audited)")
def change_case_status(case_id: str, body: S.CaseStatusChange, conn=Conn):
    c = _get_or_404(conn, "collection_case", "case_id", case_id, "Case")
    if body.new_status not in ALLOWED_TRANSITIONS[c["case_status"]]:
        raise HTTPException(409, f"Transition {c['case_status']} -> {body.new_status} is not allowed (RESOLVED is terminal; open a new case instead).")
    _, ledger = _ledger_for_loan(conn, c["loan_id"])
    st = ledger.state(c["loan_id"], REFERENCE_DATE)
    if body.new_status == "RESOLVED" and st.arrears > 0:
        raise HTTPException(422, "Cannot resolve a case while arrears remain.")
    dispute = services.one(conn, "SELECT event_type FROM flag_event WHERE flag_type='DISPUTE' AND loan_id=? ORDER BY event_at DESC, flag_event_id DESC LIMIT 1", (c["loan_id"],))
    if dispute and dispute["event_type"] == "RAISED" and body.new_status in ("OPEN", "PROMISE_PENDING"):
        raise HTTPException(409, "An active dispute exists: the case must stay in DISPUTE_REVIEW until the dispute is cleared.")
    with db.transaction(conn):
        closed = REFERENCE_DATE.isoformat() if body.new_status == "RESOLVED" else None
        conn.execute("UPDATE collection_case SET case_status=?, closed_date=COALESCE(?, closed_date), updated_at=? WHERE case_id=?", (body.new_status, closed, db.now_iso(), case_id))
        db.write_audit(conn, "CASE", case_id, "STATUS_CHANGED", c["case_status"], body.new_status, body.actor, body.reason)
        if body.new_status == "RESOLVED":
            services.cancel_scheduled_attempts(conn, case_id, body.actor)
    return services.one(conn, "SELECT * FROM collection_case WHERE case_id=?", (case_id,))


# ============================================================================ prioritization
@app.get("/prioritization/cases/{case_id}", tags=["Prioritization"], summary="Live, fully explained prioritization decision for one case")
def explain_case(case_id: str, conn=Conn):
    c = _get_or_404(conn, "collection_case", "case_id", case_id, "Case")
    t = db.load_slice(conn, [c["loan_id"]])
    res = prioritize_cases(t, REFERENCE_DATE, RULES, [case_id])
    if not res:
        raise HTTPException(404, "Case not found in slice")
    return res[0]


@app.post("/prioritization/run", status_code=201, tags=["Prioritization"], summary="Run and persist prioritization for all non-resolved cases")
def run_prioritization(conn=Conn):
    return pipeline.persist_prioritization(conn, REFERENCE_DATE)


@app.get("/prioritization/results", response_model=list[S.PrioritizationOut], tags=["Prioritization"])
def prioritization_results(run_id: Optional[str] = Query(None, description="Defaults to the latest run"), priority_category: Optional[str] = None,
                           limit: int = Query(100, ge=1, le=2000), offset: int = Query(0, ge=0), conn=Conn):
    run = run_id or (services.latest_run(conn, "PRIORITIZATION") or {}).get("run_id")
    if not run:
        raise HTTPException(404, "No prioritization run exists yet; POST /prioritization/run")
    where, params = ["run_id=?"], [run]
    if priority_category:
        where.append("priority_category=?"); params.append(priority_category)
    return _page(conn, "prioritization_result", where, params, limit, offset, "priority_score DESC, case_id")


# ============================================================================ contact
def _build_ctx(conn, loan_id: str):
    t, ledger = _ledger_for_loan(conn, loan_id)
    ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], t["flag_event"], t["contact_attempt"], ledger, RULES)
    return ctx


def _eligibility(conn, body: S.ContactRequestIn):
    case = _get_or_404(conn, "collection_case", "case_id", body.case_id, "Case")
    ctx = _build_ctx(conn, case["loan_id"])
    dec = ctx.evaluate(ContactRequest(case["case_id"], case["customer_id"], case["loan_id"], body.channel, body.scheduled_for, body.purpose))
    return case, dec


def _elig_out(dec) -> S.EligibilityOut:
    return S.EligibilityOut(permitted=dec.permitted, reason_codes=dec.reason_codes, explanations=dec.explanations,
                            effective_window=list(dec.effective_window) if dec.effective_window else None, rules_version=dec.rules_version)


@app.post("/contact/eligibility", response_model=S.EligibilityOut, tags=["Contact management"], summary="Dry-run eligibility check (nothing is stored)")
def check_eligibility(body: S.ContactRequestIn, conn=Conn):
    return _elig_out(_eligibility(conn, body)[1])


@app.post("/contact-attempts", response_model=S.ScheduleOut, status_code=201, tags=["Contact management"],
          responses={422: {"description": "Rejected by the eligibility engine (the rejection is logged)"}},
          summary="Schedule a contact task. Prohibited requests are rejected with reasons and logged.")
def schedule_contact(body: S.ContactRequestIn, conn=Conn):
    case, dec = _eligibility(conn, body)
    if case["case_status"] == "RESOLVED":
        raise HTTPException(409, "Case is RESOLVED; no further contact can be scheduled.")
    now = db.now_iso()
    with db.transaction(conn):
        log_id = ids.scheduling_log_id(db.next_counter(conn, "contact_scheduling_log", "log_id", "SCH-"))
        attempt_id = None
        if dec.permitted:
            seq = (conn.execute("SELECT COALESCE(MAX(sequence_number),0) FROM contact_attempt WHERE case_id=?", (case["case_id"],)).fetchone()[0]) + 1
            attempt_id = ids.attempt_id(case["case_id"], seq)
            conn.execute("INSERT INTO contact_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (attempt_id, case["case_id"], case["customer_id"], seq, body.channel, body.purpose, body.scheduled_for.isoformat(timespec="seconds"), "SCHEDULED", None, now, now))
            db.write_audit(conn, "CONTACT_ATTEMPT", attempt_id, "SCHEDULED", None, body.channel, body.requested_by, body.purpose)
        conn.execute("INSERT INTO contact_scheduling_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (log_id, case["case_id"], case["customer_id"], body.channel, body.scheduled_for.isoformat(timespec="seconds"), body.purpose,
                      "PERMITTED" if dec.permitted else "REJECTED", dec.reasons_csv, attempt_id, body.requested_by, now, dec.rules_version, now, now))
    out = S.ScheduleOut(decision="PERMITTED" if dec.permitted else "REJECTED", log_id=log_id, attempt_id=attempt_id, eligibility=_elig_out(dec))
    if not dec.permitted:
        return JSONResponse(status_code=422, content={"detail": "Contact request rejected by eligibility rules", **out.model_dump()})
    return out


@app.get("/contact-attempts", response_model=list[S.AttemptOut], tags=["Contact management"])
def list_attempts(case_id: Optional[str] = None, customer_id: Optional[str] = None, attempt_status: Optional[str] = None,
                  limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("case_id", case_id), ("customer_id", customer_id), ("attempt_status", attempt_status)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "contact_attempt", where, params, limit, offset, "scheduled_for DESC")


@app.patch("/contact-attempts/{attempt_id}/outcome", response_model=S.AttemptOut, tags=["Contact management"],
           summary="Record the outcome of a scheduled attempt (dispute/hardship outcomes raise the flag automatically)")
def record_outcome(attempt_id: str, body: S.OutcomeIn, conn=Conn):
    a = _get_or_404(conn, "contact_attempt", "attempt_id", attempt_id, "Attempt")
    if a["attempt_status"] != "SCHEDULED":
        raise HTTPException(409, f"Attempt is {a['attempt_status']}; only SCHEDULED attempts can be completed.")
    case = _get_or_404(conn, "collection_case", "case_id", a["case_id"], "Case")
    with db.transaction(conn):
        conn.execute("UPDATE contact_attempt SET attempt_status='COMPLETED', outcome=?, updated_at=? WHERE attempt_id=?", (body.outcome, db.now_iso(), attempt_id))
        db.write_audit(conn, "CONTACT_ATTEMPT", attempt_id, "COMPLETED", "SCHEDULED", body.outcome, body.actor)
        at = datetime.fromisoformat(a["scheduled_for"]) + timedelta(minutes=5)
        if body.outcome == "REACHED_DISPUTE_RAISED":
            services.raise_or_clear_flag(conn, a["customer_id"], case["loan_id"], "DISPUTE", "RAISED", at, "CUSTOMER_CONTACT", "Raised during contact", body.actor)
        elif body.outcome == "REACHED_HARDSHIP_DECLARED":
            services.raise_or_clear_flag(conn, a["customer_id"], None, "HARDSHIP", "RAISED", at, "CUSTOMER_CONTACT", "Declared during contact", body.actor)
    return services.one(conn, "SELECT * FROM contact_attempt WHERE attempt_id=?", (attempt_id,))


@app.get("/contact-scheduling-log", response_model=list[S.SchedulingLogOut], tags=["Contact management"], summary="Permitted and rejected scheduling requests with reasons")
def scheduling_log(decision: Optional[str] = Query(None, pattern="^(PERMITTED|REJECTED)$"), case_id: Optional[str] = None,
                   limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("decision", decision), ("case_id", case_id)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "contact_scheduling_log", where, params, limit, offset, "requested_for DESC")


@app.post("/flags", response_model=S.FlagOut, status_code=201, tags=["Contact management"], summary="Raise/clear a hardship or dispute flag (append-only event)")
def post_flag(body: S.FlagIn, conn=Conn):
    _get_or_404(conn, "customer", "customer_id", body.customer_id, "Customer")
    if body.flag_type == "DISPUTE":
        if not body.loan_id:
            raise HTTPException(422, "loan_id is required for DISPUTE flags.")
        loan = _get_or_404(conn, "loan", "loan_id", body.loan_id, "Loan")
        if loan["customer_id"] != body.customer_id:
            raise HTTPException(422, "loan_id does not belong to customer_id.")
    at = body.event_at or datetime.combine(REFERENCE_DATE, time(12, 0))
    with db.transaction(conn):
        fid = services.raise_or_clear_flag(conn, body.customer_id, body.loan_id, body.flag_type, body.event_type, at, "ANALYST", body.note, body.actor)
    return services.one(conn, "SELECT * FROM flag_event WHERE flag_event_id=?", (fid,))


# ============================================================================ promises
@app.get("/promises", response_model=list[S.PromiseOut], tags=["Promises to pay"])
def list_promises(case_id: Optional[str] = None, promise_status: Optional[str] = None, loan_id: Optional[str] = None,
                  limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("case_id", case_id), ("promise_status", promise_status), ("loan_id", loan_id)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "promise_to_pay", where, params, limit, offset, "promise_id")


@app.post("/promises", response_model=S.PromiseOut, status_code=201, tags=["Promises to pay"],
          summary="Create a promise; amount may not exceed arrears unless a documented exception is supplied")
def create_promise(body: S.PromiseIn, conn=Conn):
    case = _get_or_404(conn, "collection_case", "case_id", body.case_id, "Case")
    if case["case_status"] == "RESOLVED":
        raise HTTPException(409, "Case is RESOLVED.")
    if case["case_status"] in ("DISPUTE_REVIEW", "HARDSHIP_REVIEW"):
        raise HTTPException(409, f"Case is {case['case_status']}: promises cannot be recorded until the review/dispute workflow completes.")
    created = body.created_date or REFERENCE_DATE
    if body.promised_date < created:
        raise HTTPException(422, "promised_date must be on/after created_date.")
    t, ledger = _ledger_for_loan(conn, case["loan_id"])
    st = ledger.state(case["loan_id"], created)
    if st.arrears <= 0:
        raise HTTPException(422, "No arrears at creation date; nothing can be promised.")
    amount = to_minor(body.promised_amount)
    if amount > st.arrears * RULES["case_policy"]["promise_max_ratio_of_arrears"]:
        if not (body.exception_approved_by and body.exception_reason):
            raise HTTPException(422, f"Promised amount {body.promised_amount:.2f} exceeds arrears {from_minor(st.arrears):.2f}. "
                                     "Provide exception_approved_by and exception_reason (documented exception workflow).")
        if amount > st.outstanding:
            raise HTTPException(422, f"Even with an exception the promise cannot exceed the outstanding balance {from_minor(st.outstanding):.2f}.")
    open_p = services.one(conn, "SELECT promise_id FROM promise_to_pay WHERE case_id=? AND promise_status='PENDING'", (body.case_id,))
    if open_p:
        raise HTTPException(409, f"Case already has a pending promise {open_p['promise_id']}.")
    if body.attempt_id and not services.one(conn, "SELECT 1 x FROM contact_attempt WHERE attempt_id=? AND case_id=?", (body.attempt_id, body.case_id)):
        raise HTTPException(422, "attempt_id does not exist for this case.")
    now = db.now_iso()
    with db.transaction(conn):
        pid = ids.promise_id(db.next_counter(conn, "promise_to_pay", "promise_id", "PTP-"))
        conn.execute("INSERT INTO promise_to_pay VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, body.case_id, case["loan_id"], case["customer_id"], body.attempt_id, created.isoformat(), body.promised_amount, body.promised_date.isoformat(),
                      from_minor(st.arrears), "PENDING", 0.0, None, body.exception_approved_by, body.exception_reason, now, now))
        db.write_audit(conn, "PROMISE", pid, "PROMISE_CREATED", None, "PENDING", body.actor, "Promise recorded", {"promised_amount": body.promised_amount})
        services.recompute_case(conn, body.case_id, body.actor, "Promise created")
    return services.one(conn, "SELECT * FROM promise_to_pay WHERE promise_id=?", (pid,))


def _evaluate_and_store(conn, p: dict, ledger: Ledger, actor: str) -> Optional[str]:
    st, recv, ev = evaluate_promise(ledger, p["loan_id"], date.fromisoformat(p["created_date"]), date.fromisoformat(p["promised_date"]),
                                    to_minor(p["promised_amount"]), REFERENCE_DATE, GRACE)
    if st != p["promise_status"] or abs(recv / 100 - p["amount_received"]) > 0.005:
        conn.execute("UPDATE promise_to_pay SET promise_status=?, amount_received=?, evaluated_on=?, updated_at=? WHERE promise_id=?",
                     (st, from_minor(recv), ev.isoformat() if ev else None, db.now_iso(), p["promise_id"]))
        if st != p["promise_status"]:
            db.write_audit(conn, "PROMISE", p["promise_id"], "STATUS_CHANGED", p["promise_status"], st, actor, "Outcome derived from payment allocations")
        return st
    return None


@app.post("/promises/{promise_id}/evaluate", response_model=S.PromiseOut, tags=["Promises to pay"], summary="Reconcile a promise against subsequent payment allocations")
def evaluate_one(promise_id: str, conn=Conn):
    p = _get_or_404(conn, "promise_to_pay", "promise_id", promise_id, "Promise")
    _, ledger = _ledger_for_loan(conn, p["loan_id"])
    with db.transaction(conn):
        _evaluate_and_store(conn, p, ledger, "SYSTEM")
        services.recompute_case(conn, p["case_id"], "SYSTEM", "Promise evaluated")
    return services.one(conn, "SELECT * FROM promise_to_pay WHERE promise_id=?", (promise_id,))


# ============================================================================ payments
@app.get("/payments", response_model=list[S.PaymentOut], tags=["Payments"])
def list_payments(loan_id: Optional[str] = None, reconciliation_status: Optional[str] = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("loan_id", loan_id), ("reconciliation_status", reconciliation_status)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "payment", where, params, limit, offset, "payment_id")


@app.get("/payments/{payment_id}/allocations", response_model=list[S.AllocationOut], tags=["Payments"])
def payment_allocations(payment_id: str, conn=Conn):
    _get_or_404(conn, "payment", "payment_id", payment_id, "Payment")
    return services.rows(conn, "SELECT * FROM payment_allocation WHERE payment_id=? ORDER BY allocation_id", (payment_id,))


@app.post("/payments", response_model=S.PaymentResult, status_code=201, tags=["Payments"],
          summary="Post a payment: validated, allocated oldest-due-first, reconciled; promises and case status updated")
def post_payment(body: S.PaymentIn, conn=Conn):
    loan = _get_or_404(conn, "loan", "loan_id", body.loan_id, "Loan")
    pdate = body.payment_date or REFERENCE_DATE
    if pdate > REFERENCE_DATE:
        raise HTTPException(422, "payment_date cannot be after the business reference date.")
    if pdate <= date.fromisoformat(loan["origination_date"]):
        raise HTTPException(422, "payment_date must be after loan origination.")
    if services.one(conn, "SELECT 1 x FROM payment WHERE reference=?", (body.reference,)):
        raise HTTPException(409, f"Duplicate payment reference {body.reference!r}.")
    t, ledger = _ledger_for_loan(conn, body.loan_id)
    st = ledger.state(body.loan_id, REFERENCE_DATE)
    amount = to_minor(body.amount)
    if amount > st.outstanding:
        raise HTTPException(422, f"Payment {body.amount:.2f} exceeds outstanding balance {from_minor(st.outstanding):.2f} (overpayments are not supported).")
    remaining = []
    for iid, _, _, amt in ledger.installments(body.loan_id):
        remaining.append((iid, amt - ledger.inst_paid_asof(iid, REFERENCE_DATE)))
    allocs = fifo_allocate(remaining, amount)
    now = db.now_iso()
    promises_updated: list[str] = []
    resolved = None
    with db.transaction(conn):
        pid = ids.payment_id(db.next_counter(conn, "payment", "payment_id", "PMT-"))
        conn.execute("INSERT INTO payment VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, body.loan_id, loan["customer_id"], pdate.isoformat(), body.amount, body.channel, body.reference, "UNRECONCILED", now, now))
        base = db.next_counter(conn, "payment_allocation", "allocation_id", "ALC-")
        for k, (iid, m) in enumerate(allocs):
            conn.execute("INSERT INTO payment_allocation VALUES (?,?,?,?,?,?,?,?)", (ids.allocation_id(base + k), pid, iid, from_minor(m), pdate.isoformat(), "FIFO_OLDEST_DUE_FIRST", now, now))
        db.write_audit(conn, "PAYMENT", pid, "PAYMENT_POSTED", None, f"{body.amount:.2f}", body.actor, "Payment posted and allocated FIFO", {"allocations": [(i, from_minor(m)) for i, m in allocs]})
        # derived instalment fields (history stays in payment_allocation)
        for iid, m in allocs:
            row = services.one(conn, "SELECT amount_due, amount_paid, due_date FROM installment WHERE installment_id=?", (iid,))
            paid = round(row["amount_paid"] + from_minor(m), 2)
            full = paid >= row["amount_due"] - 0.005
            status = "PAID" if full else ("NOT_DUE" if row["due_date"] >= REFERENCE_DATE.isoformat() else "PARTIALLY_PAID")
            conn.execute("UPDATE installment SET amount_paid=?, installment_status=?, fully_paid_date=?, updated_at=? WHERE installment_id=?", (paid, status, pdate.isoformat() if full else None, now, iid))
        conn.execute("UPDATE payment SET reconciliation_status='RECONCILED', updated_at=? WHERE payment_id=?", (now, pid))
        # refresh the loan from source records
        t2 = db.load_slice(conn, [body.loan_id])
        led2 = Ledger(t2["loan"], t2["installment"], t2["payment_allocation"], RULES["case_policy"]["default_dpd"])
        st2 = led2.state(body.loan_id, REFERENCE_DATE)
        if st2.status != loan["loan_status"]:
            conn.execute("UPDATE loan SET loan_status=?, payoff_date=?, updated_at=? WHERE loan_id=?", (st2.status, pdate.isoformat() if st2.status == "PAID_OFF" else None, now, body.loan_id))
            db.write_audit(conn, "LOAN", body.loan_id, "LOAN_STATUS_CHANGED", loan["loan_status"], st2.status, body.actor, "Derived after payment")
        for p in services.rows(conn, "SELECT * FROM promise_to_pay WHERE loan_id=? AND promise_status='PENDING'", (body.loan_id,)):
            if _evaluate_and_store(conn, p, led2, "SYSTEM"):
                promises_updated.append(p["promise_id"])
        for c in services.rows(conn, "SELECT * FROM collection_case WHERE loan_id=? AND case_status<>'RESOLVED'", (body.loan_id,)):
            if st2.arrears == 0:
                conn.execute("UPDATE collection_case SET case_status='RESOLVED', closed_date=?, updated_at=? WHERE case_id=?", (pdate.isoformat(), now, c["case_id"]))
                db.write_audit(conn, "CASE", c["case_id"], "STATUS_CHANGED", c["case_status"], "RESOLVED", "SYSTEM", "Arrears cleared by payment " + pid)
                resolved = c["case_id"]
                services.cancel_scheduled_attempts(conn, c["case_id"], "SYSTEM", "Case resolved by payment " + pid)
            else:
                services.recompute_case(conn, c["case_id"], "SYSTEM", "Payment posted")
    return S.PaymentResult(payment=services.one(conn, "SELECT * FROM payment WHERE payment_id=?", (pid,)),
                           allocations=services.rows(conn, "SELECT * FROM payment_allocation WHERE payment_id=?", (pid,)),
                           loan_status=st2.status, outstanding_balance=from_minor(st2.outstanding), case_resolved=resolved, promises_updated=promises_updated)


# ============================================================================ reconciliation
@app.post("/reconciliation/run", status_code=201, tags=["Reconciliation"], summary="Replay FIFO allocation for every payment and update reconciliation status (audited)")
def run_reconciliation(conn=Conn):
    return pipeline.reconcile_and_persist(conn, REFERENCE_DATE)


@app.get("/reconciliation/summary", tags=["Reconciliation"])
def reconciliation_summary(conn=Conn):
    m = services.portfolio_metrics(conn)
    return {k: m[k] for k in ("payments_total", "payments_by_reconciliation_status", "payment_reconciliation_rate", "promises_by_status", "promise_fulfillment_rate", "promise_fulfilment_or_partial_rate")}


@app.get("/reconciliation/exceptions", tags=["Reconciliation"], summary="Live list of payments whose allocation disagrees with the FIFO policy")
def reconciliation_exceptions(limit: int = Query(100, ge=1, le=1000), conn=Conn):
    t = db.load_tables(conn, ["installment", "payment", "payment_allocation"])
    r = reconcile_payments(t["installment"], t["payment"], t["payment_allocation"])
    return r[r["reconciliation_status"] != "RECONCILED"].head(limit).to_dict("records")


# ============================================================================ exceptions / validation / audit
@app.post("/exceptions/detect", status_code=201, tags=["Exceptions"], summary="Run all compliance detectors on source data and persist results")
def detect_exceptions(conn=Conn):
    return pipeline.persist_exceptions(conn, REFERENCE_DATE)


@app.get("/exceptions", response_model=list[S.ExceptionOut], tags=["Exceptions"])
def list_exceptions(run_id: Optional[str] = None, exception_type: Optional[str] = None, status: Optional[str] = None,
                    limit: int = Query(100, ge=1, le=2000), offset: int = Query(0, ge=0), conn=Conn):
    run = run_id or (services.latest_run(conn, "EXCEPTION_DETECTION") or {}).get("run_id")
    if not run:
        raise HTTPException(404, "No exception detection run yet; POST /exceptions/detect")
    where, params = ["run_id=?"], [run]
    for col, v in (("exception_type", exception_type), ("status", status)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "exception_record", where, params, limit, offset, "exception_id")


@app.patch("/exceptions/{exception_id}", response_model=S.ExceptionOut, tags=["Exceptions"], summary="Update review status (audited)")
def update_exception(exception_id: str, body: S.ExceptionStatusIn, conn=Conn):
    e = _get_or_404(conn, "exception_record", "exception_id", exception_id, "Exception")
    with db.transaction(conn):
        conn.execute("UPDATE exception_record SET status=? WHERE exception_id=?", (body.status, exception_id))
        db.write_audit(conn, "EXCEPTION", exception_id, "STATUS_CHANGED", e["status"], body.status, body.actor, body.note)
    return services.one(conn, "SELECT * FROM exception_record WHERE exception_id=?", (exception_id,))


@app.post("/validation/run", status_code=201, tags=["Validation"], summary="Run the full automated validation suite and persist the report")
def run_validation_endpoint(conn=Conn):
    return pipeline.persist_validation(conn, REFERENCE_DATE)


@app.get("/validation/reports/latest", tags=["Validation"])
def latest_validation(conn=Conn):
    run = services.latest_run(conn, "VALIDATION")
    if not run:
        raise HTTPException(404, "No validation run yet; POST /validation/run")
    return {"run": run, "results": services.rows(conn, "SELECT check_name, category, severity, passed, violation_count, detail FROM validation_result WHERE run_id=? ORDER BY result_id", (run["run_id"],))}


@app.get("/validation/evaluation", tags=["Validation"],
         summary="Score the latest detected exceptions against the separately stored ground-truth file (evaluation only)")
def evaluation(ground_truth_path: str = str(ROOT / "data" / "full" / "ground_truth.csv"), conn=Conn):
    p = Path(ground_truth_path)
    if not p.exists():
        raise HTTPException(404, f"Ground-truth file not found at {p}")
    run = services.latest_run(conn, "EXCEPTION_DETECTION")
    if not run:
        raise HTTPException(404, "No exception detection run yet")
    det = services.rows(conn, "SELECT exception_type, entity_id FROM exception_record WHERE run_id=?", (run["run_id"],))
    return evaluate_against_ground_truth(det, pd.read_csv(p, dtype=str)).to_dict("records")


@app.get("/audit-events", response_model=list[S.AuditOut], tags=["Audit"])
def audit_events(entity_type: Optional[str] = None, entity_id: Optional[str] = None, action: Optional[str] = None,
                 limit: int = Query(100, ge=1, le=2000), offset: int = Query(0, ge=0), conn=Conn):
    where, params = [], []
    for col, v in (("entity_type", entity_type), ("entity_id", entity_id), ("action", action)):
        if v:
            where.append(f"{col}=?"); params.append(v)
    return _page(conn, "audit_event", where, params, limit, offset, "event_ts DESC, audit_id DESC")
