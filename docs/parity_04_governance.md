# Step 4 — Governance layer (language gate + restricted-topic filter)

**Status:** ✅ done (feature-flag gated, off by default)
**Risk to working pipeline:** medium — mitigated by `GOVERNANCE_ENABLED` flag (default False = zero change to existing behavior)
**Time:** ~3 hours

## Why

Before Step 4, every "rule" the agent followed lived in the **system
prompt** — a suggestion to the LLM, not an enforced constraint. The LLM
could ignore it, hallucinate a policy, or be talked out of a refusal. We
saw this live: when a caller said "Hang up the call, please", the agent
replied "Call duration exceeded the 5-minute limit" — a policy that
exists nowhere in the code. The LLM invented it from a prompt mention.

Step 4 moves enforcement from prompt-level (advisory) to code-level
(enforced) for two classes of input, plus an output sanity check.

## What was added

| File | Role |
|---|---|
| `contracts/policy.py` | `PRDScripts` (canned refusal text), `detect_restricted_topic()` (input classifier), `ResponsePolicyEngine.violates()` (output hallucination check) |
| `contracts/language_interceptor.py` | `LanguageGovernanceInterceptor` (per-call 3-strike language gate) + `InterceptResult` dataclass |
| `config/__init__.py` | `GOVERNANCE_ENABLED` feature flag (env-var driven, default False) |
| `llm/groq_llm.py` | `run_llm()` gains optional `interceptor` param; runs both gates before each LLM call |
| `orchestrator/manager.py` | Creates the interceptor per call when the flag is on; passes it to `run_llm()` |

## The three checkpoints

### 1. Language gate (input) — 3-strike enforcement

Per-call `LanguageGovernanceInterceptor` instance carries strike state.
For each finalized user transcript:

- **Fast-path**: short English affirmations ("ok", "yes", "bye") allowed
  instantly — too short for any detector to classify reliably.
- **Name intro**: "Hi, my name is ..." bypasses detection — names from any
  culture are valid.
- **Lingua detection**: pure-Python detector. If confidence ≥ 0.75 and the
  detected language is not English → strike.
- **Fail-open**: if Lingua is unavailable or errors, the caller is allowed
  through (never block due to a detector problem).

Strike policy: strikes 1-2 speak a polite "please repeat in English" and
continue; strike 3 speaks a final message and the LLM loop returns, which
closes the call via the Step 3 hangup path.

### 2. Restricted-topic filter (input)

`detect_restricted_topic(user_text)` matches against four categories
before the LLM is called. On a match, the canned `PRDScripts` response is
spoken and the LLM is skipped entirely:

| Category | Example trigger | Response |
|---|---|---|
| `immigration` | "do I need a visa?" | IRCC redirect |
| `legal` | "I'll sue the college" | legal-department redirect |
| `competitor` | "is Humber better than you?" | "I can only provide info about GD College" |
| `financial_dispute` | "I want a refund" | "a team member will follow up" |

### 3. Response policy (output)

`ResponsePolicyEngine.violates(text)` scans LLM output for hallucinated
policies (e.g. the invented "5-minute limit"). Step 4 ships the detector;
wiring it to replace bad output with a fallback is a small follow-up (the
detector is in place and unit-verified).

## Feature flag

```
GOVERNANCE_ENABLED=false   # default — prompt-level guardrails only (existing behavior)
GOVERNANCE_ENABLED=true    # code-level language gate + restricted-topic filter active
```

Set in `.env` or the shell. When False, `VoiceOrchestrator` passes
`interceptor=None` to `run_llm`, and the LLM loop behaves exactly as
before Step 4 — no governance code runs. This is the safety valve: if a
gate ever over-blocks a real caller, flip the flag off without a code
change.

## Dependencies added

| Package | Version | Purpose |
|---|---|---|
| `lingua-language-detector` | 2.1.1 | Pure-Python language detection (~91 MB with models). Works on Windows, unlike `fasttext-wheel` which is Linux-only. |

## Verification

