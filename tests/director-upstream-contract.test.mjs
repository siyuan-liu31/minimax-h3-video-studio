import assert from "node:assert/strict";
import test from "node:test";

import {
  loadDirectorContract,
  scanForbiddenProxyReferences,
  validateDirectorContract,
} from "../scripts/check-director-upstream-contract.mjs";

const contract = await loadDirectorContract();

function mutated(mutator) {
  const value = structuredClone(contract);
  mutator(value);
  return value;
}

test("Director compatibility lock pins the reviewed Apache-2.0 source and five separately licensed baseline models", () => {
  assert.deepEqual(validateDirectorContract(contract), []);
  assert.equal(contract.upstream.commit, "a267324a9f88141ff4e4b0e8c1a6ed90b4e45db7");
  assert.equal(contract.upstream.license.spdx, "Apache-2.0");
  assert.equal(contract.upstream.license.copied_code, false);
  assert.equal(contract.reusable_models.length, 5);
  assert.ok(contract.reusable_models.every((model) => model.license_status === "separate-review-required"));
  assert.equal(contract.optional_models[0].default_enabled, false);
});

test("Director adapter rejects aggregate references above twelve and generated segments above 362 frames", () => {
  const tooManyRefs = mutated((value) => {
    value.constraints.max_total_references = 13;
  });
  const tooManyFrames = mutated((value) => {
    value.constraints.max_generated_segment_frames = 363;
  });

  assert.ok(validateDirectorContract(tooManyRefs).some((error) => error.includes("reference budget")));
  assert.ok(validateDirectorContract(tooManyFrames).some((error) => error.includes("362 frames")));
});

test("Director adapter rejects upstream commit drift and runtime enablement", () => {
  const wrongCommit = mutated((value) => {
    value.upstream.commit = "0000000000000000000000000000000000000000";
  });
  const enabledRuntime = mutated((value) => {
    value.integration.runtime_enabled = true;
  });

  assert.ok(validateDirectorContract(wrongCommit).some((error) => error.includes("upstream commit")));
  assert.ok(validateDirectorContract(enabledRuntime).some((error) => error.includes("runtime must remain disabled")));
});

test("every reviewed upstream HTTP route remains disabled and unproxied", async () => {
  assert.equal(contract.forbidden_proxy_prefixes.includes("/minimax/director/"), true);
  assert.equal(contract.forbidden_routes.length, 10);
  assert.ok(contract.forbidden_routes.every((route) => route.enabled === false && route.proxy === false));
  assert.deepEqual(await scanForbiddenProxyReferences(), []);

  const enabledRoute = mutated((value) => {
    value.forbidden_routes[0].enabled = true;
    value.forbidden_routes[0].proxy = true;
  });
  const errors = validateDirectorContract(enabledRoute);
  assert.ok(errors.some((error) => error.includes("forbidden route enabled")));
  assert.ok(errors.some((error) => error.includes("forbidden route proxied")));
});

test("install and rollback contracts keep optional GPU dependencies off and shared data out of immutable releases", () => {
  assert.equal(contract.dependencies.comfyui_minimum, "0.30.0");
  assert.equal(contract.dependencies.optional_python_packages[0].requirement, "nvidia-vfx");
  assert.equal(contract.dependencies.optional_python_packages[0].default_enabled, false);
  assert.match(contract.dependencies.production_policy, /isolated release environment/i);
  assert.match(contract.rollback.data_root_policy, /outside immutable releases/i);
  assert.ok(contract.rollback.steps.some((step) => step.includes("previous current-release symlink")));
  assert.ok(contract.rollback.steps.some((step) => step.includes("do not delete or migrate shared user data")));
});
