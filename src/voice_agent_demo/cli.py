from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .adapters import FixtureSpeechToText, InMemoryCalendly, InMemoryCrm, LocalToneTextToSpeech
from .engine import VoiceAgent, validate_trace_hmac_key
from .evaluation import evaluate
from .fixtures import load_fixture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline voice-agent booking reliability demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run one deterministic call fixture")
    run.add_argument("fixture", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument(
        "--unsafe-raw-output",
        action="store_true",
        help="Write a raw PII-bearing outcome to an explicit local path",
    )
    evaluation = subparsers.add_parser("evaluate", help="Evaluate all deterministic fixtures")
    evaluation.add_argument("fixtures_dir", type=Path)
    evaluation.add_argument("--output", type=Path)
    return parser


def _write_or_print(payload: dict[str, object], output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output is None:
        print(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(output)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "evaluate":
        _write_or_print(evaluate(args.fixtures_dir), args.output)
        return 0

    if args.unsafe_raw_output and args.output is None:
        print("voice_agent_demo_error: unsafe_output_requires_path", file=sys.stderr)
        return 2
    trace_key = os.environ.get("VOICE_AGENT_TRACE_HMAC_KEY")
    try:
        if trace_key is None:
            raise ValueError("trace_hmac_key_required")
        validate_trace_hmac_key(trace_key.encode())
    except ValueError:
        print("voice_agent_demo_error: trace_hmac_key_required", file=sys.stderr)
        return 2
    try:
        fixture = load_fixture(args.fixture)
        agent = VoiceAgent(
            stt=FixtureSpeechToText(),
            tts=LocalToneTextToSpeech(),
            calendar=InMemoryCalendly(fail_booking=fixture.calendar_failure),
            crm=InMemoryCrm(),
            trace_hmac_key=trace_key.encode(),
        )
        outcome = agent.run(fixture)
        payload = outcome.to_dict() if args.unsafe_raw_output else outcome.to_safe_dict()
        _write_or_print(payload, args.output)
    except Exception:
        print("voice_agent_demo_error: execution_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
