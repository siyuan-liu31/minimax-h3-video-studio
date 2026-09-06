import assert from "node:assert/strict";
import test from "node:test";

import {
  CANVAS_DOCUMENT_VERSION,
  H3_MAX_DURATION_FRAMES,
  H3_MAX_DURATION_SECONDS,
  H3_REFERENCE_BUDGET,
  V7_BACKUP_KEY,
  copyGeneratorNode,
  createCanvasNode,
  createDefaultCanvasDocument,
  h3DurationFrames,
  isValidH3DurationFrames,
  migrateCanvasDocument,
  parseCanvasDocument,
  resolveVideoExecutionProfile,
  resolveVideoProfileRequirement,
  serializeCanvasDocument,
  validateCanvasDocument,
} from "../app/studio-document.ts";

function ids(prefix = "id") {
  let next = 0;
  return () => `${prefix}-${++next}`;
}

function textOf(node) {
  return node.prompt.tokens.map((token) => token.kind === "text" ? token.text : `@${token.label}`).join("");
}

function legacySnapshot(overrides = {}) {
  return {
    version: 6,
    nodes: [
      { id: "prompt", kind: "prompt", title: "Prompt", position: { x: 10, y: 20 } },
      { id: "video", kind: "video", title: "H3 Video", position: { x: 30, y: 40 } },
      { id: "image", kind: "image", title: "Image", position: { x: 50, y: 60 } },
      { id: "output", kind: "output", title: "Output", position: { x: 70, y: 80 } },
    ],
    edges: [
      { id: "prompt-video", source: "prompt", target: "video", role: "prompt", data: { role: "prompt" } },
      { id: "video-output", source: "video", target: "output", role: "output", data: { role: "output" } },
      { id: "image-output", source: "image", target: "output", role: "output", data: { role: "output" } },
    ],
    prompt: "Keep @{asset} exactly.",
    imagePrompt: "Draw the subject.",
    videoParams: { aspectRatio: "16:9", duration: 124 / 24, steps: 4, loraStrength: 0.75, denoise: 1, seed: -1, directorMode: "auto", sourceVideoId: "" },
    imageParams: { aspectRatio: "1:1", quality: "2K", steps: 24, cfg: 7, loraStrength: 1, denoise: 0.65, seed: -1, negativePrompt: "bad" },
    profileSelection: { video: "auto", image: "flux-image" },
    job: { status: "idle", progress: 0, message: "ready" },
    jobHistory: [],
    ...overrides,
  };
}

function profile(overrides = {}) {
  return {
    id: "h3-fl-turbo",
    version: "1.0",
    display_name: "H3 FL Turbo",
    output_type: "video",
    compiler: "h3_fl",
    manifest_sha256: "a".repeat(64),
    sampling_mode: "turbo4",
    input_modalities: ["text", "image"],
    available: true,
    parameter_schema: {},
    defaults: {},
    limits: {},
    ...overrides,
  };
}

test("V7 default document uses UUID-like stable instances and only generator nodes can own jobs", () => {
  const document = createDefaultCanvasDocument(ids());
  assert.equal(document.version, CANVAS_DOCUMENT_VERSION);
  assert.deepEqual(document.viewport, { x: 0, y: 0, zoom: 1 });
  assert.deepEqual(document.nodes.map((node) => node.kind), ["video-generator", "output"]);
  assert.equal(document.nodes[0].job.status, "idle");
  assert.equal("job" in document.nodes[1], false);
  assert.equal(document.edges[0].sourceNodeId, document.nodes[0].id);
});

test("multiple generator instances and copied prompts/configs are deeply isolated", () => {
  const makeId = ids("copy");
  const first = createCanvasNode("video-generator", { x: 1, y: 2 }, makeId);
  first.prompt.tokens[0].text = "first prompt";
  first.bindings.push({ id: "old-binding", kind: "image", slot: 1, sourceNodeId: "asset", sourceOutputHandle: "image", role: "reference" });
  first.prompt.tokens.push({ kind: "mention", bindingId: "old-binding", label: "hero" });
  first.config.steps = 15;
  first.job = { id: "running", status: "running", progress: 50, message: "run" };
  first.resultVersions.push({ id: "old", mediaKind: "video" });
  const second = copyGeneratorNode(first, makeId);
  second.prompt.tokens[0].text = "second prompt";
  second.config.steps = 4;
  assert.equal(textOf(first), "first prompt@hero");
  assert.equal(first.config.steps, 15);
  assert.equal(second.job.status, "idle");
  assert.deepEqual(second.resultVersions, []);
  assert.notEqual(first.id, second.id);
  assert.notEqual(second.bindings[0].id, "old-binding");
  assert.equal(second.prompt.tokens[1].bindingId, second.bindings[0].id, "copied mention must follow its copied binding ID");
});

