import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { remoteAssetToLibraryItem } from "../app/studio-library.ts";
import { buildStoryboardDraft } from "../app/video-director-model.ts";
import { selectedTimelineRunPlan, timelinePromptPreview, validateVideoProject } from "../app/video-project.ts";
import { VideoProjectApi } from "../app/video-project-api.ts";

const timelineSource = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
const serverSource = await readFile(new URL("../server/video_projects.py", import.meta.url), "utf8");
const serverTests = await readFile(new URL("../server/tests/test_video_projects.py", import.meta.url), "utf8");

const fl = {
  id: "h3-fl", version: "1", display_name: "H3 FL", output_type: "video", compiler: "h3_fl",
  manifest_sha256: "a".repeat(64), sampling_mode: "turbo4", available: true,
  defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
  limits: { duration: [124 / 24, 362 / 24], steps: [4, 20], lora_strength: [0, 1], denoise: [0.05, 1] },
};
const ref = { ...fl, id: "h3-ref", display_name: "H3 Ref", compiler: "h3_ref", manifest_sha256: "b".repeat(64) };

function shot(id, profile = fl, prompt = `shot ${id}`, referenceId) {
  return {
    id, continuation: "none", status: "draft", attempts: [],
    request: {
      prompt, prompt_mode: "preserve_tags_only", parts: {},
      parameters: { aspect_ratio: "16:9", duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1, seed: -1 },
      profile_id: profile.id, profile_version: profile.version, profile_digest: profile.manifest_sha256,
      references: referenceId ? [{ asset_id: referenceId, role: "reference" }] : [],
    },
  };
}

test("Cycle 13 cross-layer contract matrix", async (t) => {
  await t.test("30fps normalized source uses source frames in browser and server contracts", () => {
    const id = "c".repeat(32);
    const asset = remoteAssetToLibraryItem({
      id, kind: "video", filename: "source.mp4",
      media: { duration: 10, fps: 24, reference_fps: 24, source_fps: 30, frame_count: 300, normalized_to_24fps: true },
    });
    assert.equal(asset?.media.source_fps, 30);
    const draft = buildStoryboardDraft([shot("A")], { source_asset_id: id, fps: asset.media.source_fps, frame_count: asset.media.frame_count }, [], [fl, ref], () => "new");
    assert.deepEqual(draft.segments[0].source_range, { asset_id: id, start_frame: 0, end_frame: 300, fps: 30 });
    assert.match(serverSource, /source_fps[\s\S]*storyboard_source_mismatch/);
  });

  await t.test("source range is an implicit Ref2VA input with a visible tag and reserved slot", () => {
    const sourceId = "d".repeat(32);
    const draft = buildStoryboardDraft([shot("A")], { source_asset_id: sourceId, fps: 30, frame_count: 300 }, [], [fl, ref], () => "new");
    assert.equal(draft.segments[0].request.profile_id, ref.id);
    assert.equal(timelinePromptPreview("unchanged", [], new Map(), "none", true), "unchanged; <Video 1>");
    const project = { title: "matrix", status: "draft", storyboard: draft.storyboard, segments: [{ ...draft.segments[0], request: { ...draft.segments[0].request, references: Array.from({ length: 12 }, (_, index) => ({ asset_id: index.toString(16).repeat(32), role: "reference" })) } }] };
    assert.match(validateVideoProject(project, [fl, ref]).join("\n"), /exceeds the 12-file reference budget/);
    assert.match(serverSource, /pixel_continuation = continuation in \{"tail_frame", "previous_video"\}/);
    assert.match(serverSource, /reserved_references = int\(pixel_continuation\) \+ int\(source_range is not None\)/);
    assert.match(serverSource, /_with_source_range_reference/);
  });

  await t.test("re-cutting A/B yields A/A/B without prompt or reference leakage", () => {
    const sourceId = "e".repeat(32);
    const aRef = "1".repeat(32);
    const bRef = "2".repeat(32);
    const a = { ...shot("A", ref, "prompt A", aRef), source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 } };
    const b = { ...shot("B", ref, "prompt B", bRef), source_range: { asset_id: sourceId, start_frame: 300, end_frame: 600, fps: 30 } };
    const draft = buildStoryboardDraft([a, b], { source_asset_id: sourceId, fps: 30, frame_count: 600 }, [150, 300], [ref], () => "A2");
    assert.deepEqual(draft.segments.map((item) => item.id), ["A", "A2", "B"]);
    assert.deepEqual(draft.segments.map((item) => item.request.prompt), ["prompt A", "prompt A", "prompt B"]);
    assert.deepEqual(draft.segments.map((item) => item.request.references[0].asset_id), [aRef, aRef, bRef]);
  });

  await t.test("a sub-two-second crop saves only and cannot silently become an H3 video reference", () => {
    assert.match(timelineSource, /if \(sourceDuration < 2\)[\s\S]*Prompt 和参考保持不变/);
    assert.match(timelineSource, /await onAssetCreated\(saved\);[\s\S]*if \(sourceDuration < 2\)/);
    assert.doesNotMatch(timelineSource, /appendTimelineReference\(segment\.request\.references, saved/);
    assert.match(timelineSource, /来源区间隐式引用该视频/);
  });

  await t.test("reference modality counts and video/audio duration budgets fail before run", () => {
    const videoA = { id: "3".repeat(32), kind: "video", media: { duration: 8, has_audio: true } };
    const videoB = { id: "4".repeat(32), kind: "video", media: { duration: 8, has_audio: true } };
    const audio = { id: "5".repeat(32), kind: "audio", media: { duration: 8 } };
    const segment = shot("A", ref);
    segment.request.references = [
      { asset_id: videoA.id, role: "reference", include_audio: true },
      { asset_id: videoB.id, role: "reference" },
      { asset_id: audio.id, role: "reference" },
    ];
    const errors = validateVideoProject({ title: "matrix", status: "draft", segments: [segment] }, [ref], [videoA, videoB, audio]).join("\n");
    assert.match(errors, /videos may total at most 15/);
    assert.match(errors, /audio may total at most 15/);
  });

  await t.test("selected-run request exposes and persists an automatically dependency-closed plan", async () => {
    const calls = [];
    const api = new VideoProjectApi(async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ id: "p", title: "matrix", status: "draft", segments: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    await api.runSelected("p", ["B", "B", "A"]);
    assert.deepEqual(calls, [{ url: "/api/video-projects/p/run", body: { segment_ids: ["B", "A"] } }]);
    const dependent = { ...shot("B", ref), continuation: "previous_video" };
    assert.deepEqual(
      selectedTimelineRunPlan([shot("A", ref), dependent], new Set(["B"])).map(({ id, autoIncluded }) => [id, autoIncluded]),
      [["A", true], ["B", false]],
    );
    assert.match(timelineSource, /selectedTimelineRunPlan\(project\.segments, runSelection\)/);
    assert.match(timelineSource, /自动补齐前驱/);
    assert.match(serverTests, /test_restart_selected_source_range_runs_only_selected_crop_and_gpu_job/);
    assert.match(serverTests, /test_continuation_selection_is_auto_expanded_to_dependency_closure/);
    assert.match(serverSource, /expanded = set\(selected\)/);
    assert.match(serverSource, /while cursor > 0 and segments\[cursor\]\.get\("continuation"\) != "none"/);
    assert.match(serverSource, /selected_segment_ids/);
  });
});
