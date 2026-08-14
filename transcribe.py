"""
CallProof - transcript spine (with logging).

Submit a local audio file (or public URL) to PyAI Hear, poll until done, and
save a speaker-labelled, timestamped transcript to SQLite. Each source is
transcribed once (cached by content hash). On a failed job, PyAI's actual error
is logged and raised - no more silent failures.
"""

from __future__ import annotations

import os
import sys
import re
import time
import json
import logging
import sqlite3
import hashlib
import uuid
import shutil
import subprocess

import httpx
from dotenv import load_dotenv

import applog
import pyai_usage

load_dotenv()
applog.setup_logging()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.transcribe")

PYAI_API_KEY = (os.getenv("PYAI_API_KEY") or "").strip() or None
BASE_URL = "https://api.pyai.com"
DB_PATH = "callproof.db"
RECAP_PACK_ID = os.getenv("RECAP_PACK_ID") or None

AUDIO_SOURCE = "/Users/mohammed.kashif/Downloads/test1.mp3"   # only used by the CLI main()
SEPARATION_MODE = "channel"    # "diarize" (mono or stereo) | "channel" (true dual-channel)
MODEL = "pyai-hear-telephony"

POLL_INTERVAL_SECONDS = 2
POLL_MAX_ATTEMPTS = 60
HEAR_JOB_RETRIES = 3
_TRANSIENT_HEAR = re.compile(
    r"HTTP 502|HTTP 503|HTTP 504|no healthy upstream|upstream request timeout|"
    r"upstream connect error|remote connection failure",
    re.I,
)

# Telephony-sized copy for PyAI Hear only. Playback still uses the original file.
# Must stay discrete-channel PCM (not MP3). Joint-stereo MP3 mixes L/R, and
# Hear channel=true then returns no speaker labels.
HEAR_SAMPLE_RATE = 8000
HEAR_CHANNELS = 2
HEAR_FFMPEG_TIMEOUT = 60

# Populated at import; may be refreshed by set_api_key() after sandbox mint.
HEADERS = {"Authorization": f"Bearer {PYAI_API_KEY}"} if PYAI_API_KEY else {}


def set_api_key(api_key: str):
    """Inject / rotate the PyAI key at runtime (used by api.py sandbox mint)."""
    global PYAI_API_KEY, HEADERS
    key = (api_key or "").strip()
    if not key:
        raise ValueError("PYAI_API_KEY cannot be empty")
    PYAI_API_KEY = key
    HEADERS = {"Authorization": f"Bearer {key}"}
    os.environ["PYAI_API_KEY"] = key


def _require_api_key():
    if not PYAI_API_KEY:
        raise RuntimeError(
            "PYAI_API_KEY not configured. Add it to .env, or start the API so it "
            "can mint a sandbox key (sandbox keys cannot diarize — use a live key "
            "with transcribe:jobs for CallProof)."
        )


def is_url(src):
    return src.startswith("http://") or src.startswith("https://")


def _ffmpeg_bin():
    """System ffmpeg, FFMPEG_PATH, or imageio-ffmpeg's bundled binary."""
    env = (os.getenv("FFMPEG_PATH") or "").strip()
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    return None


def _run_ffmpeg(ffmpeg, src_path, dest_path):
    # 8 kHz stereo PCM keeps agent/customer on separate channels.
    # Do not encode MP3/AAC: joint stereo bleeds L/R and Hear drops speakers.
    return subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-i", src_path,
            "-map", "0:a:0",
            "-ac", str(HEAR_CHANNELS),
            "-ar", str(HEAR_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-f", "wav",
            dest_path,
        ],
        capture_output=True,
        text=True,
        timeout=HEAR_FFMPEG_TIMEOUT,
        check=False,
    )


