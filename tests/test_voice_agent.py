from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from voice_agent_demo.adapters import (
    FixtureBindingError,
    FixtureSpeechToText,
    InMemoryCalendly,
    InMemoryCallSessionStore,
    InMemoryCrm,
    LocalToneTextToSpeech,
    SimulatedInterruption,
    SpeechRecognitionError,
)
from voice_agent_demo.engine import VoiceAgent
from voice_agent_demo.evaluation import evaluate
from voice_agent_demo.fixtures import load_fixture
from voice_agent_demo.models import CallFixture, Contact, Turn

FIXTURES = Path(__file__).parents[1] / "fixtures" / "calls"
TEST_TRACE_KEY = b"unit-test-trace-key-not-secret-2026-only"


def build_agent(
    *,
    calendar_failure: bool = False,
    crm: InMemoryCrm | None = None,
    calendar: InMemoryCalendly | None = None,
    session_store: InMemoryCallSessionStore | None = None,
) -> VoiceAgent:
    return VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=calendar or InMemoryCalendly(fail_booking=calendar_failure),
        crm=crm or InMemoryCrm(),
        trace_hmac_key=TEST_TRACE_KEY,
        session_store=session_store,
    )


def test_happy_path_books_and_preserves_auditable_trace() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")

    outcome = build_agent().run(fixture)

    assert outcome.disposition == "booked"
    assert outcome.contact.name == "Alice Morgan"
    assert outcome.contact.email == "alice@example.test"
    assert outcome.contact.interest == "assisted_living"
    assert outcome.booking is not None
    assert outcome.booking.slot == "tuesday-10am"
    assert outcome.booking.event_uri.startswith("local://calendly/scheduled_events/")
    assert {event.event for event in outcome.trace} >= {
        "stt_transcribed",
        "tts_synthesized",
        "booking_created",
        "crm_upserted",
    }


def test_calendar_failure_routes_to_callback_without_false_confirmation() -> None:
    fixture = load_fixture(FIXTURES / "02-calendar-down__callback_requested.json")

    outcome = build_agent(calendar_failure=True).run(fixture)

    assert outcome.disposition == "callback_requested"
    assert outcome.booking is None
    assert outcome.contact.booking_uri is None
    assert "calendar_provider_unavailable" in outcome.contact.notes
    assert not any(event.event == "booking_created" for event in outcome.trace)


def test_repeated_stt_failure_routes_to_callback() -> None:
    fixture = load_fixture(FIXTURES / "03-stt-down__callback_requested.json")

    outcome = build_agent().run(fixture)

    assert outcome.disposition == "callback_requested"
    failures = [event for event in outcome.trace if event.event == "stt_failed"]
    assert len(failures) == 2
    assert outcome.contact.status == "callback_requested"


def test_low_confidence_transcript_does_not_update_contact() -> None:
    fixture = CallFixture(
        call_id="low-confidence",
        caller_id="+15550002000",
        turns=(Turn("unclear.wav", "My name is Wrong Name", confidence=0.2),),
    )

    outcome = build_agent().run(fixture)

    assert outcome.contact.name is None
    assert outcome.disposition == "incomplete"
    assert any(event.event == "stt_rejected" for event in outcome.trace)


def test_two_low_confidence_turns_request_human_callback() -> None:
    fixture = CallFixture(
        call_id="low-confidence-twice",
        caller_id="+15550002001",
        turns=(
            Turn("unclear-1.wav", "My name is Wrong Name", confidence=0.2),
            Turn("unclear-2.wav", "alice@example.test", confidence=0.3),
        ),
    )

    outcome = build_agent().run(fixture)

    assert outcome.disposition == "callback_requested"
    assert outcome.contact.name is None
    assert outcome.contact.email is None
    assert len([event for event in outcome.trace if event.event == "stt_rejected"]) == 2


def test_success_resets_consecutive_stt_failure_counter() -> None:
    fixture = CallFixture(
        call_id="stt-reset",
        caller_id="+15550002002",
        turns=(
            Turn("timeout.wav", "", failure="timeout"),
            Turn("clear.wav", "My name is Alice Morgan"),
            Turn("unclear.wav", "assisted living", confidence=0.2),
        ),
    )

    outcome = build_agent().run(fixture)

    assert outcome.disposition == "incomplete"
    assert outcome.contact.name == "Alice Morgan"
    assert not any(event.event == "fallback_requested" for event in outcome.trace)


def test_crm_upsert_reuses_contact_id_for_same_email() -> None:
    crm = InMemoryCrm()
    first = crm.upsert(
        Contact(
            contact_id="pending",
            caller_id="+15550003001",
            email="same@example.test",
            status="incomplete",
        ),
        operation_id="crm-first",
    )
    second = crm.upsert(
        Contact(
            contact_id="pending",
            caller_id="+15550003002",
            email="SAME@example.test",
            status="callback_requested",
        ),
        operation_id="crm-second",
    )

    assert first.contact_id == second.contact_id
    assert len(crm.contacts) == 1
    assert crm.contacts[0].status == "callback_requested"


def test_crm_aliases_caller_and_email_and_deduplicates_notes() -> None:
    crm = InMemoryCrm()
    first = crm.upsert(
        Contact(
            contact_id="pending",
            caller_id="+15550003003",
            notes=["manual_review"],
            status="incomplete",
        ),
        operation_id="crm-alias-first",
    )
    second = crm.upsert(
        Contact(
            contact_id="pending",
            caller_id="+15550003003",
            email="alias@example.test",
            notes=["manual_review"],
            status="callback_requested",
        ),
        operation_id="crm-alias-second",
    )
    third = crm.upsert(
        Contact(
            contact_id="pending",
            caller_id="+15550009999",
            email="ALIAS@example.test",
            notes=["manual_review"],
            status="callback_requested",
        ),
        operation_id="crm-alias-third",
    )

    assert first.contact_id == second.contact_id == third.contact_id
    assert len(crm.contacts) == 1
    assert crm.contacts[0].notes == ["manual_review"]


