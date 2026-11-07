"""Orchestration: export, DB build, engine runs, validation reports, reproducibility check."""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import faker
import numpy as np
import pandas as pd

from . import __version__, db
from .config import GenConfig, ROOT
from .detection import detect_all, evaluate_against_ground_truth
from .generator import Dataset, generate
from .prioritization import prioritize_cases
from .rules import load_rules
from .spec import DATASET_TABLES, TABLE_BY_NAME
from .validation import run_validation, summarize


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------- export / import
def export_dataset(ds: Dataset, outdir: str | Path, command: str = "") -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in DATASET_TABLES:
        p = out / f"{name}.csv"
        db.normalize_types(name, ds.tables[name]).to_csv(p, index=False, float_format="%.2f", lineterminator="\n")
        files[p.name] = {"rows": int(len(ds.tables[name])), "sha256": sha256_file(p)}
    gt_csv, gt_json = out / "ground_truth.csv", out / "ground_truth.json"
    ds.ground_truth.to_csv(gt_csv, index=False, lineterminator="\n")
    gt_json.write_text(json.dumps(ds.ground_truth.to_dict("records"), indent=1, sort_keys=True), encoding="utf-8")
    files[gt_csv.name] = {"rows": int(len(ds.ground_truth)), "sha256": sha256_file(gt_csv)}
    files[gt_json.name] = {"rows": int(len(ds.ground_truth)), "sha256": sha256_file(gt_json)}
    combined = hashlib.sha256("".join(f"{k}:{v['sha256']}" for k, v in sorted(files.items())).encode()).hexdigest()
    manifest = {
        "generator": f"collections_ai {__version__}", "generator_command": command, "config": ds.config,
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__, "faker": faker.VERSION},
        "files": files, "dataset_sha256": combined,
        "ground_truth_note": "ground_truth.* are labels for evaluation ONLY; they are never read by detection or prioritization.",
        "nondeterministic_fields": "None in generated CSVs. Operational fields written at run time (run_log.started_at, API-created created_at/updated_at/event_ts, API-created audit rows) use wall-clock time.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def load_dataset_dir(path: str | Path) -> Dataset:
    p = Path(path)
    tables = {n: db.normalize_types(n, pd.read_csv(p / f"{n}.csv", dtype=str, keep_default_na=False, na_values=[""]).pipe(_typed, n)) for n in DATASET_TABLES}
    gt = pd.read_csv(p / "ground_truth.csv", dtype=str, keep_default_na=False, na_values=[""])
    man = json.loads((p / "manifest.json").read_text())
    return Dataset(tables, gt, man["config"])


def _typed(df: pd.DataFrame, table: str) -> pd.DataFrame:
    for f in TABLE_BY_NAME[table].fields:
        if f.type in ("INTEGER", "BOOL", "REAL") and f.name in df.columns:
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce")
    return df


# ---------------------------------------------------------------------------- DB build + engine runs
def build_database(ds: Dataset, db_path: str | Path) -> dict:
    conn = db.init_db(db_path, reset=True)
    counts = db.load_dataset(conn, ds.tables)
    conn.close()
    return counts


def _new_run(conn, run_type: str, ref: date, summary: dict, prefix: str) -> str:
    n = conn.execute("SELECT COUNT(*) FROM run_log WHERE run_type=?", (run_type,)).fetchone()[0] + 1
    run_id = f"{prefix}-{ref:%Y%m%d}-{n:03d}"
    conn.execute("INSERT INTO run_log VALUES (?,?,?,?,?,?)",
                 (run_id, run_type, ref.isoformat(), db.now_iso(), load_rules()["rules_version"], json.dumps(summary)))
    return run_id


def persist_prioritization(conn, ref: date, case_ids=None) -> dict:
    t = db.load_tables(conn)
    res = prioritize_cases(t, ref, case_ids=case_ids)
    summary = pd.Series([r["priority_category"] for r in res]).value_counts().to_dict()
    with db.transaction(conn):
        run_id = _new_run(conn, "PRIORITIZATION", ref, {"cases": len(res), "by_category": summary}, "PRI")
        conn.executemany("INSERT INTO prioritization_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (f"{run_id}:{r['case_id']}", run_id, r["case_id"], r["as_of_date"], r["priority_score"], r["priority_category"],
             json.dumps(r["contributing_factors"]), r["reason_codes"], r["suppression_reasons"], r["recommended_action"],
             r["recommended_channel"], r["recommended_slot"], r["explanation"]) for r in res])
        db.write_audit(conn, "RUN", run_id, "PRIORITIZATION_RUN", None, json.dumps(summary), "SYSTEM", "Prioritization run persisted")
    return {"run_id": run_id, "cases": len(res), "by_category": summary}


