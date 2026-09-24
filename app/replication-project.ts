import type { SerializedVideoProject } from "./video-project";

export const REPLICATION_RECIPE_VERSION = "h3.replication/v1" as const;
export const REPLICATION_PRESERVE_OPTIONS = [
  "timing", "motion", "camera", "composition", "environment", "lighting", "interactions",
] as const;

export type ReplicationPreserve = typeof REPLICATION_PRESERVE_OPTIONS[number];
export type ReplicationAudioPolicy = "copy-source" | "reference-source" | "generate" | "mute";
export type ReplicationContinuity = "auto" | "none" | "motion_context";
export type ReplicationReference = { asset_id: string; role: string };
export type ReplicationPlanRequest = {
  version: typeof REPLICATION_RECIPE_VERSION;
  source_asset_id: string;
  brief: string;
  title: string;
  preserve: ReplicationPreserve[];
  replace: Partial<Record<"subject" | "product" | "setting" | "script" | "language" | "style", string>>;
  references: ReplicationReference[];
  audio_policy: ReplicationAudioPolicy;
  continuity: ReplicationContinuity;
  cut_frames?: number[];
};
export type ReplicationPlan = {
  version: typeof REPLICATION_RECIPE_VERSION;
  prompt: string;
  summary: {
    source_duration: number;
    output_duration: number;
    segment_count: number;
    final_trim_frames: number;
    continuity: "none" | "motion_context";
    audio_policy: ReplicationAudioPolicy;
  };
  recipe: Record<string, unknown>;
  project: SerializedVideoProject;
};

async function jsonRequest(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const value = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    const error = value.error && typeof value.error === "object" ? value.error as { message?: string } : undefined;
    throw new Error(error?.message ?? `复刻工坊请求失败 (${response.status})`);
  }
  return value;
}

export async function analyzeReplicationScenes(assetId: string): Promise<number[]> {
  const value = await jsonRequest("/api/media/analyze-scenes", { asset_id: assetId }) as { fps?: unknown; cut_frames?: unknown };
  const fps = typeof value.fps === "number" && Number.isFinite(value.fps) && value.fps > 0 ? value.fps : 0;
  if (!fps || !Array.isArray(value.cut_frames)) return [];
  return [...new Set(value.cut_frames.flatMap((item) => typeof item === "number" && Number.isFinite(item)
    ? [Math.max(1, Math.round(item / fps * 24))]
    : []))].sort((left, right) => left - right);
}

export async function planReplication(request: ReplicationPlanRequest): Promise<ReplicationPlan> {
  const value = await jsonRequest("/api/video/replication/plan", request);
  if (!value || typeof value !== "object") throw new Error("服务端未返回有效复刻方案");
  const result = value as Partial<ReplicationPlan>;
  if (
    result.version !== REPLICATION_RECIPE_VERSION
    || !result.project || typeof result.project !== "object"
    || !result.summary || typeof result.summary !== "object"
    || typeof result.prompt !== "string"
  ) throw new Error("服务端返回的复刻方案版本或结构无效");
  return result as ReplicationPlan;
}
