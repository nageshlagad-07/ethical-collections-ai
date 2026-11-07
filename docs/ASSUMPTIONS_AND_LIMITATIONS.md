# Assumptions and Limitations

## Documented assumptions (flagged for review)
1. **Loan structure:** 3 equal monthly (30-day) instalments, flat interest, INR only, no fees or penalty interest. This yields exactly 30,000 instalments for 10,000 loans as the challenge specifies.
2. **Outstanding balance** is the *contractual* balance (total payable minus allocations), not an amortised principal balance. Chosen because the challenge allows "principal or contractual balance, according to the documented model".
3. **Reference date** is fixed at 2026-10-22 (the challenge's seed date) for reproducibility. The 12-month period is 2025-10-23..2026-10-22.
4. **Case opening threshold** is DPD >= 7 days, grace for promises is 3 days, DPD > 90 is DEFAULT. All are configurable in `config/rules.json`; none is taken from a regulation.
5. **Jurisdiction rules are illustrative.** Three synthetic profiles (POLICY_A/B/C) with example days/hours/frequency limits. **No legal requirement is asserted.** Compliance requires legal review.
6. **Payment-vs-promise window:** only allocations dated *after* the promise creation day and up to `promised_date + grace` count; same-day payments are treated as pre-existing. This is a modelling choice made to avoid crediting a promise with money received before it was made.
7. **Dispute clearing** is modelled only as "debt validated" (-> CONFIRMED). A withdrawn/cancelled-debt outcome is not modelled.
8. **Hardship** is a customer-level flag; dispute is loan-level.
9. **Written channels** (EMAIL/POST) are exempt from time-window rules in the illustrative policy; frequency limits and all restrictions still apply.
10. **Anomaly volume:** 30 per scenario type (0.3% of loans, 150 total) at full scale; 3-4 per type in the small test dataset (`max(3, round(0.003 x loans))`).
11. **Synthetic population:** 7,200 customers, 8,064 accounts for 10,000 loans (customers/accounts hold several loans). Names come from Faker (`en_IN`); e-mails use the reserved `.invalid` TLD and phones a fictional `+000-` pattern.

## Known limitations
- **Not calibrated to real portfolios.** Distribution parameters (risk-linked repayment patterns, contact outcome probabilities) are plausible but invented. In the full dataset 61.5% of loans are paid off, 12.1% are DEFAULT (>90 DPD) and the promise fulfilment rate is 26% (541 fulfilled of 2,072 evaluated). Do not read them as industry benchmarks.
- **No authentication/authorisation** on the API (local demo only). Actor names are free text.
- **Dashboard is read-only;** state changes go through the API.
- **Snapshot staleness:** delinquency snapshots are immutable history. After an API payment, the latest snapshot for that loan is stale until the next batch run; live `/loans/{id}/delinquency` is always current and validation excludes such loans from the snapshot-vs-source comparison.
- **Business date is fixed** in the API; there is no real-time clock simulation.
- **Overpayments** and refunds/reversals are not supported; payments must not exceed the outstanding balance.
- **SQLite** is single-writer; not intended for concurrent production load.
- **Time zones** are not modelled; contact times are customer-local naive datetimes.
- **Prioritization** is a rules-based score; it is not validated against collection outcomes and no machine-learning model is used.
- **Testing environment:** the test suite was executed on Linux with Python 3.13 and the package versions in `requirements-tested.txt`. It has **not** been executed on Windows/PowerShell by the AI assistant; the PowerShell commands in README.md and `scripts/*.ps1` are untested here and should be run once by the author.
- **Developer review pending:** see `docs/AI_DISCLOSURE.md` - the author must review the code and complete the personal-review section.
