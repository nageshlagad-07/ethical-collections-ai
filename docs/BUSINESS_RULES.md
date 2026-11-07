# Business Rules and Calculation Methodology

All rules below are implemented in code and exercised by tests. Tunable parameters live in `config/rules.json` (rules version `RULES-2026.1`).

> **Legal notice.** Contact days/hours, frequency limits and the hardship/dispute workflow are **illustrative, configurable policy parameters** chosen for this exercise. They are *not* statements of any law or regulation. Real use requires review by qualified legal/compliance staff against the regulations of the relevant jurisdiction. The platform does not invent legal requirements.

## 1. Business reference date
All batch calculations use the fixed reference date **2026-10-22** (`collections_ai/config.py`), so generation, validation and tests are reproducible. The 12-month data period is 2025-10-23 .. 2026-10-22. The API uses the same business date by default.

## 2. Loan model (contractual)
- Flat interest: `interest = principal x rate% x tenor/12`; `total_payable = principal + interest`.
- 3 equal instalments, due 30, 60 and 90 days after origination; the last instalment absorbs rounding.
- All money arithmetic uses integer paise internally (no floating-point drift); stored with 2 decimals.
- No late fees, penalty interest or interest accrual are modelled (see limitations).

## 3. Delinquency calculations (`collections_ai/delinquency.py`)
For a loan on date `d`:

| Measure | Definition |
|---|---|
| Total loan amount | `total_payable` = SUM(instalment.amount_due) |
| Total paid(d) | SUM(payment_allocation.allocated_amount) with allocation_date <= d. Each allocation is counted once (no double counting, because payments are only ever counted through allocations). |
| Outstanding(d) | `total_payable - total_paid(d)` (contractual balance, not amortised principal) |
| Remaining_i(d) | `amount_due_i - allocated_i(d)` |
| Overdue instalment | `due_date < d` and `remaining_i(d) > 0` (an instalment due on `d` is not yet overdue) |
| Arrears(d) | SUM(remaining_i(d)) over overdue instalments |
| Days past due | `d - due_date` of the **oldest** overdue instalment; 0 if none |
| Loan status | PAID_OFF if outstanding = 0; DEFAULT if arrears > 0 and DPD > 90; DELINQUENT if arrears > 0; else CURRENT |
| Bucket | PAID_OFF, CURRENT (0), DPD_1_30, DPD_31_60, DPD_61_90, DPD_90_PLUS |

Partial payments reduce `remaining` of the instalment they are allocated to; DPD therefore moves forward only when the oldest overdue instalment is fully cleared. Snapshots are immutable monthly (month-end) plus reference-date records; derived columns on `installment`/`loan` are recomputed from allocations.

## 4. Payment allocation
Policy `FIFO_OLDEST_DUE_FIRST`: a payment is applied to the loan's instalments in due-date order, filling each before moving on. Every allocation is an explicit `payment_allocation` row (bridge table). Overpayments beyond the outstanding balance are rejected by the API.

**Reconciliation** replays the policy from source records (`reconciliation.replay_fifo`) and compares with the recorded allocations. Payment status:
- `RECONCILED` - fully allocated, within its own loan, and identical to the FIFO replay.
- `EXCEPTION` - allocation sum mismatch, cross-loan allocation, or not oldest-due-first (this is how the *payment allocated to wrong installment* scenario is found).
- `UNRECONCILED` - no allocation recorded.

## 5. Collection cases
- A case opens when DPD >= 7 (`case_open_dpd`), evaluated on instalment due+7 days and every payment date, and closes when arrears return to 0. A loan can have several cases over time; at most one non-RESOLVED case per loan at once (validated).
- Status precedence: `RESOLVED > DISPUTE_REVIEW > HARDSHIP_REVIEW > PROMISE_PENDING > OPEN`. `RESOLVED` is terminal; a new delinquency opens a new case.
- Every transition is written to `audit_event`; history is never overwritten.

## 6. Dispute workflow
1. A dispute is raised for a **loan** (flag event `DISPUTE/RAISED`) - e.g. via contact outcome `REACHED_DISPUTE_RAISED` or `POST /flags`.
2. Case -> `DISPUTE_REVIEW`, `debt_confirmation_status = DISPUTED`.
3. While active: collection reminders and promise follow-ups are **blocked** (`ACTIVE_DISPUTE_RESTRICTION`); promises cannot be created; the case cannot be moved back to OPEN/PROMISE_PENDING; prioritization category is `SUPPRESSED` with action `FOLLOW_DISPUTE_VALIDATION_WORKFLOW`. Only `DISPUTE_RESPONSE` correspondence is allowed, and only on written channels (EMAIL/POST).
4. Clearing the dispute (`CLEARED`, outcome `DEBT_VALIDATED`) -> `debt_confirmation_status = CONFIRMED` and the case returns to its derived status. (A "debt withdrawn" outcome is not modelled; see limitations.)

A debt is therefore never treated as confirmed while a dispute is active. Scenario 5 injects exactly that inconsistency and the platform detects it and still suppresses contact.

## 7. Hardship workflow
A customer-level hardship flag moves the case to `HARDSHIP_REVIEW`. Collection reminders are blocked (`HARDSHIP_REVIEW_REQUIRED`); only `HARDSHIP_REVIEW_OUTREACH` is allowed. Prioritization category is `HARDSHIP_REVIEW`, action `REFER_TO_HARDSHIP_REVIEW_TEAM`, **no recommended channel/slot**, and the score is never used to escalate.

## 8. Contact eligibility (`collections_ai/contact.py`)
A request is PERMITTED only if **no** rule is violated; all violated rules are returned together. Both permitted and rejected requests are persisted in `contact_scheduling_log` with reason codes and rules version.

