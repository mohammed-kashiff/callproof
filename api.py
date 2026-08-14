"""
CallProof - FastAPI backend (v3, with logging + auto sandbox key).

Every request logs what it does. Crucially, /audit logs whether it served from
CACHE (stable score) or recomputed (MISS) - so you can see, per request, why a
score is or isn't changing.

On first run with no PYAI_API_KEY in the environment, the server automatically
mints a free PyAI sandbox key (POST /v1/sandbox/keys — no auth, no card) and
writes it to .env so it survives restarts until expiry.

Note: sandbox keys only include hear:transcribe (not transcribe:jobs / recap:*).
CallProof QA needs speaker-labelled async Hear jobs — use a live key for the
full stack. Sandbox minting is a bootstrap aid, not a production substitute.
"""

from __future__ import annotations

import os
import csv
import io
import re
import json
import hashlib
import logging
import sqlite3
import time
import uuid
import shutil
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

import qa_v8

import applog

load_dotenv()
applog.setup_logging()

import qa_engine as qa
import transcribe
import recap as pyai_recap
import email_notify
import pyai_usage
import cost_estimate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.api")

DB_PATH = qa.DB_PATH
AUDIO_DIR = "audio"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BULK_FILES = 100
MAX_BULK_WORKERS = 20
MAX_BATCH_ZIP_BYTES = MAX_UPLOAD_BYTES * MAX_BULK_FILES
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".mpeg", ".mpga", ".aac"}
_db_lock = threading.Lock()

PYAI_SANDBOX_MINT_URL = "https://api.pyai.com/v1/sandbox/keys"
ENV_FILE = ".env"

