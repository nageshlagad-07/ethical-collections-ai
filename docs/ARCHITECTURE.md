# Architecture

## Overview
A local, dependency-light stack: **Python + SQLite + FastAPI + Streamlit**. No cloud services, paid APIs or external data.

```
                config/rules.json  (jurisdiction profiles, case policy, scoring bands)
                        |
 collections_ai/spec.py (single source of truth: entities, enums, constraints)
      |         |                |
      |   sql/schema.sql    docs/DATA_DICTIONARY.md
      v
 generator.py --> injection.py (labelled anomalies) --> data/full/*.csv + ground_truth.* + manifest.json
      |                                                      |
      | uses the SAME engines as runtime:                    v  init-db
      |   delinquency.py   contact.py   reconciliation.py   SQLite (data/collections.db)
      |   prioritization.py   detection.py   validation.py        ^         ^
      +-----------------------------------------------------------+         |
                                                      FastAPI (collections_ai/api)   Streamlit (dashboard/app.py, read-only)
```

## Modules
| Module | Responsibility |
|---|---|
| `spec.py` | Entity/field/enum/constraint definitions; generates the SQLite DDL |
| `config.py`, `rules.py`, `ids.py` | Fixed seed + reference date, configurable rules, deterministic ID builders |
| `delinquency.py` | Integer-paise ledger, as-of arrears/DPD/status, FIFO allocation, vectorised snapshots |
| `generator.py` | Ordered, seeded generation of all entities; contact simulation runs every proposed contact through the real eligibility engine |
| `injection.py` | Injects the five labelled scenarios after the valid baseline; the **only** writer of ground truth |
| `contact.py` | Pure eligibility function + state builder + next-permissible-slot search |
| `prioritization.py` | Explainable scoring, override precedence, recommended actions |
| `reconciliation.py` | FIFO replay, payment reconciliation, promise outcome evaluation |
| `detection.py` | Exception detectors (source records only) + evaluation helper vs ground truth |
| `validation.py` | 114 automated checks (keys, FKs, enums, dates, arithmetic, lifecycle, promises, exceptions) |
| `db.py`, `services.py` | SQLite connection/transactions/audit helper, case/flag/metrics services |
| `pipeline.py`, `cli.py` | Export, DB build, engine runs, reproducibility check, report rendering |
| `api/` | FastAPI app + Pydantic schemas (row models generated from the spec) |
| `dashboard/app.py` | 10-page Streamlit dashboard reading the DB |

## Key design decisions
1. **Derived values are never generated independently.** Instalment status/amount_paid, loan status, snapshots, case/promise status and payment reconciliation are all computed from payment allocations by the same functions that the API uses.
2. **One eligibility function** is used by the generator (so the baseline has no violations), the API (to schedule) and the detector (to find violations). Rules cannot diverge between generation and enforcement.
3. **Ground truth isolation.** Injection writes labels to a separate frame/file. Engine modules do not import or reference it (enforced by a source-scan test). `detection.evaluate_against_ground_truth` is an explicit, separate scoring helper.
4. **Event sourcing for history.** Flags (`flag_event`), allocations, snapshots, scheduling decisions and `audit_event` are append-only. Status columns are caches that can be re-derived and are audited on change.
5. **Integer money.** Paise integers inside engines, 2-decimal floats at the storage boundary.
6. **Determinism.** One `SeedSequence(20261022)` spawns independent generators per stage; dictionaries/iteration orders are sorted; IDs are counters assigned after deterministic sorting; no wall-clock values enter generated data.

## Database
SQLite with `PRAGMA foreign_keys=ON`, WAL journal, explicit `BEGIN IMMEDIATE ... COMMIT/ROLLBACK` (`db.transaction`), CHECK constraints for enums and ranges, UNIQUE/composite-UNIQUE constraints, indexes on all foreign keys and filter columns. Full DDL: `sql/schema.sql`; ER diagram and cardinalities: `docs/DATA_DICTIONARY.md`, `docs/ER_DIAGRAM.mmd`.

Tables: the ten core entities (customer, account, loan, installment, delinquency_snapshot, contact_preference, contact_attempt, promise_to_pay, payment, collection_case) plus `payment_allocation` (bridge), `flag_event`, `contact_scheduling_log`, `audit_event`, `run_log`, `prioritization_result`, `exception_record`, `validation_result`.

## API surface (Swagger UI at `/docs`)
Health, metrics, customers, loans (+delinquency, installments), snapshots, collection cases (create/status), prioritization (explain/run/results), contact eligibility/scheduling/outcomes/log, flags, promises (create/evaluate), payments (post/allocations), reconciliation (run/summary/exceptions), exceptions (detect/list/update), validation (run/latest/evaluation), audit events. Status codes: 200/201 success, 404 unknown id, 409 state conflict (duplicate, illegal transition, active dispute), 422 business/validation rejection, 503 database missing.

## Operational (non-deterministic) fields
Generated CSVs contain no wall-clock values. At run time, `run_log.started_at`, API-created `created_at/updated_at/event_ts` and API audit rows use the system clock; run ids are deterministic counters per day.
