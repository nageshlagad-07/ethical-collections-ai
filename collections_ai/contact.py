"""Ethical contact eligibility engine.

`evaluate_contact` is a pure function: (request, state, rules) -> decision. It
returns ALL violated rules (not just the first) so analysts see the full
picture. Restrictions are applied from *source records only* (preferences,
flag events, ledger, prior attempts) - never from ground-truth labels.

IMPORTANT: jurisdiction parameters are configurable, illustrative placeholders
(config/rules.json). Legal compliance requires review against the regulations
that apply; this module does not encode any real law.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd

from .delinquency import Ledger
from .rules import load_rules

DAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
COLLECTION_PURPOSES = {"COLLECTION_REMINDER", "PROMISE_FOLLOWUP"}
WRITTEN_CHANNELS = {"EMAIL", "POST"}

REASON_TEXT = {
    "PAID_OFF_NO_COLLECTION_CONTACT": "Loan is fully paid; no collection contact is permitted.",
    "ACCOUNT_SUSPENDED": "Servicing account is suspended; outbound contact is blocked pending compliance review.",
    "NO_ACTIVE_PREFERENCE": "No contact preference effective on the requested date.",
    "OPT_OUT_ALL": "Customer opted out of all outbound collection contact.",
    "CHANNEL_NOT_PERMITTED": "Requested channel is not permitted by the customer's preferences.",
    "ACTIVE_DISPUTE_RESTRICTION": "Debt is under active dispute; collection contact is paused until the dispute workflow completes (only dispute correspondence is allowed).",
    "DISPUTE_RESPONSE_MUST_BE_WRITTEN": "Dispute correspondence must use a written channel (EMAIL/POST).",
    "HARDSHIP_REVIEW_REQUIRED": "Customer hardship is active; only hardship-review outreach is allowed (no collection reminders).",
    "DAY_NOT_PERMITTED": "Requested weekday is outside the permitted contact days.",
    "OUTSIDE_TIME_WINDOW": "Requested time is outside the permitted contact window.",
    "BLACKOUT_DATE": "Requested date is a configured blackout date.",
    "DAILY_LIMIT_EXCEEDED": "Daily contact-frequency limit reached.",
    "WEEKLY_LIMIT_EXCEEDED": "Rolling 7-day contact-frequency limit reached.",
}


@dataclass
class ContactRequest:
    case_id: str
    customer_id: str
    loan_id: str
    channel: str
    scheduled_for: datetime
    purpose: str = "COLLECTION_REMINDER"


@dataclass
class ContactState:
    preference: Optional[dict]
    profile: dict
    dispute_active: bool
    hardship_active: bool
    outstanding_minor: int
    account_suspended: bool
    prior_attempts: list  # list[datetime] strictly before the request, non-cancelled


@dataclass
class ContactDecision:
    permitted: bool
    reason_codes: list = field(default_factory=list)
    explanations: list = field(default_factory=list)
    effective_window: Optional[tuple] = None
    rules_version: str = ""

    @property
    def reasons_csv(self) -> str:
        return ",".join(self.reason_codes) if self.reason_codes else "ALL_RULES_PASSED"


def _hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def effective_window(pref: dict, profile: dict) -> tuple[time, time]:
    start = max(_hhmm(pref["window_start"]), _hhmm(profile["window_start"]))
    end = min(_hhmm(pref["window_end"]), _hhmm(profile["window_end"]))
    return start, end


def effective_days(pref: dict, profile: dict) -> set:
    pdays = {x for x in str(pref.get("allowed_days") or "").split(",") if x}
    return pdays & set(profile["allowed_days"])


def evaluate_contact(req: ContactRequest, state: ContactState, rules: Optional[dict] = None) -> ContactDecision:
    rules = rules or load_rules()
    reasons: list[str] = []
    pref, prof, when = state.preference, state.profile, req.scheduled_for
    window = None

    if state.outstanding_minor <= rules["case_policy"]["paid_off_tolerance"] * 100:
        reasons.append("PAID_OFF_NO_COLLECTION_CONTACT")
    if state.account_suspended:
        reasons.append("ACCOUNT_SUSPENDED")

    if pref is None:
        reasons.append("NO_ACTIVE_PREFERENCE")
    else:
        if int(pref["opt_out_all"]):
            reasons.append("OPT_OUT_ALL")
        elif not int(pref[f"allow_{req.channel.lower()}"]):
            reasons.append("CHANNEL_NOT_PERMITTED")

    if state.dispute_active and req.purpose in COLLECTION_PURPOSES:
        reasons.append("ACTIVE_DISPUTE_RESTRICTION")
    if state.dispute_active and req.purpose == "DISPUTE_RESPONSE" and req.channel not in WRITTEN_CHANNELS:
        reasons.append("DISPUTE_RESPONSE_MUST_BE_WRITTEN")
    if state.hardship_active and req.purpose in COLLECTION_PURPOSES:
        reasons.append("HARDSHIP_REVIEW_REQUIRED")

    if pref is not None and req.channel in prof["time_window_channels"]:
        days = effective_days(pref, prof)
        if DAY_CODES[when.weekday()] not in days:
            reasons.append("DAY_NOT_PERMITTED")
        start, end = effective_window(pref, prof)
        window = (start.strftime("%H:%M"), end.strftime("%H:%M"))
        if not (start <= when.time() <= end) or start >= end:
            reasons.append("OUTSIDE_TIME_WINDOW")
    if when.date().isoformat() in prof.get("blackout_dates", []):
        reasons.append("BLACKOUT_DATE")

    same_day = sum(1 for t in state.prior_attempts if t.date() == when.date())
    if same_day >= prof["max_attempts_per_day"]:
        reasons.append("DAILY_LIMIT_EXCEEDED")
    weekly_limit = prof["max_attempts_per_week"]
    if pref is not None and pref.get("max_contacts_per_week") not in (None, "") and not pd.isna(pref.get("max_contacts_per_week")):
        weekly_limit = min(weekly_limit, int(pref["max_contacts_per_week"]))
    in_week = sum(1 for t in state.prior_attempts if when - timedelta(days=7) < t <= when)
    if in_week >= weekly_limit:
        reasons.append("WEEKLY_LIMIT_EXCEEDED")

    return ContactDecision(not reasons, reasons, [REASON_TEXT[r] for r in reasons], window, rules["rules_version"])


# ---------------------------------------------------------------------------
# State builder over the stored tables
# ---------------------------------------------------------------------------
class ContactContext:
    """Indexes the tables needed to build a ContactState for any (customer, loan, time)."""

    def __init__(self, customers: pd.DataFrame, accounts: pd.DataFrame, loans: pd.DataFrame,
                 preferences: pd.DataFrame, flags: pd.DataFrame, attempts: pd.DataFrame,
                 ledger: Ledger, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        self.ledger = ledger
        self.profile_of = dict(zip(customers["customer_id"], customers["jurisdiction_profile"]))
        acct_status = dict(zip(accounts["account_id"], accounts["account_status"]))
        self.suspended_loans = {lid for lid, aid in zip(loans["loan_id"], loans["account_id"])
                                if acct_status.get(aid) == "SUSPENDED"}
        self.prefs: dict[str, list] = {}
        for r in preferences.sort_values(["customer_id", "effective_from"]).to_dict("records"):
            self.prefs.setdefault(r["customer_id"], []).append(r)
        self.flag_events: dict[tuple, list] = {}
        for r in flags.sort_values(["event_at", "flag_event_id"]).itertuples(index=False):
            key = (r.flag_type, r.loan_id if r.flag_type == "DISPUTE" else r.customer_id)
            self.flag_events.setdefault(key, []).append((datetime.fromisoformat(r.event_at), r.event_type))
        self.attempt_times: dict[str, list] = {}
        for r in attempts.sort_values(["scheduled_for", "attempt_id"]).itertuples(index=False):
            if r.attempt_status != "CANCELLED":
                self.attempt_times.setdefault(r.customer_id, []).append(datetime.fromisoformat(r.scheduled_for))

    # -- mutation (used by generator / API after a PERMITTED decision) -------
    def add_attempt(self, customer_id: str, when: datetime) -> None:
        bisect.insort(self.attempt_times.setdefault(customer_id, []), when)

    def add_flag_event(self, flag_type: str, key: str, when: datetime, event_type: str) -> None:
        lst = self.flag_events.setdefault((flag_type, key), [])
        lst.append((when, event_type))
        lst.sort(key=lambda t: t[0])

    def add_preference(self, pref: dict) -> None:
        self.prefs.setdefault(pref["customer_id"], []).append(pref)
        self.prefs[pref["customer_id"]].sort(key=lambda r: r["effective_from"])

    # -- queries ---------------------------------------------------------------
    def preference_on(self, customer_id: str, on: date) -> Optional[dict]:
        iso = on.isoformat()
        found = None
        for p in self.prefs.get(customer_id, []):
            if p["effective_from"] <= iso and (p.get("effective_to") in (None, "") or pd.isna(p.get("effective_to")) or p["effective_to"] >= iso):
                found = p
        return found

    def flag_active(self, flag_type: str, key: str, when: datetime) -> bool:
        active = False
        for ts, ev in self.flag_events.get((flag_type, key), []):
            if ts > when:
                break
            active = ev == "RAISED"
        return active

    def state(self, customer_id: str, loan_id: str, when: datetime, exclude_at_or_after: Optional[datetime] = None) -> ContactState:
        """Build the state as of `when`. Prior attempts are those strictly earlier than `when`."""
        times = self.attempt_times.get(customer_id, [])
        limit = exclude_at_or_after or when
        prior = times[: bisect.bisect_left(times, limit)]
        return ContactState(
            preference=self.preference_on(customer_id, when.date()),
            profile=self.rules["jurisdiction_profiles"][self.profile_of[customer_id]],
            dispute_active=self.flag_active("DISPUTE", loan_id, when),
            hardship_active=self.flag_active("HARDSHIP", customer_id, when),
            outstanding_minor=self.ledger.state(loan_id, when.date()).outstanding,
            account_suspended=loan_id in self.suspended_loans,
            prior_attempts=prior,
        )

    def evaluate(self, req: ContactRequest, exclude_at_or_after: Optional[datetime] = None) -> ContactDecision:
        st = self.state(req.customer_id, req.loan_id, req.scheduled_for, exclude_at_or_after)
        return evaluate_contact(req, st, self.rules)

    def next_permissible_slot(self, customer_id: str, loan_id: str, case_id: str, purpose: str,
                              after: datetime, horizon_days: Optional[int] = None):
        """Earliest (datetime, channel) that passes every rule within the horizon, else None."""
        horizon = horizon_days or self.rules["case_policy"]["lookahead_days_for_slot"]
        pref = self.preference_on(customer_id, after.date())
        if pref is None or int(pref["opt_out_all"]):
            return None
        prof = self.rules["jurisdiction_profiles"][self.profile_of[customer_id]]
        order = [pref["preferred_channel"]] + [c for c in ("PHONE", "SMS", "EMAIL", "POST") if c != pref["preferred_channel"]]
        order = [c for c in order if int(pref[f"allow_{c.lower()}"])]
        start, end = effective_window(pref, prof)
        for off in range(1, horizon + 1):
            day = after.date() + timedelta(days=off)
            for ch in order:
                if ch in prof["time_window_channels"]:
                    if start >= end:
                        continue
                    cand = datetime.combine(day, start) + timedelta(minutes=60 if (datetime.combine(day, end) - datetime.combine(day, start)) > timedelta(hours=2) else 0)
                else:
                    cand = datetime.combine(day, time(10, 0))
                dec = self.evaluate(ContactRequest(case_id, customer_id, loan_id, ch, cand, purpose))
                if dec.permitted:
                    return cand, ch
        return None
