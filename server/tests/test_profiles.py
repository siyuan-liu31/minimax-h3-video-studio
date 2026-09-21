from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY, H3_MAX_DURATION_SECONDS, ProfileRegistry
from server.tests.test_workflows import lookup
from server.workflows import parse_generation_request


class ProfileRegistryTests(unittest.TestCase):
    @staticmethod
    def manifest(**overrides):
        value = {
            "id": "custom-h3",
            "version": "1.0",
            "display_name": "Custom H3",
            "output_type": "video",
            "input_modalities": ["text", "image"],
            "required_nodes": [],
            "required_models": [],
            "parameter_schema": {},
            "defaults": {},
            "limits": {},
            "compiler": "h3_fl",
        }
        value.update(overrides)
        return value

    def load_manifest(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        Path(temporary.name, "profile.json").write_text(json.dumps(value), encoding="utf-8")
        return ProfileRegistry.load(Path(temporary.name))

    def test_auto_routes_all_builtin_generation_families(self) -> None:
        cases = [
            ({"type": "video", "prompt": "x"}, "minimax-h3-fl2va"),
            ({"type": "video", "prompt": "x", "assets": [{"id": "c" * 32}]}, "minimax-h3-ref2va"),
            ({"type": "image", "prompt": "x"}, "z-image-turbo-bf16-t2i"),
            ({"type": "image", "prompt": "x", "assets": [{"id": "a" * 32}]}, "qwen-image-edit-2511-int8"),
        ]
        for request, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(parse_generation_request(request, lookup).profile_id, expected)

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ApiError, "unknown workflow profile"):
            parse_generation_request({"type": "image", "prompt": "x", "profile_id": "missing"}, lookup)

    def test_external_manifest_may_only_use_reviewed_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.json"
            path.write_text(json.dumps({
                "id": "unsafe", "version": "1", "display_name": "unsafe", "output_type": "image",
                "input_modalities": ["text"], "required_nodes": ["ShellNode"],
                "required_models": [], "parameter_schema": {}, "defaults": {}, "limits": {},
                "compiler": "raw_comfy_graph",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ApiError, "untrusted compiler"):
                ProfileRegistry.load(Path(directory))

    def test_registry_exposes_versioned_declarative_profiles(self) -> None:
        public = DEFAULT_REGISTRY.get("minimax-h3-ref2va").public()
        self.assertEqual(public["version"], "1.3")
        self.assertEqual(public["output_type"], "video")
        self.assertEqual(public["sampling_mode"], "turbo4")
        self.assertRegex(public["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(public["manifest_sha256"], DEFAULT_REGISTRY.get("minimax-h3-ref2va").digest())
        self.assertIn("parameter_schema", public)
        self.assertIn("required_models", public)
        self.assertEqual(public["limits"]["references"], 12)
        self.assertEqual(public["reference_contract"]["max_count"], 12)
        for identifier in (
            "minimax-h3-fl2va", "minimax-h3-fl2va-base",
            "minimax-h3-fl2va-base-resumable",
            "minimax-h3-ref2va", "minimax-h3-ref2va-base",
            "minimax-h3-ref2va-base-resumable",
        ):
            with self.subTest(identifier=identifier):
                video = DEFAULT_REGISTRY.get(identifier)
                self.assertEqual(video.parameter_schema["denoise"], "number")
                self.assertEqual(video.defaults["denoise"], 1.0)
                self.assertEqual(video.limits["denoise"], [0.05, 1])

    def test_ref2va_profiles_accept_reviewed_pre_limit_upgrade_identities(self) -> None:
        identities = {
            "minimax-h3-ref2va": ("1.2", "d961eecd308a42dcf9730c1853dba9b8213284d69d75878d6865f5da1fd1465d"),
            "minimax-h3-ref2va-base": ("1.2", "71bb15b0a310d743ed777dacd5c7d5e1ccc5ca2d943e3eea9deb2e800800b53c"),
            "minimax-h3-ref2va-base-resumable": ("1.0", "451682da20ceb18f6d0f4ed08a0e7699625348b74e99bfd6d08d92620c7ce49b"),
        }
        for identifier, identity in identities.items():
            with self.subTest(identifier=identifier):
                profile = DEFAULT_REGISTRY.get(identifier)
                self.assertTrue(profile.accepts_identity(*identity))
                self.assertFalse(profile.accepts_identity(identity[0], "0" * 64))
                self.assertTrue(profile.accepts_version(identity[0]))
                self.assertTrue(profile.accepts_digest(identity[1]))
                self.assertIn(
                    {"version": identity[0], "manifest_sha256": identity[1]},
                    profile.public()["compatible_identities"],
                )
        spec = parse_generation_request({
            "type": "video", "prompt": "keep identity", "director_mode": "r2v",
            "profile_id": "minimax-h3-ref2va",
            "profile_version": identities["minimax-h3-ref2va"][0],
            "profile_digest": identities["minimax-h3-ref2va"][1],
            "assets": [{"id": "a" * 32, "role": "identity"}],
        }, lookup)
        self.assertEqual((spec.profile_version, spec.profile_digest), (
            "1.3", DEFAULT_REGISTRY.get("minimax-h3-ref2va").digest(),
        ))

    def test_flux2_klein_profile_is_unified_ordered_and_fixed_to_distilled_sampling(self) -> None:
        profile = DEFAULT_REGISTRY.get("flux2-klein-4b-fp8")
        public = profile.public()
        self.assertEqual(profile.compiler, "flux2_klein")
        self.assertEqual(profile.input_modalities, ("text", "image"))
        self.assertEqual(profile.defaults, {"steps": 4, "cfg": 1})
        self.assertEqual(profile.limits["references"], 4)
        self.assertEqual(profile.limits["steps"], [4, 4])
        self.assertEqual(profile.limits["cfg"], [1, 1])
        self.assertEqual(public["reference_contract"]["min_count"], 0)
        self.assertEqual(public["reference_contract"]["max_count"], 4)
        self.assertTrue(public["reference_contract"]["ordered"])
        self.assertEqual(public["reference_contract"]["order_field"], "reference_index")
        self.assertEqual(public["reference_contract"]["index_base"], 0)
        self.assertEqual(profile.model_bindings, {
            "image_diffusion_model": "flux-2-klein-4b-fp8.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "flux2-vae.safetensors",
        })
        self.assertEqual(public["license_id"], "Apache-2.0")
        self.assertTrue(public["license_url"].startswith("https://"))
        quality = DEFAULT_REGISTRY.get("flux2-klein-9b-fp8")
        self.assertEqual(quality.compiler, "flux2_klein")
        self.assertEqual(quality.limits["references"], 4)
        self.assertEqual((quality.defaults["steps"], quality.defaults["cfg"]), (4, 1))
        self.assertEqual(quality.model_bindings, {
            "image_diffusion_model": "flux-2-klein-9b-fp8.safetensors",
            "image_text_encoder": "qwen_3_8b_fp8mixed.safetensors",
            "image_vae": "full_encoder_small_decoder.safetensors",
        })
        self.assertIn("Non-Commercial", quality.license_id)
        self.assertIn("非商业", quality.use_notice)

    def test_qwen_image_21_exposes_full_bf16_unified_image_contract(self) -> None:
        profile = DEFAULT_REGISTRY.get("qwen-image-2.1-bf16")
        public = profile.public()
        self.assertEqual(profile.compiler, "qwen_image_21")
        self.assertEqual(profile.defaults, {"steps": 40, "cfg": 1})
        self.assertEqual(profile.limits["references"], 10)
        self.assertEqual(public["reference_contract"]["min_count"], 0)
        self.assertEqual(public["reference_contract"]["max_count"], 10)
        self.assertEqual(public["reference_contract"]["prompt_reference_format"], "<image{n}>")
        self.assertTrue(public["reference_contract"]["ordered"])
        self.assertEqual(profile.model_bindings, {
            "image_diffusion_model": "qwen_image_2.1_bf16.safetensors",
            "image_text_encoder": "qwen3vl_8b_bf16.safetensors",
            "image_vae": "qwen_image_2.1_vae_bf16.safetensors",
        })
        self.assertIn("非商业", profile.use_notice)

    def test_z_image_bf16_is_default_and_int8_remains_an_explicit_fallback(self) -> None:
        default = DEFAULT_REGISTRY.get("z-image-turbo-bf16-t2i")
        latent = DEFAULT_REGISTRY.get("z-image-turbo-bf16-img2img")
        fallback = DEFAULT_REGISTRY.get("z-image-turbo-int8-t2i")
        self.assertEqual(default.model_bindings, {
            "image_diffusion_model": "z_image_turbo_bf16.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "ae.safetensors",
        })
        self.assertEqual(latent.model_bindings, default.model_bindings)
        self.assertEqual(latent.limits["references"], 1)
        self.assertEqual(fallback.model_bindings["image_diffusion_model"], "z_image_turbo_int8_convrot.safetensors")
        self.assertEqual(
            parse_generation_request({"type": "image", "prompt": "x"}, lookup).profile_id,
            default.id,
        )

    def test_optional_license_metadata_is_safe_and_current_profile_digest_is_stable(self) -> None:
        self.assertEqual(
            DEFAULT_REGISTRY.get("minimax-h3-ref2va").digest(),
            "cda098452f5b138ad604289d6a4786ecef57b7f43ab12a3f3581f4eb224c2fe4",
        )
        with self.assertRaisesRegex(ApiError, "license_url must use https"):
            self.load_manifest(self.manifest(license_url="javascript:alert(1)"))
        custom = self.load_manifest(self.manifest(
            id="licensed-profile", license_id="Example-1.0",
            license_url="https://example.com/license", use_notice="Example notice",
        )).get("licensed-profile")
        self.assertEqual(custom.public()["license_url"], "https://example.com/license")

    def test_z_image_nsfw_lora_profiles_are_versioned_and_mode_specific(self) -> None:
        text = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-t2i")
        image = DEFAULT_REGISTRY.get("z-image-turbo-zit-nsfw-img2img")
        self.assertEqual((text.version, image.version), ("1.1", "1.1"))
        self.assertEqual(text.compiler, "z_image_lora_t2i")
        self.assertEqual(image.compiler, "z_image_lora_img2img")
        self.assertEqual(text.limits["references"], 0)
        self.assertEqual(image.limits["references"], 1)
        self.assertEqual(image.public()["reference_contract"]["min_count"], 1)
        for profile in (text, image):
            with self.subTest(profile=profile.id):
                self.assertEqual(profile.defaults["lora_strength"], 1.0)
                self.assertEqual(profile.limits["lora_strength"], [0, 1.25])
                self.assertEqual(profile.model_bindings["image_lora"], "ZITnsfwLoRA.safetensors")
                self.assertEqual(profile.model_bindings["image_diffusion_model"], "z_image_turbo_bf16.safetensors")
                self.assertEqual(profile.model_bindings["image_text_encoder"], "qwen_3_4b.safetensors")
                self.assertIn("LoraLoaderModelOnly", profile.required_nodes)
                self.assertIn("image_lora", profile.required_models)
                self.assertRegex(profile.digest(), r"^[0-9a-f]{64}$")
                self.assertEqual(profile.license_id, "Civitai Restricted License")
                self.assertEqual(
                    profile.license_url,
                    "https://civitai.com/models/2279079?modelVersionId=2565112",
                )
                self.assertIn("44bf34ce695ebcec6ca17f7dc27511f8fc4204943114d6c7c41cd4559e75dbaf", profile.use_notice)
                self.assertIn("社区文件", profile.use_notice)
                self.assertIn("allowCommercialUse=RentCivit", profile.use_notice)
                self.assertIn("allowDerivatives=false", profile.use_notice)
                self.assertIn("本地按非商业处理", profile.use_notice)
                self.assertIn("禁止二次分发和衍生训练", profile.use_notice)
                self.assertIn("合法、自愿成年人", profile.use_notice)
        self.assertEqual(image.defaults["denoise"], 0.65)
        self.assertEqual(image.limits["denoise"], [0.05, 1])
        self.assertIn("实验性 latent 图生图", image.display_name)
        self.assertIn("非官方 Z-Image Edit/模板", image.display_name)
        self.assertIn("实验性 latent img2img", image.use_notice)
        self.assertIn("非官方 Z-Image Edit", image.use_notice)
        self.assertIn("非官方模板", image.use_notice)
        self.assertIn("denoise 推荐 0.35–0.80", image.use_notice)
        self.assertIn("技术边界 0.05–1", image.use_notice)

    def test_existing_z_image_profile_identity_is_unchanged(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-int8-t2i")
        self.assertEqual(profile.compiler, "z_image_t2i")
        self.assertNotIn("image_lora", profile.required_models)
        self.assertNotIn("LoraLoaderModelOnly", profile.required_nodes)
        self.assertEqual(
            profile.digest(),
            "223c66d801712c822d42d658f1c9338e2296c3d608ef1a0840996598cea22f7d",
        )

    def test_z_image_turbo_latent_img2img_profile_is_experimental_and_lora_free(self) -> None:
        profile = DEFAULT_REGISTRY.get("z-image-turbo-int8-img2img")
        public = profile.public()
        self.assertEqual(profile.version, "1.0")
        self.assertEqual(profile.compiler, "z_image_img2img")
        self.assertEqual(profile.input_modalities, ("text", "image"))
        self.assertEqual(profile.defaults, {"steps": 8, "cfg": 1, "denoise": 0.65})
        self.assertEqual(profile.limits["references"], 1)
        self.assertEqual(profile.limits["steps"], [8, 8])
        self.assertEqual(profile.limits["cfg"], [1, 1])
        self.assertEqual(profile.limits["denoise"], [0.05, 1])
        self.assertNotIn("image_lora", profile.required_models)
        self.assertNotIn("LoraLoaderModelOnly", profile.required_nodes)
        self.assertEqual(public["reference_contract"]["min_count"], 1)
        self.assertEqual(public["reference_contract"]["max_count"], 1)
        self.assertIn("实验性 latent", profile.display_name)
        self.assertIn("非官方 Z-Image Edit/模板", profile.display_name)
        self.assertIn("非官方 Z-Image Edit", profile.use_notice)
        self.assertIn("denoise 默认 0.65", profile.use_notice)
        self.assertIn("推荐 0.35–0.80", profile.use_notice)
        self.assertIn("技术边界 0.05–1", profile.use_notice)

    def test_unreleased_z_image_edit_is_not_an_executable_registry_profile(self) -> None:
        with self.assertRaisesRegex(ApiError, "unknown workflow profile"):
            DEFAULT_REGISTRY.get("z-image-edit-unreleased")

    def test_external_z_image_lora_profile_inherits_reviewed_graph_and_safe_bindings(self) -> None:
        profile = self.load_manifest(self.manifest(
            id="custom-z-lora",
            output_type="image",
            input_modalities=["text"],
            compiler="z_image_lora_t2i",
            required_models=[],
            model_bindings={
                "image_diffusion_model": "models/z-image.safetensors",
                "image_text_encoder": "encoders/qwen.safetensors",
                "image_vae": "vae/ae.safetensors",
                "image_lora": "loras/reviewed.safetensors",
            },
        )).get("custom-z-lora")
        self.assertIn("LoraLoaderModelOnly", profile.required_nodes)
        self.assertIn("EmptySD3LatentImage", profile.required_nodes)
        self.assertEqual(profile.limits["references"], 0)
        with self.assertRaisesRegex(ApiError, "unsafe binding"):
            self.load_manifest(self.manifest(
                id="unsafe-z-lora",
                output_type="image",
                input_modalities=["text"],
                compiler="z_image_lora_t2i",
                model_bindings={"image_lora": "../../outside.safetensors"},
            ))

    def test_external_flux2_profile_exposes_its_narrowed_reference_limit(self) -> None:
        profile = self.load_manifest(self.manifest(
            id="flux2-two-images",
            display_name="FLUX.2 two images",
            output_type="image",
            input_modalities=["text", "image"],
            required_nodes=[],
            required_models=[],
            parameter_schema={},
            defaults={},
            limits={"references": 2},
            compiler="flux2_klein",
        )).get("flux2-two-images")
        self.assertEqual(profile.limits["references"], 2)
        self.assertEqual(profile.public()["reference_contract"]["max_count"], 2)

    def test_builtin_base_profiles_expose_adjustable_steps_without_turbo_lora(self) -> None:
        for identifier, compiler in (
            ("minimax-h3-fl2va-base", "h3_fl"),
            ("minimax-h3-fl2va-base-resumable", "h3_fl"),
            ("minimax-h3-ref2va-base", "h3_ref"),
            ("minimax-h3-ref2va-base-resumable", "h3_ref"),
        ):
            with self.subTest(identifier=identifier):
                profile = DEFAULT_REGISTRY.get(identifier)
                self.assertEqual(profile.compiler, compiler)
                self.assertEqual(profile.sampling_mode, "base")
                self.assertEqual(profile.defaults["steps"], 20)
                self.assertEqual(profile.limits["steps"], [4, 50])
                self.assertNotIn("LoraLoaderModelOnly", profile.required_nodes)
                self.assertFalse(any(role.endswith("_lora") for role in profile.required_models))

    def test_direct_base_is_default_and_resumable_base_requires_h3_checkpoint_nodes(self) -> None:
        profiles = DEFAULT_REGISTRY.all()
        for compiler, direct_id, resumable_id in (
            ("h3_fl", "minimax-h3-fl2va-base", "minimax-h3-fl2va-base-resumable"),
            ("h3_ref", "minimax-h3-ref2va-base", "minimax-h3-ref2va-base-resumable"),
        ):
            with self.subTest(compiler=compiler):
                direct = DEFAULT_REGISTRY.get(direct_id)
                resumable = DEFAULT_REGISTRY.get(resumable_id)
                self.assertFalse(direct.resume)
                self.assertNotIn("H3StudioSaveLatent", direct.required_nodes)
                self.assertTrue(resumable.resume["supported"])
                self.assertIn("H3StudioSaveLatent", resumable.required_nodes)
                self.assertIn("H3StudioLoadLatent", resumable.required_nodes)
                base_candidates = [
                    item for item in profiles
                    if item.output_type == "video" and item.compiler == compiler and item.sampling_mode == "base"
                ]
                self.assertEqual(base_candidates[0].id, direct_id)

    def test_builtin_turbo_profiles_default_to_four_but_allow_adjustable_steps_and_strength(self) -> None:
        for identifier in ("minimax-h3-fl2va", "minimax-h3-ref2va"):
            with self.subTest(identifier=identifier):
                profile = DEFAULT_REGISTRY.get(identifier)
                self.assertEqual(profile.sampling_mode, "turbo4")
                self.assertEqual(profile.defaults["steps"], 4)
                self.assertEqual(profile.limits["steps"], [4, 50])
                self.assertEqual(profile.defaults["lora_strength"], 0.75)
                self.assertEqual(profile.limits["lora_strength"], [0, 2])
                self.assertIn("LoraLoaderModelOnly", profile.required_nodes)

    def test_external_base_profile_can_narrow_adjustable_step_range(self) -> None:
        profile = self.load_manifest(self.manifest(
            sampling_mode="base", defaults={"steps": 24}, limits={"steps": [8, 30]},
        )).get("custom-h3")
        self.assertEqual(profile.sampling_mode, "base")
        self.assertEqual(profile.defaults["steps"], 24)
        self.assertEqual(profile.limits["steps"], [8.0, 30.0])
        self.assertNotIn("fl_lora", profile.required_models)

    def test_external_base_profile_cannot_declare_or_bind_turbo_lora(self) -> None:
        unsafe_variants = (
            {"required_nodes": ["LoraLoaderModelOnly"]},
            {"required_models": ["fl_lora"]},
            {"required_models": ["fl_lora"], "model_bindings": {"fl_lora": "turbo.safetensors"}},
            {"defaults": {"lora_strength": 0.75}},
        )
        for index, overrides in enumerate(unsafe_variants):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ApiError, "base.*LoRA|LoRA.*base"):
                self.load_manifest(self.manifest(
                    id=f"unsafe-base-{index}", sampling_mode="base", **overrides,
                ))

    def test_manifest_cannot_change_parameter_types(self) -> None:
        with self.assertRaisesRegex(ApiError, "trusted type"):
            self.load_manifest(self.manifest(parameter_schema={"steps": "string"}))

    def test_manifest_inherits_compiler_dependency_baseline(self) -> None:
        profile = self.load_manifest(self.manifest()).get("custom-h3")
        self.assertIn("MiniMaxH3ImageToVideo", profile.required_nodes)
        self.assertIn("LoadImage", profile.required_nodes)
        self.assertIn("fl_model", profile.required_models)
        self.assertIn("audio_vae", profile.required_models)
        self.assertIn("image", profile.input_modalities)

    def test_manifest_model_bindings_are_safe_and_bound_to_required_roles(self) -> None:
        profile = self.load_manifest(
            self.manifest(model_bindings={"fl_model": "custom/model.safetensors"})
        ).get("custom-h3")
        self.assertEqual(profile.model_bindings["fl_model"], "custom/model.safetensors")
        for unsafe in ("../model.safetensors", "/root/model.safetensors", "folder\\model.safetensors"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ApiError, "unsafe binding"):
                self.load_manifest(self.manifest(id=f"unsafe-{len(unsafe)}", model_bindings={"fl_model": unsafe}))

    def test_manifest_defaults_must_be_typed_and_inside_narrowed_limits(self) -> None:
        with self.assertRaisesRegex(ApiError, "invalid default"):
            self.load_manifest(self.manifest(defaults={"duration": "five"}))
        with self.assertRaisesRegex(ApiError, "outside its limits"):
            self.load_manifest(self.manifest(defaults={"lora_strength": 1.5}, limits={"lora_strength": [0, 1]}))

    def test_manifest_limits_only_narrow_trusted_bounds(self) -> None:
        profile = self.load_manifest(
            self.manifest(defaults={"duration": 6, "denoise": 0.7}, limits={"duration": [6, 20], "references": 99, "lora_strength": [0.2, 1.2], "denoise": [0, 2]})
        ).get("custom-h3")
        self.assertEqual(profile.limits["duration"], [6.0, H3_MAX_DURATION_SECONDS])
        self.assertEqual(profile.limits["references"], 2)
        self.assertEqual(profile.limits["lora_strength"], [0.2, 1.2])
        self.assertEqual(profile.limits["denoise"], [0.05, 1.0])
        self.assertEqual(profile.defaults["denoise"], 0.7)
        with self.assertRaisesRegex(ApiError, "outside its limits"):
            self.load_manifest(self.manifest(id="bad-denoise", defaults={"denoise": 0.01}))

    def test_external_turbo_profile_can_narrow_adjustable_step_range(self) -> None:
        profile = self.load_manifest(
            self.manifest(defaults={"steps": 8}, limits={"steps": [4, 20]})
        ).get("custom-h3")
        self.assertEqual(profile.defaults["steps"], 8)
        self.assertEqual(profile.limits["steps"], [4.0, 20.0])
        with self.assertRaisesRegex(ApiError, "outside its limits"):
            self.load_manifest(self.manifest(id="bad-turbo", defaults={"steps": 30}, limits={"steps": [4, 20]}))

    def test_manifest_rejects_unknown_sampling_mode(self) -> None:
        with self.assertRaisesRegex(ApiError, "sampling_mode"):
            self.load_manifest(self.manifest(sampling_mode="whatever"))


if __name__ == "__main__":
    unittest.main()
