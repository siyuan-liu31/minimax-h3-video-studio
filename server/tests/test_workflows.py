from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from server.config import Config
from server.errors import ApiError, CapabilityError
from server.profiles import DEFAULT_REGISTRY, H3_MAX_DURATION_SECONDS
from server.workflows import (
    MotionContextPlan,
    ResumeSamplingPlan,
    compile_prompt_request,
    compile_image_workflow,
    compile_z_image_img2img_workflow,
    compile_z_image_lora_workflow,
    compile_workflow,
    compile_video_workflow,
    h3_frame_count,
    parse_generation_request,
    workflow_evidence,
)


def config(root: Path, checkpoint: str = "anything-v5-PrtRE.safetensors") -> Config:
    return Config(
        host="127.0.0.1",
        port=0,
        api_key="",
        cors_origins=("*",),
        comfy_url="http://127.0.0.1:6006",
        data_root=root / "data",
        comfy_input=root / "input",
        comfy_output=root / "output",
        max_json_bytes=262144,
        max_image_bytes=1024,
        max_video_bytes=4096,
        max_audio_bytes=2048,
        max_asset_storage_bytes=1024 * 1024,
        max_active_jobs=4,
        asset_ttl_days=30,
        fl_model="fl.safetensors",
        ref_model="ref.safetensors",
        text_encoder="clip.safetensors",
        video_vae="video-vae.safetensors",
        audio_vae="audio-vae.safetensors",
        fl_lora="fl-lora.safetensors",
        ref_lora="ref-lora.safetensors",
        image_checkpoint=checkpoint,
    )


ASSETS = {
    "a" * 32: {
        "id": "a" * 32,
        "kind": "image",
        "filename": "first.png",
        "comfy_path": "h3-studio/a.png",
    },
    "b" * 32: {
        "id": "b" * 32,
        "kind": "image",
        "filename": "style.png",
        "comfy_path": "h3-studio/b.png",
    },
    "c" * 32: {
        "id": "c" * 32,
        "kind": "video",
        "filename": "motion.mp4",
        "comfy_path": "h3-studio/c.mp4",
        "media": {"duration": 4.25, "has_audio": True, "fps": 24, "reference_fps": 24},
    },
    "d" * 32: {
        "id": "d" * 32,
        "kind": "audio",
        "filename": "voice.wav",
        "comfy_path": "h3-studio/d.wav",
        "media": {"duration": 4.25, "has_audio": True},
    },
}


def lookup(asset_id: str):
    if asset_id not in ASSETS:
        raise ApiError(404, "not_found", "missing")
    return ASSETS[asset_id]


class FrameTests(unittest.TestCase):
    def test_h3_grid(self) -> None:
        self.assertEqual(h3_frame_count(5), 124)
        self.assertEqual((h3_frame_count(7) - 5) % 17, 0)
        self.assertGreaterEqual(h3_frame_count(7), 7 * 24)
        self.assertEqual(h3_frame_count(15), 362)
        self.assertEqual(h3_frame_count(H3_MAX_DURATION_SECONDS), 362)
        self.assertEqual(h3_frame_count(H3_MAX_DURATION_SECONDS) / 24, H3_MAX_DURATION_SECONDS)


class MotionContextWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = config(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def spec(self, profile_id: str = "minimax-h3-fl2va", steps: int = 7):
        profile = DEFAULT_REGISTRY.get(profile_id)
        return parse_generation_request({
            "output_type": "video",
            "prompt": "A continuous illustrated scene",
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "parameters": {"duration": 5, "aspect_ratio": "16:9", "steps": steps},
            "references": [],
        }, lookup)

    def test_turbo_custom_steps_loads_context_trims_output_and_saves_raw_latent(self) -> None:
        spec = self.spec(steps=7)
        workflow = compile_workflow(spec, self.config, "a" * 32, motion_context_plan=MotionContextPlan(
            checkpoint_input="h3-studio-motion-context/source.latent",
            save_context=True,
            video_frames=39,
            audio_frames=48,
        ))
        self.assertEqual(workflow["12"]["inputs"]["steps"], 7)
        self.assertEqual(workflow["22"]["inputs"]["context_length"], "39")
        self.assertEqual(workflow["22"]["inputs"]["audio_context_length"], 48)
        self.assertEqual(workflow["10"]["inputs"]["conditioning"], ["22", 0])
        self.assertEqual(workflow["16"]["inputs"]["images"], ["23", 0])
        self.assertEqual(workflow["16"]["inputs"]["audio"], ["23", 1])
        self.assertEqual(workflow["19"]["inputs"]["samples"], ["13", 0])
        evidence = workflow_evidence(workflow, spec, "a" * 32)
        self.assertEqual(evidence["steps"], 7)
        self.assertEqual(evidence["motion_context"]["video_frames"], 39)
        self.assertTrue(evidence["motion_context"]["trimmed"])

    def test_base_and_first_chain_link_are_supported_without_changing_normal_graphs(self) -> None:
        base = self.spec("minimax-h3-fl2va-base", steps=20)
        first = compile_workflow(base, self.config, "b" * 32, motion_context_plan=MotionContextPlan(save_context=True))
        self.assertNotIn("4", first)
        self.assertIn("19", first)
        self.assertNotIn("22", first)
        normal = compile_workflow(base, self.config, "c" * 32)
        self.assertNotIn("19", normal)
        self.assertNotIn("22", normal)
        self.assertEqual(normal["16"]["inputs"]["images"], ["14", 0])

    def test_motion_context_rejects_bad_windows_and_sampling_resume_collision(self) -> None:
        with self.assertRaises(ValueError):
            MotionContextPlan(video_frames=6)
        with self.assertRaises(ValueError):
            MotionContextPlan(audio_frames=241)
        with self.assertRaises(ValueError):
            compile_workflow(
                self.spec("minimax-h3-fl2va-base", 20), self.config, "d" * 32,
                ResumeSamplingPlan("initial", 50), MotionContextPlan(save_context=True),
            )


class RequestTests(unittest.TestCase):
    @staticmethod
    def flux2_identity() -> dict[str, str]:
        profile = DEFAULT_REGISTRY.get("flux2-klein-4b-fp8")
        return {
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }

    def test_image_presets_and_explicit_sizes_cover_four_ratios_at_two_tiers(self) -> None:
        sizes = {
            "16:9": ((1024, 576), (2048, 1152)),
            "9:16": ((576, 1024), (1152, 2048)),
            "3:4": ((768, 1024), (1536, 2048)),
            "1:1": ((1024, 1024), (2048, 2048)),
        }
        for aspect_ratio, (standard, high) in sizes.items():
            with self.subTest(aspect_ratio=aspect_ratio, tier="standard"):
                preset = parse_generation_request(
                    {"type": "image", "prompt": "product photo", "parameters": {"aspect_ratio": aspect_ratio}},
                    lookup,
                )
                self.assertEqual((preset.width, preset.height), standard)
            with self.subTest(aspect_ratio=aspect_ratio, tier="high"):
                width, height = high
                explicit = parse_generation_request(
                    {"type": "image", "prompt": "product photo", "width": width, "height": height},
                    lookup,
                )
                self.assertEqual((explicit.width, explicit.height), high)
                self.assertEqual(
                    (explicit.public_parameters()["width"], explicit.public_parameters()["height"]),
                    high,
                )

    def test_image_explicit_resolution_rejects_unaligned_or_unsafe_dimensions(self) -> None:
        with self.assertRaisesRegex(ApiError, "multiples of 8"):
            parse_generation_request(
                {"type": "image", "prompt": "product photo", "width": 1537, "height": 2048},
                lookup,
            )
        with self.assertRaisesRegex(ApiError, "between 256 and 2048"):
            parse_generation_request(
                {"type": "image", "prompt": "product photo", "width": 2048, "height": 2056},
                lookup,
            )

    def test_flux2_accepts_text_only_and_up_to_four_ordered_images(self) -> None:
        text = parse_generation_request({
            "type": "image", "prompt": "a studio product photo", **self.flux2_identity(),
        }, lookup)
        self.assertEqual(text.mode, "text-to-image")
        self.assertEqual(text.references, ())
        self.assertEqual((text.steps, text.cfg), (4, 1))

        ordered = parse_generation_request({
            "type": "image", "prompt": "put @style behind @subject",
            "assets": [
                {"id": "a" * 32, "label": "subject", "reference_index": 1},
                {"id": "b" * 32, "label": "style", "reference_index": 0},
            ],
            **self.flux2_identity(),
        }, lookup)
        self.assertEqual([reference.asset_id for reference in ordered.references], ["b" * 32, "a" * 32])
        self.assertIn("image 1", ordered.prompt)
        self.assertIn("image 2", ordered.prompt)
        self.assertNotIn("<Picture", ordered.prompt)

    def test_flux2_graph_reference_index_is_canonical_and_dense(self) -> None:
        graph = {
            "nodes": [
                {"id": "prompt", "type": "prompt", "data": {"prompt": "combine @style and @subject"}},
                {"id": "subject", "type": "image", "data": {"assetId": "a" * 32, "label": "subject"}},
                {"id": "style", "type": "image", "data": {"assetId": "b" * 32, "label": "style"}},
                {"id": "image", "type": "generator", "data": {"output_type": "image"}},
                {"id": "output", "type": "output", "data": {}},
            ],
            "edges": [
                {"source": "prompt", "target": "image"},
                {"source": "subject", "target": "image", "data": {"reference_index": 1}},
                {"source": "style", "target": "image", "data": {"reference_index": 0}},
                {"source": "image", "target": "output"},
            ],
        }
        spec = parse_generation_request({
            "type": "image", "graph": graph, **self.flux2_identity(),
        }, lookup)
        self.assertEqual([reference.asset_id for reference in spec.references], ["b" * 32, "a" * 32])
        self.assertEqual(spec.prompt, "combine image 1 and image 2")

        graph["edges"][2]["data"]["reference_index"] = 1
        with self.assertRaisesRegex(ApiError, "unique and contiguous"):
            parse_generation_request({"type": "image", "graph": graph, **self.flux2_identity()}, lookup)

    def test_flux2_rejects_non_distilled_sampling_and_non_16_aligned_output(self) -> None:
        with self.assertRaisesRegex(ApiError, "steps must be between 4 and 4"):
            parse_generation_request({
                "type": "image", "prompt": "x", "steps": 8, **self.flux2_identity(),
            }, lookup)
        with self.assertRaisesRegex(ApiError, "FLUX.2.*multiples of 16"):
            parse_generation_request({
                "type": "image", "prompt": "x", "width": 1032, "height": 1024,
                **self.flux2_identity(),
            }, lookup)
        with self.assertRaisesRegex(ApiError, "does not expose denoise"):
            parse_generation_request({
                "type": "image", "prompt": "x", "denoise": 0.5, **self.flux2_identity(),
            }, lookup)
        with self.assertRaisesRegex(ApiError, "does not use a negative prompt"):
            parse_generation_request({
                "type": "image", "prompt": "x", "negative_prompt": "blurry",
                **self.flux2_identity(),
            }, lookup)
        with self.assertRaisesRegex(ApiError, "does not use a negative prompt"):
            compile_prompt_request({
                "output_type": "image", "prompt": "x", "negative_prompt": "blurry",
                **self.flux2_identity(),
            }, lookup)

    def test_flux2_canonicalizes_direct_image_ordinals_without_misreading_resolution_terms(self) -> None:
        assets = [
            {"id": "a" * 32, "reference_index": 0},
            {"id": "b" * 32, "reference_index": 1},
        ]
        spec = parse_generation_request({
            "type": "image",
            "prompt": "图1 follows 图片2; Image1, image 2, Picture #2 and <Picture 1>",
            "assets": assets,
            **self.flux2_identity(),
        }, lookup)
        self.assertEqual(spec.prompt.count("image 1"), 3)
        self.assertEqual(spec.prompt.count("image 2"), 3)
        self.assertNotIn("<image", spec.prompt)
        for bad in ("图0", "image 3"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ApiError, "has no connected reference"):
                parse_generation_request({
                    "type": "image", "prompt": bad, "assets": assets,
                    **self.flux2_identity(),
                }, lookup)
        text = parse_generation_request({
            "type": "image", "prompt": "cinematic image 16:9, image 4K, 图4K画质",
            **self.flux2_identity(),
        }, lookup)
        self.assertEqual(text.prompt, "cinematic image 16:9, image 4K, 图4K画质")

    def test_flux2_profile_enforces_four_reference_limit(self) -> None:
        ids = [str(index) * 32 for index in range(1, 6)]

        def image_lookup(asset_id: str):
            return {
                "id": asset_id, "kind": "image", "filename": f"{asset_id}.png",
                "comfy_path": f"h3-studio/{asset_id}.png",
            }

        with self.assertRaisesRegex(ApiError, "reference limit"):
            parse_generation_request({
                "type": "image", "prompt": "combine the references",
                "assets": [{"id": asset_id} for asset_id in ids],
                **self.flux2_identity(),
            }, image_lookup)

    def test_camel_case_text_video(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "A quiet ocean at dawn",
                "aspectRatio": "9:16",
                "duration": 5,
                "steps": 4,
                "loraStrength": 0.8,
                "modelMode": "auto",
                "seed": 7,
            },
            lookup,
        )
        self.assertEqual((spec.width, spec.height), (768, 1344))
        self.assertEqual(spec.mode, "text")
        self.assertEqual(spec.frames, 124)
        self.assertEqual(spec.seed, 7)

    def test_all_h3_profiles_accept_362_frame_output_duration(self) -> None:
        for identifier in (
            "minimax-h3-fl2va",
            "minimax-h3-fl2va-base",
            "minimax-h3-ref2va",
            "minimax-h3-ref2va-base",
        ):
            profile = DEFAULT_REGISTRY.get(identifier)
            request = {
                "type": "video",
                "prompt": "A quiet ocean at dawn",
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_digest": profile.digest(),
                "parameters": {"duration": H3_MAX_DURATION_SECONDS},
            }
            if profile.compiler == "h3_ref":
                request["assets"] = [{"id": "c" * 32, "role": "motion"}]
            with self.subTest(profile=identifier):
                spec = parse_generation_request(request, lookup)
                self.assertEqual(spec.duration, H3_MAX_DURATION_SECONDS)
                self.assertEqual(spec.frames, 362)
                self.assertEqual(spec.public_parameters()["duration_actual"], 15.083)

    def test_h3_output_duration_rejects_values_above_362_frames(self) -> None:
        with self.assertRaisesRegex(ApiError, "between 5 and 15.0833"):
            parse_generation_request({
                "type": "video",
                "prompt": "A quiet ocean at dawn",
                "parameters": {"duration": H3_MAX_DURATION_SECONDS + 0.001},
            }, lookup)
        preview = compile_prompt_request({
            "output_type": "video",
            "prompt": "A quiet ocean at dawn",
            "parameters": {"duration": H3_MAX_DURATION_SECONDS},
        }, lookup)
        self.assertEqual(preview["duration_actual"], round(H3_MAX_DURATION_SECONDS, 3))
        with self.assertRaisesRegex(ApiError, "between 5 and 15.0833"):
            compile_prompt_request({
                "output_type": "video",
                "prompt": "A quiet ocean at dawn",
                "parameters": {"duration": H3_MAX_DURATION_SECONDS + 0.001},
            }, lookup)

    def test_turbo_profile_accepts_adjustable_steps_and_model_strength(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va")
        spec = parse_generation_request({
            "type": "video", "prompt": "ocean", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
            "steps": 15, "loraStrength": 0.7,
        }, lookup)
        with tempfile.TemporaryDirectory() as directory:
            workflow = compile_video_workflow(spec, config(Path(directory)), "a" * 32)
        self.assertEqual(spec.steps, 15)
        self.assertEqual(spec.lora_strength, 0.7)
        self.assertEqual(workflow["4"]["inputs"]["strength_model"], 0.7)
        self.assertEqual(workflow["12"]["inputs"]["steps"], 15)

    def test_base_profile_accepts_adjustable_steps_and_disables_turbo_lora(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
        spec = parse_generation_request({
            "type": "video", "prompt": "ocean", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(), "steps": 28, "loraStrength": 0,
        }, lookup)
        self.assertEqual(spec.steps, 28)
        self.assertEqual(spec.sampling_mode, "base")
        self.assertEqual(spec.lora_strength, 0)
        self.assertEqual((spec.sampler, spec.scheduler), ("res_multistep", "simple"))
        with tempfile.TemporaryDirectory() as directory:
            workflow = compile_video_workflow(spec, config(Path(directory)), "b" * 32)
        self.assertNotIn("4", workflow)
        self.assertEqual(workflow["10"]["inputs"]["model"], ["3", 0])
        self.assertEqual(workflow["11"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(workflow["12"]["inputs"]["steps"], 28)

    def test_base_profile_rejects_nonzero_turbo_lora_strength(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
        with self.assertRaisesRegex(ApiError, "does not load a Turbo LoRA"):
            parse_generation_request({
                "type": "video", "prompt": "ocean",
                "profile_id": profile.id, "profile_version": profile.version, "profile_digest": profile.digest(),
                "steps": 20, "loraStrength": 0.75,
            }, lookup)

    def test_ref_base_profile_builds_a_lora_free_base20_graph(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va-base")
        spec = parse_generation_request({
            "type": "video", "prompt": "Use @motion",
            "profile_id": profile.id, "profile_version": profile.version, "profile_digest": profile.digest(),
            "steps": 20, "assets": [{"id": "c" * 32, "label": "motion", "role": "motion"}],
        }, lookup)
        self.assertEqual(spec.sampling_mode, "base")
        self.assertEqual((spec.steps, spec.lora_strength), (20, 0))
        self.assertEqual((spec.sampler, spec.scheduler), ("res_multistep", "simple"))
        with tempfile.TemporaryDirectory() as directory:
            workflow = compile_video_workflow(spec, config(Path(directory)), "c" * 32)
        self.assertNotIn("4", workflow)
        self.assertEqual(workflow["8"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(workflow["10"]["inputs"]["model"], ["3", 0])
        self.assertEqual(workflow["11"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(workflow["12"]["inputs"], {
            "model": ["3", 0], "scheduler": "simple", "steps": 20, "denoise": 1.0,
        })

    def test_turbo_profile_builds_the_exact_four_step_lora_graph(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va")
        spec = parse_generation_request({
            "type": "video", "prompt": "ocean",
            "profile_id": profile.id, "profile_version": profile.version, "profile_digest": profile.digest(),
            "steps": 4, "loraStrength": 0.75,
        }, lookup)
        with tempfile.TemporaryDirectory() as directory:
            workflow = compile_video_workflow(spec, config(Path(directory)), "d" * 32)
        self.assertEqual(spec.sampling_mode, "turbo4")
        self.assertEqual(workflow["4"], {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["3", 0], "lora_name": "fl-lora.safetensors", "strength_model": 0.75},
        })
        self.assertEqual(workflow["10"]["inputs"]["model"], ["4", 0])
        self.assertEqual(workflow["11"]["inputs"]["sampler_name"], "sa_solver")
        self.assertEqual(workflow["12"]["inputs"], {
            "model": ["4", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0,
        })

    def test_video_generation_strength_reaches_basic_scheduler_and_public_parameters(self) -> None:
        for identifier in (
            "minimax-h3-fl2va", "minimax-h3-fl2va-base",
        ):
            with self.subTest(identifier=identifier):
                profile = DEFAULT_REGISTRY.get(identifier)
                spec = parse_generation_request({
                    "type": "video", "prompt": "ocean", "profile_id": profile.id,
                    "profile_version": profile.version, "profile_digest": profile.digest(),
                    "parameters": {"duration": 5, "denoise": 0.65},
                }, lookup)
                with tempfile.TemporaryDirectory() as directory:
                    workflow = compile_video_workflow(spec, config(Path(directory)), "e" * 32)
                self.assertEqual(spec.denoise, 0.65)
                self.assertEqual(spec.public_parameters()["denoise"], 0.65)
                self.assertEqual(workflow["12"]["inputs"]["denoise"], 0.65)

        with self.assertRaisesRegex(ApiError, "between 0.05 and 1"):
            parse_generation_request({"type": "video", "prompt": "ocean", "parameters": {"denoise": 0}}, lookup)

    def test_explicit_profile_requires_matching_version_and_digest(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
        request = {"type": "video", "prompt": "ocean", "profile_id": profile.id, "profile_version": profile.version}
        with self.assertRaisesRegex(ApiError, "profile_version and profile_digest"):
            parse_generation_request(request, lookup)
        request["profile_digest"] = "0" * 64
        with self.assertRaisesRegex(ApiError, "profile changed"):
            parse_generation_request(request, lookup)

    def test_graph_first_last_selects_fl2va(self) -> None:
        spec = parse_generation_request(
            {
                "output_type": "video",
                "prompt": "The person turns around",
                "graph": {
                    "nodes": [
                        {"id": "first", "type": "image", "data": {"assetId": "a" * 32}},
                        {"id": "last", "type": "image", "data": {"assetId": "b" * 32}},
                        {"id": "go", "type": "generate"},
                    ],
                    "edges": [
                        {"source": "first", "target": "go", "targetHandle": "first_frame"},
                        {"source": "last", "target": "go", "targetHandle": "last_frame"},
                    ],
                },
            },
            lookup,
        )
        self.assertEqual(spec.mode, "fl2va")
        self.assertEqual([ref.role for ref in spec.references], ["first_frame", "last_frame"])

    def test_reference_mode_rewrites_mentions_and_adds_missing_tags(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "@motion.mp4 provides the camera motion; preserve the voice",
                "assets": [
                    {"id": "c" * 32, "label": "motion.mp4"},
                    {"id": "d" * 32},
                ],
            },
            lookup,
        )
        self.assertEqual(spec.mode, "ref2va")
        self.assertIn("<Video 1>", spec.prompt)
        self.assertIn("<Audio 1>", spec.prompt)

    def test_reference_tag_requires_connected_asset(self) -> None:
        with self.assertRaisesRegex(ApiError, "no connected reference"):
            parse_generation_request(
                {
                    "type": "video",
                    "prompt": "Use <Picture 2>",
                    "assets": [{"id": "a" * 32}],
                },
                lookup,
            )

    def test_invalid_h3_resolution_is_rejected(self) -> None:
        with self.assertRaisesRegex(ApiError, "multiples of 32"):
            parse_generation_request(
                {"type": "video", "prompt": "x", "width": 1000, "height": 768}, lookup
            )

    def test_single_image_reference_routes_to_image_to_image(self) -> None:
        spec = parse_generation_request(
            {"type": "image", "prompt": "portrait", "parameters": {"denoise": 0.4}, "assets": [{"id": "a" * 32}]},
            lookup,
        )
        self.assertEqual(spec.mode, "image-to-image")
        self.assertEqual(spec.profile_id, "qwen-image-edit-2511-int8")
        self.assertEqual(spec.denoise, 0.4)

    def test_image_node_positive_prompt_graph_is_accepted_without_h3_prompt_edge(self) -> None:
        spec = parse_generation_request({
            "type": "image", "prompt": "cinematic portrait at sunset",
            "graph": {
                "nodes": [
                    {"id": "image-prompt", "type": "prompt", "data": {"prompt": "cinematic portrait at sunset"}},
                    {"id": "image", "type": "generator", "data": {"output_type": "image"}},
                    {"id": "output", "type": "output", "data": {}},
                ],
                "edges": [
                    {"id": "image-prompt-input", "source": "image-prompt", "target": "image", "role": "prompt", "data": {"role": "prompt"}},
                    {"id": "image-output", "source": "image", "target": "output", "role": "output", "data": {"role": "output"}},
                ],
            },
        }, lookup)
        self.assertEqual(spec.output_type, "image")
        self.assertEqual(spec.prompt, "cinematic portrait at sunset")

    def test_root_level_image_denoise_is_supported_by_the_public_api(self) -> None:
        profile = DEFAULT_REGISTRY.get("anything-v5-img2img")
        spec = parse_generation_request(
            {
                "type": "image",
                "prompt": "restyle it",
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_digest": profile.digest(),
                "denoise": 0.55,
                "assets": [{"id": "a" * 32, "role": "reference"}],
            },
            lookup,
        )
        self.assertEqual(spec.denoise, 0.55)

    def test_total_reference_budget(self) -> None:
        many = []
        added: list[str] = []
        try:
            kinds = ["image"] * 9 + ["video"] * 3 + ["audio"]
            for index, kind in enumerate(kinds):
                key = f"{index:032x}"
                added.append(key)
                suffix = {"image": "png", "video": "mp4", "audio": "wav"}[kind]
                ASSETS[key] = {
                    "id": key, "kind": kind, "filename": f"x.{suffix}", "comfy_path": f"x.{suffix}",
                    **({"media": {"duration": 2.0, "has_audio": False, "fps": 24, "reference_fps": 24}} if kind == "video" else {}),
                    **({"media": {"duration": 2.0, "has_audio": True}} if kind == "audio" else {}),
                }
                many.append({"id": key})
            accepted = parse_generation_request({"type": "video", "prompt": "x", "assets": many[:12]}, lookup)
            self.assertEqual(len(accepted.references), 12)
            with self.assertRaisesRegex(ApiError, "at most 12"):
                parse_generation_request({"type": "video", "prompt": "x", "assets": many}, lookup)
        finally:
            for key in added:
                ASSETS.pop(key, None)

    def test_h3_reference_modality_capacities_are_independent(self) -> None:
        cases = (("image", 10), ("video", 4), ("audio", 4))
        for kind, count in cases:
            added: list[str] = []
            try:
                references = []
                if kind == "audio":
                    references.append({"id": "a" * 32})
                for index in range(count):
                    key = f"{100 + index:032x}"
                    added.append(key)
                    suffix = {"image": "png", "video": "mp4", "audio": "wav"}[kind]
                    media = {"duration": 2.0, "has_audio": kind == "audio"}
                    if kind == "video":
                        media.update({"fps": 24, "reference_fps": 24})
                    ASSETS[key] = {"id": key, "kind": kind, "filename": f"x.{suffix}", "comfy_path": f"x.{suffix}", "media": media}
                    references.append({"id": key})
                with self.subTest(kind=kind), self.assertRaisesRegex(ApiError, f"at most .* {kind}"):
                    parse_generation_request({"type": "video", "prompt": "x", "assets": references}, lookup)
            finally:
                for key in added:
                    ASSETS.pop(key, None)

    def test_video_soundtrack_consumes_audio_slot_but_not_an_extra_file(self) -> None:
        added = ["e" * 32, "f" * 32]
        try:
            for key in added:
                ASSETS[key] = {
                    "id": key, "kind": "audio", "filename": "voice.wav", "comfy_path": "voice.wav",
                    "media": {"duration": 2.0, "has_audio": True},
                }
            references = [
                {"id": "a" * 32},
                {"id": "c" * 32, "include_audio": True},
                {"id": "d" * 32}, {"id": "e" * 32}, {"id": "f" * 32},
            ]
            with self.assertRaisesRegex(ApiError, "at most 3 audio"):
                parse_generation_request({"type": "video", "prompt": "x", "assets": references}, lookup)
        finally:
            for key in added:
                ASSETS.pop(key, None)

    def test_canonical_graph_only_reads_selected_generator_subgraph(self) -> None:
        spec = parse_generation_request(
            {
                "output_type": "image", "prompt": "edit it",
                "graph": {
                    "nodes": [
                        {"id": "a1", "type": "asset", "data": {"kind": "image", "assetId": "a" * 32}},
                        {"id": "v1", "type": "asset", "data": {"kind": "video", "assetId": "c" * 32}},
                        {"id": "image", "type": "generator", "data": {"output_type": "image"}},
                        {"id": "video", "type": "generator", "data": {"output_type": "video"}},
                    ],
                    "edges": [{"source": "a1", "target": "image"}, {"source": "v1", "target": "video"}],
                },
            }, lookup
        )
        self.assertEqual(spec.mode, "image-to-image")
        self.assertEqual([reference.asset_id for reference in spec.references], ["a" * 32])

    def test_audio_only_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(ApiError, "audio-only"):
            parse_generation_request({"type": "video", "prompt": "voice", "assets": [{"id": "d" * 32}]}, lookup)

    def test_director_modes_enforce_distinct_reference_contracts(self) -> None:
        source_id = "c" * 32
        valid = {
            "t2v": {"references": []},
            "i2v": {"references": [{"asset_id": "a" * 32, "role": "first_frame"}]},
            "fl2v": {"references": [
                {"asset_id": "a" * 32, "role": "first_frame"},
                {"asset_id": "b" * 32, "role": "last_frame"},
            ]},
            "r2v": {"references": [{"asset_id": "a" * 32, "role": "identity"}]},
            "v2v": {"source_asset_id": source_id, "references": [
                {"asset_id": source_id, "role": "motion"},
            ]},
            "rv2v": {"source_asset_id": source_id, "references": [
                {"asset_id": "a" * 32, "role": "identity"},
                {"asset_id": source_id, "role": "motion"},
                {"asset_id": "d" * 32, "role": "music"},
            ]},
        }
        for mode, fields in valid.items():
            with self.subTest(mode=mode):
                spec = parse_generation_request({
                    "type": "video", "prompt": "director shot",
                    "director_mode": mode, **fields,
                }, lookup)
                self.assertEqual(spec.director_mode, mode)
                self.assertEqual(spec.public_parameters()["resolved_director_mode"], mode)

        for endpoint in ("first_frame", "last_frame"):
            with self.subTest(fl2v_single_endpoint=endpoint):
                single = parse_generation_request({
                    "type": "video", "prompt": "single endpoint",
                    "director_mode": "fl2v",
                    "references": [{"asset_id": "a" * 32, "role": endpoint}],
                }, lookup)
                self.assertEqual(single.director_mode, "fl2v")

        invalid = (
            {"director_mode": "t2v", "references": [{"asset_id": "a" * 32, "role": "identity"}]},
            {"director_mode": "i2v", "references": [{"asset_id": "a" * 32, "role": "last_frame"}]},
            {"director_mode": "fl2v", "references": [{"asset_id": "a" * 32, "role": "identity"}]},
            {"director_mode": "r2v", "references": [{"asset_id": "d" * 32, "role": "music"}]},
            {"director_mode": "v2v", "source_asset_id": source_id, "references": [
                {"asset_id": source_id, "role": "motion"}, {"asset_id": "a" * 32, "role": "identity"},
            ]},
            {"director_mode": "rv2v", "source_asset_id": source_id, "references": [
                {"asset_id": source_id, "role": "motion", "include_audio": True},
            ]},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ApiError):
                parse_generation_request({"type": "video", "prompt": "invalid", **fields}, lookup)

    def test_unknown_generation_parameters_are_rejected_instead_of_ignored(self) -> None:
        with self.assertRaisesRegex(ApiError, "duraton"):
            parse_generation_request({
                "type": "video", "prompt": "typo",
                "parameters": {"duration": 5, "duraton": 6},
            }, lookup)
        with self.assertRaisesRegex(ApiError, "duration"):
            parse_generation_request({
                "type": "image", "prompt": "wrong modality",
                "parameters": {"duration": 5},
            }, lookup)

    def test_rv2v_preview_and_generation_share_source_first_readonly_prompt(self) -> None:
        request = {
            "type": "video",
            "prompt": f"  Keep @{{{'a' * 32}}}; music @{{{'d' * 32}}}.  \n",
            "prompt_mode": "preserve_tags_only",
            "director_mode": "rv2v",
            "source_asset_id": "c" * 32,
            "references": [
                {"asset_id": "a" * 32, "role": "identity"},
                {"asset_id": "c" * 32, "role": "motion"},
                {"asset_id": "d" * 32, "role": "music"},
            ],
        }
        spec = parse_generation_request(request, lookup)
        preview = compile_prompt_request({**request, "output_type": "video"}, lookup)
        self.assertEqual([reference.asset_id for reference in spec.references], ["c" * 32, "a" * 32, "d" * 32])
        self.assertEqual(preview["prompt"], spec.prompt)
        self.assertEqual(preview["resolved_director_mode"], "rv2v")
        self.assertEqual(spec.prompt, "  Keep <Picture 1>; music <Audio 1>.  \n\n\n<Video 1>")
        self.assertNotIn("Reference directives", spec.prompt)

    def test_rv2v_rejects_a_second_video_but_source_only_is_explicitly_supported(self) -> None:
        other_id = "e" * 32
        ASSETS[other_id] = {
            **ASSETS["c" * 32], "id": other_id, "filename": "other.mp4",
            "comfy_path": "h3-studio/e.mp4",
        }
        try:
            with self.assertRaisesRegex(ApiError, "second video"):
                parse_generation_request({
                    "type": "video", "prompt": "x", "director_mode": "rv2v",
                    "source_asset_id": "c" * 32,
                    "references": [
                        {"asset_id": other_id, "role": "camera"},
                        {"asset_id": "c" * 32, "role": "motion"},
                    ],
                }, lookup)
            source_only = parse_generation_request({
                "type": "video", "prompt": "x", "director_mode": "rv2v",
                "source_asset_id": "c" * 32,
                "references": [{"asset_id": "c" * 32, "role": "motion"}],
            }, lookup)
            self.assertEqual(source_only.director_mode, "rv2v")
        finally:
            ASSETS.pop(other_id, None)

    def test_v2v_source_still_obeys_duration_and_duplicate_reference_budgets(self) -> None:
        short_id = "f" * 32
        ASSETS[short_id] = {
            **ASSETS["c" * 32], "id": short_id, "filename": "short.mp4",
            "comfy_path": "h3-studio/f.mp4",
            "media": {"duration": 1.0, "has_audio": False, "fps": 24, "reference_fps": 24},
        }
        try:
            with self.assertRaisesRegex(ApiError, "between 2 and 15 seconds"):
                parse_generation_request({
                    "type": "video", "prompt": "x", "director_mode": "v2v",
                    "source_asset_id": short_id,
                    "references": [{"asset_id": short_id, "role": "motion"}],
                }, lookup)
            with self.assertRaisesRegex(ApiError, "unique"):
                parse_generation_request({
                    "type": "video", "prompt": "x", "director_mode": "v2v",
                    "source_asset_id": "c" * 32,
                    "references": [
                        {"asset_id": "c" * 32, "role": "motion"},
                        {"asset_id": "c" * 32, "role": "camera"},
                    ],
                }, lookup)
        finally:
            ASSETS.pop(short_id, None)

    def test_prompt_preview_adds_fl_alignment_and_ref_tags(self) -> None:
        result = compile_prompt_request(
            {"output_type": "video", "prompt": "A dancer spins", "assets": [{"id": "a" * 32, "role": "first_frame"}]}, lookup
        )
        self.assertEqual(result["mode"], "fl2va")
        self.assertIn("0.00 seconds", result["prompt"])
        ref = compile_prompt_request(
            {"output_type": "video", "prompt": "Follow @motion", "assets": [{"id": "c" * 32, "label": "motion"}]}, lookup
        )
        self.assertIn("<Video 1>", ref["prompt"])

    def test_reference_tags_are_numbered_independently_by_modality(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "Use @{hero}, @{motion}, @{style}, and @{voice}",
                "assets": [
                    {"id": "a" * 32, "label": "hero", "role": "identity"},
                    {"id": "c" * 32, "label": "motion", "role": "motion"},
                    {"id": "b" * 32, "label": "style", "role": "style"},
                    {"id": "d" * 32, "label": "voice", "role": "voice", "voice_speaker": "S1", "voice_subject": 1},
                ],
            },
            lookup,
        )
        self.assertIn("<Picture 1>", spec.prompt)
        self.assertIn("<Video 1>", spec.prompt)
        self.assertIn("<Picture 2>", spec.prompt)
        self.assertIn("<Audio 1>", spec.prompt)
        self.assertEqual(spec.references[-1].voice_speaker, "S1")
        self.assertEqual(spec.references[-1].voice_subject, 1)

    def test_voice_reference_requires_explicit_subject_and_speaker_binding(self) -> None:
        with self.assertRaisesRegex(ApiError, "explicit target speaker"):
            parse_generation_request({
                "type": "video", "prompt": "A presenter speaks",
                "assets": [
                    {"id": "a" * 32, "role": "identity"},
                    {"id": "d" * 32, "role": "voice", "voice_subject": 1},
                ],
            }, lookup)
        with self.assertRaisesRegex(ApiError, "Subject number"):
            parse_generation_request({
                "type": "video", "prompt": "A presenter speaks",
                "assets": [
                    {"id": "a" * 32, "role": "identity"},
                    {"id": "d" * 32, "role": "voice", "voice_speaker": "S1", "voice_subject": "one"},
                ],
            }, lookup)

    def test_longest_alias_wins_and_dangling_alias_is_rejected(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "Transfer @cat.png.copy, not @cat.png",
                "assets": [
                    {"id": "a" * 32, "label": "cat.png", "role": "identity"},
                    {"id": "b" * 32, "label": "cat.png.copy", "role": "style"},
                ],
            },
            lookup,
        )
        self.assertIn("Transfer <Picture 2>, not <Picture 1>", spec.prompt)
        with self.assertRaisesRegex(ApiError, "not connected"):
            parse_generation_request(
                {
                    "type": "video",
                    "prompt": "Use @missing.png",
                    "assets": [{"id": "a" * 32, "label": "cat.png", "role": "identity"}],
                },
                lookup,
            )

    def test_roles_are_strict_and_endpoint_refs_cannot_mix_with_ref2va(self) -> None:
        with self.assertRaisesRegex(ApiError, "not valid"):
            parse_generation_request(
                {
                    "type": "video",
                    "prompt": "x",
                    "assets": [{"id": "c" * 32, "role": "identity"}],
                },
                lookup,
            )
        with self.assertRaisesRegex(ApiError, "cannot be mixed"):
            parse_generation_request(
                {
                    "type": "video",
                    "prompt": "x",
                    "assets": [
                        {"id": "a" * 32, "role": "first_frame"},
                        {"id": "c" * 32, "role": "motion"},
                    ],
                },
                lookup,
            )

    def test_reference_duration_and_audio_track_contracts(self) -> None:
        added: list[str] = []
        try:
            for digit, duration, kind, has_audio in (
                ("1", 8.0, "video", False),
                ("2", 8.0, "video", True),
                ("3", 8.0, "audio", True),
            ):
                key = digit * 32
                added.append(key)
                ASSETS[key] = {
                    "id": key,
                    "kind": kind,
                    "filename": f"{digit}.mp4" if kind == "video" else f"{digit}.wav",
                    "comfy_path": f"h3-studio/{digit}",
                    "media": {
                        "duration": duration,
                        "has_audio": has_audio,
                        **({"reference_fps": 24, "fps": 24} if kind == "video" else {}),
                    },
                }
            with self.assertRaisesRegex(ApiError, "video.*total at most 15"):
                parse_generation_request(
                    {
                        "type": "video",
                        "prompt": "x",
                        "assets": [
                            {"id": "1" * 32, "role": "motion"},
                            {"id": "2" * 32, "role": "camera"},
                        ],
                    },
                    lookup,
                )
            with self.assertRaisesRegex(ApiError, "no audio track"):
                parse_generation_request(
                    {
                        "type": "video",
                        "prompt": "x",
                        "assets": [{"id": "1" * 32, "role": "motion", "include_audio": True}],
                    },
                    lookup,
                )
            with self.assertRaisesRegex(ApiError, "audio.*total at most 15"):
                parse_generation_request(
                    {
                        "type": "video",
                        "prompt": "x",
                        "assets": [
                            {"id": "2" * 32, "role": "motion", "include_audio": True},
                            {"id": "3" * 32, "role": "voice", "voice_speaker": "S1", "voice_subject": 1},
                        ],
                    },
                    lookup,
                )
            valid = parse_generation_request(
                {
                    "type": "video",
                    "prompt": "x",
                    "assets": [
                        {"id": "2" * 32, "role": "motion"},
                        {"id": "d" * 32, "role": "voice", "voice_speaker": "S1", "voice_subject": 1},
                    ],
                },
                lookup,
            )
            self.assertEqual(valid.reference_duration_total, 12.25)
            self.assertEqual(valid.public_parameters()["reference_duration_total"], 12.25)

            ASSETS["4" * 32] = {
                "id": "4" * 32,
                "kind": "video",
                "filename": "too-long-reference.mp4",
                "comfy_path": "h3-studio/too-long-reference.mp4",
                "media": {
                    "duration": H3_MAX_DURATION_SECONDS,
                    "has_audio": False,
                    "reference_fps": 24,
                    "fps": 24,
                },
            }
            added.append("4" * 32)
            with self.assertRaisesRegex(ApiError, "between 2 and 15 seconds"):
                parse_generation_request({
                    "type": "video",
                    "prompt": "x",
                    "assets": [{"id": "4" * 32, "role": "motion"}],
                }, lookup)
        finally:
            for key in added:
                ASSETS.pop(key, None)

    def test_reference_video_requires_normalized_24_fps_metadata(self) -> None:
        key = "e" * 32
        ASSETS[key] = {
            "id": key,
            "kind": "video",
            "filename": "30fps.mp4",
            "comfy_path": "h3-studio/e.mp4",
            "media": {"duration": 5, "has_audio": False, "fps": 30, "reference_fps": 30},
        }
        try:
            with self.assertRaisesRegex(ApiError, "normalized to 24 fps"):
                parse_generation_request(
                    {"type": "video", "prompt": "x", "assets": [{"id": key, "role": "motion"}]},
                    lookup,
                )
        finally:
            ASSETS.pop(key, None)

    def test_graph_rejects_cycles_and_multiple_selected_generators(self) -> None:
        cycle_graph = {
            "nodes": [
                {"id": "a1", "type": "asset", "data": {"kind": "image", "assetId": "a" * 32}},
                {"id": "video", "type": "generator", "data": {"output_type": "video"}},
            ],
            "edges": [
                {"source": "a1", "target": "video", "role": "first_frame"},
                {"source": "video", "target": "a1"},
            ],
        }
        with self.assertRaises(ApiError) as raised:
            parse_generation_request({"type": "video", "prompt": "x", "graph": cycle_graph}, lookup)
        self.assertEqual(raised.exception.code, "invalid_graph")
        multiple = {
            "nodes": [
                {"id": "v1", "type": "generator", "data": {"output_type": "video"}},
                {"id": "v2", "type": "generator", "data": {"output_type": "video"}},
            ],
            "edges": [],
        }
        with self.assertRaisesRegex(ApiError, "exactly one"):
            parse_generation_request({"type": "video", "prompt": "x", "graph": multiple}, lookup)


class CompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = config(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_text_workflow_uses_fl_model_and_valid_h3_latent(self) -> None:
        spec = parse_generation_request({"type": "video", "prompt": "ocean"}, lookup)
        workflow = compile_video_workflow(spec, self.config, "1" * 32)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "fl.safetensors")
        self.assertEqual(workflow["8"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertEqual(workflow["8"]["inputs"]["length"], 124)
        self.assertEqual(workflow["12"]["inputs"]["steps"], 4)

    def test_fl_workflow_wires_first_and_last_images(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "transition",
                "modelMode": "fl2va",
                "assets": [
                    {"id": "a" * 32, "role": "first_frame"},
                    {"id": "b" * 32, "role": "last_frame"},
                ],
            },
            lookup,
        )
        workflow = compile_video_workflow(spec, self.config, "1" * 32)
        self.assertEqual(workflow["8"]["inputs"]["first_frame"], ["100", 0])
        self.assertEqual(workflow["8"]["inputs"]["last_frame"], ["101", 0])

    def test_ref_workflow_splits_video_and_wires_audio(self) -> None:
        spec = parse_generation_request(
            {
                "type": "video",
                "prompt": "Use @motion",
                "assets": [{"id": "c" * 32, "label": "motion", "include_audio": True}, {"id": "d" * 32}],
            },
            lookup,
        )
        workflow = compile_video_workflow(spec, self.config, "2" * 32)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "ref.safetensors")
        self.assertEqual(workflow["4"]["inputs"]["lora_name"], "ref-lora.safetensors")
        self.assertEqual(workflow["101"]["class_type"], "GetVideoComponents")
        inputs = workflow["8"]["inputs"]
        self.assertEqual(inputs["ref_videos.ref_video_0"], ["101", 0])
        self.assertEqual(inputs["ref_video_audios.ref_video_audio_0"], ["101", 1])
        self.assertEqual(inputs["ref_audios.ref_audio_0"], ["102", 0])

    def test_v2v_compiles_source_as_video_one_and_injects_only_the_native_tag(self) -> None:
        spec = parse_generation_request({
            "type": "video", "prompt": "  Re-stage the camera move.  \n",
            "prompt_mode": "preserve_tags_only", "director_mode": "v2v",
            "source_asset_id": "c" * 32,
            "references": [{"asset_id": "c" * 32, "role": "motion"}],
        }, lookup)
        workflow = compile_video_workflow(spec, self.config, "2" * 32)
        self.assertEqual(spec.prompt, "  Re-stage the camera move.  \n\n\n<Video 1>")
        self.assertEqual(workflow["8"]["class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(workflow["8"]["inputs"]["ref_videos.ref_video_0"], ["101", 0])
        self.assertEqual(
            [key for key in workflow["8"]["inputs"] if key.startswith("ref_videos.ref_video_")],
            ["ref_videos.ref_video_0"],
        )
        self.assertEqual(
            sum(node.get("class_type") == "LoadVideo" for node in workflow.values()), 1,
        )
        self.assertEqual(
            sum(node.get("class_type") == "GetVideoComponents" for node in workflow.values()), 1,
        )
        self.assertFalse(any("Director" in str(node.get("class_type", "")) for node in workflow.values()))
        self.assertFalse(any(key.startswith("ref_video_audios.") for key in workflow["8"]["inputs"]))
        self.assertEqual(workflow["8"]["inputs"]["prompt"], spec.prompt)
        evidence = workflow_evidence(workflow, spec, "2" * 32)
        self.assertEqual(evidence["resolved_director_mode"], "v2v")
        self.assertEqual(evidence["source_video_tag"], "<Video 1>")
        self.assertEqual(evidence["audio_output"], "generated")

    def test_rv2v_node_layout_wires_source_first_then_image_and_audio(self) -> None:
        spec = parse_generation_request({
            "type": "video", "prompt": "Use all references",
            "director_mode": "rv2v", "source_asset_id": "c" * 32,
            "references": [
                {"asset_id": "a" * 32, "role": "identity"},
                {"asset_id": "d" * 32, "role": "music"},
                {"asset_id": "c" * 32, "role": "motion"},
            ],
        }, lookup)
        workflow = compile_video_workflow(spec, self.config, "3" * 32)
        inputs = workflow["8"]["inputs"]
        self.assertEqual([ref.asset_id for ref in spec.references], ["c" * 32, "a" * 32, "d" * 32])
        self.assertIn("<Video 1>", spec.prompt)
        self.assertEqual(inputs["ref_videos.ref_video_0"], ["101", 0])
        self.assertEqual(inputs["ref_images.ref_image_0"], ["102", 0])
        self.assertEqual(inputs["ref_audios.ref_audio_0"], ["104", 0])
        self.assertFalse(any(key.startswith("ref_video_audios.") for key in inputs))
        self.assertEqual(sum(node.get("class_type") == "LoadVideo" for node in workflow.values()), 1)
        self.assertEqual(sum(node.get("class_type") == "GetVideoComponents" for node in workflow.values()), 1)
        self.assertFalse(any("Director" in str(node.get("class_type", "")) for node in workflow.values()))

    def test_video_workflow_evidence_hashes_actual_conditioning_prompt(self) -> None:
        spec = parse_generation_request({"type": "video", "prompt": "ocean sunrise"}, lookup)
        workflow = compile_video_workflow(spec, self.config, "6" * 32)
        evidence = workflow_evidence(workflow, spec, "6" * 32)
        actual_prompt = workflow["8"]["inputs"]["prompt"]
        self.assertEqual(
            evidence["prompt_sha256"],
            hashlib.sha256(actual_prompt.encode("utf-8")).hexdigest(),
        )

    def test_preserve_tags_only_changes_only_stable_tokens_in_final_workflow_prompt(self) -> None:
        authored = f"  Keep @{{{'a' * 32}}}; copy motion from @{{{'c' * 32}}}; use music @{{{'d' * 32}}}.  \n"
        references = [
            {"asset_id": "a" * 32, "role": "identity"},
            {"asset_id": "c" * 32, "role": "motion", "include_audio": True},
            {"asset_id": "d" * 32, "role": "music"},
        ]
        spec = parse_generation_request({
            "type": "video",
            "prompt": authored,
            "prompt_mode": "preserve_tags_only",
            "parts": {"subject": "STALE STRUCTURED CONTENT MUST NOT APPEAR"},
            "references": references,
        }, lookup)
        expected = "  Keep <Picture 1>; copy motion from <Video 1>; use music <Audio 2>.  \n"
        self.assertEqual(spec.prompt, expected)
        self.assertEqual(spec.public_parameters()["prompt_mode"], "preserve_tags_only")
        workflow = compile_video_workflow(spec, self.config, "7" * 32)
        self.assertEqual(workflow["8"]["inputs"]["prompt"], expected)
        self.assertNotIn("subject_definitions", workflow["8"]["inputs"]["prompt"])
        self.assertNotIn("Reference directives", workflow["8"]["inputs"]["prompt"])
        self.assertNotIn("Use <", workflow["8"]["inputs"]["prompt"])
        self.assertNotIn("STALE STRUCTURED CONTENT", workflow["8"]["inputs"]["prompt"])

        ordinary = parse_generation_request({
            "type": "video", "prompt": authored, "references": references,
        }, lookup)
        self.assertIn("subject_definitions", ordinary.prompt)
        self.assertNotEqual(ordinary.prompt, expected)

    def test_real_text_to_image_workflow(self) -> None:
        profile = DEFAULT_REGISTRY.get("anything-v5-t2i")
        spec = parse_generation_request(
            {
                "type": "image",
                "prompt": "anime portrait",
                "negative_prompt": "blurry",
                "steps": 30,
                "cfg": 8,
                "seed": 9,
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_digest": profile.digest(),
            },
            lookup,
        )
        workflow = compile_image_workflow(spec, self.config, "3" * 32)
        self.assertEqual(workflow["1"]["inputs"]["ckpt_name"], "anything-v5-PrtRE.safetensors")
        self.assertEqual(workflow["5"]["class_type"], "KSampler")
        self.assertEqual(workflow["5"]["inputs"]["steps"], 30)
        self.assertEqual(workflow["7"]["class_type"], "SaveImage")

    def test_z_image_turbo_uses_official_eight_step_flow_graph(self) -> None:
        spec = parse_generation_request({"type": "image", "prompt": "日落下的上海街道"}, lookup)
        workflow = compile_workflow(spec, self.config, "7" * 32)
        self.assertEqual(spec.profile_id, "z-image-turbo-bf16-t2i")
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "z_image_turbo_bf16.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["clip_name"], "qwen_3_4b.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["type"], "lumina2")
        self.assertEqual(workflow["6"]["class_type"], "ConditioningZeroOut")
        self.assertEqual(workflow["8"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["8"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(workflow["8"]["inputs"]["scheduler"], "simple")

    def test_z_image_nsfw_lora_t2i_binds_reviewed_lora_and_adjustable_strength(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-t2i")
        spec = parse_generation_request({
            "type": "image", "prompt": "adult anime character", "loraStrength": 0.55,
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }, lookup)
        workflow = compile_workflow(spec, self.config, "a" * 32)
        self.assertEqual(spec.compiler, "z_image_lora_t2i")
        self.assertEqual(spec.lora_strength, 0.55)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "z_image_turbo_bf16.safetensors")
        self.assertEqual(workflow["4"]["inputs"]["clip_name"], "qwen_3_4b.safetensors")
        self.assertEqual(workflow["2"], {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0], "lora_name": "ZITnsfwLoRA.safetensors",
                "strength_model": 0.55,
            },
        })
        self.assertEqual(workflow["3"]["inputs"]["model"], ["2", 0])
        self.assertEqual(workflow["10"]["class_type"], "EmptySD3LatentImage")
        self.assertEqual(workflow["11"]["inputs"]["denoise"], 1.0)
        self.assertEqual(spec.image_lora, "ZITnsfwLoRA.safetensors")
        self.assertEqual(spec.public_parameters()["image_lora"], "ZITnsfwLoRA.safetensors")
        self.assertEqual(spec.public_parameters()["lora_strength"], 0.55)
        evidence = workflow_evidence(workflow, spec, "a" * 32)
        self.assertEqual((evidence["lora"], evidence["lora_strength"]), ("ZITnsfwLoRA.safetensors", 0.55))
        self.assertEqual(
            (evidence["image_lora"], evidence["image_lora_strength"]),
            ("ZITnsfwLoRA.safetensors", 0.55),
        )

    def test_z_image_turbo_latent_img2img_uses_fixed_lora_free_graph(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-bf16-img2img")
        identity = {
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }
        spec = parse_generation_request({
            "type": "image", "prompt": "restyle as watercolor",
            "parameters": {"denoise": 0.4}, "assets": [{"id": "a" * 32}],
            **identity,
        }, lookup)
        workflow = compile_workflow(spec, self.config, "0" * 32)
        self.assertEqual(spec.compiler, "z_image_img2img")
        self.assertEqual((spec.steps, spec.cfg, spec.denoise), (8, 1, 0.4))
        self.assertEqual((spec.sampler, spec.scheduler), ("res_multistep", "simple"))
        self.assertNotIn("LoraLoaderModelOnly", {
            node["class_type"] for node in workflow.values()
        })
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "z_image_turbo_bf16.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["clip_name"], "qwen_3_4b.safetensors")
        self.assertEqual(workflow["2"]["inputs"], {"model": ["1", 0], "shift": 3.0})
        self.assertEqual(workflow["3"]["inputs"]["type"], "lumina2")
        self.assertEqual(workflow["7"]["inputs"]["image"], "h3-studio/a.png")
        self.assertEqual(workflow["9"]["class_type"], "VAEEncode")
        self.assertEqual(workflow["10"]["inputs"], {
            "model": ["2", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["9", 0], "seed": spec.seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 0.4,
        })
        evidence = workflow_evidence(workflow, spec, "0" * 32)
        self.assertEqual((evidence["lora"], evidence["lora_strength"]), (None, 0))
        self.assertEqual((evidence["denoise"], evidence["steps"]), (0.4, 8))

        for parameters in ({"steps": 9}, {"cfg": 1.1}, {"denoise": 0.01}):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(ApiError, "must be between"):
                parse_generation_request({
                    "type": "image", "prompt": "x", "parameters": parameters,
                    "assets": [{"id": "a" * 32}], **identity,
                }, lookup)
        with self.assertRaisesRegex(ApiError, "reference limit"):
            parse_generation_request({
                "type": "image", "prompt": "x",
                "assets": [{"id": "a" * 32}, {"id": "b" * 32}], **identity,
            }, lookup)
        with self.assertRaisesRegex(ApiError, "does not support text-to-image"):
            parse_generation_request({"type": "image", "prompt": "x", **identity}, lookup)

    def test_z_image_turbo_latent_img2img_compiler_rejects_incompatible_spec(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-bf16-img2img")
        spec = parse_generation_request({
            "type": "image", "prompt": "x", "assets": [{"id": "a" * 32}],
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }, lookup)
        object.__setattr__(spec, "steps", 7)
        with self.assertRaisesRegex(CapabilityError, "requires 8 steps"):
            compile_z_image_img2img_workflow(spec, "1" * 32)

    def test_z_image_nsfw_lora_img2img_uses_one_image_and_adjustable_denoise(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-img2img")
        identity = {
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }
        spec = parse_generation_request({
            "type": "image", "prompt": "restyle the adult character",
            "parameters": {"lora_strength": 1.25, "denoise": 0.3},
            "assets": [{"id": "a" * 32}], **identity,
        }, lookup)
        workflow = compile_workflow(spec, self.config, "b" * 32)
        self.assertEqual((spec.lora_strength, spec.denoise), (1.25, 0.3))
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "z_image_turbo_bf16.safetensors")
        self.assertEqual(workflow["4"]["inputs"]["clip_name"], "qwen_3_4b.safetensors")
        self.assertEqual(workflow["8"]["inputs"]["image"], "h3-studio/a.png")
        self.assertEqual(workflow["9"]["class_type"], "ImageScale")
        self.assertEqual(workflow["10"]["class_type"], "VAEEncode")
        self.assertEqual(workflow["11"]["inputs"]["latent_image"], ["10", 0])
        self.assertEqual(workflow["11"]["inputs"]["denoise"], 0.3)
        self.assertEqual(workflow["2"]["inputs"]["strength_model"], 1.25)
        with self.assertRaisesRegex(ApiError, "reference limit"):
            parse_generation_request({
                "type": "image", "prompt": "x",
                "assets": [{"id": "a" * 32}, {"id": "b" * 32}], **identity,
            }, lookup)

    def test_z_image_nsfw_profiles_reject_wrong_mode_and_parameter_bounds(self) -> None:
        text = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-t2i")
        image = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-img2img")
        with self.assertRaisesRegex(ApiError, "does not accept"):
            parse_generation_request({
                "type": "image", "prompt": "x", "assets": [{"id": "a" * 32}],
                "profile_id": text.id, "profile_version": text.version,
                "profile_digest": text.digest(),
            }, lookup)
        for parameters in ({"lora_strength": 1.26}, {"denoise": 0.01}):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(ApiError, "must be between"):
                parse_generation_request({
                    "type": "image", "prompt": "x", "assets": [{"id": "a" * 32}],
                    "parameters": parameters, "profile_id": image.id,
                    "profile_version": image.version, "profile_digest": image.digest(),
                }, lookup)

    def test_z_image_nsfw_compiler_defends_against_forged_bindings(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-t2i")
        spec = parse_generation_request({
            "type": "image", "prompt": "x", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
        }, lookup)
        object.__setattr__(spec, "image_lora", "../../outside.safetensors")
        with self.assertRaisesRegex(CapabilityError, "binding identity changed"):
            compile_z_image_lora_workflow(spec, "c" * 32)

    def test_qwen_2512_uses_official_base_quality_graph_and_prompt_suffix(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2512-fp8-t2i")
        spec = parse_generation_request({
            "type": "image", "prompt": "窗边的电影感人像", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
        }, lookup)
        workflow = compile_workflow(spec, self.config, "8" * 32)
        self.assertIn("超清，4K，电影级构图", spec.prompt)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "qwen_image_2512_fp8_e4m3fn.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["type"], "qwen_image")
        self.assertEqual(workflow["8"]["inputs"]["steps"], 50)
        self.assertEqual(workflow["8"]["inputs"]["cfg"], 4)
        evidence = workflow_evidence(workflow, spec, "8" * 32)
        self.assertEqual(
            evidence["prompt_sha256"],
            hashlib.sha256(spec.prompt.encode("utf-8")).hexdigest(),
        )

    def test_qwen_2512_does_not_treat_accented_latin_as_chinese(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2512-fp8-t2i")
        spec = parse_generation_request({
            "type": "image", "prompt": "cinematic café portrait", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
        }, lookup)
        self.assertIn("Ultra HD, 4K, cinematic composition", spec.prompt)

    def test_qwen_2511_edit_conditions_on_source_image_and_denoise(self) -> None:
        spec = parse_generation_request({
            "type": "image", "prompt": "保留人物，把背景改成雨夜霓虹街道",
            "parameters": {"denoise": 0.7}, "assets": [{"id": "a" * 32}],
        }, lookup)
        workflow = compile_workflow(spec, self.config, "9" * 32)
        self.assertEqual(spec.profile_id, "qwen-image-edit-2511-int8")
        self.assertEqual(workflow["7"]["class_type"], "TextEncodeQwenImageEditPlus")
        self.assertEqual(workflow["7"]["inputs"]["image1"], ["6", 0])
        self.assertEqual(workflow["9"]["class_type"], "FluxKontextMultiReferenceLatentMethod")
        self.assertEqual(workflow["15"]["class_type"], "CFGNorm")
        self.assertEqual(workflow["12"]["inputs"]["model"], ["15", 0])
        self.assertEqual(workflow["12"]["inputs"]["denoise"], 0.7)
        self.assertNotIn("<Picture 1>", spec.prompt)

    def test_flux2_klein_compiler_chains_ordered_reference_latents_on_both_conditions(self) -> None:
        profile = DEFAULT_REGISTRY.get("flux2-klein-4b-fp8")
        spec = parse_generation_request({
            "type": "image", "prompt": "subject from @subject, style from @style",
            "assets": [
                {"id": "a" * 32, "label": "subject", "reference_index": 0},
                {"id": "b" * 32, "label": "style", "reference_index": 1},
            ],
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }, lookup)
        workflow = compile_workflow(spec, self.config, "f" * 32)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "flux-2-klein-4b-fp8.safetensors")
        self.assertEqual(workflow["2"]["inputs"]["type"], "flux2")
        self.assertEqual(workflow["8"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(workflow["9"]["inputs"], {"steps": 4, "width": 1024, "height": 576})
        self.assertEqual(workflow["10"]["inputs"]["cfg"], 1.0)
        self.assertEqual(workflow["100"]["inputs"]["image"], "h3-studio/a.png")
        self.assertEqual(workflow["105"]["inputs"]["image"], "h3-studio/b.png")
        self.assertEqual(workflow["103"]["inputs"]["conditioning"], ["4", 0])
        self.assertEqual(workflow["108"]["inputs"]["conditioning"], ["103", 0])
        self.assertEqual(workflow["104"]["inputs"]["conditioning"], ["5", 0])
        self.assertEqual(workflow["109"]["inputs"]["conditioning"], ["104", 0])
        self.assertEqual(workflow["10"]["inputs"]["positive"], ["108", 0])
        self.assertEqual(workflow["10"]["inputs"]["negative"], ["109", 0])
        evidence = workflow_evidence(workflow, spec, "f" * 32)
        self.assertEqual((evidence["steps"], evidence["sampler"], evidence["scheduler"]), (4, "euler", "flux2"))
        self.assertIsNone(evidence["denoise"])

    def test_image_checkpoint_must_be_configured(self) -> None:
        profile = DEFAULT_REGISTRY.get("anything-v5-t2i")
        spec = parse_generation_request({
            "type": "image", "prompt": "portrait", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
        }, lookup)
        with self.assertRaises(CapabilityError):
            compile_image_workflow(spec, config(Path(self.temp.name), checkpoint=""), "4" * 32)

    def test_qwen_image_21_bf16_text_generation_uses_native_graph_and_2k_resolution(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2.1-bf16")
        spec = parse_generation_request({
            "type": "image", "prompt": "一只陶瓷茶壶", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
            "parameters": {"aspect_ratio": "16:9", "seed": 42},
        }, lookup)
        self.assertEqual((spec.width, spec.height, spec.steps, spec.cfg), (2752, 1536, 40, 1))
        workflow = compile_workflow(spec, self.config, "1" * 32)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "qwen_image_2.1_bf16.safetensors")
        self.assertEqual(workflow["2"]["inputs"]["clip_name"], "qwen3vl_8b_bf16.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["vae_name"], "qwen_image_2.1_vae_bf16.safetensors")
        self.assertEqual(workflow["4"]["inputs"]["dtype"], "default")
        self.assertEqual(workflow["5"]["class_type"], "TextEncodeQwenImage21")
        self.assertEqual(workflow["6"]["inputs"], {"width": 2752, "height": 1536, "batch_size": 1})
        self.assertEqual(workflow["7"]["inputs"]["positive"], ["5", 0])
        self.assertEqual(workflow["7"]["inputs"]["negative"], ["5", 1])
        self.assertEqual(workflow["7"]["inputs"]["latent_image"], ["6", 0])
        self.assertEqual(workflow_evidence(workflow, spec, "1" * 32)["prompt_sha256"], hashlib.sha256(spec.prompt.encode()).hexdigest())

    def test_qwen_image_21_edit_binds_ordered_images_without_latent_denoise(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2.1-bf16")
        spec = parse_generation_request({
            "type": "image", "prompt": "保留图1人物，换上图2的衣服，参考 <image2>",
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "assets": [
                {"id": "a" * 32, "reference_index": 1},
                {"id": "b" * 32, "reference_index": 0},
            ],
            "parameters": {"width": 2048, "height": 2048, "seed": 9},
        }, lookup)
        self.assertEqual([ref.asset_id for ref in spec.references], ["b" * 32, "a" * 32])
        self.assertEqual(spec.prompt, "保留<image1>人物，换上<image2>的衣服，参考 <image2>")
        workflow = compile_workflow(spec, self.config, "2" * 32)
        self.assertEqual(workflow["5"]["inputs"]["images.image_1"], ["103", 0])
        self.assertEqual(workflow["5"]["inputs"]["images.image_2"], ["105", 0])
        self.assertEqual(workflow["5"]["inputs"]["resolution"], 0)
        self.assertEqual(workflow["7"]["inputs"]["latent_image"], ["5", 2])
        self.assertEqual(workflow["7"]["inputs"]["denoise"], 1.0)
        self.assertNotIn("denoise", spec.public_parameters())
        self.assertNotIn("6", workflow)
        with self.assertRaisesRegex(ApiError, "does not expose latent denoise"):
            parse_generation_request({
                "type": "image", "prompt": "edit", "profile_id": profile.id,
                "profile_version": profile.version, "profile_digest": profile.digest(),
                "assets": [{"id": "a" * 32}], "parameters": {"denoise": 0.5},
            }, lookup)

    def test_qwen_image_21_prompt_preview_matches_submitted_edit_prompt(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2.1-bf16")
        request = {
            "output_type": "image", "prompt": "保留图1主体，采用 <image2> 配色",
            "profile_id": profile.id,
            "references": [
                {"asset_id": "a" * 32, "reference_index": 0},
                {"asset_id": "b" * 32, "reference_index": 1},
            ],
        }
        preview = compile_prompt_request(request, lookup)
        spec = parse_generation_request({
            **request, "profile_version": profile.version, "profile_digest": profile.digest(),
        }, lookup)
        self.assertEqual(preview["prompt"], spec.prompt)
        self.assertEqual(preview["prompt"], "保留<image1>主体，采用 <image2> 配色")

    def test_qwen_image_21_accepts_ten_images_and_rejects_missing_or_eleventh_reference(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2.1-bf16")
        assets = {f"{i:032x}": {
            "id": f"{i:032x}", "kind": "image", "filename": f"{i}.png", "comfy_path": f"h3-studio/{i}.png",
        } for i in range(1, 12)}
        request = {"type": "image", "prompt": "合成图10", "profile_id": profile.id,
                   "profile_version": profile.version, "profile_digest": profile.digest(),
                   "assets": [{"id": key, "reference_index": index} for index, key in enumerate(list(assets)[:10])]}
        spec = parse_generation_request(request, assets.__getitem__)
        self.assertEqual(len(spec.references), 10)
        self.assertIn("<image10>", spec.prompt)
        self.assertEqual(len([n for n in compile_workflow(spec, self.config, "3" * 32).values() if n["class_type"] == "LoadImage"]), 10)
        with self.assertRaisesRegex(ApiError, "at most 10"):
            parse_generation_request({**request, "assets": [{"id": key} for key in assets]}, assets.__getitem__)
        with self.assertRaisesRegex(ApiError, "no connected reference"):
            parse_generation_request({**request, "prompt": "use <image11>"}, assets.__getitem__)

    def test_image_to_image_scales_encodes_and_uses_denoise(self) -> None:
        profile = DEFAULT_REGISTRY.get("anything-v5-img2img")
        spec = parse_generation_request(
            {
                "type": "image", "prompt": "watercolor", "parameters": {"denoise": 0.35},
                "assets": [{"id": "a" * 32}], "profile_id": profile.id,
                "profile_version": profile.version, "profile_digest": profile.digest(),
            }, lookup
        )
        workflow = compile_image_workflow(spec, self.config, "5" * 32)
        self.assertEqual(workflow["8"]["class_type"], "LoadImage")
        self.assertEqual(workflow["9"]["class_type"], "ImageScale")
        self.assertEqual(workflow["10"]["class_type"], "VAEEncode")
        self.assertEqual(workflow["5"]["inputs"]["latent_image"], ["10", 0])
        self.assertEqual(workflow["5"]["inputs"]["denoise"], 0.35)


if __name__ == "__main__":
    unittest.main()
