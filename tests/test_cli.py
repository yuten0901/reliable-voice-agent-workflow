from __future__ import annotations

import json
from pathlib import Path

import pytest

import voice_agent_demo.cli as cli_module
from voice_agent_demo.cli import main

ROOT = Path(__file__).parents[1]


def test_cli_help_is_portable_ascii(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "Offline voice-agent booking reliability demo" in help_text
    assert help_text.isascii()


def test_evaluate_cli_writes_machine_readable_report(tmp_path: Path) -> None:
    output = tmp_path / "evaluation.json"

    result = main(["evaluate", str(ROOT / "fixtures" / "calls"), "--output", str(output)])

    assert result == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["failed"] == 0
    assert len(report["limitations"]) == 3


def test_run_cli_writes_redacted_call_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "call.json"
    monkeypatch.setenv("VOICE_AGENT_TRACE_HMAC_KEY", "cli-test-key-with-entropy-2026-only")

    result = main(
        [
            "run",
            str(ROOT / "fixtures" / "calls" / "01-happy-path__booked.json"),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    call = json.loads(output.read_text(encoding="utf-8"))
    assert call["disposition"] == "booked"
    assert call["booking"] == {"created": True}
    assert all(item["format"] == "wav" for item in call["audio_outputs"])
    serialized = json.dumps(call)
    for private_value in (
        "call-happy-001",
        "+15550001001",
        "Alice Morgan",
        "alice@example.test",
        "local://calendly",
    ):
        assert private_value not in serialized


def test_run_cli_fails_closed_without_trace_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "call.json"
    monkeypatch.delenv("VOICE_AGENT_TRACE_HMAC_KEY", raising=False)

    result = main(
        [
            "run",
            str(ROOT / "fixtures" / "calls" / "01-happy-path__booked.json"),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not output.exists()
    assert capsys.readouterr().err.strip() == "voice_agent_demo_error: trace_hmac_key_required"


def test_raw_cli_output_requires_explicit_flag_path_and_contains_warning_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "unsafe-call.json"
    monkeypatch.setenv("VOICE_AGENT_TRACE_HMAC_KEY", "cli-test-key-with-entropy-2026-only")

    result = main(
        [
            "run",
            str(ROOT / "fixtures" / "calls" / "01-happy-path__booked.json"),
            "--unsafe-raw-output",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert "alice@example.test" in output.read_text(encoding="utf-8")


def test_cli_top_level_error_never_prints_provider_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "secret-canary@example.test token=sk-dynamic-canary"
    monkeypatch.setenv("VOICE_AGENT_TRACE_HMAC_KEY", "cli-test-key-with-entropy-2026-only")

    def fail_load(_path: Path) -> object:
        raise RuntimeError(canary)

    monkeypatch.setattr(cli_module, "load_fixture", fail_load)

    result = main(["run", "ignored.json"])

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "voice_agent_demo_error: execution_failed"
    assert canary not in captured.err
