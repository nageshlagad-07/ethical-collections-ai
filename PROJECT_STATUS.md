# Project Status

_Last updated: 2026-11-06 (IST)._ Submission deadline per the invitation e-mail: **Saturday 7 November 2026, 10:06:44 PM IST** - check your own e-mail for the authoritative time.

## Completed (all executed and verified by the AI assistant in a Linux sandbox)
| Phase | Status | Evidence |
|---|---|---|
| 1 Requirements extraction | done | `docs/REQUIREMENTS_TRACEABILITY.md` |
| 2 Traceability matrix | done | same |
| 3 Architecture, schema, data dictionary | done | `docs/ARCHITECTURE.md`, `sql/schema.sql`, `docs/DATA_DICTIONARY.md`, `docs/ER_DIAGRAM.mmd` |
| 4 Synthetic generator (10,000 loans, seed 20261022, same logic at any scale) | done | `data/full/*`, `data/full/manifest.json` |
| 5 Delinquency + validation engines | done | 114 checks, all pass (`docs/VALIDATION_REPORT.md`) |
| 6 Prioritization + contact rules | done | `collections_ai/prioritization.py`, `contact.py` |
| 7 Promises, payments, reconciliation, 5 negative scenarios | done | 150/150 anomalies detected, 0 false positives |
| 8 FastAPI | done | `collections_ai/api/`, 17 API tests |
| 9 Streamlit dashboard | done | 10 pages rendered headlessly without exceptions |
| 10 Tests | done | **109 passed**, 0 failed (`reports/test_report.txt`) |
| 11 Documentation | done | `README.md`, `docs/*` |
| 12 Acceptance run + packaging | done | full pipeline re-run (identical dataset SHA-256), ZIP built and verified |

## Remaining - only YOU can do these
1. **Run the project on your Windows machine** following `README.md` (setup, `python -m pytest`, API, dashboard). The AI assistant could not execute anything on Windows/PowerShell, and has not looked at the dashboard with human eyes.
2. **Complete `docs/AI_DISCLOSURE.md` section 5** (what *you* reviewed/changed) honestly, and add any prompts from **other AI tools** you used to `PROMPT_HISTORY.md` (the screenshot showed another AI chat tab).
3. **Review the code** until you can explain it - the guidelines say you must be able to walk through and own the solution.
4. Check `prompt_history/prompt_001_master_instruction_prompt.txt` before publishing: it paraphrases parts of the challenge brief, which carried a confidentiality notice.
5. **Create a new public GitHub repository**, push the project (commit in meaningful steps; set your own `git config user.name/email` first), and submit the repository link on the portal before the deadline.
6. Re-check the third-party licence table in `docs/SOURCE_MANIFEST.md` and re-run `python -m collections_ai.cli pipeline` if you change any generator logic (then update the documents quoting numbers).

## Optional improvements (not required)
API authentication, fee/penalty-interest modelling, "debt withdrawn" dispute outcome, calibration of the synthetic distributions, browser-level UI tests.

## How to continue
```powershell
.\scripts\setup.ps1          # venv, dependencies, database, engines
.\scripts\run_tests.ps1
.\scripts\run_api.ps1        # http://127.0.0.1:8000/docs
.\scripts\run_dashboard.ps1  # http://localhost:8501
```
