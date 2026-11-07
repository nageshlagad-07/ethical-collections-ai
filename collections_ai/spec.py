"""Single source of truth for entity definitions.

The SQL schema (sql/schema.sql), the CSV column order, the validators and the
data dictionary (docs/DATA_DICTIONARY.md) are all generated from / driven by the
specifications in this module so they cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ----------------------------------------------------------------------------
# Enumerations (allowed values)
# ----------------------------------------------------------------------------
JURISDICTION_PROFILES = ["POLICY_A", "POLICY_B", "POLICY_C"]
RISK_CATEGORIES = ["LOW", "MEDIUM", "HIGH"]
CHANNELS = ["PHONE", "SMS", "EMAIL", "POST"]
PURPOSES = ["COLLECTION_REMINDER", "PROMISE_FOLLOWUP", "DISPUTE_RESPONSE", "HARDSHIP_REVIEW_OUTREACH"]
LOAN_STATUSES = ["CURRENT", "DELINQUENT", "DEFAULT", "PAID_OFF"]
BUCKETS = ["PAID_OFF", "CURRENT", "DPD_1_30", "DPD_31_60", "DPD_61_90", "DPD_90_PLUS"]
INSTALLMENT_STATUSES = ["NOT_DUE", "PAID", "PARTIALLY_PAID", "OVERDUE"]
CASE_STATUSES = ["OPEN", "PROMISE_PENDING", "DISPUTE_REVIEW", "HARDSHIP_REVIEW", "RESOLVED"]
DEBT_STATUSES = ["UNVERIFIED", "DISPUTED", "CONFIRMED"]
ATTEMPT_STATUSES = ["SCHEDULED", "COMPLETED", "CANCELLED"]
OUTCOMES = ["NO_ANSWER", "MESSAGE_SENT", "BOUNCED", "REACHED_NO_COMMITMENT",
            "REACHED_PROMISE_MADE", "REACHED_DISPUTE_RAISED", "REACHED_HARDSHIP_DECLARED"]
PROMISE_STATUSES = ["PENDING", "FULFILLED", "PARTIALLY_FULFILLED", "BROKEN"]
RECON_STATUSES = ["UNRECONCILED", "RECONCILED", "EXCEPTION"]
PAYMENT_CHANNELS = ["BANK_TRANSFER", "UPI", "CARD", "CASH_DEPOSIT", "AUTO_DEBIT"]
ALLOCATION_RULES = ["FIFO_OLDEST_DUE_FIRST"]
FLAG_TYPES = ["HARDSHIP", "DISPUTE"]
FLAG_EVENT_TYPES = ["RAISED", "CLEARED"]
FLAG_SOURCES = ["CUSTOMER_CONTACT", "ANALYST", "SYSTEM"]
ISSUE_TYPES = [
    "PAID_ACCOUNT_IN_COLLECTION",
    "CONTACT_OUTSIDE_ALLOWED_WINDOW",
    "PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT",
    "PROMISE_AMOUNT_EXCEEDS_ARREARS",
    "DISPUTED_DEBT_TREATED_AS_CONFIRMED",
]
EXCEPTION_STATUSES = ["OPEN", "UNDER_REVIEW", "RESOLVED"]
SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
RUN_TYPES = ["PRIORITIZATION", "EXCEPTION_DETECTION", "VALIDATION", "RECONCILIATION", "DATA_LOAD"]
PRIORITY_CATEGORIES = ["P1_HIGH", "P2_MEDIUM", "P3_LOW", "HARDSHIP_REVIEW", "MONITOR_PROMISE", "SUPPRESSED"]
AUDIT_ENTITY_TYPES = ["CASE", "PROMISE", "PAYMENT", "CONTACT_ATTEMPT", "EXCEPTION", "RUN", "FLAG", "LOAN"]
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


@dataclass
class Field:
    name: str
    type: str  # TEXT | INTEGER | REAL | DATE | DATETIME | BOOL | JSON
    required: bool = True
    allowed: Optional[list] = None
    unit: str = ""
    unique: bool = False
    rule: str = ""
    fk: Optional[str] = None  # "table.column"
    check: Optional[str] = None  # raw SQL boolean expression on this column


@dataclass
class Table:
    name: str
    pk: str
    description: str
    fields: list
    core: bool = True  # one of the ten mandated entities
    unique_together: list = field(default_factory=list)
    table_checks: list = field(default_factory=list)
    indexes: list = field(default_factory=list)
    cardinality: list = field(default_factory=list)
    lifecycle: str = ""

    @property
    def columns(self) -> list:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)


TS = [
    Field("created_at", "DATETIME", True, rule="Record creation timestamp (generated data: deterministic business timestamp)"),
    Field("updated_at", "DATETIME", True, rule="Last update timestamp (>= created_at)"),
]

TABLES: list[Table] = [
    Table(
        "customer", "customer_id",
        "Synthetic borrower. No real personal data: names are generated, email uses the reserved .invalid TLD, phone numbers use a clearly fictional pattern.",
        [
            Field("customer_id", "TEXT", rule="Pattern CUS-NNNNNN; immutable", unique=True),
            Field("full_name", "TEXT", rule="Synthetic name"),
            Field("date_of_birth", "DATE", rule="Age >= 21 at reference date"),
            Field("email", "TEXT", unique=True, rule="Synthetic address @example.invalid"),
            Field("phone", "TEXT", rule="Fictional pattern +000-NNNNNNNNNN"),
            Field("jurisdiction_profile", "TEXT", allowed=JURISDICTION_PROFILES,
                  rule="Key into config/rules.json jurisdiction_profiles (illustrative, not legal advice)"),
            Field("credit_score", "INTEGER", unit="points", check="credit_score BETWEEN 300 AND 900"),
            Field("risk_category", "TEXT", allowed=RISK_CATEGORIES,
                  rule="Derived from credit_score: >=700 LOW, 600-699 MEDIUM, <600 HIGH"),
            Field("customer_since", "DATE"),
        ] + TS,
        cardinality=["Customer 1:N Account", "Customer 1:N Loan (denormalised, must equal account.customer_id)",
                     "Customer 1:N ContactPreference (history; one active at a time)", "Customer 1:N Payment"],
        lifecycle="Static master record.",
    ),
    Table(
        "account", "account_id",
        "Servicing/billing account owned by a customer. One account can service several loans.",
        [
            Field("account_id", "TEXT", rule="Pattern ACC-NNNNNN", unique=True),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("account_type", "TEXT", allowed=["SALARY_LINKED", "SAVINGS_LINKED", "CURRENT_LINKED"]),
            Field("repayment_method", "TEXT", allowed=["AUTO_DEBIT", "MANUAL_TRANSFER"]),
            Field("account_status", "TEXT", allowed=["ACTIVE", "SUSPENDED", "CLOSED"],
                  rule="CLOSED requires closed_date; SUSPENDED accounts are excluded from outbound contact"),
            Field("opened_date", "DATE"),
            Field("closed_date", "DATE", False, rule=">= opened_date; NOT NULL iff account_status = CLOSED"),
            Field("currency", "TEXT", allowed=["INR"], unit="ISO 4217"),
        ] + TS,
        table_checks=["closed_date IS NULL OR closed_date >= opened_date",
                      "((account_status = 'CLOSED') = (closed_date IS NOT NULL))"],
        indexes=["customer_id"],
        cardinality=["Account N:1 Customer", "Account 1:N Loan"],
        lifecycle="ACTIVE -> SUSPENDED -> ACTIVE, ACTIVE -> CLOSED (terminal).",
    ),
    Table(
        "loan", "loan_id",
        "Instalment loan. Contractual model: flat interest, equal monthly instalments (30-day spacing), last instalment absorbs rounding.",
        [
            Field("loan_id", "TEXT", rule="Pattern LOAN-2026-NNNNNN", unique=True),
            Field("account_id", "TEXT", fk="account.account_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id", rule="Must equal account.customer_id"),
            Field("product_type", "TEXT", allowed=["PERSONAL_LOAN", "CONSUMER_DURABLE", "SMALL_BUSINESS", "EDUCATION"]),
            Field("principal_amount", "REAL", unit="INR", check="principal_amount > 0"),
            Field("interest_rate_pct", "REAL", unit="% p.a.", check="interest_rate_pct >= 0"),
            Field("tenor_installments", "INTEGER", unit="count", check="tenor_installments >= 1"),
            Field("total_payable", "REAL", unit="INR", rule="= SUM(installment.amount_due) (arithmetic check)"),
            Field("origination_date", "DATE"),
            Field("first_due_date", "DATE", rule="= origination_date + 30 days"),
            Field("maturity_date", "DATE", rule="= due date of last instalment"),
            Field("loan_status", "TEXT", allowed=LOAN_STATUSES,
                  rule="DERIVED as of status_as_of from instalments+allocations (see BUSINESS_RULES)"),
            Field("status_as_of", "DATE", rule="Business reference date used to derive loan_status"),
            Field("payoff_date", "DATE", False, rule="Date of final allocation; NOT NULL iff loan_status = PAID_OFF"),
        ] + TS,
        table_checks=["((loan_status = 'PAID_OFF') = (payoff_date IS NOT NULL))",
                      "maturity_date >= first_due_date", "first_due_date > origination_date"],
        indexes=["account_id", "customer_id", "loan_status", "origination_date"],
        cardinality=["Loan N:1 Account", "Loan 1:N Installment (exactly tenor_installments)", "Loan 1:N DelinquencySnapshot",
                     "Loan 1:N Payment", "Loan 1:N CollectionCase (at most one non-RESOLVED at a time)"],
        lifecycle="CURRENT <-> DELINQUENT -> DEFAULT (DPD>90); any -> PAID_OFF (terminal).",
    ),
    Table(
        "installment", "installment_id",
        "Scheduled repayment. amount_paid / status / fully_paid_date are derived from payment_allocation.",
        [
            Field("installment_id", "TEXT", rule="Pattern INS-<loan_id>-NNN", unique=True),
            Field("loan_id", "TEXT", fk="loan.loan_id"),
            Field("installment_number", "INTEGER", check="installment_number >= 1"),
            Field("due_date", "DATE"),
            Field("amount_due", "REAL", unit="INR", check="amount_due > 0"),
            Field("principal_component", "REAL", unit="INR", check="principal_component >= 0"),
            Field("interest_component", "REAL", unit="INR", check="interest_component >= 0"),
            Field("amount_paid", "REAL", unit="INR", rule="DERIVED: SUM(allocated_amount); 0 <= amount_paid <= amount_due"),
            Field("installment_status", "TEXT", allowed=INSTALLMENT_STATUSES,
                  rule="PAID if remaining=0; NOT_DUE if due_date>=as-of; OVERDUE if due<as-of and nothing paid; else PARTIALLY_PAID"),
            Field("fully_paid_date", "DATE", False, rule="Date of allocation that cleared the instalment; NOT NULL iff PAID"),
        ] + TS,
        unique_together=[("loan_id", "installment_number")],
        table_checks=["amount_paid >= 0 AND amount_paid <= amount_due + 0.005",
                      "((installment_status = 'PAID') = (fully_paid_date IS NOT NULL))"],
        indexes=["loan_id", "due_date", "installment_status"],
        cardinality=["Installment N:1 Loan", "Installment N:M Payment via payment_allocation"],
        lifecycle="NOT_DUE -> OVERDUE -> PARTIALLY_PAID -> PAID (or NOT_DUE -> PAID).",
    ),
    Table(
        "delinquency_snapshot", "snapshot_id",
        "Immutable point-in-time delinquency measurement for a loan (month-end snapshots + reference date).",
        [
            Field("snapshot_id", "TEXT", rule="Pattern SNP-2026-NNNNNN", unique=True),
            Field("loan_id", "TEXT", fk="loan.loan_id"),
            Field("snapshot_date", "DATE"),
            Field("total_amount_paid", "REAL", unit="INR", rule="SUM of allocations dated <= snapshot_date"),
            Field("outstanding_balance", "REAL", unit="INR", rule="total_payable - total_amount_paid (contractual balance)"),
            Field("total_arrears", "REAL", unit="INR", rule="SUM(amount_due - allocated as of date) over instalments with due_date < snapshot_date"),
            Field("overdue_installments", "INTEGER", unit="count"),
            Field("days_past_due", "INTEGER", unit="days", rule="snapshot_date - due_date of the oldest unpaid overdue instalment; 0 if none"),
            Field("delinquency_bucket", "TEXT", allowed=BUCKETS),
            Field("loan_status", "TEXT", allowed=LOAN_STATUSES),
        ] + TS,
        unique_together=[("loan_id", "snapshot_date")],
        table_checks=["total_arrears >= 0", "outstanding_balance >= -0.005", "days_past_due >= 0"],
        indexes=["loan_id", "snapshot_date", "delinquency_bucket"],
        cardinality=["DelinquencySnapshot N:1 Loan"],
        lifecycle="Append-only; never updated.",
    ),
    Table(
        "contact_preference", "preference_id",
        "Customer communication preferences with effective dating (history retained; one active row per customer at a time).",
        [
            Field("preference_id", "TEXT", rule="Pattern CPF-2026-NNNNNN", unique=True),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("preferred_channel", "TEXT", allowed=CHANNELS),
            Field("allow_phone", "BOOL"), Field("allow_sms", "BOOL"), Field("allow_email", "BOOL"), Field("allow_post", "BOOL"),
            Field("allowed_days", "TEXT", rule="Comma list of MON..SUN; may be empty only if opt_out_all"),
            Field("window_start", "TEXT", unit="HH:MM local time"),
            Field("window_end", "TEXT", unit="HH:MM local time", rule="> window_start"),
            Field("max_contacts_per_week", "INTEGER", False, unit="count", rule="Optional stricter customer limit"),
            Field("opt_out_all", "BOOL", rule="1 = customer opted out of ALL outbound collection contact"),
            Field("consent_source", "TEXT", allowed=["ENROLMENT", "CUSTOMER_REQUEST", "BRANCH"]),
            Field("effective_from", "DATE"),
            Field("effective_to", "DATE", False, rule="NULL while current; >= effective_from"),
            Field("status", "TEXT", allowed=["ACTIVE", "SUPERSEDED"], rule="ACTIVE iff effective_to IS NULL"),
        ] + TS,
        table_checks=["effective_to IS NULL OR effective_to >= effective_from",
                      "((status = 'ACTIVE') = (effective_to IS NULL))", "window_end > window_start"],
        indexes=["customer_id", "effective_from"],
        cardinality=["ContactPreference N:1 Customer (history); exactly one ACTIVE per customer"],
        lifecycle="ACTIVE -> SUPERSEDED (a new row is inserted; old rows are never overwritten).",
    ),
    Table(
        "collection_case", "case_id",
        "Collection workflow case opened when a loan reaches the case-opening DPD threshold.",
        [
            Field("case_id", "TEXT", rule="Pattern COL-2026-NNNNNN", unique=True),
            Field("loan_id", "TEXT", fk="loan.loan_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("opened_date", "DATE"),
            Field("closed_date", "DATE", False, rule="NOT NULL iff case_status = RESOLVED"),
            Field("case_status", "TEXT", allowed=CASE_STATUSES,
                  rule="Precedence: RESOLVED > DISPUTE_REVIEW > HARDSHIP_REVIEW > PROMISE_PENDING > OPEN"),
            Field("debt_confirmation_status", "TEXT", allowed=DEBT_STATUSES,
                  rule="DISPUTED while a dispute flag is active; CONFIRMED after a dispute cleared as DEBT_VALIDATED; else UNVERIFIED"),
            Field("opening_dpd", "INTEGER", unit="days", rule="Days past due when case opened (derived)"),
            Field("opening_arrears", "REAL", unit="INR", rule="Arrears when case opened (derived)"),
            Field("assigned_analyst", "TEXT", rule="Synthetic analyst code"),
        ] + TS,
        table_checks=["closed_date IS NULL OR closed_date >= opened_date",
                      "((case_status = 'RESOLVED') = (closed_date IS NOT NULL))"],
        indexes=["loan_id", "customer_id", "case_status", "opened_date"],
        cardinality=["CollectionCase N:1 Loan", "CollectionCase N:1 Customer", "CollectionCase 1:N ContactAttempt",
                     "CollectionCase 1:N PromiseToPay"],
        lifecycle="OPEN <-> PROMISE_PENDING / DISPUTE_REVIEW / HARDSHIP_REVIEW -> RESOLVED (terminal). History in audit_event.",
    ),
    Table(
        "contact_attempt", "attempt_id",
        "A contact task (scheduled or completed) against a case. Only created after the eligibility engine PERMITS the request.",
        [
            Field("attempt_id", "TEXT", rule="Pattern CTP-<case_id>-NNN", unique=True),
            Field("case_id", "TEXT", fk="collection_case.case_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("sequence_number", "INTEGER", rule="1-based per case; matches suffix of attempt_id"),
            Field("channel", "TEXT", allowed=CHANNELS),
            Field("purpose", "TEXT", allowed=PURPOSES),
            Field("scheduled_for", "DATETIME", rule="Local customer time, ISO-8601 without zone"),
            Field("attempt_status", "TEXT", allowed=ATTEMPT_STATUSES),
            Field("outcome", "TEXT", False, allowed=OUTCOMES, rule="NOT NULL iff attempt_status = COMPLETED"),
        ] + TS,
        unique_together=[("case_id", "sequence_number")],
        table_checks=["((attempt_status = 'COMPLETED') = (outcome IS NOT NULL))"],
        indexes=["case_id", "customer_id", "scheduled_for"],
        cardinality=["ContactAttempt N:1 CollectionCase", "ContactAttempt N:1 Customer", "ContactAttempt 0..1:N PromiseToPay"],
        lifecycle="SCHEDULED -> COMPLETED | CANCELLED.",
    ),
    Table(
        "promise_to_pay", "promise_id",
        "Customer commitment to pay an amount by a date. Status is derived from subsequent payment allocations.",
        [
            Field("promise_id", "TEXT", rule="Pattern PTP-2026-NNNNNN", unique=True),
            Field("case_id", "TEXT", fk="collection_case.case_id"),
            Field("loan_id", "TEXT", fk="loan.loan_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("attempt_id", "TEXT", False, fk="contact_attempt.attempt_id", rule="NULL for inbound/unlinked promises"),
            Field("created_date", "DATE"),
            Field("promised_amount", "REAL", unit="INR", check="promised_amount > 0"),
            Field("promised_date", "DATE", rule=">= created_date"),
            Field("arrears_at_creation", "REAL", unit="INR", rule="Arrears as of created_date recomputed from source and compared by validation"),
            Field("promise_status", "TEXT", allowed=PROMISE_STATUSES),
            Field("amount_received", "REAL", unit="INR", rule="DERIVED: allocations dated in [created_date, promised_date + grace]"),
            Field("evaluated_on", "DATE", False, rule="Date the final outcome was determined; NULL while PENDING"),
            Field("exception_approved_by", "TEXT", False, rule="Required when promised_amount > arrears_at_creation (documented exception workflow)"),
            Field("exception_reason", "TEXT", False),
        ] + TS,
        table_checks=["promised_date >= created_date", "amount_received >= 0",
                      "((promise_status = 'PENDING') = (evaluated_on IS NULL))"],
        indexes=["case_id", "loan_id", "promise_status", "promised_date"],
        cardinality=["PromiseToPay N:1 CollectionCase", "PromiseToPay N:1 Loan", "PromiseToPay N:1 ContactAttempt (optional)"],
        lifecycle="PENDING -> FULFILLED | PARTIALLY_FULFILLED | BROKEN (outcome transitions logged in audit_event).",
    ),
    Table(
        "payment", "payment_id",
        "Money received on a loan. Allocation to instalments is recorded explicitly in payment_allocation.",
        [
            Field("payment_id", "TEXT", rule="Pattern PMT-2026-NNNNNN", unique=True),
            Field("loan_id", "TEXT", fk="loan.loan_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("payment_date", "DATE"),
            Field("amount", "REAL", unit="INR", check="amount > 0"),
            Field("channel", "TEXT", allowed=PAYMENT_CHANNELS),
            Field("reference", "TEXT", unique=True, rule="Synthetic bank reference; unique (duplicate detection)"),
            Field("reconciliation_status", "TEXT", allowed=RECON_STATUSES,
                  rule="RECONCILED = fully allocated and consistent with FIFO policy; EXCEPTION otherwise"),
        ] + TS,
        indexes=["loan_id", "customer_id", "payment_date", "reconciliation_status"],
        cardinality=["Payment N:1 Loan", "Payment N:M Installment via payment_allocation"],
        lifecycle="Immutable once posted; reconciliation_status transitions are audited.",
    ),
    # ------------------------- supporting tables -------------------------
    Table(
        "payment_allocation", "allocation_id",
        "Bridge table (Payment N:M Installment): explicit, auditable allocation of payment amounts.",
        [
            Field("allocation_id", "TEXT", rule="Pattern ALC-2026-NNNNNN", unique=True),
            Field("payment_id", "TEXT", fk="payment.payment_id"),
            Field("installment_id", "TEXT", fk="installment.installment_id"),
            Field("allocated_amount", "REAL", unit="INR", check="allocated_amount > 0"),
            Field("allocation_date", "DATE", rule="Equals payment.payment_date"),
            Field("allocation_rule", "TEXT", allowed=ALLOCATION_RULES, rule="Policy applied: oldest due first"),
        ] + TS,
        core=False, unique_together=[("payment_id", "installment_id")],
        indexes=["payment_id", "installment_id"],
        cardinality=["PaymentAllocation N:1 Payment", "PaymentAllocation N:1 Installment"],
        lifecycle="Append-only.",
    ),
    Table(
        "flag_event", "flag_event_id",
        "Event history of hardship and dispute flags (raised/cleared). Current state = latest event at or before the as-of time.",
        [
            Field("flag_event_id", "TEXT", rule="Pattern FLG-2026-NNNNNN", unique=True),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("loan_id", "TEXT", False, fk="loan.loan_id", rule="Required for DISPUTE (loan-level); NULL for HARDSHIP (customer-level)"),
            Field("flag_type", "TEXT", allowed=FLAG_TYPES),
            Field("event_type", "TEXT", allowed=FLAG_EVENT_TYPES),
            Field("event_at", "DATETIME"),
            Field("source", "TEXT", allowed=FLAG_SOURCES),
            Field("resolution_outcome", "TEXT", False, allowed=["DEBT_VALIDATED"], rule="Only on CLEARED DISPUTE"),
            Field("note", "TEXT", False),
        ] + TS,
        core=False,
        table_checks=["(flag_type <> 'DISPUTE' OR loan_id IS NOT NULL)",
                      "(resolution_outcome IS NULL OR (flag_type = 'DISPUTE' AND event_type = 'CLEARED'))"],
        indexes=["customer_id", "loan_id", "event_at"],
        cardinality=["FlagEvent N:1 Customer", "FlagEvent N:1 Loan (disputes)"],
        lifecycle="Append-only event log.",
    ),
    Table(
        "contact_scheduling_log", "log_id",
        "Every contact scheduling request with the eligibility decision and reason codes (permitted and rejected).",
        [
            Field("log_id", "TEXT", rule="Pattern SCH-2026-NNNNNN", unique=True),
            Field("case_id", "TEXT", fk="collection_case.case_id"),
            Field("customer_id", "TEXT", fk="customer.customer_id"),
            Field("requested_channel", "TEXT", allowed=CHANNELS),
            Field("requested_for", "DATETIME"),
            Field("purpose", "TEXT", allowed=PURPOSES),
            Field("decision", "TEXT", allowed=["PERMITTED", "REJECTED"]),
            Field("reason_codes", "TEXT", rule="Comma list of reason codes; ALL_RULES_PASSED when permitted"),
            Field("attempt_id", "TEXT", False, fk="contact_attempt.attempt_id", rule="Set when PERMITTED"),
            Field("requested_by", "TEXT"),
            Field("decided_at", "DATETIME"),
            Field("rules_version", "TEXT"),
        ] + TS,
        core=False,
        table_checks=["((decision = 'PERMITTED') = (attempt_id IS NOT NULL))"],
        indexes=["case_id", "customer_id", "decision"],
        cardinality=["SchedulingLog N:1 CollectionCase", "SchedulingLog 0..1:1 ContactAttempt"],
        lifecycle="Append-only.",
    ),
    Table(
        "audit_event", "audit_id",
        "Append-only audit trail of important state transitions.",
        [
            Field("audit_id", "TEXT", rule="Pattern AUD-2026-NNNNNN", unique=True),
            Field("entity_type", "TEXT", allowed=AUDIT_ENTITY_TYPES),
            Field("entity_id", "TEXT"),
            Field("action", "TEXT"),
            Field("old_value", "TEXT", False),
            Field("new_value", "TEXT", False),
            Field("actor", "TEXT", rule="SYSTEM, GENERATOR, or an analyst/API identity"),
            Field("reason", "TEXT", False),
            Field("event_ts", "DATETIME"),
            Field("details", "JSON", False),
        ],
        core=False, indexes=["entity_type", "entity_id", "event_ts"],
        lifecycle="Append-only.",
    ),
    Table(
        "run_log", "run_id",
        "Execution log of prioritization / exception-detection / validation / reconciliation runs.",
        [
            Field("run_id", "TEXT"),
            Field("run_type", "TEXT", allowed=RUN_TYPES),
            Field("as_of_date", "DATE"),
            Field("started_at", "DATETIME", rule="Operational wall-clock time (intentionally non-deterministic for API runs)"),
            Field("rules_version", "TEXT", False),
            Field("summary", "JSON", False),
        ],
        core=False, indexes=["run_type"],
    ),
    Table(
        "prioritization_result", "result_id",
        "Persisted, explainable prioritization output for a case in a run.",
        [
            Field("result_id", "TEXT"),
            Field("run_id", "TEXT", fk="run_log.run_id"),
            Field("case_id", "TEXT", fk="collection_case.case_id"),
            Field("as_of_date", "DATE"),
            Field("priority_score", "REAL", unit="0-100", check="priority_score BETWEEN 0 AND 100"),
            Field("priority_category", "TEXT", allowed=PRIORITY_CATEGORIES),
            Field("contributing_factors", "JSON", rule="List of {factor, value, points, explanation}"),
            Field("reason_codes", "TEXT"),
            Field("suppression_reasons", "TEXT", False),
            Field("recommended_action", "TEXT"),
            Field("recommended_channel", "TEXT", False, allowed=CHANNELS),
            Field("recommended_slot", "DATETIME", False),
            Field("explanation", "TEXT"),
        ],
        core=False, unique_together=[("run_id", "case_id")], indexes=["run_id", "case_id", "priority_category"],
    ),
    Table(
        "exception_record", "exception_id",
        "Compliance/data exception detected from source data (never from ground truth).",
        [
            Field("exception_id", "TEXT"),
            Field("run_id", "TEXT", fk="run_log.run_id"),
            Field("exception_type", "TEXT", allowed=ISSUE_TYPES),
            Field("severity", "TEXT", allowed=SEVERITIES),
            Field("entity_type", "TEXT"),
            Field("entity_id", "TEXT"),
            Field("case_id", "TEXT", False),
            Field("loan_id", "TEXT", False),
            Field("customer_id", "TEXT", False),
            Field("description", "TEXT"),
            Field("expected_decision", "TEXT", rule="What the system/analyst should have done"),
            Field("system_response", "TEXT", rule="What the platform does about it"),
            Field("detected_as_of", "DATE"),
            Field("status", "TEXT", allowed=EXCEPTION_STATUSES),
        ],
        core=False, indexes=["run_id", "exception_type", "entity_id", "status"],
    ),
    Table(
        "validation_result", "result_id",
        "Result of one automated validation check in a validation run.",
        [
            Field("result_id", "TEXT"),
            Field("run_id", "TEXT", fk="run_log.run_id"),
            Field("check_name", "TEXT"),
            Field("category", "TEXT"),
            Field("severity", "TEXT", allowed=SEVERITIES),
            Field("passed", "BOOL"),
            Field("violation_count", "INTEGER"),
            Field("detail", "TEXT", False),
        ],
        core=False, indexes=["run_id"],
    ),
]

TABLE_BY_NAME = {t.name: t for t in TABLES}
CORE_TABLES = [t.name for t in TABLES if t.core]
# Dataset (CSV) tables produced by the generator, in FK-safe load order.
DATASET_TABLES = [
    "customer", "account", "loan", "installment", "payment", "payment_allocation",
    "delinquency_snapshot", "contact_preference", "collection_case", "flag_event",
    "contact_attempt", "contact_scheduling_log", "promise_to_pay", "audit_event",
]
# Load order for SQLite (parents first). run_log precedes result tables.
DB_LOAD_ORDER = DATASET_TABLES
ID_PATTERNS = {
    "customer": r"^CUS-\d{6}$", "account": r"^ACC-\d{6}$", "loan": r"^LOAN-2026-\d{6}$",
    "installment": r"^INS-LOAN-2026-\d{6}-\d{3}$", "delinquency_snapshot": r"^SNP-2026-\d{6}$",
    "contact_preference": r"^CPF-2026-\d{6}$", "collection_case": r"^COL-2026-\d{6}$",
    "contact_attempt": r"^CTP-COL-2026-\d{6}-\d{3}$", "promise_to_pay": r"^PTP-2026-\d{6}$",
    "payment": r"^PMT-2026-\d{6}$", "payment_allocation": r"^ALC-2026-\d{6}$",
    "flag_event": r"^FLG-2026-\d{6}$", "contact_scheduling_log": r"^SCH-2026-\d{6}$",
    "audit_event": r"^AUD-2026-\d{6}$",
}


def _sql_type(f: Field) -> str:
    return {"TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "REAL", "DATE": "TEXT", "DATETIME": "TEXT",
            "BOOL": "INTEGER", "JSON": "TEXT"}[f.type]


def build_schema_sql() -> str:
    """Generate the full SQLite DDL from the table specs."""
    out = ["-- GENERATED by `python -m collections_ai.cli schema` from collections_ai/spec.py. Do not edit by hand.",
           "PRAGMA foreign_keys = ON;", ""]
    ordered = [TABLE_BY_NAME[n] for n in DATASET_TABLES] + [
        TABLE_BY_NAME[n] for n in ["run_log", "prioritization_result", "exception_record", "validation_result"]]
    for t in ordered:
        lines = []
        for f in t.fields:
            col = f"    {f.name} {_sql_type(f)}"
            if f.name == t.pk:
                col += " PRIMARY KEY"
            elif f.required:
                col += " NOT NULL"
            if f.unique and f.name != t.pk:
                col += " UNIQUE"
            if f.allowed:
                vals = ",".join(f"'{v}'" for v in f.allowed)
                col += f" CHECK ({f.name} IN ({vals}))" if f.required else f" CHECK ({f.name} IS NULL OR {f.name} IN ({vals}))"
            if f.type == "BOOL":
                col += f" CHECK ({f.name} IN (0,1))"
            if f.check:
                col += f" CHECK ({f.check})"
            lines.append(col)
        for f in t.fields:
            if f.fk:
                tbl, c = f.fk.split(".")
                lines.append(f"    FOREIGN KEY ({f.name}) REFERENCES {tbl}({c})")
        for cols in t.unique_together:
            lines.append(f"    UNIQUE ({', '.join(cols)})")
        for chk in t.table_checks:
            lines.append(f"    CHECK ({chk})")
        out.append(f"CREATE TABLE IF NOT EXISTS {t.name} (\n" + ",\n".join(lines) + "\n);")
        for c in t.indexes:
            out.append(f"CREATE INDEX IF NOT EXISTS idx_{t.name}_{c} ON {t.name}({c});")
        out.append("")
    return "\n".join(out)
