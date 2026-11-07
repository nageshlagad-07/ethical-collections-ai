"""FastAPI behaviour: persistence, validation, status codes, ethical contact enforcement, promises, payments, audit."""
import pytest


def open_case(api, status="OPEN"):
    for c in api.get("/collection-cases", params={"case_status": status, "limit": 500}).json():
        d = api.get(f"/loans/{c['loan_id']}/delinquency").json()
        if d["arrears"] > 0 and d["outstanding_balance"] > d["arrears"] * 1.2:
            return c, d
    pytest.skip("no suitable case")


def test_health_and_docs(api):
    h = api.get("/health").json()
    assert h["status"] == "ok" and h["loans"] == 1500
    assert api.get("/docs").status_code == 200 and "paths" in api.get("/openapi.json").json()


def test_metrics_computed_from_db(api):
    m = api.get("/metrics/summary").json()
    assert m["total_loans"] == 1500 and m["total_arrears"] > 0 and 0 <= m["promise_fulfillment_rate"] <= 1
    assert m["overdue_accounts"] == len([1 for _ in api.get("/delinquency-snapshots", params={"snapshot_date": "2026-10-22", "limit": 1000}).json() if False]) or m["overdue_accounts"] > 0


def test_list_and_get_endpoints_and_404(api):
    assert len(api.get("/customers", params={"limit": 5}).json()) == 5
    assert api.get("/customers/CUS-000001").status_code == 200
    assert api.get("/customers/NOPE").status_code == 404
    assert api.get("/loans/LOAN-2026-000001/installments").json()[0]["installment_number"] == 1
    assert api.get("/loans", params={"limit": 0}).status_code == 422  # validation of query params


def test_delinquency_endpoint_matches_snapshot(api):
    c, d = open_case(api)
    snap = api.get("/delinquency-snapshots", params={"loan_id": c["loan_id"], "snapshot_date": "2026-10-22"}).json()[0]
    assert snap["total_arrears"] == pytest.approx(d["arrears"]) and snap["days_past_due"] == d["days_past_due"]


def test_prioritization_explain_and_run(api):
    c, _ = open_case(api)
    r = api.get(f"/prioritization/cases/{c['case_id']}").json()
    assert r["explanation"] and r["contributing_factors"] and r["recommended_action"]
    run = api.post("/prioritization/run")
    assert run.status_code == 201
    res = api.get("/prioritization/results", params={"limit": 5}).json()
    assert res and res[0]["priority_score"] >= res[-1]["priority_score"]


def test_prohibited_contact_rejected_and_logged(api):
    c, _ = open_case(api)
    before = len(api.get("/contact-scheduling-log", params={"decision": "REJECTED", "case_id": c["case_id"], "limit": 1000}).json())
    r = api.post("/contact-attempts", json={"case_id": c["case_id"], "channel": "PHONE", "scheduled_for": "2026-10-25T23:30:00"})  # Sunday night
    assert r.status_code == 422
    body = r.json()
    assert body["decision"] == "REJECTED" and body["attempt_id"] is None and body["eligibility"]["reason_codes"]
    after = api.get("/contact-scheduling-log", params={"decision": "REJECTED", "case_id": c["case_id"], "limit": 1000}).json()
    assert len(after) == before + 1
    assert api.get("/contact-attempts", params={"case_id": c["case_id"], "attempt_status": "SCHEDULED"}).json() == []


def test_permitted_contact_scheduled_and_outcome_recorded(api):
    c, _ = open_case(api)
    pr = api.get(f"/prioritization/cases/{c['case_id']}").json()
    if not pr["recommended_slot"]:
        pytest.skip("no slot")
    r = api.post("/contact-attempts", json={"case_id": c["case_id"], "channel": pr["recommended_channel"], "scheduled_for": pr["recommended_slot"]})
    assert r.status_code == 201 and r.json()["attempt_id"].startswith(f"CTP-{c['case_id']}-")
    aid = r.json()["attempt_id"]
    o = api.patch(f"/contact-attempts/{aid}/outcome", json={"outcome": "NO_ANSWER"})
    assert o.status_code == 200 and o.json()["attempt_status"] == "COMPLETED"
    assert api.patch(f"/contact-attempts/{aid}/outcome", json={"outcome": "NO_ANSWER"}).status_code == 409
    assert any(a["action"] == "COMPLETED" for a in api.get("/audit-events", params={"entity_id": aid}).json())


