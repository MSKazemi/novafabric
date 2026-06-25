# Event Envelope v1 — Reference Corpus

This directory contains 1000 reference `EventEnvelope` events used for CI validation.

## Contents

- `0001.json` through `1000.json` — one `EventEnvelope` per file, in JSON format.
- `generate_corpus.py` — deterministic generator script (seed 42).

## Purpose

The corpus is used by `make spec-test` to verify:

1. Every file parses as valid JSON.
2. Every event validates against `schemas/event-envelope-v1/envelope-v1.json`.
3. Required fields are present and correctly typed.
4. Optional fields (`nova.batch.signature`, `payload_hash`, `parent_run_id`, etc.) appear in valid combinations.

## Generation

The corpus is generated deterministically with `random.seed(42)`. Re-running the generator produces byte-identical output for the same script version.

```bash
cd schemas/event-envelope-v1/corpus
python3 generate_corpus.py
```

## Event type distribution

Events cycle through the six known event types:

| `event_type` | Count |
|---|---|
| `run.start` | ~167 |
| `run.end` | ~167 |
| `model_call` | ~167 |
| `tool_call` | ~167 |
| `span` | ~166 |
| `capsule.finalize` | ~166 |

## Field coverage

- `parent_run_id`: `null` for every 5th event (index mod 5 == 0); a valid ULID otherwise.
- `cluster_id`: `null` for events 1–200; non-null for events 201–1000.
- `tenant_id`: `null` for events 1–100; non-null for events 101–1000.
- `nova.batch.signature` and `nova.batch.signing_key_id`: present (non-null) for events 501–1000.
- `payload` and `payload_hash`: present for all `model_call` and `tool_call` events.
- `emitter_node_id`: present for events 401–1000.

## CI integration

The `make spec-test` target in `collector/Makefile` validates the corpus. It can also be run standalone:

```bash
python3 -c "
import json, pathlib
schema_path = pathlib.Path('../envelope-v1.json')
schema = json.load(open(schema_path))
files = sorted(pathlib.Path('.').glob('[0-9]*.json'))
errors = []
for f in files:
    data = json.load(open(f))
    for field in schema['required']:
        if field not in data:
            errors.append(f'{f}: missing required field {field!r}')
if errors:
    for e in errors:
        print('FAIL:', e)
else:
    print(f'{len(files)}/{len(files)} corpus events OK')
"
```
