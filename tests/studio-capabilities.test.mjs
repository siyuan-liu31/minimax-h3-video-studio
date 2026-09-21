import assert from "node:assert/strict";
import test from "node:test";

import { imageProfileAcceptsReferenceCount, imageReferencePolicy, profileSupportsParameter, promptImageReferenceNumbers } from "../app/studio-capabilities.ts";

function imageProfile(overrides = {}) {
  return {
    id: "test-image-profile",
    version: "1",
    display_name: "Test image profile",
    output_type: "image",
    compiler: "reviewed_image_compiler",
    manifest_sha256: "a".repeat(64),
    input_modalities: ["text", "image"],
    available: true,
    parameter_schema: { steps: "integer", lora_strength: "number", denoise: "number" },
    defaults: { steps: 8, lora_strength: 0.8, denoise: 0.65 },
    limits: { references: 1, steps: [8, 8], lora_strength: [0, 1.5], denoise: [0.05, 1] },
    reference_contract: { media_types: ["image"], min_count: 1, max_count: 1, ordered: false },
    ...overrides,
  };
}

test("single-image img2img compatibility comes from the profile reference contract", () => {
  const profile = imageProfile();
  assert.deepEqual(imageReferencePolicy(profile), {
    min: 1,
    max: 1,
    ordered: false,
    promptExamples: [],
    indexBase: 0,
    promptIndexBase: 1,
    source: "capability",
  });
  assert.equal(imageProfileAcceptsReferenceCount(profile, 0), false);
  assert.equal(imageProfileAcceptsReferenceCount(profile, 1), true);
  assert.equal(imageProfileAcceptsReferenceCount(profile, 2), false);
});

test("image sampling controls follow parameter_schema without inspecting model paths", () => {
  const profile = imageProfile();
  assert.equal(profileSupportsParameter(profile, "lora_strength"), true);
  assert.equal(profileSupportsParameter(profile, "denoise"), true);
  assert.equal(profileSupportsParameter(profile, "negative_prompt"), false);
  assert.equal(profileSupportsParameter({ ...profile, parameter_schema: { steps: "integer" } }, "lora_strength"), false);
});

test("an original Z-Image latent img2img profile can expose denoise without exposing LoRA", () => {
  const profile = imageProfile({
    id: "z-image-turbo-latent-img2img",
    compiler: "z_image_img2img",
    parameter_schema: { steps: "integer", cfg: "number", denoise: "number", seed: "integer" },
    defaults: { steps: 8, cfg: 1, denoise: 0.65 },
    limits: { references: 1, steps: [8, 8], cfg: [1, 1], denoise: [0.05, 1] },
  });

  assert.equal(imageProfileAcceptsReferenceCount(profile, 1), true);
  assert.equal(profileSupportsParameter(profile, "denoise"), true);
  assert.equal(profileSupportsParameter(profile, "lora_strength"), false);
});

test("Qwen-Image 2.1 accepts zero to ten ordered images and reads native image tags", () => {
  const profile = imageProfile({
    id: "qwen-image-2.1-bf16",
    compiler: "qwen_image_21",
    parameter_schema: { steps: "integer", cfg: "number", seed: "integer" },
    reference_contract: { media_types: ["image"], min_count: 0, max_count: 10, ordered: true },
  });
  assert.equal(imageProfileAcceptsReferenceCount(profile, 0), true);
  assert.equal(imageProfileAcceptsReferenceCount(profile, 10), true);
  assert.equal(imageProfileAcceptsReferenceCount(profile, 11), false);
  assert.equal(profileSupportsParameter(profile, "denoise"), false);
  assert.deepEqual(promptImageReferenceNumbers("Keep <image1> and use 图10; render at 4K 16:9."), [1, 10]);
});
