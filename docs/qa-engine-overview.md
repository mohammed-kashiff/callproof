# CallProof QA engine — presentation guide

This document is a walkthrough of how CallProof scores a support call: the pipeline, the **PyAI** and **Anthropic** APIs and models involved, how the **0–100 score** is built, and what **hybrid** mode changes.

**Audience:** anyone presenting or reviewing the product (engineering, QA, ops, leadership).  
**Live code path:** `rubric.json` (v8) → `qa_engine.py` → `qa_v8.py` → `rules_v8.py`.  
**Default mode:** `AUDIT_MODE=hybrid`.

---

## 1. In one minute

CallProof does **not** send the audio to Claude. It:

1. Transcribes with **PyAI Hear** (`pyai-hear-telephony`) into speaker-labelled, timestamped turns.
2. Labels who is the **AGENT** vs **CUSTOMER** with greeting/channel heuristics (no LLM).
3. Scores four dimensions (100 points total) with a mix of **deterministic phrase/timing rules** and **Claude Sonnet 5**.
4. Optionally asks **PyAI Recap** for a TL;DR / summary / action items (soft-fail; score still works without it).
5. Flags **manager review** on hostile language or a score below 60.

```text
Audio  →  PyAI Hear (telephony STT)  →  speaker turns in SQLite
                                              │
                     ┌────────────────────────┼────────────────────────┐
                     ▼                        ▼                        ▼
              Role classification      QA wave (rules + Claude)     PyAI Recap
              (no API)                 Resolution, Ownership,       (optional)
                                       Listening, Tone + churn
                                              │
                                              ▼
                                    Score 0–100 + band + flags
```

---

## 2. End-to-end pipeline

### 2.1 Ingest

The UI uploads audio to the CallProof API (`POST /api/upload` or `/api/upload-batch`). Limits: **25 MB** per file, **100** files per zip.

For Hear, CallProof makes an **8 kHz stereo PCM** copy so agent and customer stay on separate channels. Playback still uses the original file. Joint-stereo MP3 is avoided because it bleeds L/R and Hear then returns no speaker labels.

### 2.2 Transcribe (PyAI Hear)

CallProof submits the audio to PyAI as an **async job**, polls until complete, then stores segments (`speaker`, `channel`, `start`, `end`, `text`) in SQLite.

| Setting | Value |
|---|---|
| Model | `pyai-hear-telephony` |
| Separation | `channel: true` (true dual-channel). Alternative in code: `diarize: true` |
| Numerals | on |
| Output | JSON with timed segments |
| Poll budget | 200 attempts × 3 s ≈ **10 minutes** |

If the key lacks `transcribe:jobs` (typical **sandbox** key), CallProof falls back to sync `POST /v1/audio/transcriptions`. That path only continues if the response has usable speaker-labelled segments. Text-only transcripts cannot be audited.

### 2.3 Role classification (no model)

`classify_roles` decides who is the agent:

1. **Greeting / company cues** in the first 15 turns (`thank you for calling`, `how can I help`, vs customer cues like `I'm calling about`).
2. If those tie: **lower Hear channel index** = agent (typical left/agent dual-channel).
3. Last resort: first speaker.

The transcript is then formatted as labelled lines Claude can quote:

```text
[seq 0] (AGENT, 0.4s) Thank you for calling, how can I help?
[seq 1] (CUSTOMER, 3.1s) I need to cancel my order.
```

### 2.4 First-load QA wave

One parallel wave scores the four dimensions **and** churn. Recap runs **beside** that wave (not on the critical scoring path).

**Not** in the first-load wave (on-demand later):

- Areas of Improvement (`POST /api/calls/{id}/feedback`)
- Stakeholder retention email (`GET /api/calls/{id}/stakeholder-email/compose`)

### 2.5 Cache

`GET /api/calls/{id}/audit` caches the JSON scorecard keyed by rubric hash. Same call + same rubric → same score. `?refresh=true` recomputes.

---

## 3. Models in use

