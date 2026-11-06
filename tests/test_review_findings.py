from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from voice_agent_demo.adapters import (
    CrmError,
    FixtureBindingError,
    FixtureSpeechToText,
    InMemoryCalendly,
    InMemoryCallSessionStore,
    InMemoryCrm,
    LocalToneTextToSpeech,
    SimulatedInterruption,
)
from voice_agent_demo.engine import SafeBoundaryError, VoiceAgent
from voice_agent_demo.models import CallCheckpoint, CallFixture, Contact, Turn

KEY_A = b"review-key-a-with-enough-entropy-2026-only"
KEY_B = b"review-key-b-with-enough-entropy-2026-only"


def booking_fixture(call_id: str = "review-booking") -> CallFixture:
    return CallFixture(
        call_id=call_id,
        caller_id="+15551234567",
        turns=(
            Turn("name.wav", "My name is Canary Person"),
            Turn("interest.wav", "assisted living"),
            Turn("email.wav", "canary@example.test"),
            Turn("slot.wav", "Tuesday at ten"),
        ),
    )


def agent(
    *,
    calendar: object | None = None,
    crm: object | None = None,
    store: object | None = None,
    tts: object | None = None,
    stt: object | None = None,
    key: bytes = KEY_A,
) -> VoiceAgent:
    return VoiceAgent(
        stt=stt or FixtureSpeechToText(),  # type: ignore[arg-type]
        tts=tts or LocalToneTextToSpeech(),  # type: ignore[arg-type]
        calendar=calendar or InMemoryCalendly(),  # type: ignore[arg-type]
        crm=crm or InMemoryCrm(),  # type: ignore[arg-type]
        session_store=store,  # type: ignore[arg-type]
        trace_hmac_key=key,
    )


class OneShotFailingStore:
    def __init__(self, predicate: object) -> None:
        self.base = InMemoryCallSessionStore()
        self.predicate = predicate
        self.failed = False

    @contextmanager
    def claim(self, call_id: str, fixture_digest: str) -> Iterator[None]:
        with self.base.claim(call_id, fixture_digest):
            yield

    def load(self, call_id: str) -> CallCheckpoint | None:
        return self.base.load(call_id)

    def save(self, checkpoint: CallCheckpoint) -> None:
        should_fail = self.predicate(checkpoint)  # type: ignore[operator]
        if should_fail and not self.failed:
            self.failed = True
            raise RuntimeError("store leaked canary@example.test")
        self.base.save(checkpoint)


def test_concurrent_duplicate_call_is_claimed_once() -> None:
    fixture = booking_fixture("concurrent-call")
    store = InMemoryCallSessionStore()
    calendar = InMemoryCalendly()
    crm = InMemoryCrm()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: agent(calendar=calendar, crm=crm, store=store).run(fixture),
                range(2),
            )
        )

    assert outcomes[0] == outcomes[1]
    assert calendar.booking_writes == 1
    assert crm.write_count == 1


def test_booking_success_then_checkpoint_failure_reuses_operation_id() -> None:
    fixture = booking_fixture("booking-save-crash")
    store = OneShotFailingStore(
        lambda checkpoint: checkpoint.booking is not None
        and checkpoint.completed_outcome is None
    )
    calendar = InMemoryCalendly()
    crm = InMemoryCrm()

    with pytest.raises(SafeBoundaryError, match="session_store_unavailable"):
        agent(calendar=calendar, crm=crm, store=store).run(fixture)
    outcome = agent(calendar=calendar, crm=crm, store=store, key=KEY_B).run(fixture)

    assert outcome.disposition == "booked"
    assert calendar.booking_attempts == 2
    assert calendar.booking_writes == 1


class TransientCrm:
    def __init__(self) -> None:
        self.delegate = InMemoryCrm()
        self.attempts = 0

    def upsert(self, contact: Contact, *, operation_id: str) -> Contact:
        self.attempts += 1
        if self.attempts == 1:
            raise CrmError("crm_provider_unavailable")
        return self.delegate.upsert(contact, operation_id=operation_id)