def is_hear_wav(path):
    """True if path is already an 8 kHz stereo PCM WAV (browser bulk Hear copy)."""
    try:
        with open(path, "rb") as f:
            head = f.read(44)
    except OSError:
        return False
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    fmt_at = head.find(b"fmt ")
    if fmt_at < 0 or fmt_at + 16 > len(head):
        return False
    audio_format = int.from_bytes(head[fmt_at + 8:fmt_at + 10], "little")
    channels = int.from_bytes(head[fmt_at + 10:fmt_at + 12], "little")
    rate = int.from_bytes(head[fmt_at + 12:fmt_at + 16], "little")
    return audio_format == 1 and channels == HEAR_CHANNELS and rate == HEAR_SAMPLE_RATE


def make_hear_copy(src_path, dest_path):
    """
    Encode a smaller 8 kHz stereo PCM WAV for PyAI Hear (channel mode).

    Returns dest_path if the copy exists and is strictly smaller than src.
    Returns None if ffmpeg is missing, transcode fails, or there is no size
    win (typical for recordings that are already 8 kHz stereo WAV). Never
    modifies src_path — playback should keep the original.
    """
    if not src_path or not os.path.isfile(src_path):
        return None
    original_bytes = os.path.getsize(src_path)
    if original_bytes <= 0:
        return None

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        applog.event(
            log, "transcode_skipped",
            reason="ffmpeg_missing",
            original_bytes=original_bytes,
        )
        log.info(
            "Hear transcode skipped (no ffmpeg). Install ffmpeg or "
            "pip install imageio-ffmpeg. Uploading the original file."
        )
        return None

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.exists(dest_path):
        os.remove(dest_path)

    started = time.perf_counter()
    last_err = None
    try:
        proc = _run_ffmpeg(ffmpeg, src_path, dest_path)
        if proc.returncode == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            last_err = None
        else:
            err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:200]
            last_err = err or f"ffmpeg exit {proc.returncode}"
            if os.path.exists(dest_path):
                os.remove(dest_path)
    except (OSError, subprocess.TimeoutExpired) as e:
        last_err = f"{type(e).__name__}: {e}"
        log.warning("Hear transcode failed: %s", last_err)
        if os.path.exists(dest_path):
            os.remove(dest_path)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if last_err or not os.path.isfile(dest_path):
        applog.event(
            log, "transcode_skipped",
            reason="ffmpeg_failed",
            original_bytes=original_bytes,
            duration_ms=duration_ms,
            error=last_err or "no_output",
        )
        log.warning(
            "Hear transcode failed (%s); uploading the original file",
            last_err or "no_output",
        )
        return None

    hear_bytes = os.path.getsize(dest_path)
    if hear_bytes >= original_bytes:
        os.remove(dest_path)
        applog.event(
            log, "transcode_skipped",
            reason="no_savings",
            original_bytes=original_bytes,
            hear_bytes=hear_bytes,
            duration_ms=duration_ms,
        )
        log.info(
            "Hear copy not smaller (%d -> %d bytes); "
            "uploading original to preserve L/R channels",
            original_bytes, hear_bytes,
        )
        return None

    applog.event(
        log, "transcode_success",
        original_bytes=original_bytes,
        hear_bytes=hear_bytes,
        duration_ms=duration_ms,
        saved_bytes=original_bytes - hear_bytes,
    )
    log.info(
        "Hear copy %d -> %d bytes (%.1f%% smaller) in %.0f ms (8 kHz stereo PCM)",
        original_bytes,
        hear_bytes,
        (1 - hear_bytes / original_bytes) * 100,
        duration_ms,
    )
    return dest_path


# ---------- Database ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, audio_url TEXT UNIQUE NOT NULL,
        job_id TEXT, status TEXT, full_text TEXT, speakers INTEGER,
        audio_seconds REAL, raw_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER NOT NULL,
        seq INTEGER, speaker TEXT, channel INTEGER, start REAL, end REAL, text TEXT)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(calls)").fetchall()]
    if "pyai_call_id" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN pyai_call_id TEXT")
    if "filename" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN filename TEXT")
    conn.commit()
    return conn