CallProof uses **two vendors**. PyAI is speech + recap. Anthropic is judgment.

### 3.1 PyAI

| Product | Model / pack | What it does | Required for scoring? |
|---|---|---|---|
| **Hear** | `pyai-hear-telephony` | Telephony STT with channel/diarize speaker labels and timestamps | **Yes** — the QA engine scores the transcript, not the audio |
| **Recap** | Recap add-on (optional `RECAP_PACK_ID`) | TL;DR, summary, action items from labelled utterances | **No** — UI enrichment only |

There is **no PyAI LLM** in the score. Hear does not grade the agent.

**Keys**

| Key | Prefix | Scopes that matter |
|---|---|---|
| Sandbox | `pyai_test_…` | `hear:transcribe` only. Auto-minted via `POST /v1/sandbox/keys` if `.env` has no key. Async jobs / Recap usually fail. |
| Live | `pyai_live_…` | `transcribe:jobs` (required for production-like QA). Recap needs `recap:read` (+ add-on). |

### 3.2 Anthropic (Claude)

| Use | Model | Effort | Tokens | Timeout |
|---|---|---|---|---|
| Scoring + churn + retention email | `claude-sonnet-5` | `high` | 2,000 | 60 s |
| Areas of Improvement | `claude-sonnet-5` | `high` | 3,000 | 90 s |

Temperature is effectively deterministic (`thinking` disabled; no sampling temperature). A given transcript + rubric is meant to produce the same verdicts.

`test_claude.py` uses `claude-haiku-4-5` as a **connectivity check only**. It is not on the scoring path.

---

## 4. External API calls

All outbound HTTP is recorded locally (`api_usage`) so the UI can show today’s hits. PyAI does not expose a “requests remaining” feed. CallProof **never returns the API key** from `/api/pyai/status`.

### 4.1 PyAI — `https://api.pyai.com`

| When | Method | Path | Why |
|---|---|---|---|
| First boot, no key | `POST` | `/v1/sandbox/keys` | Mint a free sandbox key (no card) |
| Status chip | `GET` | `/v1/me` | Key health, plan, limits (RPS, daily unit cap). Live prepaid balance is **not** shown |
| Transcribe (preferred) | `POST` | `/v1/transcription/jobs` | Async Hear job. Body/form: `model=pyai-hear-telephony`, `channel=true`, `numerals`, `output_formats=json`. Optional `call_id`, `pack_id` |
| Transcribe poll | `GET` | `/v1/transcription/jobs/{job_id}` | Until `completed` / `failed` (up to ~10 min) |
| Result fetch | `GET` | job `result_url` | Speaker-labelled JSON |
| Transcribe fallback | `POST` | `/v1/audio/transcriptions` | Sync path when async returns 403 (sandbox). `response_format=verbose_json` |
| Recap read | `GET` | `/v1/recap/calls/{id}` | Existing recap, or poll after trigger (~90 s) |
| Recap start | `POST` | `/v1/recap/calls/{id}` | Utterances with `speaker_role` agent/customer |

Auth: `Authorization: Bearer <PYAI_API_KEY>`. Job submit also sends an `Idempotency-Key` for URL jobs.

### 4.2 Anthropic — `https://api.anthropic.com`

| When | Method | Path | Why |
|---|---|---|---|
| Every Claude call | `POST` | `/v1/messages` | Scoring, churn, on-demand feedback, on-demand retention email |

Headers: `x-api-key`, `anthropic-version: 2023-06-01`. Body: `model: claude-sonnet-5`, `max_tokens`, `output_config.effort: high`, `thinking: { type: disabled }`.

**Retries:** 4 HTTP attempts with 2s / 4s / 6s backoff on 429 and 5xx. Invalid JSON: one aimed retry (v8) or two parse attempts (legacy v3). 400/401/403 are not retried.

### 4.3 How many Claude calls per call?

