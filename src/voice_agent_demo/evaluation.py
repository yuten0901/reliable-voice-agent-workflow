from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .adapters import (
    FixtureSpeechToText,
    InMemoryCalendly,
    InMemoryCallSessionStore,
    InMemoryCrm,
    LocalToneTextToSpeech,
    SimulatedInterruption,
)
from .engine import VoiceAgent
from .fixtures import load_fixture
from .models import CallFixture, Turn

EVALUATION_TRACE_KEY = b"offline-fixture-evaluation-key-2026-only"
SECOND_EVALUATION_TRACE_KEY = b"second-fixture-environment-key-2026-only"


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    expected: str
    actual: str
    passed: bool
    trace_events: int
    wav_outputs: int
    crm_contacts: int


@dataclass(frozen=True)
class ControlResult:
    control: str
    passed: bool
    evidence: str


def evaluate(fixtures_dir: Path) -> dict[str, object]:
    results: list[ScenarioResult] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        fixture = load_fixture(path)
        crm = InMemoryCrm()
        agent = VoiceAgent(
            stt=FixtureSpeechToText(),
            tts=LocalToneTextToSpeech(),
            calendar=InMemoryCalendly(fail_booking=fixture.calendar_failure),
            crm=crm,
            trace_hmac_key=EVALUATION_TRACE_KEY,
        )
        outcome = agent.run(fixture)
        expected = path.stem.split("__", 1)[1]
        results.append(
            ScenarioResult(
                scenario=path.stem,
                expected=expected,
                actual=outcome.disposition,
                passed=outcome.disposition == expected,
                trace_events=len(outcome.trace),
                wav_outputs=len(outcome.audio_outputs),
                crm_contacts=len(crm.contacts),
            )
        )
    passed = sum(result.passed for result in results)
    happy_fixture = load_fixture(fixtures_dir / "01-happy-path__booked.json")
    controls = _evaluate_hardening_controls(happy_fixture, fixtures_dir)
    controls_passed = sum(control.passed for control in controls)
    return {
        "schema_version": 2,
        "execution_mode": "offline_fixture_only",
        "summary": {"scenarios": len(results), "passed": passed, "failed": len(results) - passed},
        "scenarios": [asdict(result) for result in results],
        "hardening_summary": {
            "controls": len(controls),
            "passed": controls_passed,
            "failed": len(controls) - controls_passed,
        },
        "hardening_controls": [asdict(control) for control in controls],
        "limitations": [
            "STTはfixture transcriptを返すtest doubleで、実音声認識品質を測定していない",
            "TTSはadapter境界を検証するtone WAVで、自然音声品質を測定していない",
            "電話回線・Calendly・CRMの外部API、認証、課金、実通話は使用していない",
        ],
    }