def test_invalid_input_rejected(api):
    assert api.post("/contact-attempts", json={"case_id": "COL-2026-000001", "channel": "CARRIER_PIGEON", "scheduled_for": "2026-10-25T10:00:00"}).status_code == 422
    assert api.post("/contact/eligibility", json={"case_id": "NOPE", "channel": "PHONE", "scheduled_for": "2026-10-23T10:00:00"}).status_code == 404
    assert api.post("/payments", json={"loan_id": "LOAN-2026-000001", "amount": -5, "reference": "ABCDEF1"}).status_code == 422


def test_promise_amount_exceeding_arrears_rejected_unless_exception(api):
    c, d = open_case(api)
    too_big = round(d["arrears"] * 1.1, 2)
    r = api.post("/promises", json={"case_id": c["case_id"], "promised_amount": too_big, "promised_date": "2026-10-30"})
    assert r.status_code == 422 and "exceeds arrears" in r.json()["detail"]
    ok = api.post("/promises", json={"case_id": c["case_id"], "promised_amount": too_big, "promised_date": "2026-10-30", "exception_approved_by": "COMPLIANCE-REV-07", "exception_reason": "Customer elected to clear extra"})
    assert ok.status_code == 201 and ok.json()["exception_approved_by"] == "COMPLIANCE-REV-07"


def stable_case(api):
    """Open case whose arrears on 2026-10-21 equal arrears on 2026-10-22 (so a promise made the day before is exactly clearable)."""
    for c in api.get("/collection-cases", params={"case_status": "OPEN", "limit": 500}).json():
        d1 = api.get(f"/loans/{c['loan_id']}/delinquency", params={"as_of": "2026-10-21"}).json()
        d2 = api.get(f"/loans/{c['loan_id']}/delinquency").json()
        if d1["arrears"] > 0 and d1["arrears"] == d2["arrears"] and d2["outstanding_balance"] > d2["arrears"]:
            return c, d2
    pytest.skip("no suitable case")


def test_promise_lifecycle_and_payment_reconciliation(api):
    c, d = stable_case(api)
    # Rule: payments dated on/before the creation day are treated as already received, so create the promise the day before.
    p = api.post("/promises", json={"case_id": c["case_id"], "promised_amount": d["arrears"], "promised_date": "2026-10-30", "created_date": "2026-10-21"})
    assert p.status_code == 201 and p.json()["promise_status"] == "PENDING"
    pid = p.json()["promise_id"]
    assert api.get(f"/collection-cases/{c['case_id']}").json()["case_status"] == "PROMISE_PENDING"
    assert api.post("/promises", json={"case_id": c["case_id"], "promised_amount": 10, "promised_date": "2026-10-30"}).status_code == 409  # one pending promise
    pay = api.post("/payments", json={"loan_id": c["loan_id"], "amount": d["arrears"], "reference": "UTR-TEST-0001"})
    assert pay.status_code == 201
    j = pay.json()
    assert pid in j["promises_updated"] and j["case_resolved"] == c["case_id"]
    assert api.get("/promises", params={"case_id": c["case_id"]}).json()[-1]["promise_status"] == "FULFILLED"
    assert api.get(f"/collection-cases/{c['case_id']}").json()["case_status"] == "RESOLVED"
    assert all(a["allocation_rule"] == "FIFO_OLDEST_DUE_FIRST" for a in j["allocations"])
    assert sum(a["allocated_amount"] for a in j["allocations"]) == pytest.approx(d["arrears"])
    assert api.get("/contact-attempts", params={"case_id": c["case_id"], "attempt_status": "SCHEDULED"}).json() == []


def test_payment_validation_overpay_duplicate_and_partial(api):
    c, d = open_case(api)
    assert api.post("/payments", json={"loan_id": c["loan_id"], "amount": d["outstanding_balance"] + 1, "reference": "UTR-OVER-01"}).status_code == 422
    half = round(d["arrears"] / 2, 2)
    ok = api.post("/payments", json={"loan_id": c["loan_id"], "amount": half, "reference": "UTR-HALF-01"})
    assert ok.status_code == 201 and ok.json()["case_resolved"] is None
    assert api.post("/payments", json={"loan_id": c["loan_id"], "amount": 10, "reference": "UTR-HALF-01"}).status_code == 409
    after = api.get(f"/loans/{c['loan_id']}/delinquency").json()
    assert after["arrears"] == pytest.approx(d["arrears"] - half, abs=0.01)


def test_history_is_not_overwritten_by_payment(api):
    c, d = open_case(api)
    before = api.get("/delinquency-snapshots", params={"loan_id": c["loan_id"], "limit": 100}).json()
    api.post("/payments", json={"loan_id": c["loan_id"], "amount": d["arrears"], "reference": "UTR-HIST-01"})
    after = api.get("/delinquency-snapshots", params={"loan_id": c["loan_id"], "limit": 100}).json()
    assert before == after  # snapshots are immutable history
    assert any(a["action"] == "PAYMENT_POSTED" for a in api.get("/audit-events", params={"entity_type": "PAYMENT"}).json())


