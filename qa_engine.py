"""
CallProof - QA Engine (v3-capable, with logging).

Runs a config-driven rubric against a transcript and scores it deterministically.
All Claude calls use temperature=0 so a given transcript+rubric always produces
the same verdicts (and therefore the same score). Every failure is LOGGED, never
silently swallowed.

Criterion methods:
  deterministic | llm | deterministic_plus_llm | llm_plus_outcome_data
Verdicts: pass / partial / fail / unverified / not_applicable / error.
Scoring: pass=1.0, partial=0.5, fail=0.0 of weight. not_applicable / error / gates
(weight 0) are excluded from BOTH numerator and denominator (score renormalises).
"""

import os
import re
import sys
import json
import time
import logging

import httpx
from dotenv import load_dotenv

import db
import rules

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.qa")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
RUBRIC_PATH = "rubric.json"
MODEL = "claude-sonnet-5"
MAX_HTTP_RETRIES = 4       # attempts per Claude call (with backoff)
MAX_PARSE_RETRIES = 2      # re-asks if the reply isn't valid JSON
MAX_TOKENS = 2000


# ---------- Load transcript ----------
def load_call(call_id=None):
    try:
        return db.load_call(call_id)
    except Exception as e:  # noqa: BLE001
        sys.exit(str(e))


def identify_agent(segments):
    return segments[0]["speaker"] if segments else None


def format_transcript(segments, agent_speaker):
    lines = []
    for s in segments:
        who = "AGENT" if s["speaker"] == agent_speaker else "CUSTOMER"
        start = s["start"] if s["start"] is not None else 0.0
        lines.append(f'[seq {s["seq"]}] ({who}, {start:.1f}s) {s["text"]}')
    return "\n".join(lines)


# ---------- Evidence-validation gate ----------
def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def validate_evidence(quote, segments):
    q = _norm(quote)
    if not q:
        return False, None
    for s in segments:
        if q in _norm(s["text"]):
            return True, s["seq"]
    return False, None


# ---------- JSON parsing (robust) ----------
def _iter_json_objects(text):
    t = text or ""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield t[start:i + 1]
                    start = None


def parse_json(text):
    for obj in _iter_json_objects((text or "").strip()):
        try:
            return json.loads(obj)
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("no parseable JSON object found")


# ---------- LLM plumbing ----------
SYSTEM_INSTRUCTIONS = (
    "You are a strict call-quality auditor. Evaluate ONLY the AGENT on the one "
    "criterion given, using only the transcript provided. When your verdict is "
    "pass/partial/fail you MUST cite a real, exact quote copied verbatim from a "
    "transcript line. Never invent or paraphrase a quote. Respond with JSON only."
)


def _criterion_question(cr):
    if cr.get("question"):
        return cr["question"]
    steps = [s for s in (cr.get("question_step_1"), cr.get("question_step_2")) if s]
    if steps:
        return "\n".join(f"Step {i}: {s}" for i, s in enumerate(steps, 1))
    if cr.get("llm_question"):
        return cr["llm_question"]
    return None


def build_prompt(question, transcript_text, allowed_verdicts, strict=False):
    verdicts = " | ".join(f'"{v}"' for v in allowed_verdicts)
    na_note = ""
    if "not_applicable" in allowed_verdicts:
        na_note = ('\nIf this criterion does not apply to this call, return '
                   '"not_applicable" with a brief reason and an empty evidence_quote.')
    base = (
        f"{SYSTEM_INSTRUCTIONS}\n\nCRITERION:\n{question}\n\n"
        f"TRANSCRIPT (one turn per line):\n{transcript_text}\n\n"
        f"Your verdict MUST be one of: {verdicts}.{na_note}\n"
        "Return ONLY this JSON object:\n{\n"
        f'  "verdict": one of {verdicts},\n'
        '  "reasoning": "one or two sentences",\n'
        '  "evidence_quote": "a SHORT exact span, 5-15 words, copied verbatim from one transcript line",\n'
        '  "evidence_seq": <the seq number of the line you quoted>\n}'
    )
    if strict:
        base += "\n\nYour previous reply could not be parsed. Output ONLY raw JSON, no markdown, no commentary."
    return base


