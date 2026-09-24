import { remoteAssetToLibraryItem, type LibraryAsset } from "./studio-library.ts";
export type DouyinTask = {
  id: string; url: string; mode: "parse" | "download"; quality: string;
  status: "queued" | "running" | "cancelling" | "completed" | "failed" | "canceled";
  stage: string; progress: number; metadata?: {title?: string; uploader?: string; duration?: number};
  asset?: LibraryAsset; error?: {code: string; message: string};
};
export type DouyinCapabilities = { available: boolean; reason: string; cookie_configured: boolean };
export class DouyinApiError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "DouyinApiError";
    this.code = code;
  }
}
export function parseDouyinTask(raw: unknown): DouyinTask {
  const item = raw as Record<string, unknown>;
  if (!item || typeof item.id !== "string" || !/^[0-9a-f]{32}$/.test(item.id) ||
      !["queued", "running", "cancelling", "completed", "failed", "canceled"].includes(String(item.status)) ||
      !["parse", "download"].includes(String(item.mode)) || typeof item.url !== "string") throw new Error("下载任务回执无效");
  return { ...item, progress: typeof item.progress === "number" && Number.isFinite(item.progress) ? Math.max(0, Math.min(100, item.progress)) : 0,
    asset: remoteAssetToLibraryItem(item.asset) } as DouyinTask;
}
export async function douyinRequest(path: string, body?: unknown): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`/api/douyin/${path}`, body === undefined ? {cache: "no-store"} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
  } catch {
    throw new DouyinApiError("server_unavailable", "无法连接 Studio 服务器");
  }
  const data = await response.json().catch(() => null);
  if (!data) throw new DouyinApiError("invalid_response", "Studio 返回了无法识别的抖音任务结果");
  if (!response.ok) throw new DouyinApiError(data.error?.code || "api_error", data.error?.message || "抖音请求失败");
  return data;
}
export async function listDouyinTasks(): Promise<DouyinTask[]> {
  const data = await douyinRequest("tasks") as {tasks: unknown[]};
  return data.tasks.map(parseDouyinTask);
}
export const douyinActive = (task: DouyinTask) => ["queued", "running", "cancelling"].includes(task.status);
