"""Delinquency calculations, arrears reconciliation, payment allocation, promise reconciliation."""
from datetime import date, timedelta

import pandas as pd
import pytest

from collections_ai.delinquency import Ledger, bucket_for, fifo_allocate, snapshot_frame, status_for, to_minor
from collections_ai.reconciliation import evaluate_promise, reconcile_payments, replay_fifo


def mini(amounts=(10000, 10000, 10000), dues=("2026-08-01", "2026-09-01", "2026-10-01"), pays=()):
    loans = pd.DataFrame([{"loan_id": "L1", "total_payable": float(sum(amounts)), "origination_date": "2026-07-01"}])
    inst = pd.DataFrame([{"installment_id": f"I{k + 1}", "loan_id": "L1", "installment_number": k + 1, "due_date": d, "amount_due": float(a)}
                         for k, (a, d) in enumerate(zip(amounts, dues))])
    alloc = pd.DataFrame(pays, columns=["allocation_id", "payment_id", "installment_id", "allocated_amount", "allocation_date"])
    return loans, inst, alloc


def test_no_payment_arrears_and_dpd():
    loans, inst, alloc = mini()
    s = Ledger(loans, inst, alloc).state("L1", date(2026, 10, 22))
    assert s.arrears == to_minor(30000) and s.outstanding == to_minor(30000)
    assert s.dpd == (date(2026, 10, 22) - date(2026, 8, 1)).days
    assert s.overdue_installments == 3 and s.status == "DELINQUENT"


def test_partial_payment_not_double_counted():
    loans, inst, alloc = mini(pays=[("A1", "P1", "I1", 4000.0, "2026-08-05")])
    s = Ledger(loans, inst, alloc).state("L1", date(2026, 10, 22))
    assert s.total_paid == to_minor(4000)
    assert s.arrears == to_minor(26000)          # 6000 + 10000 + 10000
    assert s.outstanding == to_minor(26000)
    assert s.dpd == (date(2026, 10, 22) - date(2026, 8, 1)).days  # oldest still partly unpaid


def test_dpd_moves_to_next_oldest_after_clearing_first():
    loans, inst, alloc = mini(pays=[("A1", "P1", "I1", 10000.0, "2026-08-05")])
    s = Ledger(loans, inst, alloc).state("L1", date(2026, 10, 22))
    assert s.dpd == (date(2026, 10, 22) - date(2026, 9, 1)).days and s.overdue_installments == 2


def test_as_of_date_ignores_future_payments():
    loans, inst, alloc = mini(pays=[("A1", "P1", "I1", 10000.0, "2026-10-01")])
    led = Ledger(loans, inst, alloc)
    assert led.state("L1", date(2026, 9, 20)).total_paid == 0
    assert led.state("L1", date(2026, 10, 22)).total_paid == to_minor(10000)


def test_paid_off_status_and_bucket():
    loans, inst, alloc = mini(pays=[("A1", "P1", "I1", 10000.0, "2026-08-01"), ("A2", "P2", "I2", 10000.0, "2026-09-01"), ("A3", "P3", "I3", 10000.0, "2026-10-01")])
    s = Ledger(loans, inst, alloc).state("L1", date(2026, 10, 22))
    assert s.status == "PAID_OFF" and s.outstanding == 0 and bucket_for(s.dpd, s.status) == "PAID_OFF"


@pytest.mark.parametrize("dpd,expected", [(0, "CURRENT"), (1, "DPD_1_30"), (30, "DPD_1_30"), (31, "DPD_31_60"), (60, "DPD_31_60"), (61, "DPD_61_90"), (90, "DPD_61_90"), (91, "DPD_90_PLUS")])
def test_bucket_boundaries(dpd, expected):
    assert bucket_for(dpd, "DELINQUENT" if dpd else "CURRENT") == expected


def test_default_threshold_boundary():
    assert status_for(100, 100, 90) == "DELINQUENT"
    assert status_for(100, 100, 91) == "DEFAULT"


def test_fifo_allocation_oldest_first_and_shared_state():
    rem = [("I1", 5000), ("I2", 10000)]
    assert fifo_allocate(rem, 7000) == [("I1", 5000), ("I2", 2000)]
    assert rem == [("I1", 0), ("I2", 8000)]
    assert fifo_allocate(rem, 8000) == [("I2", 8000)]


def test_snapshot_frame_matches_ledger(tables, cfg):
    snap = snapshot_frame(tables["loan"], tables["installment"], tables["payment_allocation"], cfg.reference_date).set_index("loan_id")
    led = Ledger(tables["loan"], tables["installment"], tables["payment_allocation"])
    for lid in tables["loan"]["loan_id"].sample(120, random_state=3):
        st = led.state(lid, cfg.reference_date)
        assert snap.at[lid, "total_arrears"] == pytest.approx(st.arrears / 100)
        assert snap.at[lid, "days_past_due"] == st.dpd
        assert snap.at[lid, "outstanding_balance"] == pytest.approx(st.outstanding / 100)