def _evaluate_hardening_controls(
    fixture: CallFixture, fixtures_dir: Path
) -> list[ControlResult]:
    retry_calendar = InMemoryCalendly(fail_booking_attempts=1)
    retry_outcome = VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=retry_calendar,
        crm=InMemoryCrm(),
        trace_hmac_key=EVALUATION_TRACE_KEY,
    ).run(fixture)

    store = InMemoryCallSessionStore()
    resume_calendar = InMemoryCalendly()
    resume_crm = InMemoryCrm()
    first = VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=resume_calendar,
        crm=resume_crm,
        trace_hmac_key=EVALUATION_TRACE_KEY,
        session_store=store,
    )
    interrupted = False
    try:
        first.run(fixture, interrupt_after_turns=2)
    except SimulatedInterruption:
        interrupted = True
    resumed = VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=resume_calendar,
        crm=resume_crm,
        trace_hmac_key=EVALUATION_TRACE_KEY,
        session_store=store,
    ).run(fixture)
    duplicate = first.run(fixture)
    privacy_surfaces: list[object] = []
    private_values: list[str] = ["local://calendly", "Your appointment is confirmed"]
    for fixture_path in sorted(fixtures_dir.glob("*.json")):
        privacy_fixture = load_fixture(fixture_path)
        privacy_outcome = VoiceAgent(
            stt=FixtureSpeechToText(),
            tts=LocalToneTextToSpeech(),
            calendar=InMemoryCalendly(fail_booking=privacy_fixture.calendar_failure),
            crm=InMemoryCrm(),
            trace_hmac_key=EVALUATION_TRACE_KEY,
        ).run(privacy_fixture)
        privacy_surfaces.extend([privacy_outcome.trace, privacy_outcome.to_safe_dict()])
        private_values.extend([privacy_fixture.call_id, privacy_fixture.caller_id])
        private_values.extend(turn.audio_ref for turn in privacy_fixture.turns)
        private_values.extend(turn.transcript for turn in privacy_fixture.turns if turn.transcript)
        private_values.extend(
            value
            for value in (
                privacy_outcome.contact.caller_id,
                privacy_outcome.contact.name,
                privacy_outcome.contact.email,
                privacy_outcome.contact.booking_uri,
            )
            if value is not None
        )
        if privacy_outcome.booking is not None:
            private_values.extend(
                [
                    privacy_outcome.booking.slot,
                    privacy_outcome.booking.event_uri,
                    privacy_outcome.booking.invitee_email,
                ]
            )
    dynamic_token = uuid4().hex
    dynamic_suffix = dynamic_token[:8].translate(str.maketrans("0123456789", "abcdefghij"))
    dynamic_name = f"Canary {dynamic_suffix}"
    dynamic_email = f"fixture-{dynamic_token}@example.test"
    dynamic_fixture = CallFixture(
        call_id=f"call-{dynamic_token}",
        caller_id=f"+1555{int(dynamic_token[:8], 16) % 10_000_000:07d}",
        turns=(
            Turn(f"name-{dynamic_token}.wav", f"My name is {dynamic_name}"),
            Turn(f"interest-{dynamic_token}.wav", "assisted living"),
            Turn(f"email-{dynamic_token}.wav", dynamic_email),
            Turn(f"slot-{dynamic_token}.wav", "Tuesday at ten"),
        ),
    )
    dynamic_outcome = VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=InMemoryCalendly(),
        crm=InMemoryCrm(),
        trace_hmac_key=EVALUATION_TRACE_KEY,
    ).run(dynamic_fixture)
    privacy_surfaces.extend([dynamic_outcome.trace, dynamic_outcome.to_safe_dict()])
    private_values.extend(
        [
            dynamic_fixture.call_id,
            dynamic_fixture.caller_id,
            dynamic_name.title(),
            dynamic_email,
            *(turn.audio_ref for turn in dynamic_fixture.turns),
            *(turn.transcript for turn in dynamic_fixture.turns),
        ]
    )
    if dynamic_outcome.booking is not None:
        private_values.extend(
            [
                dynamic_outcome.booking.slot,
                dynamic_outcome.booking.event_uri,
                dynamic_outcome.booking.invitee_email,
            ]
        )
    serialized_privacy_surfaces = repr(privacy_surfaces)
    concurrent_store = InMemoryCallSessionStore()
    concurrent_calendar = InMemoryCalendly()
    concurrent_crm = InMemoryCrm()

    def run_concurrent(_: int) -> object:
        return VoiceAgent(
            stt=FixtureSpeechToText(),
            tts=LocalToneTextToSpeech(),
            calendar=concurrent_calendar,
            crm=concurrent_crm,
            trace_hmac_key=EVALUATION_TRACE_KEY,
            session_store=concurrent_store,
        ).run(fixture)

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent_outcomes = list(pool.map(run_concurrent, range(2)))
    second_environment = VoiceAgent(
        stt=FixtureSpeechToText(),
        tts=LocalToneTextToSpeech(),
        calendar=InMemoryCalendly(),
        crm=InMemoryCrm(),
        trace_hmac_key=SECOND_EVALUATION_TRACE_KEY,
    ).run(fixture)
    return [
        ControlResult(
            control="calendar_retry_before_confirmation",
            passed=retry_outcome.disposition == "booked" and retry_calendar.booking_attempts == 2,
            evidence=(
                f"disposition={retry_outcome.disposition}; "
                f"attempts={retry_calendar.booking_attempts}"
            ),
        ),
        ControlResult(
            control="checkpoint_resume_and_duplicate_convergence",
            passed=(
                interrupted
                and resumed == duplicate
                and resume_calendar.booking_attempts == 1
                and resume_crm.upsert_attempts == 1
            ),
            evidence=(
                f"interrupted={interrupted}; booking_attempts={resume_calendar.booking_attempts}; "
                f"crm_attempts={resume_crm.upsert_attempts}"
            ),
        ),
        ControlResult(
            control="structured_trace_redacts_pii",
            passed=not any(value in serialized_privacy_surfaces for value in private_values),
            evidence=(
                "source and derived values from all fixtures plus a dynamic canary are absent"
            ),
        ),
        ControlResult(
            control="concurrent_duplicate_claim_convergence",
            passed=(
                concurrent_outcomes[0] == concurrent_outcomes[1]
                and concurrent_calendar.booking_writes == 1
                and concurrent_crm.write_count == 1
            ),
            evidence=(
                f"booking_writes={concurrent_calendar.booking_writes}; "
                f"crm_writes={concurrent_crm.write_count}"
            ),
        ),
        ControlResult(
            control="environment_key_separates_pseudonymous_refs",
            passed=(
                retry_outcome.trace[0].detail["caller_ref"]
                != second_environment.trace[0].detail["caller_ref"]
            ),
            evidence="same synthetic caller has different HMAC refs under two environment keys",
        ),
    ]
