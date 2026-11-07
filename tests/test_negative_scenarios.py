"""The five required labelled negative scenarios: inject -> detect -> explain -> system response. No ground-truth leakage."""
import inspect
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from collections_ai import detection, prioritization, contact, reconciliation, delinquency
from collections_ai.detection import detect_all, evaluate_against_ground_truth
from collections_ai.prioritization import prioritize_cases
from collections_ai.spec import ISSUE_TYPES, TABLE_BY_NAME

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def detected(tables, cfg):
    return detect_all(tables, cfg.reference_date)


def test_all_five_issue_types_present_with_ground_truth(ds):
    gt = ds.ground_truth
    assert set(gt["issue_type"]) == set(ISSUE_TYPES)
    for k in ISSUE_TYPES:
        assert (gt["issue_type"] == k).sum() >= 3
    for col in ("issue_type", "ground_truth", "expected_decision", "entity_id"):
        assert gt[col].notna().all() and (gt[col].astype(str) != "").all()


def test_ground_truth_is_separate_from_tables(ds):
    for t in ds.tables.values():
        assert not {"issue_type", "ground_truth", "ground_truth_id"} & set(t.columns)


def test_injection_preserves_valid_relationships(ds, cfg):
    from collections_ai.validation import run_validation
    res = run_validation(ds.tables, cfg.reference_date)
    assert all(r["passed"] for r in res if r["category"] != "BUSINESS_RULE_EXCEPTION")


@pytest.mark.parametrize("issue", ISSUE_TYPES)
def test_each_scenario_detected_with_perfect_precision_recall(issue, detected, ds):
    ev = evaluate_against_ground_truth(detected, ds.ground_truth).set_index("issue_type").loc[issue]
    assert ev["ground_truth"] > 0
    assert ev["recall"] == 1.0 and ev["precision"] == 1.0, ev.to_dict()


def test_no_unlabelled_exceptions_in_baseline(baseline, cfg):
    assert detect_all(baseline.tables, cfg.reference_date) == []


def test_detection_returns_explanations(detected):
    for d in detected:
        assert d["description"] and d["expected_decision"] and d["system_response"] and d["severity"]


def test_scenario1_paid_account_in_collection(ds, tables, cfg):
    ids_ = ds.ground_truth.query("issue_type == 'PAID_ACCOUNT_IN_COLLECTION'")["entity_id"]
    prio = {r["case_id"]: r for r in prioritize_cases(tables, cfg.reference_date)}
    cases = tables["collection_case"].set_index("case_id")
    for cid in ids_:
        assert cases.at[cid, "case_status"] != "RESOLVED"
        r = prio[cid]
        assert r["priority_category"] == "SUPPRESSED" and r["recommended_action"] == "CLOSE_CASE_AFTER_REVIEW" and "SUP_PAID_OFF" in r["suppression_reasons"]


def test_scenario2_contact_outside_window(ds, tables):
    from collections_ai.contact import ContactContext, ContactRequest
    from collections_ai.delinquency import Ledger
    led = Ledger(tables["loan"], tables["installment"], tables["payment_allocation"])
    ctx = ContactContext(tables["customer"], tables["account"], tables["loan"], tables["contact_preference"], tables["flag_event"], tables["contact_attempt"], led)
    att = tables["contact_attempt"].set_index("attempt_id")
    cases = tables["collection_case"].set_index("case_id")
    for r in ds.ground_truth.query("issue_type == 'CONTACT_OUTSIDE_ALLOWED_WINDOW'").itertuples():
        a = att.loc[r.entity_id]
        dec = ctx.evaluate(ContactRequest(a["case_id"], a["customer_id"], cases.at[a["case_id"], "loan_id"], a["channel"], datetime.fromisoformat(a["scheduled_for"]), a["purpose"]))
        assert not dec.permitted and any(c in dec.reason_codes for c in ("OUTSIDE_TIME_WINDOW", "DAY_NOT_PERMITTED"))


def test_scenario3_wrong_installment(ds, tables):
    from collections_ai.reconciliation import reconcile_payments
    r = reconcile_payments(tables["installment"], tables["payment"], tables["payment_allocation"]).set_index("payment_id")
    for g in ds.ground_truth.query("issue_type == 'PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT'").itertuples():
        assert r.at[g.entity_id, "reconciliation_status"] == "EXCEPTION" and r.at[g.entity_id, "reason"] == "NOT_OLDEST_DUE_FIRST"
        detail = __import__("json").loads(g.injection_detail)
        assert detail["correct_installment"] in r.at[g.entity_id, "expected_installment_ids"]


def test_scenario4_promise_exceeds_arrears(ds, tables):
    p = tables["promise_to_pay"].set_index("promise_id")
    for g in ds.ground_truth.query("issue_type == 'PROMISE_AMOUNT_EXCEEDS_ARREARS'").itertuples():
        assert p.at[g.entity_id, "promised_amount"] > p.at[g.entity_id, "arrears_at_creation"] and pd.isna(p.at[g.entity_id, "exception_approved_by"])


def test_legitimate_exception_workflow_promises_not_flagged(tables, detected):
    approved = tables["promise_to_pay"][tables["promise_to_pay"]["exception_approved_by"].notna()]
    flagged = {d["entity_id"] for d in detected if d["exception_type"] == "PROMISE_AMOUNT_EXCEEDS_ARREARS"}
    assert not (set(approved["promise_id"]) & flagged)


def test_scenario5_disputed_debt_treated_as_confirmed(ds, tables, cfg):
    prio = {r["case_id"]: r for r in prioritize_cases(tables, cfg.reference_date)}
    cases = tables["collection_case"].set_index("case_id")
    for g in ds.ground_truth.query("issue_type == 'DISPUTED_DEBT_TREATED_AS_CONFIRMED'").itertuples():
        assert cases.at[g.entity_id, "debt_confirmation_status"] == "CONFIRMED"
        r = prio[g.entity_id]  # despite the wrong label, the system still protects the customer
        assert r["priority_category"] == "SUPPRESSED" and r["recommended_action"] == "FOLLOW_DISPUTE_VALIDATION_WORKFLOW"


def test_no_ground_truth_leakage_in_engines():
    """Engine modules must not reference ground-truth labels/files or import the injection module."""
    forbidden = re.compile(r"ground_truth|injection|GT-\d|issue_type", re.I)
    for mod in (prioritization, contact, reconciliation, delinquency):
        src = Path(inspect.getsourcefile(mod)).read_text()
        body = re.sub(r"#.*", "", re.sub(r'"""[\s\S]*?"""', "", src))
        assert not forbidden.findall(body), mod.__name__
    # detection may only mention ground truth inside the separate, explicit evaluation helper
    src = Path(inspect.getsourcefile(detection)).read_text()
    detect_part = src.split("def evaluate_against_ground_truth")[0]
    body = re.sub(r"#.*", "", re.sub(r'"""[\s\S]*?"""', "", detect_part))
    assert not forbidden.findall(body)


def test_detection_and_prioritization_ignore_ground_truth_even_if_removed(tables, cfg, detected):
    """Same results whether or not any ground-truth file exists: the engines only accept source tables."""
    assert set(tables) == set(TABLE_BY_NAME) & set(tables)
    again = detect_all({k: v.copy() for k, v in tables.items()}, cfg.reference_date)
    assert [(d["exception_type"], d["entity_id"]) for d in again] == [(d["exception_type"], d["entity_id"]) for d in detected]
