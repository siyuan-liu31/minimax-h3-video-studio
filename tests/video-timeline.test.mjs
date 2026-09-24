import assert from "node:assert/strict";
import test from "node:test";

import {
  appendTimelineReference,
  clampDurationToProfile,
  continuationChoices,
  continuationSourceReady,
  draftVideoMediaSegment,
  H3_DURATION_OPTIONS,
  H3_MAX_GENERATION_DURATION,
  h3EffectiveDuration,
  h3ReferenceTagMap,
  findTimelineProfile,
  isH3DurationOption,
  isVideoMediaSegment,
  latestResolvedWorkflow,
  mergedResultNotificationKey,
  mergeVideoProject,
  moveVideoSegment,
  notifyMergedResultOnce,
  profileBounds,
  profileDurationOptions,
  resolvedWorkflowSummary,
  retargetSegmentCompiler,
  retargetSegmentForReferences,
  projectCanMerge,
  projectMergeBlockReason,
  projectIsActive,
  selectedTimelineRunPlan,
  serializeVideoProject,
  timelineStatusLabel,
  timelineProfileKey,
  timelineAssetMentionToken,
  timelinePromptPreview,
  timelineRequiredCompiler,
  timelineWorkflowModeLabel,
  retargetSegmentSampling,
  validateVideoProject,
  videoSegmentDuration,
} from "../app/video-project.ts";
import { VideoProjectApi } from "../app/video-project-api.ts";

function segment(id, overrides = {}) {
  return {
    id,
    continuation: "none",
    status: "draft",
    attempts: [],
    request: {
      prompt: `shot ${id}`,
      prompt_mode: "preserve_tags_only",
      parts: { subject: "robot" },
      parameters: { aspect_ratio: "16:9", duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1, seed: -1 },
      profile_id: "h3-fl-turbo",
      profile_version: "1",
      profile_digest: "a".repeat(64),
      references: [],
    },
    ...overrides,
  };
}

function project(overrides = {}) {
  return { id: "p1", title: "Long story", status: "draft", segments: [segment("s1"), segment("s2")], ...overrides };
}

test("direct video assets serialize as merge-only clips and never enter the H3 run plan", () => {
  const asset = { id: "f".repeat(32), media: { duration: 8, fps: 30, frame_count: 240, has_audio: true } };
  const clip = draftVideoMediaSegment("e".repeat(32), asset);
  assert.equal(isVideoMediaSegment(clip), true);
  assert.equal(videoSegmentDuration(clip), 8);
  assert.deepEqual(selectedTimelineRunPlan([clip], new Set([clip.id])), []);
  assert.deepEqual(serializeVideoProject({ title: "Imported", status: "completed", segments: [clip] }).segments, [{ id: clip.id, kind: "media", media_source: { type: "asset", asset_id: asset.id, start_frame: 0, end_frame: 240, fps: 30, keep_audio: true } }]);
});

test("project serialization keeps editable workflow inputs and strips durable execution output", () => {
  const value = project({
    merged: { preview_url: "/merged/preview", download_url: "/merged/download" },
    segments: [segment("s1", {
      status: "completed",
      job_id: "job-1",
      attempts: [{ job_id: "job-1", status: "completed" }],
      preview_url: "/preview/1",
      download_url: "/download/1",
      request: {
        ...segment("s1").request,
        references: [{ asset_id: "b".repeat(32), role: "identity" }],
      },
    })],
  });

  assert.deepEqual(serializeVideoProject(value), {
    title: "Long story",
    segments: [{
      id: "s1",
      continuation: "none",
      request: {
        prompt: "shot s1",
        prompt_mode: "preserve_tags_only",
        parts: {},
        parameters: { aspect_ratio: "16:9", duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1, seed: -1 },
        profile_id: "h3-fl-turbo",
        profile_version: "1",
        profile_digest: "a".repeat(64),
        references: [{ asset_id: "b".repeat(32), role: "identity" }],
      },
    }],
  });
});

