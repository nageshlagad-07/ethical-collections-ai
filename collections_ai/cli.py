"""Command line interface: python -m collections_ai.cli <command> ..."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from . import db, pipeline
from .config import GenConfig, ROOT, DEFAULT_SEED, REFERENCE_DATE, default_db_path
from .generator import generate
from .spec import build_schema_sql


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="collections_ai", description="Ethical Loan Collections Intelligence Platform")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="regenerate sql/schema.sql from the entity spec")
    g = sub.add_parser("generate", help="generate synthetic dataset (CSV + ground truth + manifest)")
    for p in (g,):
        p.add_argument("--loans", type=int, default=10_000)
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--out", default="data/full")
        p.add_argument("--no-anomalies", action="store_true")
    d = sub.add_parser("init-db", help="create the SQLite DB and load a generated dataset")
    d.add_argument("--data", default="data/full")
    d.add_argument("--db", default=str(default_db_path()))
    e = sub.add_parser("run-engines", help="reconcile payments, prioritize cases, detect exceptions, validate (persisted)")
    e.add_argument("--db", default=str(default_db_path()))
    r = sub.add_parser("reproducibility", help="generate twice with the same seed and compare")
    r.add_argument("--loans", type=int, default=10_000)
    r.add_argument("--seed", type=int, default=DEFAULT_SEED)
    pl = sub.add_parser("pipeline", help="full end-to-end build: data, DB, engines, reports")
    pl.add_argument("--loans", type=int, default=10_000)
    pl.add_argument("--seed", type=int, default=DEFAULT_SEED)
    pl.add_argument("--out", default="data/full")
    pl.add_argument("--db", default=str(default_db_path()))
    a = ap.parse_args(argv)

    if a.cmd == "schema":
        (ROOT / "sql" / "schema.sql").write_text(build_schema_sql(), encoding="utf-8")
        print("wrote sql/schema.sql")
    elif a.cmd == "generate":
        cfg = GenConfig(n_loans=a.loans, seed=a.seed, target_dir=a.out)
        ds = generate(cfg, inject=not a.no_anomalies, verbose=True)
        man = pipeline.export_dataset(ds, a.out, f"python -m collections_ai.cli generate --loans {a.loans} --seed {a.seed} --out {a.out}")
        print(json.dumps({"rows": ds.stats, "dataset_sha256": man["dataset_sha256"]}, indent=2))
    elif a.cmd == "init-db":
        ds = pipeline.load_dataset_dir(a.data)
        print(json.dumps(pipeline.build_database(ds, a.db), indent=2))
    elif a.cmd == "run-engines":
        conn = db.connect(a.db)
        ref = REFERENCE_DATE
        print(json.dumps({"reconciliation": pipeline.reconcile_and_persist(conn, ref), "prioritization": pipeline.persist_prioritization(conn, ref),
                          "exceptions": pipeline.persist_exceptions(conn, ref)}, indent=2, default=str))
        print(json.dumps(pipeline.persist_validation(conn, ref), indent=2, default=str))
    elif a.cmd == "reproducibility":
        rep = pipeline.reproducibility_check(GenConfig(n_loans=a.loans, seed=a.seed), ROOT / "reports")
        print(json.dumps({k: v for k, v in rep.items() if k != "per_file_sha256"}, indent=2))
        return 0 if rep["reproducible"] else 1
    elif a.cmd == "pipeline":
        cfg = GenConfig(n_loans=a.loans, seed=a.seed, target_dir=a.out)
        s = pipeline.full_pipeline(cfg, a.out, a.db, ROOT / "reports", ROOT / "docs")
        print(json.dumps({k: v for k, v in s.items() if k not in ("evaluation",)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
