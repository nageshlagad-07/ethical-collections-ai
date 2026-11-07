# Ethical Loan Collections Intelligence Platform

**YottaFlex AI — Round 3 Code Challenge**

An end-to-end Ethical Loan Collections Intelligence Platform designed to help operations teams prioritize overdue loan accounts, schedule permissible customer-contact activities, monitor promises to pay, reconcile payments, and identify compliance exceptions.

The platform emphasizes **explainability, responsible collections, configurable policies, and auditability**.

All project data is synthetic.

---

## 1. Business Problem

Loan collection operations need a reliable way to determine:

- Which overdue accounts should be reviewed first?
- When and how may customers be contacted?
- Are promises to pay being fulfilled?
- Are payments allocated correctly?
- Are compliance violations or data anomalies occurring?

This platform addresses these requirements through a collection of integrated data-processing engines and operational tools.

### Core capabilities

- Derives arrears and Days Past Due (DPD) from instalments and payment allocations.
- Uses transparent, rules-based prioritization with explainable decision factors.
- Applies contact restrictions that override prioritization scores.
- Enforces configurable contact days, hours, channels, and frequency limits.
- Records both accepted and rejected contact-scheduling requests.
- Tracks promises to pay and evaluates subsequent payments.
- Reconciles payments against instalments using oldest-due-first allocation.
- Detects five deliberately introduced compliance and data-quality anomaly categories.
- Maintains an append-only audit trail.

> **Compliance notice:** Contact windows, frequency limits, and dispute/hardship handling are illustrative, configurable parameters. They are not legal advice. Production deployment requires review against applicable laws and regulations.

---

## 2. Key Features

| Component | Implementation |
|---|---|
| Synthetic data | 10,000 loans and 30,000 instalments |
| Data model | 10 core entities plus supporting bridge, event, and run tables |
| Data generation | Deterministic generation with seed `20261022` |
| Delinquency engine | DPD and arrears calculation |
| Prioritization engine | Explainable rules-based case prioritization |
| Contact management | Eligibility checks and scheduling restrictions |
| Promise-to-pay | Promise tracking and payment evaluation |
| Reconciliation | Oldest-due-first payment allocation |
| Exception detection | Five labelled negative scenarios |
| Validation | 114 automated integrity and validation checks |
| Backend | FastAPI and Pydantic |
| Database | SQLite with transaction and audit support |
| Dashboard | Streamlit with 10 operational pages |
| Testing | 109 automated pytest tests |

### Negative test scenarios

The system includes five deliberately introduced scenarios:

1. Paid account incorrectly present in collections.
2. Contact attempt outside permitted hours.
3. Payment allocated to the wrong instalment.
4. Promise amount exceeding outstanding arrears.
5. Disputed debt treated as confirmed.

Each scenario has separate ground-truth labels to support detection evaluation without label leakage.

---

## 3. Architecture and Technology Stack

**Backend and processing**
- Python 3.11+
- FastAPI
- Pydantic
- pandas and NumPy

**Database**
- SQLite

**Data generation**
- Faker
- Deterministic synthetic-data generation

**Dashboard and visualization**
- Streamlit
- Plotly

**Testing and development**
- pytest
- Git
- Windows Command Prompt (CMD)

### Project structure

```text
ethical-collections-ai/
│
├── collections_ai/
│   ├── api/
│   ├── cli.py
│   ├── config.py
│   ├── contact.py
│   ├── delinquency.py
│   ├── detection.py
│   ├── generator.py
│   ├── prioritization.py
│   ├── reconciliation.py
│   ├── validation.py
│   └── ...
│
├── config/
│   └── rules.json
│
├── dashboard/
│   └── app.py
│
├── data/
│   └── full/
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_DICTIONARY.md
│   ├── BUSINESS_RULES.md
│   ├── TESTING.md
│   ├── VALIDATION_REPORT.md
│   ├── AI_DISCLOSURE.md
│   └── ASSUMPTIONS_AND_LIMITATIONS.md
│
├── prompt_history/
│
├── reports/
│
├── scripts/
│
├── sql/
│
├── tests/
│
├── .env.example
├── .gitignore
├── PROMPT_HISTORY.md
├── PROJECT_STATUS.md
├── README.md
├── pytest.ini
├── requirements.txt
└── requirements-tested.txt
```