test("v2-v6 fixed IDs migrate to UUID instances and fan-out Prompt is copied, never shared", () => {
  const snapshot = legacySnapshot({
    version: 2,
    nodes: [
      { id: "prompt", kind: "prompt", title: "Prompt", position: { x: 0, y: 0 } },
      { id: "video", kind: "video", title: "Video A", position: { x: 0, y: 0 } },
      { id: "video-b", kind: "video", title: "Video B", position: { x: 0, y: 0 } },
      { id: "output", kind: "output", title: "Output", position: { x: 0, y: 0 } },
    ],
    edges: [
      { id: "p-a", source: "prompt", target: "video", data: { role: "prompt" } },
      { id: "p-b", source: "prompt", target: "video-b", data: { role: "prompt" } },
      { id: "a-o", source: "video", target: "output", data: { role: "output" } },
      { id: "b-o", source: "video-b", target: "output", data: { role: "output" } },
    ],
    prompt: "shared legacy text",
  });
  const migrated = migrateCanvasDocument(snapshot, { createId: ids("v7") });
  const videos = migrated.nodes.filter((node) => node.kind === "video-generator");
  assert.equal(videos.length, 2);
  assert.ok(videos.every((node) => node.id !== "video" && node.id !== "video-b"));
  assert.deepEqual(videos.map(textOf), ["shared legacy text", "shared legacy text"]);
  videos[0].prompt.tokens[0].text = "changed";
  assert.equal(textOf(videos[1]), "shared legacy text");
  assert.equal(migrated.nodes.some((node) => node.kind === "prompt"), false);
  assert.equal(migrated.edges.filter((edge) => edge.targetNodeId === migrated.nodes.find((node) => node.kind === "output").id).length, 2);
});

test("legacy global parameters, base sampling and result history become per-node state", () => {
  const snapshot = legacySnapshot({
    videoParams: { aspectRatio: "9:16", duration: 362 / 24, steps: 20, loraStrength: 0.8, denoise: 0.9, seed: 42, modelMode: "Ref2VA" },
    profileSelection: { video: "minimax-h3-ref-base", image: "flux-image" },
    job: { id: "job-v", status: "completed", progress: 100, media: "video", previewUrl: "/v.mp4", parameters: { output_type: "video", profile_id: "minimax-h3-ref-base", sampling_mode: "base" } },
    jobHistory: [{ id: "job-i", status: "completed", media: "image", previewUrl: "/i.png", parameters: { output_type: "image" } }],
  });
  const migrated = migrateCanvasDocument(snapshot, { createId: ids("m") });
  const video = migrated.nodes.find((node) => node.kind === "video-generator");
  const image = migrated.nodes.find((node) => node.kind === "image-generator");
  assert.deepEqual({ mode: video.config.directorMode, sampling: video.config.samplingPreset, frames: video.config.durationFrames, steps: video.config.steps, lora: video.config.loraStrength }, { mode: "r2v", sampling: "base20", frames: 362, steps: 20, lora: 0 });
  assert.equal(video.job.id, "job-v");
  assert.deepEqual(video.resultVersions.map((item) => item.id), ["job-v"]);
  assert.deepEqual(image.resultVersions.map((item) => item.id), ["job-i"]);
  assert.equal(image.config.profileId, "flux-image");
  assert.equal(image.config.quality, "2K");
});

