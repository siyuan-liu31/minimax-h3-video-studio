from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.comfy import ComfyClient, find_outputs
from server.errors import ApiError, CapabilityError
from server.profiles import DEFAULT_REGISTRY, H3_MAX_DURATION_SECONDS
from server.tests.test_workflows import config, lookup
from server.workflows import parse_generation_request


def choices(name: str, values: list[str]):
    return {"input": {"required": {name: [values]}}}


def combo_choices(name: str, values: list[str]):
    return {"input": {"required": {name: ["COMBO", {"options": values}]}}}


def full_object_info():
    names = {
        "PathchSageAttentionKJ",
        "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "RandomNoise",
        "BasicGuider",
        "KSamplerSelect",
        "BasicScheduler",
        "SamplerCustomAdvanced",
        "VAEDecode",
        "VAEDecodeAudio",
        "CreateVideo",
        "SaveVideo",
        "LoadImage",
        "LoadVideo",
        "LoadAudio",
        "GetVideoComponents",
        "CLIPTextEncode",
        "EmptyLatentImage",
        "KSampler",
        "SaveImage",
        "SplitSigmas",
        "H3StudioSaveLatent",
        "H3StudioLoadLatent",
        "DisableNoise",
    }
    info = {name: {} for name in names}
    info["UNETLoader"] = choices("unet_name", ["fl.safetensors", "ref.safetensors"])
    info["LoraLoaderModelOnly"] = choices("lora_name", ["fl-lora.safetensors", "ref-lora.safetensors"])
    info["KSamplerSelect"] = choices("sampler_name", ["sa_solver", "res_multistep"])
    info["BasicScheduler"] = choices("scheduler", ["simple"])
    info["CLIPLoader"] = choices("clip_name", ["clip.safetensors"])
    info["VAELoader"] = choices("vae_name", ["video-vae.safetensors", "audio-vae.safetensors"])
    info["CheckpointLoaderSimple"] = choices("ckpt_name", ["anything-v5-PrtRE.safetensors"])
    return info


def motion_context_object_info():
    info = full_object_info()
    info["MiniMaxH3MotionContext"] = {"input": {
        "required": {name: ["ANY"] for name in ("conditioning", "vae", "latent", "context_length", "audio_context_length")},
        "optional": {"context_latent": ["LATENT"]},
    }}
    info["MiniMaxH3MotionContextTrim"] = {"input": {
        "required": {name: ["ANY"] for name in ("images", "trim_frames")},
        "optional": {name: ["ANY"] for name in ("audio", "fps", "match_tail")},
    }}
    info["H3StudioSaveLatent"] = {"input": {"required": {
        name: ["ANY"] for name in ("samples", "video_done", "filename_prefix")
    }}}
    info["H3StudioLoadLatent"] = {"input": {"required": {"latent": ["ANY"]}}}
    return info


