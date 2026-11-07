"""Deterministic, parameterised synthetic data generator.

Generation order (identical logic for the 300-loan dev set and the 10,000-loan full set):
  1 customers -> 2 accounts -> 3 loans -> 4 installments -> 5 payments + FIFO allocations
  -> 6 delinquency snapshots -> 7 contact preferences -> 8 collection cases
  -> 9 contact attempts (+ scheduling log, hardship/dispute flag events)
  -> 10 promises to pay -> baseline audit trail -> (optional) labelled anomaly injection.

Everything derived (instalment status, loan status, arrears, DPD, case status,
promise outcome, reconciliation status) is computed from the source records by
the same engines used at run time (delinquency.py, contact.py, reconciliation.py).
Ground-truth labels are produced ONLY by the injection step and stored separately.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from faker import Faker

from . import ids
from .config import GenConfig
from .contact import (ContactContext, ContactRequest, effective_days, effective_window, DAY_CODES)
from .delinquency import (Ledger, derive_installments, derive_loans, fifo_allocate, from_minor,
                          snapshot_dates, snapshot_frame, to_minor)
from .reconciliation import evaluate_promise, reconcile_payments
from .rules import load_rules
from .spec import TABLE_BY_NAME, DATASET_TABLES, CHANNELS

PATTERNS = ["ON_TIME", "SLIGHTLY_LATE", "EARLY_PAYOFF", "PARTIAL", "MISSED_CURE", "DEFAULT"]
PATTERN_PROBS = {
    "LOW": [0.70, 0.14, 0.08, 0.03, 0.03, 0.02],
    "MEDIUM": [0.48, 0.18, 0.04, 0.11, 0.10, 0.09],
    "HIGH": [0.26, 0.15, 0.01, 0.17, 0.16, 0.25],
}


@dataclass
class Dataset:
    tables: dict
    ground_truth: pd.DataFrame
    config: dict
    stats: dict = field(default_factory=dict)


def empty(table: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TABLE_BY_NAME[table].columns})


def frame(table: str, rows: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=TABLE_BY_NAME[table].columns) if rows else empty(table)


def ts(d: date | str, hhmmss: str = "08:00:00") -> str:
    return f"{str(d)[:10]}T{hhmmss}"


def records(df: pd.DataFrame) -> list:
    """DataFrame -> list of dicts with NaN/NaT normalised to None."""
    return [{k: (None if (v is not None and not isinstance(v, (str, list, dict)) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.to_dict("records")]


def risk_from_score(score: int) -> str:
    return "LOW" if score >= 700 else ("MEDIUM" if score >= 600 else "HIGH")


# ===========================================================================
# 1-3  customers, accounts, loans
# ===========================================================================
def gen_customers(cfg: GenConfig, rng: np.random.Generator) -> pd.DataFrame:
    n = max(2, round(cfg.n_loans * cfg.customers_per_loan))
    fake = Faker("en_IN")
    fake.seed_instance(cfg.seed)
    scores = np.clip(rng.normal(655, 85, n), 300, 900).astype(int)
    profiles = rng.choice(["POLICY_A", "POLICY_B", "POLICY_C"], size=n, p=[0.6, 0.3, 0.1])
    since_off = rng.integers(200, 3000, n)
    ages = rng.integers(21, 71, n)
    dob_off = rng.integers(0, 365, n)
    rows = []
    for i in range(n):
        cid = ids.customer_id(i + 1)
        since = cfg.reference_date - timedelta(days=int(since_off[i]))
        dob = cfg.reference_date - timedelta(days=int(ages[i]) * 365 + int(dob_off[i]))
        rows.append({
            "customer_id": cid, "full_name": f"{fake.first_name()} {fake.last_name()}",
            "date_of_birth": dob.isoformat(), "email": f"cus{i + 1:06d}@example.invalid",
            "phone": f"+000-{i + 1:010d}", "jurisdiction_profile": str(profiles[i]),
            "credit_score": int(scores[i]), "risk_category": risk_from_score(int(scores[i])),
            "customer_since": since.isoformat(), "created_at": ts(since), "updated_at": ts(since),
        })
    return frame("customer", rows)


def gen_accounts(cfg: GenConfig, rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    n_cust = len(customers)
    n_acc = min(cfg.n_loans, n_cust + int(n_cust * cfg.extra_account_share))
    owners = list(range(n_cust)) + list(rng.integers(0, n_cust, n_acc - n_cust))
    since = pd.to_datetime(customers["customer_since"]).dt.date.tolist()
    types = rng.choice(["SALARY_LINKED", "SAVINGS_LINKED", "CURRENT_LINKED"], n_acc, p=[0.45, 0.4, 0.15])
    methods = rng.choice(["AUTO_DEBIT", "MANUAL_TRANSFER"], n_acc, p=[0.55, 0.45])
    open_off = rng.integers(0, 120, n_acc)
    suspended = rng.random(n_acc) < 0.008
    rows = []
    for i in range(n_acc):
        c = owners[i]
        opened = min(since[c] + timedelta(days=int(open_off[i])), cfg.last_origination - timedelta(days=5))
        rows.append({
            "account_id": ids.account_id(i + 1), "customer_id": ids.customer_id(c + 1),
            "account_type": str(types[i]), "repayment_method": str(methods[i]),
            "account_status": "SUSPENDED" if suspended[i] else "ACTIVE",
            "opened_date": opened.isoformat(), "closed_date": None, "currency": "INR",
            "created_at": ts(opened), "updated_at": ts(opened),
        })
    return frame("account", rows)


def gen_loans_installments(cfg: GenConfig, rng: np.random.Generator, customers: pd.DataFrame,
                           accounts: pd.DataFrame):
    n, n_acc = cfg.n_loans, len(accounts)
    assign = np.concatenate([rng.permutation(n_acc), rng.integers(0, n_acc, n - n_acc)])
    assign = rng.permutation(assign)
    acc_opened = pd.to_datetime(accounts["opened_date"]).dt.date.tolist()
    acc_cust = accounts["customer_id"].tolist()
    risk_of = dict(zip(customers["customer_id"], customers["risk_category"]))
    orig = []
    u = rng.random(n)
    for k in range(n):
        lo = max(cfg.window_start, acc_opened[assign[k]])
        hi = cfg.last_origination
        lo = min(lo, hi)
        orig.append(lo + timedelta(days=int(u[k] * ((hi - lo).days + 1))))
    order = sorted(range(n), key=lambda k: (orig[k], int(assign[k]), k))
    principal = np.clip(np.exp(rng.normal(np.log(60000), 0.75, n)), 10000, 500000)
    principal = (np.round(principal / 500) * 500).astype(int)
    products = rng.choice(["PERSONAL_LOAN", "CONSUMER_DURABLE", "SMALL_BUSINESS", "EDUCATION"], n, p=[0.4, 0.25, 0.25, 0.1])
    rate_u = rng.random(n)
    loan_rows, inst_rows = [], []
    T = cfg.installments_per_loan
    for new_idx, k in enumerate(order):
        lid = ids.loan_id(new_idx + 1)
        acc = accounts.iloc[assign[k]]
        cust = acc_cust[assign[k]]
        risk = risk_of[cust]
        lo_r, hi_r = {"LOW": (10, 14), "MEDIUM": (14, 19), "HIGH": (19, 26)}[risk]
        rate = round((lo_r + (hi_r - lo_r) * rate_u[k]) * 2) / 2
        p_minor = int(principal[k]) * 100
        interest_minor = int(round(p_minor * rate / 100 * T / 12))
        total_minor = p_minor + interest_minor
        base, pbase = total_minor // T, p_minor // T
        amts = [base] * (T - 1) + [total_minor - base * (T - 1)]
        pcomp = [pbase] * (T - 1) + [p_minor - pbase * (T - 1)]
        o = orig[k]
        dues = [o + timedelta(days=30 * (j + 1)) for j in range(T)]
        loan_rows.append({
            "loan_id": lid, "account_id": acc["account_id"], "customer_id": cust,
            "product_type": str(products[k]), "principal_amount": from_minor(p_minor), "interest_rate_pct": rate,
            "tenor_installments": T, "total_payable": from_minor(total_minor), "origination_date": o.isoformat(),
            "first_due_date": dues[0].isoformat(), "maturity_date": dues[-1].isoformat(),
            "loan_status": "CURRENT", "status_as_of": cfg.reference_date.isoformat(), "payoff_date": None,
            "created_at": ts(o), "updated_at": ts(o),
        })
        for j in range(T):
            inst_rows.append({
                "installment_id": ids.installment_id(lid, j + 1), "loan_id": lid, "installment_number": j + 1,
                "due_date": dues[j].isoformat(), "amount_due": from_minor(amts[j]),
                "principal_component": from_minor(pcomp[j]), "interest_component": from_minor(amts[j] - pcomp[j]),
                "amount_paid": 0.0, "installment_status": "NOT_DUE", "fully_paid_date": None,
                "created_at": ts(o), "updated_at": ts(o),
            })
    return frame("loan", loan_rows), frame("installment", inst_rows)


# ===========================================================================
# 5  payments and FIFO allocations
# ===========================================================================
def _simulate_loan_payments(rng, pattern: str, dues: list, amts: list, orig: date, ref: date) -> list:
    ev: list[tuple[date, int]] = []
    K = len(dues)
    if pattern == "ON_TIME":
        ev = [(dues[k] + timedelta(days=int(rng.integers(-3, 3))), amts[k]) for k in range(K)]
    elif pattern == "SLIGHTLY_LATE":
        ev = [(dues[k] + timedelta(days=int(rng.integers(1, 13))), amts[k]) for k in range(K)]
    elif pattern == "EARLY_PAYOFF":
        ev = [(dues[0] + timedelta(days=int(rng.integers(-3, 3))), amts[0])]
        span = max(1, (dues[1] - dues[0]).days - 4)
        ev.append((dues[0] + timedelta(days=1 + int(rng.integers(0, span))), sum(amts[1:])))
    elif pattern == "PARTIAL":
        for k in range(K):
            f = float(rng.uniform(0.3, 0.8))
            first = max(1, int(amts[k] * f))
            d1 = dues[k] + timedelta(days=int(rng.integers(0, 16)))
            ev.append((d1, first))
            if rng.random() < 0.35:
                ev.append((d1 + timedelta(days=int(rng.integers(8, 40))), amts[k] - first))
    elif pattern == "MISSED_CURE":
        m = int(rng.integers(0, K))
        for k in range(K):
            if k != m:
                ev.append((dues[k] + timedelta(days=int(rng.integers(-3, 3))), amts[k]))
        ev.append((dues[m] + timedelta(days=int(rng.integers(15, 70))), amts[m]))
    elif pattern == "DEFAULT":
        j = int(rng.choice([0, 1, 2], p=[0.4, 0.4, 0.2]))
        for k in range(min(j, K)):
            ev.append((dues[k] + timedelta(days=int(rng.integers(-2, 8))), amts[k]))
    return sorted((d, a) for d, a in ev if orig + timedelta(days=1) <= d <= ref and a > 0)


def gen_payments(cfg: GenConfig, rng: np.random.Generator, customers, accounts, loans, installments):
    risk_of = dict(zip(customers["customer_id"], customers["risk_category"]))
    method_of = dict(zip(accounts["account_id"], accounts["repayment_method"]))
    inst_by_loan = {lid: g.sort_values("installment_number") for lid, g in installments.groupby("loan_id")}
    events = []
    for r in loans.itertuples(index=False):
        g = inst_by_loan[r.loan_id]
        dues = [date.fromisoformat(x) for x in g["due_date"]]
        amts = [to_minor(x) for x in g["amount_due"]]
        pattern = str(rng.choice(PATTERNS, p=PATTERN_PROBS[risk_of[r.customer_id]]))
        for seq, (d, a) in enumerate(_simulate_loan_payments(rng, pattern, dues, amts, date.fromisoformat(r.origination_date), cfg.reference_date)):
            auto = method_of[r.account_id] == "AUTO_DEBIT" and rng.random() < 0.85
            chan = "AUTO_DEBIT" if auto else str(rng.choice(["BANK_TRANSFER", "UPI", "CARD", "CASH_DEPOSIT"], p=[0.3, 0.45, 0.15, 0.1]))
            events.append((d, r.loan_id, seq, a, r.customer_id, chan))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    pay_rows, alloc_rows = [], []
    remaining: dict[str, list] = {}
    for lid, g in inst_by_loan.items():
        remaining[lid] = [(i, to_minor(a)) for i, a in zip(g["installment_id"], g["amount_due"])]
    for n, (d, lid, _, a, cust, chan) in enumerate(events, start=1):
        pid = ids.payment_id(n)
        pay_rows.append({
            "payment_id": pid, "loan_id": lid, "customer_id": cust, "payment_date": d.isoformat(),
            "amount": from_minor(a), "channel": chan, "reference": f"UTR{cfg.seed}{n:08d}",
            "reconciliation_status": "UNRECONCILED", "created_at": ts(d, "12:00:00"), "updated_at": ts(d, "12:00:00"),
        })
        allocs = fifo_allocate(remaining[lid], a)
        assert sum(m for _, m in allocs) == a, "generator produced a payment larger than the outstanding balance"
        for iid, m in allocs:
            alloc_rows.append({
                "allocation_id": ids.allocation_id(len(alloc_rows) + 1), "payment_id": pid, "installment_id": iid,
                "allocated_amount": from_minor(m), "allocation_date": d.isoformat(),
                "allocation_rule": "FIFO_OLDEST_DUE_FIRST", "created_at": ts(d, "12:00:00"), "updated_at": ts(d, "12:00:00"),
            })
    return frame("payment", pay_rows), frame("payment_allocation", alloc_rows)


# ===========================================================================
# derived refresh (also re-used after anomaly injection)
# ===========================================================================
def refresh_derived(cfg: GenConfig, t: dict, close_accounts: bool = False, rng: Optional[np.random.Generator] = None) -> None:
    """Recompute everything derived from allocations, in place on the table dict."""
    ref = cfg.reference_date
    rules = load_rules()
    t["installment"] = derive_installments(t["installment"], t["payment_allocation"], ref)[TABLE_BY_NAME["installment"].columns]
    t["installment"]["updated_at"] = ts(ref, "23:59:59")
    t["loan"] = derive_loans(t["loan"], t["installment"], t["payment_allocation"], ref, rules["case_policy"]["default_dpd"])[TABLE_BY_NAME["loan"].columns]
    t["loan"]["updated_at"] = ts(ref, "23:59:59")
    if close_accounts:
        loans = t["loan"]
        acc = t["account"].copy()
        by_acc = loans.groupby("account_id")
        draws = rng.random(len(acc))
        offs = rng.integers(1, 31, len(acc))
        for i, a in enumerate(acc["account_id"]):
            g = by_acc.get_group(a) if a in by_acc.groups else None
            if g is None or acc.at[i, "account_status"] != "ACTIVE":
                continue
            if (g["loan_status"] == "PAID_OFF").all() and draws[i] < 0.5:
                closed = min(date.fromisoformat(max(g["payoff_date"])) + timedelta(days=int(offs[i])), ref)
                acc.at[i, "account_status"] = "CLOSED"
                acc.at[i, "closed_date"] = closed.isoformat()
                acc.at[i, "updated_at"] = ts(closed)
        t["account"] = acc
    ledger_snaps = [snapshot_frame(t["loan"], t["installment"], t["payment_allocation"], d, rules["case_policy"]["default_dpd"])
                    for d in snapshot_dates(cfg.window_start, ref)]
    snaps = pd.concat(ledger_snaps, ignore_index=True).sort_values(["snapshot_date", "loan_id"], kind="stable").reset_index(drop=True)
    snaps["snapshot_id"] = [ids.snapshot_id(i + 1) for i in range(len(snaps))]
    snaps["created_at"] = [ts(d, "23:59:59") for d in snaps["snapshot_date"]]
    snaps["updated_at"] = snaps["created_at"]
    t["delinquency_snapshot"] = snaps[TABLE_BY_NAME["delinquency_snapshot"].columns].reset_index(drop=True)
    rec = reconcile_payments(t["installment"], t["payment"], t["payment_allocation"]).set_index("payment_id")
    pay = t["payment"].copy()
    pay["reconciliation_status"] = pay["payment_id"].map(rec["reconciliation_status"])
    pay["updated_at"] = ts(ref, "23:59:59")
    t["payment"] = pay


# ===========================================================================
# 7  contact preferences
# ===========================================================================
def _make_pref(rng, customer_id: str, eff_from: date, eff_to: Optional[date], force_opt_out: bool = False) -> dict:
    pref_ch = str(rng.choice(CHANNELS, p=[0.45, 0.25, 0.2, 0.1]))
    allow = {c: (c == pref_ch) or rng.random() < p for c, p in zip(CHANNELS, [0.6, 0.6, 0.7, 0.5])}
    day_choice = rng.choice(["ALL", "WEEKDAYS", "CUSTOM"], p=[0.5, 0.35, 0.15])
    if day_choice == "ALL":
        days = DAY_CODES
    elif day_choice == "WEEKDAYS":
        days = DAY_CODES[:5]
    else:
        drop = set(rng.choice(DAY_CODES[:6], size=int(rng.integers(1, 3)), replace=False))
        days = [d for d in DAY_CODES[:6] if d not in drop]
    ws = str(rng.choice(["08:00", "09:00", "10:00", "12:00"], p=[0.25, 0.35, 0.25, 0.15]))
    we = str(rng.choice(["17:00", "18:00", "19:00", "20:00", "21:00"], p=[0.25, 0.35, 0.2, 0.1, 0.1]))
    mx = None if rng.random() < 0.85 else int(rng.integers(1, 3))
    opt_out = force_opt_out or rng.random() < 0.035
    return {
        "customer_id": customer_id, "preferred_channel": pref_ch,
        "allow_phone": int(allow["PHONE"]), "allow_sms": int(allow["SMS"]), "allow_email": int(allow["EMAIL"]), "allow_post": int(allow["POST"]),
        "allowed_days": ",".join(days), "window_start": ws, "window_end": we, "max_contacts_per_week": mx,
        "opt_out_all": int(opt_out), "consent_source": str(rng.choice(["ENROLMENT", "CUSTOMER_REQUEST", "BRANCH"], p=[0.6, 0.25, 0.15])),
        "effective_from": eff_from.isoformat(), "effective_to": eff_to.isoformat() if eff_to else None,
        "status": "SUPERSEDED" if eff_to else "ACTIVE",
    }


def gen_preferences(cfg: GenConfig, rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    raw = []
    for r in customers.itertuples(index=False):
        since = date.fromisoformat(r.customer_since)
        if rng.random() < 0.07:
            change = cfg.reference_date - timedelta(days=int(rng.integers(30, 330)))
            change = max(change, since + timedelta(days=31))
            raw.append(_make_pref(rng, r.customer_id, since, change - timedelta(days=1), force_opt_out=False))
            raw.append(_make_pref(rng, r.customer_id, change, None, force_opt_out=rng.random() < 0.25))
        else:
            raw.append(_make_pref(rng, r.customer_id, since, None))
    raw.sort(key=lambda p: (p["customer_id"], p["effective_from"]))
    rows = []
    for i, p in enumerate(raw):
        p = dict(p, preference_id=ids.preference_id(i + 1), created_at=ts(p["effective_from"]), updated_at=ts(p["effective_to"] or p["effective_from"]))
        rows.append(p)
    return frame("contact_preference", rows)


# ===========================================================================
# 8  collection cases
# ===========================================================================
def gen_cases(cfg: GenConfig, rng, ledger: Ledger, loans: pd.DataFrame) -> list[dict]:
    rules = load_rules()
    open_dpd = rules["case_policy"]["case_open_dpd"]
    raw = []
    for r in loans.itertuples(index=False):
        for opened, closed in ledger.case_episodes(r.loan_id, open_dpd, cfg.reference_date):
            st = ledger.state(r.loan_id, opened)
            raw.append({"loan_id": r.loan_id, "customer_id": r.customer_id, "opened_date": opened, "closed_date": closed,
                        "opening_dpd": st.dpd, "opening_arrears": from_minor(st.arrears)})
    raw.sort(key=lambda c: (c["opened_date"], c["loan_id"]))
    analysts = rng.integers(1, 13, len(raw))
    for i, c in enumerate(raw):
        c["case_id"] = ids.case_id(i + 1)
        c["assigned_analyst"] = f"ANL-{int(analysts[i]):02d}"
    return raw


# ===========================================================================
# 9-10  contact simulation (attempts, scheduling log, flags, promises)
# ===========================================================================
OUTCOME_PROBS = {
    "PHONE": (["NO_ANSWER", "REACHED_NO_COMMITMENT", "REACHED_PROMISE_MADE", "REACHED_DISPUTE_RAISED", "REACHED_HARDSHIP_DECLARED"],
              [0.55, 0.22, 0.12, 0.025, 0.012]),
    "SMS": (["MESSAGE_SENT", "BOUNCED", "REACHED_PROMISE_MADE", "REACHED_NO_COMMITMENT", "REACHED_DISPUTE_RAISED", "REACHED_HARDSHIP_DECLARED"],
            [0.85, 0.05, 0.05, 0.03, 0.01, 0.005]),
    "EMAIL": (["MESSAGE_SENT", "BOUNCED", "REACHED_PROMISE_MADE", "REACHED_NO_COMMITMENT", "REACHED_DISPUTE_RAISED", "REACHED_HARDSHIP_DECLARED"],
              [0.85, 0.05, 0.05, 0.03, 0.01, 0.005]),
    "POST": (["MESSAGE_SENT", "BOUNCED"], [0.93, 0.07]),
}


def _pick(rng, values, probs):
    p = np.array(probs, dtype=float)
    return str(rng.choice(values, p=p / p.sum()))


def simulate_contacts(cfg: GenConfig, rng: np.random.Generator, t: dict, cases: list[dict], ledger: Ledger):
    """Event-driven simulation in time order across all cases.

    Every proposed contact goes through the SAME eligibility engine used by the API, so the baseline
    contains no violations; rejected proposals are logged with reasons.
    """
    rules = load_rules()
    ref, grace = cfg.reference_date, rules["case_policy"]["promise_grace_days"]
    ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], empty("flag_event"), empty("contact_attempt"), ledger, rules)
    pref_of = ctx.preference_on
    case_by_id = {c["case_id"]: c for c in cases}
    st = {c["case_id"]: {"token": 0, "seq": 0, "promises": [], "followup": False, "dresp": False, "hout": False,
                         "dispute_done": False, "hardship_done": False, "retries": 0, "terminal": False} for c in cases}
    attempts, logs, flags, promises = [], [], [], []
    heap: list = []
    counter = [0]
    now = [datetime.min]  # time of the item currently being processed; the schedule never moves backwards

    def end_dt(c) -> datetime:
        return datetime.combine(c["closed_date"] - timedelta(days=1), time(23, 59, 59)) if c["closed_date"] else datetime.combine(ref, time(23, 59, 59))

    def propose(c, day: date, bad: bool = False) -> tuple[datetime, str]:
        cust = c["customer_id"]
        pref = pref_of(cust, day)
        prof = rules["jurisdiction_profiles"][ctx.profile_of[cust]]
        if pref is None or int(pref["opt_out_all"]):
            return datetime.combine(day, time(10, 0, int(rng.integers(0, 60)))), "PHONE"
        allowed = [ch for ch in CHANNELS if int(pref[f"allow_{ch.lower()}"])]
        w = np.array([0.55 if ch == pref["preferred_channel"] else 0.45 / max(1, len(allowed) - 1) for ch in allowed])
        ch = str(rng.choice(allowed, p=w / w.sum())) if len(allowed) > 1 else allowed[0]
        if ch in prof["time_window_channels"] and not bad:
            days = effective_days(pref, prof)
            for _ in range(7):
                if DAY_CODES[day.weekday()] in days:
                    break
                day += timedelta(days=1)
        if ch in prof["time_window_channels"]:
            s, e = effective_window(pref, prof)
            s_m, e_m = s.hour * 60 + s.minute, e.hour * 60 + e.minute
            if bad:
                minute = min(1439, e_m + int(rng.integers(61, 180))) if rng.random() < 0.5 else max(0, s_m - int(rng.integers(61, 180)))
            elif e_m - s_m >= 30:
                minute = int(rng.integers(s_m, e_m - 10))
            else:
                minute = 600
        else:
            minute = int(rng.integers(8 * 60, 20 * 60))
        return datetime.combine(day, time(minute // 60, minute % 60, int(rng.integers(0, 60)))), ch

    def push(c, when: datetime, channel: str, kind: str = "REG"):
        s = st[c["case_id"]]
        if when <= now[0]:
            when = datetime.combine(now[0].date() + timedelta(days=1), when.time())
        if when > end_dt(c) or s["terminal"]:
            return
        s["token"] += 1
        counter[0] += 1
        heapq.heappush(heap, (when, c["case_id"], counter[0], s["token"], channel, kind))

    def push_regular(c, day: date, allow_bad: bool = True):
        bad = allow_bad and rng.random() < 0.05
        when, ch = propose(c, day, bad)
        push(c, when, ch)

    for c in cases:
        push_regular(c, c["opened_date"] + timedelta(days=1), allow_bad=False)

    def clear_time(key_type, key, after: datetime):
        for ts_, ev in ctx.flag_events.get((key_type, key), []):
            if ev == "CLEARED" and ts_ > after:
                return ts_
        return None

    def raise_flag(c, flag_type: str, when: datetime):
        s = st[c["case_id"]]
        at = when + timedelta(minutes=5)
        loan_key = c["loan_id"] if flag_type == "DISPUTE" else None
        flags.append({"customer_id": c["customer_id"], "loan_id": loan_key, "flag_type": flag_type, "event_type": "RAISED",
                      "event_at": at.isoformat(timespec="seconds"), "source": "CUSTOMER_CONTACT", "resolution_outcome": None,
                      "note": "Raised during contact"})
        ctx.add_flag_event(flag_type, c["loan_id"] if flag_type == "DISPUTE" else c["customer_id"], at, "RAISED")
        lo, hi, prob = (15, 45, 0.6) if flag_type == "DISPUTE" else (30, 75, 0.4)
        if rng.random() < prob:
            clr = at + timedelta(days=int(rng.integers(lo, hi)), hours=int(rng.integers(1, 8)))
            if clr.date() <= ref:
                flags.append({"customer_id": c["customer_id"], "loan_id": loan_key, "flag_type": flag_type, "event_type": "CLEARED",
                              "event_at": clr.isoformat(timespec="seconds"), "source": "ANALYST",
                              "resolution_outcome": "DEBT_VALIDATED" if flag_type == "DISPUTE" else None,
                              "note": "Dispute validated by review" if flag_type == "DISPUTE" else "Hardship review completed"})
                ctx.add_flag_event(flag_type, c["loan_id"] if flag_type == "DISPUTE" else c["customer_id"], clr, "CLEARED")

    def try_promise(c, when: datetime, created: date, attempt_id: Optional[str]):
        s = st[c["case_id"]]
        if len(s["promises"]) >= 2:
            return None
        if any(created <= p["promised_date"] + timedelta(days=grace) for p in s["promises"]):
            return None
        state = ledger.state(c["loan_id"], created)
        arrears = state.arrears
        if arrears <= 0 or created > ref:
            return None
        dates = ledger.loan_alloc_dates.get(c["loan_id"], [])
        nxt = next((x for x in dates if created < x <= created + timedelta(days=21)), None)
        exception = None
        if nxt is not None and rng.random() < 0.9:
            pdate = nxt
            on_day = ledger.paid_asof(c["loan_id"], nxt) - ledger.paid_asof(c["loan_id"], nxt - timedelta(days=1))
            amount = min(arrears, on_day) if rng.random() < 0.55 else int(arrears * float(rng.choice([1.0, 0.75, 0.5], p=[0.5, 0.25, 0.25])))
        else:
            pdate = created + timedelta(days=int(rng.integers(3, 16)))
            amount = int(arrears * float(rng.choice([1.0, 0.75, 0.5], p=[0.5, 0.25, 0.25])))
        if rng.random() < 0.01 and state.outstanding > arrears * 1.1:
            amount = min(state.outstanding, int(arrears * float(rng.uniform(1.1, 1.5))))
            if amount > arrears:
                exception = ("COMPLIANCE-REV-01", "Customer elected to clear more than arrears; approved via exception workflow")
        amount = max(amount, 100)
        status, recv, ev_on = evaluate_promise(ledger, c["loan_id"], created, pdate, amount, ref, grace)
        p = {"case_id": c["case_id"], "loan_id": c["loan_id"], "customer_id": c["customer_id"], "attempt_id": attempt_id,
             "created_date": created, "created_dt": when, "promised_amount": from_minor(amount), "promised_date": pdate,
             "arrears_at_creation": from_minor(arrears), "promise_status": status, "amount_received": from_minor(recv),
             "evaluated_on": ev_on, "exception_approved_by": exception[0] if exception else None,
             "exception_reason": exception[1] if exception else None}
        s["promises"].append(p)
        promises.append(p)
        return p

    while heap:
        when, cid, _, token, channel, kind = heapq.heappop(heap)
        now[0] = when
        c, s = case_by_id[cid], st[cid]
        if token != s["token"] or s["terminal"] or when > end_dt(c):
            continue
        cust, loan = c["customer_id"], c["loan_id"]
        d_act = ctx.flag_active("DISPUTE", loan, when)
        h_act = ctx.flag_active("HARDSHIP", cust, when)
        # --- pick purpose by current state -----------------------------------
        if d_act and not s["dresp"]:
            purpose = "DISPUTE_RESPONSE"
        elif h_act and not s["hout"]:
            purpose = "HARDSHIP_REVIEW_OUTREACH"
        elif d_act or h_act:
            nxt = clear_time("DISPUTE", loan, when) if d_act else clear_time("HARDSHIP", cust, when)
            if nxt:
                s["dresp"] = s["hout"] = False
                push_regular(c, nxt.date() + timedelta(days=1), allow_bad=False)
            continue
        else:
            purpose = "PROMISE_FOLLOWUP" if s["followup"] else "COLLECTION_REMINDER"
        pref = pref_of(cust, when.date())
        if purpose == "DISPUTE_RESPONSE" and pref is not None and not int(pref["opt_out_all"]):
            written = [ch for ch in ("EMAIL", "POST") if int(pref[f"allow_{ch.lower()}"])]
            if not written:
                s["terminal"] = True
                continue
            channel = written[int(rng.integers(0, len(written)))]
        req = ContactRequest(cid, cust, loan, channel, when, purpose)
        dec = ctx.evaluate(req)
        log = {"case_id": cid, "customer_id": cust, "requested_channel": channel, "requested_for": when.isoformat(timespec="seconds"),
               "purpose": purpose, "decision": "PERMITTED" if dec.permitted else "REJECTED", "reason_codes": dec.reasons_csv,
               "attempt_id": None, "requested_by": f"ANALYST-{c['assigned_analyst'][-2:]}", "decided_at": when.isoformat(timespec="seconds"),
               "rules_version": dec.rules_version}
        if not dec.permitted:
            logs.append(log)
            if set(dec.reason_codes) & {"OPT_OUT_ALL", "PAID_OFF_NO_COLLECTION_CONTACT", "ACCOUNT_SUSPENDED", "NO_ACTIVE_PREFERENCE"}:
                s["terminal"] = True
                continue
            s["retries"] += 1
            if s["retries"] > 6:
                s["terminal"] = True
                continue
            when2, ch2 = propose(c, when.date() + timedelta(days=1))
            push(c, when2, ch2)
            continue
        s["retries"] = 0
        s["seq"] += 1
        aid = ids.attempt_id(cid, s["seq"])
        log["attempt_id"] = aid
        logs.append(log)
        ctx.add_attempt(cust, when)
        if purpose in ("DISPUTE_RESPONSE", "HARDSHIP_REVIEW_OUTREACH"):
            outcome = "MESSAGE_SENT" if channel in ("EMAIL", "POST", "SMS") else _pick(rng, ["REACHED_NO_COMMITMENT", "NO_ANSWER"], [0.6, 0.4])
        else:
            outcome = _pick(rng, *OUTCOME_PROBS[channel])
        # outcome side-effects --------------------------------------------------
        promise = None
        if outcome == "REACHED_PROMISE_MADE":
            promise = try_promise(c, when, when.date(), aid)
            if promise is None:
                outcome = "REACHED_NO_COMMITMENT"
        elif outcome == "REACHED_NO_COMMITMENT" and purpose != "HARDSHIP_REVIEW_OUTREACH" and rng.random() < 0.06:
            cb = when + timedelta(days=int(rng.integers(1, 3)))
            if cb.date() < (c["closed_date"] or ref + timedelta(days=1)) and cb.date() <= ref:
                promise = try_promise(c, cb, cb.date(), None)  # inbound call-back promise (no attempt link)
        elif outcome == "REACHED_DISPUTE_RAISED" and not s["dispute_done"]:
            s["dispute_done"], s["dresp"] = True, False
            raise_flag(c, "DISPUTE", when)
        elif outcome == "REACHED_DISPUTE_RAISED":
            outcome = "REACHED_NO_COMMITMENT"
        elif outcome == "REACHED_HARDSHIP_DECLARED" and not s["hardship_done"]:
            s["hardship_done"], s["hout"] = True, False
            raise_flag(c, "HARDSHIP", when)
        elif outcome == "REACHED_HARDSHIP_DECLARED":
            outcome = "REACHED_NO_COMMITMENT"
        if purpose == "DISPUTE_RESPONSE":
            s["dresp"] = True
        if purpose == "HARDSHIP_REVIEW_OUTREACH":
            s["hout"] = True
        if purpose == "PROMISE_FOLLOWUP":
            s["followup"] = False
        attempts.append({"attempt_id": aid, "case_id": cid, "customer_id": cust, "sequence_number": s["seq"], "channel": channel,
                         "purpose": purpose, "scheduled_for": when.isoformat(timespec="seconds"), "attempt_status": "COMPLETED",
                         "outcome": outcome, "created_at": when.isoformat(timespec="seconds"), "updated_at": when.isoformat(timespec="seconds")})
        # --- schedule the next step -------------------------------------------
        d_now = ctx.flag_active("DISPUTE", loan, when + timedelta(minutes=10))
        h_now = ctx.flag_active("HARDSHIP", cust, when + timedelta(minutes=10))
        if outcome in ("REACHED_DISPUTE_RAISED", "REACHED_HARDSHIP_DECLARED"):
            when2, ch2 = propose(c, when.date() + timedelta(days=1))
            push(c, when2.replace(hour=11, minute=int(rng.integers(0, 59))), ch2)
        elif d_now or h_now:
            nxt = clear_time("DISPUTE", loan, when) if d_now else clear_time("HARDSHIP", cust, when)
            if nxt:
                s["dresp"] = s["hout"] = False
                push_regular(c, nxt.date() + timedelta(days=1), allow_bad=False)
            else:
                s["terminal"] = True
        elif promise is not None and promise["created_dt"] >= when:
            s["followup"] = True
            push_regular(c, promise["promised_date"] + timedelta(days=1), allow_bad=False)
        else:
            if s["promises"] and not s["followup"] and s["promises"][-1]["promised_date"] + timedelta(days=grace) >= when.date():
                push_regular(c, s["promises"][-1]["promised_date"] + timedelta(days=1), allow_bad=False)
                s["followup"] = True
            else:
                push_regular(c, when.date() + timedelta(days=int(rng.integers(3, 9))))
    return attempts, logs, flags, promises, ctx


# ===========================================================================
# case status derivation + audit trail
# ===========================================================================
def case_status_at(ctx: ContactContext, c: dict, promises: list, dt: datetime) -> str:
    if c["closed_date"] and dt.date() >= c["closed_date"]:
        return "RESOLVED"
    if ctx.flag_active("DISPUTE", c["loan_id"], dt):
        return "DISPUTE_REVIEW"
    if ctx.flag_active("HARDSHIP", c["customer_id"], dt):
        return "HARDSHIP_REVIEW"
    for p in promises:
        if p["created_dt"] <= dt and (p["evaluated_on"] is None or dt.date() < p["evaluated_on"]):
            return "PROMISE_PENDING"
    return "OPEN"


def debt_status_at(ctx: ContactContext, c: dict, dt: datetime) -> str:
    if ctx.flag_active("DISPUTE", c["loan_id"], dt):
        return "DISPUTED"
    if any(ev == "CLEARED" and ts_ <= dt for ts_, ev in ctx.flag_events.get(("DISPUTE", c["loan_id"]), [])):
        return "CONFIRMED"
    return "UNVERIFIED"


def build_cases_and_audit(cfg: GenConfig, cases, promises, ctx: ContactContext):
    ref_dt = datetime.combine(cfg.reference_date, time(23, 59, 59))
    by_case: dict[str, list] = {}
    for p in promises:
        by_case.setdefault(p["case_id"], []).append(p)
    case_rows, audit = [], []
    for c in cases:
        ps = by_case.get(c["case_id"], [])
        opened_dt = datetime.combine(c["opened_date"], time(8, 0, 0))
        times = {opened_dt}
        for key in (("DISPUTE", c["loan_id"]), ("HARDSHIP", c["customer_id"])):
            for ts_, _ in ctx.flag_events.get(key, []):
                if opened_dt < ts_ <= ref_dt and (not c["closed_date"] or ts_.date() < c["closed_date"]):
                    times.add(ts_)
        for p in ps:
            times.add(p["created_dt"])
            if p["evaluated_on"]:
                times.add(datetime.combine(p["evaluated_on"], time(0, 5, 0)))
        if c["closed_date"]:
            times.add(datetime.combine(c["closed_date"], time(0, 0, 0)))
        prev = None
        for tm in sorted(times):
            new = case_status_at(ctx, c, ps, tm)
            if new != prev:
                audit.append({"entity_type": "CASE", "entity_id": c["case_id"], "action": "CASE_OPENED" if prev is None else "STATUS_CHANGED",
                              "old_value": prev, "new_value": new, "actor": "GENERATOR", "reason": "Derived from source events",
                              "event_ts": tm.isoformat(timespec="seconds"), "details": None})
                prev = new
        status = case_status_at(ctx, c, ps, ref_dt)
        case_rows.append({
            "case_id": c["case_id"], "loan_id": c["loan_id"], "customer_id": c["customer_id"], "opened_date": c["opened_date"].isoformat(),
            "closed_date": c["closed_date"].isoformat() if c["closed_date"] else None, "case_status": status,
            "debt_confirmation_status": debt_status_at(ctx, c, ref_dt), "opening_dpd": c["opening_dpd"],
            "opening_arrears": c["opening_arrears"], "assigned_analyst": c["assigned_analyst"],
            "created_at": ts(c["opened_date"]), "updated_at": ts(cfg.reference_date, "23:59:59"),
        })
    return case_rows, audit


def finalize_promises_audit(promises: list) -> tuple[list, list]:
    promises = sorted(promises, key=lambda p: (p["created_date"], p["case_id"], p["created_dt"]))
    rows, audit = [], []
    for i, p in enumerate(promises):
        pid = ids.promise_id(i + 1)
        rows.append({
            "promise_id": pid, "case_id": p["case_id"], "loan_id": p["loan_id"], "customer_id": p["customer_id"], "attempt_id": p["attempt_id"],
            "created_date": p["created_date"].isoformat(), "promised_amount": p["promised_amount"], "promised_date": p["promised_date"].isoformat(),
            "arrears_at_creation": p["arrears_at_creation"], "promise_status": p["promise_status"], "amount_received": p["amount_received"],
            "evaluated_on": p["evaluated_on"].isoformat() if p["evaluated_on"] else None,
            "exception_approved_by": p["exception_approved_by"], "exception_reason": p["exception_reason"],
            "created_at": p["created_dt"].isoformat(timespec="seconds"), "updated_at": (p["evaluated_on"] or p["created_date"]).isoformat() + "T00:05:00",
        })
        audit.append({"entity_type": "PROMISE", "entity_id": pid, "action": "PROMISE_CREATED", "old_value": None, "new_value": "PENDING",
                      "actor": "GENERATOR", "reason": "Promise recorded", "event_ts": p["created_dt"].isoformat(timespec="seconds"), "details": None})
        if p["evaluated_on"]:
            audit.append({"entity_type": "PROMISE", "entity_id": pid, "action": "STATUS_CHANGED", "old_value": "PENDING", "new_value": p["promise_status"],
                          "actor": "GENERATOR", "reason": "Outcome derived from payment allocations",
                          "event_ts": p["evaluated_on"].isoformat() + "T00:05:00", "details": None})
    return rows, audit


def assign_audit_ids(audit: list) -> pd.DataFrame:
    audit = sorted(audit, key=lambda a: (a["event_ts"], a["entity_type"], a["entity_id"], a["action"], str(a["new_value"])))
    for i, a in enumerate(audit):
        a["audit_id"] = ids.audit_id(i + 1)
    return frame("audit_event", audit)


# ===========================================================================
# orchestration
# ===========================================================================
def generate(cfg: GenConfig, inject: bool = True, verbose: bool = False) -> Dataset:
    log = (lambda m: print(f"[generate] {m}", flush=True)) if verbose else (lambda m: None)
    ss = np.random.SeedSequence(cfg.seed)
    r = {k: np.random.default_rng(s) for k, s in zip(
        ["customers", "accounts", "loans", "payments", "prefs", "cases", "contacts", "closing", "anomalies"], ss.spawn(9))}
    t: dict = {}
    t["customer"] = gen_customers(cfg, r["customers"]); log(f"customers={len(t['customer'])}")
    t["account"] = gen_accounts(cfg, r["accounts"], t["customer"]); log(f"accounts={len(t['account'])}")
    t["loan"], t["installment"] = gen_loans_installments(cfg, r["loans"], t["customer"], t["account"]); log(f"loans={len(t['loan'])} installments={len(t['installment'])}")
    t["payment"], t["payment_allocation"] = gen_payments(cfg, r["payments"], t["customer"], t["account"], t["loan"], t["installment"]); log(f"payments={len(t['payment'])}")
    t["delinquency_snapshot"] = empty("delinquency_snapshot")
    refresh_derived(cfg, t, close_accounts=True, rng=r["closing"]); log(f"snapshots={len(t['delinquency_snapshot'])}")
    t["contact_preference"] = gen_preferences(cfg, r["prefs"], t["customer"]); log(f"preferences={len(t['contact_preference'])}")
    rules = load_rules()
    ledger = Ledger(t["loan"], t["installment"], t["payment_allocation"], rules["case_policy"]["default_dpd"])
    cases = gen_cases(cfg, r["cases"], ledger, t["loan"]); log(f"cases={len(cases)}")
    attempts, logs, flags, promises, ctx = simulate_contacts(cfg, r["contacts"], t, cases, ledger)
    log(f"attempts={len(attempts)} sched_log={len(logs)} flags={len(flags)} promises={len(promises)}")
    case_rows, audit = build_cases_and_audit(cfg, cases, promises, ctx)
    t["collection_case"] = frame("collection_case", case_rows)
    attempts.sort(key=lambda a: (a["case_id"], a["sequence_number"]))
    t["contact_attempt"] = frame("contact_attempt", attempts)
    logs.sort(key=lambda x: (x["requested_for"], x["case_id"]))
    t["contact_scheduling_log"] = frame("contact_scheduling_log", [dict(l, log_id=ids.scheduling_log_id(i + 1), created_at=l["decided_at"], updated_at=l["decided_at"]) for i, l in enumerate(logs)])
    flags.sort(key=lambda f: (f["event_at"], f["customer_id"], f["flag_type"], f["event_type"]))
    t["flag_event"] = frame("flag_event", [dict(f, flag_event_id=ids.flag_event_id(i + 1), created_at=f["event_at"], updated_at=f["event_at"]) for i, f in enumerate(flags)])
    p_rows, p_audit = finalize_promises_audit(promises)
    t["promise_to_pay"] = frame("promise_to_pay", p_rows)
    t["audit_event"] = assign_audit_ids(audit + p_audit)
    gt = pd.DataFrame(columns=["ground_truth_id", "issue_type", "entity_type", "entity_id", "case_id", "loan_id", "customer_id",
                               "ground_truth", "expected_decision", "injection_detail"])
    ds = Dataset(t, gt, cfg.as_dict())
    if inject:
        from .injection import inject_anomalies
        inject_anomalies(cfg, ds, r["anomalies"])
    ds.stats = {k: len(v) for k, v in ds.tables.items()}
    ds.stats["ground_truth"] = len(ds.ground_truth)
    return ds
