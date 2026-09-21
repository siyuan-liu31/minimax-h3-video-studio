import { remoteAssetToLibraryItem, type LibraryAsset } from "./studio-library.ts";

export type VoiceEngine = "vevo2" | "yingmusic";
export type YingMusicParameters = { diffusion_steps: number; inference_cfg_rate: number; seed: number };
export type VoiceCapability = { id: VoiceEngine; available: boolean; mode: string; reason?: string; tuning?: Record<keyof YingMusicParameters, { default: number; minimum: number; maximum: number }> };
export type VoiceTask = {
  id: string;
  engine: VoiceEngine;
  sourceAssetId: string;
  referenceAssetId: string;
  status: "queued" | "running" | "cancelling" | "completed" | "failed" | "canceled";
  stage: string;
  progress: number;
  createdAt: number;
  queuePosition?: number;
  queueReason?: string;
  output?: { filename: string; size: number };
  error?: string;
  parameters?: YingMusicParameters;
};

const ID = /^[0-9a-f]{32}$/;
const AUDIO_EXTENSION = /\.(wav|flac|ogg|mp3)$/i;
const ENGINES = new Set<VoiceEngine>(["vevo2", "yingmusic"]);
const STATUSES = new Set<VoiceTask["status"]>(["queued", "running", "cancelling", "completed", "failed", "canceled"]);

export const YINGMUSIC_DEFAULTS: YingMusicParameters = { diffusion_steps: 100, inference_cfg_rate: 0.7, seed: -1 };

export function validateYingMusicParameters(value: YingMusicParameters): YingMusicParameters {
  if (!Number.isInteger(value.diffusion_steps) || value.diffusion_steps < 10 || value.diffusion_steps > 200) throw new Error("采样步数须为 10–200 的整数");
  if (!Number.isFinite(value.inference_cfg_rate) || value.inference_cfg_rate < 0 || value.inference_cfg_rate > 2) throw new Error("引导强度须为 0–2 的数字");
  if (!Number.isInteger(value.seed) || value.seed < -1 || value.seed > 4294967295) throw new Error("随机种子须为 -1 或 0–4294967295 的整数");
  return value;
}

function parseParameters(raw: unknown): YingMusicParameters | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  try { return validateYingMusicParameters(value as YingMusicParameters); } catch { return undefined; }
}

export function isSupportedVoiceAudio(file: Pick<File, "name" | "type">): boolean {
  // The server checks the file signature; this only gives fast UI feedback.
  return AUDIO_EXTENSION.test(file.name);
}

export function voiceDownloadUrl(id: string): string {
  if (!ID.test(id)) throw new Error("无效的换声任务 ID");
  return `/api/voice/tasks/${id}/download`;
}

export function parseVoiceTask(raw: unknown): VoiceTask | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  const id = typeof value.id === "string" ? value.id : value.task_id;
  if (typeof id !== "string" || !ID.test(id) || !ENGINES.has(value.engine as VoiceEngine) || !STATUSES.has(value.status as VoiceTask["status"])) return undefined;
  if (typeof value.source_asset_id !== "string" || !ID.test(value.source_asset_id) || typeof value.reference_asset_id !== "string" || !ID.test(value.reference_asset_id)) return undefined;
  const output = value.output && typeof value.output === "object" ? value.output as Record<string, unknown> : undefined;
  const error = value.error && typeof value.error === "object" ? value.error as Record<string, unknown> : undefined;
  const parameters = parseParameters(value.parameters);
  return {
    id,
    engine: value.engine as VoiceEngine,
    sourceAssetId: value.source_asset_id,
    referenceAssetId: value.reference_asset_id,
    status: value.status as VoiceTask["status"],
    stage: typeof value.stage === "string" ? value.stage : "",
    progress: typeof value.progress === "number" && Number.isFinite(value.progress) ? Math.max(0, Math.min(100, value.progress)) : 0,
    createdAt: typeof value.created_at === "number" && Number.isFinite(value.created_at) ? value.created_at : 0,
    ...(typeof value.queue_position === "number" ? { queuePosition: value.queue_position } : {}),
    ...(typeof value.queue_reason === "string" ? { queueReason: value.queue_reason } : {}),
    ...(output && typeof output.filename === "string" && typeof output.size === "number" ? { output: { filename: output.filename, size: output.size } } : {}),
    ...(error && typeof error.message === "string" ? { error: error.message } : {}),
    ...(parameters ? { parameters } : {}),
  };
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(path, { cache: "no-store", ...init });
  const body = await response.json().catch(() => ({})) as { error?: { message?: string } };
  if (!response.ok) throw new Error(body.error?.message ?? `换声请求失败 (${response.status})`);
  return body;
}