| Mode | First-load Claude calls | Notes |
|---|---|---|
| **hybrid** (default) | **2** | Resolution + churn, in parallel |
| **full** | **2–4** | Always Resolution + churn. Plus Tone LLM if no hostility. Plus Ownership step-2 if a vague phrase matched |
| On-demand | +1 each | Feedback; retention email (medium/high churn only) |

Rubric note: scoring LLM envelope is **1–3** calls depending on branches; churn is extra and always on first load.

### 4.4 CallProof HTTP API (what the UI calls)

These are **our** routes, not PyAI’s. Included so a demo can follow the UI.

| UI action | CallProof route |
|---|---|
| Upload | `POST /api/upload`, `POST /api/upload-batch` |
| Open scorecard | `GET /api/calls/{id}/audit` |
| Areas of Improvement | `POST /api/calls/{id}/feedback` |
| Email stakeholder | `GET /api/calls/{id}/stakeholder-email/compose` |
| Flag / solve review | `POST /api/calls/{id}/flag`, `/solve` |
| Key + usage chip | `GET /api/pyai/status` |

---

## 5. Scoring — the logic

### 5.1 Why four dimensions

The v8 rubric is a **soft-skills / technical-skills** build, not a 20-item checklist.

| Bucket | Weight | Dimensions |
|---|---|---|
| Technical | 60 | Resolution (40) + Ownership (20) |
| Soft skills | 40 | Listening (20) + Tone (20) |

Resolution is the largest slice because “did we actually help?” matters more than polish. Ownership is separate so a justified escalation can still pass Resolution, while a vague handoff is scored on whether the agent **named who owns next**.

### 5.2 Verdict math

Every dimension returns `pass`, `partial`, or `fail`:

```text
points = weight × { pass: 1.0,  partial: 0.5,  fail: 0.0 }
score  = round(sum of points, 1)          # already out of 100
if hostile language on Tone:
    score = min(score, 60)
```

There is **no renormalization** on v8 (all four dimensions always score). Legacy v3 excluded `not_applicable`, `error`, and weight-0 gates from both numerator and denominator.

Bands are **labels only**. The number is what is stored and trended.

| Score | Band | Manager cue |
|---|---|---|
| 95–100 | Star Performer | Call out publicly |
| 90–94 | Excelling | Reinforce what worked |
| 80–89 | Solid Performer | Normal cadence |
| 70–79 | Developing | Coach the lowest dimension |
| 60–69 | Needs Improvement | 1:1 this cycle |
| 0–59 | Needs Immediate Attention | Auto manager review |

### 5.3 Worked example

Agent: resolves the issue (Resolution pass), says “I’ll personally follow up tomorrow” (Ownership pass), does not interrupt (Listening pass), greets + empathizes, no hostility (Tone pass).

| Dimension | Verdict | Points |
|---|---|---|
| Resolution | pass | 40 |
| Ownership | pass | 20 |
| Listening | pass | 20 |
| Tone | pass | 20 |
| **Total** | | **100 — Star Performer** |

Same call, but the agent says “that’s not my problem” (hostile):

| Dimension | Verdict | Points |
|---|---|---|
| Resolution | pass | 40 |
| Ownership | pass | 20 |
| Listening | pass | 20 |
| Tone | **fail (gate)** | 0 |
| Weighted sum | | 80 |
| **After hostile cap** | | **60** + high-severity manager review |

A 60 here is **not** “the other skills were mediocre.” The cap exists so hostility cannot be averaged away by a good resolution.

### 5.4 Dimension logic

#### Resolution Effectiveness — 40 — always Claude

Prompt (agent only): did they resolve the issue, or make clear appropriate progress, including a **justified escalation**?

- **pass** — resolved or clearly progressed with a sound plan  
- **partial** — some effort, incomplete / unclear  
- **fail** — no meaningful resolution path  

Must cite an **exact** transcript quote. Returns a short `coaching_note`.

This is the one dimension that is **always** an LLM call, in both hybrid and full.

#### Ownership & Next Steps — 20 — rules first, Claude only if vague

