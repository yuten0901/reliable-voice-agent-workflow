from __future__ import annotations

import json
from pathlib import Path

from .models import CallFixture, Turn


def load_fixture(path: Path) -> CallFixture:
    payload = json.loads(path.read_text(encoding="utf-8"))
    turns = tuple(Turn(**turn) for turn in payload["turns"])
    return CallFixture(
        call_id=payload["call_id"],
        caller_id=payload["caller_id"],
        turns=turns,
        calendar_failure=payload.get("calendar_failure", False),
    )