test("ordered legacy media edges become typed slots and a visible paired soundtrack binding", () => {
  const asset = (id, media, includeAudio = false) => ({ id, kind: "asset", title: id, position: { x: 0, y: 0 }, asset: { media, fileName: `${id}.${media}`, remoteId: id.repeat(32).slice(0, 32), uploadState: "ready", role: "reference", includeAudio } });
  const base = legacySnapshot();
  const snapshot = legacySnapshot({
    nodes: [...base.nodes, asset("a", "image"), asset("b", "video", true), asset("c", "audio")],
    prompt: `Follow @{${"b".repeat(32)}} exactly.`,
    videoParams: { ...base.videoParams, directorMode: "rv2v", sourceVideoId: "b".repeat(32) },
    edges: [
      ...base.edges,
      { id: "c-v", source: "c", target: "video", role: "reference", data: { role: "reference", reference_index: 2 } },
      { id: "a-v", source: "a", target: "video", role: "first_frame", data: { role: "first_frame", reference_index: 0 } },
      { id: "b-v", source: "b", target: "video", role: "reference", data: { role: "reference", reference_index: 1, include_audio: true } },
    ],
  });
  const migrated = migrateCanvasDocument(snapshot, { createId: ids("slot") });
  const video = migrated.nodes.find((node) => node.kind === "video-generator");
  assert.deepEqual(video.bindings.map((binding) => [binding.kind, binding.slot, binding.role]), [
    ["image", 1, "first_frame"], ["video", 1, "reference"], ["audio", 1, "soundtrack"], ["audio", 2, "reference"],
  ]);
  assert.equal(video.bindings[1].pairedBindingId, video.bindings[2].id);
  assert.equal(video.bindings[2].pairedBindingId, video.bindings[1].id);
  assert.equal(migrated.edges.filter((edge) => edge.targetNodeId === video.id).length, 4, "paired soundtrack is an explicit ordered edge too");
  assert.equal(video.config.sourceBindingId, video.bindings[1].id, "legacy V2V/RV2V source is migrated to a stable binding ID");
  assert.equal(video.prompt.tokens.find((token) => token.kind === "mention").bindingId, video.bindings[1].id);
});

test("an ambiguous legacy global job is retained as unassigned history instead of attached to an arbitrary generator", () => {
  const base = legacySnapshot();
  const migrated = migrateCanvasDocument(legacySnapshot({
    nodes: [...base.nodes, { id: "video-b", kind: "video", title: "B", position: { x: 0, y: 0 } }],
    job: { id: "ambiguous", status: "completed", media: "video", previewUrl: "/ambiguous.mp4", parameters: { output_type: "video" } },
  }), { createId: ids("amb") });
  assert.deepEqual(migrated.unassignedResults.map((result) => result.id), ["ambiguous"]);
  assert.ok(migrated.nodes.filter((node) => node.kind === "video-generator").every((node) => node.resultVersions.length === 0));
});

test("bad JSON recovers a valid default while valid legacy JSON returns an exact backup request", () => {
  const broken = parseCanvasDocument("{not json", { createId: ids("bad") });
  assert.equal(broken.ok, false);
  assert.equal(broken.document.version, 7);
  assert.match(broken.error, /JSON|property|position/i);
  const raw = JSON.stringify(legacySnapshot());
  const parsed = parseCanvasDocument(raw, { createId: ids("parse") });
  assert.equal(parsed.ok, true);
  assert.equal(parsed.migrated, true);
  assert.deepEqual(parsed.backup, { key: V7_BACKUP_KEY, value: raw });
});

test("dangling bindings and reference overflow are preserved and visibly marked for repair", () => {
  const document = createDefaultCanvasDocument(ids("repair"));
  const video = document.nodes.find((node) => node.kind === "video-generator");
  video.bindings = Array.from({ length: H3_REFERENCE_BUDGET + 1 }, (_, index) => ({
    id: `binding-${index}`, kind: "image", slot: index + 1, sourceNodeId: `missing-${index}`, sourceOutputHandle: "image", role: "reference",
  }));
  const repaired = migrateCanvasDocument(document);
  const repairedVideo = repaired.nodes.find((node) => node.kind === "video-generator");
  assert.equal(repairedVideo.bindings.length, H3_REFERENCE_BUDGET + 1, "migration must not discard user bindings");
  assert.ok(repairedVideo.repairFlags.some((flag) => flag.code === "dangling-binding"));
  assert.ok(repairedVideo.repairFlags.some((flag) => flag.code === "reference-budget-exceeded"));
  assert.match(validateCanvasDocument(repaired).map((item) => item.message).join("\n"), /超过最多 12/);
});