def test_completed_conversation_retries_pending_crm() -> None:
    fixture = booking_fixture("crm-retry")
    store = InMemoryCallSessionStore()
    crm = TransientCrm()
    calendar = InMemoryCalendly()

    first = agent(calendar=calendar, crm=crm, store=store).run(fixture)
    second = agent(calendar=calendar, crm=crm, store=store).run(fixture)
    third = agent(calendar=calendar, crm=crm, store=store).run(fixture)

    assert first.disposition == second.disposition == third.disposition == "booked"
    assert crm.attempts == 2
    assert crm.delegate.write_count == 1
    assert calendar.booking_writes == 1


def test_crm_success_then_checkpoint_failure_does_not_duplicate_write() -> None:
    fixture = booking_fixture("crm-save-crash")
    store = OneShotFailingStore(lambda checkpoint: checkpoint.crm_synced)
    crm = InMemoryCrm()

    with pytest.raises(SafeBoundaryError, match="session_store_unavailable"):
        agent(crm=crm, store=store).run(fixture)
    outcome = agent(crm=crm, store=store, key=KEY_B).run(fixture)

    assert outcome.disposition == "booked"
    assert crm.upsert_attempts == 2
    assert crm.write_count == 1


class RecordingTts:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return b"RIFF-local-test"


def test_booking_confirmation_uses_actual_wednesday_slot() -> None:
    fixture = replace(
        booking_fixture("wednesday-booking"),
        turns=(*booking_fixture().turns[:-1], Turn("slot.wav", "Wednesday at two")),
    )
    tts = RecordingTts()

    outcome = agent(tts=tts).run(fixture)

    assert outcome.booking is not None
    assert outcome.booking.slot == "wednesday-2pm"
    assert "Your appointment is confirmed for Wednesday at 2 PM." in tts.texts
    assert "Your appointment is confirmed for Tuesday at 10 AM." not in tts.texts


def test_crm_alias_collision_merges_records_aliases_and_notes() -> None:
    crm = InMemoryCrm()
    first = crm.upsert(
        Contact("pending", "caller-a", notes=["from-a"]), operation_id="first"
    )
    second = crm.upsert(
        Contact("pending", "caller-b", email="same@example.test", notes=["from-b"]),
        operation_id="second",
    )
    merged = crm.upsert(
        Contact("pending", "caller-a", email="same@example.test", notes=["incoming"]),
        operation_id="merge",
    )
    via_old_alias = crm.upsert(
        Contact("pending", "caller-b", notes=["after-merge"]), operation_id="after"
    )

    assert first.contact_id != second.contact_id
    assert merged.contact_id == via_old_alias.contact_id
    assert len(crm.contacts) == 1
    assert set(crm.contacts[0].notes) == {"from-a", "from-b", "incoming", "after-merge"}


def test_contact_ids_are_never_reused_and_operation_replay_resolves_canonical_identity() -> None:
    crm = InMemoryCrm()
    first = crm.upsert(Contact("pending", "caller-a"), operation_id="operation-a")
    second = crm.upsert(
        Contact("pending", "caller-b", email="same@example.test"),
        operation_id="operation-b",
    )
    merged = crm.upsert(
        Contact("pending", "caller-a", email="same@example.test"),
        operation_id="operation-merge",
    )
    unrelated = crm.upsert(Contact("pending", "caller-c"), operation_id="operation-c")

    replay_first = crm.upsert(Contact("pending", "ignored-a"), operation_id="operation-a")
    replay_second = crm.upsert(Contact("pending", "ignored-b"), operation_id="operation-b")
    replay_merge = crm.upsert(Contact("pending", "ignored-m"), operation_id="operation-merge")

    assert first.contact_id != second.contact_id
    assert unrelated.contact_id not in {first.contact_id, second.contact_id}
    assert replay_first.contact_id == replay_second.contact_id == replay_merge.contact_id
    assert replay_first.contact_id == merged.contact_id
    assert replay_first.caller_id != unrelated.caller_id


def test_hmac_refs_are_keyed_environment_specific_and_fail_closed() -> None:
    fixture = booking_fixture("hmac-call")
    first = agent(key=KEY_A).run(fixture)
    second = agent(key=KEY_B).run(fixture)
    first_caller_ref = first.trace[0].detail["caller_ref"]
    second_caller_ref = second.trace[0].detail["caller_ref"]

    assert first_caller_ref != second_caller_ref
    assert str(first_caller_ref).startswith("hmac-sha256:")
    with pytest.raises(ValueError, match="trace_hmac_key_required"):
        agent(key=b"short")
    with pytest.raises(ValueError, match="trace_hmac_key_required"):
        agent(key=b"replace-with-an-environment-specific-secret")
    with pytest.raises(ValueError, match="trace_hmac_key_required"):
        agent(key=b"a" * 64)


