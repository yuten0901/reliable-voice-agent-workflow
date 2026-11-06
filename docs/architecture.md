# Architecture and trust boundaries

## Goal

Prove the orchestration and failure semantics of one English appointment-setting call without
requiring a paid provider or making an external request.

## Ports and adapters

`VoiceAgent` depends only on five small protocols:

- `SpeechToText.transcribe` returns transcript plus confidence or raises a typed failure.
- `TextToSpeech.synthesize` returns playable WAV bytes.
- `Calendar.available_slots` and `Calendar.book(..., operation_id)` expose the booking boundary.
- `Crm.upsert(..., operation_id)` persists one contact using stable identity and write keys.
- `CallSessionStore.claim/load/save` serializes one call and stores its checkpoint.

The fixture adapters are deliberately named and documented as test doubles. Replacing them with
Twilio/Retell/Vapi, Deepgram/Whisper, a natural TTS provider, Calendly and HubSpot must not change
the orchestrator's safety behavior.

## Safety invariants

1. A booking is confirmed only after `Calendar.book` returns a booking URI.
2. Calendar failure creates a callback request and never emits `booking_created`.
3. Low-confidence speech cannot update contact fields.
4. Two consecutive STT failures end automated capture and request a callback.
5. CRM writes alias normalized email and caller ID to one contact and deduplicate notes.
6. A transient calendar failure is retried a bounded number of times before human fallback.
7. A successful STT turn resets the consecutive failure count.
8. A checkpoint binds one fixture digest to one call ID; conflicting reuse is rejected.
9. A completed duplicate call event returns the stored result without another booking or CRM write.
10. Structured trace details do not include raw call/audio references, caller ID, transcript,
    contact fields, spoken text, booking URI or unexpected provider-error text.
11. Conversation completion and CRM synchronization are separate; pending CRM is retried.
12. Booking/CRM retries reuse a stable operation ID across checkpoint-save failure.
13. Caller correlation uses environment-keyed HMAC pseudonyms and fails closed without a key.
14. Contact IDs are monotonic; retired IDs and old operations resolve to the canonical contact.
15. Booking creation and confirmation delivery have separate checkpoint states.
16. Zero slots routes to callback, one slot is offered alone, and multiple slots are bounded.

## State transition boundary

The orchestrator advances `in_progress → booked`, `in_progress → callback_requested`, or
`in_progress → incomplete`. It never emits the booking-confirmation prompt before `Calendar.book`
returns. Each consumed turn advances `next_turn_index` and saves a checkpoint. A second agent
instance can resume only when it receives the same store instance; the bundled store is not a
database, is not durable across process restart and cannot lock across processes.

After booking, confirmation delivery transitions `pending → delivered` or, after bounded failures,
`pending → human_followup_required`. Re-delivery reuses the saved booking and never calls
`Calendar.book` again.

## Deliberately absent

Real phone calls, outbound dialing, call recording, credentials, PII, legal consent decisions,
provider billing and external writes are outside this repository. Those are separate acceptance
gates, not hidden TODOs in the default demo path.