def sanitize_filename(name: str | None, fallback: str = "recording.mp3") -> str:
    """Keep a safe display name from an upload (basename only, no path tricks)."""
    raw = (name or "").strip() or fallback
    base = os.path.basename(raw.replace("\\", "/"))
    base = base.strip().lstrip(".")
    if not base:
        base = fallback
    # Cap length; keep extension if present
    if len(base) > 180:
        root, ext = os.path.splitext(base)
        base = root[: 180 - len(ext)] + ext
    return base


def new_pyai_call_id():
    return f"callproof_{uuid.uuid4().hex[:12]}"


def find_existing_call(conn, identity):
    return conn.execute(
        "SELECT id, pyai_call_id FROM calls WHERE audio_url = ? AND status = 'completed'",
        (identity,)).fetchone()


def save_transcript(conn, identity, job_id, result, pyai_call_id=None, filename=None):
    segments = result.get("segments") or []
    safe_name = sanitize_filename(filename) if filename else None
    cur = conn.execute(
        """INSERT INTO calls (audio_url, job_id, status, full_text, speakers, audio_seconds,
           raw_json, pyai_call_id, filename)
           VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?)""",
        (identity, job_id, result.get("text", ""), result.get("speakers"),
         result.get("audio_seconds"), json.dumps(result), pyai_call_id, safe_name))
    call_id = cur.lastrowid
    for i, seg in enumerate(segments):
        conn.execute(
            """INSERT INTO segments (call_id, seq, speaker, channel, start, end, text)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (call_id, i, seg.get("speaker"), seg.get("channel"),
             seg.get("start"), seg.get("end"), seg.get("text")))
    conn.commit()
    log.info(
        "saved transcript for call %d (%d segments, pyai_call_id=%s, filename=%s)",
        call_id, len(segments), pyai_call_id, safe_name,
    )
    return call_id


def set_filename_if_empty(conn, call_id, filename):
    """Backfill filename on deduped uploads when the row has no name yet."""
    if not filename:
        return
    safe = sanitize_filename(filename)
    conn.execute(
        """UPDATE calls SET filename=?
           WHERE id=? AND (filename IS NULL OR TRIM(filename) = '')""",
        (safe, call_id),
    )
    conn.commit()


# ---------- PyAI service wrapper ----------
def submit_job_url(audio_url, call_id=None):
    _require_api_key()
    body = {"audio_url": audio_url, "model": MODEL, "numerals": True, "output_formats": ["json"]}
    body.update({"channel": True} if SEPARATION_MODE == "channel" else {"diarize": True})
    if call_id:
        body["call_id"] = call_id
        if RECAP_PACK_ID:
            body["pack_id"] = RECAP_PACK_ID
    idem = hashlib.sha256(audio_url.encode()).hexdigest()[:32]
    resp = pyai_usage.post(f"{BASE_URL}/v1/transcription/jobs", json=body,
                      headers={**HEADERS, "Idempotency-Key": idem}, timeout=60)
    return _job_id_from(resp)


# In-memory store for sync fallback results — keyed by synthetic job_id.
# Only populated when the async path is unavailable (sandbox keys).
_SYNC_RESULTS: dict = {}

_SYNC_SCOPE_HELP = (
    "This API key cannot use async Hear jobs (transcribe:jobs). "
    "CallProof needs speaker-labelled segments from POST /v1/transcription/jobs "
    "(diarize/channel). Sandbox keys only include hear:transcribe (text-only sync). "
    "Add a live PYAI_API_KEY with transcribe:jobs to .env and restart."
)


def submit_job_file(path, call_id=None):
    """
    Submit audio for transcription.

    Tries the async jobs endpoint first (transcribe:jobs scope).
    If the key lacks that scope (403) — typical for sandbox keys —
    attempts the sync endpoint and only accepts a diarized segment payload
    compatible with the rest of CallProof. Text-only sync responses fail
    with a clear error instead of saving an empty transcript.
    """
    _require_api_key()
    with open(path, "rb") as f:
        audio_bytes = f.read()

    files = {"audio": (os.path.basename(path), audio_bytes, "application/octet-stream")}
    data = {"model": MODEL, "numerals": "true", "output_formats": "json"}
    data.update({"channel": "true"} if SEPARATION_MODE == "channel" else {"diarize": "true"})
    if call_id:
        data["call_id"] = call_id
        if RECAP_PACK_ID:
            data["pack_id"] = RECAP_PACK_ID

    log.info("submitting %.2f MB to PyAI Hear (%s mode, call_id=%s)",
             len(audio_bytes) / 1_000_000, SEPARATION_MODE, call_id)

    resp = pyai_usage.post(
        f"{BASE_URL}/v1/transcription/jobs",
        files=files, data=data, headers=HEADERS, timeout=120,
    )

    if resp.status_code == 403:
        log.warning(
            "async jobs returned 403 (missing transcribe:jobs) — "
            "trying sync endpoint for a diarized payload"
        )
        return _submit_sync_fallback(audio_bytes, path, data)

    return _job_id_from(resp)


def _normalize_sync_result(result: dict) -> dict:
    """Map sync/Whisper-shaped payloads toward the async jobs result shape."""
    out = dict(result)
    if not out.get("segments") and out.get("words"):
        out["segments"] = out.get("words") or []
    return out


def _has_usable_segments(result: dict) -> bool:
    segments = result.get("segments") or []
    if not segments:
        return False
    # Need at least one timed utterance with text; speaker preferred for QA.
    usable = 0
    speakers = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        usable += 1
        if seg.get("speaker") is not None:
            speakers.add(seg.get("speaker"))
    return usable > 0 and len(speakers) >= 1


def _submit_sync_fallback(audio_bytes, path, data):
    """
    Sync fallback: POST /v1/audio/transcriptions (hear:transcribe scope).
    Returns a synthetic job_id only when the response includes speaker segments.
    """
    files = {"file": (os.path.basename(path), audio_bytes, "application/octet-stream")}
    sync_data = {
        "model": data.get("model", MODEL),
        "response_format": "verbose_json",
    }
    # Best-effort: some deployments accept these; ignored if unsupported.
    if "diarize" in data:
        sync_data["diarize"] = data["diarize"]
    if "channel" in data:
        sync_data["channel"] = data["channel"]
    if "numerals" in data:
        sync_data["numerals"] = data["numerals"]

    log.info("sync fallback: POST /v1/audio/transcriptions")
    resp = pyai_usage.post(
        f"{BASE_URL}/v1/audio/transcriptions",
        files=files, data=sync_data, headers=HEADERS, timeout=180,
    )

    if resp.status_code != 200:
        log.error("sync fallback rejected: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(
            f"PyAI sync transcription failed: {resp.status_code} {resp.text}. "
            f"{_SYNC_SCOPE_HELP}"
        )

    result = _normalize_sync_result(resp.json())
    if not _has_usable_segments(result):
        log.error(
            "sync fallback returned no speaker-labelled segments "
            "(keys=%s). CallProof cannot audit text-only transcripts.",
            sorted(result.keys()),
        )
        raise RuntimeError(_SYNC_SCOPE_HELP)

    call_id = data.get("call_id", "unknown")
    job_id = f"sync:{call_id}"
    _SYNC_RESULTS[job_id] = result
    log.info(
        "sync transcription usable (%d segments); synthetic job_id=%s",
        len(result.get("segments") or []), job_id,
    )
    return job_id


def _job_id_from(resp):
    if resp.status_code not in (200, 202):
        log.error("job submission rejected: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"PyAI rejected the job: {resp.status_code} {resp.text}")
    job_id = resp.json().get("job_id")
    if not job_id:
        raise RuntimeError(f"No job_id in PyAI response: {resp.text}")
    log.info("job submitted: %s", job_id)
    return job_id


def poll_job(job_id):
    _require_api_key()
    # Sync fallback path — result already in memory, return immediately
    if job_id.startswith("sync:"):
        result = _SYNC_RESULTS.pop(job_id, None)
        if result is None:
            raise RuntimeError(f"Sync result for {job_id} not found (already consumed?)")
        log.info("sync job %s: returning inline result (no polling needed)", job_id)
        return result

    last_status = None
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        resp = pyai_usage.get(
            f"{BASE_URL}/v1/transcription/jobs/{job_id}", headers=HEADERS, timeout=30
        )
        if resp.status_code != 200:
            log.error("poll error: %s %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"PyAI poll error: {resp.status_code} {resp.text}")
        data = resp.json()
        status = data.get("status")
        if status != last_status:
            log.info("job %s status: %s (attempt %d)", job_id, status, attempt)
            last_status = status
        if status == "completed":
            return get_result(data)
        if status in ("failed", "cancelled"):
            reason = data.get("error") or data
            log.error("job %s %s: %s", job_id, status, reason)
            raise RuntimeError(f"PyAI job {status}: {reason}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"PyAI job {job_id} did not finish within "
        f"{POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
    )


def is_transient_hear_error(exc: BaseException) -> bool:
    return bool(_TRANSIENT_HEAR.search(str(exc)))


def run_hear_job(path, call_id=None, retries=HEAR_JOB_RETRIES):
    """Submit + poll. Resubmit on PyAI STT 502/503/504 upstream failures."""
    last_err: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            job_id = submit_job_file(path, call_id=call_id)
            result = poll_job(job_id)
            return job_id, result
        except (RuntimeError, httpx.HTTPError) as e:
            last_err = e
            if attempt >= retries or not is_transient_hear_error(e):
                raise
            wait = min(8, 2 * attempt)
            applog.event(
                log, "transcription_retry",
                attempt=attempt,
                retries=retries,
                wait_s=wait,
                error=str(e)[:300],
            )
            log.warning(
                "transient Hear error on attempt %d/%d: %s; retrying in %ds",
                attempt, retries, e, wait,
            )
            time.sleep(wait)
    raise last_err  # pragma: no cover


def get_result(job_data):
    if job_data.get("result"):
        return job_data["result"]
    if job_data.get("result_url"):
        r = pyai_usage.get(job_data["result_url"], timeout=30)
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Completed job has no result or result_url: {job_data}")


# ---------- Orchestrator (CLI) ----------
def identity_for(src):
    if is_url(src):
        return src
    with open(src, "rb") as f:
        return "file-sha256:" + hashlib.sha256(f.read()).hexdigest()[:24]


def main():
    if not PYAI_API_KEY:
        sys.exit("ERROR: PYAI_API_KEY not found. Is .env in this folder?")
    src = AUDIO_SOURCE
    conn = init_db()
    if not is_url(src) and not os.path.isfile(src):
        sys.exit(f"ERROR: file not found: {src}")
    identity = identity_for(src)
    existing = find_existing_call(conn, identity)
    if existing:
        log.info("already transcribed (call id %d) - loading from DB, no API call", existing[0])
        return
    pyai_id = new_pyai_call_id()
    hear_tmp = None
    try:
        if is_url(src):
            job_id = submit_job_url(src, call_id=pyai_id)
            result = poll_job(job_id)
        else:
            hear_tmp = src + ".hear-tmp.wav"
            upload_path = make_hear_copy(src, hear_tmp) or src
            job_id, result = run_hear_job(upload_path, call_id=pyai_id)
        call_id = save_transcript(
            conn, identity, job_id, result,
            pyai_call_id=pyai_id,
            filename=None if is_url(src) else os.path.basename(src),
        )
        log.info("done: call id %d", call_id)
    finally:
        if hear_tmp and os.path.exists(hear_tmp):
            os.remove(hear_tmp)


if __name__ == "__main__":
    main()