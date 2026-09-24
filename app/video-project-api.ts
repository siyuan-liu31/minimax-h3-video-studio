import type { SerializedVideoProject, VideoProject } from "./video-project";

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export class VideoProjectApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "VideoProjectApiError";
    this.status = status;
    this.code = code;
  }
}

function projectFrom(value: unknown): VideoProject {
  if (!value || typeof value !== "object") throw new VideoProjectApiError("服务未返回有效长视频项目", 502);
  const record = value as Record<string, unknown>;
  const project = record.project && typeof record.project === "object" ? record.project : record;
  return project as VideoProject;
}

export class VideoProjectApi {
  private readonly fetcher: FetchLike;

  // Native browser fetch is receiver-sensitive. Keeping it as a property and
  // later invoking `this.fetcher(...)` gives it the VideoProjectApi instance as
  // `this`, which Chromium rejects with "Illegal invocation". The wrapper
  // deliberately performs the native call through the global object.
  constructor(fetcher: FetchLike = (url, init) => globalThis.fetch(url, init)) { this.fetcher = fetcher; }

  private async request(path: string, method = "GET", body?: unknown): Promise<unknown> {
    const response = await this.fetcher(path, {
      method,
      cache: "no-store",
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const value = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) {
      const nested = value.error && typeof value.error === "object" ? value.error as Record<string, unknown> : undefined;
      const message = typeof nested?.message === "string" ? nested.message : typeof value.message === "string" ? value.message : `长视频服务请求失败 (${response.status})`;
      throw new VideoProjectApiError(message, response.status, typeof nested?.code === "string" ? nested.code : undefined);
    }
    return value;
  }

  async list(): Promise<VideoProject[]> {
    const value = await this.request("/api/video-projects") as { projects?: unknown[] };
    return (Array.isArray(value.projects) ? value.projects : []).filter((project): project is VideoProject => Boolean(project && typeof project === "object"));
  }

  async create(project: SerializedVideoProject): Promise<VideoProject> {
    return projectFrom(await this.request("/api/video-projects", "POST", project));
  }

  async get(projectId: string): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}`));
  }

  async save(projectId: string, project: SerializedVideoProject): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}`, "PUT", project));
  }

  async delete(projectId: string): Promise<void> {
    await this.request(`/api/video-projects/${encodeURIComponent(projectId)}`, "DELETE");
  }

  async run(projectId: string): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}/run`, "POST", {}));
  }

  async runSelected(projectId: string, segmentIds: string[]): Promise<VideoProject> {
    return projectFrom(await this.request(
      `/api/video-projects/${encodeURIComponent(projectId)}/run`,
      "POST",
      { segment_ids: [...new Set(segmentIds)] },
    ));
  }

  async stop(projectId: string): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}/stop`, "POST", {}));
  }

  async merge(projectId: string): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}/merge`, "POST", {}));
  }

  async editReplicationSegment(projectId: string, segmentId: string, edit: {
    expected_updated_at: number; prompt?: string; steps?: number; seed?: number;
  }): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segmentId)}`, "PATCH", edit));
  }

  async runSegment(projectId: string, segmentId: string): Promise<VideoProject> {
    return projectFrom(await this.request(`/api/video-projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segmentId)}/run`, "POST", {}));
  }
}
