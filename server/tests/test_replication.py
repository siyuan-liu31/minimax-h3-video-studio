from __future__ import annotations

import unittest
from copy import deepcopy

from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY
from server.replication import SCHEMA_VERSION, build_prompt, plan, validate_recipe, validate_execution, _segment_windows


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
        self.assertEqual(result["summary"]["segment_count"], 5)
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

    def test_short_and_long_sources_cover_every_frame_with_legal_segments(self):
        for duration in (1 / 24, 0.1, 1.0, 5.0, 14.9, 60.1, 600.0, 3600.0, 18000.0):
            with self.subTest(duration=duration):
                result = self.planning(source=asset(SOURCE_ID, "video", duration=duration))
                recipe = result["recipe"]
                self.assertEqual(validate_recipe(recipe), recipe)
                self.assertEqual(recipe["output"]["frames"], round(duration * 24))
                self.assertEqual(recipe["segmentation"]["windows"][-1]["end_frame"], round(duration * 24))
                self.assertLess(recipe["segmentation"]["final_trim_frames"], 124)
                if duration < 5:
                    self.assertEqual(result["summary"]["segment_count"], 1)
                    self.assertEqual(recipe["segmentation"]["windows"][0]["generated_frames"], 124)
                if duration == 18000:
                    self.assertGreater(result["summary"]["segment_count"], 1000)

    def test_partition_boundaries_have_no_gaps_or_illegal_padding(self):
        for source_frames in range(1, 2500):
            windows = _segment_windows(source_frames, [])
            cursor = 0
            for index, (start, end, generated) in enumerate(windows):
                self.assertEqual(start, cursor)
                self.assertIn(generated, range(124, 363, 17))
                self.assertLessEqual(end - start, generated)
                if index < len(windows) - 1:
                    self.assertEqual(end - start, generated)
                cursor = end
            self.assertEqual(cursor, source_frames)

    def test_motion_context_composition_owns_every_source_frame_after_trim(self):
        for frames in list(range(1, 1500)) + [1560, 14400, 86400]:
            windows = _segment_windows(frames, [], 22)
            cursor = 0
            composed = 0
            for index, (start, end, generated) in enumerate(windows):
                head = 22 if index else 0
                self.assertEqual(start, cursor)
                self.assertIn(generated, range(124, 363, 17))
                self.assertLessEqual(end - start, generated - head)
                if index < len(windows) - 1:
                    self.assertEqual(end - start, generated - head)
                self.assertGreaterEqual(start - head, 0)
                cursor = end
                composed += generated - head
            self.assertEqual(cursor, frames)
            self.assertGreaterEqual(composed, frames)
            self.assertLess(composed - frames, 124)

    def test_motion_plan_references_preceding_source_frames_and_rejects_stale_execution(self):
        result = self.planning(source=asset(SOURCE_ID, "video", duration=16))
        project = result["project"]
        validate_execution(project)
        windows = result["recipe"]["segmentation"]["windows"]
        self.assertEqual(project["segments"][1]["source_range"]["start_frame"], windows[1]["start_frame"] - 22)
        composed = sum(round(s["request"]["parameters"]["duration"] * 24) - (22 if i else 0) for i, s in enumerate(project["segments"]))
        self.assertEqual(composed - result["recipe"]["segmentation"]["final_trim_frames"], 384)
        for change in ("duration", "source", "continuation"):
            changed = deepcopy(project)
            segment = changed["segments"][1]
            if change == "duration": segment["request"]["parameters"]["duration"] += 17 / 24
            if change == "source": segment["source_range"]["start_frame"] += 22
            if change == "continuation": segment["continuation"] = "none"
            with self.subTest(change=change), self.assertRaises(ApiError) as raised:
                validate_execution(changed)
            self.assertEqual(raised.exception.code, "replication_replan_required")
        legacy = self.planning(spec(continuity="none"), source=asset(SOURCE_ID, "video", duration=16))["project"]
        legacy["recipe"]["continuity"] = "motion_context"
        legacy["recipe"]["segmentation"].pop("motion_context_frames")
        self.assertEqual(validate_recipe(legacy["recipe"]), legacy["recipe"])
        with self.assertRaises(ApiError) as raised:
            validate_execution(legacy)
        self.assertEqual(raised.exception.code, "replication_replan_required")

    def test_resource_budget_rejects_unrepresentable_plans_before_allocating(self):
        for duration in (3600, 1e100):
            with self.subTest(duration=duration), self.assertRaises(ApiError) as raised:
                plan(spec(), source=asset(SOURCE_ID, "video", duration=duration),
                     reference_assets=[asset(IMAGE_ID, "image")], registry=DEFAULT_REGISTRY,
                     max_project_bytes=1024)
            self.assertEqual(raised.exception.code, "replication_project_size")

    def test_invalid_source_durations_are_rejected(self):
        for duration in (-1, 0, 0.001, float("nan"), float("inf"), 1e308):
            with self.subTest(duration=duration), self.assertRaises(ApiError):
                self.planning(source=asset(SOURCE_ID, "video", duration=duration))

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
            (spec(), asset(SOURCE_ID, "video", duration=0), True, "positive"),
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
