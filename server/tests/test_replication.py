from __future__ import annotations

import unittest

from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY
from server.replication import SCHEMA_VERSION, build_prompt, plan, validate_recipe


SOURCE_ID = "a" * 32
IMAGE_ID = "b" * 32


def asset(asset_id: str, kind: str, *, duration: float = 60.0, audio: bool = True) -> dict:
    return {
        "id": asset_id,
        "kind": kind,
        "size": 10_000_000,
        "storage_size": 10_000_000,
        "sha256": ("c" if kind == "video" else "d") * 64,
        "media": {
            "duration": duration,
            "video_duration": duration,
            "width": 1080,
            "height": 1920,
            "rotation": 0,
            "has_audio": audio,
        },
    }


def spec(**overrides):
    value = {
        "version": SCHEMA_VERSION,
        "source_asset_id": SOURCE_ID,
        "brief": "Replace the presenter while keeping the original performance",
        "title": "Product demo replication",
        "preserve": ["timing", "motion", "camera", "composition"],
        "replace": {"product": "a matte black H3 camera"},
        "references": [{"asset_id": IMAGE_ID, "role": "new presenter identity"}],
        "audio_policy": "copy-source",
        "continuity": "auto",
    }
    value.update(overrides)
    return value


class ReplicationPlannerTests(unittest.TestCase):
    def planning(self, value=None, *, source=None, motion=True):
        return plan(
            value or spec(),
            source=source or asset(SOURCE_ID, "video"),
            reference_assets=[asset(IMAGE_ID, "image")],
            registry=DEFAULT_REGISTRY,
            available_profiles={"minimax-h3-ref2va", "minimax-h3-ref2va-base"},
            motion_context_available=motion,
        )

    def test_sixty_seconds_compiles_to_legal_h3_segments_and_exact_output(self):
        result = self.planning()
        recipe = result["recipe"]
        windows = recipe["segmentation"]["windows"]
        self.assertEqual(result["summary"]["segment_count"], 4)
        self.assertEqual(windows[0]["start_frame"], 0)
        self.assertEqual(windows[-1]["end_frame"], 1440)
        self.assertEqual(recipe["output"]["frames"], 1440)
        for window in windows:
            self.assertIn(window["generated_frames"], range(124, 363, 17))
            self.assertLessEqual(window["end_frame"] - window["start_frame"], window["generated_frames"])
        self.assertEqual(result["project"]["segments"][0]["continuation"], "none")
        self.assertEqual(result["project"]["segments"][1]["continuation"], "motion_context")
        self.assertEqual(result["project"]["segments"][1]["motion_context"]["video_frames"], 22)
        self.assertEqual(recipe["output"]["aspect_ratio"], "9:16")
        self.assertEqual(validate_recipe(recipe), recipe)

    def test_scene_cut_is_used_when_it_is_a_legal_balanced_boundary(self):
        result = self.planning(
            spec(cut_frames=[328, 690, 1035]),
            source=asset(SOURCE_ID, "video", duration=55.0),
        )
        boundaries = [item["end_frame"] for item in result["recipe"]["segmentation"]["windows"][:-1]]
        self.assertIn(328, boundaries)

    def test_auto_continuity_falls_back_without_motion_context(self):
        result = self.planning(motion=False)
        self.assertEqual(result["recipe"]["continuity"], "none")
        self.assertTrue(all(item["continuation"] == "none" for item in result["project"]["segments"]))

    def test_prompt_uses_stable_reference_aliases(self):
        prompt = build_prompt(
            brief="Create a product demo",
            preserve=["timing", "camera"],
            replace={"product": "H3 Studio"},
            references=[{"asset_id": IMAGE_ID, "role": "product reference"}],
        )
        self.assertIn(f"@{{{IMAGE_ID}}}", prompt)
        self.assertIn("timing, camera", prompt)
        self.assertIn("Replace product", prompt)

    def test_validation_rejects_duration_audio_references_and_explicit_motion(self):
        cases = [
            (spec(), asset(SOURCE_ID, "video", duration=14.9), True, "between 15 and 60"),
            (spec(audio_policy="copy-source"), asset(SOURCE_ID, "video", audio=False), True, "requires source audio"),
            (spec(references=[{"asset_id": SOURCE_ID, "role": "duplicate"}]), None, True, "cannot repeat"),
            (spec(continuity="motion_context"), None, False, "unavailable"),
            (spec(unknown=True), None, True, "unknown field"),
        ]
        for value, source, motion, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ApiError) as raised:
                    self.planning(value, source=source, motion=motion)
                self.assertIn(message, raised.exception.message)


if __name__ == "__main__":
    unittest.main()
