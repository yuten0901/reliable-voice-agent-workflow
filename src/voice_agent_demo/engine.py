from __future__ import annotations

import hmac
import json
import re
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Literal

from .adapters import (
    Calendar,
    CalendarError,
    CallSessionStore,
    Crm,
    FixtureBindingError,
    InMemoryCallSessionStore,
    SimulatedInterruption,
    SpeechRecognitionError,
    SpeechToText,
    TextToSpeech,
)
from .models import CallCheckpoint, CallFixture, CallOutcome, Contact, TraceEvent

EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
NAME_PATTERN = re.compile(r"(?:my name is|i'm)\s+([a-z][a-z .'-]{1,40})", re.IGNORECASE)
SLOT_LABELS = {
    "tuesday-10am": "Tuesday at 10 AM",
    "wednesday-2pm": "Wednesday at 2 PM",
}


class SafeBoundaryError(RuntimeError):
    """raw provider/store messageを外へ出さない固定code例外。"""


class _InternalInterruption(RuntimeError):
    """orchestrator内部だけで生成するtest用control signal。"""


def validate_trace_hmac_key(key: bytes) -> None:
    """公開placeholderや低entropy鍵を拒否する。"""
    normalized = key.strip().lower()
    forbidden = {
        b"replace-with-an-environment-specific-secret",
        b"changeme",
        b"default",
        b"password",
        b"test-secret",
    }
    if len(key) < 32 or normalized in forbidden or len(set(key)) < 8:
        raise ValueError("trace_hmac_key_required")