test("storyboard serialization and validation preserve independently recoverable frame ranges", () => {
  const sourceId = "c".repeat(32);
  const value = project({
    storyboard: { source_asset_id: sourceId, fps: 30, frame_count: 600, cut_frames: [300] },
    segments: [
      segment("s1", { source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 } }),
      segment("s2", { source_range: { asset_id: sourceId, start_frame: 300, end_frame: 600, fps: 30 } }),
    ],
  });
  const serialized = serializeVideoProject(value);
  assert.deepEqual(serialized.storyboard, value.storyboard);
  assert.deepEqual(serialized.segments.map((item) => item.source_range), value.segments.map((item) => item.source_range));
  assert.equal(validateVideoProject(value).filter((error) => /source range|Storyboard/.test(error)).length, 0);
  const tooLong = project({
    storyboard: { source_asset_id: sourceId, fps: 30, frame_count: 451, cut_frames: [] },
    segments: [segment("s1", { source_range: { asset_id: sourceId, start_frame: 0, end_frame: 451, fps: 30 } })],
  });
  assert.match(validateVideoProject(tooLong).join("\n"), /may not exceed 15 seconds/);
});

test("a storyboard may coexist with an independent blank generation segment", () => {
  const sourceId = "c".repeat(32);
  const value = project({
    storyboard: { source_asset_id: sourceId, fps: 30, frame_count: 300, cut_frames: [] },
    segments: [
      segment("source", { source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 } }),
      segment("blank"),
    ],
  });
  assert.equal(validateVideoProject(value).filter((error) => /Storyboard|source range/.test(error)).length, 0);
  assert.equal(serializeVideoProject(value).segments[1].source_range, undefined);
});

test("durable polling merges execution state without dropping omitted local fields", () => {
  const local = project();
  const merged = mergeVideoProject(local, {
    id: "p1",
    status: "running",
    segments: [{ id: "s1", status: "completed", attempts: [{ job_id: "job-1" }], preview_url: "/preview/1" }, { id: "s2", status: "running", job_id: "job-2" }],
  });

  assert.equal(merged.status, "running");
  assert.equal(merged.segments[0].request.prompt, "shot s1");
  assert.deepEqual(merged.segments[0].request.parts, {});
  assert.equal(merged.segments[0].preview_url, "/preview/1");
  assert.equal(merged.segments[1].request.parameters.steps, 4);
  assert.equal(merged.segments[1].job_id, "job-2");
});

test("only a completed merged result with durable identity triggers a result-library refresh", () => {
  assert.equal(mergedResultNotificationKey(project({ merged: { status: "merging", result_job_id: "job-merge" } })), undefined);
  assert.equal(mergedResultNotificationKey(project({ merged: { status: "completed" } })), undefined);
  assert.equal(
    mergedResultNotificationKey(project({ merged: { status: "completed", result_job_id: "job-merge" } })),
    "p1:job-merge",
  );
});

test("polling transition from merging to completed refreshes Results exactly once", () => {
  const notified = new Set();
  let refreshes = 0;
  const notify = () => { refreshes += 1; };
  assert.equal(notifyMergedResultOnce(project({ merged: { status: "merging", result_job_id: "job-merge" } }), notified, notify), false);
  const completed = project({ merged: { status: "completed", result_job_id: "job-merge" } });
  assert.equal(notifyMergedResultOnce(completed, notified, notify), true);
  assert.equal(notifyMergedResultOnce(completed, notified, notify), false);
  assert.equal(refreshes, 1);
});

test("continuation is exactly one enum choice and first segment cannot depend on a predecessor", () => {
  assert.deepEqual(continuationChoices(0), ["none"]);
  assert.deepEqual(continuationChoices(1), ["none", "tail_frame", "previous_video", "motion_context"]);
  assert.equal(continuationSourceReady([segment("s1"), segment("s2")], 1), true);
  assert.equal(continuationSourceReady([segment("s1"), segment("s2", { continuation: "tail_frame" })], 1), false);
  assert.equal(continuationSourceReady([segment("s1", { status: "completed" }), segment("s2", { continuation: "tail_frame" })], 1), true);

  const invalidFirst = project({ segments: [segment("s1", { continuation: "tail_frame" })] });
  assert.match(validateVideoProject(invalidFirst).join("\n"), /first segment.*none/i);

  const tooMany = project({ segments: [segment("s1"), segment("s2", {
    continuation: "previous_video",
    request: {
      ...segment("s2").request,
      references: Array.from({ length: 12 }, (_, index) => ({ asset_id: index.toString(16).repeat(32), role: "identity" })),
    },
  })] });
  assert.match(validateVideoProject(tooMany).join("\n"), /12-file reference budget/i);
  const mixedTail = project({ segments: [segment("s1"), segment("s2", {
    continuation: "tail_frame",
    request: { ...segment("s2").request, references: [{ asset_id: "b".repeat(32), role: "identity" }] },
  })] });
  assert.match(validateVideoProject(mixedTail).join("\n"), /last_frame image reference/i);
});

