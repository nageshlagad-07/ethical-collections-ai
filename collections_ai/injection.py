"""Controlled injection of the five mandatory negative scenarios.

Runs AFTER the valid baseline is generated. Each injector changes only the rows
needed for the scenario, keeps every foreign key valid, and writes a labelled
row to the separate ground-truth frame. Nothing here is imported by the
detection / prioritization code (no label leakage).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from . import ids
from .config import GenConfig
from .contact import ContactContext, ContactRequest, DAY_CODES
from .delinquency import Ledger, from_minor, to_minor
from .rules import load_rules
from .reconciliation import evaluate_promise
from .generator import records


def inject_anomalies(cfg: GenConfig, ds, rng: np.random.Generator) -> None:
    from .generator import refresh_derived, assign_audit_ids, ts

    t, ref, n = ds.tables, cfg.reference_date, cfg.n_anomalies
    rules = load_rules()
    grace = rules["case_policy"]["promise_grace_days"]
    ledger = Ledger(t["loan"], t["installment"], t["payment_allocation"], rules["case_policy"]["default_dpd"])
    ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], t["flag_event"], t["contact_attempt"], ledger, rules)
    audit = [{k: v for k, v in r.items() if k != "audit_id"} for r in records(t["audit_event"])]
    gt: list[dict] = []
    ref_ts = ts(ref, "23:59:59")

    def add_gt(issue, etype, eid, case=None, loan=None, cust=None, truth="", expected="", detail=None):
        gt.append({"ground_truth_id": f"GT-{len(gt) + 1:04d}", "issue_type": issue, "entity_type": etype, "entity_id": eid,
                   "case_id": case, "loan_id": loan, "customer_id": cust, "ground_truth": truth, "expected_decision": expected,
                   "injection_detail": json.dumps(detail or {}, sort_keys=True)})

    cases = t["collection_case"]
    case_rows = records(cases)
    next_case = len(case_rows) + 1

    # ---- 1. paid account remains in collection ---------------------------------
    open_loans = set(cases.loc[cases["closed_date"].isna(), "loan_id"])
    cand = t["loan"][(t["loan"]["loan_status"] == "PAID_OFF") & (~t["loan"]["loan_id"].isin(open_loans))
                     & (t["loan"]["payoff_date"] <= (ref - timedelta(days=10)).isoformat())]
    cand = cand.sort_values("loan_id").reset_index(drop=True)
    pick = rng.permutation(len(cand))[:n]
    new_cases = []
    for i in pick:
        L = cand.iloc[i]
        opened = date.fromisoformat(L["payoff_date"]) + timedelta(days=int(rng.integers(1, 8)))
        cid = ids.case_id(next_case)
        next_case += 1
        row = {"case_id": cid, "loan_id": L["loan_id"], "customer_id": L["customer_id"], "opened_date": opened.isoformat(),
               "closed_date": None, "case_status": "OPEN", "debt_confirmation_status": "UNVERIFIED", "opening_dpd": 0,
               "opening_arrears": 0.0, "assigned_analyst": f"ANL-{int(rng.integers(1, 13)):02d}", "created_at": ts(opened), "updated_at": ref_ts}
        new_cases.append(row)
        audit.append({"entity_type": "CASE", "entity_id": cid, "action": "CASE_OPENED", "old_value": None, "new_value": "OPEN", "actor": "ANL-BATCH",
                      "reason": "Case opened", "event_ts": ts(opened, "09:00:00"), "details": None})
        add_gt("PAID_ACCOUNT_IN_COLLECTION", "CASE", cid, cid, L["loan_id"], L["customer_id"],
               f"Loan {L['loan_id']} was fully paid on {L['payoff_date']} but case {cid} was opened afterwards and remains open.",
               "Suppress all collection actions, flag exception, close case after compliance review.",
               {"payoff_date": L["payoff_date"], "case_opened": opened.isoformat()})
    case_rows += new_cases

    # ---- 2. contact outside allowed window ---------------------------------------
    att = t["contact_attempt"].copy()
    att_rows = records(att)
    last_by_case = att.groupby("case_id")["scheduled_for"].max().to_dict()
    seq_by_case = att.groupby("case_id")["sequence_number"].max().to_dict()
    base_cases = [c for c in case_rows if c["case_id"] in last_by_case]
    order = rng.permutation(len(base_cases))
    made = 0
    for oi in order:
        if made >= n:
            break
        c = base_cases[oi]
        last = datetime.fromisoformat(last_by_case[c["case_id"]]).date()
        hi = min(date.fromisoformat(c["closed_date"]) - timedelta(days=1) if c["closed_date"] else ref, ref)
        if last + timedelta(days=1) > hi:
            continue
        want_day_violation = made % 2 == 1
        for _ in range(10):
            day = last + timedelta(days=int(rng.integers(1, (hi - last).days + 1)))
            if want_day_violation:
                if day.weekday() != 6:
                    continue
                tm = time(int(rng.integers(10, 16)), int(rng.integers(0, 60)), int(rng.integers(0, 60)))
            else:
                hr = int(rng.choice([21, 22, 23, 5, 6]))
                tm = time(hr, int(rng.integers(0, 60)), int(rng.integers(0, 60)))
            when = datetime.combine(day, tm)
            pref = ctx.preference_on(c["customer_id"], day)
            if pref is None:
                continue
            chans = [ch for ch in ("PHONE", "SMS") if int(pref[f"allow_{ch.lower()}"])]
            if not chans:
                continue
            ch = chans[int(rng.integers(0, len(chans)))]
            dec = ctx.evaluate(ContactRequest(c["case_id"], c["customer_id"], c["loan_id"], ch, when, "COLLECTION_REMINDER"))
            expected_code = "DAY_NOT_PERMITTED" if want_day_violation else "OUTSIDE_TIME_WINDOW"
            if dec.reason_codes != [expected_code]:
                continue
            if any(when < x <= when + timedelta(days=7) for x in ctx.attempt_times.get(c["customer_id"], [])):
                continue
            seq = seq_by_case[c["case_id"]] + 1
            seq_by_case[c["case_id"]] = seq
            aid = ids.attempt_id(c["case_id"], seq)
            ctx.add_attempt(c["customer_id"], when)
            att_rows.append({"attempt_id": aid, "case_id": c["case_id"], "customer_id": c["customer_id"], "sequence_number": seq, "channel": ch,
                             "purpose": "COLLECTION_REMINDER", "scheduled_for": when.isoformat(timespec="seconds"), "attempt_status": "COMPLETED",
                             "outcome": "NO_ANSWER" if ch == "PHONE" else "MESSAGE_SENT", "created_at": when.isoformat(timespec="seconds"),
                             "updated_at": when.isoformat(timespec="seconds")})
            add_gt("CONTACT_OUTSIDE_ALLOWED_WINDOW", "CONTACT_ATTEMPT", aid, c["case_id"], c["loan_id"], c["customer_id"],
                   f"Attempt {aid} ({ch}) at {when.isoformat(timespec='seconds')} violates " + ("permitted contact days." if want_day_violation else "the permitted time window."),
                   "Eligibility engine must REJECT this request; the completed attempt is a compliance exception for review.",
                   {"violated_rule": expected_code, "preference_id": pref["preference_id"]})
            made += 1
            break

    # ---- 3. payment allocated to wrong installment ---------------------------------
    alloc = t["payment_allocation"].copy()
    inst = t["installment"]
    inst_num = dict(zip(inst["installment_id"], inst["installment_number"]))
    inst_amt = dict(zip(inst["installment_id"], inst["amount_due"]))
    inst_alloc_ct = alloc.groupby("installment_id").size().to_dict()
    pay_alloc_ct = alloc.groupby("payment_id").size().to_dict()
    case_loans = {c["loan_id"] for c in case_rows}
    pay_loan = dict(zip(t["payment"]["payment_id"], t["payment"]["loan_id"]))
    cands = []
    for i, r in enumerate(alloc.itertuples(index=False)):
        if inst_num[r.installment_id] != 1 or pay_alloc_ct[r.payment_id] != 1:
            continue
        loan = pay_loan[r.payment_id]
        if loan in case_loans:
            continue
        tgt = ids.installment_id(loan, 2)
        if inst_alloc_ct.get(tgt, 0) or abs(r.allocated_amount - inst_amt[r.installment_id]) > 0.005 or r.allocated_amount > inst_amt[tgt] + 0.005:
            continue
        cands.append((i, r.payment_id, loan, r.installment_id, tgt))
    for pi in rng.permutation(len(cands))[:n]:
        i, pid, loan, old, tgt = cands[pi]
        alloc.iat[i, alloc.columns.get_loc("installment_id")] = tgt
        alloc.iat[i, alloc.columns.get_loc("updated_at")] = ref_ts
        cust = t["loan"].loc[t["loan"]["loan_id"] == loan, "customer_id"].iloc[0]
        add_gt("PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT", "PAYMENT", pid, None, loan, cust,
               f"Payment {pid} was allocated to {tgt} while older instalment {old} remained unpaid (policy: oldest due first).",
               f"Reconciliation must mark {pid} as EXCEPTION and propose re-allocation to {old}.", {"correct_installment": old, "recorded_installment": tgt})
    t["payment_allocation"] = alloc

    # ---- 4. promise amount exceeds arrears ------------------------------------------
    promises = records(t["promise_to_pay"])
    next_promise = len(promises) + 1
    by_case_p: dict[str, list] = {}
    for p in promises:
        by_case_p.setdefault(p["case_id"], []).append(p)
    base_open = [c for c in case_rows if c["closed_date"] is None and c["case_status"] == "OPEN" and c["case_id"] not in {x["case_id"] for x in new_cases}]
    made = 0
    for oi in rng.permutation(len(base_open)):
        if made >= n:
            break
        c = base_open[oi]
        created = ref - timedelta(days=int(rng.integers(0, 5)))
        if created <= date.fromisoformat(c["opened_date"]):
            continue
        if any(date.fromisoformat(p["promised_date"]) + timedelta(days=grace) >= created for p in by_case_p.get(c["case_id"], [])):
            continue
        arrears = ledger.state(c["loan_id"], created).arrears
        if arrears <= 0:
            continue
        amount = int(arrears * float(rng.uniform(1.3, 2.0)))
        pdate = ref + timedelta(days=int(rng.integers(3, 11)))
        pid = ids.promise_id(next_promise)
        next_promise += 1
        created_dt = datetime.combine(created, time(11, int(rng.integers(0, 60)), 0))
        promises.append({"promise_id": pid, "case_id": c["case_id"], "loan_id": c["loan_id"], "customer_id": c["customer_id"], "attempt_id": None,
                         "created_date": created.isoformat(), "promised_amount": from_minor(amount), "promised_date": pdate.isoformat(),
                         "arrears_at_creation": from_minor(arrears), "promise_status": "PENDING", "amount_received": 0.0, "evaluated_on": None,
                         "exception_approved_by": None, "exception_reason": None, "created_at": created_dt.isoformat(timespec="seconds"),
                         "updated_at": created_dt.isoformat(timespec="seconds")})
        c["case_status"], c["updated_at"] = "PROMISE_PENDING", ref_ts
        audit.append({"entity_type": "PROMISE", "entity_id": pid, "action": "PROMISE_CREATED", "old_value": None, "new_value": "PENDING", "actor": "ANL-BATCH",
                      "reason": "Promise recorded", "event_ts": created_dt.isoformat(timespec="seconds"), "details": None})
        audit.append({"entity_type": "CASE", "entity_id": c["case_id"], "action": "STATUS_CHANGED", "old_value": "OPEN", "new_value": "PROMISE_PENDING",
                      "actor": "ANL-BATCH", "reason": "Promise created", "event_ts": created_dt.isoformat(timespec="seconds"), "details": None})
        add_gt("PROMISE_AMOUNT_EXCEEDS_ARREARS", "PROMISE", pid, c["case_id"], c["loan_id"], c["customer_id"],
               f"Promise {pid} for {from_minor(amount):.2f} exceeds arrears {from_minor(arrears):.2f} at creation and has no approved exception.",
               "Promise validation must reject (or require documented exception approval) because promised amount > arrears.",
               {"arrears_at_creation": from_minor(arrears), "promised_amount": from_minor(amount)})
        made += 1

    # ---- 5. disputed debt treated as confirmed ---------------------------------------
    disp = [c for c in case_rows if c["case_status"] == "DISPUTE_REVIEW" and c["debt_confirmation_status"] == "DISPUTED"]
    for oi in rng.permutation(len(disp))[:n]:
        c = disp[oi]
        c["case_status"], c["debt_confirmation_status"], c["updated_at"] = "OPEN", "CONFIRMED", ref_ts
        audit.append({"entity_type": "CASE", "entity_id": c["case_id"], "action": "DEBT_STATUS_CHANGED", "old_value": "DISPUTED", "new_value": "CONFIRMED",
                      "actor": "ANL-BATCH", "reason": "Status updated", "event_ts": ts(ref, "09:30:00"), "details": None})
        add_gt("DISPUTED_DEBT_TREATED_AS_CONFIRMED", "CASE", c["case_id"], c["case_id"], c["loan_id"], c["customer_id"],
               f"Case {c['case_id']} has an active dispute on {c['loan_id']} but is recorded as CONFIRMED debt and OPEN for collection.",
               "Restore DISPUTED status, suppress collection contact, follow dispute validation workflow.",
               {"original_status": "DISPUTE_REVIEW", "original_debt_status": "DISPUTED"})

    # ---- write back + recompute everything derived ----------------------------------
    from .generator import frame
    t["collection_case"] = frame("collection_case", sorted(case_rows, key=lambda c: c["case_id"]))
    att_rows.sort(key=lambda a: (a["case_id"], a["sequence_number"]))
    t["contact_attempt"] = frame("contact_attempt", att_rows)
    refresh_derived(cfg, t)
    final_ledger = Ledger(t["loan"], t["installment"], t["payment_allocation"], rules["case_policy"]["default_dpd"])
    for p in promises:
        st, recv, ev = evaluate_promise(final_ledger, p["loan_id"], date.fromisoformat(p["created_date"]), date.fromisoformat(p["promised_date"]),
                                        to_minor(p["promised_amount"]), ref, grace)
        p["promise_status"], p["amount_received"], p["evaluated_on"] = st, from_minor(recv), ev.isoformat() if ev else None
    promises.sort(key=lambda p: p["promise_id"])
    t["promise_to_pay"] = frame("promise_to_pay", promises)
    t["audit_event"] = assign_audit_ids(audit)
    ds.ground_truth = pd.DataFrame(gt, columns=ds.ground_truth.columns)