class VoiceAgent:
    """予約用Voice Agentのprovider非依存オーケストレーター。"""

    def __init__(
        self,
        *,
        stt: SpeechToText,
        tts: TextToSpeech,
        calendar: Calendar,
        crm: Crm,
        trace_hmac_key: bytes,
        confidence_threshold: float = 0.65,
        max_stt_failures: int = 2,
        max_calendar_attempts: int = 2,
        max_confirmation_attempts: int = 2,
        session_store: CallSessionStore | None = None,
    ) -> None:
        validate_trace_hmac_key(trace_hmac_key)
        self._stt = stt
        self._tts = tts
        self._calendar = calendar
        self._crm = crm
        self._trace_hmac_key = trace_hmac_key
        self._confidence_threshold = confidence_threshold
        self._max_stt_failures = max_stt_failures
        self._max_calendar_attempts = max_calendar_attempts
        self._max_confirmation_attempts = max_confirmation_attempts
        self._session_store = session_store or InMemoryCallSessionStore()

    def run(
        self, fixture: CallFixture, *, interrupt_after_turns: int | None = None
    ) -> CallOutcome:
        """call単位claim内でfixtureを実行する。"""
        fixture_digest = self._fixture_digest(fixture)
        try:
            with self._session_store.claim(fixture.call_id, fixture_digest):
                return self._run_claimed(fixture, fixture_digest, interrupt_after_turns)
        except FixtureBindingError:
            raise FixtureBindingError("call_id_already_bound_to_different_fixture") from None
        except _InternalInterruption:
            raise SimulatedInterruption("simulated_process_interruption") from None
        except SafeBoundaryError as error:
            allowed = {
                "booking_requires_email",
                "booking_confirmation_without_booking",
                "internal_boundary_failure",
                "session_store_unavailable",
                "unknown_booking_slot",
            }
            code = str(error)
            safe_code = code if code in allowed else "internal_boundary_failure"
            raise SafeBoundaryError(safe_code) from None
        except Exception:
            raise SafeBoundaryError("internal_boundary_failure") from None

    def _run_claimed(
        self, fixture: CallFixture, fixture_digest: str, interrupt_after_turns: int | None
    ) -> CallOutcome:
        checkpoint = self._load(fixture.call_id)
        if checkpoint is not None and checkpoint.fixture_digest != fixture_digest:
            raise FixtureBindingError("call_id_already_bound_to_different_fixture")
        if checkpoint is not None and checkpoint.completed_outcome is not None:
            if checkpoint.booking_confirmation_status == "pending":
                self._deliver_booking_confirmation(checkpoint)
            if checkpoint.crm_synced:
                checkpoint.completed_outcome = self._outcome(checkpoint)
                self._save(checkpoint)
                return checkpoint.completed_outcome
            return self._sync_crm(checkpoint)
        if checkpoint is None:
            checkpoint = CallCheckpoint(
                call_id=fixture.call_id,
                fixture_digest=fixture_digest,
                next_turn_index=0,
                contact=Contact(contact_id="pending", caller_id=fixture.caller_id),
                booking=None,
                trace=[],
                audio_outputs=[],
            )
            self._emit(
                checkpoint,
                "call_started",
                call_ref=self._private_ref("call", fixture.call_id),
                caller_ref=self._private_ref("caller", fixture.caller_id),
            )
            self._speak(checkpoint, "Thank you for calling. How may I help you today?", "greeting")
            self._save(checkpoint)

        for processed, turn_index in enumerate(
            range(checkpoint.next_turn_index, len(fixture.turns)), start=1
        ):
            turn = fixture.turns[turn_index]
            try:
                transcript, confidence = self._stt.transcribe(turn)
            except Exception as error:
                checkpoint.consecutive_stt_failures += 1
                self._emit(
                    checkpoint,
                    "stt_failed",
                    reason=self._provider_reason(error, "stt_provider_failure"),
                    consecutive_failures=checkpoint.consecutive_stt_failures,
                )
                self._handle_unusable_speech(checkpoint)
            else:
                self._emit(
                    checkpoint,
                    "stt_transcribed",
                    audio_ref=self._private_ref("audio", turn.audio_ref),
                    confidence=confidence,
                )
                if confidence < self._confidence_threshold:
                    checkpoint.consecutive_stt_failures += 1
                    self._emit(
                        checkpoint,
                        "stt_rejected",
                        reason="low_confidence",
                        consecutive_failures=checkpoint.consecutive_stt_failures,
                    )
                    self._handle_unusable_speech(checkpoint)
                else:
                    checkpoint.consecutive_stt_failures = 0
                    self._capture(checkpoint.contact, transcript)
                    self._emit(
                        checkpoint,
                        "contact_fields_updated",
                        has_name=checkpoint.contact.name is not None,
                        has_email=checkpoint.contact.email is not None,
                        has_interest=checkpoint.contact.interest is not None,
                    )
                    slot = self._requested_slot(transcript)
                    if slot is not None and checkpoint.contact.email is not None:
                        self._try_booking(checkpoint, slot)
                    else:
                        prompt = self._next_prompt(checkpoint)
                        if prompt is not None:
                            self._speak(checkpoint, prompt[1], prompt[0])

            checkpoint.next_turn_index = turn_index + 1
            self._save(checkpoint)
            if checkpoint.contact.status != "in_progress":
                break
            if interrupt_after_turns is not None and processed >= interrupt_after_turns:
                raise _InternalInterruption

        if checkpoint.contact.status == "in_progress":
            checkpoint.contact.status = "incomplete"
            checkpoint.contact.notes.append("call_ended_before_booking")
        self._emit(checkpoint, "call_completed", disposition=checkpoint.contact.status)
        checkpoint.completed_outcome = self._outcome(checkpoint)
        checkpoint.crm_synced = False
        self._save(checkpoint)
        return self._sync_crm(checkpoint)

    def _sync_crm(self, checkpoint: CallCheckpoint) -> CallOutcome:
        if checkpoint.crm_operation_id is None:
            checkpoint.crm_operation_id = self._operation_id(
                "crm", checkpoint.call_id, "contact-upsert"
            )
            self._save(checkpoint)
        operation_id = checkpoint.crm_operation_id
        try:
            contact = self._crm.upsert(checkpoint.contact, operation_id=operation_id)
        except Exception as error:
            self._emit(
                checkpoint,
                "crm_upsert_failed",
                reason=self._provider_reason(error, "crm_provider_failure"),
            )
            checkpoint.crm_synced = False
        else:
            checkpoint.contact = contact
            checkpoint.crm_synced = True
            self._emit(
                checkpoint,
                "crm_upserted",
                contact_ref=self._private_ref("contact", contact.contact_id),
                status=contact.status,
            )
        checkpoint.completed_outcome = self._outcome(checkpoint)
        self._save(checkpoint)
        return checkpoint.completed_outcome

    def _try_booking(self, checkpoint: CallCheckpoint, slot: str) -> None:
        email = checkpoint.contact.email
        if email is None:
            raise SafeBoundaryError("booking_requires_email")
        if checkpoint.booking_operation_id is None:
            checkpoint.booking_operation_id = self._operation_id(
                "booking", checkpoint.call_id, slot
            )
            self._save(checkpoint)
        operation_id = checkpoint.booking_operation_id
        last_error = "calendar_provider_failure"
        for attempt in range(1, self._max_calendar_attempts + 1):
            try:
                booking = self._calendar.book(
                    slot=slot, email=email, operation_id=operation_id
                )
            except Exception as error:
                last_error = self._provider_reason(error, "calendar_provider_failure")
                self._emit(
                    checkpoint,
                    "calendar_booking_failed",
                    reason=last_error,
                    attempt=attempt,
                    retrying=attempt < self._max_calendar_attempts,
                )
                continue
            checkpoint.booking = booking
            checkpoint.contact.status = "booked"
            checkpoint.contact.booking_uri = booking.event_uri
            self._emit(
                checkpoint,
                "booking_created",
                slot_ref=self._private_ref("slot", booking.slot),
                booking_ref=self._private_ref("booking", booking.event_uri),
                attempt=attempt,
            )
            checkpoint.booking_confirmation_status = "pending"
            self._save(checkpoint)
            self._deliver_booking_confirmation(checkpoint)
            return
        checkpoint.contact.status = "callback_requested"
        checkpoint.contact.notes.append(last_error)
        self._speak(
            checkpoint,
            "I cannot confirm the calendar right now. A team member will follow up.",
            "calendar_callback",
        )
        self._emit(checkpoint, "fallback_requested", reason=last_error)

    def _next_prompt(self, checkpoint: CallCheckpoint) -> tuple[str, str] | None:
        contact = checkpoint.contact
        if contact.name is None:
            return "request_name", "May I have your name?"
        if contact.interest is None:
            return "request_interest", "Are you looking for assisted living or memory care?"
        if contact.email is None:
            return (
                "request_email",
                "What email address should receive the appointment confirmation?",
            )
        try:
            slots = self._calendar.available_slots()
        except Exception as error:
            contact.status = "callback_requested"
            self._emit(
                checkpoint,
                "calendar_availability_failed",
                reason=self._provider_reason(error, "calendar_provider_failure"),
            )
            self._speak(
                checkpoint,
                "I cannot access the calendar right now. A team member will follow up.",
                "calendar_callback",
            )
            return None
        labels = [SLOT_LABELS.get(slot, "another available time") for slot in slots[:2]]
        if not labels:
            contact.status = "callback_requested"
            contact.notes.append("no_available_slots")
            self._emit(checkpoint, "fallback_requested", reason="no_available_slots")
            self._speak(
                checkpoint,
                "There are no appointment times available. A team member will follow up.",
                "no_slots_callback",
            )
            return None
        if len(labels) == 1:
            return "offer_single_slot", f"I have {labels[0]}. Does that work for you?"
        return "offer_slots", f"I have {labels[0]} or {labels[1]}. Which works for you?"

    def _deliver_booking_confirmation(self, checkpoint: CallCheckpoint) -> None:
        booking = checkpoint.booking
        if booking is None:
            raise SafeBoundaryError("booking_confirmation_without_booking")
        label = SLOT_LABELS.get(booking.slot)
        if label is None:
            raise SafeBoundaryError("unknown_booking_slot")
        checkpoint.booking_confirmation_attempts += 1
        delivered = self._speak(
            checkpoint,
            f"Your appointment is confirmed for {label}.",
            "booking_confirmation",
        )
        if delivered:
            checkpoint.booking_confirmation_status = "delivered"
        elif checkpoint.booking_confirmation_attempts >= self._max_confirmation_attempts:
            checkpoint.booking_confirmation_status = "human_followup_required"
            checkpoint.contact.notes.append("booking_confirmation_delivery_failed")
            self._emit(
                checkpoint,
                "confirmation_delivery_escalated",
                reason="tts_provider_failure",
            )
        self._save(checkpoint)

    def _handle_unusable_speech(self, checkpoint: CallCheckpoint) -> None:
        if checkpoint.consecutive_stt_failures >= self._max_stt_failures:
            checkpoint.contact.status = "callback_requested"
            checkpoint.contact.notes.append("speech_recognition_failed")
            self._speak(
                checkpoint,
                "I am having trouble hearing you. A team member will call you back.",
                "human_callback",
            )
            self._emit(checkpoint, "fallback_requested", reason="stt_failure_limit")
        else:
            self._speak(
                checkpoint,
                "I did not catch that. Please say it once more.",
                "repeat_request",
            )

    def _speak(self, checkpoint: CallCheckpoint, text: str, prompt: str) -> bool:
        try:
            audio = self._tts.synthesize(text)
        except Exception as error:
            self._emit(
                checkpoint,
                "tts_failed",
                prompt=prompt,
                reason=self._provider_reason(error, "tts_provider_failure"),
            )
            return False
        checkpoint.audio_outputs.append(audio)
        self._emit(checkpoint, "tts_synthesized", prompt=prompt, bytes=len(audio), format="wav")
        return True

    def _load(self, call_id: str) -> CallCheckpoint | None:
        try:
            return self._session_store.load(call_id)
        except Exception:
            raise SafeBoundaryError("session_store_unavailable") from None

    def _save(self, checkpoint: CallCheckpoint) -> None:
        try:
            self._session_store.save(checkpoint)
        except Exception:
            raise SafeBoundaryError("session_store_unavailable") from None

    def _private_ref(self, purpose: str, value: str) -> str:
        digest = hmac.new(
            self._trace_hmac_key, f"{purpose}\0{value}".encode(), sha256
        ).hexdigest()[:24]
        return f"hmac-sha256:{digest}"

    def _operation_id(self, purpose: str, call_id: str, intent: str) -> str:
        return self._private_ref(f"operation:{purpose}", f"{call_id}\0{intent}")

    @staticmethod
    def _provider_reason(error: Exception, fallback: str) -> str:
        if isinstance(error, SpeechRecognitionError) and str(error) in {
            "timeout",
            "provider_error",
        }:
            return str(error)
        if isinstance(error, CalendarError) and str(error) in {
            "calendar_provider_unavailable",
            "slot_unavailable",
        }:
            return str(error)
        return fallback

    @staticmethod
    def _emit(checkpoint: CallCheckpoint, event: str, **detail: object) -> None:
        checkpoint.trace.append(TraceEvent(len(checkpoint.trace) + 1, event, detail))

    @staticmethod
    def _outcome(checkpoint: CallCheckpoint) -> CallOutcome:
        disposition: Literal["booked", "callback_requested", "incomplete"] = (
            "booked"
            if checkpoint.contact.status == "booked"
            else "callback_requested"
            if checkpoint.contact.status == "callback_requested"
            else "incomplete"
        )
        return CallOutcome(
            call_id=checkpoint.call_id,
            disposition=disposition,
            contact=replace(checkpoint.contact, notes=list(checkpoint.contact.notes)),
            booking=checkpoint.booking,
            trace=tuple(checkpoint.trace),
            audio_outputs=tuple(checkpoint.audio_outputs),
        )

    @staticmethod
    def _capture(contact: Contact, transcript: str) -> None:
        name_match = NAME_PATTERN.search(transcript)
        if name_match:
            contact.name = name_match.group(1).strip(" .,'-").title()
        email_match = EMAIL_PATTERN.search(transcript)
        if email_match:
            contact.email = email_match.group(0).casefold()
        lowered = transcript.casefold()
        if "assisted living" in lowered:
            contact.interest = "assisted_living"
        elif "memory care" in lowered:
            contact.interest = "memory_care"

    @staticmethod
    def _requested_slot(transcript: str) -> str | None:
        lowered = transcript.casefold()
        if "tuesday" in lowered and ("10" in lowered or "ten" in lowered):
            return "tuesday-10am"
        if "wednesday" in lowered and ("2" in lowered or "two" in lowered):
            return "wednesday-2pm"
        return None

    @staticmethod
    def _fixture_digest(fixture: CallFixture) -> str:
        serialized = json.dumps(asdict(fixture), sort_keys=True, separators=(",", ":"))
        return sha256(serialized.encode()).hexdigest()
