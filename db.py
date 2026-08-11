"""
CallProof - PocketBase data access.

All persistence goes through the PocketBase REST API (superuser auth).
Collection API rules are locked (null); only this backend talks to PB.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("callproof.db")

POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090").rstrip("/")

_token: str | None = None
_token_expires_at: float = 0.0


class PocketBaseError(RuntimeError):
    pass


def _pb_config():
    """Re-read .env on each auth so password rotations take effect without restart."""
    load_dotenv(override=True)
    url = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090").rstrip("/")
    email = os.getenv("POCKETBASE_ADMIN_EMAIL", "")
    password = os.getenv("POCKETBASE_ADMIN_PASSWORD", "")
    if not email or not password:
        raise PocketBaseError(
            "POCKETBASE_ADMIN_EMAIL and POCKETBASE_ADMIN_PASSWORD must be set in .env"
        )
    return url, email, password


def _clear_token():
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


def _auth_headers() -> dict[str, str]:
    global _token, _token_expires_at, POCKETBASE_URL
    url, email, password = _pb_config()
    POCKETBASE_URL = url
    now = time.time()
    if _token and now < _token_expires_at - 60:
        return {"Authorization": _token}

    resp = httpx.post(
        f"{url}/api/collections/_superusers/auth-with-password",
        json={"identity": email, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        _clear_token()
        raise PocketBaseError(
            f"PocketBase auth failed ({resp.status_code}). "
            "Is PocketBase running and are admin credentials correct?"
        )
    data = resp.json()
    _token = data["token"]
    # Prefer token expiry if present; otherwise refresh hourly.
    _token_expires_at = now + 3600
    return {"Authorization": _token}


def _request(method: str, path: str, *, params=None, json_body=None, retry_auth=True):
    headers = _auth_headers()
    url = f"{POCKETBASE_URL}{path}"
    resp = httpx.request(
        method, url, headers=headers, params=params, json=json_body, timeout=60
    )
    # Invalid/stale tokens often come back as 403 ("Only superusers...") after password rotation.
    if resp.status_code in (401, 403) and retry_auth:
        _clear_token()
        return _request(method, path, params=params, json_body=json_body, retry_auth=False)
    if resp.status_code >= 400:
        raise PocketBaseError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def ping() -> None:
    """Verify PocketBase is reachable and credentials work."""
    _request("GET", "/api/health")
    # Auth + collections touch proves credentials + migrations applied.
    _request("GET", "/api/collections/calls/records", params={"perPage": 1})
    log.info("pocketbase ok at %s", POCKETBASE_URL)


def init_db() -> None:
    """Compatibility shim — schema lives in PocketBase migrations."""
    ping()


# ---------- Calls / segments ----------

def list_completed_calls() -> list[dict[str, Any]]:
    data = _request(
        "GET",
        "/api/collections/calls/records",
        params={
            "filter": 'status = "completed"',
            "sort": "-created",
            "perPage": 200,
            "fields": "id,audio_seconds,speakers",
        },
    )
    return list(data.get("items") or [])


def call_completed(call_id: str) -> bool:
    try:
        row = _request("GET", f"/api/collections/calls/records/{quote(call_id, safe='')}")
    except PocketBaseError as e:
        if "404" in str(e):
            return False
        raise
    return row.get("status") == "completed"


def find_existing_call(identity: str) -> str | None:
    # Escape double quotes in filter values
    safe = identity.replace("\\", "\\\\").replace('"', '\\"')
    data = _request(
        "GET",
        "/api/collections/calls/records",
        params={
            "filter": f'audio_url = "{safe}" && status = "completed"',
            "perPage": 1,
            "fields": "id",
        },
    )
    items = data.get("items") or []
    return items[0]["id"] if items else None


def save_transcript(identity: str, job_id: str, result: dict) -> str:
    segments = result.get("segments") or []
    call = _request(
        "POST",
        "/api/collections/calls/records",
        json_body={
            "audio_url": identity,
            "job_id": job_id or "",
            "status": "completed",
            "full_text": result.get("text") or "",
            "speakers": result.get("speakers") or 0,
            "audio_seconds": result.get("audio_seconds") or 0,
            "raw_json": result,
        },
    )
    call_id = call["id"]
    for i, seg in enumerate(segments):
        _request(
            "POST",
            "/api/collections/segments/records",
            json_body={
                "call": call_id,
                "seq": i,
                "speaker": seg.get("speaker") or "",
                "channel": seg.get("channel") if seg.get("channel") is not None else 0,
                "start": seg.get("start") if seg.get("start") is not None else 0,
                "end": seg.get("end") if seg.get("end") is not None else 0,
                "text": seg.get("text") or "",
            },
        )
    log.info("saved transcript for call %s (%d segments)", call_id, len(segments))
    return call_id


def load_call(call_id: str | None = None):
    """Return (call_id, meta, segments) matching the old SQLite helper shape."""
    if call_id is None:
        rows = list_completed_calls()
        if not rows:
            raise RuntimeError("No completed calls in PocketBase. Upload a call first.")
        call_id = rows[0]["id"]

    meta_row = _request("GET", f"/api/collections/calls/records/{quote(call_id, safe='')}")
    meta = {
        "id": meta_row["id"],
        "full_text": meta_row.get("full_text"),
        "speakers": meta_row.get("speakers"),
        "audio_seconds": meta_row.get("audio_seconds"),
    }

    # Paginate segments in case of long calls
    segments: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _request(
            "GET",
            "/api/collections/segments/records",
            params={
                "filter": f'call = "{call_id}"',
                "sort": "seq",
                "perPage": 200,
                "page": page,
            },
        )
        for s in data.get("items") or []:
            segments.append({
                "seq": int(s.get("seq") or 0),
                "speaker": s.get("speaker"),
                "channel": s.get("channel"),
                "start": s.get("start"),
                "end": s.get("end"),
                "text": s.get("text"),
            })
        total_pages = int(data.get("totalPages") or 1)
        if page >= total_pages:
            break
        page += 1

    return call_id, meta, segments


# ---------- Audits ----------

def get_audit(call_id: str) -> dict[str, Any] | None:
    data = _request(
        "GET",
        "/api/collections/audits/records",
        params={
            "filter": f'call = "{call_id}"',
            "perPage": 1,
        },
    )
    items = data.get("items") or []
    if not items:
        return None
    row = items[0]
    audit_json = row.get("audit_json")
    if isinstance(audit_json, str):
        audit_json = json.loads(audit_json)
    return {
        "id": row["id"],
        "audit_json": audit_json,
        "rubric_hash": row.get("rubric_hash"),
    }


def upsert_audit(call_id: str, audit: dict, rubric_hash: str) -> None:
    existing = get_audit(call_id)
    body = {
        "call": call_id,
        "audit_json": audit,
        "rubric_hash": rubric_hash,
    }
    if existing:
        _request(
            "PATCH",
            f"/api/collections/audits/records/{quote(existing['id'], safe='')}",
            json_body=body,
        )
    else:
        _request("POST", "/api/collections/audits/records", json_body=body)
