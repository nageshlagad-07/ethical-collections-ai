"""Transparent, rules-based collection prioritization.

Score (0-100) = sum of explicit, banded factor points (config/rules.json -> scoring).
Legal / customer-contact restrictions then OVERRIDE the score: a suppressed or
review-only case can never be escalated by a high score. Every decision carries
factors, reason codes, suppression reasons, a recommended permissible action and
a plain-language explanation. No black-box model, no ground-truth input.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd

from .contact import ContactContext, effective_window
from .delinquency import Ledger, from_minor, to_minor
from .rules import load_rules

ACTION_TEXT = {
    "CLOSE_CASE_AFTER_REVIEW": "Loan is fully paid: take NO collection action; analyst to confirm and close the case.",
    "REFER_TO_COMPLIANCE_REVIEW": "Servicing account suspended: refer to risk/compliance reviewer; no outbound contact.",
    "FOLLOW_DISPUTE_VALIDATION_WORKFLOW": "Debt is disputed: pause collection contact, acknowledge the dispute in writing and validate the debt before any further collection.",
    "REFER_TO_HARDSHIP_REVIEW_TEAM": "Hardship flag active: refer to hardship review (assess affordability/arrangement). Do not escalate.",
    "NO_OUTBOUND_CONTACT_ANALYST_REVIEW": "Customer opted out of outbound contact: no outbound contact; analyst reviews permitted alternatives.",
    "NO_PERMITTED_CHANNEL_ANALYST_REVIEW": "No contact channel/window is permitted by the customer's preferences: analyst review required.",
    "MONITOR_PROMISE": "Active promise-to-pay: do not contact until the promise window ends; reconcile payments.",
    "SEND_PAYMENT_REMINDER": "Send a courteous payment reminder on a permitted channel/slot.",
    "SCHEDULE_PROMISE_TO_PAY_CALL": "Schedule a conversation to agree a realistic promise-to-pay.",
    "OFFER_PAYMENT_ARRANGEMENT_DISCUSSION": "Discuss a payment arrangement suited to the customer's circumstances.",
    "REFER_TO_SENIOR_ANALYST_REVIEW": "Long-overdue: refer to a senior analyst for human review (no automatic escalation).",
}


def _band_points(value: float, bands: list) -> tuple[float, str]:
    for lo, hi, pts in bands:
        if value >= lo and (hi is None or value < hi):
            return pts, f"[{lo}, {hi if hi is not None else 'inf'})"
    return 0, "n/a"


def score_features(f: dict, rules: dict) -> tuple[float, list]:
    """Pure scoring function: features -> (score, factors)."""
    sc = rules["scoring"]
    factors = []
    pts, band = _band_points(f["arrears"], sc["arrears_amount_bands"])
    factors.append({"factor": "arrears_amount", "value": round(f["arrears"], 2), "points": pts,
                    "explanation": f"Arrears {f['arrears']:.2f} INR falls in band {band}"})
    pts, band = _band_points(f["dpd"], sc["dpd_bands"])
    factors.append({"factor": "days_past_due", "value": f["dpd"], "points": pts, "explanation": f"{f['dpd']} days past due, band {band} days"})
    pts = min(sc["late_installment_cap"], sc["late_installment_points_each"] * f["late_installments"])
    factors.append({"factor": "payment_history", "value": f["late_installments"], "points": pts,
                    "explanation": f"{f['late_installments']} instalment(s) paid >7 days late or still unpaid"})
    pts = sc["risk_points"][f["risk_category"]]
    factors.append({"factor": "risk_category", "value": f["risk_category"], "points": pts, "explanation": f"Customer risk category {f['risk_category']}"})
    pts = min(sc["broken_promise_cap"], sc["broken_promise_points_each"] * f["broken_promises"])
    factors.append({"factor": "broken_promises", "value": f["broken_promises"], "points": pts, "explanation": f"{f['broken_promises']} previously broken promise(s)"})
    pts, band = _band_points(f["outstanding"], sc["outstanding_balance_bands"])
    factors.append({"factor": "outstanding_balance", "value": round(f["outstanding"], 2), "points": pts,
                    "explanation": f"Outstanding contractual balance {f['outstanding']:.2f} INR, band {band}"})
    return float(min(100, sum(x["points"] for x in factors))), factors


def _category(score: float, rules: dict) -> str:
    th = rules["scoring"]["category_thresholds"]
    return "P1_HIGH" if score >= th["P1_HIGH"] else ("P2_MEDIUM" if score >= th["P2_MEDIUM"] else "P3_LOW")


def _action_for_dpd(dpd: int) -> str:
    if dpd < 30:
        return "SEND_PAYMENT_REMINDER"
    if dpd < 60:
        return "SCHEDULE_PROMISE_TO_PAY_CALL"
    if dpd <= 90:
        return "OFFER_PAYMENT_ARRANGEMENT_DISCUSSION"
    return "REFER_TO_SENIOR_ANALYST_REVIEW"


def prioritize_cases(t: dict, ref: date, rules: Optional[dict] = None, case_ids: Optional[list] = None) -> list[dict]:
    """Prioritize all non-resolved cases (or the given case_ids). Returns result dicts (no ids)."""
    rules = rules or load_rules()
    tol = rules["case_policy"]["paid_off_tolerance"] * 100
    grace = rules["case_policy"]["promise_grace_days"]
    ledger = Ledger(t["loan"], t["installment"], t["payment_allocation"], rules["case_policy"]["default_dpd"])
    ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], t["flag_event"], t["contact_attempt"], ledger, rules)
    risk_of = dict(zip(t["customer"]["customer_id"], t["customer"]["risk_category"]))
    ref_dt = datetime.combine(ref, time(23, 59, 59))
    promises = t["promise_to_pay"]
    broken = promises[promises["promise_status"] == "BROKEN"].groupby("loan_id").size().to_dict()
    pending = promises[promises["promise_status"] == "PENDING"].groupby("case_id")["promised_date"].max().to_dict()
    cases = t["collection_case"]
    cases = cases[cases["case_status"] != "RESOLVED"] if case_ids is None else cases[cases["case_id"].isin(case_ids)]
    out = []
    for c in cases.itertuples(index=False):
        st = ledger.state(c.loan_id, ref)
        feats = {"arrears": from_minor(st.arrears), "dpd": st.dpd, "outstanding": from_minor(st.outstanding),
                 "late_installments": ledger.late_installment_count(c.loan_id, ref), "risk_category": risk_of[c.customer_id],
                 "broken_promises": int(broken.get(c.loan_id, 0))}
        score, factors = score_features(feats, rules)
        pref = ctx.preference_on(c.customer_id, ref)
        d_act = ctx.flag_active("DISPUTE", c.loan_id, ref_dt)
        h_act = ctx.flag_active("HARDSHIP", c.customer_id, ref_dt)
        suppress: list[str] = []
        if st.outstanding <= tol:
            suppress.append("SUP_PAID_OFF")
        if c.loan_id in ctx.suspended_loans:
            suppress.append("SUP_ACCOUNT_SUSPENDED")
        if d_act:
            suppress.append("SUP_ACTIVE_DISPUTE")
        opt_out = pref is not None and bool(int(pref["opt_out_all"]))
        no_channel = pref is None or not any(int(pref[f"allow_{ch}"]) for ch in ("phone", "sms", "email", "post"))
        if opt_out:
            suppress.append("SUP_OPT_OUT_ALL")
        elif no_channel:
            suppress.append("SUP_NO_PERMITTED_CHANNEL")
        reasons = [f"{x['factor'].upper()}:+{x['points']}" for x in factors if x["points"]]
        channel = slot = None
        # ------- override precedence: legal/contact restrictions beat the score ---------
        if "SUP_PAID_OFF" in suppress:
            cat, action = "SUPPRESSED", "CLOSE_CASE_AFTER_REVIEW"
        elif "SUP_ACCOUNT_SUSPENDED" in suppress:
            cat, action = "SUPPRESSED", "REFER_TO_COMPLIANCE_REVIEW"
        elif d_act:
            cat, action = "SUPPRESSED", "FOLLOW_DISPUTE_VALIDATION_WORKFLOW"
        elif h_act:
            cat, action = "HARDSHIP_REVIEW", "REFER_TO_HARDSHIP_REVIEW_TEAM"
            reasons.append("REV_HARDSHIP")
        elif opt_out:
            cat, action = "SUPPRESSED", "NO_OUTBOUND_CONTACT_ANALYST_REVIEW"
        elif no_channel:
            cat, action = "SUPPRESSED", "NO_PERMITTED_CHANNEL_ANALYST_REVIEW"
        elif c.case_id in pending:
            cat, action = "MONITOR_PROMISE", "MONITOR_PROMISE"
            reasons.append("SUP_PROMISE_PENDING")
        else:
            cat, action = _category(score, rules), _action_for_dpd(st.dpd)
            found = ctx.next_permissible_slot(c.customer_id, c.loan_id, c.case_id, "COLLECTION_REMINDER", ref_dt)
            if found:
                slot, channel = found[0].isoformat(timespec="seconds"), found[1]
            else:
                reasons.append("NO_PERMISSIBLE_SLOT_IN_HORIZON")
        if h_act and "SUP_ACTIVE_DISPUTE" not in suppress and cat != "HARDSHIP_REVIEW":
            reasons.append("REV_HARDSHIP")
        pieces = [f"Score {score:.0f}/100 ("
                  + ", ".join(f"{x['factor']} +{x['points']}" for x in factors if x["points"]) + ")."]
        if cat in ("SUPPRESSED", "HARDSHIP_REVIEW", "MONITOR_PROMISE"):
            pieces.append(f"Category {cat} - the score is NOT used because: " + (", ".join(suppress) or ("HARDSHIP_ACTIVE" if h_act else "PROMISE_PENDING")) + ".")
        else:
            pieces.append(f"Category {cat} (thresholds P1>={rules['scoring']['category_thresholds']['P1_HIGH']}, P2>={rules['scoring']['category_thresholds']['P2_MEDIUM']}).")
        pieces.append(ACTION_TEXT[action])
        if slot:
            pieces.append(f"Next permissible slot: {slot} via {channel}.")
        out.append({"case_id": c.case_id, "loan_id": c.loan_id, "customer_id": c.customer_id, "as_of_date": ref.isoformat(),
                    "priority_score": round(score, 1), "priority_category": cat, "contributing_factors": factors,
                    "reason_codes": ",".join(reasons), "suppression_reasons": ",".join(suppress) or None,
                    "recommended_action": action, "recommended_channel": channel, "recommended_slot": slot,
                    "explanation": " ".join(pieces), "arrears": feats["arrears"], "days_past_due": st.dpd})
    return out