def persist_exceptions(conn, ref: date) -> dict:
    t = db.load_tables(conn)
    det = detect_all(t, ref)
    summary = pd.Series([d["exception_type"] for d in det]).value_counts().to_dict()
    with db.transaction(conn):
        run_id = _new_run(conn, "EXCEPTION_DETECTION", ref, {"exceptions": len(det), "by_type": summary}, "EXC")
        conn.executemany("INSERT INTO exception_record VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (f"{run_id}:{i + 1:05d}", run_id, d["exception_type"], d["severity"], d["entity_type"], d["entity_id"], d["case_id"], d["loan_id"],
             d["customer_id"], d["description"], d["expected_decision"], d["system_response"], ref.isoformat(), "OPEN") for i, d in enumerate(det)])
        db.write_audit(conn, "RUN", run_id, "EXCEPTION_DETECTION_RUN", None, json.dumps(summary), "SYSTEM", "Exception detection persisted")
    return {"run_id": run_id, "exceptions": len(det), "by_type": summary}


def persist_validation(conn, ref: date) -> dict:
    t = db.load_tables(conn)
    results = run_validation(t, ref)
    summ = summarize(results)
    with db.transaction(conn):
        run_id = _new_run(conn, "VALIDATION", ref, summ, "VAL")
        conn.executemany("INSERT INTO validation_result VALUES (?,?,?,?,?,?,?,?)", [
            (f"{run_id}:V{i + 1:03d}", run_id, r["check_name"], r["category"], r["severity"], r["passed"], r["violation_count"], r["detail"])
            for i, r in enumerate(results)])
        db.write_audit(conn, "RUN", run_id, "VALIDATION_RUN", None, json.dumps(summ["exceptions_detected"]), "SYSTEM", "Validation persisted")
    return {"run_id": run_id, **summ}


def reconcile_and_persist(conn, ref: date) -> dict:
    """Recompute payment reconciliation_status; audit every status change (history is kept in audit_event)."""
    from .reconciliation import reconcile_payments
    t = db.load_tables(conn, ["installment", "payment", "payment_allocation"])
    rec = reconcile_payments(t["installment"], t["payment"], t["payment_allocation"])
    cur = dict(zip(t["payment"]["payment_id"], t["payment"]["reconciliation_status"]))
    changed = rec[rec["payment_id"].map(cur) != rec["reconciliation_status"]]
    with db.transaction(conn):
        run_id = _new_run(conn, "RECONCILIATION", ref, {"payments": len(rec), "changed": len(changed)}, "REC")
        for r in changed.itertuples(index=False):
            conn.execute("UPDATE payment SET reconciliation_status=?, updated_at=? WHERE payment_id=?", (r.reconciliation_status, db.now_iso(), r.payment_id))
            db.write_audit(conn, "PAYMENT", r.payment_id, "RECONCILIATION_STATUS_CHANGED", cur[r.payment_id], r.reconciliation_status, "SYSTEM", r.reason)
    return {"run_id": run_id, "payments": len(rec), "changed": len(changed), "status_counts": rec["reconciliation_status"].value_counts().to_dict()}


