# AI Disclosure

The challenge permits AI coding tools provided they are used transparently. This document states what was done by AI, what was verified by tests, and - importantly - what the author still has to review and attest to. **Sections marked `AUTHOR TO COMPLETE` must be filled in truthfully by the candidate before submission; the AI assistant cannot know what you personally did.**

## 1. AI tools used
| Tool | Model | Used for |
|---|---|---|
| Claude (claude.ai, Claude Code-based agent session with shell and file tools) | configured as `claude-sonnet-5-5` (the serving model may differ) | Planning, all source code, tests, SQL schema generation, documentation, running the pipeline and tests in a Linux sandbox |
| Other tools | AUTHOR TO COMPLETE | e.g. any chat tool used to draft the master prompt (see `PROMPT_HISTORY.md`) |

## 2. What AI accelerated (done by the AI assistant in the session)
- Requirement extraction from the supplied brief and the phase plan.
- Architecture, SQLite schema, data dictionary generation from one specification module.
- Synthetic data generator, labelled anomaly injection, delinquency engine, contact eligibility engine, explainable prioritization, reconciliation and promise evaluation, detection and validation engines.
- FastAPI backend, Streamlit dashboard, CLI/pipeline, 109 pytest tests, documentation, packaging.
- Debugging during development (examples: a contact scheduler that could schedule backwards in time and caused daily-limit violations in the baseline; invalid preference dates; test expectations that were too strict).

## 3. Design decisions suggested by the AI (review these)
- 3 flat-interest monthly instalments per loan; contractual (not amortised) balance.
- Fixed business reference date 2026-10-22; case opens at DPD >= 7; DPD > 90 = DEFAULT; 3 grace days for promises.
- Oldest-due-first (FIFO) payment allocation as the reconciliation policy.
- Score bands and thresholds (P1 >= 78, P2 >= 58) and the override precedence order; hardship routes to review (never escalation); disputes suppress collection contact.
- Three illustrative jurisdiction profiles; no real law asserted.
- Same-day payments are not credited to a promise created that day.
- Anomaly counts (0.3% of loans per scenario type) and the injection approach (modify minimal rows, label in a separate file).

## 4. What was independently tested (executed by the AI in the sandbox)
- 109 pytest tests passed (log: `reports/test_report.txt`).
- Full 10,000-loan pipeline: 114/114 validation checks, 150/150 anomalies detected with no false positives, reproducibility identical across two runs (`docs/VALIDATION_REPORT.md`, `reports/reproducibility_report.json`).
- 10 dashboard pages rendered without exceptions (headless).
- Not done: execution on Windows/PowerShell, visual review of the dashboard, any review by a human.

## 5. What I personally reviewed, modified and validated - AUTHOR TO COMPLETE
> The AI cannot fill this in. Suggested checklist - tick only what you really did, and describe changes you made:
- [ ] Read every source file and can explain the generator, delinquency, contact, prioritization and reconciliation logic.
- [ ] Ran the commands in `README.md` on my own machine (Windows) and confirmed the tests pass.
- [ ] Opened the dashboard and API docs and checked pages/endpoints behave as described.
- [ ] Reviewed and agreed (or changed) the assumptions in `docs/ASSUMPTIONS_AND_LIMITATIONS.md`.
- [ ] Reviewed `config/rules.json` policy parameters.
- [ ] Modifications I made: ______________________
- [ ] Anything I removed or rewrote because I disagreed: ______________________

## 6. Assumptions and limitations that remain
See `docs/ASSUMPTIONS_AND_LIMITATIONS.md`. Headline items: synthetic distributions are not calibrated to real data; compliance rules are illustrative and not legal advice; the API has no authentication; Windows execution untested by the assistant.

## 7. Honesty note
Test counts, row counts, checksums and detection results quoted in the documentation come from commands actually executed during the session and saved in `reports/`. Re-run them yourself (`README.md`) and update the documents if anything differs on your machine.
