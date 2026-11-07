"""Generate docs/DATA_DICTIONARY.md and the ER diagram from collections_ai/spec.py (single source of truth)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collections_ai.spec import TABLES, CORE_TABLES

CORE_LABEL = {t.name: f"{i + 1}. {t.name}" for i, t in enumerate([t for t in TABLES if t.core])}


def er_mermaid() -> str:
    lines = ["erDiagram"]
    for t in TABLES:
        for f in t.fields:
            if f.fk:
                parent = f.fk.split(".")[0]
                card = "|o--o{" if not f.required else "||--o{"
                lines.append(f"    {parent} {card} {t.name} : \"{f.name}\"")
    return "\n".join(lines)


def md() -> str:
    out = ["# Data Dictionary", "",
           "_Generated from `collections_ai/spec.py` by `python scripts/build_docs.py` - the same specification produces `sql/schema.sql`, so this document cannot drift from the database._", "",
           "Ten mandated entities are marked **[CORE]**. Supporting tables (bridge table, event logs, run outputs) are marked **[SUPPORT]**.",
           "Types: TEXT, INTEGER, REAL, DATE (ISO `YYYY-MM-DD`), DATETIME (ISO `YYYY-MM-DDTHH:MM:SS`, customer-local, no zone), BOOL (0/1), JSON (stored as TEXT).", "",
           "## Entity relationship diagram", "", "```mermaid", er_mermaid(), "```", "",
           "## Relationship and cardinality summary", ""]
    for t in TABLES:
        if t.cardinality:
            out.append(f"- **{t.name}**: " + "; ".join(t.cardinality))
    out += ["", "Many-to-many relationships use bridge tables: **Payment N:M Installment** via `payment_allocation` (explicit, auditable allocation rows).", ""]
    for t in TABLES:
        tag = "CORE" if t.core else "SUPPORT"
        out += [f"## {t.name} [{tag}]", "", t.description, "", f"- **Primary key:** `{t.pk}`"]
        fks = [f"`{f.name}` -> `{f.fk}`" + ("" if f.required else " (optional)") for f in t.fields if f.fk]
        out.append("- **Foreign keys:** " + (", ".join(fks) if fks else "none"))
        if t.unique_together:
            out.append("- **Composite uniqueness:** " + ", ".join("(" + ", ".join(u) + ")" for u in t.unique_together))
        if t.lifecycle:
            out.append(f"- **Lifecycle / states:** {t.lifecycle}")
        if t.table_checks:
            out.append("- **Table constraints:** " + "; ".join(f"`{c}`" for c in t.table_checks))
        out += ["", "| Field | Type | Req. | Allowed values / constraint | Unit | Unique | Validation / derivation rule |", "|---|---|---|---|---|---|---|"]
        for f in t.fields:
            allowed = ", ".join(f.allowed) if f.allowed else (f.check or "")
            out.append(f"| `{f.name}` | {f.type} | {'yes' if f.required else 'no'} | {allowed} | {f.unit} | {'yes' if (f.unique or f.name == t.pk) else ''} | {f.rule} |")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    (ROOT / "docs" / "DATA_DICTIONARY.md").write_text(md(), encoding="utf-8")
    (ROOT / "docs" / "ER_DIAGRAM.mmd").write_text(er_mermaid() + "\n", encoding="utf-8")
    print("wrote docs/DATA_DICTIONARY.md and docs/ER_DIAGRAM.mmd")
