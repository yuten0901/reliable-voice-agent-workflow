from __future__ import annotations

import io
import math
import struct
import threading
import wave
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import Protocol

from .models import Booking, CallCheckpoint, Contact, Turn


class SpeechRecognitionError(RuntimeError):
    """STT providerが音声を確定できない場合。"""


class CalendarError(RuntimeError):
    """予約providerが利用不能な場合。"""


class CrmError(RuntimeError):
    """CRM providerへ状態を反映できない場合。"""


class FixtureBindingError(RuntimeError):
    """同じcall IDへ異なる入力を混在させようとした場合。"""


class SimulatedInterruption(RuntimeError):
    """fixture評価でprocess中断を再現する場合。"""


class SpeechToText(Protocol):
    def transcribe(self, turn: Turn) -> tuple[str, float]: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class Calendar(Protocol):
    def available_slots(self) -> tuple[str, ...]: ...

    def book(self, *, slot: str, email: str, operation_id: str) -> Booking: ...


class Crm(Protocol):
    def upsert(self, contact: Contact, *, operation_id: str) -> Contact: ...


class CallSessionStore(Protocol):
    def claim(self, call_id: str, fixture_digest: str) -> AbstractContextManager[None]: ...

    def load(self, call_id: str) -> CallCheckpoint | None: ...

    def save(self, checkpoint: CallCheckpoint) -> None: ...


class FixtureSpeechToText:
    """通話fixtureの期待transcriptを返す、明示的なSTT test double。"""

    def transcribe(self, turn: Turn) -> tuple[str, float]:
        if turn.failure != "none":
            raise SpeechRecognitionError(turn.failure)
        return turn.transcript, turn.confidence


class LocalToneTextToSpeech:
    """外部TTSなしでWAV境界を検証する決定的なtone synthesizer。

    自然音声の証拠ではなく、provider adapterが再生可能なPCM/WAVを返すことだけを
    ローカルで検証する。公開時には実providerでの別検証が必要である。
    """

    sample_rate = 8_000

    def synthesize(self, text: str) -> bytes:
        duration = min(0.08 + len(text) * 0.002, 0.35)
        frame_count = int(self.sample_rate * duration)
        frequency = 440 + (int(sha256(text.encode()).hexdigest()[:2], 16) % 120)
        frames = bytearray()
        for index in range(frame_count):
            sample = int(4_000 * math.sin(2 * math.pi * frequency * index / self.sample_rate))
            frames.extend(struct.pack("<h", sample))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            output.writeframes(bytes(frames))
        return buffer.getvalue()


class InMemoryCalendly:
    """Calendly adapterと同じ境界を持つ、課金・認証不要の予約fixture。"""

    def __init__(self, *, fail_booking: bool = False, fail_booking_attempts: int = 0) -> None:
        self._slots = ("tuesday-10am", "wednesday-2pm")
        self._fail_booking = fail_booking
        self._remaining_failures = fail_booking_attempts
        self._bookings: dict[str, Booking] = {}
        self.booking_attempts = 0
        self.booking_writes = 0

    def available_slots(self) -> tuple[str, ...]:
        return self._slots

    def book(self, *, slot: str, email: str, operation_id: str) -> Booking:
        self.booking_attempts += 1
        if operation_id in self._bookings:
            return self._bookings[operation_id]
        if self._fail_booking or self._remaining_failures > 0:
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
            raise CalendarError("calendar_provider_unavailable")
        if slot not in self._slots:
            raise CalendarError("slot_unavailable")
        booking_id = sha256(operation_id.encode()).hexdigest()[:12]
        booking = Booking(
            event_uri=f"local://calendly/scheduled_events/{booking_id}",
            slot=slot,
            invitee_email=email.casefold(),
        )
        self._bookings[operation_id] = booking
        self.booking_writes += 1
        return booking


