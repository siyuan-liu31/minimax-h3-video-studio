import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildVideoDirectorContract,
  graphRoleForVideoAsset,
  normalizeVideoDirectorMode,
  videoDirectorReferenceLabels,
  videoDirectorPayload,
  VIDEO_DIRECTOR_MODES,
} from "../app/studio-video-mode.ts";

const studio = await readFile(new URL("../app/studio.tsx", import.meta.url), "utf8");
const controls = await readFile(new URL("../app/studio-video-mode-controls.tsx", import.meta.url), "utf8");

const image = (nodeId, assetId = nodeId.repeat(32).slice(0, 32), role = "reference") => ({ nodeId, assetId, kind: "image", label: `${nodeId}.png`, role });
const video = (nodeId, assetId = nodeId.repeat(32).slice(0, 32)) => ({ nodeId, assetId, kind: "video", label: `${nodeId}.mp4`, role: "reference" });
const audio = (nodeId, assetId = nodeId.repeat(32).slice(0, 32)) => ({ nodeId, assetId, kind: "audio", label: `${nodeId}.wav`, role: "reference" });

test("legacy snapshots migrate without dropping the old FL/Ref routing intent", () => {
  assert.equal(normalizeVideoDirectorMode("rv2v"), "rv2v");
  assert.equal(normalizeVideoDirectorMode(undefined, "Ref2VA"), "r2v");
  assert.equal(normalizeVideoDirectorMode(undefined, "FL2VA"), "auto");
  assert.equal(normalizeVideoDirectorMode("unknown", "auto"), "auto");
  assert.match(studio, /V7_STORAGE_KEY/);
  assert.match(studio, /LEGACY_STORAGE_KEYS as DOCUMENT_LEGACY_STORAGE_KEYS/);
  assert.match(studio, /const STORAGE_KEY = V7_STORAGE_KEY/);
  assert.match(studio, /const LEGACY_STORAGE_KEYS = DOCUMENT_LEGACY_STORAGE_KEYS/);
  assert.match(studio, /localStorage\.getItem\(STORAGE_KEY\) \?\? LEGACY_STORAGE_KEYS\.map/);
  assert.match(studio, /const restored = raw \? parseCanvasDocument\(raw\) : undefined/);
  assert.match(studio, /runtimesFromDocument\(document\)/);
});

test("Auto resolves only ordinary T2V/I2V/FL2V/R2V and never silently elects a source video", () => {
  assert.equal(buildVideoDirectorContract("auto", "", []).resolvedMode, "t2v");
  assert.equal(buildVideoDirectorContract("auto", "", [image("a", undefined, "first_frame")]).resolvedMode, "i2v");
  assert.equal(buildVideoDirectorContract("auto", "", [image("a", undefined, "last_frame")]).resolvedMode, "fl2v");
  assert.equal(buildVideoDirectorContract("auto", "", [image("a")]).resolvedMode, "r2v");
  assert.equal(buildVideoDirectorContract("auto", "", [image("a", undefined, "first_frame"), image("b", undefined, "last_frame")]).resolvedMode, "fl2v");
  assert.equal(buildVideoDirectorContract("auto", video("v").assetId, [video("v")]).resolvedMode, "r2v");
});

test("Director modes compile to the pinned low-level modes and payload keys", () => {
  const expectations = [
    ["t2v", "text", "h3_fl"],
    ["i2v", "fl2va", "h3_fl"],
    ["fl2v", "fl2va", "h3_fl"],
    ["r2v", "ref2va", "h3_ref"],
    ["v2v", "ref2va", "h3_ref"],
    ["rv2v", "ref2va", "h3_ref"],
  ];
  for (const [mode, lowLevel, compiler] of expectations) {
    const source = video("v");
    const assets = mode === "t2v" ? [] : mode === "i2v" ? [image("a")] : mode === "fl2v" ? [image("a", undefined, "last_frame")] : mode === "r2v" ? [image("a")] : [source];
    const contract = buildVideoDirectorContract(mode, source.assetId, assets);
    assert.equal(contract.lowLevelMode, lowLevel);
    assert.equal(contract.compiler, compiler);
    assert.equal(videoDirectorPayload(contract).director_mode, mode);
    assert.equal(videoDirectorPayload(contract).mode, lowLevel);
  }
});