test("V7 migration and serialization are idempotent", () => {
  const once = migrateCanvasDocument(legacySnapshot(), { createId: ids("once") });
  const twice = migrateCanvasDocument(once, { createId: ids("unused") });
  assert.deepEqual(twice, once);
  assert.deepEqual(JSON.parse(serializeCanvasDocument(twice)), once);
});

test("H3 uses integer frames as truth: 15.1 seconds rounds to the supported 362-frame ceiling", () => {
  assert.equal(h3DurationFrames(15.1), H3_MAX_DURATION_FRAMES);
  assert.equal(H3_MAX_DURATION_SECONDS, 362 / 24);
  assert.ok(H3_MAX_DURATION_SECONDS <= 15.1);
  assert.equal(isValidH3DurationFrames(124), true);
  assert.equal(isValidH3DurationFrames(123), false);
  assert.equal(isValidH3DurationFrames(362), true);
  assert.equal(isValidH3DurationFrames(363), false);
  const document = createDefaultCanvasDocument(ids("duration"));
  const video = document.nodes.find((node) => node.kind === "video-generator");
  video.config.durationFrames = 363;
  assert.ok(validateCanvasDocument(document).some((issue) => issue.code === "duration-exceeded"));
});

test("exactly twelve distinct reference files are accepted but the thirteenth is rejected", () => {
  const document = createDefaultCanvasDocument(ids("budget"));
  const video = document.nodes.find((node) => node.kind === "video-generator");
  video.bindings = [
    ...Array.from({ length: 9 }, (_, index) => ({ id: `p-${index}`, kind: "image", slot: index + 1, sourceNodeId: `picture-${index}`, sourceOutputHandle: "image", role: "reference" })),
    ...Array.from({ length: 3 }, (_, index) => ({ id: `a-${index}`, kind: "audio", slot: index + 1, sourceNodeId: `audio-${index}`, sourceOutputHandle: "audio", role: "reference" })),
  ];
  assert.equal(validateCanvasDocument(document).some((issue) => issue.code === "reference-budget-exceeded"), false);
  video.bindings.push({ id: "v-0", kind: "video", slot: 1, sourceNodeId: "video-0", sourceOutputHandle: "video", role: "reference" });
  assert.equal(validateCanvasDocument(document).some((issue) => issue.code === "reference-budget-exceeded"), true);
});

test("director mode × sampling resolves compiler requirements and the newest available profile", () => {
  assert.deepEqual(resolveVideoProfileRequirement("fl2v", "turbo4"), { requestedMode: "fl2v", resolvedMode: "fl2v", compiler: "h3_fl", samplingPreset: "turbo4", samplingMode: "turbo4" });
  assert.equal(resolveVideoProfileRequirement("rv2v", "base20").compiler, "h3_ref");
  assert.equal(resolveVideoProfileRequirement("rv2v", "base20").samplingMode, "base");
  assert.equal(resolveVideoProfileRequirement("auto", "turbo4", []).resolvedMode, "t2v");
  assert.equal(resolveVideoProfileRequirement("auto", "turbo4", [{ id: "p", kind: "image", slot: 1, sourceNodeId: "a", sourceOutputHandle: "image", role: "first_frame" }]).resolvedMode, "i2v");

  const profiles = [
    profile({ version: "1.0" }),
    profile({ id: "h3-fl-turbo-v2", version: "2.0" }),
    profile({ id: "h3-ref-base", compiler: "h3_ref", sampling_mode: "base", version: "3.0" }),
  ];
  assert.equal(resolveVideoExecutionProfile("i2v", "turbo4", profiles).profile.version, "2.0");
  assert.equal(resolveVideoExecutionProfile("rv2v", "base20", profiles).profile.id, "h3-ref-base");
  const missing = resolveVideoExecutionProfile("rv2v", "turbo4", profiles);
  assert.equal(missing.available, false);
  assert.match(missing.reason, /h3_ref \+ turbo4/);
});
