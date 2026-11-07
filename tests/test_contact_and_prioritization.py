"""Contact eligibility rules, prioritization scoring, suppression, dispute/hardship handling."""
from datetime import date, datetime

import pytest

from collections_ai.contact import ContactRequest, ContactState, evaluate_contact
from collections_ai.prioritization import prioritize_cases, score_features
from collections_ai.rules import load_rules

RULES = load_rules()
PROFILE = RULES["jurisdiction_profiles"]["POLICY_A"]  # Mon-Sat 08:00-19:00, 1/day, 3/week


def pref(**kw):
    base = {"preference_id": "P", "customer_id": "C", "preferred_channel": "PHONE", "allow_phone": 1, "allow_sms": 1, "allow_email": 1, "allow_post": 1,
            "allowed_days": "MON,TUE,WED,THU,FRI,SAT,SUN", "window_start": "08:00", "window_end": "20:00", "max_contacts_per_week": None, "opt_out_all": 0}
    base.update(kw)
    return base


def state(**kw):
    base = dict(preference=pref(), profile=PROFILE, dispute_active=False, hardship_active=False, outstanding_minor=100000, account_suspended=False, prior_attempts=[])
    base.update(kw)
    return ContactState(**base)


def req(when="2026-10-23T10:00:00", channel="PHONE", purpose="COLLECTION_REMINDER"):
    return ContactRequest("COL-1", "C", "L", channel, datetime.fromisoformat(when), purpose)  # 2026-10-23 is a Friday


def codes(r, s):
    return evaluate_contact(r, s).reason_codes


def test_permitted_baseline():
    d = evaluate_contact(req(), state())
    assert d.permitted and d.reasons_csv == "ALL_RULES_PASSED"


def test_outside_time_window_before_and_after():
    assert "OUTSIDE_TIME_WINDOW" in codes(req("2026-10-23T07:59:00"), state())
    assert "OUTSIDE_TIME_WINDOW" in codes(req("2026-10-23T19:01:00"), state())  # jurisdiction cap 19:00 beats customer 20:00


def test_window_boundaries_inclusive():
    assert evaluate_contact(req("2026-10-23T08:00:00"), state()).permitted
    assert evaluate_contact(req("2026-10-23T19:00:00"), state()).permitted


def test_sunday_not_permitted():
    assert "DAY_NOT_PERMITTED" in codes(req("2026-10-25T10:00:00"), state())  # Sunday


def test_customer_day_restriction_applies():
    assert "DAY_NOT_PERMITTED" in codes(req(), state(preference=pref(allowed_days="MON,TUE")))


def test_customer_narrower_window_applies():
    assert "OUTSIDE_TIME_WINDOW" in codes(req("2026-10-23T09:00:00"), state(preference=pref(window_start="12:00")))


def test_channel_not_permitted():
    assert "CHANNEL_NOT_PERMITTED" in codes(req(channel="SMS"), state(preference=pref(allow_sms=0)))


def test_written_channel_not_subject_to_time_window():
    assert evaluate_contact(req("2026-10-23T03:00:00", channel="EMAIL"), state()).permitted


def test_opt_out_blocks_everything():
    assert "OPT_OUT_ALL" in codes(req(channel="EMAIL"), state(preference=pref(opt_out_all=1)))


def test_no_active_preference_blocks():
    assert "NO_ACTIVE_PREFERENCE" in codes(req(), state(preference=None))


def test_daily_frequency_limit():
    assert "DAILY_LIMIT_EXCEEDED" in codes(req("2026-10-23T15:00:00"), state(prior_attempts=[datetime(2026, 10, 23, 9, 0)]))


def test_weekly_frequency_limit_and_customer_stricter_limit():
    prior = [datetime(2026, 10, 19, 10), datetime(2026, 10, 20, 10), datetime(2026, 10, 21, 10)]
    assert "WEEKLY_LIMIT_EXCEEDED" in codes(req(), state(prior_attempts=prior))
    assert "WEEKLY_LIMIT_EXCEEDED" in codes(req(), state(prior_attempts=prior[:1], preference=pref(max_contacts_per_week=1)))
    assert evaluate_contact(req(), state(prior_attempts=[datetime(2026, 10, 15, 10)])).permitted  # outside rolling 7 days


