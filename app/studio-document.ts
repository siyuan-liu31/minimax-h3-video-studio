import type { ProfileCapability } from "./studio-capabilities.ts";
import { normalizeVideoDirectorMode, type VideoDirectorMode } from "./studio-video-mode.ts";

export const CANVAS_DOCUMENT_VERSION = 7 as const;
export const V7_STORAGE_KEY = "h3-studio-workflow-v7";
export const V7_BACKUP_KEY = "h3-studio-workflow-v6-backup-before-v7";
export const LEGACY_STORAGE_KEYS = [
  "h3-studio-workflow-v6",
  "h3-studio-workflow-v5",
  "h3-studio-workflow-v4",
  "h3-studio-workflow-v3",
  "h3-studio-workflow-v2",
] as const;

export const H3_CANVAS_FPS = 24;
export const H3_MIN_DURATION_FRAMES = 124;
export const H3_MAX_DURATION_FRAMES = 362;
export const H3_MAX_DURATION_SECONDS = H3_MAX_DURATION_FRAMES / H3_CANVAS_FPS;
export const H3_REFERENCE_BUDGET = 12;
export const H3_REFERENCE_CAPACITY = { image: 9, video: 3, audio: 3 } as const;

export type XY = { x: number; y: number };
export type NodeSize = { width: number; height: number };
export type Viewport = { x: number; y: number; zoom: number };
export type MediaKind = "image" | "video" | "audio";
export type SamplingPreset = "turbo4" | "base20";
export type CanvasNodeKind = "asset" | "video-generator" | "image-generator" | "output";
export type GeneratorNodeKind = Extract<CanvasNodeKind, "video-generator" | "image-generator">;

export type PromptToken =
  | { kind: "text"; text: string }
  | { kind: "mention"; bindingId: string; label: string };

export type PromptDocument = { tokens: PromptToken[] };

export type RepairCode =
  | "dangling-edge"
  | "dangling-binding"
  | "reference-budget-exceeded"
  | "media-capacity-exceeded"
  | "duration-exceeded"
  | "duration-grid-invalid"
  | "asset-rebind-required";

export type RepairFlag = { code: RepairCode; message: string; relatedId?: string };

export type NodeJobStatus = "idle" | "submitting" | "queued" | "running" | "completed" | "failed" | "cancelled" | "unknown";
export type NodeJobState = {
  id?: string;
  status: NodeJobStatus;
  progress: number;
  message: string;
  submittedRevision?: number;
  receipt?: Record<string, unknown>;
  error?: string;
};

export type NodeResult = {
  id: string;
  mediaKind: "image" | "video";
  createdAt?: number;
  previewUrl?: string;
  thumbnailUrl?: string;
  downloadUrl?: string;
  prompt?: string;
  revision?: number;
  receipt?: Record<string, unknown>;
};

export type MediaBinding = {
  id: string;
  kind: MediaKind;
  slot: number;
  sourceNodeId: string;
  sourceOutputHandle: string;
  role: string;
  pairedBindingId?: string;
};

type CanvasNodeBase = {
  id: string;
  title: string;
  position: XY;
  size: NodeSize;
  repairFlags: RepairFlag[];
};

export type AssetNode = CanvasNodeBase & {
  kind: "asset";
  mediaKind: MediaKind;
  asset: {
    remoteId?: string;
    fileName: string;
    contentUrl?: string;
    thumbnailUrl?: string;
    uploadState: "uploading" | "ready" | "error";
    media?: Record<string, unknown>;
    role?: string;
    source?: {
      kind: "library" | "job" | "derivation" | "local";
      localUrl?: string;
      sourceJobId?: string;
      derivationId?: string;
    };
  };
};

type GeneratorNodeBase = CanvasNodeBase & {
  prompt: PromptDocument;
  bindings: MediaBinding[];
  job: NodeJobState;
  resultVersions: NodeResult[];
  configRevision: number;
  lastSuccessfulRevision?: number;
};

export type VideoGeneratorConfig = {
  directorMode: VideoDirectorMode;
  samplingPreset: SamplingPreset;
  aspectRatio: "16:9" | "9:16";
  durationFrames: number;
  steps: number;
  loraStrength: number;
  denoise: number;
  seed: number;
  sourceBindingId?: string;
};