---

## 4. Installation on Windows (CMD)

Open Command Prompt and navigate to the project directory.

```cmd
cd /d "D:\YottaFlex_AI_Round3_Ethical_Collections\ethical-collections-ai"
```

### Create a virtual environment

```cmd
python -m venv myvenv
```

### Activate the environment

```cmd
myvenv\Scripts\activate
```

### Install dependencies

```cmd
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The commands above use Windows CMD, not PowerShell.

---

## 5. Configuration

### Business rules

The file `config/rules.json` contains configurable policy parameters, including:

- Jurisdiction profiles (`POLICY_A`, `POLICY_B`, `POLICY_C`).
- Case-opening DPD thresholds.
- Promise grace periods.
- Prioritization scoring bands.
- Contact eligibility restrictions.

Rules are versioned and associated with decisions.

### Database configuration

The default SQLite database is:

```text
data/collections.db
```

The optional environment variable `COLLECTIONS_DB` can be used to specify another database location.

Example in CMD:

```cmd
set COLLECTIONS_DB=data/collections.db
```

No external database server or credentials are required for the current implementation.

### Reference date and random seed

The configured business reference date and generation seed are maintained in:

```text
collections_ai/config.py
```

Reference date: `2026-10-22`

Seed: `20261022`

---

## 6. Synthetic Data Generation (Optional)

The complete synthetic dataset is already included in `data/full/`.

To regenerate the full dataset:

```cmd
python -m collections_ai.cli generate --loans 10000 --seed 20261022 --out data/full
```

To generate a smaller development dataset:

```cmd
python -m collections_ai.cli generate --loans 300 --out data/dev
```

To execute the complete pipeline:

```cmd
python -m collections_ai.cli pipeline --loans 10000
```

To check reproducibility:

```cmd
python -m collections_ai.cli reproducibility --loans 10000
```

The reproducibility process generates data twice and compares relevant row counts, identifiers, checksums, and ground-truth information.

---

## 7. Initialize the Database and Run Engines

Run the following commands from the repository root with the virtual environment activated.

```cmd
python -m collections_ai.cli init-db --data data/full
```

This initializes the SQLite database using the supplied dataset.

Next, execute the processing engines:

```cmd
python -m collections_ai.cli run-engines
```

The pipeline performs:

1. Payment reconciliation.
2. Collection-case prioritization.
3. Compliance and anomaly detection.
4. Data-integrity validation.

**Important:** Avoid reinitializing an existing database unnecessarily, as database initialization may recreate its contents.

---

## 8. Start the FastAPI Backend

Start the backend:

```cmd
uvicorn collections_ai.api.main:app --host 127.0.0.1 --port 8000
```

Keep this CMD window open while using the API.

### API documentation

Swagger UI:

http://127.0.0.1:8000/docs

API health:

http://127.0.0.1:8000/health

The health endpoint reports application status and database connectivity.

---

## 9. Start the Streamlit Dashboard

Open a **second CMD window**.

Navigate to the project directory:

```cmd
cd /d "D:\YottaFlex_AI_Round3_Ethical_Collections\ethical-collections-ai"
```

Activate the virtual environment:

```cmd
myvenv\Scripts\activate
```

Start the dashboard:

```cmd
streamlit run dashboard\app.py
```

Open:

http://localhost:8501

### Dashboard pages

1. Executive Overview
2. Loan Portfolio
3. Delinquency Analysis
4. Collection Prioritization
5. Contact Management
6. Promise-to-Pay Monitoring
7. Payment Reconciliation
8. Compliance and Exceptions
9. Data Quality and Validation
10. Audit Trail

Dashboard metrics are queried from the database.

---

## 10. API Workflows

The API provides endpoints for working with collection cases, prioritization, contact scheduling, promises, payments, flags, exceptions, and audit events.

Open Swagger UI:

http://127.0.0.1:8000/docs

The following workflows can be demonstrated using the interactive API documentation.

### A. Collection-case prioritization

- Retrieve open collection cases.
- Select a case.
- Request its prioritization details.
- Review the score, contributing factors, reason codes, and recommended action.

### B. Contact eligibility

- Submit a contact-scheduling request.
- Test an allowed contact window.
- Test a prohibited contact window.
- Review the decision and associated reason codes.
- Inspect the contact-scheduling audit log.

### C. Promise-to-pay and reconciliation

- Create a promise to pay.
- Submit a payment.
- Review payment allocation.
- Evaluate the promise.
- Inspect case resolution and audit events.

### D. Dispute handling

- Submit a dispute flag.
- Review the resulting case status.
- Verify that prohibited collection contact is blocked.
- Review the prioritization decision.

### E. Compliance review

- Execute exception detection.
- Review detected exceptions.
- Compare detection results with the supplied ground truth.
- Inspect the Compliance and Exceptions dashboard page.

---

## 11. Testing and Validation

Run the automated test suite:

```cmd
python -m pytest -v
```

### Latest verified results

| Verification | Result |
|---|---:|
| Automated tests collected | 109 |
| Tests passed | 109 |
| Tests failed | 0 |
| Validation checks | 114 |
| Validation checks passed | 114 |
| Validation checks failed | 0 |

**Environment:** Windows 11, Python 3.11.9.

The test execution completed successfully in approximately 3 minutes and 55 seconds.

One Starlette deprecation warning was reported; it did not affect test success.

Additional results are documented in:

- `docs/TESTING.md`
- `reports/test_report.txt`
- `docs/VALIDATION_REPORT.md`

---

## 12. Dataset and Processing Statistics

The supplied full synthetic dataset includes:

| Metric | Count |
|---|---:|
| Customers | 7,200 |
| Loans | 10,000 |
| Instalments | 30,000 |
| Payments | 24,273 |
| Payment allocations | 25,892 |
| Delinquency snapshots | 69,067 |
| Collection cases | 4,864 |
| Contact attempts | 44,115 |
| Promises to pay | 2,214 |
| Detected negative scenarios | 150 |

The 150 detected anomalies represent five deliberately seeded test categories, with 30 scenarios in each category.

Full dataset validation information is available in:

`docs/VALIDATION_REPORT.md`

---

## 13. Assumptions and Limitations

- All supplied customer, loan, and payment records are synthetic.
- Synthetic distributions are not calibrated to an actual lending portfolio.
- Contact and collection policies are illustrative and configurable.
- The platform does not provide legal advice.
- API authentication is not implemented.
- The business reference date is fixed.
- Fees, interest accrual, and overpayments are outside the current implementation scope.
- SQLite is used for the current database implementation.

For further details, see:

`docs/ASSUMPTIONS_AND_LIMITATIONS.md`

---

## 14. AI Usage and Development Transparency

AI tools were used as development assistants during project planning, implementation guidance, debugging, documentation, and verification.

The development records are maintained in:

- `PROMPT_HISTORY.md`
- `prompt_history/`
- `docs/AI_DISCLOSURE.md`

The AI disclosure describes the tools used and the role of AI assistance. The candidate is responsible for reviewing, understanding, and explaining the final implementation.

---

## 15. Project Status

The current local verification confirms:

- Database initialization completed successfully.
- Processing engines executed successfully.
- 109 automated tests passed.
- 114 validation checks passed.
- FastAPI health check returned a successful response.
- SQLite connectivity was confirmed.
- Streamlit dashboard launched successfully.

**Current stage:** Local implementation and verification completed. Final repository publication and challenge submission remain separate delivery steps.
