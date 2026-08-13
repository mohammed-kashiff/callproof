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

import os
import json
import hashlib
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import qa_v8

import applog

load_dotenv()
applog.setup_logging()

import qa_engine as qa
import transcribe
import recap as pyai_recap
import email_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.api")

DB_PATH = qa.DB_PATH
AUDIO_DIR = "audio"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

PYAI_SANDBOX_MINT_URL = "https://api.pyai.com/v1/sandbox/keys"
ENV_FILE = ".env"

app = FastAPI(title="CallProof API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
    c = sqlite3.connect(DB_PATH)
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
            "   ➤ The QA engine and coaching tips need this.\n"
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
    os.makedirs(AUDIO_DIR, exist_ok=True)
    log.info("startup complete; db=%s", DB_PATH)


_startup()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rubric_hash():
    with open(qa.RUBRIC_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


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

    is_v8 = qa_v8.is_v8_rubric(rubric)
    if is_v8:
        n_items = len(qa_v8.list_dimensions(rubric))
        log.info(
            "computing audit for call %d (%d v8 dimensions) — parallel Claude wave",
            call_id, n_items,
        )
        criteria_arg = []
    else:
        n_items = len(rubric["criteria"])
        log.info(
            "computing audit for call %d (%d criteria) — parallel Claude wave",
            call_id, n_items,
        )
        criteria_arg = rubric["criteria"]

    # One parallel wave: dimensions/criteria + churn + feedback + Recap.
    # Retention email + coaching tips are on-demand (button / compose).
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
    # On-demand — drafted when Email stakeholder / Get tips is used
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
    )

    return {
        "call_id": call_id, "audio_seconds": meta.get("audio_seconds"),
        "agent_speaker": agent, "rubric": rubric["name"],
        "rubric_id": rubric.get("rubric_id") or rubric.get("name"),
        "score": score, "grade": grade, "tally": tally,
        "gate_fails": gate_fails, "flagged": flagged,
        "manager_review": manager_review,
        "segments": segments, "findings": findings,
        "coaching": [],
        "churn": churn, "feedback": feedback,
        "retention_email": None, "recap": call_recap,
    }


def _load_or_compute_audit(call_id: int, refresh: bool = False):
    """Return (audit_dict, rubric_hash). Computes and caches on miss/refresh."""
    rh = _rubric_hash()
    if not refresh:
        with _conn() as c:
            row = c.execute(
                "SELECT audit_json, rubric_hash FROM audits WHERE call_id=?",
                (call_id,),
            ).fetchone()
        if row and row["rubric_hash"] == rh:
            return json.loads(row["audit_json"]), rh
    audit = analyze_call(call_id)
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO audits (call_id, audit_json, rubric_hash) "
            "VALUES (?, ?, ?)",
            (call_id, json.dumps(audit), rh),
        )
    return audit, rh


def _weak_from_findings(findings):
    weak = []
    for f in findings or []:
        if f.get("verdict") not in ("fail", "partial", "unverified"):
            continue
        weak.append((
            {"name": f.get("name", "Criterion")},
            {
                "verdict": f["verdict"],
                "reasoning": f.get("reasoning", ""),
                "evidence_text": f.get("evidence_text"),
            },
        ))
    return weak


# ── Routes ────────────────────────────────────────────────────────────────────
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
        item = {
            "id": r["id"],
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
            "audited_at": r["audited_at"],
        }
        if r["audit_json"]:
            try:
                cached = json.loads(r["audit_json"])
                item["has_audit"] = True
                item["audit_fresh"] = r["rubric_hash"] == rh
                item["score"] = cached.get("score")
                item["grade"] = cached.get("grade")
            except (TypeError, json.JSONDecodeError):
                item["has_audit"] = True
        out.append(item)
    return out


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
            return cached
        applog.event(log, "audit_cache", result="MISS", call_id=call_id)
        log.info("cache MISS  call %d - computing fresh audit", call_id)
    else:
        applog.event(log, "audit_cache", result="BYPASS", call_id=call_id)
        log.info("cache BYPASS (refresh) call %d - computing fresh audit", call_id)
    audit, _rh = _load_or_compute_audit(call_id, refresh=True)
    log.info("cached audit for call %d (score %s)", call_id, audit["score"])
    return audit


def _save_audit(call_id: int, audit: dict, rh: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO audits (call_id, audit_json, rubric_hash) "
            "VALUES (?, ?, ?)",
            (call_id, json.dumps(audit), rh),
        )


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


@app.post("/api/calls/{call_id}/coaching")
def post_coaching(call_id: int):
    """On-demand coaching tips (one Claude call) — not part of the audit hot path."""
    audit, rh = _load_or_compute_audit(call_id, refresh=False)
    weak = _weak_from_findings(audit.get("findings"))
    if not weak:
        log.info("coaching skipped for call %d — no weak findings", call_id)
        audit["coaching"] = []
        _save_audit(call_id, audit, rh)
        return {"call_id": call_id, "coaching": []}

    log.info("generating on-demand coaching for call %d (%d weak areas)", call_id, len(weak))
    coaching = qa.generate_coaching(weak)
    audit["coaching"] = coaching
    _save_audit(call_id, audit, rh)
    return {"call_id": call_id, "coaching": coaching}


@app.post("/api/calls/{call_id}/stakeholder-email/compose")
def post_stakeholder_email_compose(call_id: int):
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


@app.get("/api/calls/{call_id}/audio")
def get_audio(call_id: int):
    path = os.path.join(AUDIO_DIR, f"{call_id}.mp3")
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"No audio at {path}. Copy the call's file there as {call_id}.mp3.",
        )
    return FileResponse(path, media_type="audio/mpeg")


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
    tmp = os.path.join(AUDIO_DIR, "_upload_tmp")
    with open(tmp, "wb") as f:
        f.write(data)

    try:
        identity = transcribe.identity_for(tmp)
        conn = sqlite3.connect(DB_PATH)
        existing = transcribe.find_existing_call(conn, identity)
        if existing:
            call_id = existing[0]
            applog.event(
                log, "transcription_success",
                call_id=call_id, deduped=True, size_bytes=size,
            )
            log.info(
                "upload deduped to existing call %d (no re-transcription)", call_id
            )
        else:
            pyai_id = transcribe.new_pyai_call_id()
            job_id = transcribe.submit_job_file(tmp, call_id=pyai_id)
            result = transcribe.poll_job(job_id)
            call_id = transcribe.save_transcript(
                conn, identity, job_id, result, pyai_call_id=pyai_id
            )
            applog.event(
                log, "transcription_success",
                call_id=call_id,
                pyai_call_id=pyai_id,
                job_id=job_id,
                segments=len(result.get("segments") or []),
                size_bytes=size,
                deduped=False,
            )
            log.info(
                "transcription complete -> new call %d (pyai_call_id=%s)",
                call_id, pyai_id,
            )
        conn.close()
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
        if "daily_cap_exceeded" in msg:
            raise HTTPException(
                status_code=429,
                detail="Daily transcription cap reached (resets 00:00 UTC). "
                       "Try a fresh key or later.",
            )
        if "transcribe:jobs" in msg or "speaker-labelled" in msg:
            raise HTTPException(status_code=403, detail=msg)
        raise HTTPException(status_code=502, detail=f"Transcription failed: {msg}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return {"call_id": call_id}
