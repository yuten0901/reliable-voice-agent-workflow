# Evaluation method

The local evaluation is a deterministic regression suite, not a natural-voice benchmark.

| Scenario | Expected result | Primary invariant |
|---|---|---|
| happy path | `booked` | confirmed only after booking URI exists |
| calendar down | `callback_requested` | no false booking confirmation |
| STT down twice | `callback_requested` | retry is bounded |

The report also records five controls:

| Control | Evidence |
|---|---|
| transient calendar failure | second attempt books; confirmation follows success |
| interruption and duplicate delivery | shared-store resume books/upserts once |
| trace privacy | raw synthetic PII and spoken text are absent from trace details |
| concurrent duplicate | two workers converge to one booking and one CRM write |
| environment key separation | the same caller has different HMAC refs under different keys |

Run:

```bash
voice-agent-demo evaluate fixtures/calls --output reports/evaluation.json
pytest -q
```

The JSON report records scenario totals, actual and expected dispositions, trace counts, generated
WAV counts and CRM contact counts. Hardening controls record compact evidence strings. The report
also embeds the three evidence limitations so it cannot be separated from its qualification
without an obvious omission.

Unit tests add concurrency and checkpoint fault injection around booking/CRM writes, CRM pending
retry, alias-collision merge, actual-slot confirmation, HMAC fail-closed/key separation, redacted
CLI serialization and dynamic canaries at provider boundaries. These are deterministic local
control-flow checks, not provider reliability data.

Round 2 regressions also cover monotonic contact identity across merge/replay, confirmation-only
retry after TTS failure, 0/1/N slot cardinality, spoofed control exceptions at claim/load/save,
weak/known-key rejection, and dynamic fixture plus derived contact/booking value scanning.

## What a real-provider evaluation must add

- consented calls through the chosen carrier and phone number;
- STT accuracy over a versioned audio corpus including noise, accents, silence and interruptions;
- end-to-end and turn latency percentiles;
- natural TTS intelligibility and pronunciation review;
- actual Calendly event creation, duplicate retry and cancellation behavior;
- actual CRM create/update evidence with redacted test data;
- provider timeout, rate-limit and partial-outage drills;
- data retention, recording consent and deletion verification.