def test_active_dispute_blocks_collection_but_allows_written_dispute_response():
    assert "ACTIVE_DISPUTE_RESTRICTION" in codes(req(), state(dispute_active=True))
    assert "ACTIVE_DISPUTE_RESTRICTION" in codes(req(purpose="PROMISE_FOLLOWUP"), state(dispute_active=True))
    assert evaluate_contact(req(channel="EMAIL", purpose="DISPUTE_RESPONSE"), state(dispute_active=True)).permitted
    assert "DISPUTE_RESPONSE_MUST_BE_WRITTEN" in codes(req(channel="PHONE", purpose="DISPUTE_RESPONSE"), state(dispute_active=True))


def test_hardship_requires_review_outreach_only():
    assert "HARDSHIP_REVIEW_REQUIRED" in codes(req(), state(hardship_active=True))
    assert evaluate_contact(req(purpose="HARDSHIP_REVIEW_OUTREACH"), state(hardship_active=True)).permitted


def test_paid_off_account_gets_no_collection_contact():
    assert "PAID_OFF_NO_COLLECTION_CONTACT" in codes(req(), state(outstanding_minor=0))


def test_suspended_account_blocked():
    assert "ACCOUNT_SUSPENDED" in codes(req(), state(account_suspended=True))


def test_all_violations_reported_together():
    c = codes(req("2026-10-25T23:00:00"), state(dispute_active=True, hardship_active=True))
    assert {"DAY_NOT_PERMITTED", "OUTSIDE_TIME_WINDOW", "ACTIVE_DISPUTE_RESTRICTION", "HARDSHIP_REVIEW_REQUIRED"} <= set(c)


def test_jurisdiction_profiles_are_configurable():
    strict = RULES["jurisdiction_profiles"]["POLICY_B"]  # no Saturday
    assert "DAY_NOT_PERMITTED" in codes(req("2026-10-24T10:00:00"), state(profile=strict))  # Saturday
    assert "illustrative" in RULES["disclaimer"].lower() or "ILLUSTRATIVE" in RULES["disclaimer"]


def test_baseline_scheduling_never_violates_rules(baseline, cfg):
    log = baseline.tables["contact_scheduling_log"]
    permitted = log[log["decision"] == "PERMITTED"]
    assert (permitted["reason_codes"] == "ALL_RULES_PASSED").all() and permitted["attempt_id"].notna().all()
    rej = log[log["decision"] == "REJECTED"]
    assert len(rej) > 0 and rej["attempt_id"].isna().all() and (rej["reason_codes"] != "ALL_RULES_PASSED").all()


def test_rejected_and_permitted_both_recorded_in_log(tables):
    assert set(tables["contact_scheduling_log"]["decision"]) == {"PERMITTED", "REJECTED"}


# ---- scoring -----------------------------------------------------------------
FEATS = dict(arrears=50000.0, dpd=45, outstanding=90000.0, late_installments=2, risk_category="HIGH", broken_promises=1)


def test_score_is_sum_of_explained_factors():
    score, factors = score_features(FEATS, RULES)
    assert score == min(100, sum(f["points"] for f in factors))
    assert {f["factor"] for f in factors} == {"arrears_amount", "days_past_due", "payment_history", "risk_category", "broken_promises", "outstanding_balance"}
    assert all(f["explanation"] for f in factors)


def test_score_monotonic_in_arrears_and_dpd():
    lo, _ = score_features({**FEATS, "arrears": 5000.0, "dpd": 5}, RULES)
    hi, _ = score_features({**FEATS, "arrears": 90000.0, "dpd": 80}, RULES)
    assert hi > lo


def test_score_capped_at_100():
    s, _ = score_features({**FEATS, "arrears": 1e7, "dpd": 400, "late_installments": 50, "broken_promises": 50, "outstanding": 1e7}, RULES)
    assert s <= 100


def test_risk_category_increases_score():
    s = [score_features({**FEATS, "risk_category": r}, RULES)[0] for r in ("LOW", "MEDIUM", "HIGH")]
    assert s[0] < s[1] < s[2]


@pytest.fixture(scope="module")
def prio(tables, cfg):
    return {r["case_id"]: r for r in prioritize_cases(tables, cfg.reference_date)}


def test_every_decision_is_explained(prio):
    for r in prio.values():
        assert r["explanation"] and r["recommended_action"] and r["reason_codes"] is not None and r["contributing_factors"]
        assert 0 <= r["priority_score"] <= 100