test("Motion Context settings round-trip and enforce lossless-chain invariants", () => {
  const continued = segment("s2", {
    continuation: "motion_context",
    motion_context: { video_frames: 39, audio_frames: 48 },
  });
  const value = project({ segments: [segment("s1"), continued] });
  assert.deepEqual(serializeVideoProject(value).segments[1].motion_context, {
    video_frames: 39,
    audio_frames: 48,
  });
  assert.equal(validateVideoProject(value).length, 0);
  assert.equal(timelineWorkflowModeLabel(continued), "潜空间动作与声音续接 · Motion Context");

  const invalid = project({ segments: [segment("s1"), {
    ...continued,
    motion_context: { video_frames: 6, audio_frames: 241 },
    request: {
      ...continued.request,
      parameters: { ...continued.request.parameters, aspect_ratio: "9:16" },
      references: [{ asset_id: "b".repeat(32), role: "first_frame" }],
    },
  }] });
  const errors = validateVideoProject(invalid).join("\n");
  assert.match(errors, /video frames must be 5, 22, 39, or 56/i);
  assert.match(errors, /audio frames must be an integer in 0\.\.240/i);
  assert.match(errors, /cannot be combined with a first-frame reference/i);
  assert.match(errors, /must keep the previous aspect ratio/i);
});

test("selected execution plan visibly auto-includes every unfinished continuation predecessor", () => {
  const segments = [
    segment("s1"),
    segment("s2", { continuation: "tail_frame" }),
    segment("s3", { continuation: "previous_video" }),
    segment("s4"),
  ];
  assert.deepEqual(
    selectedTimelineRunPlan(segments, new Set(["s3"])).map(({ id, autoIncluded }) => [id, autoIncluded]),
    [["s1", true], ["s2", true], ["s3", false]],
  );
  segments[1].status = "completed";
  assert.deepEqual(
    selectedTimelineRunPlan(segments, new Set(["s3"])).map(({ id, autoIncluded }) => [id, autoIncluded]),
    [["s3", false]],
  );
  assert.deepEqual(
    selectedTimelineRunPlan(segments, new Set(["s4"])).map(({ id }) => id),
    ["s4"],
  );
});

test("every segment serializes and validates H3 generation strength", () => {
  assert.equal(serializeVideoProject(project()).segments[0].request.parameters.denoise, 1);
  assert.match(validateVideoProject(project({ segments: [segment("s1", { request: { ...segment("s1").request, parameters: { ...segment("s1").request.parameters, denoise: 0 } } })] })).join("\n"), /denoise must be 0\.05\.\.1/i);
});

test("timeline accepts the 362-frame H3 duration and rejects anything longer", () => {
  const withDuration = (duration) => project({ segments: [segment("s1", { request: {
    ...segment("s1").request,
    parameters: { ...segment("s1").request.parameters, duration },
  } })] });

  assert.equal(H3_MAX_GENERATION_DURATION, 362 / 24);
  assert.equal(H3_DURATION_OPTIONS.length, 15);
  assert.equal(H3_DURATION_OPTIONS[0], 124 / 24);
  assert.equal(H3_DURATION_OPTIONS.at(-1), 362 / 24);
  assert.doesNotMatch(validateVideoProject(withDuration(362 / 24)).join("\n"), /duration/i);
  assert.match(validateVideoProject(withDuration(362 / 24 + 0.001)).join("\n"), /duration/i);
  assert.match(validateVideoProject(withDuration(5.05)).join("\n"), /17k\+5 frame grid/i);
  assert.equal(isH3DurationOption(5.05), false);
  assert.equal(h3EffectiveDuration(5.05), 124 / 24);
  assert.equal(h3EffectiveDuration(5.17), 124 / 24);
  assert.equal(h3EffectiveDuration(5.60), 141 / 24);
  assert.equal(serializeVideoProject(withDuration(5.05)).segments[0].request.parameters.duration, 124 / 24);
});

test("timeline @ mentions preserve user text and typed reference insertion order", () => {
  const image = "b".repeat(32);
  const video = "c".repeat(32);
  const audio = "d".repeat(32);
  let references = appendTimelineReference([], { id: image, kind: "image" }, "none");
  references = appendTimelineReference(references, { id: video, kind: "video" }, "none");
  references = appendTimelineReference(references, { id: audio, kind: "audio" }, "none");
  assert.deepEqual(references, [
    { asset_id: image, role: "reference" },
    { asset_id: video, role: "reference", include_audio: false },
    { asset_id: audio, role: "reference" },
  ]);
  const prompt = `Keep this exact sentence. ${timelineAssetMentionToken(video)} then ${timelineAssetMentionToken(image)}.`;
  const payload = serializeVideoProject(project({ segments: [segment("s1", { request: { ...segment("s1").request, prompt, references } })] }));
  assert.equal(payload.segments[0].request.prompt, prompt);
  assert.deepEqual(payload.segments[0].request.references.map((reference) => reference.asset_id), [image, video, audio]);
  assert.equal(payload.segments[0].request.prompt_mode, "preserve_tags_only");
  assert.deepEqual(payload.segments[0].request.parts, {});
});