class InMemoryCrm:
    """emailを一意キーとして連絡先を冪等upsertするローカルCRM。"""

    def __init__(self, *, fail_upsert: bool = False) -> None:
        self._contacts: dict[str, Contact] = {}
        self._identity_to_id: dict[str, str] = {}
        self._retired_to_canonical: dict[str, str] = {}
        self._fail_upsert = fail_upsert
        self.upsert_attempts = 0
        self.write_count = 0
        self._operations: dict[str, str] = {}
        self._next_contact_number = 1
        self._lock = threading.RLock()

    @property
    def contacts(self) -> tuple[Contact, ...]:
        return tuple(self._contacts.values())

    def upsert(self, contact: Contact, *, operation_id: str) -> Contact:
        with self._lock:
            self.upsert_attempts += 1
            if operation_id in self._operations:
                contact_id = self._resolve_contact_id(self._operations[operation_id])
                return deepcopy(self._contacts[contact_id])
            if self._fail_upsert:
                raise CrmError("crm_provider_unavailable")
            identities = [f"caller:{contact.caller_id.casefold()}"]
            if contact.email is not None:
                identities.insert(0, f"email:{contact.email.casefold()}")
            matched_ids = {
                self._identity_to_id[key] for key in identities if key in self._identity_to_id
            }
            matched = [self._contacts[contact_id] for contact_id in sorted(matched_ids)]
            if matched:
                canonical_id = matched[0].contact_id
                notes = list(
                    dict.fromkeys(
                        note for item in [*matched, contact] for note in item.notes
                    )
                )
                persisted = replace(
                    contact,
                    contact_id=canonical_id,
                    name=contact.name or next((item.name for item in matched if item.name), None),
                    email=contact.email
                    or next((item.email for item in matched if item.email), None),
                    interest=contact.interest
                    or next((item.interest for item in matched if item.interest), None),
                    booking_uri=contact.booking_uri
                    or next((item.booking_uri for item in matched if item.booking_uri), None),
                    notes=notes,
                )
                retired_ids = matched_ids - {canonical_id}
                for retired_id in retired_ids:
                    del self._contacts[retired_id]
                    self._retired_to_canonical[retired_id] = canonical_id
                for identity, mapped_id in tuple(self._identity_to_id.items()):
                    if mapped_id in retired_ids:
                        self._identity_to_id[identity] = canonical_id
            else:
                contact_id = f"contact-{self._next_contact_number:06d}"
                self._next_contact_number += 1
                persisted = replace(contact, contact_id=contact_id)
            self._contacts[persisted.contact_id] = persisted
            for key in identities:
                self._identity_to_id[key] = persisted.contact_id
            self._operations[operation_id] = persisted.contact_id
            self.write_count += 1
            return persisted

    def _resolve_contact_id(self, contact_id: str) -> str:
        while contact_id in self._retired_to_canonical:
            contact_id = self._retired_to_canonical[contact_id]
        return contact_id


class InMemoryCallSessionStore:
    """再開と重複call収束を検証するprocess-local checkpoint store。"""

    def __init__(self) -> None:
        self._checkpoints: dict[str, CallCheckpoint] = {}
        self._bindings: dict[str, str] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._meta_lock = threading.Lock()

    @contextmanager
    def claim(self, call_id: str, fixture_digest: str) -> Iterator[None]:
        with self._meta_lock:
            lock = self._locks.setdefault(call_id, threading.RLock())
        with lock:
            bound = self._bindings.get(call_id)
            if bound is not None and bound != fixture_digest:
                raise FixtureBindingError("call_id_already_bound_to_different_fixture")
            self._bindings[call_id] = fixture_digest
            yield

    def load(self, call_id: str) -> CallCheckpoint | None:
        checkpoint = self._checkpoints.get(call_id)
        return deepcopy(checkpoint) if checkpoint is not None else None

    def save(self, checkpoint: CallCheckpoint) -> None:
        self._checkpoints[checkpoint.call_id] = deepcopy(checkpoint)