**Step 1 (deterministic, negation-aware):** scan agent turns (including adjacent turns joined, because Hear splits sentences).

- Specific personal commitment (`I'll take care of this`, `I'll make sure`, `I'm on the case`, …) or a **named team** / ticket-style commit → **pass**. Stop. No Claude.
- Nothing of that kind → **fail**. Template coaching: always name who owns the follow-up. No Claude.
- Only a **vague** closer (`someone will get back to you`, `we'll look into it`, …) → Step 2.

**Step 2 (Claude, full mode only):** is that vagueness **transparently honest** about a constraint outside their control (still taking accountability) or **dismissive** to avoid commitment?

| Classification | Verdict | Points |
|---|---|---|
| `transparent_honest` | pass | 20 |
| `dismissive` | partial | 10 |

**Hybrid:** Step 2 is skipped. Vague language is scored **partial** (10 of 20) with an explicit reason that Claude was not asked.

Design intent: Resolution already covers *who can fix it*. Ownership only tests whether the **handoff was concrete**. Honest “this depends on billing, I’ll personally ping them” should not score the same as “someone will get back to you.”

#### Active Listening — 20 — rules only (never Claude)

Worse of two checks:

**Interruptions** (first **5** customer turns):

- Overlap ≥ **1.5 s** while the customer is still speaking counts.
- Ignored: short backchannels (≤ 3 words: `okay`, `mhm`, `got it`, …) and overlaps longer than the customer turn (likely a diarization glitch).
- 0 real interruptions → pass; **1** → partial; **2+** → fail.

**Dead air:**

- Gap ≥ **20 s** between turns.
- If the previous agent turn had a hold warning (`one moment`, `let me pull that up`, `please hold`, …):
  - and Resolution **passed** → dead air **pass** (time spent fixing).
  - else → **partial**.
- Unwarned 20 s+ silence → **fail**.

Overall listening verdict = the worse of the two. After the wave, if Resolution passed, listening is **re-run** so warned dead air can upgrade.

#### Tone, Empathy & Professionalism — 20 — gate + hybrid/full split

**Step 1 — hostile / profanity (always, both modes).** Word-boundary match, **not** negation-aware (`that's not my problem` still fails). On a hit:

- Tone = **fail** (0 of 20)
- Overall score **capped at 60**
- **Manager review** (`hostile_language_override`, high severity)
- No coaching note (this is a conduct flag, not a dashboard tip)
- Tone step-2 Claude is **skipped**

**Step 2 — if clean:**

Hybrid uses **six phrase banks** on agent turns:

| Check | Role |
|---|---|
| Profanity | Instant fail (gate) |
| Hostile phrases | Instant fail (gate) |
| Greeting | First **3** agent turns (`thank you for calling`, `how can I help`, …) |
| Empathy | `I'm sorry`, `I understand`, `that must be frustrating`, … |
| Professionalism | `thank you`, courtesy / close |
| Willingness to help | Offer to help, **or** customer says it helped, **or** Resolution passed |

Then:

- **2+** of the four positives, no hostility → **pass** (20)
- **1** positive → **partial** (10)
- **0** positives → **fail** (0)

Full mode: hostility still fails the same way; otherwise **Claude** judges warmth / empathy / professionalism across the whole call (pass / partial / fail + coaching note). Phrase-bank checks are still stored as subchecks.

### 5.5 Evidence and coaching

- Claude must copy a **verbatim 5–15 word span** from one transcript line. Quotes are checked against normalized segment text.
- Coaching notes that quote text not found in the transcript are **withheld** (data-quality flag, not the agent review inbox).
- Routing: `pass` reinforcement → agent dashboard; `partial` / `fail` → manager queue first (a 1:1 should open a conversation, not dump a correction on the agent with no context).

### 5.6 Churn (not in the 100)

A parallel Claude call rates `none | low | medium | high` from the **customer’s** words, with a verified quote. It does not change the agent score. Medium/high unlocks the stakeholder email action.

