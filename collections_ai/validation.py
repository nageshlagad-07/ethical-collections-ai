"""Automated dataset validation.

Structural / arithmetic / lifecycle checks must pass on ANY dataset (including the one with injected
anomalies - injection preserves structure). Business-rule checks run the same detectors the
platform uses; on the baseline they must find nothing, on the final dataset they find the injected cases.
"""
from __future__ import annotations

import re
from datetime import date

import numpy as np
import pandas as pd

from .delinquency import Ledger, snapshot_frame, to_minor
from .detection import detect_all
from .reconciliation import evaluate_promise
from .rules import load_rules
from .spec import TABLE_BY_NAME, DATASET_TABLES, ID_PATTERNS, ISSUE_TYPES

LEGAL_LOAN_TRANSITIONS = {
    "CURRENT": {"CURRENT", "DELINQUENT", "DEFAULT", "PAID_OFF"},
    "DELINQUENT": {"CURRENT", "DELINQUENT", "DEFAULT", "PAID_OFF"},
    "DEFAULT": {"DEFAULT", "DELINQUENT", "CURRENT", "PAID_OFF"},
    "PAID_OFF": {"PAID_OFF"},
}


def _res(name, category, violations, severity="HIGH", detail="", expected_zero=True):
    return {"check_name": name, "category": category, "severity": severity, "passed": int(violations == 0) if expected_zero else 1,
            "violation_count": int(violations), "detail": detail[:900]}


def _sample(ids, k=5):
    ids = list(ids)
    return f"e.g. {ids[:k]}" if ids else ""


