import { useState, useEffect, useRef } from "react";
import "./App.css";

const API = "http://localhost:8000";
const FRACTION = { pass: 1, partial: 0.5, fail: 0, unverified: 0, error: 0 };

function fmtTime(s) {
  if (s == null || isNaN(s)) return "--:--";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function bandClass(grade) {
  return (
    {
      Excellent: "band-excellent",
      Good: "band-good",
      "Needs improvement": "band-fair",
      Poor: "band-poor",
    }[grade] || "band-fair"
  );
}

export default function App() {
  const [calls, setCalls] = useState([]);
  const [callId, setCallId] = useState(null);
  const [audit, setAudit] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const audioRef = useRef(null);
  const fileInputRef = useRef(null);

  function refreshCalls() {
    return fetch(`${API}/api/calls`)
      .then((r) => r.json())
      .then((data) => {
        setCalls(data);
        return data;
      });
  }

  useEffect(() => {
    refreshCalls()
      .then((data) => {
        if (data.length) setCallId(data[0].id);
      })
      .catch(() =>
        setError("Could not reach the backend. Is uvicorn running on port 8000?")
      );
  }, []);

  function loadAudit(id, refresh = false) {
    setLoading(true);
    setError(null);
    if (!refresh) setAudit(null);
    fetch(`${API}/api/calls/${id}/audit${refresh ? "?refresh=true" : ""}`)
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json();
      })
      .then(setAudit)
      .catch(() => setError("Could not load the audit for this call."))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (callId != null) loadAudit(callId);
  }, [callId]);

  async function handleFiles(files) {
    const f = files && files[0];
    if (!f) return;
    setUploading(true);
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(`${API}/api/upload`, { method: "POST", body: fd });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || "Upload failed");
      }
      const { call_id } = await r.json();
      await refreshCalls();
      setCallId(call_id);
    } catch (e) {
      setUploadError(e.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  function onDrop(e) {
    e.preventDefault();
    setDragOver(false);
    if (!uploading) handleFiles(e.dataTransfer.files);
  }

  const segBySeq = {};
  if (audit) audit.segments.forEach((s) => (segBySeq[s.seq] = s));

  function jumpTo(seconds) {
    const a = audioRef.current;
    if (!a || seconds == null) return;
    a.currentTime = seconds;
    a.play();
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span> CallProof
          <span className="tagline">AI Call Quality Auditor</span>
        </div>
        {calls.length > 0 && (
          <select
            className="call-select"
            value={callId ?? ""}
            onChange={(e) => setCallId(e.target.value)}
          >
            {calls.map((c) => (
              <option key={c.id} value={c.id}>
                Call #{c.id} — {fmtTime(c.audio_seconds)}
              </option>
            ))}
          </select>
        )}
      </header>

      <div
        className={`dropzone ${dragOver ? "over" : ""} ${uploading ? "busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!uploading) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => !uploading && fileInputRef.current && fileInputRef.current.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*"
          hidden
          onChange={(e) => handleFiles(e.target.files)}
        />
        {uploading ? (
          <span>⏳ Transcribing your call… this can take 20–40 seconds</span>
        ) : (
          <span>⬆ Drag a call recording here, or click to choose a file</span>
        )}
      </div>
      {uploadError && <div className="banner error">{uploadError}</div>}

      {error && <div className="banner error">{error}</div>}
      {loading && (
        <div className="banner">Auditing the call… (the first run calls the model)</div>
      )}

      {audit && (
        <main className="layout">
          <section className="col-left">
            <div className={`score-card ${bandClass(audit.grade)}`}>
              <div className="score-num">{audit.score}</div>
              <div className="score-meta">
                <div className="grade">{audit.grade}</div>
                <div className="outof">out of 100</div>
                <div className="tally">
                  {Object.entries(audit.tally).map(([k, n]) => (
                    <span key={k} className={`pill v-${k}`}>
                      {n} {k}
                    </span>
                  ))}
                </div>
              </div>
              <button className="rerun" onClick={() => loadAudit(callId, true)}>
                ↻ Re-run
              </button>
            </div>

            <div className="rubric-line">
              Rubric: <b>{audit.rubric}</b> · agent = {audit.agent_speaker} ·{" "}
              {fmtTime(audit.audio_seconds)}
            </div>

            <h2 className="h">Findings</h2>
            {audit.findings.map((f) => {
              const pts = (FRACTION[f.verdict] ?? 0) * f.weight;
              const seg = f.evidence_seq != null ? segBySeq[f.evidence_seq] : null;
              return (
                <div className="finding" key={f.id}>
                  <div className="finding-head">
                    <span className={`badge v-${f.verdict}`}>{f.verdict}</span>
                    <span className="finding-name">{f.name}</span>
                    <span className="finding-pts">
                      {pts} / {f.weight}
                    </span>
                  </div>
                  <div className="finding-tag">
                    {f.method === "llm" ? "AI judgment" : "deterministic rule"}
                  </div>
                  <p className="finding-reason">{f.reasoning}</p>
                  {f.evidence_text && (
                    <div className="evidence">
                      <span className="quote">“{f.evidence_text}”</span>
                      {f.method === "llm" && (
                        <span className={`verify ${f.evidence_verified ? "ok" : "no"}`}>
                          {f.evidence_verified ? "✓ verified" : "✗ rejected"}
                        </span>
                      )}
                      {seg && (
                        <button className="jump" onClick={() => jumpTo(seg.start)}>
                          ▶ {fmtTime(seg.start)}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}

            {audit.coaching.length > 0 && (
              <>
                <h2 className="h">Coaching</h2>
                {audit.coaching.map((c, i) => (
                  <div className="coach" key={i}>
                    <div className="coach-crit">{c.criterion}</div>
                    <div className="coach-tip">{c.tip}</div>
                  </div>
                ))}
              </>
            )}
          </section>

          <section className="col-right">
            <h2 className="h">
              Transcript <span className="hint">click any line to hear it</span>
            </h2>
            <div className="transcript">
              {audit.segments.map((s) => {
                const isAgent = s.speaker === audit.agent_speaker;
                const active = now >= s.start && now < s.end;
                return (
                  <div
                    key={s.seq}
                    className={`turn ${isAgent ? "agent" : "customer"} ${
                      active ? "active" : ""
                    }`}
                    onClick={() => jumpTo(s.start)}
                  >
                    <div className="turn-meta">
                      <span className="who">{isAgent ? "AGENT" : "CUSTOMER"}</span>
                      <span className="ts">{fmtTime(s.start)}</span>
                    </div>
                    <div className="turn-text">{s.text}</div>
                  </div>
                );
              })}
            </div>
          </section>
        </main>
      )}

      {audit && (
        <footer className="player">
          <audio
            ref={audioRef}
            controls
            src={`${API}/api/calls/${callId}/audio`}
            onTimeUpdate={(e) => setNow(e.target.currentTime)}
          />
        </footer>
      )}
    </div>
  );
}