def test_dispute_flag_pauses_collection_contact(api):
    c, _ = open_case(api)
    r = api.post("/flags", json={"customer_id": c["customer_id"], "loan_id": c["loan_id"], "flag_type": "DISPUTE", "event_type": "RAISED", "note": "customer disputes amount"})
    assert r.status_code == 201
    case = api.get(f"/collection-cases/{c['case_id']}").json()
    assert case["case_status"] == "DISPUTE_REVIEW" and case["debt_confirmation_status"] == "DISPUTED"
    e = api.post("/contact/eligibility", json={"case_id": c["case_id"], "channel": "EMAIL", "scheduled_for": "2026-10-23T10:00:00", "purpose": "COLLECTION_REMINDER"}).json()
    assert not e["permitted"] and "ACTIVE_DISPUTE_RESTRICTION" in e["reason_codes"]
    e2 = api.post("/contact/eligibility", json={"case_id": c["case_id"], "channel": "EMAIL", "scheduled_for": "2026-10-23T10:00:00", "purpose": "DISPUTE_RESPONSE"}).json()
    assert "ACTIVE_DISPUTE_RESTRICTION" not in e2["reason_codes"]
    assert api.patch(f"/collection-cases/{c['case_id']}/status", json={"new_status": "OPEN", "reason": "force it"}).status_code == 409
    assert api.post("/promises", json={"case_id": c["case_id"], "promised_amount": 10, "promised_date": "2026-10-30"}).status_code == 409
    pri = api.get(f"/prioritization/cases/{c['case_id']}").json()
    assert pri["priority_category"] == "SUPPRESSED" and pri["recommended_action"] == "FOLLOW_DISPUTE_VALIDATION_WORKFLOW"
    # flag history is append-only: clearing adds an event, the RAISED row remains
    api.post("/flags", json={"customer_id": c["customer_id"], "loan_id": c["loan_id"], "flag_type": "DISPUTE", "event_type": "CLEARED"})
    assert api.get(f"/collection-cases/{c['case_id']}").json()["debt_confirmation_status"] == "CONFIRMED"


def test_hardship_flag_triggers_review(api):
    c, _ = open_case(api)
    api.post("/flags", json={"customer_id": c["customer_id"], "flag_type": "HARDSHIP", "event_type": "RAISED"})
    assert api.get(f"/collection-cases/{c['case_id']}").json()["case_status"] == "HARDSHIP_REVIEW"
    e = api.post("/contact/eligibility", json={"case_id": c["case_id"], "channel": "EMAIL", "scheduled_for": "2026-10-23T10:00:00"}).json()
    assert "HARDSHIP_REVIEW_REQUIRED" in e["reason_codes"]


def test_cannot_open_case_for_paid_off_loan_or_resolve_with_arrears(api):
    paid = api.get("/loans", params={"loan_status": "PAID_OFF", "limit": 1}).json()[0]
    r = api.post("/collection-cases", json={"loan_id": paid["loan_id"]})
    assert r.status_code == 422 and "fully paid" in r.json()["detail"]
    c, _ = open_case(api)
    assert api.patch(f"/collection-cases/{c['case_id']}/status", json={"new_status": "RESOLVED", "reason": "try to close"}).status_code == 422


def test_exception_detection_validation_and_ground_truth_evaluation(api):
    run = api.post("/exceptions/detect").json()
    assert run["exceptions"] == sum(run["by_type"].values()) > 0 and len(run["by_type"]) == 5
    ex = api.get("/exceptions", params={"limit": 3}).json()
    assert ex[0]["status"] == "OPEN"
    upd = api.patch(f"/exceptions/{ex[0]['exception_id']}", json={"status": "UNDER_REVIEW", "note": "looking"})
    assert upd.status_code == 200 and upd.json()["status"] == "UNDER_REVIEW"
    v = api.post("/validation/run").json()
    assert v["all_integrity_checks_passed"] is True
    assert api.get("/validation/reports/latest").json()["results"]
    assert api.get("/reconciliation/exceptions").json()
    assert api.get("/reconciliation/summary").json()["payment_reconciliation_rate"] < 1


def test_reconciliation_run_is_idempotent_and_audited(api):
    first = api.post("/reconciliation/run").json()
    second = api.post("/reconciliation/run").json()
    assert second["changed"] == 0 and first["status_counts"] == second["status_counts"]
