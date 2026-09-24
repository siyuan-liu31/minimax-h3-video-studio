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
  profile_id?: string;
  profile_version?: string;
  profile_digest?: string;
  steps?: number;
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

export function isReplicationProject(project: { recipe?: Record<string, unknown> }): boolean {
  return project.recipe?.type === "replication" && project.recipe?.version === REPLICATION_RECIPE_VERSION;
}

export function replicationIsActive(project: { status: string } | undefined): boolean {
  return Boolean(project && ["running", "stopping", "merging", "submitting", "queued"].includes(project.status));
}

export function replicationEditImpact(segments: Array<{ id: string; continuation: string }>, segmentId: string): string[] {
  const index = segments.findIndex((segment) => segment.id === segmentId);
  if (index < 0) return [];
  const affected = [segmentId];
  for (let cursor = index + 1; cursor < segments.length && segments[cursor].continuation !== "none"; cursor++) {
    affected.push(segments[cursor].id);
  }
  return affected;
}

export function replicationStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    draft: "草稿", pending: "待生成", queued: "排队中", submitting: "提交中", running: "生成中",
    stopping: "正在停止", stopped: "已停止", canceled: "已取消", completed: "已完成",
    partial: "部分完成", stale: "需要重跑", failed: "失败", merging: "正在合并", merged: "成片完成",
  };
  return labels[status] ?? status;
}
