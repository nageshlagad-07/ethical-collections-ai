"""Export a SQL seed (schema + INSERTs for the 14 dataset tables) from the CSV dataset. Output: sql/seed.sql.gz"""
import gzip
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collections_ai import db, pipeline
from collections_ai.spec import DATASET_TABLES

data_dir = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "full")
out = ROOT / "sql" / "seed.sql.gz"
ds = pipeline.load_dataset_dir(data_dir)
conn = sqlite3.connect(":memory:")
conn.executescript((ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
for n in DATASET_TABLES:
    db.insert_frame(conn, n, ds.tables[n])
conn.commit()
with gzip.open(out, "wt", encoding="utf-8", compresslevel=9, newline="\n") as fh:
    fh.write("-- Seed data (synthetic) for the Ethical Collections platform. Load: sqlite3 data/collections.db < seed.sql\n")
    for line in conn.iterdump():
        fh.write(line + "\n")
print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