### 5.7 Manager review triggers

Either can fire alone:

| Trigger | Condition | Severity |
|---|---|---|
| `hostile_language_override` | Tone step 1 hit | high |
| `low_overall_score` | Final score **&lt; 60** | medium |

UI `gateFailed` is true when manager review is present or the audit is flagged (including a manual flag).

---

## 6. Hybrid mode (default)

Set with `AUDIT_MODE=hybrid` (or `full`) in `.env`. Unknown values fall back to hybrid.

### 6.1 Why it exists

Full mode can use up to three scoring LLM calls plus churn. Hybrid keeps the **expensive, high-judgment** call (Resolution) and churn, and replaces Tone-warmth and Ownership-honesty with **calibrated rules**. First-load latency and Claude spend drop; hostility and listening stay deterministic in both modes.

### 6.2 Side-by-side

| Piece | Hybrid | Full |
|---|---|---|
| Role classification | Greeting → channel → first speaker | Same |
| Resolution (40) | Claude | Claude |
| Churn | Claude (parallel) | Claude (parallel) |
| Listening (20) | Rules | Rules |
| Hostile / profanity | Fail + cap 60 + review | Same |
| Tone if clean (20) | Phrase banks: 2+ positives = pass, 1 = partial, 0 = fail | Claude judges warmth |
| Ownership specific / none | Pass / fail, no Claude | Same |
| Ownership vague closer | **Partial (10/20), no Claude** | Claude: honest = pass, dismissive = partial |
| Feedback / retention email | On-demand | On-demand |
| Typical first-load Claude | **2 calls** | **2–4 calls** |

### 6.3 What hybrid is *not*

- It is **not** “rules-only QA.” Resolution and churn still go to Sonnet 5.
- It is **not** a different rubric. Weights, bands, and the hostile cap are identical.
- It does **not** skip Recap; Recap is independent and optional in both modes.

### 6.4 When to use full

Use `AUDIT_MODE=full` when you want Claude’s judgment on **warmth** (sarcasm, cold professionalism that never swears) and on **honest vs dismissive** vague ownership. That is the right demo for “the model caught the tone dip,” at the cost of extra latency and tokens.

---

## 7. Demo talking points

1. **Audio never goes to Claude.** Hear produces the transcript; Claude only sees labelled text.
2. **PyAI model on the critical path is one:** `pyai-hear-telephony`. Recap is a separate add-on for the scorecard narrative.
3. **100 points, four boxes.** 40 / 20 / 20 / 20. Hostile language caps the *call* at 60 so a good resolution cannot hide conduct.
4. **Hybrid is the production default:** Claude for “did we fix it?” and churn; rules for listening, greetings/empathy, and concrete ownership.
5. **Same transcript + same rubric = same score** (cached; temperature/thinking off).
6. **Spend in the UI is an estimate** (`COST_*` env rates), not a PyAI or Anthropic invoice.

---

## 8. Source map

| File | Role |
|---|---|
| `transcribe.py` | Hear jobs, poll, `pyai-hear-telephony` |
| `recap.py` | Recap GET/POST, soft-fail |
| `qa_engine.py` | Orchestrator, Claude client, roles, evidence check, hybrid flag |
| `qa_v8.py` | Parallel v8 wave, dimension dispatch |
| `rules_v8.py` | Phrase banks, listening/tone/ownership, score cap, review |
| `rubric.json` / `rubric_v8.json` | Weights, bands, manager-review policy |
| `api.py` | Upload, audit cache, on-demand feedback/email, `/v1/me` status |
| `pyai_usage.py` | Local hit/unit counters |
| `cost_estimate.py` | Approximate $ (not invoices) |

---

## Security notes for presenters

- Do not paste `.env`, `PYAI_API_KEY`, or `ANTHROPIC_API_KEY` into slides or recordings.
- `/api/pyai/status` is designed to show sandbox vs live **without** returning the key.
- Live prepaid credit is intentionally not surfaced in the UI.