test("timeline read-only preview mirrors typed tags and implicit continuation without using legacy parts", () => {
  const picture = "b".repeat(32);
  const video = "c".repeat(32);
  const references = [
    { asset_id: picture, role: "reference" },
    { asset_id: video, role: "reference", include_audio: true },
  ];
  const kinds = new Map([[picture, "image"], [video, "video"]]);
  assert.equal(
    timelinePromptPreview(`Keep @{${picture}} and @{${video}} exactly.`, references, kinds, "none"),
    "Keep <Picture 1> and <Video 1> exactly.",
  );
  assert.equal(
    timelinePromptPreview(`Continue @{${picture}}.`, references, kinds, "previous_video"),
    "Continue <Picture 1>.; <Video 2>",
  );
  assert.equal(
    timelinePromptPreview("Continue exactly", [{ asset_id: picture, role: "last_frame" }], kinds, "tail_frame"),
    "Continue exactly; <Picture 1>",
  );
});

test("long-video validation requires the editable prompt and ignores legacy structured text", () => {
  const partsOnly = project({ segments: [segment("s1", { request: { ...segment("s1").request, prompt: "", parts: { subject: "stale robot" } } })] });
  assert.match(validateVideoProject(partsOnly).join("\n"), /needs a prompt/);
  assert.deepEqual(serializeVideoProject(partsOnly).segments[0].request.parts, {});
});

test("visible H3 reference tags match native independent type and paired-audio numbering", () => {
  const picture = "b".repeat(32);
  const videoOne = "c".repeat(32);
  const audio = "d".repeat(32);
  const videoTwo = "e".repeat(32);
  const references = [
    { asset_id: picture, role: "reference" },
    { asset_id: videoOne, role: "reference", include_audio: true },
    { asset_id: audio, role: "reference" },
    { asset_id: videoTwo, role: "reference", include_audio: true },
  ];
  const kinds = new Map([[picture, "image"], [videoOne, "video"], [audio, "audio"], [videoTwo, "video"]]);
  const tags = h3ReferenceTagMap(references, kinds);
  assert.deepEqual(tags.get(picture), { primary: "<Picture 1>" });
  assert.deepEqual(tags.get(videoOne), { primary: "<Video 1>", pairedAudio: "<Audio 1>" });
  assert.deepEqual(tags.get(videoTwo), { primary: "<Video 2>", pairedAudio: "<Audio 2>" });
  assert.deepEqual(tags.get(audio), { primary: "<Audio 3>" });
  assert.equal(h3ReferenceTagMap(references, kinds, "tail_frame").get(picture).primary, "<Picture 2>");
});

test("continuation keeps one H3 reference slot reserved and tail-frame only accepts one image", () => {
  const ids = Array.from({ length: 12 }, (_, index) => index.toString(16).repeat(32));
  let previousVideo = [];
  for (let index = 0; index < 12; index += 1) previousVideo = appendTimelineReference(previousVideo, { id: ids[index], kind: "image" }, "previous_video");
  assert.equal(previousVideo.length, 11);
  const tail = appendTimelineReference([], { id: "a".repeat(32), kind: "image" }, "tail_frame");
  assert.deepEqual(tail, [{ asset_id: "a".repeat(32), role: "last_frame" }]);
  assert.strictEqual(appendTimelineReference(tail, { id: "b".repeat(32), kind: "image" }, "tail_frame"), tail);
  assert.deepEqual(appendTimelineReference([], { id: "c".repeat(32), kind: "audio" }, "tail_frame"), []);
});

