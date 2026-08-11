/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const calls = new Collection({
    type: "base",
    name: "calls",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { type: "text", name: "audio_url", required: true },
      { type: "text", name: "job_id", required: false },
      {
        type: "select",
        name: "status",
        required: true,
        maxSelect: 1,
        values: ["pending", "completed", "failed"],
      },
      { type: "text", name: "full_text", required: false },
      { type: "number", name: "speakers", required: false },
      { type: "number", name: "audio_seconds", required: false },
      { type: "json", name: "raw_json", required: false },
      {
        type: "autodate",
        name: "created",
        onCreate: true,
        onUpdate: false,
      },
      {
        type: "autodate",
        name: "updated",
        onCreate: true,
        onUpdate: true,
      },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_calls_audio_url ON calls (audio_url)",
    ],
  });
  app.save(calls);

  const segments = new Collection({
    type: "base",
    name: "segments",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      {
        type: "relation",
        name: "call",
        required: true,
        maxSelect: 1,
        collectionId: calls.id,
        cascadeDelete: true,
      },
      // number fields: do not mark required — PocketBase treats 0 as blank
      { type: "number", name: "seq", required: false },
      { type: "text", name: "speaker", required: false },
      { type: "number", name: "channel", required: false },
      { type: "number", name: "start", required: false },
      { type: "number", name: "end", required: false },
      { type: "text", name: "text", required: false },
      {
        type: "autodate",
        name: "created",
        onCreate: true,
        onUpdate: false,
      },
      {
        type: "autodate",
        name: "updated",
        onCreate: true,
        onUpdate: true,
      },
    ],
    indexes: [
      "CREATE INDEX idx_segments_call_seq ON segments (call, seq)",
    ],
  });
  app.save(segments);

  const audits = new Collection({
    type: "base",
    name: "audits",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      {
        type: "relation",
        name: "call",
        required: true,
        maxSelect: 1,
        collectionId: calls.id,
        cascadeDelete: true,
      },
      { type: "json", name: "audit_json", required: true },
      { type: "text", name: "rubric_hash", required: true },
      {
        type: "autodate",
        name: "created",
        onCreate: true,
        onUpdate: false,
      },
      {
        type: "autodate",
        name: "updated",
        onCreate: true,
        onUpdate: true,
      },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_audits_call ON audits (call)",
    ],
  });
  app.save(audits);
}, (app) => {
  for (const name of ["audits", "segments", "calls"]) {
    try {
      const collection = app.findCollectionByNameOrId(name);
      app.delete(collection);
    } catch {
      // already gone
    }
  }
});
