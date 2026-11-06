# Deployment boundary

This repository is safe to run locally without credentials or network access. It is a work sample,
not a deployable telephony service.

## Present in this repository

- deterministic call fixtures and a provider-independent state machine;
- protocol boundaries for STT, TTS, Calendar, CRM and checkpoint storage;
- local PCM/WAV tone output;
- bounded retry, human fallback and operation-ID idempotent local writes;
- process-local atomic claim, interruption/resume and concurrent duplicate-delivery tests;
- redacted traces with environment-keyed HMAC pseudonyms (not anonymized data).
- separate booking-created and confirmation-delivered state with bounded human escalation.

## Required before any real deployment

1. Implement and contract-test a telephony webhook/media adapter for the selected provider.
2. Replace fixture STT and tone TTS with selected providers and measure latency and accuracy.
3. Replace the in-memory Calendar, CRM and checkpoint stores with authenticated, durable adapters.
4. Implement the demonstrated claim/operation-ID contracts with distributed locking, durable
   idempotency records and provider lookup/reconciliation.
5. Define recording consent, retention, deletion, incident response and regional compliance.
6. Add redacted production observability, rate limits, provider outage drills and operator handoff.
7. Run consented acceptance calls and document costs, failure rates and rollback procedures.

Generate the HMAC key with an OS-backed CSPRNG and store it in an environment-specific secret
manager. Public placeholders, repeated-character values and keys shorter than 32 bytes are rejected.

No Twilio, Vapi, Retell, carrier, hosted endpoint, real phone number or third-party account is used
or claimed here. The bundled checkpoint store cannot recover after process termination.