test("T2V, I2V, FL2V and R2V expose exact local reference errors", () => {
  assert.match(buildVideoDirectorContract("t2v", "", [image("a")]).errors.join("\n"), /不能连接参考素材/);
  assert.match(buildVideoDirectorContract("i2v", "", []).errors.join("\n"), /只允许一张/);
  assert.equal(buildVideoDirectorContract("i2v", "", [image("a")]).errors.length, 0);
  assert.equal(buildVideoDirectorContract("fl2v", "", [image("a", undefined, "first_frame")]).errors.length, 0);
  assert.equal(buildVideoDirectorContract("fl2v", "", [image("a", undefined, "last_frame")]).errors.length, 0);
  assert.equal(buildVideoDirectorContract("fl2v", "", [image("a", undefined, "first_frame"), image("b", undefined, "last_frame")]).errors.length, 0);
  assert.match(buildVideoDirectorContract("fl2v", "", [image("a")]).errors.join("\n"), /端点角色/);
  assert.match(buildVideoDirectorContract("fl2v", "", [image("a", undefined, "first_frame"), image("b", undefined, "first_frame")]).errors.join("\n"), /角色不能重复/);
  assert.match(buildVideoDirectorContract("r2v", "", []).errors.join("\n"), /至少需要一个/);
  assert.match(buildVideoDirectorContract("r2v", "", [audio("a")]).errors.join("\n"), /不支持只有音频/);
  assert.equal(buildVideoDirectorContract("r2v", "", [image("a"), video("v"), audio("u")]).errors.length, 0);
  assert.match(buildVideoDirectorContract("r2v", "", Array.from({ length: 10 }, (_, index) => image(`i${index}`))).errors.join("\n"), /最多连接 9 张图片/);
  assert.match(buildVideoDirectorContract("r2v", "", [image("i"), ...Array.from({ length: 4 }, (_, index) => video(`v${index}`))]).errors.join("\n"), /最多连接 3 个视频/);
  assert.match(buildVideoDirectorContract("r2v", "", [image("i"), ...Array.from({ length: 4 }, (_, index) => audio(`a${index}`))]).errors.join("\n"), /最多连接 3 个音频/);
  assert.match(buildVideoDirectorContract("r2v", "", [image("i"), ...Array.from({ length: 3 }, (_, index) => ({ ...video(`v${index}`), includeAudio: true })), audio("a")]).errors.join("\n"), /最多连接 3 个音频/);
});

test("V2V and RV2V require an explicit connected source, put it first, and reject forbidden refs", () => {
  const source = video("s", "1".repeat(32));
  const second = video("v", "2".repeat(32));
  assert.match(buildVideoDirectorContract("v2v", "", [source]).errors.join("\n"), /必须明确选择/);
  assert.match(buildVideoDirectorContract("v2v", "3".repeat(32), [source]).errors.join("\n"), /未连接/);
  assert.equal(buildVideoDirectorContract("v2v", source.assetId, [source]).errors.length, 0);
  assert.match(buildVideoDirectorContract("v2v", source.assetId, [source, image("a")]).errors.join("\n"), /只允许源视频/);
  const rv2v = buildVideoDirectorContract("rv2v", source.assetId, [image("a"), source, audio("u")]);
  assert.equal(rv2v.errors.length, 0);
  assert.equal(rv2v.orderedAssets[0].nodeId, source.nodeId);
  assert.deepEqual(rv2v.references.map((item) => item.kind), ["image", "audio"]);
  assert.deepEqual(videoDirectorPayload(rv2v), { director_mode: "rv2v", mode: "ref2va", source_asset_id: source.assetId });
  assert.equal(buildVideoDirectorContract("rv2v", source.assetId, [source]).errors.length, 0, "RV2V additional image/audio refs are optional");
  assert.match(buildVideoDirectorContract("rv2v", source.assetId, [source, second]).errors.join("\n"), /只允许一条源视频/);
  assert.match(buildVideoDirectorContract("v2v", source.assetId, [{ ...source, includeAudio: true }]).errors.join("\n"), /不支持把源视频音轨作为参考/);
});

