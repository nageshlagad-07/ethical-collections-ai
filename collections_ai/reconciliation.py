"""Payment allocation replay, payment reconciliation and promise-to-pay outcome evaluation.

Allocation policy (documented in docs/BUSINESS_RULES.md): payments are applied to
the loan's instalments OLDEST-DUE-FIRST (FIFO), in (payment_date, payment_id) order.
`replay_fifo` recomputes what the allocations *should* be from source records; the
recorded `payment_allocation` rows are then compared against that replay.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .delinquency import Ledger, fifo_allocate, to_minor


def replay_fifo(installments: pd.DataFrame, payments: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Return {payment_id: {installment_id: minor}} expected under the FIFO policy."""
    inst_by_loan: dict[str, list] = {}
    for r in installments.sort_values(["loan_id", "installment_number"]).itertuples(index=False):
        inst_by_loan.setdefault(r.loan_id, []).append([r.installment_id, to_minor(r.amount_due)])
    expected: dict[str, dict[str, int]] = {}
    pays = payments.sort_values(["loan_id", "payment_date", "payment_id"])
    cur_loan, remaining = None, []
    for r in pays.itertuples(index=False):
        if r.loan_id != cur_loan:
            cur_loan = r.loan_id
            remaining = [(i, a) for i, a in inst_by_loan.get(cur_loan, [])]
        allocs = fifo_allocate(remaining, to_minor(r.amount))
        expected[r.payment_id] = dict(allocs)
    return expected


def recorded_allocations(allocations: pd.DataFrame) -> dict[str, dict[str, int]]:
    rec: dict[str, dict[str, int]] = {}
    for r in allocations.itertuples(index=False):
        rec.setdefault(r.payment_id, {})
        rec[r.payment_id][r.installment_id] = rec[r.payment_id].get(r.installment_id, 0) + to_minor(r.allocated_amount)
    return rec


def reconcile_payments(installments: pd.DataFrame, payments: pd.DataFrame, allocations: pd.DataFrame) -> pd.DataFrame:
    """Reconcile every payment to its instalment allocations.

    Columns: payment_id, reconciliation_status, reason, expected_installment_ids, recorded_installment_ids, variance
    """
    expected = replay_fifo(installments, payments)
    recorded = recorded_allocations(allocations)
    inst_loan = dict(zip(installments["installment_id"], installments["loan_id"]))
    rows = []
    for r in payments.itertuples(index=False):
        exp, rec = expected.get(r.payment_id, {}), recorded.get(r.payment_id, {})
        amt = to_minor(r.amount)
        total = sum(rec.values())
        if not rec:
            status, reason = "UNRECONCILED", "UNALLOCATED"
        elif any(inst_loan.get(i) != r.loan_id for i in rec):
            status, reason = "EXCEPTION", "CROSS_LOAN_ALLOCATION"
        elif total != amt:
            status, reason = "EXCEPTION", "ALLOCATION_SUM_MISMATCH"
        elif rec != exp:
            status, reason = "EXCEPTION", "NOT_OLDEST_DUE_FIRST"
        else:
            status, reason = "RECONCILED", "OK"
        rows.append((r.payment_id, status, reason, ",".join(sorted(exp)), ",".join(sorted(rec)), (amt - total) / 100))
    return pd.DataFrame(rows, columns=["payment_id", "reconciliation_status", "reason",
                                       "expected_installment_ids", "recorded_installment_ids", "variance"])


# ---------------------------------------------------------------------------
# Promise-to-pay outcome evaluation
# ---------------------------------------------------------------------------
def evaluate_promise(ledger: Ledger, loan_id: str, created: date, promised_date: date, promised_minor: int,
                     ref: date, grace_days: int) -> tuple[str, int, Optional[date]]:
    """Derive (status, amount_received_minor, evaluated_on) from allocations.

    Window = allocations dated strictly after `created` and up to promised_date + grace.
    * FULFILLED            : received >= promised (evaluated_on = date cumulative receipts reached it)
    * PARTIALLY_FULFILLED  : window closed, 0 < received < promised
    * BROKEN               : window closed, nothing received
    * PENDING              : window still open (ref <= promised_date + grace)
    """
    end = promised_date + timedelta(days=grace_days)
    upto = min(end, ref)
    base = ledger.paid_asof(loan_id, created)
    received = ledger.paid_asof(loan_id, upto) - base
    if received >= promised_minor:
        dates = ledger.loan_alloc_dates.get(loan_id, [])
        cum = ledger.loan_alloc_cum.get(loan_id, [])
        hit = upto
        for dt, c in zip(dates, cum):
            if dt > created and dt <= upto and c - base >= promised_minor:
                hit = dt
                break
        return "FULFILLED", received, hit
    if ref > end:
        return ("PARTIALLY_FULFILLED" if received > 0 else "BROKEN"), received, end + timedelta(days=1)
    return "PENDING", received, None