def run_validation(t: dict, ref: date, rules: dict | None = None) -> list[dict]:
    rules = rules or load_rules()
    R: list[dict] = []
    present = [n for n in DATASET_TABLES if n in t]

    # --- 1. primary keys, id patterns ------------------------------------------------
    for n in present:
        pk = TABLE_BY_NAME[n].pk
        dup = t[n][pk].duplicated().sum()
        R.append(_res(f"pk_unique:{n}", "STRUCTURAL", dup, detail=f"{len(t[n])} rows"))
        if n in ID_PATTERNS:
            bad = t[n][~t[n][pk].astype(str).str.match(ID_PATTERNS[n])][pk]
            R.append(_res(f"id_pattern:{n}", "STRUCTURAL", len(bad), detail=_sample(bad)))

    # --- 2. foreign keys -------------------------------------------------------------
    for n in present:
        for f in TABLE_BY_NAME[n].fields:
            if f.fk and f.fk.split(".")[0] in t:
                pt, pc = f.fk.split(".")
                vals = t[n][f.name].dropna()
                bad = vals[~vals.isin(set(t[pt][pc]))]
                R.append(_res(f"fk:{n}.{f.name}->{pt}.{pc}", "STRUCTURAL", len(bad), "CRITICAL", _sample(bad)))

    # --- 3. enums + mandatory values ----------------------------------------------------
    for n in present:
        spec = TABLE_BY_NAME[n]
        bad_enum = bad_null = 0
        notes = []
        for f in spec.fields:
            col = t[n][f.name]
            if f.allowed:
                v = col.dropna()
                k = (~v.isin(f.allowed)).sum()
                if k:
                    bad_enum += k
                    notes.append(f.name)
            if f.required and f.name != spec.pk:
                k = (col.isna() | (col.astype(str) == "")).sum()
                if k:
                    bad_null += k
                    notes.append(f"{f.name}(null)")
        R.append(_res(f"enum_values:{n}", "STRUCTURAL", bad_enum, detail=",".join(notes)))
        R.append(_res(f"mandatory_values:{n}", "STRUCTURAL", bad_null, detail=",".join(notes)))

    # --- 4. duplicates / natural keys ------------------------------------------------------
    R.append(_res("dup:payment.reference", "STRUCTURAL", t["payment"]["reference"].duplicated().sum()))
    R.append(_res("dup:installment(loan,number)", "STRUCTURAL", t["installment"].duplicated(["loan_id", "installment_number"]).sum()))
    R.append(_res("dup:payment_allocation(payment,installment)", "STRUCTURAL", t["payment_allocation"].duplicated(["payment_id", "installment_id"]).sum()))
    R.append(_res("dup:customer.email", "STRUCTURAL", t["customer"]["email"].duplicated().sum()))
    R.append(_res("dup:snapshot(loan,date)", "STRUCTURAL", t["delinquency_snapshot"].duplicated(["loan_id", "snapshot_date"]).sum()))
    open_cases = t["collection_case"][t["collection_case"]["case_status"] != "RESOLVED"]
    R.append(_res("open_cases_per_loan<=1", "STRUCTURAL", open_cases["loan_id"].duplicated().sum()))

    # --- 5. date ordering --------------------------------------------------------------------
    loan, inst, pay, alloc, case, acct = t["loan"], t["installment"], t["payment"], t["payment_allocation"], t["collection_case"], t["account"]
    R.append(_res("dates:loan first_due>origination, maturity>=first_due", "STRUCTURAL",
                  ((loan["first_due_date"] <= loan["origination_date"]) | (loan["maturity_date"] < loan["first_due_date"])).sum()))
    la = loan.merge(acct[["account_id", "opened_date", "customer_id"]].rename(columns={"customer_id": "acct_customer"}), on="account_id")
    R.append(_res("dates:loan origination >= account opened", "STRUCTURAL", (la["origination_date"] < la["opened_date"]).sum()))
    R.append(_res("customer_consistency:loan.customer == account.customer", "STRUCTURAL", (la["customer_id"] != la["acct_customer"]).sum()))
    pl = pay.merge(loan[["loan_id", "origination_date"]], on="loan_id")
    R.append(_res("dates:payment after origination and <= reference date", "STRUCTURAL",
                  ((pl["payment_date"] <= pl["origination_date"]) | (pl["payment_date"] > ref.isoformat())).sum()))
    ia = alloc.merge(pay[["payment_id", "payment_date"]], on="payment_id")
    R.append(_res("dates:allocation_date == payment_date", "STRUCTURAL", (ia["allocation_date"] != ia["payment_date"]).sum()))
    R.append(_res("dates:case closed>=opened", "STRUCTURAL", (case["closed_date"].notna() & (case["closed_date"] < case["opened_date"])).sum()))
    R.append(_res("dates:account closed>=opened", "STRUCTURAL", (acct["closed_date"].notna() & (acct["closed_date"] < acct["opened_date"])).sum()))
    pr = t["promise_to_pay"]
    R.append(_res("dates:promise promised_date>=created_date", "STRUCTURAL", (pr["promised_date"] < pr["created_date"]).sum()))
    ca = t["contact_attempt"][t["contact_attempt"]["attempt_status"] != "CANCELLED"].merge(case[["case_id", "opened_date", "closed_date"]], on="case_id")
    R.append(_res("dates:attempt within case life", "STRUCTURAL",
                  ((ca["scheduled_for"].str[:10] <= ca["opened_date"]) | (ca["closed_date"].notna() & (ca["scheduled_for"].str[:10] >= ca["closed_date"].fillna("9999")))).sum()))
    R.append(_res("dates:preference effective_to>=effective_from", "STRUCTURAL",
                  (t["contact_preference"]["effective_to"].notna() & (t["contact_preference"]["effective_to"] < t["contact_preference"]["effective_from"])).sum()))
    pf = t["contact_preference"]
    R.append(_res("preferences: exactly one ACTIVE per customer", "STRUCTURAL",
                  (pf[pf["status"] == "ACTIVE"].groupby("customer_id").size() != 1).sum() + len(set(t["customer"]["customer_id"]) - set(pf["customer_id"]))))
    ids_ok = ca.assign(expect=ca["case_id"].map(lambda x: x))  # attempt id prefix/suffix consistency
    bad = t["contact_attempt"][t["contact_attempt"].apply(lambda r: r["attempt_id"] != f"CTP-{r['case_id']}-{int(r['sequence_number']):03d}", axis=1)]
    R.append(_res("ids:attempt_id == CTP-<case_id>-<sequence>", "STRUCTURAL", len(bad)))
    bad = inst[inst["installment_id"] != inst["loan_id"].radd("INS-") + "-" + inst["installment_number"].astype(int).astype(str).str.zfill(3)]
    R.append(_res("ids:installment_id == INS-<loan_id>-<nnn>", "STRUCTURAL", len(bad)))

    # --- 6. arithmetic reconciliation ----------------------------------------------------------
    s_inst = inst.assign(m=(inst["amount_due"] * 100).round().astype("int64")).groupby("loan_id")["m"].sum()
    tp = (loan.set_index("loan_id")["total_payable"] * 100).round().astype("int64")
    R.append(_res("arith:loan.total_payable == SUM(installment.amount_due)", "ARITHMETIC", (s_inst.reindex(tp.index) != tp).sum()))
    R.append(_res("arith:installment principal+interest == amount_due", "ARITHMETIC",
                  (((inst["principal_component"] + inst["interest_component"] - inst["amount_due"]).abs()) > 0.005).sum()))
    cnt = inst.groupby("loan_id").size().reindex(loan["loan_id"])
    R.append(_res("arith:installment count == tenor", "ARITHMETIC", (cnt.values != loan["tenor_installments"].values).sum()))
    a_inst = alloc.assign(m=(alloc["allocated_amount"] * 100).round().astype("int64")).groupby("installment_id")["m"].sum()
    paid = (inst.set_index("installment_id")["amount_paid"] * 100).round().astype("int64")
    R.append(_res("arith:installment.amount_paid == SUM(allocations)", "ARITHMETIC", (a_inst.reindex(paid.index).fillna(0).astype("int64") != paid).sum()))
    amt = (inst.set_index("installment_id")["amount_due"] * 100).round().astype("int64")
    R.append(_res("arith:allocations never exceed instalment amount_due", "ARITHMETIC", (a_inst > amt.reindex(a_inst.index)).sum()))
    a_pay = alloc.assign(m=(alloc["allocated_amount"] * 100).round().astype("int64")).groupby("payment_id")["m"].sum()
    pam = (pay.set_index("payment_id")["amount"] * 100).round().astype("int64")
    R.append(_res("payment_allocation: SUM(allocations) == payment.amount (fully allocated)", "ARITHMETIC", (a_pay.reindex(pam.index).fillna(0).astype("int64") != pam).sum()))
    pl2 = alloc.merge(pay[["payment_id", "loan_id"]], on="payment_id").merge(inst[["installment_id", "loan_id"]], on="installment_id", suffixes=("_pay", "_inst"))
    R.append(_res("payment_allocation: allocation stays within the payment's loan", "ARITHMETIC", (pl2["loan_id_pay"] != pl2["loan_id_inst"]).sum()))
    snap_ref = snapshot_frame(loan, inst, alloc, ref, rules["case_policy"]["default_dpd"]).set_index("loan_id")
    stored = t["delinquency_snapshot"][t["delinquency_snapshot"]["snapshot_date"] == ref.isoformat()].set_index("loan_id")
    cols = ["total_amount_paid", "outstanding_balance", "total_arrears", "overdue_installments", "days_past_due", "loan_status"]
    # Snapshots are immutable history: loans with a payment POSTED after the snapshot was taken are stale by design
    # (live /loans/{id}/delinquency stays correct; the next batch snapshot captures the change).
    stale = set(pay.loc[pay["created_at"] > ref.isoformat() + "T23:59:59", "loan_id"])
    j = stored[cols].join(snap_ref[cols], rsuffix="_calc", how="outer")
    j = j.drop(index=[x for x in stale if x in j.index])
    mism = 0
    for c in cols[:3]:
        mism += ((j[c] - j[c + "_calc"]).abs() > 0.005).sum()
    for c in cols[3:]:
        mism += (j[c] != j[c + "_calc"]).sum()
    R.append(_res("arith:reference-date snapshot == recomputation from source", "ARITHMETIC", mism + j.isna().any(axis=1).sum()))
    ls = loan.set_index("loan_id")["loan_status"]
    R.append(_res("arith:loan.loan_status == derived status", "ARITHMETIC", (ls != snap_ref["loan_status"].reindex(ls.index)).sum()))
    # arrears reconciles with installments (independent recomputation from instalment table)
    due_i = inst[(inst["due_date"] < ref.isoformat())]
    arr_i = ((due_i["amount_due"] - due_i["amount_paid"]).clip(lower=0) * 100).round().astype("int64").groupby(due_i["loan_id"]).sum()
    arr_s = (snap_ref["total_arrears"] * 100).round().astype("int64")
    R.append(_res("arith:total_arrears == SUM(instalment amount_due - amount_paid) for overdue instalments", "ARITHMETIC",
                  (arr_s - arr_i.reindex(arr_s.index).fillna(0).astype("int64")).abs().gt(1).sum()))

    # --- 7. conditional nullability / lifecycle ---------------------------------------------------
    cond = (
        ((loan["loan_status"] == "PAID_OFF") != loan["payoff_date"].notna()).sum()
        + ((inst["installment_status"] == "PAID") != inst["fully_paid_date"].notna()).sum()
        + ((case["case_status"] == "RESOLVED") != case["closed_date"].notna()).sum()
        + ((acct["account_status"] == "CLOSED") != acct["closed_date"].notna()).sum()
        + ((t["contact_attempt"]["attempt_status"] == "COMPLETED") != t["contact_attempt"]["outcome"].notna()).sum()
        + ((pr["promise_status"] == "PENDING") != pr["evaluated_on"].isna()).sum())
    R.append(_res("lifecycle:status <-> date-field consistency (payoff/closed/fully_paid/outcome/evaluated_on)", "LIFECYCLE", cond))
    sn = t["delinquency_snapshot"].sort_values(["loan_id", "snapshot_date"])
    sn = sn.assign(prev=sn.groupby("loan_id")["loan_status"].shift())
    bad = sn[sn["prev"].notna() & ~sn.apply(lambda r: r["loan_status"] in LEGAL_LOAN_TRANSITIONS.get(r["prev"], set()), axis=1)] if len(sn) else sn
    R.append(_res("lifecycle:loan status transitions across snapshots are legal (PAID_OFF terminal)", "LIFECYCLE", len(bad), detail=_sample(bad["snapshot_id"]) if len(bad) else ""))
    au = t["audit_event"]
    au_case = au[(au["entity_type"] == "CASE") & (au["action"] == "STATUS_CHANGED")]
    bad = au_case[au_case["old_value"] == "RESOLVED"]
    R.append(_res("lifecycle:no case transition out of RESOLVED (audit trail)", "LIFECYCLE", len(bad)))
    R.append(_res("lifecycle:every case has a CASE_OPENED audit event", "LIFECYCLE",
                  len(set(case["case_id"]) - set(au[au["action"] == "CASE_OPENED"]["entity_id"]))))

    # --- 8. promise reconciliation -----------------------------------------------------------------
    ledger = Ledger(loan, inst, alloc, rules["case_policy"]["default_dpd"])
    grace = rules["case_policy"]["promise_grace_days"]
    mism = arrears_mism = 0
    bad_ids = []
    same_day_late = alloc.merge(pay[["payment_id", "loan_id"]], on="payment_id")[["loan_id", "allocation_date", "created_at"]]
    for p in pr.itertuples(index=False):
        st, recv, ev = evaluate_promise(ledger, p.loan_id, date.fromisoformat(p.created_date), date.fromisoformat(p.promised_date),
                                        to_minor(p.promised_amount), ref, grace)
        if st != p.promise_status or abs(recv / 100 - p.amount_received) > 0.005:
            mism += 1
            bad_ids.append(p.promise_id)
        if abs(ledger.state(p.loan_id, date.fromisoformat(p.created_date)).arrears / 100 - p.arrears_at_creation) > 0.005:
            late = same_day_late[(same_day_late["loan_id"] == p.loan_id) & (same_day_late["allocation_date"] == p.created_date)
                                 & (same_day_late["created_at"] >= p.created_at)]
            if late.empty:  # not explained by a same-day payment posted after the promise
                arrears_mism += 1
    R.append(_res("promise_reconciliation:status/amount_received == recomputed from payments", "BUSINESS_RULE", mism, detail=_sample(bad_ids)))
    R.append(_res("promise_reconciliation:arrears_at_creation == recomputed arrears", "BUSINESS_RULE", arrears_mism))

    # --- 9. business-rule exceptions (same detectors as the platform) ----------------------------------
    det = detect_all(t, ref, rules)
    by = {}
    for d in det:
        by.setdefault(d["exception_type"], []).append(d["entity_id"])
    for kind in ISSUE_TYPES:
        R.append(_res(f"exception:{kind}", "BUSINESS_RULE_EXCEPTION", len(by.get(kind, [])), "HIGH", _sample(by.get(kind, []))))
    return R


def summarize(results: list[dict]) -> dict:
    structural = [r for r in results if r["category"] in ("STRUCTURAL", "ARITHMETIC", "LIFECYCLE", "BUSINESS_RULE")]
    exc = {r["check_name"].split(":", 1)[1]: r["violation_count"] for r in results if r["category"] == "BUSINESS_RULE_EXCEPTION"}
    return {"checks_total": len(structural), "checks_passed": sum(r["passed"] for r in structural),
            "checks_failed": sum(1 - r["passed"] for r in structural), "exceptions_detected": exc,
            "all_integrity_checks_passed": all(r["passed"] for r in structural)}
