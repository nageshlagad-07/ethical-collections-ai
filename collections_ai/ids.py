"""Deterministic, immutable identifier builders (zero-padded counters)."""
from __future__ import annotations

import re

YEAR = 2026


def customer_id(n: int) -> str: return f"CUS-{n:06d}"
def account_id(n: int) -> str: return f"ACC-{n:06d}"
def loan_id(n: int) -> str: return f"LOAN-{YEAR}-{n:06d}"
def installment_id(loan: str, seq: int) -> str: return f"INS-{loan}-{seq:03d}"
def snapshot_id(n: int) -> str: return f"SNP-{YEAR}-{n:06d}"
def preference_id(n: int) -> str: return f"CPF-{YEAR}-{n:06d}"
def case_id(n: int) -> str: return f"COL-{YEAR}-{n:06d}"
def attempt_id(case: str, seq: int) -> str: return f"CTP-{case}-{seq:03d}"
def promise_id(n: int) -> str: return f"PTP-{YEAR}-{n:06d}"
def payment_id(n: int) -> str: return f"PMT-{YEAR}-{n:06d}"
def allocation_id(n: int) -> str: return f"ALC-{YEAR}-{n:06d}"
def flag_event_id(n: int) -> str: return f"FLG-{YEAR}-{n:06d}"
def scheduling_log_id(n: int) -> str: return f"SCH-{YEAR}-{n:06d}"
def audit_id(n: int) -> str: return f"AUD-{YEAR}-{n:06d}"


def counter_of(identifier: str) -> int:
    """Numeric counter embedded at the end of an id (for generating the next one)."""
    m = re.search(r"(\d+)$", identifier)
    if not m:
        raise ValueError(f"No counter in id {identifier!r}")
    return int(m.group(1))