class ConfirmationFailsOnceTts(RecordingTts):
    def __init__(self) -> None:
        super().__init__()
        self.confirmation_attempts = 0

    def synthesize(self, text: str) -> bytes:
        if text.startswith("Your appointment is confirmed"):
            self.confirmation_attempts += 1
            if self.confirmation_attempts == 1:
                raise RuntimeError("private confirmation provider failure")
        return super().synthesize(text)


def test_confirmation_delivery_retries_without_recreating_booking() -> None:
    fixture = booking_fixture("confirmation-retry")
    store = InMemoryCallSessionStore()
    calendar = InMemoryCalendly()
    tts = ConfirmationFailsOnceTts()

    first = agent(store=store, calendar=calendar, tts=tts).run(fixture)
    second = agent(store=store, calendar=calendar, tts=tts).run(fixture)

    assert first.disposition == second.disposition == "booked"
    assert calendar.booking_writes == 1
    assert tts.confirmation_attempts == 2
    assert any(event.event == "tts_failed" for event in first.trace)
    assert any(
        event.event == "tts_synthesized"
        and event.detail["prompt"] == "booking_confirmation"
        for event in second.trace
    )


def test_confirmation_delivery_escalates_after_bounded_failures() -> None:
    class ConfirmationAlwaysFailsTts(RecordingTts):
        def __init__(self) -> None:
            super().__init__()
            self.confirmation_attempts = 0

        def synthesize(self, text: str) -> bytes:
            if text.startswith("Your appointment is confirmed"):
                self.confirmation_attempts += 1
                raise RuntimeError("private confirmation failure")
            return super().synthesize(text)

    fixture = booking_fixture("confirmation-escalation")
    store = InMemoryCallSessionStore()
    calendar = InMemoryCalendly()
    tts = ConfirmationAlwaysFailsTts()

    agent(store=store, calendar=calendar, tts=tts).run(fixture)
    second = agent(store=store, calendar=calendar, tts=tts).run(fixture)
    third = agent(store=store, calendar=calendar, tts=tts).run(fixture)
    checkpoint = store.load(fixture.call_id)

    assert checkpoint is not None
    assert checkpoint.booking_confirmation_status == "human_followup_required"
    assert tts.confirmation_attempts == 2
    assert calendar.booking_writes == 1
    assert second == third
    assert any(event.event == "confirmation_delivery_escalated" for event in third.trace)


@pytest.mark.parametrize(
    ("slots", "expected_disposition", "expected_prompt"),
    [
        ((), "callback_requested", "no_slots_callback"),
        (("wednesday-2pm",), "incomplete", "offer_single_slot"),
        (("tuesday-10am", "wednesday-2pm", "extra-slot"), "incomplete", "offer_slots"),
    ],
)
def test_available_slot_cardinality_is_handled_safely(
    slots: tuple[str, ...], expected_disposition: str, expected_prompt: str
) -> None:
    class SlotCalendar(InMemoryCalendly):
        def available_slots(self) -> tuple[str, ...]:
            return slots

    fixture = replace(booking_fixture("slot-cardinality"), turns=booking_fixture().turns[:3])

    outcome = agent(calendar=SlotCalendar()).run(fixture)

    assert outcome.disposition == expected_disposition
    assert any(
        event.event == "tts_synthesized" and event.detail["prompt"] == expected_prompt
        for event in outcome.trace
    )


def test_store_cannot_spoof_internal_interruption_with_private_message() -> None:
    canary = "store-secret-canary@example.test"

    class SpoofingStore(InMemoryCallSessionStore):
        @contextmanager
        def claim(self, _call_id: str, _fixture_digest: str) -> Iterator[None]:
            raise SimulatedInterruption(canary)
            yield

    with pytest.raises(SafeBoundaryError) as caught:
        agent(store=SpoofingStore()).run(booking_fixture("spoofed-interruption"))

    assert str(caught.value) == "internal_boundary_failure"
    assert canary not in str(caught.value)


