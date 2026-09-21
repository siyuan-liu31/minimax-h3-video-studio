import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  cancelVoiceTask, deleteVoiceTask, getVoiceCapabilities, isSupportedVoiceAudio,
  listVoiceTasks, parseVoiceTask, submitVoiceTask, uploadVoiceAudio, voiceDownloadUrl, voicePreviewUrl,
  validateYingMusicParameters, validateYingMusicOutputOptions,
} from "../app/voice-studio-api.ts";
import { translateUiText } from "../app/ui-language.ts";

const sourceId = "a".repeat(32);
const referenceId = "b".repeat(32);
const taskId = "c".repeat(32);
const task = {
  id: taskId, engine: "yingmusic", source_asset_id: sourceId, reference_asset_id: referenceId,
  status: "queued", stage: "waiting_for_gpu", progress: 0, created_at: 100,
  queue_position: 2, queue_reason: "waiting_for_video_task",
};

function reply(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

test("voice task receipts accept only safe IDs, engines and statuses", () => {
  assert.equal(parseVoiceTask({ ...task, id: "../escape" }), undefined);
  assert.equal(parseVoiceTask({ ...task, engine: "unknown" }), undefined);
  assert.equal(parseVoiceTask({ ...task, status: "unknown" }), undefined);
  assert.equal(voiceDownloadUrl(taskId), `/api/voice/tasks/${taskId}/download`);
  assert.equal(voiceDownloadUrl(taskId, "dry_vocal"), `/api/voice/tasks/${taskId}/download?track=dry_vocal`);
  assert.equal(voicePreviewUrl(taskId, "dry_vocal"), `/api/voice/tasks/${taskId}/preview?track=dry_vocal`);
  assert.throws(() => voiceDownloadUrl("https://elsewhere.invalid"));
  assert.throws(() => voicePreviewUrl(taskId, "../escape"));
  assert.deepEqual(parseVoiceTask(task), {
    id: taskId, engine: "yingmusic", sourceAssetId: sourceId, referenceAssetId: referenceId,
    status: "queued", stage: "waiting_for_gpu", progress: 0, createdAt: 100,
    queuePosition: 2, queueReason: "waiting_for_video_task",
  });
});

test("voice upload advertises only formats recognized by the server signature gate", () => {
  for (const name of ["voice.wav", "voice.FLAC", "voice.ogg", "voice.mp3"]) assert.equal(isSupportedVoiceAudio({ name, type: "" }), true);
  for (const name of ["voice.m4a", "voice.aac", "video.mp4", "fake.mp3.exe"]) assert.equal(isSupportedVoiceAudio({ name, type: "audio/mpeg" }), false);
});

test("YingMusic tuning validates ranges and preserves effective seed receipts", () => {
  assert.deepEqual(validateYingMusicParameters({ diffusion_steps: 100, inference_cfg_rate: 0.7, seed: -1 }), { diffusion_steps: 100, inference_cfg_rate: 0.7, seed: -1 });
  for (const parameters of [
    { diffusion_steps: 9, inference_cfg_rate: 0.7, seed: 1 },
    { diffusion_steps: 100.5, inference_cfg_rate: 0.7, seed: 1 },
    { diffusion_steps: 100, inference_cfg_rate: 2.1, seed: 1 },
    { diffusion_steps: 100, inference_cfg_rate: 0.7, seed: 4294967296 },
  ]) assert.throws(() => validateYingMusicParameters(parameters));
  assert.deepEqual(parseVoiceTask({ ...task, parameters: { diffusion_steps: 75, inference_cfg_rate: 0.9, seed: 42 } }).parameters,
    { diffusion_steps: 75, inference_cfg_rate: 0.9, seed: 42 });
});

test("YingMusic output options and retained tracks are parsed safely", () => {
  const options = { include_stems: true, echo: false, reverb: true };
  assert.deepEqual(validateYingMusicOutputOptions(options), options);
  assert.throws(() => validateYingMusicOutputOptions({ ...options, echo: 1 }));
  const parsed = parseVoiceTask({ ...task, status: "completed", output_options: options,
    output: { filename: "converted.wav", size: 100 },
    outputs: { mix: { filename: "converted.wav", size: 100 }, dry_vocal: { filename: "dry-vocal.wav", size: 90 }, unknown: { filename: "other.wav", size: 12 } },
  });
  assert.deepEqual(parsed.outputOptions, options);
  assert.deepEqual(Object.keys(parsed.outputs), ["mix", "dry_vocal"]);
});

test("voice UI API uploads audio, checks capability, submits and manages durable tasks", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, init = {}) => {
    calls.push({ path, init });
    if (path === "/api/voice/capabilities") return reply({ engines: [{ id: "vevo2", available: false, reason: "missing runtime" }, { id: "yingmusic", available: true, mode: "separate_convert_remix", tuning: { diffusion_steps: { default: 100, minimum: 10, maximum: 200 }, inference_cfg_rate: { default: 0.7, minimum: 0, maximum: 2 }, seed: { default: -1, minimum: -1, maximum: 4294967295 } }, output_options: { include_stems: false, echo: true, reverb: true } }] });
    if (path === "/api/assets") return reply({ asset: { id: sourceId, kind: "audio", filename: "song.mp3", content_url: `/api/assets/${sourceId}/content` } }, 201);
    if (path === "/api/voice/tasks" && init.method === "POST") return reply(task, 202);
    if (path === "/api/voice/tasks") return reply({ items: [task, { ...task, id: "invalid" }] });
    if (path === `/api/voice/tasks/${taskId}/cancel`) return reply({ ...task, status: "canceled" }, 202);
    if (path === `/api/voice/tasks/${taskId}` && init.method === "DELETE") return reply({ deleted: true });
    throw new Error(`unexpected request ${path}`);
  };
  try {
    const capabilities = await getVoiceCapabilities();
    assert.deepEqual(capabilities.map(({ id, available }) => [id, available]), [["vevo2", false], ["yingmusic", true]]);
    assert.equal(capabilities[1].tuning.diffusion_steps.default, 100);
    assert.deepEqual(capabilities[1].outputOptions, { include_stems: false, echo: true, reverb: true });
    const asset = await uploadVoiceAudio(new File(["ID3", new Uint8Array([1, 2, 3])], "song.mp3", { type: "audio/mpeg" }));
    assert.equal(asset.id, sourceId);
    assert.equal(asset.kind, "audio");
    assert.ok(calls.find(({ path, init }) => path === "/api/assets" && init.body instanceof FormData));
    const submitted = await submitVoiceTask("yingmusic", sourceId, referenceId, { diffusion_steps: 75, inference_cfg_rate: 0.9, seed: -1 }, { include_stems: true, echo: false, reverb: false });
    assert.equal(submitted.id, taskId);
    const request = JSON.parse(calls.find(({ path, init }) => path === "/api/voice/tasks" && init.method === "POST").init.body);
    assert.deepEqual([request.engine, request.source_asset_id, request.reference_asset_id], ["yingmusic", sourceId, referenceId]);
    assert.deepEqual([request.diffusion_steps, request.inference_cfg_rate, request.seed], [75, 0.9, -1]);
    assert.deepEqual(request.output_options, { include_stems: true, echo: false, reverb: false });
    assert.match(request.request_id, /^[0-9a-f]{32}$/);
    assert.equal((await listVoiceTasks()).length, 1);
    assert.equal((await cancelVoiceTask(taskId)).status, "canceled");
    await deleteVoiceTask(taskId);
    assert.equal(calls.at(-1).init.method, "DELETE");
  } finally { globalThis.fetch = originalFetch; }
});