export async function getVoiceCapabilities(signal?: AbortSignal): Promise<VoiceCapability[]> {
  const body = await request("/api/voice/capabilities", { signal }) as { engines?: unknown[] };
  return (Array.isArray(body.engines) ? body.engines : []).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const value = entry as Record<string, unknown>;
    if (!ENGINES.has(value.id as VoiceEngine)) return [];
    const rawTuning = value.tuning && typeof value.tuning === "object" ? value.tuning as Record<string, unknown> : undefined;
    const tuning = rawTuning && ["diffusion_steps", "inference_cfg_rate", "seed"].every((key) => {
      const rule = rawTuning[key];
      return rule && typeof rule === "object" && ["default", "minimum", "maximum"].every((part) => typeof (rule as Record<string, unknown>)[part] === "number");
    }) ? rawTuning as VoiceCapability["tuning"] : undefined;
    return [{ id: value.id as VoiceEngine, available: value.available === true, mode: typeof value.mode === "string" ? value.mode : "", ...(typeof value.reason === "string" ? { reason: value.reason } : {}), ...(tuning ? { tuning } : {}) }];
  });
}

export async function listVoiceTasks(signal?: AbortSignal): Promise<VoiceTask[]> {
  const body = await request("/api/voice/tasks", { signal }) as { items?: unknown[] };
  return (Array.isArray(body.items) ? body.items : []).flatMap((item) => {
    const task = parseVoiceTask(item);
    return task ? [task] : [];
  });
}

export async function uploadVoiceAudio(file: File, signal?: AbortSignal): Promise<LibraryAsset> {
  if (!isSupportedVoiceAudio(file)) throw new Error("只接受 WAV、FLAC、OGG 或 MP3 音频文件");
  const form = new FormData();
  form.append("file", file);
  const body = await request("/api/assets", { method: "POST", body: form, signal }) as { asset?: unknown };
  const asset = remoteAssetToLibraryItem(body.asset ?? body);
  if (!asset || asset.kind !== "audio") throw new Error("服务端未返回有效的音频资产");
  return asset;
}

export async function submitVoiceTask(engine: VoiceEngine, sourceAssetId: string, referenceAssetId: string, parameters?: YingMusicParameters): Promise<VoiceTask> {
  if (!ENGINES.has(engine) || !ID.test(sourceAssetId) || !ID.test(referenceAssetId)) throw new Error("请选择引擎、原音频与参考音频");
  if (parameters && engine !== "yingmusic") throw new Error("仅歌曲换声支持这些参数");
  if (parameters) validateYingMusicParameters(parameters);
  const body = await request("/api/voice/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ engine, source_asset_id: sourceAssetId, reference_asset_id: referenceAssetId, request_id: crypto.randomUUID().replaceAll("-", ""), ...(parameters ?? {}) }),
  });
  const task = parseVoiceTask(body);
  if (!task) throw new Error("服务端未返回有效的换声任务");
  return task;
}

export async function cancelVoiceTask(id: string): Promise<VoiceTask> {
  if (!ID.test(id)) throw new Error("无效的换声任务 ID");
  const task = parseVoiceTask(await request(`/api/voice/tasks/${id}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }));
  if (!task) throw new Error("服务端未返回有效的换声任务");
  return task;
}

export async function deleteVoiceTask(id: string): Promise<void> {
  if (!ID.test(id)) throw new Error("无效的换声任务 ID");
  await request(`/api/voice/tasks/${id}`, { method: "DELETE" });
}