test("completed segment exposes actual sampler, scheduler, steps, denoise and LoRA receipt", () => {
  const evidence = { steps: 4, sampler: "sa_solver", scheduler: "simple", denoise: 0.65, lora: "h3-turbo.safetensors", lora_strength: 0.75, diffusion_model: "h3.safetensors" };
  const value = segment("s1", { attempts: [{ status: "completed", workflow_evidence: evidence }] });
  assert.deepEqual(latestResolvedWorkflow(value), evidence);
  assert.equal(resolvedWorkflowSummary(evidence), "4 steps · sa_solver / simple · denoise 0.65 · LoRA 0.75");
  assert.equal(resolvedWorkflowSummary({ steps: 20, sampler: "sa_solver", scheduler: "simple", denoise: 1, lora: null }), "20 steps · sa_solver / simple · denoise 1.00 · LoRA off");
});

test("segment order and active status are deterministic", () => {
  assert.deepEqual(moveVideoSegment(project().segments, "s2", -1).map((item) => item.id), ["s2", "s1"]);
  assert.deepEqual(moveVideoSegment(project().segments, "s1", -1).map((item) => item.id), ["s1", "s2"]);
  assert.equal(projectIsActive(project({ status: "running" })), true);
  assert.equal(projectIsActive(project({ segments: [segment("s1", { status: "queued" })] })), true);
  assert.equal(projectIsActive(project({ status: "completed" })), false);
  assert.equal(projectCanMerge(project({ segments: [segment("s1", { status: "completed", download_url: "/one.mp4" })] })), true);
  assert.equal(projectCanMerge(project({ segments: [segment("s1", { status: "completed" })] })), true);
  assert.equal(projectCanMerge(project({ segments: [segment("s1", { status: "failed" })] })), false);
  assert.match(projectMergeBlockReason(project({ segments: [segment("s1", { status: "stale" })] })), /过期/);
  assert.match(projectMergeBlockReason(project({ segments: [segment("s1", { status: "failed" })] })), /尚未完成/);
  assert.equal(projectMergeBlockReason(project({ segments: [segment("s1", { status: "completed" })] })), undefined);
  assert.equal(timelineStatusLabel("merging"), "合并中");
});

test("long-video workflow mode and sampling are separate trusted profile dimensions", () => {
  const flTurbo = {
    id: "fl-turbo", version: "1", display_name: "FL Turbo", output_type: "video", compiler: "h3_fl",
    manifest_sha256: "a".repeat(64), sampling_mode: "turbo4", available: true,
    defaults: { steps: 4, lora_strength: 0.75 }, limits: { steps: [4, 20], lora_strength: [0, 1] },
  };
  const flBase = {
    ...flTurbo, id: "fl-base", display_name: "FL Base", manifest_sha256: "b".repeat(64), sampling_mode: "base",
    defaults: { steps: 20, lora_strength: 0 }, limits: { steps: [8, 50], lora_strength: [0, 0] },
  };
  const refBase = { ...flBase, id: "ref-base", compiler: "h3_ref", manifest_sha256: "c".repeat(64) };
  const value = segment("s2", { request: { ...segment("s2").request, profile_id: flTurbo.id, profile_digest: flTurbo.manifest_sha256 } });
  const base = retargetSegmentSampling(value, [flTurbo, flBase, refBase], "base");
  assert.equal(base.request.profile_id, "fl-base");
  assert.equal(base.request.parameters.steps, 20);
  assert.equal(base.request.parameters.lora_strength, 0);
  assert.equal(timelineRequiredCompiler(base), "h3_fl");
  assert.equal(timelineWorkflowModeLabel(base), "独立生成 · T2V");
  assert.equal(timelineWorkflowModeLabel({ ...base, continuation: "tail_frame" }), "尾帧约束 · FL2V");
  assert.equal(timelineWorkflowModeLabel({ ...base, continuation: "motion_context" }), "潜空间动作与声音续接 · Motion Context");
  const continued = { ...base, continuation: "previous_video" };
  const ref = retargetSegmentSampling(continued, [flTurbo, flBase, refBase], "base");
  assert.equal(ref.request.profile_id, "ref-base");
  assert.equal(timelineRequiredCompiler(ref), "h3_ref");
  assert.equal(timelineWorkflowModeLabel(ref), "上一段视频参考 · Ref2VA");
  assert.equal(ref.request.prompt, continued.request.prompt, "sampling must never rewrite Prompt");
});

