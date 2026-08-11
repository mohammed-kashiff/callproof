"""
CallProof - transcript spine (PocketBase-backed, with logging).

Submit a local audio file (or public URL) to PyAI Hear, poll until done, and
save a speaker-labelled, timestamped transcript via PocketBase. Each source is
transcribed once (cached by content hash). On a failed job, PyAI's actual error
is logged and raised - no more silent failures.
"""

import os
import sys
import time
import logging
import hashlib

import httpx
from dotenv import load_dotenv

import db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.transcribe")

PYAI_API_KEY = os.getenv("PYAI_API_KEY")
BASE_URL = "https://api.pyai.com"

AUDIO_SOURCE = "/Users/mohammed.kashif/Downloads/test1.mp3"   # only used by the CLI main()
SEPARATION_MODE = "diarize"    # "diarize" (mono or stereo) | "channel" (true dual-channel)
MODEL = "pyai-hear-telephony"

POLL_INTERVAL_SECONDS = 2
POLL_MAX_ATTEMPTS = 60


def _headers():
    if not PYAI_API_KEY:
        raise RuntimeError("PYAI_API_KEY not found. Is .env in this folder?")
    return {"Authorization": f"Bearer {PYAI_API_KEY}"}


def is_url(src):
    return src.startswith("http://") or src.startswith("https://")


# ---------- Database (compat wrappers) ----------
def init_db():
    db.init_db()
    return db


def find_existing_call(_conn, identity):
    """Returns (call_id,) tuple or None — matches old sqlite Row shape for callers."""
    call_id = db.find_existing_call(identity)
    return (call_id,) if call_id else None


def save_transcript(_conn, identity, job_id, result):
    return db.save_transcript(identity, job_id, result)


# ---------- PyAI service wrapper ----------
def submit_job_url(audio_url):
    body = {"audio_url": audio_url, "model": MODEL, "numerals": True, "output_formats": ["json"]}
    body.update({"channel": True} if SEPARATION_MODE == "channel" else {"diarize": True})
    idem = hashlib.sha256(audio_url.encode()).hexdigest()[:32]
    resp = httpx.post(f"{BASE_URL}/v1/transcription/jobs", json=body,
                      headers={**_headers(), "Idempotency-Key": idem}, timeout=60)
    return _job_id_from(resp)


def submit_job_file(path):
    with open(path, "rb") as f:
        audio_bytes = f.read()
    files = {"audio": (os.path.basename(path), audio_bytes, "application/octet-stream")}
    data = {"model": MODEL, "numerals": "true", "output_formats": "json"}
    data.update({"channel": "true"} if SEPARATION_MODE == "channel" else {"diarize": "true"})
    log.info("submitting %.2f MB to PyAI Hear (%s mode)", len(audio_bytes) / 1_000_000, SEPARATION_MODE)
    resp = httpx.post(f"{BASE_URL}/v1/transcription/jobs",
                      files=files, data=data, headers=_headers(), timeout=120)
    return _job_id_from(resp)


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
    last_status = None
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        resp = httpx.get(f"{BASE_URL}/v1/transcription/jobs/{job_id}", headers=_headers(), timeout=30)
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
    raise RuntimeError(f"PyAI job {job_id} did not finish within "
                       f"{POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s")


def get_result(job_data):
    if job_data.get("result"):
        return job_data["result"]
    if job_data.get("result_url"):
        r = httpx.get(job_data["result_url"], timeout=30)
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
    src = AUDIO_SOURCE
    db.init_db()
    if not is_url(src) and not os.path.isfile(src):
        sys.exit(f"ERROR: file not found: {src}")
    identity = identity_for(src)
    existing = db.find_existing_call(identity)
    if existing:
        log.info("already transcribed (call id %s) - loading from DB, no API call", existing)
        return
    job_id = submit_job_url(src) if is_url(src) else submit_job_file(src)
    result = poll_job(job_id)
    call_id = db.save_transcript(identity, job_id, result)
    log.info("done: call id %s", call_id)


if __name__ == "__main__":
    main()
