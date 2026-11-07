"""SQLite persistence: schema creation, bulk load, slices, audit helper, explicit transactions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from . import ids
from .config import ROOT
from .spec import TABLE_BY_NAME, DATASET_TABLES, build_schema_sql

SCHEMA_PATH = ROOT / "sql" / "schema.sql"
SLICE_TABLES = ["customer", "account", "loan", "installment", "payment", "payment_allocation", "contact_preference",
                "flag_event", "contact_attempt", "promise_to_pay", "collection_case", "delinquency_snapshot",
                "contact_scheduling_log"]


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{Path(path).as_posix()}?mode=ro" if readonly else str(path)
    conn = sqlite3.connect(uri, uri=readonly, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(path: str | Path, reset: bool = True) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
    conn = connect(p)
    sql = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else build_schema_sql()
    conn.executescript(sql)
    return conn


def normalize_types(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Coerce columns to their spec types (used for CSV export, CSV import and DB load)."""
    df = df.copy()
    for f in TABLE_BY_NAME[table].fields:
        if f.name not in df.columns:
            continue
        if f.type in ("INTEGER", "BOOL"):
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce").astype("Int64")
        elif f.type == "REAL":
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce").round(2)
    return df[TABLE_BY_NAME[table].columns]


def _py(v):
    if v is None or (not isinstance(v, (str, list, dict)) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def insert_frame(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    cols = TABLE_BY_NAME[table].columns
    df = normalize_types(table, df)
    rows = [tuple(_py(v) for v in r) for r in df[cols].itertuples(index=False, name=None)]
    conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    return len(rows)


def load_dataset(conn: sqlite3.Connection, tables: dict) -> dict:
    counts = {}
    with transaction(conn):
        for name in DATASET_TABLES:
            counts[name] = insert_frame(conn, name, tables[name])
    return counts


def read_table(conn: sqlite3.Connection, name: str, where: str = "", params: Iterable = ()) -> pd.DataFrame:
    df = pd.read_sql_query(f"SELECT * FROM {name} {where}", conn, params=list(params))
    return df


def load_tables(conn: sqlite3.Connection, names: Optional[list] = None) -> dict:
    return {n: read_table(conn, n) for n in (names or DATASET_TABLES)}


def _in(col: str, values: list) -> tuple[str, list]:
    return f"WHERE {col} IN ({','.join('?' * len(values))})", list(values)


def load_slice(conn: sqlite3.Connection, loan_ids: list) -> dict:
    """All tables restricted to the given loans and their customers (for single-case API operations)."""
    loan_ids = list(dict.fromkeys(loan_ids))
    w, p = _in("loan_id", loan_ids)
    t = {"loan": read_table(conn, "loan", w, p)}
    custs = sorted(set(t["loan"]["customer_id"]))
    accts = sorted(set(t["loan"]["account_id"]))
    wc, pc = _in("customer_id", custs)
    t["customer"] = read_table(conn, "customer", wc, pc)
    t["account"] = read_table(conn, "account", *_in("account_id", accts))
    t["installment"] = read_table(conn, "installment", w, p)
    t["payment"] = read_table(conn, "payment", w, p)
    pids = list(t["payment"]["payment_id"]) or [""]
    t["payment_allocation"] = read_table(conn, "payment_allocation", *_in("payment_id", pids))
    t["contact_preference"] = read_table(conn, "contact_preference", wc, pc)
    t["flag_event"] = read_table(conn, "flag_event", wc, pc)
    t["contact_attempt"] = read_table(conn, "contact_attempt", wc, pc)
    t["promise_to_pay"] = read_table(conn, "promise_to_pay", w, p)
    t["collection_case"] = read_table(conn, "collection_case", w, p)
    return t


# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def next_counter(conn: sqlite3.Connection, table: str, pk: str, pattern_len_prefix: str) -> int:
    row = conn.execute(f"SELECT MAX({pk}) AS m FROM {table} WHERE {pk} LIKE ?", (pattern_len_prefix + "%",)).fetchone()
    return ids.counter_of(row["m"]) + 1 if row["m"] else 1


def write_audit(conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str, old=None, new=None,
                actor: str = "SYSTEM", reason: Optional[str] = None, details: Optional[dict] = None) -> str:
    """Append an audit event (call inside an open transaction)."""
    aid = ids.audit_id(next_counter(conn, "audit_event", "audit_id", "AUD-"))
    conn.execute("INSERT INTO audit_event VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (aid, entity_type, entity_id, action, old, new, actor, reason, now_iso(), json.dumps(details) if details else None))
    return aid