test("graph roles are determined by explicit creative semantics without rewriting prompt text", () => {
  const first = image("a", undefined, "first_frame");
  const last = image("b", undefined, "last_frame");
  const fl = buildVideoDirectorContract("fl2v", "", [first, last]);
  assert.equal(graphRoleForVideoAsset(fl, first), "first_frame");
  assert.equal(graphRoleForVideoAsset(fl, last), "last_frame");
  assert.equal(graphRoleForVideoAsset(buildVideoDirectorContract("fl2v", "", [last]), last), "last_frame");
  const source = video("s");
  const rv = buildVideoDirectorContract("rv2v", source.assetId, [source, first]);
  assert.equal(graphRoleForVideoAsset(rv, source), "reference");
  assert.match(studio, /onModeChange=\{\(directorMode\) => updateNodeVideo\(\(current\) => \(\{ \.\.\.current, directorMode \}\)\)\}/);
  assert.doesNotMatch(controls, /setPrompt|onPrompt|prompt:/);
});

test("native audio labels number every selected video soundtrack before standalone audio", () => {
  const standalone = audio("a");
  const withSoundtrack = { ...video("v"), includeAudio: true };
  const contract = buildVideoDirectorContract("r2v", "", [standalone, withSoundtrack, image("i")]);
  const labels = Object.fromEntries(videoDirectorReferenceLabels(contract).map((item) => [item.nodeId, item.tag]));
  assert.equal(labels.v, "<Video 1> · <Audio 1> (视频音轨)");
  assert.equal(labels.a, "<Audio 2>");
  assert.equal(labels.i, "<Picture 1>");
});

test("V2V/RV2V graph keeps the source node and edge at reference_index zero", () => {
  assert.match(studio, /referenceOrder = new Map\(targetContract\.orderedAssets/);
  assert.match(studio, /videoReferenceIndexBySource = new Map\(targetContract\.orderedAssets/);
  assert.doesNotMatch(studio, /targetContract\.orderedAssets\.filter\([^)]*source/);
  assert.match(studio, /reference_index: videoReferenceIndexBySource\.get\(node\.id\)/);
  assert.match(studio, /reference_index: videoReferenceIndexBySource\.get\(edge\.source\)/);
  assert.match(studio, /\.\.\.videoDirectorPayload\(videoDirectorContract\)/);
});

test("main-canvas and inspector UI expose mode, source, errors, tags and compiled workflow actions", () => {
  assert.deepEqual(VIDEO_DIRECTOR_MODES, ["auto", "t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v"]);
  assert.match(controls, /VIDEO_DIRECTOR_MODES\.map/);
  assert.match(controls, /固定映射为 &lt;Video 1&gt;/);
  assert.match(controls, /只改变工作流，不修改 Prompt/);
  assert.match(controls, /role="alert"/);
  assert.match(controls, /\/api\/workflows\/director\/\$\{workflowMode\}/);
  assert.match(controls, /\?download=1/);
  assert.match(controls, /模式合同\/官方节点模板/);
  assert.match(controls, /不冒充某次生成任务的实际执行图/);
  assert.match(studio, /源视频 <b/);
  assert.match(studio, /&lt;Video 1&gt;/);
  assert.match(studio, /video-mode-node-error/);
  assert.match(studio, /查看模式合同/);
  assert.match(studio, /function ActualWorkflowActions/);
  assert.match(studio, /\/api\/jobs\/\$\{encodeURIComponent\(job\.id\)\}\/workflow/);
  assert.match(studio, /查看实际工作流/);
  assert.match(studio, /下载实际工作流/);
  assert.match(studio, /workflowSha256 && <small/);
});
