import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const contract = JSON.parse(await readFile(new URL(
  "./fixtures/director-upstream-workflow-contract.json",
  import.meta.url,
), "utf8"));

test("Cycle 14 locks the reviewed r2v/v2v/rv2v upstream workflows and source implementation", () => {
  assert.equal(contract.upstream.commit, "a267324a9f88141ff4e4b0e8c1a6ed90b4e45db7");
  assert.deepEqual(Object.keys(contract.upstream.workflow_files), ["r2v", "v2v", "rv2v"]);
  for (const workflow of Object.values(contract.upstream.workflow_files)) {
    assert.match(workflow.path, /^example_workflows\/minimax_h3_director_(?:r2v|v2v|rv2v)\.json$/);
    assert.match(workflow.sha256, /^[0-9a-f]{64}$/);
  }
  assert.match(contract.upstream.reviewed_source_sha256["director/executor_core.py"], /^[0-9a-f]{64}$/);
  assert.match(contract.upstream.reviewed_source_sha256["director/plan.py"], /^[0-9a-f]{64}$/);
  assert.match(contract.upstream.reviewed_source_sha256["director/audio_export.py"], /^[0-9a-f]{64}$/);
});

test("all Director edit modes use Ref2VA but have different executable source and reference shapes", () => {
  assert.equal(contract.models.unet, "minimax_h3_ref2va_pruned_int8_convrot.safetensors");
  for (const mode of ["r2v", "v2v", "rv2v"]) {
    assert.equal(contract.modes[mode].compiler, "h3_ref");
  }
  assert.equal(contract.modes.r2v.source, "forbidden");
  assert.deepEqual(contract.modes.r2v.ordinary_reference_kinds, ["image", "video", "audio"]);
  assert.equal(contract.modes.r2v.audio_only, false);
  assert.equal(contract.modes.v2v.source, "required");
  assert.deepEqual(contract.modes.v2v.ordinary_reference_kinds, []);
  assert.equal(contract.modes.v2v.source_video_input, "ref_videos.ref_video_0");
  assert.equal(contract.modes.rv2v.source, "required");
  assert.deepEqual(contract.modes.rv2v.ordinary_reference_kinds, ["image", "audio"]);
  assert.equal(contract.modes.rv2v.ordinary_references_min, 0);
  assert.match(contract.modes.rv2v.documented_equivalence, /same conditioning graph as v2v/);
});

test("the source is a first-class receipt and must produce <Video 1> without duplicate wiring", () => {
  assert.equal(contract.request_contract.source_field, "source_asset_id");
  assert.equal(contract.request_contract.source_requires_connected_reference, true);
  assert.match(contract.request_contract.source_semantic_role, /already-connected video reference/);
  assert.match(contract.request_contract.source_semantic_role, /must not be duplicated/);
  assert.equal(contract.request_contract.stored_prompt_mutated, false);
  assert.equal(contract.request_contract.compiled_prompt_must_expose_source_tag, true);
  assert.equal(contract.compiled_graph_contract.source_must_be_first_video, true);
  assert.equal(contract.compiled_graph_contract.source_must_not_be_wired_to_ref_video_audio, true);
  assert.equal(contract.modes.v2v.source_prompt_tag, "<Video 1>");
  assert.equal(contract.modes.rv2v.source_prompt_tag, "<Video 1>");
  assert.deepEqual(contract.compiled_graph_contract.source_chain, [
    "LoadVideo",
    "GetVideoComponents",
    "MiniMaxH3ReferenceToVideo.ref_videos.ref_video_0",
  ]);
});

test("mode selection is rejected as metadata-only unless the graph proves it or upstream documents equivalence", () => {
  assert.equal(contract.compiled_graph_contract.mode_must_change_graph_or_be_documented_equivalent, true);
  assert.equal(contract.compiled_graph_contract.static_upstream_template_is_not_execution_evidence, true);
  assert.equal(contract.compiled_graph_contract.workflow_export_kind, "exact immutable compiled prompt graph from job evidence");
  assert.deepEqual(contract.compiled_graph_contract.forbidden_node_types, ["MiniMaxH3Director"]);
  assert.equal(contract.compiled_graph_contract.required_reference_node, "MiniMaxH3ReferenceToVideo");
  assert.ok(contract.compiled_graph_contract.required_output_stages.includes("VAEDecodeAudio"));
  assert.ok(contract.compiled_graph_contract.required_output_stages.includes("SaveVideo"));
});

test("source audio modes remain separate output semantics and are not faked as reference audio", () => {
  assert.deepEqual(contract.audio_contract.upstream_modes, ["generate", "source", "mute"]);
  assert.equal(contract.audio_contract.source_audio_is_not_reference_audio, true);
  assert.equal(contract.audio_contract.studio_cycle14_baseline, "generate-only");
  assert.match(contract.audio_contract.ui_gate, /Do not expose source or mute/);
  assert.ok(contract.capability_boundary.upstream_custom_or_separate_implementation.includes("source-audio passthrough and mute output modes"));
  assert.ok(contract.capability_boundary.upstream_custom_or_separate_implementation.includes("motion-context latent/audio pinning and prefix trimming"));
});

test("Studio keeps the official H3 reference limits and its licensing gates", () => {
  assert.equal(contract.studio_limits.max_generated_frames, 362);
  assert.equal(contract.studio_limits.max_total_references, 12);
  assert.equal(contract.studio_limits.max_image_references, 9);
  assert.equal(contract.studio_limits.max_video_references, 3);
  assert.equal(contract.studio_limits.max_audio_references, 3);
  assert.deepEqual(contract.studio_limits.reference_media_seconds, [2, 15]);
  assert.equal(contract.capability_boundary.optional_latent_upscaler.default_enabled, false);
  assert.equal(contract.capability_boundary.optional_latent_upscaler.separate_model_license_and_digest_required, true);
});
