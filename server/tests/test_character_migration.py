from __future__ import annotations

import unittest

from server.character_migration import (
    LEGAL_SEGMENT_FRAMES, SCHEMA_VERSION, build_prompt, plan, validate_recipe,
)
from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY


SOURCE_ID = "a" * 32
CHARACTER_ID = "b" * 32


def asset(asset_id: str, kind: str, *, duration: float = 60.0, width: int = 1920, height: int = 1080, rotation: int = 0, audio: bool = True) -> dict:
    return {
        "id": asset_id,
        "kind": kind,
        "size": 20_000_000,
        "storage_size": 20_000_000,
        "sha256": ("c" if kind == "video" else "d") * 64,
        "media": {"duration": duration, "video_duration": duration, "width": width, "height": height, "rotation": rotation, "has_audio": audio},
    }


def spec(**overrides):
    value = {
        "version": SCHEMA_VERSION,
        "source_asset_id": SOURCE_ID,
        "targets": [{"character_asset_id": CHARACTER_ID, "source_subject": "the center performer"}],
        "profile_id": "minimax-h3-ref2va",
        "steps": 4,
        "segment_frames": 243,
        "overlap_frames": 39,
        "audio_policy": "copy-source",
    }
    value.update(overrides)
    return value


class CharacterMigrationPlannerTests(unittest.TestCase):
    def planning(self, value=None, *, source=None):
        return plan(
            value or spec(),
            source=source or asset(SOURCE_ID, "video"),
            character=asset(CHARACTER_ID, "image"),
            registry=DEFAULT_REGISTRY,
            available_profiles={"minimax-h3-ref2va", "minimax-h3-ref2va-base"},
            motion_context_available=True,
            free_disk_bytes=10**12,
            merged_quota_bytes=10**12,
            motion_context_quota_bytes=10**12,
        )

    def test_short_source_is_one_legal_segment_and_exact_final_trim(self):
        result = self.planning(source=asset(SOURCE_ID, "video", duration=3.0))
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["source_frames"], 72)
        self.assertEqual(result["windows"][0]["generated_frames"], 243)
        self.assertEqual(result["final_trim"]["remove_tail_frames"], 171)
        self.assertEqual(result["project"]["segments"][0]["source_range"]["end_frame"], 72)

    def test_sixty_seconds_has_deterministic_overlapping_ranges(self):
        result = self.planning()
        self.assertEqual(len(result["windows"]), 7)
        self.assertEqual(
            [(item["source_start_frame"], item["source_end_frame"]) for item in result["windows"]],
            [(0, 243), (204, 447), (408, 651), (612, 855), (816, 1059), (1020, 1263), (1207, 1440)],
        )
        self.assertEqual(result["windows"][-1]["input_padding_frames"], 10)
        self.assertEqual(result["final_trim"]["remove_tail_frames"], 10)
        self.assertEqual(result["recipe"]["segmentation"]["terminal_overlap_frames"], 56)
        self.assertEqual(result["project"]["segments"][1]["motion_context"], {"video_frames": 39, "audio_frames": 39})
        self.assertEqual(result["project"]["segments"][-1]["motion_context"], {"video_frames": 56, "audio_frames": 56})

    def test_terminal_window_backfills_source_and_avoids_padding_when_grid_allows(self):
        source = asset(SOURCE_ID, "video", duration=311 / 24, width=768, height=1344)
        source["media"].update({"frame_count": 311, "reference_fps": 24.0})
        result = self.planning(
            spec(segment_frames=124, overlap_frames=5), source=source,
        )
        self.assertEqual(
            [(item["source_start_frame"], item["source_end_frame"]) for item in result["windows"]],
            [(0, 124), (119, 243), (187, 311)],
        )
        self.assertEqual(result["windows"][-1]["trim_head_frames"], 56)
        self.assertEqual(result["windows"][-1]["owned_output_frames"], 68)
        self.assertEqual(result["windows"][-1]["input_padding_frames"], 0)
        self.assertEqual(result["final_trim"]["remove_tail_frames"], 0)
        self.assertEqual(result["recipe"]["segmentation"]["composed_frames"], 311)
        self.assertEqual(result["project"]["storyboard"]["cut_frames"], [119, 187])

    def test_backfilled_windows_preserve_order_and_exact_output_invariants(self):
        for segment_frames in (124, 243, 362):
            for overlap_frames in (5, 22, 39, 56):
                if overlap_frames >= segment_frames:
                    continue
                for source_frames in (
                    1, segment_frames - 1, segment_frames,
                    segment_frames + 1, segment_frames * 2 + 73,
                ):
                    source = asset(SOURCE_ID, "video", duration=source_frames / 24)
                    source["media"].update({"frame_count": source_frames, "reference_fps": 24.0})
                    result = self.planning(
                        spec(segment_frames=segment_frames, overlap_frames=overlap_frames),
                        source=source,
                    )
                    windows = result["windows"]
                    self.assertEqual(windows[0]["source_start_frame"], 0)
                    self.assertEqual(windows[-1]["source_end_frame"], source_frames)
                    output_cursor = 0
                    for index, window in enumerate(windows):
                        trim_head = window["trim_head_frames"]
                        expected_start = 0 if index == 0 else output_cursor - trim_head
                        self.assertEqual(window["source_start_frame"], expected_start)
                        self.assertGreater(window["source_end_frame"], window["source_start_frame"])
                        self.assertEqual(
                            window["input_padding_frames"],
                            segment_frames - window["source_frames"],
                        )
                        output_cursor += window["owned_output_frames"]
                    self.assertEqual(
                        output_cursor,
                        result["recipe"]["segmentation"]["composed_frames"],
                    )
                    self.assertEqual(
                        output_cursor - result["final_trim"]["remove_tail_frames"],
                        source_frames,
                    )

    def test_portrait_rotation_uses_display_orientation(self):
        result = self.planning(source=asset(SOURCE_ID, "video", width=1920, height=1080, rotation=90))
        output = result["recipe"]["output"]
        self.assertEqual((output["aspect_ratio"], output["width"], output["height"]), ("9:16", 768, 1344))

    def test_base_and_turbo_step_contracts(self):
        turbo = self.planning(spec(steps=4))
        self.assertEqual(turbo["project"]["segments"][0]["request"]["parameters"]["steps"], 4)
        base = self.planning(spec(profile_id="minimax-h3-ref2va-base", steps=20, lora_strength=0))
        request = base["project"]["segments"][0]["request"]
        self.assertEqual(request["parameters"]["steps"], 20)
        self.assertNotIn("lora_strength", request["parameters"])

    def test_reviewed_legacy_profile_identity_remains_plannable(self):
        result = self.planning(spec(
            profile_version="1.2",
            profile_digest="d961eecd308a42dcf9730c1853dba9b8213284d69d75878d6865f5da1fd1465d",
        ))
        request = result["project"]["segments"][0]["request"]
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va")
        self.assertEqual(request["profile_version"], profile.version)
        self.assertEqual(request["profile_digest"], profile.digest())

    def test_prompt_binds_subjects_and_preservation_policy(self):
        prompt = build_prompt(source_subject="the center performer", character_asset_id=CHARACTER_ID)
        self.assertIn("<Subject 1>", prompt)
        self.assertIn("<Subject 2>", prompt)
        self.assertIn(f"@{{{CHARACTER_ID}}}", prompt)
        self.assertIn("identity_not_preserved", prompt)
        self.assertIn("identity_fully_preserved", prompt)
        self.assertIn("camera movement", prompt)
        self.assertIn("temporal flicker", prompt)

    def test_validation_rejects_fields_media_audio_grid_overlap_profile_and_storage(self):
        cases = [
            (spec(typo=True), None, "unknown field"),
            (spec(targets=[{"character_asset_id": CHARACTER_ID, "source_subject": "person"}]), None, "identify one person"),
            (spec(segment_frames=242), None, "17k+5"),
            (spec(segment_frames=124, overlap_frames=124), None, "allowed values"),
            (spec(overlap_frames=6), None, "allowed values"),
            (spec(profile_id="minimax-h3-fl2va"), None, "Ref2VA"),
            (spec(audio_policy="copy-source"), asset(SOURCE_ID, "video", audio=False), "requires a usable"),
        ]
        for value, source, message in cases:
            with self.subTest(value=value):
                with self.assertRaises(ApiError) as raised:
                    self.planning(value, source=source)
                self.assertIn(message, raised.exception.message)
        self.assertIn(243, LEGAL_SEGMENT_FRAMES)
        with self.assertRaises(ApiError) as disk:
            plan(
                spec(), source=asset(SOURCE_ID, "video"), character=asset(CHARACTER_ID, "image"),
                registry=DEFAULT_REGISTRY, free_disk_bytes=1,
            )
        self.assertEqual(disk.exception.code, "disk_full")

    def test_expert_prompt_is_preserved_exactly(self):
        prompt = "  Exact expert prompt with @{asset} markers and spacing\n"
        result = self.planning(spec(prompt=prompt, audio_policy="generate"))
        self.assertEqual(result["prompt"], prompt)

    def test_persisted_recipe_nested_contract_is_strict_and_self_consistent(self):
        recipe = self.planning()["recipe"]
        self.assertEqual(validate_recipe(recipe), recipe)
        legacy = __import__("copy").deepcopy(recipe)
        legacy["segmentation"].pop("terminal_overlap_frames")
        legacy["segmentation"]["composed_frames"] = 1467
        legacy["segmentation"]["final_trim_frames"] = 27
        self.assertEqual(validate_recipe(legacy), legacy)
        cases = []
        for section, key, value in (
            ("targets", "typo", True),
            ("prompt_policy", "typo", True),
            ("segmentation", "stride_frames", 1),
            ("output", "frames", 1),
        ):
            changed = __import__("copy").deepcopy(recipe)
            if section == "targets":
                changed[section][0][key] = value
            else:
                changed[section][key] = value
            cases.append(changed)
        invalid_terminal_overlap = __import__("copy").deepcopy(recipe)
        invalid_terminal_overlap["segmentation"]["terminal_overlap_frames"] = 5
        cases.append(invalid_terminal_overlap)
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ApiError):
                    validate_recipe(changed)


if __name__ == "__main__":
    unittest.main()
