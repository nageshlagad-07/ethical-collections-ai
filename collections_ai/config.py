"""Generation / runtime configuration.

The business reference date is fixed so generation, validation and tests are
reproducible. Every value can be overridden (e.g. through the CLI).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = 20261022
REFERENCE_DATE = date(2026, 10, 22)
ID_YEAR = 2026


@dataclass(frozen=True)
class GenConfig:
    seed: int = DEFAULT_SEED
    n_loans: int = 10_000
    installments_per_loan: int = 3
    reference_date: date = REFERENCE_DATE
    period_months: int = 12
    customers_per_loan: float = 0.72
    extra_account_share: float = 0.12
    anomalies_per_type: int = 0  # 0 => auto: max(3, round(n_loans * 0.003))
    target_dir: str = "data/full"

    @property
    def window_start(self) -> date:
        """First day of the 12-month data period."""
        return self.reference_date - timedelta(days=365) + timedelta(days=1)

    @property
    def last_origination(self) -> date:
        return self.reference_date - timedelta(days=15)

    @property
    def n_anomalies(self) -> int:
        return self.anomalies_per_type or max(3, round(self.n_loans * 0.003))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["reference_date"] = self.reference_date.isoformat()
        d["window_start"] = self.window_start.isoformat()
        d["n_anomalies_per_type"] = self.n_anomalies
        return d


def default_db_path() -> Path:
    return ROOT / "data" / "collections.db"