test("timeline API helper uses every declared durable project endpoint", async () => {
  const calls = [];
  const fakeFetch = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : undefined });
    return new Response(JSON.stringify({ id: "p1", title: "x", status: "draft", segments: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const api = new VideoProjectApi(fakeFetch);
  const payload = serializeVideoProject(project());
  await api.list();
  await api.create(payload);
  await api.get("p1");
  await api.save("p1", payload);
  await api.run("p1");
  await api.runSelected("p1", ["s2", "s2", "s1"]);
  await api.stop("p1");
  await api.merge("p1");
  await api.runSegment("p1", "s2");
  await api.editReplicationSegment("p1", "s2", { expected_updated_at: 123.5, prompt: "exact prompt" });

  assert.deepEqual(calls.map(({ url, method }) => [method, url]), [
    ["GET", "/api/video-projects"],
    ["POST", "/api/video-projects"],
    ["GET", "/api/video-projects/p1"],
    ["PUT", "/api/video-projects/p1"],
    ["POST", "/api/video-projects/p1/run"],
    ["POST", "/api/video-projects/p1/run"],
    ["POST", "/api/video-projects/p1/stop"],
    ["POST", "/api/video-projects/p1/merge"],
    ["POST", "/api/video-projects/p1/segments/s2/run"],
    ["PATCH", "/api/video-projects/p1/segments/s2"],
  ]);
  assert.deepEqual(calls[3].body, payload);
  assert.deepEqual(calls[5].body, { segment_ids: ["s2", "s1"] });
  assert.deepEqual(calls[9].body, { expected_updated_at: 123.5, prompt: "exact prompt" });
});

test("default timeline API wraps receiver-sensitive native fetch", async () => {
  const nativeFetch = globalThis.fetch;
  let receiverIsGlobal = false;
  globalThis.fetch = async function (url) {
    receiverIsGlobal = Object.is(this, globalThis);
    assert.equal(url, "/api/video-projects");
    return new Response(JSON.stringify({ projects: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    assert.deepEqual(await new VideoProjectApi().list(), []);
    assert.equal(receiverIsGlobal, true);
  } finally {
    globalThis.fetch = nativeFetch;
  }
});

test("timeline sampling validation follows narrowed external profile limits", () => {
  const base = {
    id: "h3-fl-turbo", version: "1", display_name: "Narrow Base", output_type: "video",
    compiler: "h3_fl", manifest_sha256: "a".repeat(64), sampling_mode: "base", available: true,
    defaults: { duration: 7, steps: 12, lora_strength: 0, denoise: 1 },
    limits: { duration: [141 / 24, 192 / 24], steps: [8, 16], lora_strength: [0, 0], denoise: [0.4, 0.9] },
  };
  assert.deepEqual(profileBounds(base, "steps", [1, 100]), [8, 16]);
  assert.deepEqual(profileDurationOptions(base), [141 / 24, 158 / 24, 175 / 24, 192 / 24]);
  assert.equal(clampDurationToProfile(5.17, base), 141 / 24);
  const invalid = project({ segments: [segment("s1", { request: {
    ...segment("s1").request,
    parameters: { ...segment("s1").request.parameters, steps: 17, lora_strength: 0.2, denoise: 0.3 },
  } })] });
  const errors = validateVideoProject(invalid, [base]).join("\n");
  assert.match(errors, /steps must be an integer in 8\.\.16/);
  assert.match(errors, /LoRA strength must be in 0\.\.0/);
  assert.match(errors, /denoise must be 0\.4\.\.0\.9/);
  assert.match(errors, /duration must be in the profile range/);

  const turbo = {
    ...base, display_name: "Narrow Turbo", sampling_mode: "turbo4",
    defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
    limits: { duration: [124 / 24, 362 / 24], steps: [4, 20], lora_strength: [0.4, 0.9], denoise: [0.2, 1] },
  };
  const badTurbo = project({ segments: [segment("s1", { request: {
    ...segment("s1").request,
    parameters: { ...segment("s1").request.parameters, steps: 21, lora_strength: 1.2 },
  } })] });
  const turboErrors = validateVideoProject(badTurbo, [turbo]).join("\n");
  assert.match(turboErrors, /steps must be an integer in 4\.\.20/);
  assert.match(turboErrors, /LoRA strength must be in 0\.4\.\.0\.9/);
  const validTurbo = project({ segments: [segment("s1", { request: {
    ...segment("s1").request,
    parameters: { ...segment("s1").request.parameters, steps: 15, lora_strength: 0.7 },
  } })] });
  assert.doesNotMatch(validateVideoProject(validTurbo, [turbo]).join("\n"), /steps|LoRA strength/);
});

test("long-video profile selection pins id and version instead of picking an older duplicate", () => {
  const first = {
    id: "h3-ref", version: "1", display_name: "H3 Ref v1", output_type: "video",
    compiler: "h3_ref", manifest_sha256: "a".repeat(64), sampling_mode: "turbo4", available: true,
    defaults: { steps: 4 }, limits: { steps: [4, 20] },
  };
  const second = { ...first, version: "2", display_name: "H3 Ref v2", manifest_sha256: "b".repeat(64) };
  assert.equal(timelineProfileKey(second), "h3-ref@2");
  assert.equal(findTimelineProfile([first, second], "h3-ref@2"), second);
  assert.equal(findTimelineProfile([first, second], "h3-ref", "2"), second);
  assert.equal(findTimelineProfile([first, second], "h3-ref"), first);
  const current = { ...second, compatible_identities: [{ version: "1", manifest_sha256: first.manifest_sha256 }] };
  assert.equal(findTimelineProfile([current], "h3-ref", "1"), current);
});

test("adding any Ref2VA reference retargets to h3_ref while preserving and clamping tuned values", () => {
  const fl = {
    id: "h3-fl", version: "1", display_name: "H3 FL", output_type: "video", compiler: "h3_fl",
    manifest_sha256: "a".repeat(64), sampling_mode: "turbo4", available: true,
    defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
    limits: { duration: [124 / 24, 362 / 24], steps: [4, 30], lora_strength: [0, 1.5], denoise: [0.05, 1] },
  };
  const ref = {
    ...fl, id: "h3-ref", display_name: "H3 Ref", compiler: "h3_ref", manifest_sha256: "b".repeat(64),
    limits: { ...fl.limits, duration: [141 / 24, 260 / 24], steps: [6, 20], lora_strength: [0.4, 0.9], denoise: [0.3, 0.8] },
  };
  for (const assetId of ["c".repeat(32), "d".repeat(32), "e".repeat(32)]) {
    const source = segment("s1", { request: {
      ...segment("s1").request,
      profile_id: fl.id, profile_version: fl.version, profile_digest: fl.manifest_sha256,
      parameters: { ...segment("s1").request.parameters, duration: 362 / 24, steps: 25, lora_strength: 1.2, denoise: 0.1, seed: 99 },
      references: [{ asset_id: assetId, role: "reference" }],
    } });
    const next = retargetSegmentForReferences(source, [fl, ref]);
    assert.equal(next.request.profile_id, "h3-ref");
    assert.equal(next.request.parameters.duration, 260 / 24);
    assert.equal(next.request.parameters.steps, 20);
    assert.equal(next.request.parameters.lora_strength, 0.9);
    assert.equal(next.request.parameters.denoise, 0.3);
    assert.equal(next.request.parameters.seed, 99);
    assert.equal(next.request.prompt, source.request.prompt);
    assert.deepEqual(next.request.references, source.request.references);
  }
  const noReferences = segment("s1", { request: { ...segment("s1").request, profile_id: ref.id, profile_version: ref.version, profile_digest: ref.manifest_sha256 } });
  const withoutReference = retargetSegmentForReferences(noReferences, [fl, ref]);
  assert.equal(withoutReference.request.profile_id, "h3-fl", "removing the final reference restores the trusted FL compiler");
  assert.equal(withoutReference.request.prompt, noReferences.request.prompt);
  assert.equal(retargetSegmentCompiler(noReferences, [fl, ref], "h3_ref"), noReferences);
  assert.match(validateVideoProject(project({ segments: [noReferences] }), [fl, ref]).join("\n"), /h3_ref profile requires an explicit reference/);
  const backToFl = retargetSegmentCompiler(noReferences, [fl, ref], "h3_fl");
  assert.equal(backToFl.request.profile_id, "h3-fl", "changing continuation to none with no refs can restore same-mode FL");
});

test("prompt preview changes only explicit stable asset tokens", () => {
  const id = "c".repeat(32);
  const prompt = `Keep @nickname, punctuation! @{${id}} then mention ${id} without @ token.`;
  assert.equal(
    timelinePromptPreview(prompt, [{ asset_id: id, role: "reference" }], new Map([[id, "video"]])),
    `Keep @nickname, punctuation! <Video 1> then mention ${id} without @ token.`,
  );
});

test("source ranges reserve a Ref2VA slot and appear after explicit videos in readonly prompt numbering", () => {
  const explicit = "c".repeat(32);
  assert.equal(
    timelinePromptPreview(`Use @{${explicit}}`, [{ asset_id: explicit, role: "reference" }], new Map([[explicit, "video"]]), "previous_video", true),
    "Use <Video 1>; <Video 2>; <Video 3>",
  );
  const refProfile = {
    id: "h3-ref", version: "1", display_name: "H3 Ref", output_type: "video", compiler: "h3_ref",
    manifest_sha256: "b".repeat(64), sampling_mode: "turbo4", available: true,
    defaults: {}, limits: {},
  };
  const sourceId = "d".repeat(32);
  const value = project({
    storyboard: { source_asset_id: sourceId, fps: 30, frame_count: 300, cut_frames: [] },
    segments: [segment("s1", {
      source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 },
      request: { ...segment("s1").request, profile_id: refProfile.id, profile_version: refProfile.version, profile_digest: refProfile.manifest_sha256, references: Array.from({ length: 12 }, (_, index) => ({ asset_id: index.toString(16).repeat(32), role: "reference" })) },
    })],
  });
  const errors = validateVideoProject(value, [refProfile]).join("\n");
  assert.match(errors, /exceeds the 12-file reference budget/);
  assert.doesNotMatch(errors, /h3_ref profile requires/);
});

test("timeline preflights H3 modality counts, per-clip duration and aggregate duration", () => {
  const refProfile = {
    id: "h3-ref", version: "1", display_name: "H3 Ref", output_type: "video", compiler: "h3_ref",
    manifest_sha256: "b".repeat(64), sampling_mode: "turbo4", available: true,
    defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
    limits: { duration: [124 / 24, 362 / 24], steps: [4, 20], lora_strength: [0, 1], denoise: [0.05, 1] },
  };
  const makeAsset = (id, kind, duration, hasAudio = false) => ({ id, kind, media: { duration, has_audio: hasAudio } });
  const videos = ["1", "2", "3", "4"].map((digit) => makeAsset(digit.repeat(32), "video", 4, true));
  const audios = ["5", "6", "7", "8"].map((digit) => makeAsset(digit.repeat(32), "audio", 4));
  const refSegment = (references) => segment("s1", { request: {
    ...segment("s1").request,
    profile_id: refProfile.id, profile_version: refProfile.version, profile_digest: refProfile.manifest_sha256,
    references,
  } });
  assert.match(validateVideoProject(project({ segments: [refSegment(audios.slice(0, 1).map((asset) => ({ asset_id: asset.id, role: "reference" })))] }), [refProfile], audios).join("\n"), /audio-only/);
  assert.match(validateVideoProject(project({ segments: [refSegment(videos.map((asset) => ({ asset_id: asset.id, role: "reference" })))] }), [refProfile], videos).join("\n"), /at most three video references/);
  assert.match(validateVideoProject(project({ segments: [refSegment(audios.map((asset) => ({ asset_id: asset.id, role: "reference" })))] }), [refProfile], audios).join("\n"), /at most three selected audio references/);
  const longVideos = [makeAsset("9".repeat(32), "video", 8), makeAsset("a".repeat(32), "video", 8)];
  assert.match(validateVideoProject(project({ segments: [refSegment(longVideos.map((asset) => ({ asset_id: asset.id, role: "reference" })))] }), [refProfile], longVideos).join("\n"), /videos may total at most 15/);
  const mixedAudio = [makeAsset("b".repeat(32), "video", 8, true), makeAsset("c".repeat(32), "audio", 8)];
  assert.match(validateVideoProject(project({ segments: [refSegment([{ asset_id: mixedAudio[0].id, role: "reference", include_audio: true }, { asset_id: mixedAudio[1].id, role: "reference" }])] }), [refProfile], mixedAudio).join("\n"), /audio may total at most 15/);
  const tooShort = [makeAsset("d".repeat(32), "video", 1.5)];
  assert.match(validateVideoProject(project({ segments: [refSegment([{ asset_id: tooShort[0].id, role: "reference" }])] }), [refProfile], tooShort).join("\n"), /between 2 and 15 seconds/);
});


test("timeline round-trip preserves replication recipe without leaking execution data", () => {
  const recipe = { type: "replication", version: "h3.replication/v1", output: { frames: 1440 } };
  const saved = serializeVideoProject(project({ recipe }));
  assert.deepEqual(saved.recipe, recipe);
  saved.recipe.output.frames = 1;
  assert.equal(recipe.output.frames, 1440);
  assert.equal(saved.status, undefined);
});


test("project API exposes conflict codes without losing server details", async () => {
  const api = new VideoProjectApi(async () => new Response(JSON.stringify({error: {code: "project_changed", message: "reload"}}), {status: 409}));
  await assert.rejects(api.editReplicationSegment("p", "s", {expected_updated_at: 1, prompt: "exact"}), (error) => error.status === 409 && error.code === "project_changed" && error.message === "reload");
});