# ---------------------------------------------------------------------------- reproducibility
def reproducibility_check(cfg: GenConfig, report_dir: str | Path | None = None) -> dict:
    """Generate twice with the same seed into temp dirs and compare counts, ids, checksums and ground truth."""
    tmp = Path(tempfile.mkdtemp(prefix="repro_"))
    try:
        runs = []
        for i in (1, 2):
            ds = generate(cfg)
            man = export_dataset(ds, tmp / f"run{i}")
            runs.append((ds, man))
        (d1, m1), (d2, m2) = runs
        counts_equal = {k: m1["files"][k]["rows"] == m2["files"][k]["rows"] for k in m1["files"]}
        sums_equal = {k: m1["files"][k]["sha256"] == m2["files"][k]["sha256"] for k in m1["files"]}
        ids_equal = {n: d1.tables[n][TABLE_BY_NAME[n].pk].tolist() == d2.tables[n][TABLE_BY_NAME[n].pk].tolist() for n in DATASET_TABLES}
        gt_equal = d1.ground_truth.equals(d2.ground_truth)
        report = {
            "seed": cfg.seed, "n_loans": cfg.n_loans, "row_counts": {k: v["rows"] for k, v in m1["files"].items()},
            "row_counts_identical": all(counts_equal.values()), "deterministic_ids_identical": all(ids_equal.values()),
            "file_checksums_identical": all(sums_equal.values()), "ground_truth_identical": bool(gt_equal),
            "dataset_sha256_run1": m1["dataset_sha256"], "dataset_sha256_run2": m2["dataset_sha256"],
            "per_file_sha256": {k: v["sha256"] for k, v in m1["files"].items()},
            "reproducible": bool(all(counts_equal.values()) and all(ids_equal.values()) and all(sums_equal.values()) and gt_equal),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if report_dir:
        rd = Path(report_dir)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "reproducibility_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


# ---------------------------------------------------------------------------- reports
def render_validation_markdown(cfg: GenConfig, baseline: list, final: list, evaluation: pd.DataFrame, gt_counts: dict, repro: dict | None) -> str:
    bs, fs = summarize(baseline), summarize(final)
    L = ["# Validation Report", "",
         f"_Generated by `python -m collections_ai.cli pipeline` on {datetime.now():%Y-%m-%d %H:%M} for seed `{cfg.seed}`, "
         f"{cfg.n_loans:,} loans, reference date {cfg.reference_date}. All numbers below are actual results from that run._", "",
         "## 1. Summary", "",
         "| Dataset | Integrity checks | Passed | Failed | Business-rule exceptions found |", "|---|---:|---:|---:|---|",
         f"| Baseline (before anomaly injection) | {bs['checks_total']} | {bs['checks_passed']} | {bs['checks_failed']} | {sum(bs['exceptions_detected'].values())} |",
         f"| Final (with 5 labelled anomaly types) | {fs['checks_total']} | {fs['checks_passed']} | {fs['checks_failed']} | {sum(fs['exceptions_detected'].values())} |", "",
         "Structural, arithmetic and lifecycle checks must pass on both datasets: anomaly injection changes *business* facts only, never keys or arithmetic. "
         "The baseline must contain **zero** business-rule exceptions (proves normal data is clean); the final dataset must contain exactly the injected ones.", "",
         "## 2. Detection vs separately stored ground truth", "",
         "Detectors read source tables only. `ground_truth.csv` is used here purely to score them.", "",
         evaluation.to_markdown(index=False), ""]
    if repro:
        L += ["## 3. Reproducibility (same seed, two runs)", "",
              f"- Row counts identical: **{repro['row_counts_identical']}**", f"- Deterministic IDs identical: **{repro['deterministic_ids_identical']}**",
              f"- Per-file SHA-256 checksums identical: **{repro['file_checksums_identical']}**", f"- Ground-truth records identical: **{repro['ground_truth_identical']}**",
              f"- Dataset SHA-256 (run 1): `{repro['dataset_sha256_run1']}`", f"- Dataset SHA-256 (run 2): `{repro['dataset_sha256_run2']}`", ""]
    L += ["## 4. All checks - final dataset", "", "| Category | Check | Violations | Result |", "|---|---|---:|---|"]
    for r in final:
        if r["category"] == "BUSINESS_RULE_EXCEPTION":
            res = "expected (injected)" if r["violation_count"] else "none"
        else:
            res = "PASS" if r["passed"] else "**FAIL**"
        L.append(f"| {r['category']} | `{r['check_name']}` | {r['violation_count']} | {res} |")
    L += ["", "## 5. Baseline business-rule exceptions (must be 0)", "", "| Exception type | Baseline count |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in bs["exceptions_detected"].items()]
    return "\n".join(L) + "\n"


def full_pipeline(cfg: GenConfig, out_dir: str | Path, db_path: str | Path, report_dir: str | Path, docs_dir: str | Path, log=print) -> dict:
    log(f"[1/6] Generating {cfg.n_loans:,} loans (seed {cfg.seed}) ...")
    base = generate(cfg, inject=False)
    baseline_results = run_validation(base.tables, cfg.reference_date)
    ds = generate(cfg, inject=True)
    log("[2/6] Exporting CSV + ground truth + manifest ...")
    cmd = f"python -m collections_ai.cli generate --loans {cfg.n_loans} --seed {cfg.seed} --out {out_dir}"
    manifest = export_dataset(ds, out_dir, cmd)
    log("[3/6] Validating final dataset ...")
    final_results = run_validation(ds.tables, cfg.reference_date)
    det = detect_all(ds.tables, cfg.reference_date)
    evaluation = evaluate_against_ground_truth(det, ds.ground_truth)
    log("[4/6] Reproducibility check (generating twice) ...")
    repro = reproducibility_check(cfg, report_dir)
    log("[5/6] Building SQLite database and running engines ...")
    counts = build_database(ds, db_path)
    conn = db.connect(db_path)
    rec = reconcile_and_persist(conn, cfg.reference_date)
    pri = persist_prioritization(conn, cfg.reference_date)
    exc = persist_exceptions(conn, cfg.reference_date)
    val = persist_validation(conn, cfg.reference_date)
    conn.close()
    log("[6/6] Writing reports ...")
    rd = Path(report_dir)
    rd.mkdir(parents=True, exist_ok=True)
    md = render_validation_markdown(cfg, baseline_results, final_results, evaluation, ds.stats, repro)
    (Path(docs_dir) / "VALIDATION_REPORT.md").write_text(md, encoding="utf-8")
    (rd / "validation_report.json").write_text(json.dumps({"baseline": baseline_results, "final": final_results,
                                                          "evaluation": evaluation.to_dict("records")}, indent=1), encoding="utf-8")
    summary = {"row_counts": ds.stats, "db_counts": counts, "reconciliation": rec, "prioritization": pri, "exceptions": exc,
               "validation": {k: v for k, v in val.items() if k != "run_id"}, "reproducible": repro["reproducible"],
               "dataset_sha256": manifest["dataset_sha256"], "evaluation": evaluation.to_dict("records")}
    (rd / "pipeline_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary
