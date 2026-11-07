"""Rule-based exception detection from SOURCE records only.

This module never reads ground-truth labels. It re-derives the expected state
from allocations, preferences, flag events and the ledger, and reports where the
stored records disagree with the documented rules.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from .contact import ContactContext, ContactRequest, REASON_TEXT
from .delinquency import Ledger, from_minor, to_minor
from .reconciliation import reconcile_payments
from .rules import load_rules

SEVERITY = {
    "PAID_ACCOUNT_IN_COLLECTION": "HIGH", "CONTACT_OUTSIDE_ALLOWED_WINDOW": "CRITICAL",
    "PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT": "MEDIUM", "PROMISE_AMOUNT_EXCEEDS_ARREARS": "MEDIUM",
    "DISPUTED_DEBT_TREATED_AS_CONFIRMED": "CRITICAL",
}


def _exc(kind, etype, eid, case=None, loan=None, cust=None, desc="", expected="", response=""):
    return {"exception_type": kind, "severity": SEVERITY[kind], "entity_type": etype, "entity_id": eid, "case_id": case,
            "loan_id": loan, "customer_id": cust, "description": desc, "expected_decision": expected, "system_response": response}


def detect_all(t: dict, ref: date, rules: dict | None = None) -> list[dict]:
    """Run all five detectors on a table dictionary and return exception dicts."""
    rules = rules or load_rules()
    ledger = Ledger(t["loan"], t["installment"], t["payment_allocation"], rules["case_policy"]["default_dpd"])
    out: list[dict] = []
    tol = rules["case_policy"]["paid_off_tolerance"]
    cases = t["collection_case"]
    ref_dt = datetime.combine(ref, time(23, 59, 59))
    ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], t["flag_event"], t["contact_attempt"], ledger, rules)

    # 1 paid account remains in collection
    for c in cases[cases["case_status"] != "RESOLVED"].itertuples(index=False):
        st = ledger.state(c.loan_id, ref)
        if st.outstanding <= tol * 100:
            out.append(_exc("PAID_ACCOUNT_IN_COLLECTION", "CASE", c.case_id, c.case_id, c.loan_id, c.customer_id,
                            f"Case {c.case_id} is {c.case_status} but loan {c.loan_id} has zero outstanding balance.",
                            "Suppress collection actions and close the case after review.",
                            "Prioritization suppresses the case (SUP_PAID_OFF); contact engine rejects all collection contact."))

    # 2 contact outside allowed window (and any other eligibility breach)
    loan_of = dict(zip(cases["case_id"], cases["loan_id"]))
    for a in t["contact_attempt"].itertuples(index=False):
        if a.attempt_status == "CANCELLED":
            continue
        when = datetime.fromisoformat(a.scheduled_for)
        dec = ctx.evaluate(ContactRequest(a.case_id, a.customer_id, loan_of[a.case_id], a.channel, when, a.purpose))
        if not dec.permitted:
            out.append(_exc("CONTACT_OUTSIDE_ALLOWED_WINDOW", "CONTACT_ATTEMPT", a.attempt_id, a.case_id, loan_of[a.case_id], a.customer_id,
                            f"Attempt {a.attempt_id} violates: " + "; ".join(f"{r} ({REASON_TEXT[r]})" for r in dec.reason_codes),
                            "Eligibility engine must reject this contact; review how it bypassed scheduling controls.",
                            "Contact engine would have REJECTED this request; recorded as compliance exception."))

    # 3 payment allocated to wrong installment
    rec = reconcile_payments(t["installment"], t["payment"], t["payment_allocation"])
    pay = t["payment"].set_index("payment_id")
    for r in rec[rec["reconciliation_status"] == "EXCEPTION"].itertuples(index=False):
        loan = pay.at[r.payment_id, "loan_id"]
        out.append(_exc("PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT", "PAYMENT", r.payment_id, None, loan, pay.at[r.payment_id, "customer_id"],
                        f"Payment {r.payment_id}: {r.reason}. Expected {r.expected_installment_ids or '-'}; recorded {r.recorded_installment_ids or '-'}.",
                        "Re-allocate oldest-due-first and recompute arrears.", "Reconciliation marks payment EXCEPTION."))

    # 4 promise amount exceeds arrears (without approved exception)
    for p in t["promise_to_pay"].itertuples(index=False):
        if p.exception_approved_by not in (None, "") and not pd.isna(p.exception_approved_by):
            continue
        arrears = ledger.state(p.loan_id, date.fromisoformat(p.created_date)).arrears
        if to_minor(p.promised_amount) > arrears * rules["case_policy"]["promise_max_ratio_of_arrears"] + 1:
            out.append(_exc("PROMISE_AMOUNT_EXCEEDS_ARREARS", "PROMISE", p.promise_id, p.case_id, p.loan_id, p.customer_id,
                            f"Promise {p.promise_id} of {p.promised_amount:.2f} exceeds arrears {from_minor(arrears):.2f} on {p.created_date}.",
                            "Reject or route to documented exception approval.", "Promise API rejects such promises (HTTP 422)."))

    # 5 disputed debt treated as confirmed
    for c in cases[cases["case_status"] != "RESOLVED"].itertuples(index=False):
        if ctx.flag_active("DISPUTE", c.loan_id, ref_dt) and (c.debt_confirmation_status == "CONFIRMED" or c.case_status != "DISPUTE_REVIEW"):
            out.append(_exc("DISPUTED_DEBT_TREATED_AS_CONFIRMED", "CASE", c.case_id, c.case_id, c.loan_id, c.customer_id,
                            f"Loan {c.loan_id} has an active dispute but case {c.case_id} is {c.case_status} / {c.debt_confirmation_status}.",
                            "Restore DISPUTED status; pause collection; follow dispute workflow.",
                            "Prioritization suppresses the case (SUP_ACTIVE_DISPUTE) regardless of case label."))
    return out


def evaluate_against_ground_truth(detected: list[dict], ground_truth: pd.DataFrame) -> pd.DataFrame:
    """Score detection vs the separately stored labels (evaluation only; never feeds detection)."""
    det = {(d["exception_type"], d["entity_id"]) for d in detected}
    gt = {(r.issue_type, r.entity_id) for r in ground_truth.itertuples(index=False)}
    rows = []
    for kind in sorted({k for k, _ in det} | {k for k, _ in gt}):
        d = {e for k, e in det if k == kind}
        g = {e for k, e in gt if k == kind}
        tp = len(d & g)
        rows.append({"issue_type": kind, "ground_truth": len(g), "detected": len(d), "true_positive": tp,
                     "false_positive": len(d - g), "false_negative": len(g - d),
                     "precision": round(tp / len(d), 4) if d else None, "recall": round(tp / len(g), 4) if g else None})
    return pd.DataFrame(rows)
