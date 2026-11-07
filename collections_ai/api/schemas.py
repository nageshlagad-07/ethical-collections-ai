"""Pydantic models. Response models for table rows are generated from the entity spec."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from ..spec import TABLE_BY_NAME, CHANNELS, PURPOSES, OUTCOMES, PAYMENT_CHANNELS, CASE_STATUSES, FLAG_TYPES, FLAG_EVENT_TYPES, EXCEPTION_STATUSES

_PY = {"TEXT": str, "DATE": str, "DATETIME": str, "JSON": str, "INTEGER": int, "BOOL": int, "REAL": float}


def row_model(table: str) -> type[BaseModel]:
    spec = TABLE_BY_NAME[table]
    fields = {f.name: (Optional[_PY[f.type]], None) for f in spec.fields}
    return create_model(f"{table.title().replace('_', '')}Out", __config__=ConfigDict(from_attributes=True), **fields)


CustomerOut = row_model("customer")
LoanOut = row_model("loan")
InstallmentOut = row_model("installment")
SnapshotOut = row_model("delinquency_snapshot")
CaseOut = row_model("collection_case")
AttemptOut = row_model("contact_attempt")
PromiseOut = row_model("promise_to_pay")
PaymentOut = row_model("payment")
AllocationOut = row_model("payment_allocation")
SchedulingLogOut = row_model("contact_scheduling_log")
AuditOut = row_model("audit_event")
ExceptionOut = row_model("exception_record")
PrioritizationOut = row_model("prioritization_result")
FlagOut = row_model("flag_event")


class Health(BaseModel):
    status: str
    database: str
    loans: int
    reference_date: str
    version: str


class DelinquencyOut(BaseModel):
    loan_id: str
    as_of: str
    total_payable: float
    total_paid: float
    outstanding_balance: float
    arrears: float
    days_past_due: int
    overdue_installments: int
    loan_status: str
    bucket: str


class CaseCreate(BaseModel):
    loan_id: str
    assigned_analyst: str = Field("ANL-01", pattern=r"^ANL-\d{2}$")
    actor: str = "ANALYST"


class CaseStatusChange(BaseModel):
    new_status: str
    reason: str = Field(min_length=3)
    actor: str = "ANALYST"

    @field_validator("new_status")
    @classmethod
    def _valid(cls, v: str) -> str:
        if v not in CASE_STATUSES:
            raise ValueError(f"new_status must be one of {CASE_STATUSES}")
        return v


class ContactRequestIn(BaseModel):
    case_id: str
    channel: str
    scheduled_for: datetime
    purpose: str = "COLLECTION_REMINDER"
    requested_by: str = "ANALYST"

    @field_validator("channel")
    @classmethod
    def _ch(cls, v: str) -> str:
        if v not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}")
        return v

    @field_validator("purpose")
    @classmethod
    def _pu(cls, v: str) -> str:
        if v not in PURPOSES:
            raise ValueError(f"purpose must be one of {PURPOSES}")
        return v


class EligibilityOut(BaseModel):
    permitted: bool
    reason_codes: list[str]
    explanations: list[str]
    effective_window: Optional[list[str]] = None
    rules_version: str
    disclaimer: str = "Illustrative configurable policy parameters; legal compliance requires review against applicable regulations."


class ScheduleOut(BaseModel):
    decision: str
    log_id: str
    attempt_id: Optional[str] = None
    eligibility: EligibilityOut


class OutcomeIn(BaseModel):
    outcome: str
    actor: str = "ANALYST"

    @field_validator("outcome")
    @classmethod
    def _o(cls, v: str) -> str:
        if v not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        return v


class FlagIn(BaseModel):
    customer_id: str
    loan_id: Optional[str] = None
    flag_type: str
    event_type: str
    note: Optional[str] = None
    actor: str = "ANALYST"
    event_at: Optional[datetime] = None

    @field_validator("flag_type")
    @classmethod
    def _f(cls, v: str) -> str:
        if v not in FLAG_TYPES:
            raise ValueError(f"flag_type must be one of {FLAG_TYPES}")
        return v

    @field_validator("event_type")
    @classmethod
    def _e(cls, v: str) -> str:
        if v not in FLAG_EVENT_TYPES:
            raise ValueError(f"event_type must be one of {FLAG_EVENT_TYPES}")
        return v


class PromiseIn(BaseModel):
    case_id: str
    promised_amount: float = Field(gt=0)
    promised_date: date
    created_date: Optional[date] = None
    attempt_id: Optional[str] = None
    exception_approved_by: Optional[str] = None
    exception_reason: Optional[str] = None
    actor: str = "ANALYST"


class PaymentIn(BaseModel):
    loan_id: str
    amount: float = Field(gt=0)
    payment_date: Optional[date] = None
    channel: str = "BANK_TRANSFER"
    reference: str = Field(min_length=6, max_length=40)
    actor: str = "SYSTEM"

    @field_validator("channel")
    @classmethod
    def _c(cls, v: str) -> str:
        if v not in PAYMENT_CHANNELS:
            raise ValueError(f"channel must be one of {PAYMENT_CHANNELS}")
        return v


class PaymentResult(BaseModel):
    payment: Any
    allocations: list[Any]
    loan_status: str
    outstanding_balance: float
    case_resolved: Optional[str] = None
    promises_updated: list[str] = []


class ExceptionStatusIn(BaseModel):
    status: str
    note: Optional[str] = None
    actor: str = "ANALYST"

    @field_validator("status")
    @classmethod
    def _s(cls, v: str) -> str:
        if v not in EXCEPTION_STATUSES:
            raise ValueError(f"status must be one of {EXCEPTION_STATUSES}")
        return v