@pytest.mark.parametrize("boundary", ["claim", "load", "save"])
@pytest.mark.parametrize(
    "error_type", [RuntimeError, SimulatedInterruption, FixtureBindingError]
)
def test_store_boundary_exception_types_never_expose_dynamic_message(
    boundary: str, error_type: type[Exception]
) -> None:
    canary = f"store-{boundary}-{uuid4()}@example.test"

    class BoundaryStore(InMemoryCallSessionStore):
        @contextmanager
        def claim(self, call_id: str, fixture_digest: str) -> Iterator[None]:
            if boundary == "claim":
                raise error_type(canary)
            with super().claim(call_id, fixture_digest):
                yield

        def load(self, call_id: str) -> CallCheckpoint | None:
            if boundary == "load":
                raise error_type(canary)
            return super().load(call_id)

        def save(self, checkpoint: CallCheckpoint) -> None:
            if boundary == "save":
                raise error_type(canary)
            super().save(checkpoint)

    with pytest.raises((SafeBoundaryError, FixtureBindingError)) as caught:
        agent(store=BoundaryStore()).run(booking_fixture(f"store-{boundary}"))

    assert canary not in str(caught.value)


def test_dynamic_fixture_and_derived_values_are_absent_from_safe_surfaces() -> None:
    token = uuid4().hex
    name = f"Canary {token[:8].translate(str.maketrans('0123456789', 'abcdefghij'))}"
    email = f"fixture-{token}@example.test"
    caller = f"+1555{int(token[:8], 16) % 10_000_000:07d}"
    audio_ref = f"audio-{token}.wav"
    fixture = CallFixture(
        call_id=f"call-{token}",
        caller_id=caller,
        turns=(
            Turn(audio_ref, f"My name is {name}"),
            Turn(f"interest-{token}.wav", "assisted living"),
            Turn(f"email-{token}.wav", email),
            Turn(f"slot-{token}.wav", "Tuesday at ten"),
        ),
    )

    outcome = agent().run(fixture)
    surfaces = repr((outcome.trace, outcome.to_safe_dict()))
    assert outcome.booking is not None
    derived = (
        fixture.call_id,
        caller,
        audio_ref,
        name.title(),
        email,
        outcome.booking.slot,
        outcome.booking.event_uri,
        outcome.booking.invitee_email,
    )
    assert not any(value in surfaces for value in derived)


@pytest.mark.parametrize("boundary", ["stt", "tts", "availability", "booking", "crm", "store"])
def test_dynamic_canary_provider_errors_never_escape_or_enter_trace(boundary: str) -> None:
    canary = f"secret-{uuid4()}@example.test token=sk-{uuid4().hex}"

    class LeakyTts:
        def synthesize(self, _text: str) -> bytes:
            raise RuntimeError(canary)

    class LeakyStt:
        def transcribe(self, _turn: Turn) -> tuple[str, float]:
            raise RuntimeError(canary)

    class LeakyCalendar(InMemoryCalendly):
        def available_slots(self) -> tuple[str, ...]:
            if boundary == "availability":
                raise RuntimeError(canary)
            return super().available_slots()

        def book(self, *, slot: str, email: str, operation_id: str):  # type: ignore[no-untyped-def]
            if boundary == "booking":
                raise RuntimeError(canary)
            return super().book(slot=slot, email=email, operation_id=operation_id)

    class LeakyCrm(InMemoryCrm):
        def upsert(self, contact: Contact, *, operation_id: str) -> Contact:
            if boundary == "crm":
                raise RuntimeError(canary)
            return super().upsert(contact, operation_id=operation_id)

    class LeakyStore(InMemoryCallSessionStore):
        @contextmanager
        def claim(self, _call_id: str, _fixture_digest: str) -> Iterator[None]:
            raise RuntimeError(canary)
            yield

    if boundary == "store":
        with pytest.raises(SafeBoundaryError) as caught:
            agent(store=LeakyStore()).run(booking_fixture("canary-store"))
        assert canary not in str(caught.value)
        return

    outcome = agent(
        calendar=LeakyCalendar(),
        crm=LeakyCrm(),
        tts=LeakyTts() if boundary == "tts" else LocalToneTextToSpeech(),
        stt=LeakyStt() if boundary == "stt" else FixtureSpeechToText(),
    ).run(booking_fixture(f"canary-{boundary}"))

    assert canary not in repr(outcome.trace)
    assert canary not in repr(outcome.to_safe_dict())
