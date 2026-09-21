import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { encodeMicrophoneWav, MAX_MICROPHONE_SECONDS, recordedMicrophoneFile } from "../app/microphone-audio.ts";
import { translateUiText } from "../app/ui-language.ts";

test("microphone PCM export is a signed, correctly sized 16-bit mono WAV", async () => {
  const wav = encodeMicrophoneWav({
    length: 4, sampleRate: 48_000, numberOfChannels: 2,
    getChannelData(index) { return index === 0 ? new Float32Array([1, -1, 0.5, 0]) : new Float32Array([1, -1, -0.5, 0]); },
  });
  assert.equal(wav.type, "audio/wav");
  const bytes = await wav.arrayBuffer();
  const view = new DataView(bytes);
  assert.equal(bytes.byteLength, 52);
  assert.equal(Buffer.from(bytes, 0, 4).toString(), "RIFF");
  assert.equal(Buffer.from(bytes, 8, 4).toString(), "WAVE");
  assert.equal(Buffer.from(bytes, 36, 4).toString(), "data");
  assert.equal(view.getUint32(4, true), 44);
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 48_000);
  assert.equal(view.getUint32(28, true), 96_000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 8);
  assert.deepEqual([0, 1, 2, 3].map((index) => view.getInt16(44 + index * 2, true)), [32767, -32768, 0, 0]);
});

test("microphone WAV refuses invalid or oversized decoded audio", () => {
  const audio = { length: 1, sampleRate: 48_000, numberOfChannels: 1, getChannelData: () => new Float32Array([0]) };
  assert.throws(() => encodeMicrophoneWav({ ...audio, sampleRate: 0 }), /录音音频数据无效/);
  assert.throws(() => encodeMicrophoneWav({ ...audio, length: 140_000_000 }), /录音过大/);
  assert.equal(MAX_MICROPHONE_SECONDS, 300);
});

test("microphone container decoding closes AudioContext and names the WAV for upload", async () => {
  const original = globalThis.AudioContext;
  let closed = 0;
  globalThis.AudioContext = class {
    async decodeAudioData() { return { length: 2, sampleRate: 44_100, numberOfChannels: 1, getChannelData: () => new Float32Array([0.25, -0.25]) }; }
    async close() { closed += 1; }
  };
  try {
    const file = await recordedMicrophoneFile(new Blob(["recorded"]), new Date("2026-09-21T04:05:06Z"));
    assert.equal(file.name, "microphone-20260921040506.wav");
    assert.equal(file.type, "audio/wav");
    assert.equal(file.size, 48);
    assert.equal(closed, 1);
    await assert.rejects(() => recordedMicrophoneFile(new Blob([])), /没有录到声音/);
    assert.equal(closed, 1);
  } finally { globalThis.AudioContext = original; }
});

test("microphone UI requires explicit upload and releases the captured stream on close", async () => {
  const [recorder, drawer] = await Promise.all([
    readFile(new URL("../app/microphone-recorder.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/voice-studio.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(recorder, /navigator\.mediaDevices\.getUserMedia\(\{ audio: true, video: false \}\)/);
  assert.match(recorder, /new MediaRecorder\(stream\)/);
  assert.match(recorder, /recorder\.onstop = \(\) =>/);
  assert.match(recorder, /recorder\.onstop = \(\) => \{\s+clearTimers\(\);\s+stopStream\(streamRef\.current\);/);
  assert.match(recorder, /recordedMicrophoneFile\(recorded\)/);
  assert.match(recorder, /if \(await onRecorded\(recordedFile\)\)/);
  assert.match(recorder, /stopStream\(streamRef\.current\)/);
  assert.match(recorder, /URL\.revokeObjectURL/);
  assert.match(drawer, /<MicrophoneRecorder/);
  assert.match(drawer, /!microphonePending && !submitting/);
  assert.equal(translateUiText("话筒录音", "en"), "Microphone Recording");
  assert.equal(translateUiText("使用这段录音", "en"), "Use This Recording");
});