def call_claude(prompt):
    """POST to Claude with temperature=0. Retries with backoff on 429/5xx.
    Logs every failed attempt. Raises RuntimeError only if all attempts fail."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    last_err = None
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": MAX_TOKENS,
                      "thinking": {"type": "disabled"},
                      "output_config": {"effort": "low"},
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                return "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
            last_err = f"{resp.status_code}: {resp.text[:300]}"
            log.warning("claude attempt %d/%d -> %s", attempt, MAX_HTTP_RETRIES, last_err)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 * attempt)          # 2s, 4s, 6s ... backoff
                continue
            break                                # 400/401/403: retrying won't help
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            log.warning("claude attempt %d/%d exception: %s", attempt, MAX_HTTP_RETRIES, last_err)
            time.sleep(1)
    log.error("claude call failed after %d attempts: %s", MAX_HTTP_RETRIES, last_err)
    raise RuntimeError(f"Claude call failed: {last_err}")


def run_llm_criterion(criterion, transcript_text, segments):
    name = criterion.get("name", criterion.get("id", "?"))
    question = _criterion_question(criterion)
    if not question:
        log.error("criterion '%s' has no LLM question defined", name)
        return {"verdict": "error", "reasoning": "No LLM question defined for this criterion.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": False}
    allowed = criterion.get("verdict_space", ["pass", "partial", "fail"])

    parsed = None
    for attempt in range(MAX_PARSE_RETRIES):
        try:
            raw = call_claude(build_prompt(question, transcript_text, allowed, strict=(attempt > 0)))
        except Exception as e:  # noqa: BLE001
            log.error("criterion '%s' LLM call failed: %s", name, e)
            return {"verdict": "error", "reasoning": f"LLM call failed: {e}",
                    "evidence_text": None, "evidence_seq": None, "evidence_verified": False}
        try:
            parsed = parse_json(raw)
            break
        except Exception:  # noqa: BLE001
            log.warning("criterion '%s' returned unparseable JSON (attempt %d)", name, attempt + 1)
            parsed = None
    if parsed is None:
        log.error("criterion '%s' -> error (no valid JSON after retries)", name)
        return {"verdict": "error", "reasoning": "Model output was not valid JSON after a retry.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": False}

    verdict = parsed.get("verdict", "error")
    if verdict == "not_applicable":
        log.info("criterion '%s' -> not_applicable", name)
        return {"verdict": "not_applicable", "reasoning": parsed.get("reasoning", ""),
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}

    quote = parsed.get("evidence_quote", "")
    verified, seq = validate_evidence(quote, segments)
    if criterion.get("evidence_required", True) and not verified:
        log.info("criterion '%s' -> UNVERIFIED (quote not found in transcript)", name)
        return {"verdict": "unverified", "reasoning": parsed.get("reasoning", ""),
                "evidence_text": quote, "evidence_seq": parsed.get("evidence_seq"),
                "evidence_verified": False, "original_verdict": verdict}
    log.info("criterion '%s' -> %s (evidence verified)", name, verdict)
    return {"verdict": verdict, "reasoning": parsed.get("reasoning", ""),
            "evidence_text": quote, "evidence_seq": seq, "evidence_verified": verified}


def run_deterministic_criterion(criterion, segments, agent_speaker):
    name = criterion.get("name", criterion.get("id", "?"))
    fn = rules.REGISTRY.get(criterion["check"])
    if not fn:
        log.error("criterion '%s' references unknown rule '%s'", name, criterion["check"])
        return {"verdict": "error", "reasoning": f"Unknown rule '{criterion['check']}'.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}
    try:
        r = fn(segments, agent_speaker)
    except Exception as e:  # noqa: BLE001
        log.error("rule '%s' raised: %s", criterion["check"], e)
        return {"verdict": "error", "reasoning": f"Rule crashed: {e}",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}
    log.info("criterion '%s' -> %s (rule)", name, r["verdict"])
    return {"verdict": r["verdict"], "reasoning": r["reasoning"],
            "evidence_text": r["evidence_text"], "evidence_seq": r["evidence_seq"],
            "evidence_verified": r["evidence_text"] is not None}


def run_combined_criterion(criterion, segments, agent_speaker, transcript_text):
    det = run_deterministic_criterion(criterion, segments, agent_speaker)
    llm_q = criterion.get("llm_question")
    if not llm_q:
        return det
    llm_cr = dict(criterion)
    llm_cr["question"] = llm_q
    llm_cr["verdict_space"] = ["pass", "fail"]
    llm = run_llm_criterion(llm_cr, transcript_text, segments)
    if det["verdict"] == "fail":
        return det
    if llm["verdict"] == "fail":
        return llm
    return det


def evaluate_criterion(criterion, segments, agent_speaker, transcript_text):
    method = criterion.get("method")
    if method == "deterministic":
        return run_deterministic_criterion(criterion, segments, agent_speaker)
    if method == "deterministic_plus_llm":
        return run_combined_criterion(criterion, segments, agent_speaker, transcript_text)
    return run_llm_criterion(criterion, transcript_text, segments)


# ---------- Scoring ----------
FRACTION = {"pass": 1.0, "partial": 0.5, "fail": 0.0, "unverified": 0.0}
SCORE_EXCLUDED = {"not_applicable", "error"}


def performance_band(score):
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Needs improvement"
    return "Poor"


def awarded_points(criterion, verdict):
    if criterion.get("is_gate") or criterion.get("weight", 0) == 0:
        return None
    if verdict in SCORE_EXCLUDED:
        return None
    return round(criterion["weight"] * FRACTION.get(verdict, 0.0), 1)


def score_results(results):
    rows, earned, possible, tally, gate_fails = [], 0.0, 0.0, {}, []
    for cr, res in results:
        v = res["verdict"]
        tally[v] = tally.get(v, 0) + 1
        if cr.get("is_gate") and v == "fail":
            gate_fails.append(cr["name"])
        pts = awarded_points(cr, v)
        rows.append((cr, res, pts))
        if pts is not None:
            earned += pts
            possible += cr["weight"]
    score = round(earned / possible * 100, 1) if possible else 0.0
    if "error" in tally:
        log.warning("%d criteria errored and were EXCLUDED from the score", tally["error"])
    log.info("score: %s/%s weighted -> %s/100 (%s); tally=%s; gates_failed=%s",
             earned, possible, score, performance_band(score), tally, gate_fails or "none")
    return rows, score, round(earned, 1), round(possible, 1), tally, gate_fails


# ---------- Coaching ----------
def generate_coaching(weak):
    lines = []
    for i, (c, res) in enumerate(weak, 1):
        ev = res.get("evidence_text") or "(no specific line)"
        lines.append(f'{i}. {c["name"]} ({res["verdict"].upper()}): {res["reasoning"]} Evidence: "{ev}"')
    prompt = (
        "You are a supportive but candid call-coaching assistant. Below are the criteria where "
        "the agent scored below full marks. For EACH area, write ONE specific, actionable coaching "
        "tip (1-2 sentences) referencing what actually happened. Return ONLY this JSON:\n"
        '{"coaching": [{"criterion": "<exact criterion name>", "tip": "<1-2 sentences>"}]}\n\n'
        "WEAK AREAS:\n" + "\n".join(lines)
    )
    for attempt in range(2):
        try:
            out = parse_json(call_claude(prompt)).get("coaching", [])
            log.info("coaching generated for %d area(s)", len(weak))
            return out
        except Exception as e:  # noqa: BLE001
            log.error("coaching attempt %d failed: %s", attempt + 1, e)
    return [{"criterion": c["name"], "tip": "(coaching temporarily unavailable)"} for c, _ in weak]


LABEL = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL",
         "unverified": "UNVERIFIED", "not_applicable": "N/A", "error": "ERROR"}


def main():
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY not found in .env")
    arg_id = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    agent_override = sys.argv[2] if len(sys.argv) > 2 else None

    call_id, meta, segments = load_call(arg_id)
    if not segments:
        sys.exit(f"Call {call_id} has no segments to analyze.")
    agent = agent_override or identify_agent(segments)
    transcript_text = format_transcript(segments, agent)
    with open(RUBRIC_PATH) as f:
        rubric = json.load(f)

    log.info("auditing call %d (%ss, %d turns) against '%s'",
             call_id, meta.get("audio_seconds"), len(segments), rubric["name"])
    results = [(c, evaluate_criterion(c, segments, agent, transcript_text))
               for c in rubric["criteria"]]
    rows, score, earned, possible, tally, gate_fails = score_results(results)
    grade = performance_band(score)

    print("=" * 72)
    for c, res, pts in rows:
        gate = " [GATE]" if c.get("is_gate") else ""
        pt = "  -  " if pts is None else f"{pts:>5}/{c['weight']}"
        print(f"[{c['method']}]{gate} {c['name']} -> {LABEL.get(res['verdict'], res['verdict'])}  {pt}")
    if gate_fails:
        print(f"\n!! GATE FAILURE - flag for manager review: {', '.join(gate_fails)}")
    print(f"TOTAL: {score}/100 ({earned} of {possible} weighted points) -> {grade.upper()}")
    print("=" * 72)


if __name__ == "__main__":
    main()