test("voice API reports backend errors without claiming GPU inference ran", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => reply({ error: { message: "missing configured voice runtime" } }, 503);
  try { await assert.rejects(() => getVoiceCapabilities(), /missing configured voice runtime/); }
  finally { globalThis.fetch = originalFetch; }
});

test("voice drawer is wired to upload/drop, persisted task polling, cancellation, preview and download", async () => {
  const [studio, drawer] = await Promise.all([
    readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/voice-studio.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(studio, /railPanel === "voice" && <VoiceStudio/);
  assert.match(drawer, /onDrop=\{drop\}/);
  assert.match(drawer, /uploadVoiceAudio\(file, controller\.signal\)/);
  assert.match(drawer, /getVoiceCapabilities\(controller\.signal\)/);
  assert.match(drawer, /listVoiceTasks\(signal\)/);
  assert.match(drawer, /window\.setInterval/);
  assert.match(drawer, /submitVoiceTask\(engine, sourceId, referenceId, currentParameters\(\),/);
  assert.match(drawer, /cancelVoiceTask\(task\.id\)/);
  assert.match(drawer, /deleteVoiceTask\(task\.id\)/);
  assert.match(drawer, /audio key=\{`\$\{task\.id\}:\$\{selected\}`\} controls preload="none" src=\{voicePreviewUrl\(task\.id, selected\)\}/);
  assert.match(drawer, /href=\{voiceDownloadUrl\(task\.id, selected\)\}/);
  assert.match(drawer, /download=\{`voice-/);
  assert.equal(translateUiText("换声", "en"), "Voice Conversion");
  assert.equal(translateUiText("开始换声", "en"), "Convert Voice");
  assert.equal(translateUiText("种子", "en"), "Seed");
  assert.equal(translateUiText("试听与导出音轨", "en"), "Track to preview and export");
  assert.equal(translateUiText("最终混音加入回声", "en"), "Add echo to final mix");
  assert.match(drawer, /<span>种子<\/span>/);
});
