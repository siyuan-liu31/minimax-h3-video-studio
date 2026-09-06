#!/usr/bin/env node

import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
export const CONTRACT_PATH = path.join(
  REPO_ROOT,
  "tests",
  "fixtures",
  "director-upstream-lock.json",
);

const EXPECTED_COMMIT = "a267324a9f88141ff4e4b0e8c1a6ed90b4e45db7";
const EXPECTED_MODELS = new Map([
  ["fl2va_unet", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"],
  ["ref2va_unet", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"],
  ["text_encoder", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"],
  ["video_vae", "minimax_h3_video_vae_fp16.safetensors"],
  ["audio_vae", "minimax_h3_audio_vae_fp32.safetensors"],
]);
const EXPECTED_ROUTES = new Set([
  "/minimax/director/upload_chunk",
  "/minimax/director/probe_video",
  "/minimax/director/detect_shots",
  "/minimax/director/enhance_models",
  "/minimax/director/get_template",
  "/minimax/director/enhance",
  "/minimax/director/extract_frames",
  "/minimax/director/image_b64",
  "/minimax/director/unload_model",
  "/minimax/director/unload_ollama",
]);

function requireValue(condition, message, errors) {
  if (!condition) errors.push(message);
}

export async function loadDirectorContract(contractPath = CONTRACT_PATH) {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

export function validateDirectorContract(contract) {
  const errors = [];
  const upstream = contract?.upstream ?? {};
  const integration = contract?.integration ?? {};
  const constraints = contract?.constraints ?? {};

  requireValue(contract?.schema_version === "1.0", "schema_version must remain 1.0", errors);
  requireValue(upstream.repository === "https://github.com/AIMixer/ComfyUI_MiniMaxH3_Director", "upstream repository drift", errors);
  requireValue(upstream.commit === EXPECTED_COMMIT, `upstream commit must remain ${EXPECTED_COMMIT}`, errors);
  requireValue(upstream.locked === true, "upstream commit must be locked", errors);
  requireValue(upstream.license?.spdx === "Apache-2.0", "upstream source license must remain Apache-2.0", errors);
  requireValue(upstream.license?.copied_code === false, "the reviewed adapter must not claim copied upstream code", errors);

  requireValue(integration.mode === "specification-adapter-only", "integration must remain specification-adapter-only", errors);
  requireValue(integration.runtime_enabled === false, "upstream runtime must remain disabled", errors);
  requireValue(integration.direct_plugin_proxy === false, "direct upstream proxy must remain disabled", errors);
  requireValue(integration.auto_update === false, "upstream auto-update must remain disabled", errors);

  requireValue(constraints.fps === 24, "H3 contract fps must remain 24", errors);
  requireValue(constraints.frame_grid === "17k+5", "H3 frame grid must remain 17k+5", errors);
  requireValue(constraints.max_generated_segment_frames === 362, "generated segment limit must remain 362 frames", errors);
  requireValue(constraints.max_total_references === 12, "aggregate reference budget must remain 12", errors);
  requireValue(
    Array.isArray(constraints.reference_budget_includes)
      && constraints.reference_budget_includes.includes("implicit continuity context"),
    "reference budget must include implicit continuity context",
    errors,
  );

  const models = Array.isArray(contract?.reusable_models) ? contract.reusable_models : [];
  requireValue(models.length === 5, "exactly five reusable baseline models must be locked", errors);
  const modelRoles = new Set();
  const modelFiles = new Set();
  for (const model of models) {
    modelRoles.add(model?.role);
    modelFiles.add(model?.filename);
    requireValue(model?.required === true, `baseline model ${model?.role ?? "<unknown>"} must remain required`, errors);
    requireValue(model?.license_status === "separate-review-required", `baseline model ${model?.role ?? "<unknown>"} needs separate license review`, errors);
  }
  for (const [role, filename] of EXPECTED_MODELS) {
    requireValue(modelRoles.has(role), `missing baseline model role ${role}`, errors);
    requireValue(modelFiles.has(filename), `missing locked baseline model ${filename}`, errors);
  }

  const optionalModels = Array.isArray(contract?.optional_models) ? contract.optional_models : [];
  const latent = optionalModels.find((model) => model?.role === "h3_latent_upscaler");
  requireValue(latent?.filename === "minimax_h3_latent_upscaler_3d_bf16.safetensors", "optional H3 latent upscaler filename drift", errors);
  requireValue(latent?.required === false && latent?.default_enabled === false, "optional H3 latent upscaler must remain disabled by default", errors);
  requireValue(latent?.license_status === "separate-review-required", "optional H3 latent upscaler needs separate license review", errors);

  const deps = contract?.dependencies ?? {};
  requireValue(deps.python === ">=3.10", "Python baseline drift", errors);
  requireValue(deps.comfyui_minimum === "0.30.0", "ComfyUI minimum drift", errors);
  const requiredPackages = new Set(deps.required_python_packages ?? []);
  for (const requirement of [
    "opencv-python-headless>=4.8",
    "imageio-ffmpeg>=0.4",
    "scenedetect>=0.6.4,<0.8",
  ]) {
    requireValue(requiredPackages.has(requirement), `missing upstream dependency lock ${requirement}`, errors);
  }
  const nvidiaVfx = (deps.optional_python_packages ?? []).find((item) => item?.requirement === "nvidia-vfx");
  requireValue(nvidiaVfx?.default_enabled === false, "nvidia-vfx must remain optional and disabled", errors);

  const forbiddenPrefixes = new Set(contract?.forbidden_proxy_prefixes ?? []);
  requireValue(forbiddenPrefixes.has("/minimax/director/"), "the upstream Director namespace must remain deny-by-default", errors);
  const routes = Array.isArray(contract?.forbidden_routes) ? contract.forbidden_routes : [];
  const routePaths = new Set(routes.map((route) => route?.path));
  requireValue(routes.length === EXPECTED_ROUTES.size, "forbidden route inventory drift", errors);
  for (const expected of EXPECTED_ROUTES) {
    requireValue(routePaths.has(expected), `missing forbidden route ${expected}`, errors);
  }
  for (const route of routes) {
    requireValue(route?.enabled === false, `forbidden route enabled: ${route?.path ?? "<unknown>"}`, errors);
    requireValue(route?.proxy === false, `forbidden route proxied: ${route?.path ?? "<unknown>"}`, errors);
  }

  requireValue(contract?.install_policy?.state === "reviewed-not-installed", "install state must remain reviewed-not-installed", errors);
  requireValue((contract?.install_policy?.steps?.length ?? 0) >= 8, "install policy is incomplete", errors);
  requireValue((contract?.rollback?.trigger_conditions?.length ?? 0) >= 5, "rollback triggers are incomplete", errors);
  requireValue((contract?.rollback?.steps?.length ?? 0) >= 5, "rollback procedure is incomplete", errors);

  return errors;
}

async function sourceFilesUnder(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.name === "__pycache__" || entry.name === "tests") continue;
    const itemPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...await sourceFilesUnder(itemPath));
    } else if (/\.(?:py|mjs|js|ts|tsx)$/.test(entry.name)) {
      files.push(itemPath);
    }
  }
  return files;
}

export async function scanForbiddenProxyReferences(repoRoot = REPO_ROOT) {
  const findings = [];
  for (const rootName of ["app", "server"]) {
    const root = path.join(repoRoot, rootName);
    for (const file of await sourceFilesUnder(root)) {
      const source = await readFile(file, "utf8");
      if (source.includes("/minimax/director/")) {
        findings.push(`${path.relative(repoRoot, file)} references forbidden /minimax/director/ proxy namespace`);
      }
    }
  }
  const envExample = await readFile(path.join(repoRoot, ".env.example"), "utf8");
  if (/H3_DIRECTOR_(?:ENABLED|PROXY)\s*=\s*(?:1|true|yes)/i.test(envExample)) {
    findings.push(".env.example enables the upstream Director runtime or proxy");
  }
  return findings;
}

export async function runDirectorContractCheck() {
  const contract = await loadDirectorContract();
  const errors = [
    ...validateDirectorContract(contract),
    ...await scanForbiddenProxyReferences(),
  ];
  if (errors.length) {
    for (const error of errors) console.error(`FAIL: ${error}`);
    return 1;
  }
  console.log("PASS: Director upstream lock, limits and deny-by-default proxy contract are valid.");
  return 0;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = await runDirectorContractCheck();
}