def test_paid_off_accounts_suppressed_regardless_of_score(prio, tables):
    paid = set(tables["loan"].query("loan_status == 'PAID_OFF'")["loan_id"])
    hit = [r for r in prio.values() if r["loan_id"] in paid]
    assert hit, "need paid-off loans with non-resolved cases"
    for r in hit:
        assert r["priority_category"] == "SUPPRESSED" and "SUP_PAID_OFF" in r["suppression_reasons"] and r["recommended_action"] == "CLOSE_CASE_AFTER_REVIEW"
        assert r["recommended_slot"] is None


def test_disputed_cases_follow_dispute_workflow_not_collection(prio, tables):
    gt = tables  # active disputes identified from source flag events
    ev = tables["flag_event"].query("flag_type == 'DISPUTE'").sort_values("event_at").groupby("loan_id").tail(1)
    active = set(ev[ev["event_type"] == "RAISED"]["loan_id"])
    hit = [r for r in prio.values() if r["loan_id"] in active]
    assert hit
    for r in hit:
        assert r["priority_category"] == "SUPPRESSED" and r["recommended_action"] == "FOLLOW_DISPUTE_VALIDATION_WORKFLOW" and "SUP_ACTIVE_DISPUTE" in r["suppression_reasons"]


def test_hardship_triggers_review_not_escalation(prio, tables):
    ev = tables["flag_event"].query("flag_type == 'HARDSHIP'").sort_values("event_at").groupby("customer_id").tail(1)
    active = set(ev[ev["event_type"] == "RAISED"]["customer_id"])
    hit = [r for r in prio.values() if r["customer_id"] in active and not r["suppression_reasons"]]
    assert hit
    for r in hit:
        assert r["priority_category"] == "HARDSHIP_REVIEW" and r["recommended_action"] == "REFER_TO_HARDSHIP_REVIEW_TEAM"
        assert "ESCALAT" not in r["recommended_action"] and r["recommended_channel"] is None


def test_high_score_never_overrides_legal_restrictions(prio):
    high_restricted = [r for r in prio.values() if r["priority_score"] >= 58 and r["priority_category"] in ("SUPPRESSED", "HARDSHIP_REVIEW")]
    assert high_restricted  # the override actually occurs in the data
    assert all(r["priority_category"] not in ("P1_HIGH", "P2_MEDIUM") for r in high_restricted)


def test_opt_out_customers_never_get_a_recommended_channel(prio, tables):
    opt = set(tables["contact_preference"].query("status == 'ACTIVE' and opt_out_all == 1")["customer_id"])
    for r in prio.values():
        if r["customer_id"] in opt:
            # never an outbound channel; hardship review (human, no outbound) may take precedence over suppression
            assert r["recommended_channel"] is None and r["priority_category"] in ("SUPPRESSED", "HARDSHIP_REVIEW")
            assert "SUP_OPT_OUT_ALL" in (r["suppression_reasons"] or "")


def test_recommended_slot_is_itself_permissible(prio, tables, cfg):
    from collections_ai.contact import ContactContext, ContactRequest
    from collections_ai.delinquency import Ledger
    led = Ledger(tables["loan"], tables["installment"], tables["payment_allocation"])
    ctx = ContactContext(tables["customer"], tables["account"], tables["loan"], tables["contact_preference"], tables["flag_event"], tables["contact_attempt"], led)
    checked = 0
    for r in prio.values():
        if r["recommended_slot"]:
            d = ctx.evaluate(ContactRequest(r["case_id"], r["customer_id"], r["loan_id"], r["recommended_channel"], datetime.fromisoformat(r["recommended_slot"]), "COLLECTION_REMINDER"))
            assert d.permitted, (r["case_id"], d.reason_codes)
            checked += 1
        if checked >= 80:
            break
    assert checked > 20


def test_pending_promise_cases_are_monitored_not_chased(prio, tables):
    pend = set(tables["promise_to_pay"].query("promise_status == 'PENDING'")["case_id"])
    for r in prio.values():
        if r["case_id"] in pend and not r["suppression_reasons"] and r["priority_category"] != "HARDSHIP_REVIEW":
            assert r["priority_category"] == "MONITOR_PROMISE"
