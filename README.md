# CallProof (Call Loop)

CallProof scores support calls against a soft-skills rubric. Agents (or managers) upload recordings; the stack transcribes them with **PyAI Hear**, evaluates them with **Claude** (and hybrid rules), then shows a scorecard, churn signals, coaching-style feedback, and a review queue in the **Call Loop** UI.

Current product branch: **`v2testing-ui-final`**.

---

## What the product does

- **Ingest** call audio (single file or bulk zip, up to 100 files)
- **Transcribe** with PyAI Hear (speaker-labelled async jobs on a live key)
- **Score** against rubric v8 (Resolution, Ownership, Tone/Empathy/Professionalism, etc.)
- **Surface** scorecards, churn risk, areas of improvement, stakeholder email drafts
- **Review** flagged calls (pending / solved)
- **Estimate** approximate PyAI + Claude spend (tunable rates; not an invoice)

UI brand in this branch: **Call Loop v3** (React + TypeScript + Vite).  
**Training** in the sidebar is a placeholder (“Coming soon”) — not wired yet.

---

## How it functions

```text
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Call Loop  │────▶│  CallProof API   │────▶│  SQLite + audio │
│  UI :5173   │◀────│  FastAPI :8000   │◀────│  callproof.db   │
└─────────────┘     └────────┬─────────┘     └─────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌───────────┐  ┌────────────┐
        │ PyAI Hear│  │PyAI Recap │  │  Claude    │
        │transcribe│  │ (optional)│  │  (Anthropic)│
        └──────────┘  └───────────┘  └────────────┘
```

### Pipeline (one call)

1. **Upload** — UI sends audio to `POST /api/upload` (or batch zip).
2. **Hear copy** — browser/server prepare telephony-friendly audio when needed.
3. **Transcribe** — PyAI async job → speaker segments stored in SQLite.
4. **Audit** — `GET /api/calls/{id}/audit` runs hybrid/full QA (rules + Claude).
5. **Report** — score, grade, findings, churn; on-demand feedback / email / flag for review.

### Main pieces

| Layer | Role |
|-------|------|
| `frontend/` | Call Loop UI (pages, sidebar, upload queue, review) |
| `api.py` | FastAPI routes (upload, audit, flag/solve, export, status) |
| `transcribe.py` | PyAI Hear jobs |
| `qa_engine.py` + `rules_v8.py` + `rubric_v8.json` | Scoring |
| `pyai_usage.py` + `cost_estimate.py` | Local usage + $ estimates |
| `callproof.db` / `audio/` / `logs/` | Data, playback, app logs |

---

## Sandbox vs live PyAI key

