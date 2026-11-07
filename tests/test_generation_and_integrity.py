"""Data generation, referential integrity, ids, reproducibility."""
import pandas as pd
import pytest

from collections_ai import pipeline
from collections_ai.config import GenConfig
from collections_ai.generator import generate
from collections_ai.spec import DATASET_TABLES, ID_PATTERNS, TABLE_BY_NAME
from collections_ai.validation import run_validation, summarize


def test_volume_baseline_and_parameterised_scale(tables, cfg):
    assert len(tables["loan"]) == cfg.n_loans
    assert len(tables["installment"]) == cfg.n_loans * 3


def test_full_scale_parameters_match_challenge():
    c = GenConfig()
    assert c.n_loans == 10_000 and c.seed == 20261022 and c.installments_per_loan == 3 and c.period_months == 12


def test_all_ten_entities_present(tables):
    for e in ["customer", "account", "loan", "installment", "delinquency_snapshot", "contact_preference",
              "contact_attempt", "promise_to_pay", "payment", "collection_case"]:
        assert e in tables and len(tables[e]) > 0


def test_id_conventions(tables):
    import re
    for n, pat in ID_PATTERNS.items():
        assert tables[n][TABLE_BY_NAME[n].pk].astype(str).map(lambda x: bool(re.match(pat, x))).all(), n
    assert tables["loan"]["loan_id"].iloc[0] == "LOAN-2026-000001"
    assert tables["collection_case"]["case_id"].iloc[0] == "COL-2026-000001"
    assert tables["payment"]["payment_id"].iloc[0] == "PMT-2026-000001"
    i = tables["installment"].iloc[0]
    assert i["installment_id"] == f"INS-{i['loan_id']}-001"
    a = tables["contact_attempt"].iloc[0]
    assert a["attempt_id"] == f"CTP-{a['case_id']}-{int(a['sequence_number']):03d}"


def test_validation_suite_passes_on_baseline_and_final(baseline, ds, cfg):
    for d in (baseline, ds):
        res = run_validation(d.tables, cfg.reference_date)
        failed = [r for r in res if not r["passed"] and r["category"] != "BUSINESS_RULE_EXCEPTION"]
        assert not failed, failed[:3]


def test_baseline_has_zero_business_exceptions(baseline, cfg):
    s = summarize(run_validation(baseline.tables, cfg.reference_date))
    assert sum(s["exceptions_detected"].values()) == 0


def test_referential_integrity_in_sqlite(api):
    from collections_ai import db
    import os
    conn = db.connect(os.environ["COLLECTIONS_DB"])
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_enforced_by_database(api):
    import os, sqlite3
    from collections_ai import db
    conn = db.connect(os.environ["COLLECTIONS_DB"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account VALUES ('ACC-999999','CUS-NOPE','SALARY_LINKED','AUTO_DEBIT','ACTIVE','2026-01-01',NULL,'INR','x','x')")


def test_realistic_variation_present(tables):
    assert set(tables["loan"]["loan_status"]) >= {"PAID_OFF", "CURRENT", "DELINQUENT", "DEFAULT"}
    assert set(tables["promise_to_pay"]["promise_status"]) >= {"FULFILLED", "BROKEN", "PARTIALLY_FULFILLED"}
    assert (tables["installment"]["installment_status"] == "PARTIALLY_PAID").any()
    assert (tables["contact_preference"]["opt_out_all"] == 1).any()
    assert set(tables["flag_event"]["flag_type"]) == {"HARDSHIP", "DISPUTE"}
    assert (tables["contact_scheduling_log"]["decision"] == "REJECTED").any()
    snaps = tables["delinquency_snapshot"]
    assert snaps["snapshot_date"].nunique() >= 12  # twelve-month period
    multi = tables["delinquency_snapshot"].query("overdue_installments >= 2")
    assert len(multi) > 0  # multiple overdue instalments


def test_no_real_personal_data(tables):
    assert tables["customer"]["email"].str.endswith("@example.invalid").all()
    assert tables["customer"]["phone"].str.startswith("+000-").all()


def test_reproducibility_same_seed_identical(tmp_path):
    cfg = GenConfig(n_loans=300)
    rep = pipeline.reproducibility_check(cfg)
    assert rep["reproducible"] and rep["row_counts_identical"] and rep["deterministic_ids_identical"]
    assert rep["file_checksums_identical"] and rep["ground_truth_identical"]


def test_different_seed_gives_different_data():
    a = generate(GenConfig(n_loans=200, seed=1), inject=False)
    b = generate(GenConfig(n_loans=200, seed=2), inject=False)
    assert not a.tables["payment"]["amount"].equals(b.tables["payment"]["amount"])


def test_csv_roundtrip_preserves_dataset(ds, tmp_path):
    pipeline.export_dataset(ds, tmp_path)
    back = pipeline.load_dataset_dir(tmp_path)
    for n in DATASET_TABLES:
        assert len(back.tables[n]) == len(ds.tables[n])
    assert len(back.ground_truth) == len(ds.ground_truth)