app = FastAPI(title="CallProof API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_http(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:  # noqa: BLE001
        duration_ms = (time.perf_counter() - started) * 1000
        applog.event(
            log, "http_error",
            level=logging.ERROR,
            method=request.method,
            path=request.url.path,
            duration_ms=round(duration_ms, 1),
            error=f"{type(e).__name__}: {e}",
        )
        raise

    duration_ms = (time.perf_counter() - started) * 1000
    status = response.status_code
    fields = {
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    if status >= 400:
        applog.event(log, "http_error", level=logging.ERROR, **fields)
    else:
        applog.event(log, "http_request", **fields)
    return response


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _apply_pyai_key(api_key: str):
    """Push a key into the process + both PyAI client modules."""
    transcribe.set_api_key(api_key)
    pyai_recap.set_api_key(api_key)


# ── Auto sandbox key mint ─────────────────────────────────────────────────────
def _mint_sandbox_key():
    """
    If PYAI_API_KEY is not set, hit POST /v1/sandbox/keys (no auth needed)
    to get a free pyai_test_... key. Writes it to .env so it persists across
    restarts until expiry. Called once at startup.
    """
    try:
        resp = httpx.post(
            PYAI_SANDBOX_MINT_URL,
            json={"label": "callproof"},
            timeout=10,
        )
        if resp.status_code == 404:
            log.warning(
                "sandbox key minting is disabled on this PyAI deployment.\n"
                "   ➤ Add a live PYAI_API_KEY to .env manually and restart."
            )
            return None

        if resp.status_code in (429, 529):
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail") or body.get("title") or ""
            except Exception:
                detail = (resp.text or "")[:200]
            log.warning(
                "sandbox key minting failed (HTTP %s)%s\n"
                "   ➤ This network has hit PyAI's sandbox-key limit.\n"
                "   ➤ Change to a different internet connection (e.g. phone hotspot) and retry,\n"
                "   ➤ or add a live PYAI_API_KEY from https://console.pyai.com to .env and restart.",
                resp.status_code,
                f": {detail}" if detail else "",
            )
            return None

        if resp.status_code != 201:
            log.warning(
                "sandbox key minting failed (HTTP %s): %s\n"
                "   ➤ Add a live PYAI_API_KEY to .env manually and restart.",
                resp.status_code,
                resp.text[:200],
            )
            return None

        data = resp.json()
        api_key = data.get("api_key")
        expires = data.get("expires_at")  # Unix epoch ms

        if not api_key:
            log.warning("sandbox key response had no api_key field")
            return None

        expiry_str = "unknown"
        if expires:
            try:
                expiry_str = datetime.fromtimestamp(
                    expires / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

        _write_key_to_env(api_key)
        _apply_pyai_key(api_key)

        scopes = data.get("scopes") or []
        log.info(
            "PyAI sandbox key minted (expires %s; scopes: %s). "
            "Sandbox keys cannot run diarized Hear jobs or Recap — "
            "replace with a live key in .env for the full CallProof stack.",
            expiry_str,
            ", ".join(scopes) if scopes else "unknown",
        )
        return api_key

    except httpx.RequestError as e:
        log.warning(
            "could not reach PyAI to mint sandbox key: %s\n"
            "   ➤ Check your internet connection, or add PYAI_API_KEY to .env.",
            e,
        )
        return None


def _write_key_to_env(api_key: str):
    """
    Upsert PYAI_API_KEY in .env. Creates the file if it doesn't exist.
    Replaces a blank PYAI_API_KEY= line; never overwrites a non-empty key.
    """
    try:
        lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r") as f:
                lines = f.readlines()

        new_lines = []
        replaced = False
        for line in lines:
            if line.startswith("PYAI_API_KEY="):
                existing = line.split("=", 1)[1].strip().strip('"').strip("'")
                if existing:
                    # Keep a manually configured key; do not clobber.
                    new_lines.append(line)
                    replaced = True
                else:
                    new_lines.append(f"PYAI_API_KEY={api_key}\n")
                    replaced = True
            else:
                new_lines.append(line)

        if not replaced:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] = new_lines[-1] + "\n"
            new_lines.append(f"PYAI_API_KEY={api_key}\n")

        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)

    except OSError as e:
        log.warning("could not write sandbox key to .env: %s", e)


# ── Startup ───────────────────────────────────────────────────────────────────
def _startup():
    pyai_key = (os.environ.get("PYAI_API_KEY") or "").strip()
    if not pyai_key:
        log.info("No PYAI_API_KEY found — minting a free sandbox key...")
        minted = _mint_sandbox_key()
        if not minted:
            log.warning(
                "No PYAI_API_KEY available. Uploads will fail until you add one."
            )
    else:
        # Ensure both client modules see the same key (import order / blank reload).
        _apply_pyai_key(pyai_key)
        kind = "sandbox" if pyai_key.startswith("pyai_test_") else "configured"
        log.info("PYAI_API_KEY present (%s key)", kind)
        if kind == "sandbox":
            log.warning(
                "Using a sandbox key: async diarized jobs and Recap are likely "
                "unavailable. Prefer a live key with transcribe:jobs + recap:read."
            )

    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        log.warning(
            "ANTHROPIC_API_KEY is not set.\n"
            "   ➤ The QA engine needs this.\n"
            "   ➤ Get one at https://console.anthropic.com and add to .env"
        )

    transcribe.init_db().close()
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS audits ("
            "call_id INTEGER PRIMARY KEY, audit_json TEXT, "
            "rubric_hash TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        cols = [r[1] for r in c.execute("PRAGMA table_info(audits)").fetchall()]
        if "rubric_hash" not in cols:
            c.execute("ALTER TABLE audits ADD COLUMN rubric_hash TEXT")
        call_cols = [r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()]
        if "filename" not in call_cols:
            c.execute("ALTER TABLE calls ADD COLUMN filename TEXT")
    os.makedirs(AUDIO_DIR, exist_ok=True)
    pyai_usage.init_usage_db(DB_PATH)
    log.info("startup complete; db=%s audit_mode=%s claude_model=%s", DB_PATH, qa.audit_mode(), qa.MODEL)


_startup()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rubric_hash():
    with open(qa.RUBRIC_PATH, "rb") as f:
        body = f.read()
    # Bust cache when scoring policy changes (hybrid vs full).
    body += (
        f"\naudit_mode={qa.audit_mode()}\nrole=channel\nmodel={qa.MODEL}\n"
        f"claude_effort={getattr(qa, 'CLAUDE_EFFORT', 'high')}\n"
        f"rules_rev=tone_banks_v3\n"
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


def _call_filename(call_id: int) -> str:
    with _conn() as c:
        row = c.execute("SELECT filename FROM calls WHERE id=?", (call_id,)).fetchone()
    name = (row["filename"] if row else None) or ""
    name = name.strip()
    return name or f"call-{call_id}.mp3"


def _attach_filename(audit: dict, call_id: int) -> dict:
    out = dict(audit)
    out["call_id"] = call_id
    out["filename"] = _call_filename(call_id)
    return out



def analyze_call(call_id, agent_override=None):
    started = time.perf_counter()
    applog.event(log, "audit_started", call_id=call_id)

    with _conn() as c:
        exists = c.execute(
            "SELECT 1 FROM calls WHERE id=? AND status='completed'", (call_id,)
        ).fetchone()
    if not exists:
        applog.event(
            log, "audit_failed", level=logging.ERROR,
            call_id=call_id, error="call_not_found_or_incomplete",
        )
        raise HTTPException(
            status_code=404, detail=f"No completed call with id {call_id}"
        )

    call_id, meta, segments = qa.load_call(call_id)
    if not segments:
        applog.event(
            log, "audit_failed", level=logging.ERROR,
            call_id=call_id, error="no_segments",
        )
        raise HTTPException(
            status_code=422, detail=f"Call {call_id} has no segments"
        )

    agent = agent_override or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    with open(qa.RUBRIC_PATH) as f:
        rubric = json.load(f)

    mode = qa.audit_mode()
    is_v8 = qa_v8.is_v8_rubric(rubric)
    if is_v8:
        n_items = len(qa_v8.list_dimensions(rubric))
        log.info(
            "computing audit for call %d (%d v8 dimensions, mode=%s)",
            call_id, n_items, mode,
        )
        criteria_arg = []
    else:
        n_items = len(rubric["criteria"])
        log.info(
            "computing audit for call %d (%d criteria, mode=%s)",
            call_id, n_items, mode,
        )
        criteria_arg = rubric["criteria"]

    # One parallel wave: dimensions/criteria + churn + Recap.
    # Retention email and areas of improvement are on-demand.
    with ThreadPoolExecutor(max_workers=2) as pool:
        wave_f = pool.submit(
            qa.run_parallel_claude_wave,
            criteria_arg, segments, agent, transcript_text,
            None, rubric,
        )
        recap_f = pool.submit(
            pyai_recap.ensure_recap,
            call_id, segments, agent,
            meta.get("audio_seconds"), meta.get("pyai_call_id"),
        )
        wave = wave_f.result()
        try:
            call_recap = recap_f.result()
        except Exception as e:  # noqa: BLE001
            log.error("recap failed for call %d: %s", call_id, e)
            applog.event(
                log, "recap_failure", level=logging.ERROR,
                call_id=call_id, error=str(e),
            )
            call_recap = {"status": "error", "error": str(e)}

    churn = wave.get("churn")
    feedback = wave.get("feedback")
    manager_review = wave.get("manager_review") or []
    # On-demand — drafted when Email stakeholder is used
    retention_email = {"status": "pending"}

    if wave.get("mode") == "v8":
        score = wave["score"]
        grade = wave["grade"]
        tally = wave["tally"]
        findings = wave["findings"]
        gate_fails = [t.get("reason", "manager_review") for t in manager_review]
        flagged = bool(manager_review)
    else:
        results = wave["results"]
        _rows, score, _e, _p, tally, gate_fails = qa.score_results(results)
        grade = qa.performance_band(score, rubric)
        findings = [
            {
                "id": cr["id"], "name": cr["name"], "method": cr["method"],
                "weight": cr["weight"], "is_gate": bool(cr.get("is_gate")),
                "verdict": res["verdict"], "reasoning": res.get("reasoning", ""),
                "why": (
                    f"{cr['name']}: {(res.get('verdict') or '').title()} — "
                    f"{qa.awarded_points(cr, res['verdict']) if qa.awarded_points(cr, res['verdict']) is not None else '—'} "
                    f"of {cr['weight']} points. {(res.get('reasoning') or '').strip()}"
                ).strip(),
                "points": qa.awarded_points(cr, res["verdict"]),
                "evidence_text": res.get("evidence_text"),
                "evidence_seq": res.get("evidence_seq"),
                "evidence_verified": res.get("evidence_verified"),
            }
            for cr, res in results
        ]
        flagged = bool(gate_fails)

    duration_ms = (time.perf_counter() - started) * 1000
    applog.event(
        log, "audit_completed",
        call_id=call_id,
        score=score,
        grade=grade,
        duration_ms=round(duration_ms, 1),
        recap_status=(call_recap or {}).get("status"),
        flagged=flagged,
        manager_review_count=len(manager_review),
        audit_mode=qa.audit_mode(),
        agent_speaker=agent,
    )

    return {
        "call_id": call_id,
        "filename": _call_filename(call_id),
        "audio_seconds": meta.get("audio_seconds"),
        "agent_speaker": agent, "rubric": rubric["name"],
        "rubric_id": rubric.get("rubric_id") or rubric.get("name"),
        "score": score, "grade": grade, "tally": tally,
        "gate_fails": gate_fails, "flagged": flagged,
        "manager_review": manager_review,
        "segments": segments, "findings": findings,
        "churn": churn, "feedback": feedback,
        "retention_email": retention_email, "recap": call_recap,
        "audit_mode": qa.audit_mode(),
    }


def _load_or_compute_audit(call_id: int, refresh: bool = False):
    """Return (audit_dict, rubric_hash). Computes and caches on miss/refresh."""
    rh = _rubric_hash()
    prev = None
    prev_hash = None
    with _db_lock:
        with _conn() as c:
            row = c.execute(
                "SELECT audit_json, rubric_hash FROM audits WHERE call_id=?",
                (call_id,),
            ).fetchone()
    if row:
        prev_hash = row["rubric_hash"]
        try:
            prev = json.loads(row["audit_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            prev = None
        if not refresh and prev_hash == rh and isinstance(prev, dict):
            return prev, rh
    audit = analyze_call(call_id)
    if isinstance(prev, dict) and prev.get("manual_review"):
        audit["manual_review"] = True
        audit["flagged"] = True
        if prev.get("manual_review_at"):
            audit["manual_review_at"] = prev["manual_review_at"]
    if isinstance(prev, dict) and prev.get("review_solved"):
        audit["flagged"] = True
        audit["review_solved"] = True
        if prev.get("review_solved_at"):
            audit["review_solved_at"] = prev["review_solved_at"]
    with _db_lock:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO audits (call_id, audit_json, rubric_hash) "
                "VALUES (?, ?, ?)",
                (call_id, json.dumps(audit), rh),
            )
    return audit, rh


_FLAG_REASON_LABELS = {
    "hostile_language_override": "hostile language",
    "low_overall_score": "low overall score",
}


def _audit_is_flagged(audit) -> bool:
    if not isinstance(audit, dict):
        return False
    return bool(
        audit.get("flagged")
        or audit.get("manual_review")
        or (audit.get("manager_review") or [])
        or (audit.get("gate_fails") or [])
    )


def _flag_sources(audit: dict) -> list[str]:
    sources = []
    if audit.get("manual_review"):
        sources.append("manual")
    auto = bool(audit.get("manager_review") or audit.get("gate_fails"))
    if not auto and audit.get("flagged") and not audit.get("manual_review"):
        auto = True
    if auto:
        sources.append("auto")
    return sources


def _flag_reason_text(audit: dict) -> str:
    parts = []
    if audit.get("manual_review"):
        parts.append("manual flag")
    for t in audit.get("manager_review") or []:
        reason = t.get("reason") if isinstance(t, dict) else t
        label = _FLAG_REASON_LABELS.get(reason, str(reason or "").replace("_", " "))
        if label:
            parts.append(label)
    for g in audit.get("gate_fails") or []:
        if not isinstance(g, str):
            continue
        label = _FLAG_REASON_LABELS.get(g, g.replace("_", " "))
        if label:
            parts.append(label)
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return "; ".join(seen)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/api/pyai/status")
def pyai_status():
    """
    Safe PyAI key posture + local CallProof usage counters for the UI.
    Never returns the API key. Usage is CallProof-recorded outbound hits
    (PyAI does not publish a remaining-request counter).
    """
    def _snapshot():
        usage = pyai_usage.usage_summary()
        pyai_u = (usage.get("by_provider") or {}).get("pyai") or {}
        claude_u = (usage.get("by_provider") or {}).get("anthropic") or {}
        return usage, {
            "pyai_hits": int(pyai_u.get("hits") or 0),
            "pyai_actions": int(pyai_u.get("actions") or 0),
            "pyai_polls": int(pyai_u.get("polls") or 0),
            "pyai_units": float(pyai_u.get("units") or 0),
            "claude_hits": int(claude_u.get("hits") or 0),
        }

    def _chip_text(stats, balance_label=None):
        bits = [f"{stats['pyai_actions']} PyAI"]
        if stats["pyai_polls"]:
            bits.append(f"{stats['pyai_polls']} polls")
        bits.append(f"{stats['claude_hits']} Claude")
        if stats["pyai_units"]:
            bits.append(f"{stats['pyai_units']:g} units")
        if balance_label:
            bits.append(balance_label)
        return " · ".join(bits)

    def _pack(usage, stats, **extra):
        parts = []
        if stats["pyai_hits"]:
            parts.append(
                f"PyAI {stats['pyai_actions']} calls / {stats['pyai_hits']} hits"
            )
        else:
            parts.append("PyAI 0 hits today")
        if stats["claude_hits"]:
            parts.append(f"Claude {stats['claude_hits']}")
        if stats["pyai_units"]:
            parts.append(f"{stats['pyai_units']:g} units")
        spend = cost_estimate.today_from_usage(usage)
        out = {
            "usage": usage,
            "usage_label": " · ".join(parts),
            "cost_today": spend,
            "cost_label": spend.get("label"),
            **stats,
        }
        out.update(extra)
        return out

    key = (transcribe.PYAI_API_KEY or os.environ.get("PYAI_API_KEY") or "").strip()
    if not key:
        usage, stats = _snapshot()
        return _pack(
            usage, stats,
            ok=False,
            configured=False,
            env=None,
            label="No key",
            status="missing",
            quota_label=_chip_text(stats, "Add PYAI_API_KEY"),
            healthy=False,
        )

    kind = "sandbox" if key.startswith("pyai_test_") else "live"
    try:
        r = pyai_usage.get(
            f"{transcribe.BASE_URL}/v1/me",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        log.warning("pyai /v1/me failed: %s", e)
        usage, stats = _snapshot()
        return _pack(
            usage, stats,
            ok=False,
            configured=True,
            env="test" if kind == "sandbox" else "live",
            label="Sandbox" if kind == "sandbox" else "Live",
            status="unreachable",
            quota_label=_chip_text(stats, "Could not reach PyAI"),
            healthy=False,
            error="unreachable",
        )

    usage, stats = _snapshot()

    if r.status_code == 401:
        return _pack(
            usage, stats,
            ok=False,
            configured=True,
            env="test" if kind == "sandbox" else "live",
            label="Sandbox" if kind == "sandbox" else "Live",
            status="unauthorized",
            quota_label=_chip_text(stats, "Key invalid or revoked"),
            healthy=False,
            error="unauthorized",
        )

    if r.status_code != 200:
        return _pack(
            usage, stats,
            ok=False,
            configured=True,
            env="test" if kind == "sandbox" else "live",
            label="Sandbox" if kind == "sandbox" else "Live",
            status="error",
            quota_label=_chip_text(stats, f"HTTP {r.status_code}"),
            healthy=False,
            error=f"http_{r.status_code}",
        )

    body = r.json() if r.content else {}
    env = (body.get("env") or ("test" if kind == "sandbox" else "live")).lower()
    is_sandbox = env == "test" or kind == "sandbox"
    label = "Sandbox" if is_sandbox else "Live"
    limits = body.get("limits") or {}
    key_status = body.get("status") or "unknown"
    healthy = key_status == "active" and (body.get("org_status") or "active") == "active"

    daily_cap = limits.get("daily_unit_cap")
    if is_sandbox:
        # Sandbox is not billed; optional daily unit cap only (not prepaid $).
        balance_label = (
            f"cap {daily_cap} u/day" if daily_cap is not None else "not billed"
        )
        quota_kind = "sandbox_daily"
        quota_value = daily_cap
    else:
        # Live: show local usage only — do not surface prepaid credit balance.
        balance_label = None
        quota_kind = "live_usage"
        quota_value = None

    return _pack(
        usage, stats,
        ok=True,
        configured=True,
        env=env,
        label=label,
        status=key_status,
        org_status=body.get("org_status"),
        plan=body.get("plan"),
        healthy=healthy,
        quota_kind=quota_kind,
        quota_label=_chip_text(stats, balance_label),
        quota_value=quota_value,
        balance_label=balance_label,
        limits={
            "rps": limits.get("rps"),
            "burst": limits.get("burst"),
            "concurrency": limits.get("concurrency"),
            "daily_unit_cap": daily_cap,
            "monthly_units": limits.get("monthly_units"),
        },
    )


@app.get("/api/dev/logs")
def dev_logs(lines: int = 200):
    """
    Tail CallProof's rotating app log (logs/callproof.log) for the Dev Logs UI.
    Secrets are redacted. Same structured events as the terminal callproof.* stream.
    """
    payload = applog.read_tail(lines=lines)
    usage = pyai_usage.usage_summary()
    payload["usage"] = {
        "total_hits": usage.get("total_hits"),
        "total_actions": usage.get("total_actions"),
        "total_polls": usage.get("total_polls"),
        "total_units": usage.get("total_units"),
        "by_provider": usage.get("by_provider"),
        "top_paths": usage.get("top_paths"),
        "window": usage.get("window"),
    }
    return payload


@app.get("/api/calls")
def list_calls():
    """
    Library listing of stored calls (SQLite). Omits raw transcript / raw_json
    payloads — those stay on the audit/detail paths.
    """
    rh = _rubric_hash()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              c.id,
              c.status,
              c.audio_seconds,
              c.speakers,
              c.created_at,
              c.pyai_call_id,
              c.job_id,
              c.filename,
              (SELECT COUNT(*) FROM segments s WHERE s.call_id = c.id) AS segment_count,
              a.audit_json,
              a.rubric_hash,
              a.created_at AS audited_at
            FROM calls c
            LEFT JOIN audits a ON a.call_id = c.id
            ORDER BY c.id DESC
            """
        ).fetchall()

    out = []
    for r in rows:
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        item = {
            "id": r["id"],
            "filename": fname,
            "status": r["status"],
            "audio_seconds": r["audio_seconds"],
            "speakers": r["speakers"],
            "created_at": r["created_at"],
            "pyai_call_id": r["pyai_call_id"],
            "job_id": r["job_id"],
            "segment_count": r["segment_count"] or 0,
            "has_audit": False,
            "audit_fresh": False,
            "score": None,
            "grade": None,
            "flagged": False,
            "review_solved": False,
            "churn_risk": "",
            "audited_at": r["audited_at"],
        }
        if r["audit_json"]:
            try:
                cached = json.loads(r["audit_json"])
                item["has_audit"] = True
                item["audit_fresh"] = r["rubric_hash"] == rh
                item["score"] = cached.get("score")
                item["grade"] = cached.get("grade")
                item["flagged"] = _audit_is_flagged(cached)
                item["review_solved"] = bool(cached.get("review_solved"))
                churn = cached.get("churn") or {}
                if isinstance(churn, dict):
                    item["churn_risk"] = str(churn.get("risk") or "").strip().lower()
            except (TypeError, json.JSONDecodeError):
                item["has_audit"] = True
        item["cost"] = cost_estimate.estimate_call_cost(
            item.get("audio_seconds"),
            has_audit=bool(item.get("has_audit")),
        )
        out.append(item)
    return out


def _playback_root() -> str:
    return os.path.realpath(AUDIO_DIR)


def _clear_playback_audio() -> int:
    """Delete playback copies and batch temps under audio/. Keeps the folder."""
    root = _playback_root()
    if not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        real = os.path.realpath(path)
        if not real.startswith(root + os.sep):
            log.warning("skipping audio path outside %s", AUDIO_DIR)
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
                removed += 1
            elif os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
                removed += 1
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
    return removed


@app.post("/api/cache/clear")
def clear_cache():
    """Delete stored transcripts, scorecards, and playback audio so the next
    upload transcribes and scores from scratch."""
    with _db_lock:
        with _conn() as c:
            n_calls = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            n_segments = c.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            n_audits = c.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
            c.execute("DELETE FROM audits")
            c.execute("DELETE FROM segments")
            c.execute("DELETE FROM calls")
            try:
                c.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN (?, ?, ?)",
                    ("calls", "segments", "audits"),
                )
            except sqlite3.OperationalError:
                pass
            c.commit()
    n_audio = _clear_playback_audio()
    applog.event(
        log, "cache_cleared",
        calls=n_calls, segments=n_segments, audits=n_audits, audio=n_audio,
    )
    log.info(
        "cache cleared: %d call(s), %d segment(s), %d scorecard(s), %d audio item(s)",
        n_calls, n_segments, n_audits, n_audio,
    )
    return {
        "status": "ok",
        "deleted": {
            "calls": n_calls,
            "segments": n_segments,
            "audits": n_audits,
            "audio": n_audio,
        },
    }


def _recap_export_fields(recap: dict | None) -> tuple[str, str, str]:
    recap = recap or {}
    if recap.get("status") and recap.get("status") != "ok":
        err = (recap.get("error") or recap.get("status") or "").strip()
        return "", "", err
    tldr = (recap.get("tldr") or recap.get("headline") or "").strip()
    summary = (recap.get("summary") or "").strip()
    actions = []
    for it in recap.get("action_items") or []:
        if isinstance(it, dict):
            task = (it.get("task") or "").strip()
            meta = " · ".join(
                x for x in [(it.get("owner") or "").strip(), (it.get("due") or "").strip()] if x
            )
            if task:
                actions.append(f"{task} ({meta})" if meta else task)
        elif it:
            actions.append(str(it).strip())
    return tldr, summary, " | ".join(actions)


def _ratings_export_text(findings: list | None) -> str:
    parts = []
    for f in findings or []:
        name = f.get("name") or f.get("id") or "criterion"
        verdict = f.get("verdict") or ""
        pts = f.get("points")
        weight = f.get("weight")
        if pts is not None and weight is not None:
            parts.append(f"{name}={verdict} ({pts}/{weight})")
        else:
            parts.append(f"{name}={verdict}")
    return " | ".join(parts)


_SCORECARD_DIM_ORDER = (
    "resolution_effectiveness",
    "ownership_next_steps",
    "active_listening",
    "tone_empathy_professionalism",
)
_AGENT_NAME_RE = re.compile(
    r"\b(?:my name is|i am|i'm)\s+([a-z][a-z'`.-]{1,32}(?:\s+[a-z][a-z'`.-]{1,32})?)",
    re.I,
)
_AGENT_NAME_STOP = {
    "calling", "from", "with", "your", "the", "here", "today", "a", "an",
    "sorry", "happy", "glad", "going", "looking", "checking", "trying",
    "just", "so", "very", "really",
}


def _xml_text(value) -> str:
    return xml_escape("" if value is None else str(value), {'"': "&quot;"})


def _agent_display_name(audit: dict) -> str:
    """Best-effort name from the agent's opening turns; else speaker label."""
    speaker = audit.get("agent_speaker")
    snippets = []
    for s in audit.get("segments") or []:
        if speaker and s.get("speaker") != speaker:
            continue
        snippets.append(s.get("text") or "")
        if len(snippets) >= 12:
            break
    blob = " ".join(snippets)
    m = _AGENT_NAME_RE.search(blob)
    if m:
        words = [
            w for w in m.group(1).replace(".", " ").split()
            if w.lower() not in _AGENT_NAME_STOP
        ]
        if words:
            return " ".join(words).title()
    return speaker or "Unknown"


def _score_style_id(score) -> str:
    try:
        n = float(score)
    except (TypeError, ValueError):
        return "cell"
    if n >= 80:
        return "scoreGood"
    if n >= 60:
        return "scoreMid"
    return "scoreLow"


def _scorecard_dimension_columns(records: list[dict]) -> list[tuple[str, str]]:
    """Return [(finding_id, header_label), ...] in rubric order, then extras."""
    seen: dict[str, str] = {}
    for rec in records:
        for f in rec.get("findings") or []:
            fid = f.get("id") or f.get("name")
            if not fid:
                continue
            seen.setdefault(str(fid), f.get("name") or str(fid))
    ordered = []
    for fid in _SCORECARD_DIM_ORDER:
        if fid in seen:
            ordered.append((fid, seen.pop(fid)))
    rest = sorted(seen.items(), key=lambda kv: kv[1].lower())
    return ordered + rest


def _scorecard_cell_xml(value, style="cell", number=False) -> str:
    if value is None or value == "":
        return f'<Cell ss:StyleID="{style}"/>'
    if number:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return (
                f'<Cell ss:StyleID="{style}">'
                f'<Data ss:Type="String">{_xml_text(value)}</Data></Cell>'
            )
        if num.is_integer():
            shown = str(int(num))
        else:
            shown = str(num)
        return (
            f'<Cell ss:StyleID="{style}">'
            f'<Data ss:Type="Number">{shown}</Data></Cell>'
        )
    return (
        f'<Cell ss:StyleID="{style}">'
        f'<Data ss:Type="String">{_xml_text(value)}</Data></Cell>'
    )


def _scorecard_xls(records: list[dict]) -> bytes:
    """Excel XML Spreadsheet (opens in Excel/Sheets) with a colored Score column.

    Plain CSV cannot store cell colors; this is the spreadsheet form of a CSV table.
    """
    dim_cols = _scorecard_dimension_columns(records)
    headers = [
        "Recording ID",
        "Recording name",
        "Agent name",
        "Score",
        "Grade",
    ] + [label for _fid, label in dim_cols]

    rows_xml = [
        "<Row>"
        + "".join(_scorecard_cell_xml(h, style="header") for h in headers)
        + "</Row>"
    ]
    for rec in records:
        by_id = {}
        for f in rec.get("findings") or []:
            fid = f.get("id") or f.get("name")
            if fid:
                by_id[str(fid)] = f
        score = rec.get("score")
        cells = [
            _scorecard_cell_xml(rec.get("call_id"), number=True),
            _scorecard_cell_xml(rec.get("filename")),
            _scorecard_cell_xml(rec.get("agent_name")),
            _scorecard_cell_xml(score, style=_score_style_id(score), number=True),
            _scorecard_cell_xml(rec.get("grade")),
        ]
        for fid, _label in dim_cols:
            f = by_id.get(fid) or {}
            pts = f.get("points")
            weight = f.get("weight")
            if pts is not None and weight is not None:
                cells.append(_scorecard_cell_xml(f"{pts}/{weight}"))
            elif pts is not None:
                cells.append(_scorecard_cell_xml(pts, number=True))
            else:
                cells.append(_scorecard_cell_xml(f.get("verdict")))
        rows_xml.append("<Row>" + "".join(cells) + "</Row>")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Styles>
  <Style ss:ID="header">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#1F2937" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="cell"/>
  <Style ss:ID="scoreGood">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#16A34A" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="scoreMid">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#EA580C" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="scoreLow">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#DC2626" ss:Pattern="Solid"/>
  </Style>
 </Styles>
 <Worksheet ss:Name="Scorecard">
  <Table>
   {"".join(rows_xml)}
  </Table>
 </Worksheet>
</Workbook>
"""
    return xml.encode("utf-8")


def _load_scorecard_records() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              c.id,
              c.filename,
              a.audit_json
            FROM calls c
            INNER JOIN audits a ON a.call_id = c.id
            WHERE c.status = 'completed' OR c.status IS NULL OR c.status = ''
            ORDER BY c.id ASC
            """
        ).fetchall()
    records = []
    for r in rows:
        try:
            audit = json.loads(r["audit_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(audit, dict):
            continue
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        records.append({
            "call_id": r["id"],
            "filename": fname,
            "agent_name": _agent_display_name(audit),
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "findings": audit.get("findings") or [],
        })
    return records


@app.get("/api/calls/flagged")
def list_flagged_calls():
    """Scorecards flagged for manager review (manual button or auto triggers)."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              c.id,
              c.filename,
              c.audio_seconds,
              c.created_at,
              a.audit_json,
              a.created_at AS audited_at
            FROM calls c
            INNER JOIN audits a ON a.call_id = c.id
            ORDER BY c.id DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        try:
            audit = json.loads(r["audit_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not _audit_is_flagged(audit):
            continue
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        recap = audit.get("recap") if isinstance(audit.get("recap"), dict) else {}
        out.append({
            "id": r["id"],
            "filename": fname,
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "agent_name": _agent_display_name(audit),
            "audio_seconds": r["audio_seconds"],
            "flagged": True,
            "manual_review": bool(audit.get("manual_review")),
            "solved": bool(audit.get("review_solved")),
            "sources": _flag_sources(audit),
            "reasons": _flag_reason_text(audit),
            "recap_tldr": (recap.get("tldr") or recap.get("headline") or "").strip(),
            "created_at": r["created_at"],
            "audited_at": r["audited_at"],
            "manual_review_at": audit.get("manual_review_at"),
            "review_solved_at": audit.get("review_solved_at"),
        })
    pending = sum(1 for i in out if not i["solved"])
    applog.event(log, "flagged_list", count=len(out), pending=pending, solved=len(out) - pending)
    return out


@app.get("/api/calls/export-scorecard")
def export_scorecard():
    """Scorecard spreadsheet: recording id/name, agent, overall score (colored),
    and each scoring area. Excel XML so the Score column can be green/orange/red
    (plain CSV cannot store cell colors)."""
    records = _load_scorecard_records()
    if not records:
        raise HTTPException(
            status_code=404,
            detail="No audited calls to export. Score a call first.",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body = _scorecard_xls(records)
    applog.event(log, "scorecard_exported", count=len(records))
    log.info("scorecard export %d audited call(s)", len(records))
    return Response(
        content=body,
        media_type="application/vnd.ms-excel",
        headers={
            "Content-Disposition": (
                f'attachment; filename="callproof-scorecard-{stamp}.xls"'
            ),
        },
    )


@app.get("/api/calls/export")
def export_calls(format: str = "csv"):
    """
    One-click bulk export of score/grade, finding ratings, and recap.
    Omits raw transcripts. Defaults to CSV download; use format=json for JSON.
    """
    fmt = (format or "csv").strip().lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")

    with _conn() as c:
        rows = c.execute(
            """
            SELECT
              c.id,
              c.filename,
              c.status,
              c.audio_seconds,
              c.created_at,
              a.audit_json,
              a.created_at AS audited_at
            FROM calls c
            INNER JOIN audits a ON a.call_id = c.id
            WHERE c.status = 'completed' OR c.status IS NULL OR c.status = ''
            ORDER BY c.id ASC
            """
        ).fetchall()

    records = []
    for r in rows:
        try:
            audit = json.loads(r["audit_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(audit, dict):
            continue

        recap_tldr, recap_summary, recap_actions = _recap_export_fields(audit.get("recap"))
        churn = audit.get("churn") or {}
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        records.append({
            "call_id": r["id"],
            "filename": fname,
            "created_at": r["created_at"] or "",
            "audited_at": r["audited_at"] or "",
            "audio_seconds": r["audio_seconds"],
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "flagged": bool(audit.get("flagged")),
            "churn_risk": (churn.get("risk") or ""),
            "ratings": _ratings_export_text(audit.get("findings")),
            "recap_tldr": recap_tldr,
            "recap_summary": recap_summary,
            "recap_actions": recap_actions,
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    applog.event(log, "calls_exported", count=len(records), format=fmt)
    log.info("bulk export %d audited call(s) as %s", len(records), fmt)

    if fmt == "json":
        body = json.dumps({"exported_at": stamp, "count": len(records), "calls": records}, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="callproof-export-{stamp}.json"',
            },
        )

    buf = io.StringIO()
    fields = [
        "filename", "call_id", "created_at", "audited_at", "audio_seconds",
        "score", "grade", "flagged", "churn_risk", "ratings",
        "recap_tldr", "recap_summary", "recap_actions",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)

    # UTF-8 BOM helps Excel open the CSV cleanly
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="callproof-export-{stamp}.csv"',
        },
    )


@app.get("/api/calls/{call_id}/audit")
def get_audit(call_id: int, refresh: bool = False):
    rh = _rubric_hash()
    if not refresh:
        with _conn() as c:
            row = c.execute(
                "SELECT audit_json, rubric_hash FROM audits WHERE call_id=?",
                (call_id,),
            ).fetchone()
        if row and row["rubric_hash"] == rh:
            cached = json.loads(row["audit_json"])
            applog.event(
                log, "audit_cache",
                result="HIT", call_id=call_id, score=cached.get("score"),
            )
            log.info(
                "cache HIT  call %d (score %s) - returning stored audit",
                call_id, cached.get("score"),
            )
            return _attach_filename(cached, call_id)
        applog.event(log, "audit_cache", result="MISS", call_id=call_id)
        log.info("cache MISS  call %d - computing fresh audit", call_id)
    else:
        applog.event(log, "audit_cache", result="BYPASS", call_id=call_id)
        log.info("cache BYPASS (refresh) call %d - computing fresh audit", call_id)
    audit, _rh = _load_or_compute_audit(call_id, refresh=True)
    log.info("cached audit for call %d (score %s)", call_id, audit["score"])
    return _attach_filename(audit, call_id)


def _save_audit(call_id: int, audit: dict, rh: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO audits (call_id, audit_json, rubric_hash) "
            "VALUES (?, ?, ?)",
            (call_id, json.dumps(audit), rh),
        )


@app.post("/api/calls/{call_id}/flag")
def flag_call_for_review(call_id: int):
    """Persist a manual manager-review flag on the stored scorecard."""
    if call_id < 1:
        raise HTTPException(status_code=400, detail="Invalid call id.")
    with _conn() as c:
        row = c.execute(
            "SELECT audit_json, rubric_hash FROM audits WHERE call_id=?",
            (call_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="No scorecard for this call. Score it before flagging.",
        )
    try:
        audit = json.loads(row["audit_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    if not isinstance(audit, dict):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    already = _audit_is_flagged(audit)
    already_manual = bool(audit.get("manual_review"))
    already_solved = bool(audit.get("review_solved"))
    audit["flagged"] = True
    audit["manual_review"] = True
    if not already:
        audit["review_solved"] = False
        audit["review_solved_at"] = None
    if not audit.get("manual_review_at"):
        audit["manual_review_at"] = datetime.now(timezone.utc).isoformat()
    rh = row["rubric_hash"] or _rubric_hash()
    _save_audit(call_id, audit, rh)
    applog.event(
        log, "call_flagged",
        call_id=call_id,
        source="manual",
        already=already_manual,
        solved=bool(audit.get("review_solved")),
    )
    log.info("call %d flagged for manual review", call_id)
    return {
        "status": "ok",
        "call_id": call_id,
        "flagged": True,
        "manual_review": True,
        "solved": bool(audit.get("review_solved")),
        "reasons": _flag_reason_text(audit),
        "already": already_manual or already_solved,
    }


@app.post("/api/calls/{call_id}/solve")
def solve_flagged_review(call_id: int):
    """Move a flagged scorecard from Pending to Solved."""
    if call_id < 1:
        raise HTTPException(status_code=400, detail="Invalid call id.")
    with _conn() as c:
        row = c.execute(
            "SELECT audit_json, rubric_hash FROM audits WHERE call_id=?",
            (call_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="No scorecard for this call.",
        )
    try:
        audit = json.loads(row["audit_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    if not isinstance(audit, dict):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    if not _audit_is_flagged(audit):
        raise HTTPException(
            status_code=400,
            detail="This call is not in the review queue.",
        )
    already = bool(audit.get("review_solved"))
    audit["flagged"] = True
    audit["review_solved"] = True
    if not audit.get("review_solved_at"):
        audit["review_solved_at"] = datetime.now(timezone.utc).isoformat()
    rh = row["rubric_hash"] or _rubric_hash()
    _save_audit(call_id, audit, rh)
    applog.event(
        log, "review_solved",
        call_id=call_id,
        already=already,
    )
    log.info("call %d review marked solved", call_id)
    return {
        "status": "ok",
        "call_id": call_id,
        "flagged": True,
        "solved": True,
        "already": already,
    }


def _ensure_retention_draft(call_id: int, audit: dict, rh: str) -> dict:
    """
    Run the retention Claude draft once if missing, cache on the audit, return updated audit.
    """
    existing = audit.get("retention_email") or {}
    if existing.get("status") == "ok" and (existing.get("body") or "").strip():
        return audit

    call_id, _meta, segments = qa.load_call(call_id)
    if not segments:
        audit["retention_email"] = {
            "status": "error",
            "error": "No transcript segments available for retention draft.",
            "subject": "",
            "body": "",
            "summary": "",
            "suggested_actions": [],
        }
        _save_audit(call_id, audit, rh)
        return audit

    agent = audit.get("agent_speaker") or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    log.info("on-demand retention draft for call %d", call_id)
    draft = qa.draft_retention_email(transcript_text, segments)
    audit["retention_email"] = draft
    _save_audit(call_id, audit, rh)
    return audit


@app.post("/api/calls/{call_id}/feedback")
def post_feedback(call_id: int):
    """On-demand areas of improvement (Sonnet, effort=high). Cached after first success
    that includes at least one agent insight."""
    audit, rh = _load_or_compute_audit(call_id, refresh=False)
    existing = audit.get("feedback") or {}
    if existing.get("status") == "ok" and (existing.get("agent") or []):
        log.info("on-demand feedback cache HIT for call %d", call_id)
        applog.event(log, "feedback_cache", result="HIT", call_id=call_id)
        return {"call_id": call_id, "feedback": existing}

    _cid, _meta, segments = qa.load_call(call_id)
    if not segments:
        audit["feedback"] = {
            "status": "error",
            "error": "No transcript segments available for areas of improvement.",
            "agent": [],
            "product": [],
        }
        _save_audit(call_id, audit, rh)
        applog.event(
            log, "feedback_failure", level=logging.ERROR,
            call_id=call_id, error="no_segments",
        )
        return {"call_id": call_id, "feedback": audit["feedback"]}

    agent = audit.get("agent_speaker") or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    log.info(
        "on-demand areas of improvement for call %d (model=%s effort=%s)",
        call_id, qa.MODEL, qa.CLAUDE_EFFORT,
    )
    feedback = qa.extract_feedback(
        transcript_text, segments, findings=audit.get("findings"),
    )
    audit["feedback"] = feedback
    _save_audit(call_id, audit, rh)
    applog.event(
        log, "feedback_success" if feedback.get("status") == "ok" else "feedback_failure",
        call_id=call_id,
        agent_items=len(feedback.get("agent") or []),
        product_items=len(feedback.get("product") or []),
        status=feedback.get("status"),
        model="claude-sonnet-5",
        effort="high",
    )
    return {"call_id": call_id, "feedback": feedback}


@app.get("/api/calls/{call_id}/stakeholder-email/compose")
def get_stakeholder_email_compose(call_id: int):
    """
    Prefill a Gmail compose draft for this call's churn alert.
    Drafts the retention email with Claude on first use, then caches it.
    Frontend opens gmail_url in a new tab (user sends from their own Gmail).
    """
    audit, rh = _load_or_compute_audit(call_id, refresh=False)
    risk = ((audit.get("churn") or {}).get("risk") or "").lower()
    if risk not in ("high", "medium"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Stakeholder email is only available for medium/high churn risk "
                f"(this call is '{risk or 'unknown'}')."
            ),
        )

    audit = _ensure_retention_draft(call_id, audit, rh)
    payload = email_notify.build_compose_payload(call_id, audit)
    log.info(
        "stakeholder Gmail compose for call %d (risk=%s, to=%s, retention=%s)",
        call_id, risk, payload.get("to") or "(blank)",
        (audit.get("retention_email") or {}).get("status"),
    )
    return {
        "call_id": call_id,
        "status": "compose",
        "churn_risk": risk,
        "to": payload["to"],
        "subject": payload["subject"],
        "body": payload["body"],
        "gmail_url": payload["gmail_url"],
        "retention_email": audit.get("retention_email"),
    }


def _ingest_audio_file(src_path: str, source_name: str) -> tuple[int, bool]:
    """
    Dedup or transcribe one local audio file. Hear temp is unique per src_path.
    Returns (call_id, deduped). Caller stores the playback copy.
    """
    source_name = transcribe.sanitize_filename(source_name)
    identity = transcribe.identity_for(src_path)
    size = os.path.getsize(src_path)
    hear_tmp = f"{src_path}.{uuid.uuid4().hex}.hear.wav"

    try:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                existing = transcribe.find_existing_call(conn, identity)
                if existing:
                    call_id = existing[0]
                    transcribe.set_filename_if_empty(conn, call_id, source_name)
                    applog.event(
                        log, "transcription_success",
                        call_id=call_id, deduped=True, size_bytes=size,
                        filename=source_name,
                    )
                    log.info(
                        "upload deduped to existing call %d (no re-transcription)",
                        call_id,
                    )
                    return call_id, True
            finally:
                conn.close()

        pyai_id = transcribe.new_pyai_call_id()
        if transcribe.is_hear_wav(src_path):
            upload_path = src_path
        else:
            upload_path = transcribe.make_hear_copy(src_path, hear_tmp) or src_path
        job_id = transcribe.submit_job_file(upload_path, call_id=pyai_id)
        result = transcribe.poll_job(job_id)
        with _db_lock:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                existing = transcribe.find_existing_call(conn, identity)
                if existing:
                    call_id = existing[0]
                    transcribe.set_filename_if_empty(conn, call_id, source_name)
                    return call_id, True
                try:
                    call_id = transcribe.save_transcript(
                        conn, identity, job_id, result,
                        pyai_call_id=pyai_id,
                        filename=source_name,
                    )
                except sqlite3.IntegrityError:
                    existing = transcribe.find_existing_call(conn, identity)
                    if not existing:
                        raise
                    call_id = existing[0]
                    transcribe.set_filename_if_empty(conn, call_id, source_name)
                    return call_id, True
            finally:
                conn.close()
        applog.event(
            log, "transcription_success",
            call_id=call_id,
            pyai_call_id=pyai_id,
            job_id=job_id,
            segments=len(result.get("segments") or []),
            size_bytes=size,
            deduped=False,
            filename=source_name,
        )
        log.info(
            "transcription complete -> new call %d (filename=%s, pyai_call_id=%s)",
            call_id, source_name, pyai_id,
        )
        return call_id, False
    finally:
        if os.path.exists(hear_tmp):
            os.remove(hear_tmp)


def _audio_media_type(path: str) -> str:
    """Playback files may be original MP3s or 8 kHz Hear WAVs from bulk import."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return "audio/mpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    return "audio/mpeg"


def _store_playback(src_path: str, call_id: int):
    dest = os.path.join(AUDIO_DIR, f"{call_id}.mp3")
    os.makedirs(AUDIO_DIR, exist_ok=True)
    if os.path.abspath(src_path) != os.path.abspath(dest):
        shutil.copy2(src_path, dest)


def _upload_error_status(msg: str) -> HTTPException:
    if "daily_cap_exceeded" in msg:
        return HTTPException(
            status_code=429,
            detail="Daily transcription cap reached (resets 00:00 UTC). "
                   "Try a fresh key or later.",
        )
    if "transcribe:jobs" in msg or "speaker-labelled" in msg:
        return HTTPException(status_code=403, detail=msg)
    return HTTPException(status_code=502, detail=f"Transcription failed: {msg}")


def _safe_zip_base_name(filename: str) -> str | None:
    raw = (filename or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("..") or "/../" in f"/{raw}/":
        return None
    base = transcribe.sanitize_filename(os.path.basename(raw))
    ext = os.path.splitext(base)[1].lower()
    if ext == ".zip" or ext not in AUDIO_EXTS:
        return None
    return base


def _extract_batch_zip(zip_path: str, batch_dir: str) -> list[dict]:
    """Extract audio members into batch_dir. Raises HTTPException on bad zip."""
    extracted = []
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="The upload was not a valid zip file.")

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise HTTPException(status_code=400, detail="The zip did not contain any files.")
        if len(infos) > MAX_BULK_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"Zip has too many files (max {MAX_BULK_FILES}).",
            )
        total_uncompressed = 0
        batch_abs = os.path.abspath(batch_dir) + os.sep
        for i, info in enumerate(infos):
            name = _safe_zip_base_name(info.filename)
            if not name:
                raise HTTPException(
                    status_code=400,
                    detail=f"Zip member is not an allowed audio file: {os.path.basename(info.filename)}",
                )
            if info.file_size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_BATCH_ZIP_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Uncompressed zip contents exceed the batch size limit.",
                )
            display = name
            if len(name) > 3 and name[0:2].isdigit() and name[2] == "_":
                display = name[3:] or name
            dest = os.path.join(batch_dir, f"{i:02d}_{name}")
            dest_abs = os.path.abspath(dest)
            if not dest_abs.startswith(batch_abs):
                raise HTTPException(status_code=400, detail="Invalid zip member path.")
            copied = 0
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"{name} exceeded the per-file size limit while extracting.",
                        )
                    out.write(chunk)
            extracted.append({"path": dest, "filename": display, "index": i})
    return extracted


@app.get("/api/calls/{call_id}/audio")
def get_audio(call_id: int):
    path = os.path.join(AUDIO_DIR, f"{call_id}.mp3")
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"No audio at {path}. Copy the call's file there as {call_id}.mp3.",
        )
    return FileResponse(path, media_type=_audio_media_type(path))


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    size = len(data)
    size_mb = size / (1024 * 1024)
    applog.event(
        log, "upload_received",
        filename=file.filename or "unknown",
        size_bytes=size,
        size_mb=round(size_mb, 3),
    )
    log.info("upload received: %s (%.2f MB)", file.filename, size_mb)
    if size > MAX_UPLOAD_BYTES:
        applog.event(
            log, "upload_rejected", level=logging.ERROR,
            filename=file.filename or "unknown",
            size_bytes=size,
            size_mb=round(size_mb, 3),
            error="file_too_large",
        )
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large for transcription ({size_mb:.1f} MB). "
                f"Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )

    os.makedirs(AUDIO_DIR, exist_ok=True)
    tmp = os.path.join(AUDIO_DIR, f"_upload_{uuid.uuid4().hex}")
    with open(tmp, "wb") as f:
        f.write(data)

    source_name = transcribe.sanitize_filename(file.filename)

    try:
        call_id, _deduped = _ingest_audio_file(tmp, source_name)
        os.replace(tmp, os.path.join(AUDIO_DIR, f"{call_id}.mp3"))
    except HTTPException:
        raise
    except (Exception, SystemExit) as e:
        msg = str(e)
        applog.event(
            log, "transcription_failure", level=logging.ERROR,
            filename=file.filename or "unknown",
            size_bytes=size,
            error=msg,
        )
        log.error("upload/transcription failed: %s", msg)
        raise _upload_error_status(msg)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return {"call_id": call_id, "filename": _call_filename(call_id)}


@app.post("/api/upload-batch")
def upload_batch(file: UploadFile = File(...)):
    """
    One zip of up to MAX_BULK_FILES audio files. Extract to unique paths,
    transcribe all on PyAI in parallel, then run Claude QA in parallel.
    """
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded zip was empty.")
    if len(data) > MAX_BATCH_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Zip is too large. Maximum is {MAX_BATCH_ZIP_BYTES // (1024 * 1024)} MB.",
        )

    batch_id = uuid.uuid4().hex
    batch_dir = os.path.join(AUDIO_DIR, "batches", batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    zip_path = os.path.join(batch_dir, "batch.zip")
    with open(zip_path, "wb") as f:
        f.write(data)

    started = time.perf_counter()
    try:
        extracted = _extract_batch_zip(zip_path, batch_dir)
        applog.event(
            log, "batch_received",
            count=len(extracted),
            zip_bytes=len(data),
            batch_id=batch_id,
        )
        log.info("batch %s: %d file(s), parallel transcribe then parallel QA", batch_id, len(extracted))

        ingest_rows = [None] * len(extracted)

        def ingest_one(item):
            try:
                call_id, deduped = _ingest_audio_file(item["path"], item["filename"])
                _store_playback(item["path"], call_id)
                return {
                    "index": item["index"],
                    "filename": item["filename"],
                    "call_id": call_id,
                    "deduped": deduped,
                    "error": None,
                }
            except (Exception, SystemExit) as e:  # noqa: BLE001
                msg = str(e)
                applog.event(
                    log, "transcription_failure", level=logging.ERROR,
                    filename=item["filename"],
                    error=msg,
                )
                return {
                    "index": item["index"],
                    "filename": item["filename"],
                    "call_id": None,
                    "deduped": False,
                    "error": msg,
                }

        workers = min(MAX_BULK_WORKERS, max(1, len(extracted)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(ingest_one, item) for item in extracted]
            for fut in as_completed(futs):
                row = fut.result()
                ingest_rows[row["index"]] = row

        to_audit = [r for r in ingest_rows if r and r.get("call_id") and not r.get("error")]

        def audit_one(row):
            try:
                audit, _rh = _load_or_compute_audit(row["call_id"], refresh=False)
                return {
                    **row,
                    "status": "ok",
                    "score": audit.get("score"),
                    "grade": audit.get("grade"),
                    "flagged": bool(audit.get("flagged")),
                }
            except (Exception, SystemExit) as e:  # noqa: BLE001
                return {
                    **row,
                    "status": "error",
                    "error": f"Transcribed but audit failed: {e}",
                    "score": None,
                    "grade": None,
                    "flagged": False,
                }

        audited = {}
        if to_audit:
            with ThreadPoolExecutor(max_workers=min(MAX_BULK_WORKERS, len(to_audit))) as pool:
                futs = [pool.submit(audit_one, row) for row in to_audit]
                for fut in as_completed(futs):
                    row = fut.result()
                    audited[row["index"]] = row

        calls = []
        for row in ingest_rows:
            if not row:
                continue
            if row.get("error") and not row.get("call_id"):
                calls.append({
                    "filename": row["filename"],
                    "status": "error",
                    "error": row["error"],
                    "call_id": None,
                    "score": None,
                    "grade": None,
                    "flagged": False,
                    "deduped": False,
                })
            elif row["index"] in audited:
                out = audited[row["index"]]
                calls.append({
                    "filename": out["filename"],
                    "status": out.get("status") or "ok",
                    "error": out.get("error"),
                    "call_id": out.get("call_id"),
                    "score": out.get("score"),
                    "grade": out.get("grade"),
                    "flagged": bool(out.get("flagged")),
                    "deduped": bool(out.get("deduped")),
                })
            else:
                calls.append({
                    "filename": row["filename"],
                    "status": "ok",
                    "error": None,
                    "call_id": row.get("call_id"),
                    "score": None,
                    "grade": None,
                    "flagged": False,
                    "deduped": bool(row.get("deduped")),
                })

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        applog.event(
            log, "batch_completed",
            batch_id=batch_id,
            count=len(calls),
            duration_ms=duration_ms,
        )
        log.info("batch %s done in %.0f ms (%d call(s))", batch_id, duration_ms, len(calls))
        return {"count": len(calls), "calls": calls}
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)
