# Testing

Run: `python -m pytest` (configured by `pytest.ini`). Full verbose log of the last run: `reports/test_report.txt` (JUnit XML: `reports/junit.xml`).

## Actual result of the last run
**109 passed, 0 failed, 0 skipped in about 75 seconds** (Python 3.13, Linux; pandas 3.0.5, FastAPI 0.162.0, pytest 9.1.1). Tests use a 1,500-loan dataset generated once per session with the fixed seed; the API tests build a fresh SQLite database per test. The full 10,000-loan dataset is validated separately by the pipeline (see `docs/VALIDATION_REPORT.md`).

| File | Tests | Covers |
|---|---:|---|
| `test_generation_and_integrity.py` | 13 | volume/parameters, ten entities, ID conventions, 114-check validation suite on baseline and final data, zero baseline exceptions, SQLite FK enforcement, realistic variation, no real PII, reproducibility (same seed -> identical counts, IDs, checksums, ground truth), different seed differs, CSV round-trip |
| `test_delinquency_and_reconciliation.py` | 22 | arrears/DPD maths, partial payments without double counting, as-of dates, paid-off, bucket and default boundaries, FIFO allocation, snapshot vs ledger, arrears vs instalment table, allocations fully allocated, payment reconciliation, promise outcomes (fulfilled, partial, broken, pending, late payment, pre-creation payment) and stored promise outcomes vs payments |
| `test_contact_and_prioritization.py` | 32 | every eligibility rule incl. boundaries, jurisdiction config, frequency limits, dispute/hardship/paid-off/suspended, baseline scheduling never violates rules, rejected + permitted logged, score = sum of explained factors, monotonicity, cap, risk ordering, paid-off suppression, dispute workflow, hardship review not escalation, restrictions override high scores, opt-out never gets a channel, recommended slot passes the engine, pending promises monitored |
| `test_negative_scenarios.py` | 14 | all five scenarios: ground-truth present and separate, injection keeps relationships valid, precision = recall = 1.0 per scenario, no baseline false positives, per-scenario expected decision and system response, approved exception promises not flagged, **no ground-truth leakage** into engines |
| `test_api.py` | 17 | health/docs, metrics, 404/422 handling, live delinquency = snapshot, prioritization explain/run, prohibited contact rejected and logged (422), permitted scheduling + outcome + audit, promise > arrears rejected unless exception, promise -> payment -> FULFILLED -> case RESOLVED -> tasks cancelled, partial/duplicate/overpayment validation, snapshots immutable after payment, dispute/hardship flags change case status and contact eligibility, paid-off case creation refused, exception detection/validation/evaluation, idempotent reconciliation |

## Other verification performed
- All 10 dashboard pages were rendered headlessly with Streamlit `AppTest` against the full database: no exceptions on any page (manual script, not part of the pytest suite).
- Full pipeline on 10,000 loans: 114/114 integrity checks passed, 150/150 injected anomalies detected with 0 false positives, reproducibility check passed (identical checksums across two generations).

## Not tested / gaps
- No browser-level UI tests; dashboard visuals were not reviewed by eye by the assistant.
- No load/concurrency tests.
- Windows/PowerShell execution not performed by the assistant.