| | Sandbox (`pyai_test_…`) | Live (`pyai_live_…`) |
|--|-------------------------|----------------------|
| How you get it | Auto-minted on first API start if `.env` has no key, **or** set manually | [console.pyai.com](https://console.pyai.com) |
| Typical scopes | `hear:transcribe` (sync text) | `transcribe:jobs` (+ Recap when enabled) |
| CallProof full QA | **Limited** — diarized async jobs / Recap often fail | **Required** for production-like scoring |

Sandbox is fine to **boot the stack and explore the UI**. For real transcription + scorecards, put a **live** `PYAI_API_KEY` in `.env`.

You always need an **`ANTHROPIC_API_KEY`** for Claude scoring — paste your real key from the Anthropic console (do not commit it).

> **Sandbox key minting returns 429?**  
> Your network has hit PyAI's sandbox key limit (rate limited per IP/network).  
> Switch to a different internet connection (e.g. phone hotspot) and restart,  
> or add a live `PYAI_API_KEY` from [console.pyai.com](https://console.pyai.com) to `.env` manually.

---

## Install & run (every terminal command)

Use **two terminal tabs**. Commands use `~/callproof` — adjust if your clone lives elsewhere.

### Prerequisites

- Git  
- Python **3.11+** (3.12 fine)  
- Node.js **20+** and npm  
- Internet (PyAI + Anthropic)  
- Optional: system **ffmpeg** if browser/server Hear transcodes fail on your machine (`imageio-ffmpeg` is already a Python dependency for many paths)

Check:

```bash
git --version
python3 --version
node --version
npm --version
```

---

### A. Clone and enter the repo

```bash
git clone https://github.com/mohammed-kashiff/callproof.git
cd callproof
git checkout v2testing-ui-final
git pull origin v2testing-ui-final
```

If you already have the repo:

```bash
cd ~/callproof
git checkout v2testing-ui-final
git pull origin v2testing-ui-final
```

---

### B. Python backend (sandbox-friendly)

```bash
cd ~/callproof
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create env file:

```bash
cp .env.example .env
```

Edit `.env` (open in any editor). Minimum for Claude scoring:

```bash
# Required for QA — paste your real Anthropic key (never commit .env)
ANTHROPIC_API_KEY=

# Leave blank to auto-mint a PyAI sandbox key on first API start,
# OR paste a sandbox/live key yourself:
PYAI_API_KEY=

AUDIT_MODE=hybrid
```

Optional spend-estimate knobs (already in `.env.example`):

```bash
COST_PYAI_USD_PER_MINUTE=0.01
COST_PYAI_USD_PER_UNIT=0.01
COST_CLAUDE_USD_PER_AUDIT=0.06
COST_CLAUDE_USD_PER_HIT=0.02
```

**Never commit `.env`.**

Start the API (**Terminal 1** — leave it running):

```bash
cd ~/callproof
source .venv/bin/activate
uvicorn api:app --reload --port 8000
```

On first start with an empty `PYAI_API_KEY`, the API tries to mint a free sandbox key and write it into `.env`. Watch the terminal for:

- `No PYAI_API_KEY found — minting a free sandbox key...`
- or `PYAI_API_KEY present (sandbox key)` / `(configured key)`

> **If you see `sandbox key minting failed (HTTP 429)`:**  
> Your network has hit PyAI's sandbox key limit. Switch to a phone hotspot  
> and restart uvicorn, or add a live key to `.env` manually (see Section G).

API base: **http://127.0.0.1:8000**

Quick check (optional, new tab):

```bash
curl -s http://127.0.0.1:8000/api/calls | head
curl -s http://127.0.0.1:8000/api/pyai/status | head
```

---

### C. Frontend — Call Loop UI

**Terminal 2:**

```bash
cd ~/callproof/frontend
npm install
npm run dev
```

UI: **http://127.0.0.1:5173**

Open that URL in your browser.

---

### D. Day-to-day restart (after install)

**Terminal 1 — API**

```bash
cd ~/callproof
source .venv/bin/activate
uvicorn api:app --reload --port 8000
```

**Terminal 2 — UI**

```bash
cd ~/callproof/frontend
npm run dev
```

---

### E. Update to latest `v2testing-ui-final`

```bash
cd ~/callproof
git checkout v2testing-ui-final
git pull origin v2testing-ui-final
source .venv/bin/activate
pip install -r requirements.txt
cd frontend
npm install
```

Then restart API + UI as in section D.

---

### F. Free ports if something is already bound

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs kill
lsof -tiTCP:5173 -sTCP:LISTEN | xargs kill
```

Then start API and UI again.

---

### G. Optional: use a live PyAI key (full transcription)

1. Create a live key at [console.pyai.com](https://console.pyai.com) with `transcribe:jobs`.
2. Put it in `.env`:

```bash
PYAI_API_KEY=pyai_live_your_key_here
```

3. Restart uvicorn (Ctrl+C in Terminal 1, then start again).

---

## Smoke checklist

1. UI loads at http://127.0.0.1:5173  
2. Status chip shows **SANDBOX** or **LIVE**  
3. Upload a short call (or use Agents Pulse / call list if data exists)  
4. Open a scorecard after audit completes  
5. Logs: `logs/callproof.log`  
6. Expect **Training** to say Coming soon — that is normal

---

## Useful paths

| Path | Purpose |
|------|---------|
| `.env` | Secrets (local only) |
| `logs/callproof.log` | Backend event log |
| `callproof.db` | Calls, segments, audits, usage |
| `audio/` | Playback copies |
| `rubric_v8.json` / `rules_v8.py` | Scoring rubric |
| `docs/qa-engine-overview.md` | Presentation guide: pipeline, APIs, models, scoring, hybrid |

---

## Security notes

- Do not commit API keys or `.env`.
- Sandbox keys are for bootstrap; treat live keys as secrets.
- Cost figures in the UI are **estimates** from local usage × `COST_*` rates, not provider invoices.

---

## License / contact

Internal CallProof / Call Loop project. For PyAI keys and Anthropic access, use your team's console accounts.