export type ImageGeneratorConfig = {
  profileId: string;
  aspectRatio: "16:9" | "9:16" | "3:4" | "1:1";
  quality: "1K" | "2K";
  steps: number;
  cfg: number;
  loraStrength: number;
  denoise: number;
  seed: number;
  negativePrompt: string;
};

export type VideoGeneratorNode = GeneratorNodeBase & { kind: "video-generator"; config: VideoGeneratorConfig };
export type ImageGeneratorNode = GeneratorNodeBase & { kind: "image-generator"; config: ImageGeneratorConfig };
export type OutputNode = CanvasNodeBase & {
  kind: "output";
  resultVersions: NodeResult[];
};

export type CanvasNode = AssetNode | VideoGeneratorNode | ImageGeneratorNode | OutputNode;

export type CanvasEdge = {
  id: string;
  sourceNodeId: string;
  sourceHandle: string;
  targetNodeId: string;
  targetHandle: string;
  order: number;
};

export type CanvasGroup = {
  id: string;
  title: string;
  position: XY;
  size: NodeSize;
  nodeIds: string[];
  sequence: string[];
};

export type CanvasDocumentV7 = {
  version: typeof CANVAS_DOCUMENT_VERSION;
  viewport: Viewport;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  groups: CanvasGroup[];
  unassignedResults: NodeResult[];
  migration: {
    sourceVersion?: number;
    issues: string[];
  };
};

export type DocumentIssue = {
  code: RepairCode;
  message: string;
  nodeId?: string;
  edgeId?: string;
  bindingId?: string;
};

export type IdFactory = () => string;

export type MigrationOptions = {
  createId?: IdFactory;
};

export type ParseCanvasDocumentResult = {
  ok: boolean;
  document: CanvasDocumentV7;
  migrated: boolean;
  sourceVersion?: number;
  backup?: { key: string; value: string };
  error?: string;
};

export type VideoProfileRequirement = {
  requestedMode: VideoDirectorMode;
  resolvedMode: Exclude<VideoDirectorMode, "auto">;
  compiler: "h3_fl" | "h3_ref";
  samplingPreset: SamplingPreset;
  samplingMode: "turbo4" | "base";
};

export type VideoProfileResolution = VideoProfileRequirement & {
  profile?: ProfileCapability;
  available: boolean;
  reason?: string;
};

const DEFAULT_VIEWPORT: Viewport = { x: 0, y: 0, zoom: 1 };
const DEFAULT_JOB: NodeJobState = { status: "idle", progress: 0, message: "准备就绪" };
const DEFAULT_VIDEO_SIZE: NodeSize = { width: 620, height: 640 };
const DEFAULT_IMAGE_SIZE: NodeSize = { width: 560, height: 600 };
const DEFAULT_ASSET_SIZE: NodeSize = { width: 248, height: 284 };
const DEFAULT_OUTPUT_SIZE: NodeSize = { width: 360, height: 360 };

function numberValue(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function mediaKind(value: unknown): MediaKind | undefined {
  return value === "image" || value === "video" || value === "audio" ? value : undefined;
}

function positionValue(value: unknown, fallback: XY): XY {
  const record = recordValue(value);
  return { x: numberValue(record.x, fallback.x), y: numberValue(record.y, fallback.y) };
}

function promptDocument(text: unknown): PromptDocument {
  return { tokens: [{ kind: "text", text: stringValue(text) }] };
}

function clonePrompt(prompt: PromptDocument): PromptDocument {
  return { tokens: prompt.tokens.map((token) => ({ ...token })) };
}

function tokenizeLegacyMentions(prompt: PromptDocument, bindings: MediaBinding[], nodeById: Map<string, CanvasNode>): PromptDocument {
  const text = prompt.tokens.length === 1 && prompt.tokens[0].kind === "text" ? prompt.tokens[0].text : undefined;
  if (text === undefined || !text.includes("@{")) return prompt;
  const bindingByAssetId = new Map<string, { binding: MediaBinding; label: string }>();
  for (const binding of bindings) {
    const source = nodeById.get(binding.sourceNodeId);
    if (source?.kind !== "asset" || !source.asset.remoteId || bindingByAssetId.has(source.asset.remoteId)) continue;
    bindingByAssetId.set(source.asset.remoteId, { binding, label: source.asset.fileName || source.asset.remoteId });
  }
  const tokens: PromptToken[] = [];
  const expression = /@\{([^}]+)\}/gu;
  let cursor = 0;
  for (const match of text.matchAll(expression)) {
    const index = match.index ?? cursor;
    if (index > cursor) tokens.push({ kind: "text", text: text.slice(cursor, index) });
    const resolved = bindingByAssetId.get(match[1]);
    if (resolved) tokens.push({ kind: "mention", bindingId: resolved.binding.id, label: resolved.label });
    else tokens.push({ kind: "text", text: match[0] });
    cursor = index + match[0].length;
  }
  if (cursor < text.length) tokens.push({ kind: "text", text: text.slice(cursor) });
  return { tokens: tokens.length ? tokens : [{ kind: "text", text }] };
}