def test_calendar_booking_is_idempotent() -> None:
    calendar = InMemoryCalendly()

    first = calendar.book(
        slot="tuesday-10am", email="alice@example.test", operation_id="booking-001"
    )
    second = calendar.book(
        slot="tuesday-10am", email="ALICE@example.test", operation_id="booking-001"
    )

    assert first == second


def test_calendar_transient_failure_is_retried_before_confirmation() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")
    calendar = InMemoryCalendly(fail_booking_attempts=1)

    outcome = build_agent(calendar=calendar).run(fixture)

    assert outcome.disposition == "booked"
    assert calendar.booking_attempts == 2
    failure = next(event for event in outcome.trace if event.event == "calendar_booking_failed")
    assert failure.detail["retrying"] is True
    assert any(
        event.event == "tts_synthesized"
        and event.detail["prompt"] == "booking_confirmation"
        for event in outcome.trace
    )


def test_persistent_calendar_failure_exhausts_retry_without_confirmation() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")
    calendar = InMemoryCalendly(fail_booking=True)

    outcome = build_agent(calendar=calendar).run(fixture)

    assert outcome.disposition == "callback_requested"
    assert calendar.booking_attempts == 2
    assert not any(event.event == "booking_created" for event in outcome.trace)
    assert not any(
        event.event == "tts_synthesized"
        and event.detail["prompt"] == "booking_confirmation"
        for event in outcome.trace
    )


def test_checkpoint_resume_and_duplicate_delivery_converge_without_side_effects() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")
    store = InMemoryCallSessionStore()
    calendar = InMemoryCalendly()
    crm = InMemoryCrm()
    first_agent = build_agent(calendar=calendar, crm=crm, session_store=store)

    with pytest.raises(SimulatedInterruption):
        first_agent.run(fixture, interrupt_after_turns=2)

    resumed = build_agent(calendar=calendar, crm=crm, session_store=store).run(fixture)
    duplicate = build_agent(calendar=calendar, crm=crm, session_store=store).run(fixture)

    assert resumed == duplicate
    assert resumed.disposition == "booked"
    assert calendar.booking_attempts == 1
    assert crm.upsert_attempts == 1
    assert len([event for event in resumed.trace if event.event == "call_started"]) == 1


def test_same_call_id_cannot_be_reused_with_different_input() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")
    store = InMemoryCallSessionStore()
    agent = build_agent(session_store=store)
    agent.run(fixture)
    conflicting = CallFixture(
        call_id=fixture.call_id,
        caller_id=fixture.caller_id,
        turns=(Turn("other.wav", "My name is Mallory"),),
    )

    with pytest.raises(FixtureBindingError, match="different_fixture"):
        agent.run(conflicting)


def test_trace_redacts_transcript_contact_and_booking_pii() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")

    outcome = build_agent().run(fixture)
    serialized_trace = repr(outcome.trace)

    assert fixture.call_id not in serialized_trace
    assert fixture.caller_id not in serialized_trace
    assert fixture.turns[0].audio_ref not in serialized_trace
    assert "Alice Morgan" not in serialized_trace
    assert "alice@example.test" not in serialized_trace
    assert "local://calendly" not in serialized_trace
    assert "Your appointment is confirmed" not in serialized_trace


def test_unexpected_provider_error_text_is_redacted_from_trace() -> None:
    class LeakySpeechToText:
        def transcribe(self, _turn: Turn) -> tuple[str, float]:
            raise SpeechRecognitionError("alice@example.test")

    fixture = CallFixture(
        call_id="leaky-error",
        caller_id="+15550002003",
        turns=(Turn("alice-recording.wav", ""),),
    )
    agent = VoiceAgent(
        stt=LeakySpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=InMemoryCalendly(),
        crm=InMemoryCrm(),
        trace_hmac_key=TEST_TRACE_KEY,
    )

    outcome = agent.run(fixture)
    serialized_trace = repr(outcome.trace)

    assert "alice@example.test" not in serialized_trace
    assert "stt_provider_failure" in serialized_trace


def test_crm_failure_is_explicit_without_corrupting_booking_result() -> None:
    fixture = load_fixture(FIXTURES / "01-happy-path__booked.json")

    outcome = build_agent(crm=InMemoryCrm(fail_upsert=True)).run(fixture)

    assert outcome.disposition == "booked"
    assert outcome.booking is not None
    assert any(event.event == "crm_upsert_failed" for event in outcome.trace)


def test_tts_output_is_valid_mono_pcm_wav() -> None:
    audio = LocalToneTextToSpeech().synthesize("Your appointment is confirmed.")

    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8_000
        assert wav.getnframes() > 0


def test_evaluation_passes_all_fixture_scenarios() -> None:
    report = evaluate(FIXTURES)

    assert report["summary"] == {"scenarios": 3, "passed": 3, "failed": 0}
    assert report["execution_mode"] == "offline_fixture_only"


@pytest.mark.parametrize("slot", ["monday-9am", "tuesday-11am"])
def test_unavailable_slot_is_rejected(slot: str) -> None:
    calendar = InMemoryCalendly()

    with pytest.raises(RuntimeError, match="slot_unavailable"):
        calendar.book(slot=slot, email="alice@example.test", operation_id=f"test:{slot}")