| Reason code | Rule |
|---|---|
| PAID_OFF_NO_COLLECTION_CONTACT | outstanding balance = 0 |
| ACCOUNT_SUSPENDED | servicing account suspended |
| NO_ACTIVE_PREFERENCE / OPT_OUT_ALL / CHANNEL_NOT_PERMITTED | customer preferences effective on the request date |
| DAY_NOT_PERMITTED | weekday outside (customer allowed days AND jurisdiction allowed days) - PHONE/SMS only |
| OUTSIDE_TIME_WINDOW | time outside the **intersection** of the customer window and jurisdiction window (inclusive bounds) - PHONE/SMS only |
| BLACKOUT_DATE | date in the jurisdiction profile's blackout list (empty by default) |
| DAILY_LIMIT_EXCEEDED / WEEKLY_LIMIT_EXCEEDED | per-customer attempts per calendar day / rolling 7 days; stricter of jurisdiction and customer limit |
| ACTIVE_DISPUTE_RESTRICTION / DISPUTE_RESPONSE_MUST_BE_WRITTEN | see section 6 |
| HARDSHIP_REVIEW_REQUIRED | see section 7 |

Jurisdiction profiles `POLICY_A/B/C` (assigned per customer) are defined in `config/rules.json`. Written channels (EMAIL/POST) are not time-window restricted in this illustrative policy.

`next_permissible_slot` searches forward (default 14 days) for the earliest slot that passes **every** rule (the same evaluation function), preferring the customer's preferred channel.

## 9. Explainable prioritization (`collections_ai/prioritization.py`)
**Score (0-100) = sum of explicit factor points** (maximum 100, no hidden weights):

| Factor | Points |
|---|---|
| arrears amount (INR) | <10k: 5; 10k-30k: 12; 30k-80k: 20; >=80k: 30 |
| days past due | <14: 8; 14-29: 16; 30-59: 24; >=60: 30 |
| payment history (instalments paid >7 days late or still unpaid) | 5 each, cap 15 |
| risk category | LOW 2, MEDIUM 6, HIGH 10 |
| broken promises | 5 each, cap 10 |
| outstanding balance (INR) | <50k: 1; 50k-150k: 3; >=150k: 5 |

Category from score: `P1_HIGH >= 78`, `P2_MEDIUM >= 58`, else `P3_LOW` (config).

**Restrictions override the score**, evaluated in this precedence order (first match wins):
1. paid off -> `SUPPRESSED` / `CLOSE_CASE_AFTER_REVIEW`
2. account suspended -> `SUPPRESSED` / `REFER_TO_COMPLIANCE_REVIEW`
3. active dispute -> `SUPPRESSED` / `FOLLOW_DISPUTE_VALIDATION_WORKFLOW`
4. active hardship -> `HARDSHIP_REVIEW` / `REFER_TO_HARDSHIP_REVIEW_TEAM`
5. opt-out of all contact -> `SUPPRESSED` / `NO_OUTBOUND_CONTACT_ANALYST_REVIEW`
6. no permitted channel -> `SUPPRESSED` / `NO_PERMITTED_CHANNEL_ANALYST_REVIEW`
7. pending promise -> `MONITOR_PROMISE` / `MONITOR_PROMISE`
8. otherwise score category with an action by DPD: <30 `SEND_PAYMENT_REMINDER`; 30-59 `SCHEDULE_PROMISE_TO_PAY_CALL`; 60-90 `OFFER_PAYMENT_ARRANGEMENT_DISCUSSION`; >90 `REFER_TO_SENIOR_ANALYST_REVIEW` (human review, never automatic escalation).

Each result stores: score, category, per-factor contributions with explanations, reason codes (`ARREARS_AMOUNT:+20`, `SUP_PAID_OFF`, `REV_HARDSHIP`, ...), suppression reasons, recommended action, the next permissible channel/slot (validated by the contact engine) and a plain-language explanation.

## 10. Promise-to-pay
- Creation validates: case not RESOLVED, not in dispute/hardship review, `promised_date >= created_date`, arrears > 0 at creation, **`promised_amount <= arrears`** unless `exception_approved_by` and `exception_reason` are supplied (documented exception workflow; even then not above the outstanding balance), at most one PENDING promise per case.
- Outcome is **derived from payment allocations** (`reconciliation.evaluate_promise`): window = allocations dated **after** `created_date` up to `promised_date + 3 grace days` (config). Payments dated on or before the creation day are treated as already received.
  - `FULFILLED`: received >= promised (evaluated_on = date cumulative receipts reached it)
  - `PARTIALLY_FULFILLED`: window closed, 0 < received < promised
  - `BROKEN`: window closed, nothing received
  - `PENDING`: window still open at the reference date
- Records are never overwritten: status changes are audited; `amount_received`/`evaluated_on` are derived fields.

## 11. Anomaly (negative scenario) rules
Detection works from source records only (`collections_ai/detection.py`):

| Issue type | Detection rule |
|---|---|
| PAID_ACCOUNT_IN_COLLECTION | non-RESOLVED case on a loan with outstanding = 0 |
| CONTACT_OUTSIDE_ALLOWED_WINDOW | any non-cancelled attempt that the eligibility engine would reject (reports the exact reason codes) |
| PAYMENT_ALLOCATED_TO_WRONG_INSTALLMENT | payment reconciliation status EXCEPTION |
| PROMISE_AMOUNT_EXCEEDS_ARREARS | promised amount > arrears recomputed at creation date, without an approved exception |
| DISPUTED_DEBT_TREATED_AS_CONFIRMED | active dispute flag while case is not DISPUTE_REVIEW or debt status is CONFIRMED |

Ground truth is produced only by the injection module and stored separately (`data/full/ground_truth.csv|json`); it is used for scoring only.
