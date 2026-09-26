# Vera Challenge Submission

This submission uses a **deterministic routing engine** instead of an external LLM. That keeps outputs fast, repeatable, and grounded in the four contexts.

## Approach

1. Route by `trigger.kind` to a category-aware message strategy.
2. Prefer the strongest current signal: explicit merchant intent, concrete performance movement, a dated external event, or a customer action that is actually consented to.
3. Pull numbers, dates, prices, sources, offers, and relationship facts directly from context.
4. Refuse to invent missing trigger details; sparse/placeholder events can be held until the judge provides enough information.
5. Customer-facing messages are consent- and state-gated.
6. `/v1/reply` includes auto-reply detection, explicit intent-transition handling, opt-out handling, and a polite off-topic redirect.

## Why no LLM?

The challenge allows any model, but the rubric emphasizes determinism and factual grounding. A rule-first engine avoids temperature/model drift and makes the behavior reproducible under the judge harness. The wording layer is deliberately small and template-driven.

## Run locally

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
```

Then:

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

Run the local checks:

```bash
python self_test.py
```

Generate the 30-line canonical submission file from the expanded dataset:

```bash
python make_submission.py
```

## Deploy

The included `Dockerfile` works with any container-friendly cloud host. Set these optional environment variables:

- `TEAM_NAME`
- `TEAM_MEMBERS`
- `CONTACT_EMAIL`
- `BOT_VERSION`

The judge should point its `BOT_URL` at the public HTTPS base URL.

## Tradeoffs

The engine is intentionally conservative when a trigger payload is a placeholder or conflicts with the merchant/customer context. This can reduce outreach volume, but it protects against fabricated facts and bad customer targeting. The main improvement path is adding more trigger-specific strategies as new context types are injected.

## Files

- `bot.py` — deterministic composer
- `server.py` — HTTP API and replay state
- `submission.jsonl` — 30 canonical outputs
- `self_test.py` — local contract/replay test
- `make_submission.py` — regenerate `submission.jsonl`
- `requirements.txt` / `Dockerfile` — deployment