def quality_image_object_info():
    info = full_object_info()
    info.update({name: {} for name in {
        "ModelSamplingAuraFlow", "ConditioningZeroOut", "EmptySD3LatentImage",
        "ImageScale", "VAEEncode", "TextEncodeQwenImageEditPlus",
        "FluxKontextMultiReferenceLatentMethod", "CFGNorm",
    }})
    info["UNETLoader"] = choices("unet_name", [
        "fl.safetensors", "ref.safetensors", "z_image_turbo_bf16.safetensors",
        "z_image_turbo_int8_convrot.safetensors",
        "qwen_image_2512_fp8_e4m3fn.safetensors", "qwen_image_edit_2511_int8_convrot.safetensors",
    ])
    info["CLIPLoader"] = {"input": {"required": {
        "clip_name": [[
            "clip.safetensors", "qwen_3_4b.safetensors", "qwen_3_4b_fp8_mixed.safetensors",
            "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        ]],
        "type": [["lumina2", "qwen_image"]],
    }}}
    info["VAELoader"] = choices("vae_name", [
        "video-vae.safetensors", "audio-vae.safetensors", "ae.safetensors", "qwen_image_vae.safetensors",
    ])
    info["KSampler"] = {"input": {"required": {
        **{name: ["ANY"] for name in (
            "model", "positive", "negative", "latent_image", "seed", "steps", "cfg", "denoise",
        )},
        "sampler_name": [["euler", "euler_ancestral", "res_multistep"]],
        "scheduler": [["simple", "normal"]],
    }}}
    info["ModelSamplingAuraFlow"] = {"input": {"required": {
        "model": ["MODEL"], "shift": ["FLOAT"],
    }}}
    info["ImageScale"] = {"input": {"required": {
        name: ["ANY"] for name in ("image", "upscale_method", "width", "height", "crop")
    }}}
    info["VAEEncode"] = {"input": {"required": {
        "pixels": ["IMAGE"], "vae": ["VAE"],
    }}}
    return info


def flux2_klein_object_info(*, installed: bool):
    info = quality_image_object_info()
    info.update({name: {} for name in {
        "ConditioningZeroOut", "ImageScaleToTotalPixels", "ReferenceLatent",
        "EmptyFlux2LatentImage", "RandomNoise", "CFGGuider", "Flux2Scheduler",
        "KSamplerSelect", "SamplerCustomAdvanced",
    }})
    unets = [
        "fl.safetensors", "ref.safetensors", "z_image_turbo_int8_convrot.safetensors",
        "qwen_image_2512_fp8_e4m3fn.safetensors", "qwen_image_edit_2511_int8_convrot.safetensors",
    ]
    clips = [
        "clip.safetensors", "qwen_3_4b_fp8_mixed.safetensors", "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    ]
    vaes = [
        "video-vae.safetensors", "audio-vae.safetensors", "ae.safetensors", "qwen_image_vae.safetensors",
    ]
    if installed:
        unets.append("flux-2-klein-4b-fp8.safetensors")
        clips.append("qwen_3_4b.safetensors")
        vaes.append("flux2-vae.safetensors")
    info["UNETLoader"] = choices("unet_name", unets)
    info["CLIPLoader"] = {"input": {"required": {
        "clip_name": [clips], "type": [["qwen_image", "flux2", "lumina2"]],
    }}}
    info["VAELoader"] = choices("vae_name", vaes)
    info["KSamplerSelect"] = combo_choices("sampler_name", ["euler", "sa_solver", "res_multistep"])
    for node, names in {
        "ReferenceLatent": ("conditioning", "latent"),
        "ImageScaleToTotalPixels": ("image", "upscale_method", "megapixels", "resolution_steps"),
        "EmptyFlux2LatentImage": ("width", "height", "batch_size"),
        "Flux2Scheduler": ("steps", "width", "height"),
        "CFGGuider": ("model", "positive", "negative", "cfg"),
        "SamplerCustomAdvanced": ("noise", "guider", "sampler", "sigmas", "latent_image"),
    }.items():
        info[node] = {"input": {"required": {name: ["ANY"] for name in names}}}
    return info


def qwen_image_21_object_info(*, installed: bool, complete_schema: bool = True):
    info = quality_image_object_info()
    info.update({name: {} for name in {"ImageScaleToTotalPixels", "QwenImage21Cache", "TextEncodeQwenImage21"}})
    unets = ["z_image_turbo_bf16.safetensors"]
    clips = ["qwen_3_4b.safetensors"]
    vaes = ["ae.safetensors"]
    if installed:
        unets.append("qwen_image_2.1_bf16.safetensors")
        clips.append("qwen3vl_8b_bf16.safetensors")
        vaes.append("qwen_image_2.1_vae_bf16.safetensors")
    info["UNETLoader"] = {"input": {"required": {
        "unet_name": [unets], "weight_dtype": [["default", "fp8_e4m3fn"]],
    }}}
    info["CLIPLoader"] = {"input": {"required": {
        "clip_name": [clips], "type": [["qwen_image"]],
    }, "optional": {"device": [["default", "cpu"]]}}}
    info["VAELoader"] = choices("vae_name", vaes)
    for node, names in {
        "TextEncodeQwenImage21": ("clip", "prompt", "negative_prompt", "vae", "resolution", "images"),
        "QwenImage21Cache": ("model", "device", "dtype"),
        "EmptyLatentImage": ("width", "height", "batch_size"),
        "ImageScaleToTotalPixels": ("image", "upscale_method", "megapixels", "resolution_steps"),
    }.items():
        info[node] = {"input": {"required": {name: ["ANY"] for name in names}}}
    if not complete_schema:
        info["TextEncodeQwenImage21"]["input"]["required"].pop("images")
    info["QwenImage21Cache"]["input"]["required"].update({
        "device": ["COMBO", {"options": ["auto", "gpu", "cpu"]}],
        "dtype": ["COMBO", {"options": ["default", "int8", "int4"]}],
    })
    return info


def z_image_lora_object_info(*, installed: bool, complete_schema: bool = True):
    info = quality_image_object_info()
    info["UNETLoader"] = choices("unet_name", [
        "z_image_turbo_bf16.safetensors", "z_image_turbo_int8_convrot.safetensors",
    ])
    info["CLIPLoader"] = {"input": {"required": {
        "clip_name": [["qwen_3_4b.safetensors", "qwen_3_4b_fp8_mixed.safetensors"]],
        "type": [["lumina2"]],
    }}}
    info["VAELoader"] = choices("vae_name", ["ae.safetensors"])
    loras = ["ZITnsfwLoRA.safetensors"] if installed else []
    info["LoraLoaderModelOnly"] = {"input": {"required": {
        "model": ["MODEL"], "lora_name": [loras], "strength_model": ["FLOAT"],
    }}}
    for node, names in {
        "ModelSamplingAuraFlow": ("model", "shift"),
        "KSampler": (
            "model", "positive", "negative", "latent_image", "seed", "steps",
            "cfg", "sampler_name", "scheduler", "denoise",
        ),
        "ImageScale": ("image", "upscale_method", "width", "height", "crop"),
        "VAEEncode": ("pixels", "vae"),
    }.items():
        info[node] = {"input": {"required": {name: ["ANY"] for name in names}}}
    info["KSampler"]["input"]["required"].update({
        "sampler_name": [["res_multistep"]], "scheduler": [["simple"]],
    })
    if not complete_schema:
        info["LoraLoaderModelOnly"]["input"]["required"].pop("strength_model")
    return info


class CapabilityTests(unittest.TestCase):
    def test_execution_environment_detects_blackwell_from_current_comfy_device_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            with patch.object(client, "health", return_value={"devices": [{"name": "cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync"}]}):
                value = client.execution_environment(config(Path(directory)))
            self.assertIn("sm120", value["gpu_architecture"])
            self.assertEqual(value["attention_backend"], "SageAttention")

    def test_z_image_latent_img2img_capability_and_edit_placeholder_are_honest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            info = z_image_lora_object_info(installed=False)
            with patch.object(client, "object_info", return_value=info):
                value = client.capabilities(config(Path(directory)))
            profiles = {profile["id"]: profile for profile in value["profiles"]}
            latent = profiles["z-image-turbo-int8-img2img"]
            self.assertTrue(latent["available"])
            self.assertEqual(latent["missing_models"], [])
            self.assertNotIn("image_lora", latent["required_models"])
            self.assertEqual(latent["reference_contract"]["min_count"], 1)

            placeholders = value["image"]["unavailable_profiles"]
            self.assertEqual(len(placeholders), 1)
            edit = placeholders[0]
            self.assertEqual(edit["id"], "z-image-edit-unreleased")
            self.assertFalse(edit["available"])
            self.assertFalse(edit["selectable"])
            self.assertTrue(edit["placeholder"])
            self.assertEqual(edit["status"], "unreleased")
            self.assertIn("尚无已发布", edit["reason"])
            self.assertIn("latent img2img 不等同于 Z-Image-Edit", edit["reason"])

    def test_z_image_nsfw_lora_capability_requires_exact_lora_and_node_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            with patch.object(client, "object_info", return_value=z_image_lora_object_info(installed=False)):
                profiles = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}
            for identifier in (
                "z-image-turbo-zit-nsfw-t2i",
                "z-image-turbo-zit-nsfw-img2img",
            ):
                self.assertFalse(profiles[identifier]["available"])
                self.assertEqual(profiles[identifier]["missing_models"], ["image_lora"])
                self.assertEqual(profiles[identifier]["missing_model_files"], ["ZITnsfwLoRA.safetensors"])

            with patch.object(client, "object_info", return_value=z_image_lora_object_info(installed=True)):
                profiles = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}
            self.assertTrue(profiles["z-image-turbo-zit-nsfw-t2i"]["available"])
            self.assertTrue(profiles["z-image-turbo-zit-nsfw-img2img"]["available"])

            broken = z_image_lora_object_info(installed=True, complete_schema=False)
            with patch.object(client, "object_info", return_value=broken):
                profile = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[
                    "z-image-turbo-zit-nsfw-t2i"
                ]
            self.assertFalse(profile["available"])
            self.assertIn("LoraLoaderModelOnly.inputs=strength_model", profile["missing_options"])

    def test_flux2_klein_capability_is_schema_and_exact_weight_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            with patch.object(client, "object_info", return_value=flux2_klein_object_info(installed=False)):
                missing = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}["flux2-klein-4b-fp8"]
            self.assertFalse(missing["available"])
            self.assertEqual(missing["missing_nodes"], [])
            self.assertEqual(missing["missing_options"], [])
            self.assertEqual(missing["missing_model_files"], [
                "flux-2-klein-4b-fp8.safetensors", "qwen_3_4b.safetensors", "flux2-vae.safetensors",
            ])
            self.assertEqual(missing["reference_contract"]["max_count"], 4)

            with patch.object(client, "object_info", return_value=flux2_klein_object_info(installed=True)):
                profiles = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}
            self.assertTrue(profiles["flux2-klein-4b-fp8"]["available"])
            self.assertFalse(profiles["flux2-klein-9b-fp8"]["available"])
            self.assertEqual(profiles["flux2-klein-9b-fp8"]["missing_model_files"], [
                "flux-2-klein-9b-fp8.safetensors",
                "qwen_3_8b_fp8mixed.safetensors",
                "full_encoder_small_decoder.safetensors",
            ])

    def test_qwen_image_21_capability_requires_native_nodes_exact_bf16_weights_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            identifier = "qwen-image-2.1-bf16"
            with patch.object(client, "object_info", return_value=quality_image_object_info()):
                missing_nodes = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[identifier]
            self.assertFalse(missing_nodes["available"])
            self.assertIn("TextEncodeQwenImage21", missing_nodes["missing_nodes"])
            self.assertIn("QwenImage21Cache", missing_nodes["missing_nodes"])

            with patch.object(client, "object_info", return_value=qwen_image_21_object_info(installed=False)):
                missing_weights = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[identifier]
            self.assertFalse(missing_weights["available"])
            self.assertEqual(missing_weights["missing_model_files"], [
                "qwen_image_2.1_bf16.safetensors", "qwen3vl_8b_bf16.safetensors",
                "qwen_image_2.1_vae_bf16.safetensors",
            ])

            with patch.object(client, "object_info", return_value=qwen_image_21_object_info(installed=True)):
                available = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[identifier]
            self.assertTrue(available["available"])
            self.assertEqual(available["missing_models"], [])

            with patch.object(client, "object_info", return_value=qwen_image_21_object_info(installed=True, complete_schema=False)):
                incompatible = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[identifier]
            self.assertFalse(incompatible["available"])
            self.assertIn("TextEncodeQwenImage21.inputs=images", incompatible["missing_options"])

            lossy_only = qwen_image_21_object_info(installed=True)
            lossy_only["QwenImage21Cache"]["input"]["required"]["dtype"] = ["COMBO", {"options": ["int8", "int4"]}]
            with patch.object(client, "object_info", return_value=lossy_only):
                unsupported = {p["id"]: p for p in client.capabilities(config(Path(directory)))["profiles"]}[identifier]
            self.assertFalse(unsupported["available"])
            self.assertIn("QwenImage21Cache.dtype=default", unsupported["missing_options"])

    def test_quality_image_profiles_require_their_exact_models_and_sampling_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            with patch.object(client, "object_info", return_value=quality_image_object_info()):
                value = client.capabilities(config(Path(directory)))
            by_id = {profile["id"]: profile for profile in value["profiles"]}
            self.assertTrue(by_id["z-image-turbo-bf16-t2i"]["available"])
            self.assertTrue(by_id["z-image-turbo-bf16-img2img"]["available"])
            self.assertTrue(by_id["z-image-turbo-int8-t2i"]["available"])
            self.assertTrue(by_id["qwen-image-2512-fp8-t2i"]["available"])
            self.assertTrue(by_id["qwen-image-edit-2511-int8"]["available"])
            self.assertTrue(value["image"]["modes"]["text-to-image"])
            self.assertTrue(value["image"]["modes"]["image-to-image"])
    def test_choices_support_current_comfy_combo_schema(self) -> None:
        info = {
            "KSamplerSelect": combo_choices(
                "sampler_name", ["sa_solver", "res_multistep"]
            )
        }
        self.assertEqual(
            ComfyClient._choices(info, "KSamplerSelect", "sampler_name"),
            {"sa_solver", "res_multistep"},
        )

    def test_capabilities_require_models_nodes_and_loras(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            with patch.object(client, "object_info", return_value=full_object_info()):
                value = client.capabilities(config(Path(directory)))
            self.assertTrue(value["video"]["modes"]["text"])
            self.assertTrue(value["video"]["modes"]["ref2va"])
            self.assertTrue(value["image"]["available"])
            self.assertEqual(
                value["video"]["duration_seconds"],
                {"min": 5, "max": H3_MAX_DURATION_SECONDS},
            )
            by_id = {profile["id"]: profile for profile in value["profiles"]}
            self.assertTrue(by_id["anything-v5-t2i"]["available"])
            self.assertFalse(by_id["anything-v5-img2img"]["available"])

    def test_motion_context_capability_requires_pinned_node_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = config(Path(directory))
            client = ComfyClient("http://unused")
            with patch.object(client, "object_info", return_value=motion_context_object_info()):
                capability = client.capabilities(settings)["video"]["motion_context"]
                client.ensure_motion_context(settings)
            self.assertTrue(capability["available"])
            self.assertEqual(capability["video_frames"], [5, 22, 39, 56])
            self.assertEqual(capability["upstream"]["tag"], "v0.5.1")
            self.assertEqual(
                capability["upstream"]["commit"],
                "429e952ae5c09b54f44cb6e3bef7331d998f0656",
            )

            broken = motion_context_object_info()
            broken["MiniMaxH3MotionContext"]["input"]["optional"].pop("context_latent")
            with patch.object(client, "object_info", return_value=broken):
                with self.assertRaisesRegex(CapabilityError, "context_latent"):
                    client.ensure_motion_context(settings)

    def test_missing_checkpoint_has_clear_capability_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            info = full_object_info()
            info["CheckpointLoaderSimple"] = choices("ckpt_name", [])
            profile = DEFAULT_REGISTRY.get("anything-v5-t2i")
            spec = parse_generation_request({
                "type": "image", "prompt": "portrait", "profile_id": profile.id,
                "profile_version": profile.version, "profile_digest": profile.digest(),
            }, lookup)
            with patch.object(client, "object_info", return_value=info):
                with self.assertRaisesRegex(CapabilityError, "checkpoint"):
                    client.ensure_capability(spec, config(Path(directory)))

    def test_base_video_profiles_do_not_require_turbo_lora(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            info = full_object_info()
            info.pop("LoraLoaderModelOnly")
            with patch.object(client, "object_info", return_value=info):
                value = client.capabilities(config(Path(directory)))
                by_id = {profile["id"]: profile for profile in value["profiles"]}
            self.assertFalse(by_id["minimax-h3-fl2va"]["available"])
            self.assertFalse(by_id["minimax-h3-ref2va"]["available"])
            self.assertTrue(by_id["minimax-h3-fl2va-base"]["available"])
            self.assertTrue(by_id["minimax-h3-ref2va-base"]["available"])
            self.assertTrue(value["video"]["available"])
            self.assertTrue(value["video"]["modes"]["text"])
            self.assertTrue(value["video"]["modes"]["fl2va"])
            self.assertTrue(value["video"]["modes"]["ref2va"])

    def test_direct_base_stays_available_without_optional_checkpoint_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            info = full_object_info()
            info.pop("H3StudioSaveLatent")
            info.pop("H3StudioLoadLatent")
            with patch.object(client, "object_info", return_value=info):
                by_id = {
                    profile["id"]: profile
                    for profile in client.capabilities(config(Path(directory)))["profiles"]
                }
            self.assertTrue(by_id["minimax-h3-fl2va-base"]["available"])
            self.assertTrue(by_id["minimax-h3-ref2va-base"]["available"])
            self.assertFalse(by_id["minimax-h3-fl2va-base-resumable"]["available"])
            self.assertFalse(by_id["minimax-h3-ref2va-base-resumable"]["available"])

    def test_sampling_profiles_require_their_sampler_and_scheduler_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ComfyClient("http://unused")
            info = full_object_info()
            info["KSamplerSelect"] = choices("sampler_name", ["sa_solver"])
            with patch.object(client, "object_info", return_value=info):
                by_id = {profile["id"]: profile for profile in client.capabilities(config(Path(directory)))["profiles"]}
            self.assertTrue(by_id["minimax-h3-fl2va"]["available"])
            self.assertFalse(by_id["minimax-h3-fl2va-base"]["available"])
            self.assertIn("res_multistep", str(by_id["minimax-h3-fl2va-base"]))

            info = full_object_info()
            info["BasicScheduler"] = choices("scheduler", [])
            with patch.object(client, "object_info", return_value=info):
                by_id = {profile["id"]: profile for profile in client.capabilities(config(Path(directory)))["profiles"]}
            for identifier in (
                "minimax-h3-fl2va", "minimax-h3-fl2va-base",
                "minimax-h3-ref2va", "minimax-h3-ref2va-base",
            ):
                self.assertFalse(by_id[identifier]["available"])
                self.assertIn("simple", str(by_id[identifier]))


class OutputTests(unittest.TestCase):
    def test_output_finder_excludes_input_and_temporary_previews(self) -> None:
        record = {"outputs": {
            "100": {"images": [{"filename": "reference.mp4", "subfolder": "h3-studio", "type": "input"}]},
            "101": {"images": [{"filename": "preview.mp4", "type": "temp"}]},
            "17": {"images": [{"filename": "generated.mp4", "subfolder": "h3-studio/videos", "type": "output"}]},
        }}
        self.assertEqual(find_outputs(record, "video"), [
            {"filename": "generated.mp4", "subfolder": "h3-studio/videos", "type": "output"},
        ])
        del record["outputs"]["17"]
        self.assertEqual(find_outputs(record, "video"), [])

    def test_output_finder_preserves_legacy_default_and_deduplicates(self) -> None:
        record = {"outputs": {"1": {"images": [
            {"filename": "image.png"},
            {"filename": "image.png", "type": "output"},
            {"filename": "input.png", "type": "input"},
            {"filename": "preview.png", "type": "temp"},
        ]}}}
        self.assertEqual(find_outputs(record, "image"), [
            {"filename": "image.png", "subfolder": "", "type": "output"},
        ])

    def test_output_finder_handles_nested_comfy_shapes(self) -> None:
        record = {
            "outputs": {
                "17": {
                    "videos": [
                        {"filename": "clip.mp4", "subfolder": "h3-studio/videos", "type": "output"}
                    ]
                }
            }
        }
        self.assertEqual(find_outputs(record, "video")[0]["filename"], "clip.mp4")
        self.assertEqual(find_outputs(record, "image"), [])

    def test_client_id_reconciliation_searches_completed_global_history(self) -> None:
        client = ComfyClient("http://unused")
        prompt_id = "completed-prompt"
        responses = {
            "/queue": {"queue_running": [], "queue_pending": []},
            "/history?max_items=200": {
                prompt_id: {
                    "prompt": [17, prompt_id, {"1": {}}, {"client_id": "stable-client"}],
                    "status": {"completed": True},
                }
            },
        }
        with patch.object(client, "request", side_effect=lambda path, **_kwargs: responses[path]):
            self.assertEqual(client.find_prompt_by_client_id("stable-client"), prompt_id)


class MemoryManagementTests(unittest.TestCase):
    @staticmethod
    def h3_workflow(model: str = "ref.safetensors") -> dict[str, object]:
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
            "3": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
            "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip.safetensors", "type": "minimax", "device": "default"}},
            "6": {"class_type": "VAELoader", "inputs": {"vae_name": "video-vae.safetensors"}},
            "7": {"class_type": "VAELoader", "inputs": {"vae_name": "audio-vae.safetensors"}},
        }

    def test_idle_free_waits_for_global_queue_and_runs_once(self) -> None:
        client = ComfyClient("http://unused")
        client._last_busy_at = 10
        calls: list[str] = []
        queues = [
            {"queue_running": [[1, "prompt"]], "queue_pending": []},
            {"queue_running": [], "queue_pending": []},
            {"queue_running": [], "queue_pending": []},
            {"queue_running": [], "queue_pending": []},
        ]

        def request(path: str, **_kwargs):
            calls.append(path)
            return queues.pop(0) if path == "/queue" else {}

        with patch.object(client, "request", side_effect=request):
            self.assertFalse(client.free_memory_if_idle(30, now=100))
            self.assertFalse(client.free_memory_if_idle(30, now=120))
            self.assertTrue(client.free_memory_if_idle(30, now=131))
            self.assertFalse(client.free_memory_if_idle(30, now=200))
        self.assertEqual(calls.count("/free"), 1)

    def test_identical_h3_resource_set_is_reused_without_another_free(self) -> None:
        client = ComfyClient("http://unused")
        calls: list[str] = []
        prompt_number = 0

        def request(path: str, **_kwargs):
            nonlocal prompt_number
            calls.append(path)
            if path == "/queue":
                return {"queue_running": [], "queue_pending": []}
            if path == "/prompt":
                prompt_number += 1
                return {"prompt_id": f"prompt-{prompt_number}"}
            return {}

        with patch.object(client, "request", side_effect=request):
            self.assertEqual(client.submit(self.h3_workflow(), "one"), "prompt-1")
            self.assertEqual(client.submit(self.h3_workflow(), "two"), "prompt-2")
        self.assertEqual(calls, ["/queue", "/free", "/prompt", "/prompt"])

    def test_h3_resource_switch_evicts_old_set_only_when_queue_is_empty(self) -> None:
        client = ComfyClient("http://unused")
        calls: list[str] = []
        busy = False

        def request(path: str, **_kwargs):
            calls.append(path)
            if path == "/queue":
                return {
                    "queue_running": [[1, "active"]] if busy else [],
                    "queue_pending": [],
                }
            if path == "/prompt":
                return {"prompt_id": "prompt"}
            return {}

        with patch.object(client, "request", side_effect=request):
            client.submit(self.h3_workflow("ref.safetensors"), "one")
            busy = True
            with self.assertRaises(ApiError) as raised:
                client.submit(self.h3_workflow("fl.safetensors"), "two")
            self.assertEqual(raised.exception.code, "h3_model_switch_busy")
            busy = False
            client.submit(self.h3_workflow("fl.safetensors"), "three")
        self.assertEqual(calls.count("/prompt"), 2)
        self.assertEqual(calls.count("/free"), 2)

    def test_non_h3_submit_invalidates_h3_resource_ownership(self) -> None:
        client = ComfyClient("http://unused")
        calls: list[str] = []
        prompt_number = 0

        def request(path: str, **_kwargs):
            nonlocal prompt_number
            calls.append(path)
            if path == "/queue":
                return {"queue_running": [], "queue_pending": []}
            if path == "/prompt":
                prompt_number += 1
                return {"prompt_id": f"prompt-{prompt_number}"}
            return {}

        with patch.object(client, "request", side_effect=request):
            client.submit(self.h3_workflow(), "h3-one")
            client.submit({"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "image.safetensors"}}}, "image")
            client.submit(self.h3_workflow(), "h3-two")

        self.assertEqual(calls.count("/prompt"), 3)
        self.assertEqual(calls.count("/free"), 2)


if __name__ == "__main__":
    unittest.main()
