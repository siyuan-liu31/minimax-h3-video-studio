import { remoteAssetToLibraryItem, type LibraryAsset } from "./studio-library.ts";

const BRIDGE = "http://127.0.0.1:8765";
const TASK_ID = /^[0-9a-f]{32}$/;
const DOWNLOAD_URL = /^\/api\/download\/[0-9a-f]{48}$/;

type LocalTask = {
  id: string;
  status: "pending" | "running" | "completed" | "failed";
  download_url?: string;
  download?: { size?: number };
  metadata?: { id?: string; ext?: string };
  error?: { code?: string; message?: string };
};

export class DouyinBridgeError extends Error {
  readonly code: string;
  readonly stage: string;
  constructor(code: string, message: string, stage: string) {
    super(message);
    this.name = "DouyinBridgeError";
    this.code = code;
    this.stage = stage;
  }
}

async function bridgeRequest(path: string, token: string, init: RequestInit = {}, stage = "download"): Promise<Response> {
  try {
    const headers = new Headers(init.headers);
    headers.set("X-H3-Douyin-Token", token);
    const response = await fetch(`${BRIDGE}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as { error?: { code?: string; message?: string } };
      const code = response.status === 429 && body.error?.code === "rate_limited" ? "bridge_rate_limited" : body.error?.code || "bridge_error";
      throw new DouyinBridgeError(code, body.error?.message || `本机辅助服务返回 ${response.status}`, stage);
    }
    return response;
  } catch (error) {
    if (error instanceof TypeError) throw new DouyinBridgeError("bridge_unavailable", "无法连接本机辅助服务。", stage);
    throw error;
  }
}

export async function connectDouyinBridge(): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${BRIDGE}/health`, { cache: "no-store" });
  } catch {
    throw new Error("本机抖音导入服务未运行。请启动本机 Studio 服务后重试。");
  }
  if (!response.ok) throw new Error(`本机抖音导入服务返回 ${response.status}`);
  const body = await response.json() as { status?: string; api_version?: string; bridge_token?: string };
  if (body.status !== "ok" || body.api_version !== "h3ctl.douyin/v1") throw new Error("本机辅助服务版本不兼容，请重新构建 h3ctl。");
  if (!/^[0-9a-f]{48}$/.test(body.bridge_token || "")) throw new Error("本机抖音导入服务未启用 Studio 连接。");
  return body.bridge_token!;
}

export async function inspectDouyinViaBridge(text: string, token: string): Promise<{ title?: string; uploader?: string; duration?: number }> {
  const response = await bridgeRequest("/api/inspect", token, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
  }, "parse");
  const body = await response.json() as { metadata?: { id?: string; title?: string; uploader?: string; duration?: number } };
  if (!body.metadata?.id) throw new Error("本机解析结果缺少视频 ID。");
  return body.metadata;
}

function parseTask(value: unknown): LocalTask {
  const task = value as LocalTask;
  if (!task || !TASK_ID.test(task.id) || !["pending", "running", "completed", "failed"].includes(task.status)) {
    throw new Error("本机辅助服务返回了无效任务。");
  }
  return task;
}

async function readTask(response: Response): Promise<LocalTask> {
  const body = await response.json() as { task?: unknown };
  return parseTask(body.task);
}

export async function importDouyinViaBridge(text: string, token: string, onStatus: (status: string) => void): Promise<LibraryAsset> {
  onStatus("正在本机解析抖音链接…");
  const started = await bridgeRequest("/api/parse", token, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
  }, "parse");
  let task = await readTask(started);
  const deadline = Date.now() + 10 * 60_000;
  while (task.status === "pending" || task.status === "running") {
    if (Date.now() >= deadline) throw new DouyinBridgeError("timeout", "本机下载等待超时。", "download");
    onStatus(task.status === "pending" ? "本机下载排队中…" : "本机正在下载视频…");
    await new Promise((resolve) => setTimeout(resolve, 1500));
    task = await readTask(await bridgeRequest(`/api/tasks/${task.id}`, token, {}, "import"));
  }
  if (task.status === "failed") {
    throw new DouyinBridgeError(task.error?.code || "download_failed", task.error?.message || "本机导入失败", "import");
  }
  if (!task.download_url || !DOWNLOAD_URL.test(task.download_url)) throw new Error("本机辅助服务未返回有效的下载地址。");
  onStatus("正在从本机读取视频…");
  const media = await bridgeRequest(task.download_url, token, {}, "download");
  const blob = await media.blob();
  if (!blob.size || (typeof task.download?.size === "number" && blob.size !== task.download.size)) throw new Error("本机视频下载不完整。");
  const extension = task.metadata?.ext?.toLowerCase();
  const ext = extension && /^[a-z0-9]{2,5}$/.test(extension) ? extension : "mp4";
  const name = `${task.metadata?.id?.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80) || task.id}.${ext}`;
  const form = new FormData();
  form.append("file", new File([blob], name, { type: blob.type || "video/mp4" }));
  onStatus("正在上传到 Studio 资产库…");
  let uploaded: Response;
  try {
    uploaded = await fetch("/api/assets", { method: "POST", body: form });
  } catch {
    throw new DouyinBridgeError("upload_failed", "无法连接 Studio 资产库。", "upload");
  }
  const body = await uploaded.json().catch(() => ({})) as { asset?: unknown; error?: { message?: string } };
  if (!uploaded.ok) throw new DouyinBridgeError("upload_failed", body.error?.message || `上传资产失败 (${uploaded.status})`, "upload");
  const asset = remoteAssetToLibraryItem(body.asset ?? body);
  if (!asset || asset.kind !== "video") throw new DouyinBridgeError("upload_failed", "Studio 未返回有效的视频资产。", "upload");
  onStatus("已导入资产库");
  return asset;
}
