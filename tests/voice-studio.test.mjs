import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  cancelVoiceTask, deleteVoiceTask, getVoiceCapabilities, isSupportedVoiceAudio,
  listVoiceTasks, parseVoiceTask, submitVoiceTask, uploadVoiceAudio, voiceDownloadUrl,
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
  assert.throws(() => voiceDownloadUrl("https://elsewhere.invalid"));
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

test("voice UI API uploads audio, checks capability, submits and manages durable tasks", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, init = {}) => {
    calls.push({ path, init });
    if (path === "/api/voice/capabilities") return reply({ engines: [{ id: "vevo2", available: false, reason: "missing runtime" }, { id: "yingmusic", available: true, mode: "separate_convert_remix" }] });
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
    const asset = await uploadVoiceAudio(new File(["ID3", new Uint8Array([1, 2, 3])], "song.mp3", { type: "audio/mpeg" }));
    assert.equal(asset.id, sourceId);
    assert.equal(asset.kind, "audio");
    assert.ok(calls.find(({ path, init }) => path === "/api/assets" && init.body instanceof FormData));
    const submitted = await submitVoiceTask("yingmusic", sourceId, referenceId);
    assert.equal(submitted.id, taskId);
    const request = JSON.parse(calls.find(({ path, init }) => path === "/api/voice/tasks" && init.method === "POST").init.body);
    assert.deepEqual([request.engine, request.source_asset_id, request.reference_asset_id], ["yingmusic", sourceId, referenceId]);
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
  assert.match(drawer, /submitVoiceTask\(engine, sourceId, referenceId\)/);
  assert.match(drawer, /cancelVoiceTask\(task\.id\)/);
  assert.match(drawer, /deleteVoiceTask\(task\.id\)/);
  assert.match(drawer, /audio controls preload="none" src=\{voiceDownloadUrl\(task\.id\)\}/);
  assert.match(drawer, /download=\{`voice-/);
  assert.equal(translateUiText("换声", "en"), "Voice Conversion");
  assert.equal(translateUiText("开始换声", "en"), "Convert Voice");
});
