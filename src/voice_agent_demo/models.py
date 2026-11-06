from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Turn:
    audio_ref: str
    transcript: str
    confidence: float = 1.0
    failure: Literal["none", "timeout", "provider_error"] = "none"


@dataclass(frozen=True)
class CallFixture:
    call_id: str
    caller_id: str
    turns: tuple[Turn, ...]
    calendar_failure: bool = False


@dataclass(frozen=True)
class Booking:
    event_uri: str
    slot: str
    invitee_email: str


@dataclass
class Contact:
    contact_id: str
    caller_id: str
    name: str | None = None
    email: str | None = None
    interest: str | None = None
    status: str = "in_progress"
    booking_uri: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    event: str
    detail: dict[str, object]


@dataclass(frozen=True)
class CallOutcome:
    call_id: str
    disposition: Literal["booked", "callback_requested", "incomplete"]
    contact: Contact
    booking: Booking | None
    trace: tuple[TraceEvent, ...]
    audio_outputs: tuple[bytes, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["audio_outputs"] = [
            {"bytes": len(audio), "format": "wav"} for audio in self.audio_outputs
        ]
        return result

    def to_safe_dict(self) -> dict[str, object]:
        """PIIを含まない既定CLI representationを返す。"""
        return {
            "call_ref": next(
                (
                    event.detail["call_ref"]
                    for event in self.trace
                    if event.event == "call_started" and "call_ref" in event.detail
                ),
                "unavailable",
            ),
            "disposition": self.disposition,
            "booking": {"created": self.booking is not None},
            "trace": [asdict(event) for event in self.trace],
            "audio_outputs": [
                {"bytes": len(audio), "format": "wav"} for audio in self.audio_outputs
            ],
        }


@dataclass
class CallCheckpoint:
    """同じprocess内で中断後の再開を検証するための状態。"""

    call_id: str
    fixture_digest: str
    next_turn_index: int
    contact: Contact
    booking: Booking | None
    trace: list[TraceEvent]
    audio_outputs: list[bytes]
    consecutive_stt_failures: int = 0
    completed_outcome: CallOutcome | None = None
    crm_synced: bool = False
    booking_operation_id: str | None = None
    crm_operation_id: str | None = None
    booking_confirmation_status: Literal[
        "not_required", "pending", "delivered", "human_followup_required"
    ] = "not_required"
    booking_confirmation_attempts: int = 0