- **Compile + import**: all 5 modified/new files compile and import clean.
- **Restricted-topic detector**: 4/4 categories matched correctly;
  normal college questions ("course duration?", "where located?") pass
  through to the LLM.
- **Language interceptor**: Spanish struck at confidence 0.90; English
  variety (tuition, admission, campus questions) all proceed; fast-path
  and name-intro never strike.
- **Both flag states boot**: server starts and serves `/voice` 200 +
  `/healthz` 200 with the flag both off and on.

## Update — multilingual detection wired in + governance default-on

After the initial Step 4 landing, two changes were made per user request:

1. **Governance is now ON by default** (`GOVERNANCE_ENABLED` defaults to
   `true`). The flag remains for debugging, but no env var is needed to get
   enforcement. Set `GOVERNANCE_ENABLED=false` only to disable.

2. **Spoken foreign languages are now caught** via Deepgram `language=multi`
   plus a non-Latin script check. This closes the romanized-Hindi gap.

### How it works now

Empirically verified on the live nova-3 stream:
- `detect_language=true` → **rejected (HTTP 400)** on nova-3 live. Unusable.
- `language=multi` → **accepted**. nova-3 transcribes foreign speech (often
  in native script) and tags each word with a language code.

Pipeline:
- `stt/deepgram_stt.py` adds `&language=multi`, aggregates per-word language
  tags via `_dominant_language()`, and pushes `(text, detected_lang)` tuples
  on `transcript_queue` (was bare strings; `None` sentinel unchanged).
- `llm/groq_llm.py` unpacks the tuple and passes `detected_lang` to
  `interceptor.check(text, detected_lang)`.
- `contracts/language_interceptor.py` `check()` now runs, in order:
  empty → fast-path → name-intro → **non-Latin script ratio** → **Deepgram
  detected_lang** → Lingua → fail-open.

### Detection coverage (unit-tested)

| Input | Caught? | By |
|---|---|---|
| Hindi (Devanagari) | ✅ strike | non-Latin script |
| Bengali | ✅ strike | non-Latin script |
| Japanese / Chinese | ✅ strike | non-Latin script |
| Romanized Hindi tagged `hi` | ✅ strike | Deepgram acoustic tag |
| Spanish / French / German (Latin) | ✅ strike | Lingua |
| English (tag, no-tag, fast-path, name) | ✅ proceed | deepgram / lingua / fast-path / name-intro |
| 3-strike escalation | ✅ | strikes 1-2 warn, strike 3 terminates |

### Residual risk

`language=multi` may slightly change English transcription quality or
latency versus the previous English-only stream — this is the only Deepgram
path that supports multilingual on nova-3 live, so it's an accepted trade.
Watch the next live English call's `view_call.py` latency numbers; if they
regress noticeably, revisit. Bengali may not be in nova-3 multi's tag set,
but the non-Latin script check catches Bengali script regardless of tags.

## Adaptations vs. company verbatim

The company's `contracts/policy.py` is 819 lines and
`language_interceptor.py` is 478. The clean-build versions are slimmer:

- **Dropped detector tiers**: company uses Deepgram acoustic (primary) →
  FastText (0.80) → Lingua (0.75) → fail-open. Clean build uses Lingua →
  fail-open only. FastText is Linux-only; Deepgram acoustic needs STT
  plumbing not yet in place.
- **Slimmer keyword lists**: company has exhaustive competitor / sensitive
  / speculative keyword sets. Clean build covers the demo-relevant
  categories; expand if QA flags gaps.
- **Enum fix**: company referenced `Language.MANDARIN`; this Lingua
  version (2.1.1) names it `Language.CHINESE`. Fixed during verification.

## What this unlocks

- The output-side `ResponsePolicyEngine` is in place for a quick follow-up
  that swaps hallucinated LLM responses for safe fallbacks.
- The interceptor's structure is ready for Deepgram acoustic detection
  (Step "follow-up") to close the romanized-Hindi gap.
- `PRDScripts` is the single source of canned text for any future refusal
  or escalation flow (e.g. CRM escalation in a later step).
