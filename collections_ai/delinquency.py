"""Delinquency calculation engine.

All money arithmetic is done in integer minor units (paise) to avoid floating
point drift, then converted back to 2-decimal floats for storage.

Methodology (also in docs/BUSINESS_RULES.md)
--------------------------------------------
* total_payable      = SUM(installment.amount_due)               (contractual model)
* total_paid(d)      = SUM(allocations with allocation_date <= d) (each allocation counted once)
* outstanding(d)     = total_payable - total_paid(d)              (contractual balance)
* remaining_i(d)     = amount_due_i - allocated_i(d)
* overdue instalment = due_date < d and remaining_i(d) > 0
* arrears(d)         = SUM(remaining_i(d)) over overdue instalments
* DPD(d)             = d - due_date of the OLDEST overdue instalment (0 if none)
* loan status(d)     = PAID_OFF if outstanding==0, DEFAULT if DPD > default_dpd,
                       DELINQUENT if arrears>0, else CURRENT
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd


def to_minor(x: float) -> int:
    return int(round(float(x) * 100))


def from_minor(m: int | float) -> float:
    return round(float(m) / 100.0, 2)


def bucket_for(dpd: int, status: str) -> str:
    if status == "PAID_OFF":
        return "PAID_OFF"
    if dpd <= 0:
        return "CURRENT"
    if dpd <= 30:
        return "DPD_1_30"
    if dpd <= 60:
        return "DPD_31_60"
    if dpd <= 90:
        return "DPD_61_90"
    return "DPD_90_PLUS"


def status_for(outstanding_minor: int, arrears_minor: int, dpd: int, default_dpd: int = 90) -> str:
    if outstanding_minor <= 0:
        return "PAID_OFF"
    if arrears_minor > 0 and dpd > default_dpd:
        return "DEFAULT"
    if arrears_minor > 0:
        return "DELINQUENT"
    return "CURRENT"


def d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


@dataclass
class LoanState:
    loan_id: str
    as_of: date
    total_payable: int
    total_paid: int
    outstanding: int
    arrears: int
    dpd: int
    overdue_installments: int
    status: str

    def as_dict_money(self) -> dict:
        return {
            "loan_id": self.loan_id, "as_of": self.as_of.isoformat(),
            "total_payable": from_minor(self.total_payable), "total_paid": from_minor(self.total_paid),
            "outstanding_balance": from_minor(self.outstanding), "arrears": from_minor(self.arrears),
            "days_past_due": self.dpd, "overdue_installments": self.overdue_installments,
            "loan_status": self.status, "bucket": bucket_for(self.dpd, self.status),
        }


class Ledger:
    """In-memory as-of ledger built from loan/installment/allocation tables."""

    def __init__(self, loans: pd.DataFrame, installments: pd.DataFrame, allocations: pd.DataFrame,
                 default_dpd: int = 90):
        self.default_dpd = default_dpd
        self.total_payable: dict[str, int] = {}
        self.inst: dict[str, list] = {}
        self.inst_alloc: dict[str, list] = {}  # installment_id -> [(date, minor)] sorted
        self.loan_alloc_dates: dict[str, list] = {}
        self.loan_alloc_cum: dict[str, list] = {}
        self._loan_inst_ids: dict[str, list] = {}
        for lid, tp in zip(loans["loan_id"], loans["total_payable"]):
            self.total_payable[lid] = to_minor(tp)
        for r in installments.sort_values(["loan_id", "installment_number"]).itertuples(index=False):
            self.inst.setdefault(r.loan_id, []).append(
                (r.installment_id, int(r.installment_number), d(r.due_date), to_minor(r.amount_due)))
            self.inst_alloc.setdefault(r.installment_id, [])
        if len(allocations):
            inst_loan = dict(zip(installments["installment_id"], installments["loan_id"]))
            tmp: dict[str, list] = {}
            for r in allocations.sort_values(["allocation_date", "allocation_id"]).itertuples(index=False):
                m = to_minor(r.allocated_amount)
                self.inst_alloc[r.installment_id].append((d(r.allocation_date), m))
                tmp.setdefault(inst_loan[r.installment_id], []).append((d(r.allocation_date), m))
            for lid, lst in tmp.items():
                lst.sort(key=lambda t: t[0])
                self.loan_alloc_dates[lid] = [t[0] for t in lst]
                cum, c = [], 0
                for _, m in lst:
                    c += m
                    cum.append(c)
                self.loan_alloc_cum[lid] = cum

    # ---- primitive as-of queries -------------------------------------------
    def paid_asof(self, loan_id: str, as_of: date) -> int:
        dates = self.loan_alloc_dates.get(loan_id)
        if not dates:
            return 0
        i = bisect.bisect_right(dates, as_of)
        return self.loan_alloc_cum[loan_id][i - 1] if i else 0

    def inst_paid_asof(self, installment_id: str, as_of: date) -> int:
        return sum(m for dt, m in self.inst_alloc.get(installment_id, []) if dt <= as_of)

    def installments(self, loan_id: str) -> list:
        return self.inst.get(loan_id, [])

    def state(self, loan_id: str, as_of: date) -> LoanState:
        tp = self.total_payable[loan_id]
        paid = self.paid_asof(loan_id, as_of)
        arrears, dpd, n_over = 0, 0, 0
        for iid, _, due, amt in self.inst.get(loan_id, []):
            if due < as_of:
                rem = amt - self.inst_paid_asof(iid, as_of)
                if rem > 0:
                    arrears += rem
                    n_over += 1
                    dpd = max(dpd, (as_of - due).days)
        out = tp - paid
        return LoanState(loan_id, as_of, tp, paid, out, arrears, dpd, n_over,
                         status_for(out, arrears, dpd, self.default_dpd))

    def fully_paid_date(self, installment_id: str, amount: int) -> Optional[date]:
        run = 0
        for dt, m in self.inst_alloc.get(installment_id, []):
            run += m
            if run >= amount:
                return dt
        return None

    def late_installment_count(self, loan_id: str, as_of: date, grace_days: int = 7) -> int:
        """Instalments past due as of `as_of` that were cleared more than grace_days late, or are still unpaid."""
        n = 0
        for iid, _, due, amt in self.inst.get(loan_id, []):
            if due >= as_of:
                continue
            cleared = self.fully_paid_date(iid, amt)
            if cleared is None or cleared > as_of:
                n += 1
            elif (cleared - due).days > grace_days:
                n += 1
        return n

    def payoff_date(self, loan_id: str) -> Optional[date]:
        cum = self.loan_alloc_cum.get(loan_id)
        if not cum or cum[-1] < self.total_payable[loan_id]:
            return None
        idx = next(i for i, c in enumerate(cum) if c >= self.total_payable[loan_id])
        return self.loan_alloc_dates[loan_id][idx]

    def arrears_asof(self, loan_id: str, as_of: date) -> int:
        return self.state(loan_id, as_of).arrears

    # ---- collection-case episodes -----------------------------------------
    def case_episodes(self, loan_id: str, open_dpd: int, ref: date) -> list[tuple[date, Optional[date]]]:
        """Return [(opened, closed|None)] delinquency episodes.

        A case opens the first day DPD >= open_dpd and closes when arrears return to zero.
        Evaluated at event dates: due+open_dpd for each instalment and each payment date.
        """
        pts = {ref}
        for _, _, due, _ in self.inst.get(loan_id, []):
            pts.add(due + timedelta(days=open_dpd))
        pts.update(self.loan_alloc_dates.get(loan_id, []))
        episodes: list[tuple[date, Optional[date]]] = []
        open_since: Optional[date] = None
        for t in sorted(p for p in pts if p <= ref):
            st = self.state(loan_id, t)
            if open_since is None and st.dpd >= open_dpd:
                open_since = t
            elif open_since is not None and st.arrears == 0:
                episodes.append((open_since, t))
                open_since = None
        if open_since is not None:
            episodes.append((open_since, None))
        return episodes


# ---------------------------------------------------------------------------
# FIFO payment allocation (the documented allocation policy)
# ---------------------------------------------------------------------------
def fifo_allocate(remaining: list[tuple[str, int]], amount_minor: int) -> list[tuple[str, int]]:
    """Allocate `amount_minor` to instalments oldest-due-first.

    `remaining` is a list of (installment_id, remaining_minor) ordered by due date.
    It is MUTATED (remaining reduced) so sequential payments share state.
    Returns [(installment_id, allocated_minor)]; any unallocated surplus is simply not returned.
    """
    out = []
    left = amount_minor
    for i, (iid, rem) in enumerate(remaining):
        if left <= 0:
            break
        if rem <= 0:
            continue
        take = min(rem, left)
        out.append((iid, take))
        remaining[i] = (iid, rem - take)
        left -= take
    return out


# ---------------------------------------------------------------------------
# Vectorised portfolio calculations
# ---------------------------------------------------------------------------
def _minor(series: pd.Series) -> pd.Series:
    return (series.astype(float) * 100).round().astype("int64")


def snapshot_frame(loans: pd.DataFrame, installments: pd.DataFrame, allocations: pd.DataFrame,
                   as_of: date, default_dpd: int = 90) -> pd.DataFrame:
    """Delinquency measurements for every loan originated on/before `as_of`."""
    ts = pd.Timestamp(as_of)
    lo = loans[pd.to_datetime(loans["origination_date"]) <= ts][["loan_id", "total_payable"]].copy()
    lo["tp"] = _minor(lo["total_payable"])
    ins = installments[installments["loan_id"].isin(lo["loan_id"])][["installment_id", "loan_id", "due_date", "amount_due"]].copy()
    ins["amt"] = _minor(ins["amount_due"])
    ins["due"] = pd.to_datetime(ins["due_date"])
    al = allocations[pd.to_datetime(allocations["allocation_date"]) <= ts]
    per_inst = al.assign(m=_minor(al["allocated_amount"])).groupby("installment_id")["m"].sum()
    ins["paid"] = ins["installment_id"].map(per_inst).fillna(0).astype("int64")
    ins["rem"] = ins["amt"] - ins["paid"]
    over = ins[(ins["due"] < ts) & (ins["rem"] > 0)].copy()
    over["dpd"] = (ts - over["due"]).dt.days
    g = over.groupby("loan_id").agg(total_arrears=("rem", "sum"), days_past_due=("dpd", "max"),
                                    overdue_installments=("rem", "size"))
    paid_loan = ins.groupby("loan_id")["paid"].sum()
    out = lo.set_index("loan_id")
    out["total_paid_m"] = paid_loan.reindex(out.index).fillna(0).astype("int64")
    out = out.join(g).fillna({"total_arrears": 0, "days_past_due": 0, "overdue_installments": 0})
    out["outstanding_m"] = out["tp"] - out["total_paid_m"]
    for c in ("total_arrears", "days_past_due", "overdue_installments"):
        out[c] = out[c].astype("int64")
    out["loan_status"] = [status_for(o, a, p, default_dpd) for o, a, p in
                          zip(out["outstanding_m"], out["total_arrears"], out["days_past_due"])]
    out["delinquency_bucket"] = [bucket_for(p, s) for p, s in zip(out["days_past_due"], out["loan_status"])]
    res = pd.DataFrame({
        "loan_id": out.index,
        "snapshot_date": as_of.isoformat(),
        "total_amount_paid": (out["total_paid_m"] / 100).round(2).values,
        "outstanding_balance": (out["outstanding_m"] / 100).round(2).values,
        "total_arrears": (out["total_arrears"] / 100).round(2).values,
        "overdue_installments": out["overdue_installments"].values,
        "days_past_due": out["days_past_due"].values,
        "delinquency_bucket": out["delinquency_bucket"].values,
        "loan_status": out["loan_status"].values,
    })
    return res.reset_index(drop=True)


def snapshot_dates(window_start: date, ref: date) -> list[date]:
    """Month-end dates inside the data period (strictly before ref) plus the reference date."""
    dates, y, m = [], window_start.year, window_start.month
    while True:
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        month_end = nxt - timedelta(days=1)
        if month_end >= ref:
            break
        if month_end >= window_start:
            dates.append(month_end)
        y, m = nxt.year, nxt.month
    dates.append(ref)
    return dates


def derive_installments(installments: pd.DataFrame, allocations: pd.DataFrame, ref: date) -> pd.DataFrame:
    """Recompute amount_paid / installment_status / fully_paid_date from allocations."""
    ins = installments.copy()
    amt = _minor(ins["amount_due"])
    al = allocations.assign(m=_minor(allocations["allocated_amount"]))
    paid = al.groupby("installment_id")["m"].sum()
    last = al.groupby("installment_id")["allocation_date"].max()
    p = ins["installment_id"].map(paid).fillna(0).astype("int64")
    ins["amount_paid"] = (p / 100).round(2)
    rem = amt - p
    due = pd.to_datetime(ins["due_date"])
    status = np.where(rem <= 0, "PAID",
                      np.where(due >= pd.Timestamp(ref), "NOT_DUE",
                               np.where(p == 0, "OVERDUE", "PARTIALLY_PAID")))
    ins["installment_status"] = status
    fpd = ins["installment_id"].map(last)
    ins["fully_paid_date"] = np.where(rem <= 0, fpd, None)
    ins["fully_paid_date"] = ins["fully_paid_date"].astype(object).where(rem <= 0, None)
    return ins


def derive_loans(loans: pd.DataFrame, installments: pd.DataFrame, allocations: pd.DataFrame, ref: date,
                 default_dpd: int = 90) -> pd.DataFrame:
    """Recompute loan_status / payoff_date as of the reference date."""
    snap = snapshot_frame(loans, installments, allocations, ref, default_dpd).set_index("loan_id")
    out = loans.copy()
    out["loan_status"] = out["loan_id"].map(snap["loan_status"])
    out["status_as_of"] = ref.isoformat()
    inst_loan = dict(zip(installments["installment_id"], installments["loan_id"]))
    last_alloc = allocations.assign(loan_id=allocations["installment_id"].map(inst_loan)).groupby("loan_id")["allocation_date"].max()
    out["payoff_date"] = out["loan_id"].map(last_alloc)
    out["payoff_date"] = out["payoff_date"].astype(object).where(out["loan_status"] == "PAID_OFF", None)
    return out