def test_arrears_reconcile_with_installment_table(tables, cfg):
    inst = tables["installment"]
    od = inst[inst["due_date"] < cfg.reference_date.isoformat()]
    arrears = (od["amount_due"] - od["amount_paid"]).groupby(od["loan_id"]).sum().round(2)
    snap = tables["delinquency_snapshot"].query("snapshot_date == @cfg.reference_date.isoformat()").set_index("loan_id")["total_arrears"]
    j = pd.concat([arrears.rename("a"), snap.rename("s")], axis=1).fillna(0)
    assert ((j["a"] - j["s"]).abs() < 0.011).all()


def test_total_payable_equals_sum_of_installments(tables):
    s = tables["installment"].groupby("loan_id")["amount_due"].sum().round(2)
    t = tables["loan"].set_index("loan_id")["total_payable"]
    assert ((s - t.reindex(s.index)).abs() < 0.011).all()


def test_payment_fully_allocated_and_never_over_installment(tables):
    a = tables["payment_allocation"].groupby("payment_id")["allocated_amount"].sum().round(2)
    p = tables["payment"].set_index("payment_id")["amount"]
    assert ((a - p.reindex(a.index)).abs() < 0.011).all() and len(a) == len(p)
    per = tables["payment_allocation"].groupby("installment_id")["allocated_amount"].sum()
    due = tables["installment"].set_index("installment_id")["amount_due"]
    assert (per <= due.reindex(per.index) + 0.005).all()


def test_baseline_reconciles_fully(baseline):
    r = reconcile_payments(baseline.tables["installment"], baseline.tables["payment"], baseline.tables["payment_allocation"])
    assert (r["reconciliation_status"] == "RECONCILED").all()


def test_wrong_installment_allocation_detected_as_exception(tables):
    r = reconcile_payments(tables["installment"], tables["payment"], tables["payment_allocation"])
    assert len(r[r["reconciliation_status"] == "EXCEPTION"]) > 0
    assert (r[r["reconciliation_status"] == "EXCEPTION"]["reason"] == "NOT_OLDEST_DUE_FIRST").all()


def test_replay_fifo_matches_recorded_on_baseline(baseline):
    exp = replay_fifo(baseline.tables["installment"], baseline.tables["payment"])
    assert sum(sum(v.values()) for v in exp.values()) == sum(to_minor(x) for x in baseline.tables["payment"]["amount"])


# ---- promise outcome evaluation ------------------------------------------------
def promise_ledger(pay_amount, pay_date):
    loans, inst, alloc = mini(pays=[("A1", "P1", "I1", float(pay_amount), pay_date)] if pay_amount else [])
    return Ledger(loans, inst, alloc)


def test_promise_fulfilled():
    st, recv, ev = evaluate_promise(promise_ledger(10000, "2026-10-05"), "L1", date(2026, 10, 2), date(2026, 10, 6), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "FULFILLED" and recv == to_minor(10000) and ev == date(2026, 10, 5)


def test_promise_partially_fulfilled():
    st, recv, _ = evaluate_promise(promise_ledger(4000, "2026-10-05"), "L1", date(2026, 10, 2), date(2026, 10, 6), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "PARTIALLY_FULFILLED" and recv == to_minor(4000)


def test_promise_broken():
    st, recv, _ = evaluate_promise(promise_ledger(0, None), "L1", date(2026, 10, 2), date(2026, 10, 6), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "BROKEN" and recv == 0


def test_promise_pending_inside_window():
    st, _, ev = evaluate_promise(promise_ledger(0, None), "L1", date(2026, 10, 20), date(2026, 10, 25), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "PENDING" and ev is None


def test_promise_payment_after_grace_does_not_count():
    st, recv, _ = evaluate_promise(promise_ledger(10000, "2026-10-12"), "L1", date(2026, 10, 2), date(2026, 10, 6), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "BROKEN" and recv == 0


def test_promise_payment_before_creation_does_not_count():
    st, recv, _ = evaluate_promise(promise_ledger(10000, "2026-10-01"), "L1", date(2026, 10, 2), date(2026, 10, 6), to_minor(10000), date(2026, 10, 22), 3)
    assert st == "BROKEN"


def test_stored_promise_outcomes_match_payments(tables, cfg):
    led = Ledger(tables["loan"], tables["installment"], tables["payment_allocation"])
    sample = tables["promise_to_pay"].sample(150, random_state=1)
    for p in sample.itertuples(index=False):
        st, recv, _ = evaluate_promise(led, p.loan_id, date.fromisoformat(p.created_date), date.fromisoformat(p.promised_date), to_minor(p.promised_amount), cfg.reference_date, 3)
        assert st == p.promise_status and recv / 100 == pytest.approx(p.amount_received)