function outputMediaKind(node: CanvasNode | undefined): MediaKind | undefined {
  if (!node) return undefined;
  if (node.kind === "asset") return node.mediaKind;
  if (node.kind === "image-generator") return "image";
  if (node.kind === "video-generator") return "video";
  return undefined;
}

function uuidFallback(): string {
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function createCanvasUuid(): string {
  return globalThis.crypto?.randomUUID?.() ?? uuidFallback();
}

function freshJob(): NodeJobState {
  return { ...DEFAULT_JOB };
}

export function createCanvasNode(
  kind: CanvasNodeKind,
  position: XY = { x: 80, y: 80 },
  createId: IdFactory = createCanvasUuid,
): CanvasNode {
  const id = createId();
  if (kind === "video-generator") return {
    id, kind, title: "H3 Video", position, size: { ...DEFAULT_VIDEO_SIZE }, repairFlags: [],
    prompt: promptDocument(""), bindings: [], job: freshJob(), resultVersions: [], configRevision: 0,
    config: { directorMode: "auto", samplingPreset: "turbo4", aspectRatio: "16:9", durationFrames: 124, steps: 4, loraStrength: 0.75, denoise: 1, seed: -1 },
  };
  if (kind === "image-generator") return {
    id, kind, title: "Image Generation", position, size: { ...DEFAULT_IMAGE_SIZE }, repairFlags: [],
    prompt: promptDocument(""), bindings: [], job: freshJob(), resultVersions: [], configRevision: 0,
    config: { profileId: "auto", aspectRatio: "16:9", quality: "1K", steps: 24, cfg: 7, loraStrength: 1, denoise: 0.65, seed: -1, negativePrompt: "low quality, blurry, distorted anatomy, watermark, text" },
  };
  if (kind === "output") return { id, kind, title: "Output", position, size: { ...DEFAULT_OUTPUT_SIZE }, repairFlags: [], resultVersions: [] };
  return {
    id, kind, title: "Media", position, size: { ...DEFAULT_ASSET_SIZE }, repairFlags: [], mediaKind: "image",
    asset: { fileName: "", uploadState: "error" },
  };
}

export function createDefaultCanvasDocument(createId: IdFactory = createCanvasUuid): CanvasDocumentV7 {
  const video = createCanvasNode("video-generator", { x: 120, y: 80 }, createId) as VideoGeneratorNode;
  const output = createCanvasNode("output", { x: 820, y: 180 }, createId) as OutputNode;
  return {
    version: CANVAS_DOCUMENT_VERSION,
    viewport: { ...DEFAULT_VIEWPORT },
    nodes: [video, output],
    edges: [{ id: createId(), sourceNodeId: video.id, sourceHandle: "video", targetNodeId: output.id, targetHandle: "input", order: 0 }],
    groups: [],
    unassignedResults: [],
    migration: { issues: [] },
  };
}

export function h3DurationFrames(seconds: number): number {
  return Math.round(seconds * H3_CANVAS_FPS);
}

export function isValidH3DurationFrames(frames: number): boolean {
  return Number.isInteger(frames) && frames >= H3_MIN_DURATION_FRAMES && frames <= H3_MAX_DURATION_FRAMES && (frames - 5) % 17 === 0;
}

function samplingPresetFromLegacy(snapshot: Record<string, unknown>, videoParams: Record<string, unknown>): SamplingPreset {
  const selections = recordValue(snapshot.profileSelection);
  const job = recordValue(snapshot.job);
  const parameters = recordValue(job.parameters);
  const evidence = recordValue(job.workflowEvidence ?? job.workflow_evidence);
  const text = [selections.video, parameters.profile_id, parameters.sampling_mode, evidence.sampling_mode, videoParams.samplingPreset]
    .filter((value) => typeof value === "string").join(" ").toLowerCase();
  if (/base20|(?:^|[^a-z])base(?:[^a-z]|$)|no[ _-]?turbo/.test(text)) return "base20";
  return "turbo4";
}

function legacyResult(jobValue: unknown, createId: IdFactory): NodeResult | undefined {
  const job = recordValue(jobValue);
  if (!Object.keys(job).length || (!job.id && !job.previewUrl && !job.downloadUrl)) return undefined;
  const parameters = recordValue(job.parameters);
  const media = stringValue(job.media || parameters.output_type).toLowerCase();
  if (media !== "video" && media !== "image") return undefined;
  const id = typeof job.id === "string" && job.id ? job.id : createId();
  return {
    id,
    mediaKind: media,
    ...(typeof job.createdAt === "number" ? { createdAt: job.createdAt } : {}),
    ...(typeof job.previewUrl === "string" ? { previewUrl: job.previewUrl } : {}),
    ...(typeof job.thumbnailUrl === "string" ? { thumbnailUrl: job.thumbnailUrl } : {}),
    ...(typeof job.downloadUrl === "string" ? { downloadUrl: job.downloadUrl } : {}),
    ...(typeof job.prompt === "string" ? { prompt: job.prompt } : {}),
    receipt: { ...parameters, ...(recordValue(job.workflowEvidence ?? job.workflow_evidence)) },
  };
}

function legacyJobState(jobValue: unknown): NodeJobState {
  const job = recordValue(jobValue);
  const rawStatus = stringValue(job.status, "idle");
  const allowed: NodeJobStatus[] = ["idle", "submitting", "queued", "running", "completed", "failed", "cancelled", "unknown"];
  const aliases: Record<string, NodeJobStatus> = { success: "completed", done: "completed", error: "failed", canceled: "cancelled" };
  const status = allowed.includes(rawStatus as NodeJobStatus) ? rawStatus as NodeJobStatus : aliases[rawStatus] ?? "unknown";
  return {
    ...(typeof job.id === "string" ? { id: job.id } : {}),
    status,
    progress: numberValue(job.progress, status === "completed" ? 100 : 0),
    message: stringValue(job.message, status === "idle" ? "准备就绪" : status),
    ...(typeof job.error === "string" ? { error: job.error } : {}),
    ...(Object.keys(recordValue(job.parameters)).length ? { receipt: recordValue(job.parameters) } : {}),
  };
}

function legacyNodeKind(value: unknown): CanvasNodeKind | undefined {
  if (value === "asset") return "asset";
  if (value === "video") return "video-generator";
  if (value === "image") return "image-generator";
  if (value === "output") return "output";
  return undefined;
}

function legacyAssetNode(oldNode: Record<string, unknown>, id: string): AssetNode {
  const asset = recordValue(oldNode.asset);
  const kind = mediaKind(asset.media) ?? "image";
  const remoteId = stringValue(asset.remoteId) || undefined;
  return {
    id,
    kind: "asset",
    title: stringValue(oldNode.title, `${kind} reference`),
    position: positionValue(oldNode.position, { x: 80, y: 80 }),
    size: { ...DEFAULT_ASSET_SIZE },
    repairFlags: remoteId ? [] : [{ code: "asset-rebind-required", message: "本地素材不能跨刷新恢复，需要重新绑定。" }],
    mediaKind: kind,
    asset: {
      ...(remoteId ? { remoteId } : {}),
      fileName: stringValue(asset.fileName),
      ...(remoteId ? { contentUrl: `/api/assets/${remoteId}/content` } : {}),
      ...(typeof asset.thumbnailUrl === "string" ? { thumbnailUrl: asset.thumbnailUrl } : {}),
      uploadState: remoteId ? "ready" : "error",
      media: recordValue(asset.mediaMeta),
    },
  };
}

function asVideoGenerator(oldNode: Record<string, unknown>, id: string, snapshot: Record<string, unknown>): VideoGeneratorNode {
  const params = recordValue(snapshot.videoParams);
  const durationFrames = h3DurationFrames(numberValue(params.duration, 124 / H3_CANVAS_FPS));
  const samplingPreset = samplingPresetFromLegacy(snapshot, params);
  return {
    id, kind: "video-generator", title: stringValue(oldNode.title, "H3 Video"),
    position: positionValue(oldNode.position, { x: 470, y: 66 }), size: { ...DEFAULT_VIDEO_SIZE }, repairFlags: [],
    prompt: promptDocument(""), bindings: [], job: freshJob(), resultVersions: [], configRevision: 0,
    config: {
      directorMode: normalizeVideoDirectorMode(params.directorMode, params.modelMode),
      samplingPreset,
      aspectRatio: params.aspectRatio === "9:16" ? "9:16" : "16:9",
      durationFrames,
      steps: numberValue(params.steps, samplingPreset === "base20" ? 20 : 4),
      loraStrength: samplingPreset === "base20" ? 0 : numberValue(params.loraStrength, 0.75),
      denoise: numberValue(params.denoise, 1),
      seed: numberValue(params.seed, -1),
    },
  };
}

function asImageGenerator(oldNode: Record<string, unknown>, id: string, snapshot: Record<string, unknown>): ImageGeneratorNode {
  const params = recordValue(snapshot.imageParams);
  const selection = recordValue(snapshot.profileSelection);
  const ratio = ["16:9", "9:16", "3:4", "1:1"].includes(stringValue(params.aspectRatio)) ? stringValue(params.aspectRatio) as ImageGeneratorConfig["aspectRatio"] : "16:9";
  return {
    id, kind: "image-generator", title: stringValue(oldNode.title, "Image Generation"),
    position: positionValue(oldNode.position, { x: 470, y: 390 }), size: { ...DEFAULT_IMAGE_SIZE }, repairFlags: [],
    prompt: promptDocument(""), bindings: [], job: freshJob(), resultVersions: [], configRevision: 0,
    config: {
      profileId: stringValue(selection.image, "auto"), aspectRatio: ratio, quality: params.quality === "2K" ? "2K" : "1K",
      steps: numberValue(params.steps, 24), cfg: numberValue(params.cfg, 7), loraStrength: numberValue(params.loraStrength, 1),
      denoise: numberValue(params.denoise, 0.65), seed: numberValue(params.seed, -1),
      negativePrompt: stringValue(params.negativePrompt, "low quality, blurry, distorted anatomy, watermark, text"),
    },
  };
}

function asOutputNode(oldNode: Record<string, unknown>, id: string): OutputNode {
  return { id, kind: "output", title: stringValue(oldNode.title, "Output"), position: positionValue(oldNode.position, { x: 875, y: 200 }), size: { ...DEFAULT_OUTPUT_SIZE }, repairFlags: [], resultVersions: [] };
}

function migrateLegacyDocument(snapshot: Record<string, unknown>, createId: IdFactory): CanvasDocumentV7 {
  const sourceVersion = Math.min(6, Math.max(2, Math.floor(numberValue(snapshot.version, 2))));
  const oldNodes = arrayValue(snapshot.nodes).map(recordValue);
  const oldEdges = arrayValue(snapshot.edges).map(recordValue);
  const idMap = new Map<string, string>();
  for (const oldNode of oldNodes) {
    const oldId = stringValue(oldNode.id);
    if (oldId) idMap.set(oldId, createId());
  }

  const nodes: CanvasNode[] = [];
  for (const oldNode of oldNodes) {
    const oldId = stringValue(oldNode.id);
    const id = idMap.get(oldId);
    const kind = legacyNodeKind(oldNode.kind);
    if (!id || !kind) continue;
    if (kind === "asset") nodes.push(legacyAssetNode(oldNode, id));
    else if (kind === "video-generator") nodes.push(asVideoGenerator(oldNode, id, snapshot));
    else if (kind === "image-generator") nodes.push(asImageGenerator(oldNode, id, snapshot));
    else nodes.push(asOutputNode(oldNode, id));
  }

  if (!nodes.some((node) => node.kind === "video-generator")) nodes.push(createCanvasNode("video-generator", { x: 470, y: 66 }, createId));
  if (!nodes.some((node) => node.kind === "image-generator")) nodes.push(createCanvasNode("image-generator", { x: 470, y: 390 }, createId));
  if (!nodes.some((node) => node.kind === "output")) nodes.push(createCanvasNode("output", { x: 875, y: 200 }, createId));

  const promptTargets = new Set(oldEdges.filter((edge) => oldNodes.find((node) => node.id === edge.source)?.kind === "prompt").map((edge) => stringValue(edge.target)));
  for (const node of nodes) {
    if (node.kind === "video-generator") {
      const oldId = [...idMap].find(([, next]) => next === node.id)?.[0];
      if (!promptTargets.size || (oldId && promptTargets.has(oldId))) node.prompt = promptDocument(snapshot.prompt);
    } else if (node.kind === "image-generator") {
      const oldId = [...idMap].find(([, next]) => next === node.id)?.[0];
      const imagePrompt = stringValue(snapshot.imagePrompt);
      node.prompt = promptDocument(imagePrompt || (oldId && promptTargets.has(oldId) ? snapshot.prompt : ""));
    }
  }

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const migratedEdges: CanvasEdge[] = [];
  const bindingCounters = new Map<string, Record<MediaKind, number>>();
  const orderedEdges = oldEdges.map((edge, index) => ({ edge, index })).sort((a, b) => {
    const left = numberValue(recordValue(a.edge.data).reference_index, a.index);
    const right = numberValue(recordValue(b.edge.data).reference_index, b.index);
    return left - right || a.index - b.index;
  });
  for (const { edge, index } of orderedEdges) {
    const oldSource = stringValue(edge.source);
    const oldTarget = stringValue(edge.target);
    const sourceId = idMap.get(oldSource);
    const targetId = idMap.get(oldTarget);
    if (!sourceId || !targetId) continue;
    const source = nodeById.get(sourceId);
    const target = nodeById.get(targetId);
    if (!source || !target || oldNodes.find((node) => node.id === oldSource)?.kind === "prompt") continue;
    const kind = outputMediaKind(source);
    if ((target.kind === "video-generator" || target.kind === "image-generator") && kind) {
      const counters = bindingCounters.get(target.id) ?? { image: 0, video: 0, audio: 0 };
      counters[kind] += 1;
      bindingCounters.set(target.id, counters);
      const bindingId = createId();
      const binding: MediaBinding = {
        id: bindingId, kind, slot: counters[kind], sourceNodeId: source.id, sourceOutputHandle: kind,
        role: stringValue(recordValue(edge.data).role, stringValue(edge.role, "reference")),
      };
      target.bindings.push(binding);
      migratedEdges.push({ id: createId(), sourceNodeId: source.id, sourceHandle: kind, targetNodeId: target.id, targetHandle: `${kind}:${binding.slot}`, order: index * 2 });
      const includeAudio = kind === "video" && Boolean(recordValue(edge.data).include_audio ?? (source.kind === "asset" ? recordValue(oldNodes.find((node) => node.id === oldSource)?.asset).includeAudio : false));
      if (includeAudio) {
        counters.audio += 1;
        const pairedId = createId();
        binding.pairedBindingId = pairedId;
        target.bindings.push({ id: pairedId, kind: "audio", slot: counters.audio, sourceNodeId: source.id, sourceOutputHandle: "audio", role: "soundtrack", pairedBindingId: bindingId });
        migratedEdges.push({ id: createId(), sourceNodeId: source.id, sourceHandle: "audio", targetNodeId: target.id, targetHandle: `audio:${counters.audio}`, order: index * 2 + 1 });
      }
      continue;
    }
    if (target.kind === "output" && (source.kind === "video-generator" || source.kind === "image-generator")) {
      migratedEdges.push({ id: createId(), sourceNodeId: source.id, sourceHandle: source.kind === "video-generator" ? "video" : "image", targetNodeId: target.id, targetHandle: "input", order: index });
    }
  }

  const legacySourceVideoId = stringValue(recordValue(snapshot.videoParams).sourceVideoId);
  for (const node of nodes) {
    if (node.kind !== "video-generator" && node.kind !== "image-generator") continue;
    node.prompt = tokenizeLegacyMentions(node.prompt, node.bindings, nodeById);
    if (node.kind === "video-generator" && legacySourceVideoId) {
      const source = node.bindings.find((binding) => {
        const sourceNode = nodeById.get(binding.sourceNodeId);
        return binding.kind === "video" && sourceNode?.kind === "asset" && sourceNode.asset.remoteId === legacySourceVideoId;
      });
      if (source) node.config.sourceBindingId = source.id;
    }
  }

  const generatorsByMedia = {
    video: nodes.filter((node): node is VideoGeneratorNode => node.kind === "video-generator"),
    image: nodes.filter((node): node is ImageGeneratorNode => node.kind === "image-generator"),
  };
  const allJobs = [...arrayValue(snapshot.jobHistory), snapshot.job].filter(Boolean);
  const unassignedResults: NodeResult[] = [];
  for (const jobValue of allJobs) {
    const result = legacyResult(jobValue, createId);
    if (!result) continue;
    const matching = generatorsByMedia[result.mediaKind];
    const generator = matching.length === 1 ? matching[0] : undefined;
    if (!generator) unassignedResults.push(result);
    else if (!generator.resultVersions.some((item) => item.id === result.id)) generator.resultVersions.push(result);
  }
  const currentResult = legacyResult(snapshot.job, createId);
  if (currentResult) {
    const matching = generatorsByMedia[currentResult.mediaKind];
    if (matching.length === 1) matching[0].job = legacyJobState(snapshot.job);
  }

  const document: CanvasDocumentV7 = {
    version: CANVAS_DOCUMENT_VERSION,
    viewport: { ...DEFAULT_VIEWPORT },
    nodes,
    edges: migratedEdges,
    groups: [],
    unassignedResults,
    migration: { sourceVersion, issues: [] },
  };
  return withRepairFlags(document);
}

function isV7(value: unknown): value is CanvasDocumentV7 {
  const record = recordValue(value);
  return record.version === CANVAS_DOCUMENT_VERSION && Array.isArray(record.nodes) && Array.isArray(record.edges);
}

function cloneV7(document: CanvasDocumentV7): CanvasDocumentV7 {
  return structuredClone(document);
}

export function validateCanvasDocument(document: CanvasDocumentV7): DocumentIssue[] {
  const issues: DocumentIssue[] = [];
  const nodeIds = new Set(document.nodes.map((node) => node.id));
  for (const edge of document.edges) {
    if (!nodeIds.has(edge.sourceNodeId) || !nodeIds.has(edge.targetNodeId)) issues.push({ code: "dangling-edge", edgeId: edge.id, message: "连线指向不存在的节点。" });
  }
  for (const node of document.nodes) {
    if (node.kind !== "video-generator" && node.kind !== "image-generator") continue;
    for (const binding of node.bindings) {
      if (!nodeIds.has(binding.sourceNodeId)) issues.push({ code: "dangling-binding", nodeId: node.id, bindingId: binding.id, message: "素材绑定指向不存在的节点。" });
    }
    const referenceFiles = new Set(node.bindings.map((binding) => binding.sourceNodeId)).size;
    if (referenceFiles > H3_REFERENCE_BUDGET) issues.push({ code: "reference-budget-exceeded", nodeId: node.id, message: `已绑定 ${referenceFiles} 个参考文件，超过最多 ${H3_REFERENCE_BUDGET} 个的限制。` });
    for (const kind of ["image", "video", "audio"] as const) {
      const count = node.bindings.filter((binding) => binding.kind === kind).length;
      if (count > H3_REFERENCE_CAPACITY[kind]) issues.push({ code: "media-capacity-exceeded", nodeId: node.id, message: `${kind} 输入 ${count} 个，超过容量 ${H3_REFERENCE_CAPACITY[kind]}。` });
    }
    if (node.kind === "video-generator") {
      if (node.config.durationFrames > H3_MAX_DURATION_FRAMES) issues.push({ code: "duration-exceeded", nodeId: node.id, message: `视频时长 ${node.config.durationFrames} 帧超过 ${H3_MAX_DURATION_FRAMES} 帧上限。` });
      else if (!isValidH3DurationFrames(node.config.durationFrames)) issues.push({ code: "duration-grid-invalid", nodeId: node.id, message: "视频时长不在 H3 的 17k+5 帧网格上。" });
    }
  }
  return issues;
}

function withRepairFlags(document: CanvasDocumentV7): CanvasDocumentV7 {
  const nodeMap = new Map(document.nodes.map((node) => [node.id, { ...node, repairFlags: node.repairFlags.filter((flag) => flag.code === "asset-rebind-required") } as CanvasNode]));
  const issues = validateCanvasDocument({ ...document, nodes: [...nodeMap.values()] });
  for (const issue of issues) {
    if (!issue.nodeId) continue;
    const node = nodeMap.get(issue.nodeId);
    if (!node) continue;
    node.repairFlags = [...node.repairFlags, { code: issue.code, message: issue.message, relatedId: issue.bindingId ?? issue.edgeId }];
  }
  return { ...document, nodes: [...nodeMap.values()], migration: { ...document.migration, issues: issues.map((issue) => issue.message) } };
}

export function migrateCanvasDocument(input: unknown, options: MigrationOptions = {}): CanvasDocumentV7 {
  const createId = options.createId ?? createCanvasUuid;
  if (isV7(input)) return withRepairFlags(cloneV7(input));
  const snapshot = recordValue(input);
  if (!Array.isArray(snapshot.nodes) || !Array.isArray(snapshot.edges)) return createDefaultCanvasDocument(createId);
  return migrateLegacyDocument(snapshot, createId);
}

export function createMigrationBackup(raw: string): { key: string; value: string } {
  return { key: V7_BACKUP_KEY, value: raw };
}

export function parseCanvasDocument(raw: string, options: MigrationOptions = {}): ParseCanvasDocumentResult {
  try {
    const parsed: unknown = JSON.parse(raw);
    const sourceVersion = numberValue(recordValue(parsed).version, 2);
    const migrated = sourceVersion !== CANVAS_DOCUMENT_VERSION;
    return {
      ok: true,
      document: migrateCanvasDocument(parsed, options),
      migrated,
      sourceVersion,
      ...(migrated ? { backup: createMigrationBackup(raw) } : {}),
    };
  } catch (error) {
    return {
      ok: false,
      document: createDefaultCanvasDocument(options.createId),
      migrated: false,
      error: error instanceof Error ? error.message : "Invalid canvas document JSON",
    };
  }
}

export function serializeCanvasDocument(document: CanvasDocumentV7): string {
  return JSON.stringify(migrateCanvasDocument(document));
}

function inferAutoMode(bindings: MediaBinding[]): Exclude<VideoDirectorMode, "auto"> {
  if (!bindings.length) return "t2v";
  const pictures = bindings.filter((binding) => binding.kind === "image");
  if (bindings.length === 1 && pictures[0]?.role === "first_frame") return "i2v";
  if (bindings.length <= 2 && pictures.length === bindings.length && pictures.every((binding) => binding.role === "first_frame" || binding.role === "last_frame")) return "fl2v";
  return "r2v";
}

export function resolveVideoProfileRequirement(
  mode: VideoDirectorMode,
  samplingPreset: SamplingPreset,
  bindings: MediaBinding[] = [],
): VideoProfileRequirement {
  const resolvedMode = mode === "auto" ? inferAutoMode(bindings) : mode;
  const compiler = resolvedMode === "r2v" || resolvedMode === "v2v" || resolvedMode === "rv2v" ? "h3_ref" : "h3_fl";
  return { requestedMode: mode, resolvedMode, compiler, samplingPreset, samplingMode: samplingPreset === "base20" ? "base" : "turbo4" };
}

function compareProfileVersion(left: ProfileCapability, right: ProfileCapability): number {
  return right.version.localeCompare(left.version, undefined, { numeric: true, sensitivity: "base" });
}

export function resolveVideoExecutionProfile(
  mode: VideoDirectorMode,
  samplingPreset: SamplingPreset,
  profiles: ProfileCapability[],
  bindings: MediaBinding[] = [],
): VideoProfileResolution {
  const requirement = resolveVideoProfileRequirement(mode, samplingPreset, bindings);
  const candidates = profiles.filter((profile) => profile.output_type === "video" && profile.compiler === requirement.compiler && profile.sampling_mode === requirement.samplingMode).sort(compareProfileVersion);
  const profile = candidates.find((candidate) => candidate.available);
  if (profile) return { ...requirement, profile, available: true };
  const reason = candidates.length ? "匹配的 Profile 缺少模型或节点依赖。" : `没有 ${requirement.compiler} + ${requirement.samplingMode} 的视频 Profile。`;
  return { ...requirement, available: false, reason };
}

export function copyGeneratorNode<T extends VideoGeneratorNode | ImageGeneratorNode>(node: T, createId: IdFactory = createCanvasUuid): T {
  const bindingIds = new Map(node.bindings.map((binding) => [binding.id, createId()]));
  return {
    ...structuredClone(node),
    id: createId(),
    prompt: {
      tokens: clonePrompt(node.prompt).tokens.map((token) => token.kind === "mention"
        ? { ...token, bindingId: bindingIds.get(token.bindingId) ?? token.bindingId }
        : token),
    },
    bindings: node.bindings.map((binding) => ({
      ...binding,
      id: bindingIds.get(binding.id)!,
      ...(binding.pairedBindingId ? { pairedBindingId: bindingIds.get(binding.pairedBindingId) ?? binding.pairedBindingId } : {}),
    })),
    job: freshJob(),
    resultVersions: [],
    lastSuccessfulRevision: undefined,
  } as T;
}
