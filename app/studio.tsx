"use client";

/* Blob URLs and authenticated generated-media routes are intentionally rendered directly. */
/* eslint-disable @next/next/no-img-element */

import { ChangeEvent, DragEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, SetStateAction, type CSSProperties, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { imageProfileAcceptsReferenceCount, imageReferencePolicy, profileSupportsParameter, promptImageReferenceNumbers, type ProfileCapability, type UnavailableProfileCapability } from "./studio-capabilities";
import { imageDimensions, type ImageAspectRatio, type ImageQuality } from "./studio-config";
import { JOB_HISTORY_CACHE_KEY, currentOriginApiUrl, formatJobElapsed, formatJobTime, jobParameterRows, mergeJobHistory, mergeJobHistoryPage, parseJobHistoryCacheEnvelope, rebaseStudioJobMedia, resumeGenerationJob, serializeJobHistoryCache, serverJobToStudioJob, type GenerationParameters, type StudioJob as Job } from "./studio-history";
import { createLibraryFolder, deleteDerivedMedia, deleteLibraryAsset, deleteLibraryFolder, deriveLibraryMedia, estimateH3ReferenceCanvas, listDerivedMedia, listLibraryFolders, mediaDisplayOrientation, remoteAssetToLibraryItem, saveDerivedMedia, updateDerivedMedia, updateJobResult, updateLibraryAsset, type DerivedMedia, type LibraryAsset, type LibraryFolder, type MediaDeriveOptions, type MediaDeriveRequest, type MediaDeriveSource } from "./studio-library";
import { H3_REFERENCE_PROMPT_TEMPLATE, hasPromptForOutput, promptForOutput, promptModePayload } from "./studio-prompt";
import PromptMentionComposer, { type PromptMentionItem } from "./prompt-mentions";
import {
  buildVideoDirectorContract,
  graphRoleForVideoAsset,
  videoDirectorReferenceLabels,
  videoDirectorPayload,
  VIDEO_DIRECTOR_MODE_LABELS,
  type VideoDirectorMode,
  type VideoModeAsset,
} from "./studio-video-mode";
import { VideoDirectorControls } from "./studio-video-mode-controls";
import VideoTimeline from "./video-timeline";
import { H3_GENERATION_FPS, H3_MAX_GENERATION_DURATION, H3_MAX_GENERATION_FRAMES } from "./video-project";
import { CANVAS_DOCUMENT_VERSION, H3_REFERENCE_BUDGET, LEGACY_STORAGE_KEYS as DOCUMENT_LEGACY_STORAGE_KEYS, V7_STORAGE_KEY, createCanvasNode, parseCanvasDocument, serializeCanvasDocument, type CanvasDocumentV7, type CanvasNode, type ImageGeneratorNode, type NodeResult, type VideoGeneratorNode } from "./studio-document";
import { buildGeneratorExecutionPlan, buildOutputCollectionPlan, compilePromptDocument, connectMedia, disconnectMedia, invalidateDownstreamGenerators } from "./studio-graph";
import { CANVAS_WORKSPACE_BACKUP_KEY, CANVAS_WORKSPACE_STORAGE_KEY, addCanvasWorkspaceTab, commitCanvasWorkspaceStorage, createCanvasWorkspace, parseCanvasWorkspace, removeCanvasWorkspaceTab, serializeCanvasWorkspace, updateCanvasWorkspaceDocument, type CanvasWorkspaceV1 } from "./studio-workspace";
import { assetPayloadFromStudio, studioAssetFromDocument } from "./studio-asset-roundtrip";
import { resolveResultPreviewTarget } from "./result-preview";
import { normalizeUiLanguage, UiLocalizer, UI_LANGUAGE_STORAGE_KEY, type UiLanguage } from "./ui-language";

type NodeKind = "asset" | "video" | "image" | "output";
type MediaKind = "image" | "video" | "audio";
type ImageRole = "first_frame" | "last_frame" | "identity" | "style" | "composition" | "reference" | "init_image" | "image_edit";
type VideoRole = "motion" | "camera" | "pacing";
type AudioRole = "voice" | "music" | "rhythm";
type AssetRole = ImageRole | VideoRole | AudioRole;
type XY = { x: number; y: number };
type PortSide = "in" | "out";
type NodePortAnchors = Record<string, Partial<Record<PortSide, XY>>>;
type CanvasViewport = { x: number; y: number; zoom: number };
type Asset = {
  media: MediaKind; fileName: string; localUrl: string; file?: File; remoteId?: string;
  derivationId?: string; sourceJobId?: string; thumbnailUrl?: string;
  uploadState: "uploading" | "ready" | "error"; role: AssetRole; restored?: boolean;
  mediaMeta?: { duration?: number; has_audio?: boolean; fps?: number; reference_fps?: number; width?: number; height?: number; rotation?: number };
  voiceSpeaker?: string; voiceSubject?: number;
};
type StudioNode = { id: string; kind: NodeKind; title: string; position: XY; asset?: Asset };
type Edge = { id: string; source: string; target: string; role: string; data: { role: string; include_audio?: boolean; reference_index?: number; binding_id?: string; paired_audio_binding_id?: string } };
type VideoParams = { aspectRatio: "16:9" | "9:16"; duration: number; steps: number; loraStrength: number; denoise: number; seed: number; directorMode: VideoDirectorMode; sourceVideoId: string };
type ImageParams = { aspectRatio: ImageAspectRatio; quality: ImageQuality; steps: number; cfg: number; loraStrength: number; denoise: number; seed: number; negativePrompt: string };
type PromptParts = { subject: string; action: string; scene: string; camera: string; light: string; style: string; dialogue: string; sound: string; music: string };
type GeneratorRuntime = {
  kind: "video" | "image";
  prompt: string;
  videoParams?: VideoParams;
  imageParams?: ImageParams;
  profileId: string;
  job: Job;
  resultVersions: NodeResult[];
  configRevision: number;
  lastSuccessfulRevision?: number;
};
type CoreNodeKind = Exclude<NodeKind, "asset">;
type CanvasContextMenuState =
  | { kind: "canvas"; screenPosition: XY; canvasPosition: XY }
  | { kind: "node"; screenPosition: XY; nodeId: string };
const BASE_NODES: StudioNode[] = [
  { id: "video", kind: "video", title: "H3 Video", position: { x: 90, y: 66 } },
  { id: "image", kind: "image", title: "Image Generation", position: { x: 720, y: 66 } },
  { id: "output", kind: "output", title: "Output", position: { x: 720, y: 650 } },
];
const DEFAULT_EDGES: Edge[] = [
  { id: "video-output", source: "video", target: "output", role: "output", data: { role: "output" } },
  { id: "image-output", source: "image", target: "output", role: "output", data: { role: "output" } },
];
const NODE_SIZE: Record<NodeKind, { w: number; h: number }> = {
  asset: { w: 248, h: 284 }, video: { w: 590, h: 760 }, image: { w: 590, h: 760 }, output: { w: 326, h: 306 },
};
// Reference roles are an internal workflow detail. The canvas preserves
// connected media roles for workflow compilation and serializes them explicitly.
const DEFAULT_ROLE: Record<MediaKind, AssetRole> = { image: "reference", video: "reference", audio: "reference" };
const STORAGE_KEY = V7_STORAGE_KEY;
const LEGACY_STORAGE_KEYS = DOCUMENT_LEGACY_STORAGE_KEYS;
const EMPTY_PARTS: PromptParts = { subject: "", action: "", scene: "", camera: "", light: "", style: "", dialogue: "", sound: "", music: "" };
const H3_DURATION_OPTIONS = Array.from({ length: 15 }, (_, index) => (124 + index * 17) / H3_GENERATION_FPS);
const DEFAULT_VIDEO_PARAMS: VideoParams = { aspectRatio: "16:9", duration: H3_DURATION_OPTIONS[0], steps: 4, loraStrength: 0.75, denoise: 1, seed: -1, directorMode: "auto", sourceVideoId: "" };
const DEFAULT_IMAGE_PARAMS: ImageParams = { aspectRatio: "16:9", quality: "1K", steps: 24, cfg: 7, loraStrength: 1, denoise: 0.65, seed: -1, negativePrompt: "low quality, blurry, distorted anatomy, watermark, text" };
const IDLE_JOB: Job = { status: "idle", progress: 0, message: "准备就绪" };
function defaultGeneratorRuntime(kind: "video" | "image"): GeneratorRuntime {
  return kind === "video"
    ? { kind, prompt: "", videoParams: { ...DEFAULT_VIDEO_PARAMS }, profileId: "turbo4", job: { ...IDLE_JOB }, resultVersions: [], configRevision: 0 }
    : { kind, prompt: "", imageParams: { ...DEFAULT_IMAGE_PARAMS }, profileId: "auto", job: { ...IDLE_JOB }, resultVersions: [], configRevision: 0 };
}
function applyStateAction<T>(current: T, action: SetStateAction<T>): T {
  return typeof action === "function" ? (action as (value: T) => T)(current) : action;
}
const CREATABLE_NODE_KINDS: CoreNodeKind[] = ["video", "image", "output"];
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 2;
const RESULT_PAGE_SIZE = 6;
const NON_DRAGGABLE_SELECTOR = "button,input,textarea,select,a,video,audio,label,summary,[contenteditable='true'],[data-no-drag]";

type CanvasExtent = { left: number; top: number; right: number; bottom: number; width: number; height: number };

function canvasOverviewExtent(nodes: StudioNode[], viewport: CanvasViewport, viewportSize: { width: number; height: number }): CanvasExtent {
  const zoom = Math.max(MIN_ZOOM, viewport.zoom);
  const visibleLeft = -viewport.x / zoom;
  const visibleTop = -viewport.y / zoom;
  const visibleRight = (viewportSize.width - viewport.x) / zoom;
  const visibleBottom = (viewportSize.height - viewport.y) / zoom;
  const contentLeft = nodes.length ? Math.min(...nodes.map((node) => node.position.x)) : visibleLeft;
  const contentTop = nodes.length ? Math.min(...nodes.map((node) => node.position.y)) : visibleTop;
  const contentRight = nodes.length ? Math.max(...nodes.map((node) => node.position.x + NODE_SIZE[node.kind].w)) : visibleRight;
  const contentBottom = nodes.length ? Math.max(...nodes.map((node) => node.position.y + NODE_SIZE[node.kind].h)) : visibleBottom;
  const rawLeft = Math.min(visibleLeft, contentLeft);
  const rawTop = Math.min(visibleTop, contentTop);
  const rawRight = Math.max(visibleRight, contentRight);
  const rawBottom = Math.max(visibleBottom, contentBottom);
  const padding = Math.max(120, Math.min(600, Math.max(rawRight - rawLeft, rawBottom - rawTop) * 0.08));
  const left = rawLeft - padding;
  const top = rawTop - padding;
  const right = rawRight + padding;
  const bottom = rawBottom + padding;
  return { left, top, right, bottom, width: Math.max(1, right - left), height: Math.max(1, bottom - top) };
}

function mediaType(file: File): MediaKind | null {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  return null;
}
function endpoint(node: StudioNode, side: PortSide, anchors?: NodePortAnchors) {
  const measured = anchors?.[node.id]?.[side];
  if (measured) return { x: node.position.x + measured.x, y: node.position.y + measured.y };
  const size = NODE_SIZE[node.kind];
  return { x: node.position.x + (side === "out" ? size.w : 0), y: node.position.y + size.h / 2 };
}
function portAnchor(element: HTMLElement, side: PortSide): XY | undefined {
  const port = element.querySelector<HTMLElement>(side === "out" ? ".output-port" : ".input-port");
  return port ? { x: port.offsetLeft + port.offsetWidth / 2, y: port.offsetTop + port.offsetHeight / 2 } : undefined;
}
function samePortAnchors(left: NodePortAnchors, right: NodePortAnchors): boolean {
  const ids = Object.keys(left);
  if (ids.length !== Object.keys(right).length) return false;
  return ids.every((id) => (["in", "out"] as const).every((side) => {
    const a = left[id]?.[side];
    const b = right[id]?.[side];
    return a === b || Boolean(a && b && a.x === b.x && a.y === b.y);
  }));
}
function curve(a: XY, b: XY) {
  const bend = Math.max(70, Math.abs(b.x - a.x) * 0.45);
  return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`;
}
function referenceIncludesAudio(edges: Edge[], targetNodeId: string, sourceNodeId: string): boolean {
  return Boolean(edges.find((edge) => edge.target === targetNodeId && edge.source === sourceNodeId)?.data.include_audio);
}
function referenceRoleForTarget(edges: Edge[], targetNodeId: string, sourceNodeId: string, fallback: string = "reference"): string {
  return edges.find((edge) => edge.target === targetNodeId && edge.source === sourceNodeId)?.data.role ?? fallback;
}
function canvasPointFromClient(canvas: HTMLElement, clientX: number, clientY: number): XY {
  const rect = canvas.getBoundingClientRect();
  const scaleX = rect.width / (canvas.offsetWidth || rect.width) || 1;
  const scaleY = rect.height / (canvas.offsetHeight || rect.height) || 1;
  return { x: (clientX - rect.left) / scaleX, y: (clientY - rect.top) / scaleY };
}
function Icon({ children }: { children: React.ReactNode }) { return <span className="icon" aria-hidden="true">{children}</span>; }
function clampProgress(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, number <= 1 ? number * 100 : number));
}
function snapshotText(parameters: Record<string, unknown> | undefined) {
  if (!parameters) return undefined;
  const profile = parameters.profile_id ? `${parameters.profile_id}@${parameters.profile_version ?? "?"}` : "auto";
  const size = parameters.width && parameters.height ? `${parameters.width}×${parameters.height}` : "";
  const duration = parameters.duration_actual ? ` · ${parameters.duration_actual}s/${parameters.frames}f` : "";
  return `${profile} · ${size}${duration} · seed ${parameters.seed ?? "?"}`;
}
function serverParameters(value: Record<string, unknown> | undefined): GenerationParameters | undefined {
  return value ? value as GenerationParameters : undefined;
}
function workflowReceiptFields(value: { workflow_sha256?: unknown; workflow_evidence?: unknown }): Pick<Job, "workflowSha256" | "workflowEvidence"> {
  return {
    ...(typeof value.workflow_sha256 === "string" ? { workflowSha256: value.workflow_sha256 } : {}),
    ...(value.workflow_evidence && typeof value.workflow_evidence === "object" ? { workflowEvidence: value.workflow_evidence as Record<string, unknown> } : {}),
  };
}
function effectiveDuration(seconds: number) { return Math.min(H3_MAX_GENERATION_FRAMES, Math.ceil((seconds * H3_GENERATION_FPS - 5) / 17) * 17 + 5) / H3_GENERATION_FPS; }
function isFlux2Profile(profile?: ProfileCapability) { return Boolean(profile && /flux[_-]?2|klein/i.test(`${profile.compiler} ${profile.id}`)); }
function isZImageProfile(profile?: ProfileCapability) { return Boolean(profile && /z[_-]?image/i.test(`${profile.compiler} ${profile.id}`)); }
function isZImageEditProfile(profile: ProfileCapability) { return /z[_-]?image[^\n]*edit|z-image[^\n]*edit/i.test(`${profile.compiler} ${profile.id} ${profile.display_name}`); }
function isZImageEditStatus(profile: UnavailableProfileCapability) { return /z[_-]?image[^\n]*edit|z-image[^\n]*edit/i.test(`${profile.id} ${profile.display_name}`); }
function imageProfileMode(profile: ProfileCapability) {
  const policy = imageReferencePolicy(profile);
  if (policy.min === 0 && policy.max === 0) return "文生图";
  if (policy.min === 1 && policy.max === 1) return isZImageProfile(profile) && profileSupportsParameter(profile, "denoise") ? "单图 latent img2img" : "单图 img2img";
  return `${policy.min}..${policy.max} 张图片参考`;
}
function createsCycle(edges: Edge[], source: string, target: string) {
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) adjacency.set(edge.source, [...(adjacency.get(edge.source) ?? []), edge.target]);
  adjacency.set(source, [...(adjacency.get(source) ?? []), target]);
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const current = stack.pop()!;
    if (current === source) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(adjacency.get(current) ?? []));
  }
  return false;
}
function dedupeEdges(edges: Edge[]) {
  const pairs = new Set<string>();
  const unique = edges.filter((edge) => {
    const pair = `${edge.source}\u0000${edge.target}`;
    if (pairs.has(pair)) return false;
    pairs.add(pair);
    return true;
  });
  return unique.length === edges.length ? edges : unique;
}
function isEditableEventTarget(target: EventTarget | null) {
  return target instanceof Element && Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
}
function dataTransferContainsFiles(dataTransfer: DataTransfer) {
  return Array.from(dataTransfer.types).includes("Files");
}
function canvasTabElementId(canvasId: string) { return `canvas-tab-${canvasId}`; }

function studioJobFromGenerator(node: VideoGeneratorNode | ImageGeneratorNode): Job {
  const receipt = node.job.receipt ?? {};
  const latest = node.resultVersions[0];
  return rebaseStudioJobMedia({
    ...(node.job.id ? { id: node.job.id } : {}),
    status: node.job.status === "cancelled" || node.job.status === "unknown" ? "failed" : node.job.status,
    progress: node.job.progress,
    message: node.job.message,
    media: node.kind === "video-generator" ? "video" : "image",
    ...(latest?.previewUrl ? { previewUrl: latest.previewUrl } : {}),
    ...(latest?.thumbnailUrl ? { thumbnailUrl: latest.thumbnailUrl } : {}),
    ...(latest?.downloadUrl ? { downloadUrl: latest.downloadUrl } : {}),
    ...(latest?.createdAt ? { createdAt: new Date(latest.createdAt).toISOString() } : {}),
    ...(Object.keys(receipt).length ? { parameters: receipt as GenerationParameters } : {}),
  });
}

function rebaseNodeResults(results: NodeResult[], jobId?: string): NodeResult[] {
  return results.map((result, index) => ({
    ...result,
    previewUrl: currentOriginApiUrl(result.previewUrl, jobId ? `/api/preview?id=${encodeURIComponent(jobId)}&index=${index}` : undefined) ?? result.previewUrl,
    thumbnailUrl: currentOriginApiUrl(result.thumbnailUrl, jobId ? `/api/jobs/${encodeURIComponent(jobId)}/thumbnail?index=${index}` : undefined) ?? result.thumbnailUrl,
    downloadUrl: currentOriginApiUrl(result.downloadUrl, jobId ? `/api/download?id=${encodeURIComponent(jobId)}&index=${index}` : undefined) ?? result.downloadUrl,
  }));
}

function runtimePrompt(node: VideoGeneratorNode | ImageGeneratorNode, document: CanvasDocumentV7): string {
  const bindings = new Map(node.bindings.map((binding) => [binding.id, binding]));
  const nodes = new Map(document.nodes.map((item) => [item.id, item]));
  return node.prompt.tokens.map((token) => {
    if (token.kind === "text") return token.text;
    const binding = bindings.get(token.bindingId);
    const source = binding ? nodes.get(binding.sourceNodeId) : undefined;
    if (source?.kind === "asset" && source.asset.remoteId) return `@{${source.asset.remoteId}}`;
    if (source?.kind === "image-generator" || source?.kind === "video-generator") return `@{${source.id}}`;
    return token.label;
  }).join("");
}

function runtimesFromDocument(document: CanvasDocumentV7): Record<string, GeneratorRuntime> {
  const documentNodes = new Map(document.nodes.map((node) => [node.id, node]));
  const runtimes: Record<string, GeneratorRuntime> = {};
  for (const node of document.nodes) {
    if (node.kind === "video-generator") runtimes[node.id] = {
      kind: "video" as const,
      prompt: runtimePrompt(node, document),
      videoParams: {
        aspectRatio: node.config.aspectRatio,
        duration: node.config.durationFrames / H3_GENERATION_FPS,
        steps: node.config.steps,
        loraStrength: node.config.loraStrength,
        denoise: node.config.denoise,
        seed: node.config.seed,
        directorMode: node.config.directorMode,
        sourceVideoId: (() => { const binding = node.bindings.find((item) => item.id === node.config.sourceBindingId); const source = binding ? documentNodes.get(binding.sourceNodeId) : undefined; return source?.kind === "asset" ? source.asset.remoteId ?? "" : ""; })(),
      },
      profileId: node.config.samplingPreset,
      job: studioJobFromGenerator(node),
      resultVersions: rebaseNodeResults(node.resultVersions, node.job.id),
      configRevision: node.configRevision,
      lastSuccessfulRevision: node.lastSuccessfulRevision,
    };
    if (node.kind === "image-generator") runtimes[node.id] = {
      kind: "image" as const,
      prompt: runtimePrompt(node, document),
      imageParams: {
        aspectRatio: node.config.aspectRatio,
        quality: node.config.quality,
        steps: node.config.steps,
        cfg: node.config.cfg,
        loraStrength: node.config.loraStrength,
        denoise: node.config.denoise,
        seed: node.config.seed,
        negativePrompt: node.config.negativePrompt,
      },
      profileId: node.config.profileId,
      job: studioJobFromGenerator(node),
      resultVersions: rebaseNodeResults(node.resultVersions, node.job.id),
      configRevision: node.configRevision,
      lastSuccessfulRevision: node.lastSuccessfulRevision,
    };
  }
  return runtimes;
}

function legacyNodesFromDocument(document: CanvasDocumentV7): StudioNode[] {
  return document.nodes.map((node) => {
    const kind: NodeKind = node.kind === "video-generator" ? "video" : node.kind === "image-generator" ? "image" : node.kind;
    if (node.kind !== "asset") return { id: node.id, kind, title: node.title, position: node.position };
    const restoredAsset = studioAssetFromDocument(node, document, DEFAULT_ROLE[node.mediaKind]);
    return {
      id: node.id,
      kind: "asset",
      title: node.title,
      position: node.position,
      asset: {
        ...restoredAsset,
        role: restoredAsset.role as AssetRole,
        restored: true,
        mediaMeta: restoredAsset.mediaMeta as Asset["mediaMeta"],
      },
    };
  });
}

function legacyEdgesFromDocument(document: CanvasDocumentV7): Edge[] {
  const bindingByTargetHandle = new Map<string, { id: string; role: string; includeAudio?: boolean; slot: number; paired?: boolean; pairedBindingId?: string }>();
  for (const node of document.nodes) {
    if (node.kind !== "video-generator" && node.kind !== "image-generator") continue;
    for (const binding of node.bindings) bindingByTargetHandle.set(`${node.id}\u0000${binding.kind}:${binding.slot}`, { id: binding.id, role: binding.role, includeAudio: Boolean(binding.pairedBindingId), slot: binding.slot - 1, paired: binding.kind === "audio" && Boolean(binding.pairedBindingId), pairedBindingId: binding.pairedBindingId });
  }
  return document.edges.flatMap((edge) => {
    const binding = bindingByTargetHandle.get(`${edge.targetNodeId}\u0000${edge.targetHandle}`);
    if (binding?.paired) return [];
    const role = binding?.role ?? "output";
    return [{ id: edge.id, source: edge.sourceNodeId, target: edge.targetNodeId, role, data: { role, ...(binding ? { reference_index: binding.slot, include_audio: binding.includeAudio, binding_id: binding.id, ...(binding.pairedBindingId ? { paired_audio_binding_id: binding.pairedBindingId } : {}) } : {}) } }];
  });
}

function promptDocumentFromRuntime(prompt: string, bindings: Array<{ id: string; sourceNodeId: string }>, nodes: StudioNode[]) {
  const bindingByReferenceId = new Map(bindings.flatMap((binding) => {
    const source = nodes.find((node) => node.id === binding.sourceNodeId);
    const keys = [source?.asset?.remoteId, source?.kind === "image" || source?.kind === "video" ? source.id : undefined].filter((value): value is string => Boolean(value));
    return keys.map((key) => [key, binding] as const);
  }));
  const tokens: Array<{ kind: "text"; text: string } | { kind: "mention"; bindingId: string; label: string }> = [];
  let cursor = 0;
  for (const match of prompt.matchAll(/@\{([^}]+)\}/g)) {
    const index = match.index ?? 0;
    if (index > cursor) tokens.push({ kind: "text", text: prompt.slice(cursor, index) });
    const binding = bindingByReferenceId.get(match[1]);
    tokens.push(binding ? { kind: "mention", bindingId: binding.id, label: match[0] } : { kind: "text", text: match[0] });
    cursor = index + match[0].length;
  }
  if (cursor < prompt.length || !tokens.length) tokens.push({ kind: "text", text: prompt.slice(cursor) });
  return { tokens };
}

function canvasDocumentFromState(nodes: StudioNode[], edges: Edge[], runtimes: Record<string, GeneratorRuntime>, viewport: CanvasViewport): CanvasDocumentV7 {
  const canvasNodes: CanvasNode[] = nodes.map((node) => {
    const created = createCanvasNode(node.kind === "video" ? "video-generator" : node.kind === "image" ? "image-generator" : node.kind, node.position, () => node.id);
    if (created.kind === "asset" && node.asset) return {
      ...created,
      title: node.title,
      mediaKind: node.asset.media,
      asset: assetPayloadFromStudio({ ...node.asset, mediaMeta: node.asset.mediaMeta as Record<string, unknown> | undefined }),
    };
    if (created.kind === "video-generator") {
      const runtime = runtimes[node.id] ?? defaultGeneratorRuntime("video");
      const config = runtime.videoParams ?? DEFAULT_VIDEO_PARAMS;
      const incoming = edges.filter((edge) => edge.target === node.id).map((edge) => ({ edge, source: nodes.find((item) => item.id === edge.source) })).filter((item) => item.source?.asset || item.source?.kind === "video" || item.source?.kind === "image");
      const counters: Record<MediaKind, number> = { image: 0, video: 0, audio: 0 };
      const bindings = incoming.flatMap(({ edge, source }) => {
        const kind: MediaKind = source!.asset?.media ?? (source!.kind === "image" ? "image" : "video");
        const slot = edge.data.reference_index !== undefined ? edge.data.reference_index + 1 : ++counters[kind]; counters[kind] = Math.max(counters[kind], slot);
        const bindingId = edge.data.binding_id ?? `binding-${edge.id}`;
        const pairedAudioBindingId = edge.data.paired_audio_binding_id ?? `audio-${edge.id}`;
        const primary = { id: bindingId, kind, slot, sourceNodeId: source!.id, sourceOutputHandle: source!.asset ? "media" : kind, role: edge.data.role, ...(edge.data.include_audio ? { pairedBindingId: pairedAudioBindingId } : {}) };
        if (!edge.data.include_audio || kind !== "video") return [primary];
        const audioSlot = ++counters.audio;
        const audio = { id: pairedAudioBindingId, kind: "audio" as const, slot: audioSlot, sourceNodeId: source!.id, sourceOutputHandle: "audio", role: "reference_audio", pairedBindingId: primary.id };
        return [{ ...primary, pairedBindingId: audio.id }, audio];
      });
      const sourceBindingId = bindings.find((binding) => { const source = nodes.find((item) => item.id === binding.sourceNodeId); return source?.asset?.remoteId === config.sourceVideoId; })?.id;
      return { ...created, title: node.title, prompt: promptDocumentFromRuntime(runtime.prompt, bindings, nodes), bindings, job: { id: runtime.job.id, status: runtime.job.status, progress: runtime.job.progress, message: runtime.job.message, receipt: runtime.job.parameters as Record<string, unknown> | undefined }, resultVersions: runtime.resultVersions, configRevision: runtime.configRevision, lastSuccessfulRevision: runtime.lastSuccessfulRevision, config: { directorMode: config.directorMode, samplingPreset: runtime.profileId === "base20" || /base/i.test(runtime.profileId) ? "base20" : "turbo4", aspectRatio: config.aspectRatio, durationFrames: Math.round(config.duration * H3_GENERATION_FPS), steps: config.steps, loraStrength: config.loraStrength, denoise: config.denoise, seed: config.seed, sourceBindingId } };
    }
    if (created.kind === "image-generator") {
      const runtime = runtimes[node.id] ?? defaultGeneratorRuntime("image");
      const config = runtime.imageParams ?? DEFAULT_IMAGE_PARAMS;
      const incoming = edges.filter((edge) => edge.target === node.id).map((edge) => ({ edge, source: nodes.find((item) => item.id === edge.source) })).filter((item) => item.source?.asset?.media === "image" || item.source?.kind === "image");
      const bindings = incoming.slice(0, 6).map(({ edge, source }, index) => ({ id: edge.data.binding_id ?? `binding-${edge.id}`, kind: "image" as const, slot: edge.data.reference_index !== undefined ? edge.data.reference_index + 1 : index + 1, sourceNodeId: source!.id, sourceOutputHandle: source!.asset ? "media" : "image", role: edge.data.role }));
      return { ...created, title: node.title, prompt: promptDocumentFromRuntime(runtime.prompt, bindings, nodes), bindings, job: { id: runtime.job.id, status: runtime.job.status, progress: runtime.job.progress, message: runtime.job.message, receipt: runtime.job.parameters as Record<string, unknown> | undefined }, resultVersions: runtime.resultVersions, configRevision: runtime.configRevision, lastSuccessfulRevision: runtime.lastSuccessfulRevision, config: { profileId: runtime.profileId, ...config } };
    }
    return { ...created, title: node.title, resultVersions: [] };
  });
  const bindingEdges = canvasNodes.flatMap((target) => target.kind === "video-generator" || target.kind === "image-generator" ? target.bindings.map((binding, order) => ({ id: `edge-${binding.id}`, sourceNodeId: binding.sourceNodeId, sourceHandle: binding.sourceOutputHandle, targetNodeId: target.id, targetHandle: `${binding.kind}:${binding.slot}`, order })) : []);
  const outputEdges = edges.filter((edge) => canvasNodes.find((node) => node.id === edge.target)?.kind === "output").map((edge, order) => ({ id: edge.id, sourceNodeId: edge.source, sourceHandle: nodes.find((node) => node.id === edge.source)?.kind === "image" ? "image" : "video", targetNodeId: edge.target, targetHandle: "result", order }));
  const canvasEdges = [...bindingEdges, ...outputEdges];
  return { version: CANVAS_DOCUMENT_VERSION, viewport, nodes: canvasNodes, edges: canvasEdges, groups: [], unassignedResults: [], migration: { issues: [] } };
}

function createFreshStudioCanvasDocument(): CanvasDocumentV7 {
  const video = createCanvasNode("video-generator", { x: 90, y: 66 }) as VideoGeneratorNode;
  const image = createCanvasNode("image-generator", { x: 760, y: 66 }) as ImageGeneratorNode;
  const output = createCanvasNode("output", { x: 760, y: 880 });
  return {
    version: CANVAS_DOCUMENT_VERSION,
    viewport: { x: 32, y: 32, zoom: 1 },
    nodes: [video, image, output],
    edges: [
      { id: crypto.randomUUID(), sourceNodeId: video.id, sourceHandle: "video", targetNodeId: output.id, targetHandle: "result", order: 0 },
      { id: crypto.randomUUID(), sourceNodeId: image.id, sourceHandle: "image", targetNodeId: output.id, targetHandle: "result", order: 1 },
    ],
    groups: [],
    unassignedResults: [],
    migration: { issues: [] },
  };
}

export default function Studio() {
  const [uiLanguage, setUiLanguage] = useState<UiLanguage>("en");
  const [nodes, setNodes] = useState<StudioNode[]>(BASE_NODES);
  const [edges, setEdges] = useState<Edge[]>(DEFAULT_EDGES);
  const [selectedId, setSelectedId] = useState("video");
  const [connecting, setConnecting] = useState<string>();
  const [connectionPointer, setConnectionPointer] = useState<XY>();
  const [generatorStates, setGeneratorStates] = useState<Record<string, GeneratorRuntime>>({ video: defaultGeneratorRuntime("video"), image: defaultGeneratorRuntime("image") });
  const [jobHistory, setJobHistory] = useState<Job[]>([]);
  const [jobHistoryState, setJobHistoryState] = useState<"loading" | "refreshing" | "ready" | "error">("loading");
  const [jobHistoryInstanceVerified, setJobHistoryInstanceVerified] = useState(false);
  const [jobHistoryError, setJobHistoryError] = useState("");
  const [jobHistoryPageError, setJobHistoryPageError] = useState("");
  const [derivedResults, setDerivedResults] = useState<DerivedMedia[]>([]);
  const [assetLibrary, setAssetLibrary] = useState<LibraryAsset[]>([]);
  const [assetLibraryState, setAssetLibraryState] = useState<"loading" | "ready" | "error">("loading");
  const [assetFolders, setAssetFolders] = useState<LibraryFolder[]>([]);
  const [savedResultAssets, setSavedResultAssets] = useState<Record<string, string>>({});
  const [railPanel, setRailPanel] = useState<"assets" | "results" | "timeline" | null>(null);
  const [profiles, setProfiles] = useState<ProfileCapability[]>([]);
  const [unavailableProfiles, setUnavailableProfiles] = useState<UnavailableProfileCapability[]>([]);
  const [assetPickerTarget, setAssetPickerTarget] = useState<{ nodeId: string; media: MediaKind; slot: number }>();
  const [compileByNode, setCompileByNode] = useState<Record<string, { prompt: string; state: "idle" | "loading" | "ready" | "error"; error: string }>>({});
  const [compileRetryToken, setCompileRetryToken] = useState(0);
  const [notice, setNotice] = useState("工作流保存在本浏览器；已上传素材可由远程 ID 恢复。");
  const [dragOver, setDragOver] = useState(false);
  const [contextMenu, setContextMenu] = useState<CanvasContextMenuState | null>(null);
  const [viewport, setViewport] = useState<CanvasViewport>({ x: 32, y: 32, zoom: 1 });
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [nodePortAnchors, setNodePortAnchors] = useState<NodePortAnchors>({});
  const [overviewGestureExtent, setOverviewGestureExtent] = useState<CanvasExtent>();
  const [canvasWorkspace, setCanvasWorkspace] = useState<CanvasWorkspaceV1>();
  const [workflowHydrated, setWorkflowHydrated] = useState(false);
  const canvasRef = useRef<HTMLDivElement>(null);
  const canvasViewportRef = useRef<HTMLDivElement>(null);
  const canvasOverviewRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dragRef = useRef<{ id: string; dx: number; dy: number; pointerId: number; captureTarget: HTMLElement } | undefined>(undefined);
  const panRef = useRef<{ clientX: number; clientY: number; originX: number; originY: number; pointerId: number; captureTarget: HTMLElement } | undefined>(undefined);
  const overviewPointerRef = useRef<number | undefined>(undefined);
  const overviewGestureExtentRef = useRef<CanvasExtent | undefined>(undefined);
  const spacePressedRef = useRef(false);
  const lastCanvasPointerRef = useRef<XY>({ x: 120, y: 120 });
  const dragPointerRef = useRef<{ clientX: number; clientY: number; connecting?: string } | undefined>(undefined);
  const dragFrameRef = useRef<number | undefined>(undefined);
  const assetUrlsRef = useRef(new Set<string>());
  const uploadControllersRef = useRef(new Map<string, AbortController>());
  const pollFailuresRef = useRef(0);
  const orchestratedJobsRef = useRef(new Set<string>());
  const autoProfileRef = useRef<Record<string, string | undefined>>({});
  const jobHistoryRequestRef = useRef<Promise<number> | undefined>(undefined);
  const jobHistoryPageRequestRef = useRef<Promise<number> | undefined>(undefined);
  const jobHistoryPendingRefreshRef = useRef(false);
  const jobHistoryLoadedAtRef = useRef(0);
  const jobHistoryInstanceRef = useRef<string | undefined>(undefined);
  const jobHistoryLegacyCacheRef = useRef(false);
  const jobHistoryPendingCacheRef = useRef<ReturnType<typeof parseJobHistoryCacheEnvelope>>({ jobs: [] });
  const jobHistoryPaginationInitializedRef = useRef(false);
  const jobHistoryCacheReadyRef = useRef(false);
  const jobHistoryCursorRef = useRef<string | undefined>(undefined);
  const trustedJobIdsRef = useRef(new Set<string>());
  const deletedJobIdsRef = useRef(new Set<string>());
  const [jobHistoryCursor, setJobHistoryCursor] = useState<string>();
  const workflowSnapshotRef = useRef<CanvasDocumentV7 | undefined>(undefined);
  const canvasWorkspaceRef = useRef<CanvasWorkspaceV1 | undefined>(undefined);
  const activeCanvasIdRef = useRef<string | undefined>(undefined);
  const generatorStatesRef = useRef(generatorStates);
  const workflowSaveTimerRef = useRef<number | undefined>(undefined);
  const canvasInteractionActiveRef = useRef(false);
  const contextMenuInvokerRef = useRef<HTMLElement | null>(null);
  const promptCompileRequestRef = useRef(0);
  const timelineRailButtonRef = useRef<HTMLButtonElement>(null);
  const previousRailPanelRef = useRef<typeof railPanel>(null);
  const uiRootRef = useRef<HTMLElement>(null);
  const uiLocalizerRef = useRef<UiLocalizer | undefined>(undefined);

  useLayoutEffect(() => {
    const root = uiRootRef.current;
    if (!root) return;
    let storedLanguage: UiLanguage = "en";
    try {
      storedLanguage = normalizeUiLanguage(window.localStorage.getItem(UI_LANGUAGE_STORAGE_KEY));
    } catch {
      // Storage may be unavailable in hardened/private browser contexts.
    }
    const localizer = new UiLocalizer(root);
    uiLocalizerRef.current = localizer;
    localizer.start(storedLanguage);
    document.documentElement.lang = storedLanguage;
    root.dataset.i18nReady = "true";
    setUiLanguage(storedLanguage);
    return () => {
      localizer.stop();
      uiLocalizerRef.current = undefined;
    };
  }, []);

  const toggleUiLanguage = useCallback(() => {
    const next: UiLanguage = uiLanguage === "en" ? "zh-CN" : "en";
    uiLocalizerRef.current?.setLanguage(next);
    document.documentElement.lang = next;
    try {
      window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, next);
    } catch {
      // The active choice still applies for this page when storage is blocked.
    }
    setUiLanguage(next);
  }, [uiLanguage]);

  useEffect(() => { generatorStatesRef.current = generatorStates; }, [generatorStates]);

  const closeContextMenu = useCallback((restoreFocus = true) => {
    setContextMenu(null);
    const invoker = contextMenuInvokerRef.current;
    contextMenuInvokerRef.current = null;
    if (restoreFocus && invoker) {
      window.requestAnimationFrame(() => {
        if (invoker.isConnected) invoker.focus();
      });
    }
  }, []);

  const zoomCanvas = useCallback((nextZoom: number, clientAnchor?: XY) => {
    const host = canvasViewportRef.current;
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const anchor = clientAnchor ?? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    setViewport((current) => {
      const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, nextZoom));
      const screenX = anchor.x - rect.left;
      const screenY = anchor.y - rect.top;
      const canvasX = (screenX - current.x) / current.zoom;
      const canvasY = (screenY - current.y) / current.zoom;
      return { x: screenX - canvasX * zoom, y: screenY - canvasY * zoom, zoom };
    });
  }, []);

  const fitCanvas = useCallback(() => {
    const host = canvasViewportRef.current;
    if (!host || !nodes.length) return;
    const rect = host.getBoundingClientRect();
    const padding = 72;
    const left = Math.min(...nodes.map((node) => node.position.x));
    const top = Math.min(...nodes.map((node) => node.position.y));
    const right = Math.max(...nodes.map((node) => node.position.x + NODE_SIZE[node.kind].w));
    const bottom = Math.max(...nodes.map((node) => node.position.y + NODE_SIZE[node.kind].h));
    const zoom = Math.max(MIN_ZOOM, Math.min(1, Math.min((rect.width - padding * 2) / Math.max(1, right - left), (rect.height - padding * 2) / Math.max(1, bottom - top))));
    setViewport({ x: (rect.width - (right - left) * zoom) / 2 - left * zoom, y: (rect.height - (bottom - top) * zoom) / 2 - top * zoom, zoom });
  }, [nodes]);

  const focusCanvasNode = useCallback((nodeId: string) => {
    const host = canvasViewportRef.current;
    const node = nodes.find((item) => item.id === nodeId);
    if (!host || !node) return;
    const rect = host.getBoundingClientRect();
    const size = NODE_SIZE[node.kind];
    setViewport((current) => ({
      ...current,
      x: rect.width / 2 - (node.position.x + size.w / 2) * current.zoom,
      y: rect.height / 2 - (node.position.y + Math.min(size.h, 520) / 2) * current.zoom,
    }));
  }, [nodes]);

  const overviewExtent = useMemo(() => canvasOverviewExtent(nodes, viewport, viewportSize), [nodes, viewport, viewportSize]);
  const renderedOverviewExtent = overviewGestureExtent ?? overviewExtent;
  const overviewViewport = useMemo(() => {
    const visibleLeft = -viewport.x / viewport.zoom;
    const visibleTop = -viewport.y / viewport.zoom;
    const visibleWidth = viewportSize.width / viewport.zoom;
    const visibleHeight = viewportSize.height / viewport.zoom;
    const width = Math.max(3, Math.min(100, visibleWidth / renderedOverviewExtent.width * 100));
    const height = Math.max(4, Math.min(100, visibleHeight / renderedOverviewExtent.height * 100));
    return {
      left: Math.max(0, Math.min(100 - width, (visibleLeft - renderedOverviewExtent.left) / renderedOverviewExtent.width * 100)),
      top: Math.max(0, Math.min(100 - height, (visibleTop - renderedOverviewExtent.top) / renderedOverviewExtent.height * 100)),
      width,
      height,
    };
  }, [renderedOverviewExtent, viewport, viewportSize]);

  useEffect(() => {
    const keyDown = (event: KeyboardEvent) => {
      if (event.code === "Space" && !isEditableEventTarget(event.target)) spacePressedRef.current = true;
    };
    const keyUp = (event: KeyboardEvent) => { if (event.code === "Space") spacePressedRef.current = false; };
    const blur = () => { spacePressedRef.current = false; };
    window.addEventListener("keydown", keyDown);
    window.addEventListener("keyup", keyUp);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", keyDown);
      window.removeEventListener("keyup", keyUp);
      window.removeEventListener("blur", blur);
    };
  }, []);

  useEffect(() => {
    const host = canvasViewportRef.current;
    if (!host) return;
    const updateSize = () => setViewportSize({ width: host.clientWidth, height: host.clientHeight });
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const nodeIdsKey = useMemo(() => nodes.map((node) => node.id).sort().join("\u0000"), [nodes]);
  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const nodesInCanvas = () => [...canvas.querySelectorAll<HTMLElement>(".studio-node[data-node-id]")];
    const measure = () => {
      const next: NodePortAnchors = {};
      for (const element of nodesInCanvas()) {
        const nodeId = element.dataset.nodeId;
        if (!nodeId) continue;
        const input = portAnchor(element, "in");
        const output = portAnchor(element, "out");
        next[nodeId] = { ...(input ? { in: input } : {}), ...(output ? { out: output } : {}) };
      }
      setNodePortAnchors((current) => samePortAnchors(current, next) ? current : next);
    };
    const observer = new ResizeObserver(measure);
    for (const element of nodesInCanvas()) observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, [nodeIdsKey]);

  const persistCanvasWorkspace = useCallback((workspace: CanvasWorkspaceV1, recoveryDocument?: CanvasDocumentV7): boolean => {
    const committed = commitCanvasWorkspaceStorage(localStorage, serializeCanvasWorkspace(workspace), recoveryDocument ? { key: STORAGE_KEY, value: serializeCanvasDocument(recoveryDocument) } : undefined);
    if (committed.ok) return true;
    setNotice(committed.stage === "recovery" ? "保存恢复副本失败，当前画布操作未应用。" : "保存画布失败：浏览器本地存储空间不足，当前操作未应用。");
    return false;
  }, []);

  const flushWorkflowSnapshot = useCallback((): boolean => {
    if (workflowSaveTimerRef.current !== undefined) {
      window.clearTimeout(workflowSaveTimerRef.current);
      workflowSaveTimerRef.current = undefined;
    }
    const snapshot = workflowSnapshotRef.current;
    if (!snapshot) return true;
    try {
      const workspace = canvasWorkspaceRef.current;
      const activeCanvasId = activeCanvasIdRef.current;
      if (workspace && activeCanvasId) {
        const next = updateCanvasWorkspaceDocument(workspace, activeCanvasId, snapshot);
        if (!persistCanvasWorkspace(next, snapshot)) return false;
        canvasWorkspaceRef.current = next;
      }
      // Keep the active V7 snapshot as a backwards-compatible recovery copy.
      if (!workspace || !activeCanvasId) localStorage.setItem(STORAGE_KEY, serializeCanvasDocument(snapshot));
      return true;
    } catch {
      setNotice("自动保存失败：浏览器本地存储空间不足，请先导出或清理旧数据。");
      return false;
    }
  }, [persistCanvasWorkspace]);

  const restoreCanvasState = useCallback((document: CanvasDocumentV7) => {
    if (workflowSaveTimerRef.current !== undefined) {
      window.clearTimeout(workflowSaveTimerRef.current);
      workflowSaveTimerRef.current = undefined;
    }
    workflowSnapshotRef.current = structuredClone(document);
    const nextNodes = legacyNodesFromDocument(document);
    const nextRuntimes = runtimesFromDocument(document);
    setNodes(nextNodes);
    setEdges(dedupeEdges(legacyEdgesFromDocument(document)));
    setGeneratorStates(nextRuntimes);
    generatorStatesRef.current = nextRuntimes;
    setViewport({ x: document.viewport.x, y: document.viewport.y, zoom: Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, document.viewport.zoom)) });
    setSelectedId(nextNodes.find((node) => node.kind === "video" || node.kind === "image")?.id ?? nextNodes[0]?.id ?? "");
    setCompileByNode({});
    setConnecting(undefined);
    setConnectionPointer(undefined);
    setAssetPickerTarget(undefined);
    overviewPointerRef.current = undefined;
    overviewGestureExtentRef.current = undefined;
    setOverviewGestureExtent(undefined);
    dragRef.current = undefined;
    panRef.current = undefined;
    lastCanvasPointerRef.current = nextNodes[0]?.position ?? { x: 120, y: 120 };
    closeContextMenu(false);
  }, [closeContextMenu]);

  const selected = nodes.find((node) => node.id === selectedId);
  const assets = useMemo(() => nodes.filter((node) => node.kind === "asset"), [nodes]);
  const selectedDownstreamGenerator = selected?.kind === "asset" ? nodes.find((node) => edges.some((edge) => edge.source === selected.id && edge.target === node.id) && (node.kind === "video" || node.kind === "image")) : undefined;
  const generator = selected?.kind === "image" || selectedDownstreamGenerator?.kind === "image" ? "image" : "video";
  const videoNodeId = selected?.kind === "video" ? selected.id : selectedDownstreamGenerator?.kind === "video" ? selectedDownstreamGenerator.id : nodes.find((node) => node.kind === "video")?.id ?? "video";
  const imageNodeId = selected?.kind === "image" ? selected.id : selectedDownstreamGenerator?.kind === "image" ? selectedDownstreamGenerator.id : nodes.find((node) => node.kind === "image")?.id ?? "image";
  const updateGenerator = useCallback((nodeId: string, update: (current: GeneratorRuntime) => GeneratorRuntime) => {
    setGeneratorStates((current) => {
      const kind = nodes.find((node) => node.id === nodeId)?.kind === "image" ? "image" : "video";
      const next = { ...current, [nodeId]: update(current[nodeId] ?? defaultGeneratorRuntime(kind)) };
      generatorStatesRef.current = next;
      return next;
    });
  }, [nodes]);
  const videoRuntime = generatorStates[videoNodeId] ?? defaultGeneratorRuntime("video");
  const imageRuntime = generatorStates[imageNodeId] ?? defaultGeneratorRuntime("image");
  const prompt = videoRuntime.prompt;
  const imagePrompt = imageRuntime.prompt;
  const videoParams = videoRuntime.videoParams ?? DEFAULT_VIDEO_PARAMS;
  const imageParams = imageRuntime.imageParams ?? DEFAULT_IMAGE_PARAMS;
  const profileSelection = useMemo(() => ({ video: videoRuntime.profileId, image: imageRuntime.profileId }), [imageRuntime.profileId, videoRuntime.profileId]);
  const job = (generator === "image" ? imageRuntime : videoRuntime).job;
  const setVideoParams = useCallback((action: SetStateAction<VideoParams>) => updateGenerator(videoNodeId, (current) => ({ ...current, videoParams: applyStateAction(current.videoParams ?? DEFAULT_VIDEO_PARAMS, action), configRevision: current.configRevision + 1 })), [updateGenerator, videoNodeId]);
  const setImageParams = useCallback((action: SetStateAction<ImageParams>) => updateGenerator(imageNodeId, (current) => ({ ...current, imageParams: applyStateAction(current.imageParams ?? DEFAULT_IMAGE_PARAMS, action), configRevision: current.configRevision + 1 })), [imageNodeId, updateGenerator]);
  const setJobForNode = useCallback((nodeId: string, action: SetStateAction<Job>) => updateGenerator(nodeId, (current) => ({ ...current, job: applyStateAction(current.job, action) })), [updateGenerator]);
  const recordTrustedJob = useCallback((item: Job) => {
    if (!item.id || deletedJobIdsRef.current.has(item.id)) return;
    trustedJobIdsRef.current.add(item.id);
    setJobHistory((current) => mergeJobHistory(current, rebaseStudioJobMedia(item), 100));
  }, []);
  const setJob = useCallback((action: SetStateAction<Job>) => setJobForNode(generator === "image" ? imageNodeId : videoNodeId, action), [generator, imageNodeId, setJobForNode, videoNodeId]);
  const compileNodeId = generator === "image" ? imageNodeId : videoNodeId;
  const updateCompile = useCallback((patch: Partial<{ prompt: string; state: "idle" | "loading" | "ready" | "error"; error: string }>) => setCompileByNode((current) => ({ ...current, [compileNodeId]: { ...(current[compileNodeId] ?? { prompt: "", state: "idle", error: "" }), ...patch } })), [compileNodeId]);
  const setCompiledPrompt = useCallback((value: string) => updateCompile({ prompt: value }), [updateCompile]);
  const setCompileState = useCallback((value: "idle" | "loading" | "ready" | "error") => updateCompile({ state: value }), [updateCompile]);
  const setCompileError = useCallback((value: string) => updateCompile({ error: value }), [updateCompile]);
  const connectedAssets = useMemo(() => edges.filter((edge) => edge.target === videoNodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset)), [edges, nodes, videoNodeId]);
  const videoModeAssets = useMemo<VideoModeAsset[]>(() => connectedAssets.map((node) => ({
    nodeId: node.id,
    assetId: node.asset?.remoteId,
    kind: node.asset!.media,
    label: node.asset!.fileName,
    role: referenceRoleForTarget(edges, videoNodeId, node.id, node.asset!.role),
    includeAudio: referenceIncludesAudio(edges, videoNodeId, node.id),
  })), [connectedAssets, edges, videoNodeId]);
  const videoDirectorContract = useMemo(
    () => buildVideoDirectorContract(videoParams.directorMode, videoParams.sourceVideoId, videoModeAssets),
    [videoModeAssets, videoParams.directorMode, videoParams.sourceVideoId],
  );
  const imageReferences = useMemo(() => edges
    .map((edge, insertionIndex) => ({ edge, insertionIndex, node: nodes.find((node) => node.id === edge.source) }))
    .filter((item): item is { edge: Edge; insertionIndex: number; node: StudioNode } => item.edge.target === imageNodeId && item.node?.kind === "asset" && item.node.asset?.media === "image")
    .sort((a, b) => (a.edge.data.reference_index ?? a.insertionIndex + 1) - (b.edge.data.reference_index ?? b.insertionIndex + 1))
    .map((item, index) => ({ ...item, referenceIndex: index + 1 })), [edges, imageNodeId, nodes]);
  const imageReferenceCount = imageReferences.length;
  const imageInput = imageReferences[0]?.node.asset;

  const route = videoParams.directorMode === "auto"
    ? `Auto → ${VIDEO_DIRECTOR_MODE_LABELS[videoDirectorContract.resolvedMode]}`
    : VIDEO_DIRECTOR_MODE_LABELS[videoDirectorContract.resolvedMode];
  const expectedCompiler = generator === "video" ? videoDirectorContract.compiler : imageReferenceCount ? `${imageReferenceCount}-image reference` : "Text-to-image";
  const profileChoices = useMemo(() => profiles.filter((profile) => {
    if (profile.output_type !== generator) return false;
    return generator === "image" || profile.compiler === expectedCompiler;
  }), [expectedCompiler, generator, profiles]);
  const resolveProfileFor = useCallback((output: "video" | "image") => {
    const compiler = videoDirectorContract.compiler;
    const choices = profiles.filter((profile) => profile.output_type === output && (output === "image" || profile.compiler === compiler));
    const compatible = choices.filter((profile) => output === "video" || imageProfileAcceptsReferenceCount(profile, imageReferenceCount));
    const selectedProfile = profileSelection[output];
    if (output === "video") return compatible.find((profile) => profile.available && profile.sampling_mode === (selectedProfile === "base20" ? "base" : "turbo4")) ?? compatible.find((profile) => profile.available) ?? compatible[0];
    return selectedProfile === "auto" ? compatible.find((profile) => profile.available) ?? compatible[0] : choices.find((profile) => profile.id === selectedProfile);
  }, [imageReferenceCount, profileSelection, profiles, videoDirectorContract.compiler]);
  const videoProfile = useMemo(() => resolveProfileFor("video"), [resolveProfileFor]);
  const imageProfile = useMemo(() => resolveProfileFor("image"), [resolveProfileFor]);
  const activeProfile = generator === "video" ? videoProfile : imageProfile;
  const confirmedSavedResultAssets = useMemo(() => {
    const existing = new Set(assetLibrary.map((item) => item.id));
    return Object.fromEntries(Object.entries(savedResultAssets).filter(([, assetId]) => existing.has(assetId)));
  }, [assetLibrary, savedResultAssets]);
  const announcedZImageEdit = useMemo(() => profiles.find(isZImageEditProfile), [profiles]);
  const unavailableZImageEdit = useMemo(() => unavailableProfiles.find(isZImageEditStatus), [unavailableProfiles]);
  const imageReferenceContract = useMemo(() => imageReferencePolicy(imageProfile), [imageProfile]);
  const imageReferenceBySource = useMemo(() => new Map(imageReferences.map((item) => [item.node.id, item.referenceIndex])), [imageReferences]);

  const loadAssetLibrary = useCallback(async (signal?: AbortSignal) => {
    setAssetLibraryState("loading");
    try {
      const response = await fetch("/api/assets", { cache: "no-store", signal });
      const body = await response.json() as { assets?: unknown[]; error?: { message?: string } };
      if (!response.ok) throw new Error(body.error?.message ?? `素材库读取失败 (${response.status})`);
      const items = (Array.isArray(body.assets) ? body.assets : [])
        .map(remoteAssetToLibraryItem)
        .filter((item): item is LibraryAsset => Boolean(item));
      setAssetLibrary(items);
      setAssetLibraryState("ready");
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") return;
      setAssetLibraryState("error");
      setNotice(error instanceof Error ? error.message : "素材库读取失败");
    }
  }, []);

  const loadAssetFolders = useCallback(async () => {
    try {
      setAssetFolders(await listLibraryFolders());
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "文件夹读取失败");
    }
  }, []);

  const loadJobHistory = useCallback(async (force = false, cursor?: string): Promise<number> => {
    const activeRequest = cursor ? jobHistoryPageRequestRef.current : jobHistoryRequestRef.current;
    if (activeRequest) {
      if (!cursor && force) jobHistoryPendingRefreshRef.current = true;
      return activeRequest;
    }
    if (cursor && jobHistoryRequestRef.current) {
      const expectedInstance = jobHistoryInstanceRef.current;
      const expectedCursor = cursor;
      await jobHistoryRequestRef.current;
      if (jobHistoryInstanceRef.current !== expectedInstance || jobHistoryCursorRef.current !== expectedCursor) return 0;
    }
    if (cursor && jobHistoryPageRequestRef.current) return jobHistoryPageRequestRef.current;
    if (!cursor && !force && Date.now() - jobHistoryLoadedAtRef.current < 15_000) return 0;
    if (!cursor) {
      setJobHistoryState((current) => current === "ready" || current === "refreshing" ? "refreshing" : "loading");
      setJobHistoryError("");
    } else setJobHistoryPageError("");
    const request = (async () => {
      const query = new URLSearchParams({ limit: "20", summary: "1", results: "1" });
      if (!cursor) query.set("include_pinned", "1");
      if (cursor) query.set("cursor", cursor);
      const response = await fetch(`/api/jobs?${query.toString()}`, { cache: "no-cache", signal: AbortSignal.timeout(12_000) });
      const body = await response.json() as { jobs?: Record<string, unknown>[]; pinned_jobs?: Record<string, unknown>[]; next_cursor?: string | null; instance_id?: string; error?: { message?: string } };
      if (!response.ok) throw new Error(body.error?.message ?? `任务历史读取失败 (${response.status})`);
      const listedReceipts = Array.isArray(body.jobs) ? body.jobs : [];
      const pinnedReceipts = Array.isArray(body.pinned_jobs) ? body.pinned_jobs : [];
      const pinnedIds = new Set(pinnedReceipts.map((receipt) => typeof receipt.id === "string" ? receipt.id : receipt.job_id).filter(Boolean));
      const receipts = [...pinnedReceipts, ...listedReceipts.filter((receipt) => !pinnedIds.has(typeof receipt.id === "string" ? receipt.id : receipt.job_id))];
      const restoredSavedAssets: Record<string, string> = {};
      for (const receipt of receipts) {
        const jobId = typeof receipt.id === "string" ? receipt.id : typeof receipt.job_id === "string" ? receipt.job_id : "";
        const outputs = Array.isArray(receipt.outputs) ? receipt.outputs : [];
        const firstOutput = outputs[0];
        const assetId = firstOutput && typeof firstOutput === "object" && typeof (firstOutput as Record<string, unknown>).asset_id === "string"
          ? String((firstOutput as Record<string, unknown>).asset_id)
          : "";
        if (jobId && /^[0-9a-f]{32}$/.test(assetId)) restoredSavedAssets[jobId] = assetId;
      }
      if (Object.keys(restoredSavedAssets).length) {
        setSavedResultAssets((current) => ({ ...current, ...restoredSavedAssets }));
      }
      const remoteJobs = receipts.map(serverJobToStudioJob).filter((item): item is Job => item !== undefined && !deletedJobIdsRef.current.has(item.id ?? ""));
      const nextCursor = typeof body.next_cursor === "string" && body.next_cursor ? body.next_cursor : undefined;
      const remoteInstance = typeof body.instance_id === "string" && body.instance_id ? body.instance_id : undefined;
      const instanceChanged = Boolean(remoteInstance && ((jobHistoryInstanceRef.current && remoteInstance !== jobHistoryInstanceRef.current) || jobHistoryLegacyCacheRef.current));
      if (cursor && instanceChanged) {
        jobHistoryCursorRef.current = undefined;
        setJobHistoryCursor(undefined);
        return 0;
      }
      if (instanceChanged) trustedJobIdsRef.current.clear();
      const newlySeen = remoteJobs.filter((item) => item.id && !trustedJobIdsRef.current.has(item.id)).length;
      remoteJobs.forEach((item) => { if (item.id) trustedJobIdsRef.current.add(item.id); });
      if (instanceChanged) {
        deletedJobIdsRef.current.clear();
        setSavedResultAssets(restoredSavedAssets);
        setJobHistory(remoteJobs);
      } else {
        setJobHistory((current) => {
          const pending = jobHistoryPendingCacheRef.current;
          const base = !cursor && pending.instanceId && pending.instanceId === remoteInstance ? pending.jobs : current;
          return mergeJobHistoryPage(base, remoteJobs, Boolean(cursor), Boolean(nextCursor), 100, cursor);
        });
      }
      if (instanceChanged || cursor || !jobHistoryPaginationInitializedRef.current || !nextCursor) {
        jobHistoryCursorRef.current = nextCursor;
        setJobHistoryCursor(nextCursor);
      }
      jobHistoryPaginationInitializedRef.current = true;
      if (remoteInstance) {
        jobHistoryInstanceRef.current = remoteInstance;
        jobHistoryLegacyCacheRef.current = false;
      }
      if (!cursor) {
        jobHistoryPendingCacheRef.current = { jobs: [] };
        jobHistoryCacheReadyRef.current = true;
        setJobHistoryInstanceVerified(true);
      }
      jobHistoryLoadedAtRef.current = Date.now();
      if (!cursor) setJobHistoryState("ready");
      return cursor ? newlySeen : remoteJobs.length;
    })();
    if (cursor) jobHistoryPageRequestRef.current = request;
    else jobHistoryRequestRef.current = request;
    try { return await request; }
    catch (error) {
      if (error instanceof Error && error.name !== "AbortError") {
        if (!cursor) {
          setJobHistoryState("error");
          setJobHistoryError(error.message);
        } else setJobHistoryPageError(error.message);
        setNotice(`无法恢复远程任务历史：${error.message}`);
      }
      throw error;
    }
    finally {
      if (cursor && jobHistoryPageRequestRef.current === request) jobHistoryPageRequestRef.current = undefined;
      if (!cursor && jobHistoryRequestRef.current === request) jobHistoryRequestRef.current = undefined;
      if (!cursor && jobHistoryPendingRefreshRef.current) {
        jobHistoryPendingRefreshRef.current = false;
        window.setTimeout(() => { void loadJobHistory(true).catch(() => undefined); }, 0);
      }
    }
  }, []);

  const handleTimelineResultCreated = useCallback(() => {
    void loadJobHistory(true).catch(() => undefined);
  }, [loadJobHistory]);

  const resumeResultJob = useCallback(async (source: Job, additionalSteps: number) => {
    if (!source.id) throw new Error("任务 ID 不存在");
    const receipt = await resumeGenerationJob(source.id, additionalSteps);
    const resumedId = String(receipt.job_id);
    setNotice(`已提交续跑：${source.resume?.current_steps ?? source.parameters?.steps ?? 0} + ${additionalSteps} 步`);
    await loadJobHistory(true).catch(() => 0);
    void (async () => {
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        try {
          const response = await fetch(`/api/status?id=${encodeURIComponent(resumedId)}`, { cache: "no-store" });
          const body = await response.json() as Record<string, unknown> & { error?: { message?: string } };
          if (!response.ok) throw new Error(body.error?.message ?? `续跑状态读取失败 (${response.status})`);
          const resumed = serverJobToStudioJob(body);
          if (resumed) setJobHistory((current) => mergeJobHistory(current, resumed));
          if (body.status === "completed") { setNotice(`续跑完成：当前共 ${body.current_steps ?? "?"} 步`); break; }
          if (body.status === "failed" || body.status === "canceled") { setNotice(String(body.message ?? "续跑失败")); break; }
        } catch (error) {
          setNotice(error instanceof Error ? error.message : "续跑状态读取失败");
          break;
        }
      }
      await loadJobHistory(true).catch(() => 0);
    })();
  }, [loadJobHistory]);

  const handleTimelineAssetCreated = useCallback((asset: LibraryAsset) => {
    setAssetLibrary((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
    setNotice(`已将分镜片段保存到资产：${asset.filename}`);
  }, []);

  const uploadTimelineVideo = useCallback(async (file: File): Promise<LibraryAsset> => {
    if (!file.type.startsWith("video/")) throw new Error("长视频时间线只接受视频文件。");
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/api/assets", { method: "POST", body: form });
    const data = await response.json().catch(() => ({})) as { asset?: unknown; reused?: boolean; error?: { message?: string } } & Record<string, unknown>;
    if (!response.ok) throw new Error(data.error?.message ?? `视频上传失败 (${response.status})`);
    const library = remoteAssetToLibraryItem(data.asset ?? data);
    if (!library || library.kind !== "video") throw new Error("服务端未返回有效的视频资产。");
    setAssetLibrary((current) => [library, ...current.filter((item) => item.id !== library.id)]);
    if (data.reused) setNotice(`素材已存在，已复用：${library.filename}`);
    return library;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/capabilities", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { profiles?: ProfileCapability[]; image?: { unavailable_profiles?: UnavailableProfileCapability[] }; error?: { message?: string } };
        if (!response.ok) throw new Error(data.error?.message ?? `能力检测失败 (${response.status})`);
        setProfiles(Array.isArray(data.profiles) ? data.profiles : []);
        setUnavailableProfiles(Array.isArray(data.image?.unavailable_profiles) ? data.image.unavailable_profiles.filter((profile) => profile?.available === false && typeof profile.id === "string" && typeof profile.display_name === "string") : []);
      })
      .catch((error) => { if (error instanceof Error && error.name !== "AbortError") setNotice(`无法读取模型能力：${error.message}`); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadAssetLibrary(controller.signal);
    void loadAssetFolders();
    void listDerivedMedia(controller.signal)
      .then(setDerivedResults)
      .catch((error) => { if (error instanceof Error && error.name !== "AbortError") setNotice(`无法恢复派生结果：${error.message}`); });
    return () => controller.abort();
  }, [loadAssetFolders, loadAssetLibrary]);

  useEffect(() => {
    if (!assetLibrary.length) return;
    const byId = new Map(assetLibrary.map((item) => [item.id, item]));
    setNodes((current) => {
      let changed = false;
      const next = current.map((node) => {
        const item = node.asset?.remoteId ? byId.get(node.asset.remoteId) : undefined;
        if (!item || (node.asset?.thumbnailUrl === item.thumbnailUrl && node.asset.fileName === item.filename && node.asset.localUrl === item.contentUrl && node.asset.uploadState === "ready" && node.asset.mediaMeta === item.media)) return node;
        changed = true;
        return { ...node, asset: { ...node.asset!, localUrl: item.contentUrl, fileName: item.filename, thumbnailUrl: item.thumbnailUrl, uploadState: "ready" as const, mediaMeta: item.media } };
      });
      return changed ? next : current;
    });
  }, [assetLibrary]);

  useEffect(() => {
    const previous = previousRailPanelRef.current;
    previousRailPanelRef.current = railPanel;
    if (!railPanel) {
      if (previous === "timeline") window.requestAnimationFrame(() => timelineRailButtonRef.current?.focus());
      return;
    }
    closeContextMenu(false);
    setConnecting(undefined);
    setConnectionPointer(undefined);
    canvasInteractionActiveRef.current = false;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setAssetPickerTarget(undefined); setRailPanel(null); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [closeContextMenu, railPanel]);

  useEffect(() => {
    const trackCanvasActivation = (event: Event) => {
      canvasInteractionActiveRef.current = event.target instanceof Node && Boolean(canvasRef.current?.contains(event.target));
    };
    window.addEventListener("pointerdown", trackCanvasActivation, true);
    window.addEventListener("contextmenu", trackCanvasActivation, true);
    return () => {
      window.removeEventListener("pointerdown", trackCanvasActivation, true);
      window.removeEventListener("contextmenu", trackCanvasActivation, true);
    };
  }, []);

  useEffect(() => {
    const cached = parseJobHistoryCacheEnvelope(sessionStorage.getItem(JOB_HISTORY_CACHE_KEY));
    jobHistoryInstanceRef.current = cached.instanceId;
    jobHistoryLegacyCacheRef.current = cached.jobs.length > 0 && !cached.instanceId;
    jobHistoryPendingCacheRef.current = cached;
    void loadJobHistory().catch(() => undefined);
  }, [loadJobHistory]);

  useEffect(() => {
    if (railPanel !== "results") return;
    void loadJobHistory().catch(() => undefined);
  }, [loadJobHistory, railPanel]);

  useEffect(() => {
    if (!jobHistoryCacheReadyRef.current) return;
    const timer = window.setTimeout(() => {
      try { sessionStorage.setItem(JOB_HISTORY_CACHE_KEY, serializeJobHistoryCache(jobHistory, Date.now(), jobHistoryInstanceRef.current)); } catch { /* optional cache */ }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [jobHistory]);

  useEffect(() => {
    try {
      const workspaceRaw = localStorage.getItem(CANVAS_WORKSPACE_STORAGE_KEY);
      const parsedWorkspace = workspaceRaw ? parseCanvasWorkspace(workspaceRaw) : undefined;
      if (workspaceRaw && (!parsedWorkspace?.ok || parsedWorkspace.issues?.length)) {
        localStorage.setItem(CANVAS_WORKSPACE_BACKUP_KEY, workspaceRaw);
      }
      if (parsedWorkspace?.ok && parsedWorkspace.workspace) {
        canvasWorkspaceRef.current = parsedWorkspace.workspace;
        activeCanvasIdRef.current = parsedWorkspace.workspace.activeCanvasId;
        setCanvasWorkspace(parsedWorkspace.workspace);
        const active = parsedWorkspace.workspace.canvases.find((canvas) => canvas.id === parsedWorkspace.workspace!.activeCanvasId)!;
        restoreCanvasState(active.document);
        setWorkflowHydrated(true);
        setNotice(parsedWorkspace.issues?.length
          ? `已恢复 ${parsedWorkspace.workspace.canvases.length} 个画布；${parsedWorkspace.issues.length} 个损坏项已隔离并备份，当前为“${active.title}”。`
          : `已恢复 ${parsedWorkspace.workspace.canvases.length} 个画布；当前为“${active.title}”。`);
        return;
      }

      const raw = localStorage.getItem(STORAGE_KEY) ?? LEGACY_STORAGE_KEYS.map((key) => localStorage.getItem(key)).find(Boolean);
      const restored = raw ? parseCanvasDocument(raw) : undefined;
      if (restored && !restored.ok) throw new Error(restored.error ?? "文档解析失败");
      if (restored?.backup) localStorage.setItem(restored.backup.key, restored.backup.value);
      if (restored?.migrated) localStorage.setItem(V7_STORAGE_KEY, serializeCanvasDocument(restored.document));
      const document = restored?.document ?? canvasDocumentFromState(BASE_NODES, DEFAULT_EDGES, { video: defaultGeneratorRuntime("video"), image: defaultGeneratorRuntime("image") }, { x: 32, y: 32, zoom: 1 });
      const workspace = createCanvasWorkspace(document);
      canvasWorkspaceRef.current = workspace;
      activeCanvasIdRef.current = workspace.activeCanvasId;
      setCanvasWorkspace(workspace);
      restoreCanvasState(document);
      persistCanvasWorkspace(workspace, document);
      setWorkflowHydrated(true);
      setNotice(restored ? `已将现有 V${restored.sourceVersion ?? 7} 工作流迁移为第一个画布，旧数据仍保留。` : "已创建第一个本地画布，所有修改会自动保存。");
    } catch {
      const document = canvasDocumentFromState(BASE_NODES, DEFAULT_EDGES, { video: defaultGeneratorRuntime("video"), image: defaultGeneratorRuntime("image") }, { x: 32, y: 32, zoom: 1 });
      const workspace = createCanvasWorkspace(document);
      canvasWorkspaceRef.current = workspace;
      activeCanvasIdRef.current = workspace.activeCanvasId;
      workflowSnapshotRef.current = document;
      setCanvasWorkspace(workspace);
      restoreCanvasState(document);
      setWorkflowHydrated(true);
      setNotice("本地工作流损坏或存储不可用，已使用默认画布；原始工作区未被覆盖。");
    }
  }, [persistCanvasWorkspace, restoreCanvasState]);

  useEffect(() => {
    // The first render intentionally uses SSR-safe defaults. Do not let that
    // render overwrite the document restored by the hydration effect above.
    if (!workflowHydrated) return;
    // Keep the latest live state in a ref; stringify only after interaction settles.
    workflowSnapshotRef.current = canvasDocumentFromState(nodes, edges, generatorStates, viewport);
    if (workflowSaveTimerRef.current !== undefined) window.clearTimeout(workflowSaveTimerRef.current);
    workflowSaveTimerRef.current = window.setTimeout(flushWorkflowSnapshot, 180);
    return () => {
      if (workflowSaveTimerRef.current !== undefined) window.clearTimeout(workflowSaveTimerRef.current);
      workflowSaveTimerRef.current = undefined;
    };
  }, [nodes, edges, generatorStates, viewport, workflowHydrated, flushWorkflowSnapshot]);
  useEffect(() => {
    const flushOnPageHide = () => { if (workflowHydrated) flushWorkflowSnapshot(); };
    window.addEventListener("pagehide", flushOnPageHide);
    return () => {
      window.removeEventListener("pagehide", flushOnPageHide);
      if (workflowHydrated) flushWorkflowSnapshot();
    };
  }, [flushWorkflowSnapshot, workflowHydrated]);
  useEffect(() => () => {
    if (dragFrameRef.current !== undefined) window.cancelAnimationFrame(dragFrameRef.current);
  }, []);
  useEffect(() => () => { assetUrlsRef.current.forEach((url) => URL.revokeObjectURL(url)); }, []);
  useEffect(() => {
    const selected = profileSelection[generator];
    if (generator === "image" && selected !== "auto" && !profileChoices.some((profile) => profile.id === selected)) {
      updateGenerator(generator === "image" ? imageNodeId : videoNodeId, (current) => ({ ...current, profileId: "auto", configRevision: current.configRevision + 1 }));
    }
  }, [generator, imageNodeId, profileChoices, profileSelection, updateGenerator, videoNodeId]);
  useEffect(() => {
    if (!videoProfile || autoProfileRef.current[videoNodeId] === videoProfile.id) return;
    autoProfileRef.current[videoNodeId] = videoProfile.id;
    setVideoParams((current) => ({
      ...current,
      steps: Number(videoProfile.defaults.steps ?? current.steps),
      loraStrength: Number(videoProfile.defaults.lora_strength ?? (videoProfile.sampling_mode === "base" ? 0 : current.loraStrength)),
      denoise: Number(videoProfile.defaults.denoise ?? current.denoise),
    }));
  }, [profileSelection.video, setVideoParams, videoNodeId, videoProfile]);
  useEffect(() => {
    if (!imageProfile || profileSelection.image !== "auto" || autoProfileRef.current[imageNodeId] === imageProfile.id) return;
    autoProfileRef.current[imageNodeId] = imageProfile.id;
    setImageParams((current) => ({
      ...current,
      steps: Number(imageProfile.defaults.steps ?? current.steps),
      cfg: Number(imageProfile.defaults.cfg ?? current.cfg),
      loraStrength: Number(imageProfile.defaults.lora_strength ?? current.loraStrength),
      denoise: Number(imageProfile.defaults.denoise ?? current.denoise),
    }));
  }, [imageNodeId, imageProfile, profileSelection.image, setImageParams]);

  const uploadAsset = useCallback(async (nodeId: string, file: File) => {
    const controller = new AbortController(); uploadControllersRef.current.set(nodeId, controller);
    try {
      const form = new FormData(); form.append("file", file);
      const response = await fetch("/api/assets", { method: "POST", body: form, signal: controller.signal });
      if (!response.ok) throw new Error(`上传失败 (${response.status})`);
      const data = await response.json() as { id?: string; asset_id?: string; kind?: MediaKind; filename?: string; created_at?: number; thumbnail_url?: string; media?: Asset["mediaMeta"]; reused?: boolean };
      const remoteId = data.id ?? data.asset_id;
      if (!remoteId) throw new Error("上传成功但服务未返回素材 ID");
      setNodes((current) => current.map((node) => node.id === nodeId && node.asset ? { ...node, asset: { ...node.asset, remoteId, thumbnailUrl: data.thumbnail_url, uploadState: "ready", mediaMeta: data.media } } : node));
      const libraryItem = remoteAssetToLibraryItem({ ...data, id: remoteId });
      if (libraryItem) setAssetLibrary((current) => [libraryItem, ...current.filter((item) => item.id !== libraryItem.id)]);
      if (data.reused) setNotice(`素材已存在，已复用：${data.filename ?? file.name}`);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") return;
      setNodes((current) => current.map((node) => node.id === nodeId && node.asset ? { ...node, asset: { ...node.asset, uploadState: "error" } } : node));
      setJob({ status: "failed", progress: 0, message: error instanceof Error ? error.message : "素材上传失败" });
    } finally { uploadControllersRef.current.delete(nodeId); }
  }, [setJob]);

  const addFiles = useCallback((files: FileList | File[], at?: XY) => {
    Array.from(files).forEach((file, index) => {
      const media = mediaType(file);
      if (!media) { setNotice(`不支持 ${file.name}；只接受图片、视频或音频。`); return; }
      const id = `asset-${Date.now()}-${index}`; const localUrl = URL.createObjectURL(file); assetUrlsRef.current.add(localUrl);
      setNodes((current) => [...current, { id, kind: "asset", title: media === "image" ? "Image Reference" : media === "video" ? "Video Reference" : "Audio Reference", position: at ? { x: at.x + index * 28, y: at.y + index * 28 } : { x: lastCanvasPointerRef.current.x + index * 28, y: lastCanvasPointerRef.current.y + index * 28 }, asset: { file, media, fileName: file.name, localUrl, uploadState: "uploading", role: DEFAULT_ROLE[media] } }]);
      setSelectedId(id); void uploadAsset(id, file);
    });
  }, [uploadAsset]);

  useEffect(() => {
    const handleClipboardPaste = (event: ClipboardEvent) => {
      if (event.defaultPrevented || isEditableEventTarget(event.target) || !canvasInteractionActiveRef.current) return;
      const imageFiles = Array.from(event.clipboardData?.items ?? []).flatMap((item) => {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (!file) return [];
          const extension = file.type === "image/jpeg" ? "jpg" : file.type === "image/webp" ? "webp" : "png";
          return [file.name ? file : new File([file], `clipboard-${Date.now()}.${extension}`, { type: file.type })];
        }
        return [];
      });
      if (!imageFiles.length) return;
      event.preventDefault();
      addFiles(imageFiles, lastCanvasPointerRef.current);
    };
    window.addEventListener("paste", handleClipboardPaste);
    return () => window.removeEventListener("paste", handleClipboardPaste);
  }, [addFiles]);

  const addLibraryAsset = useCallback((item: LibraryAsset, connectTarget?: "image" | "video", targetNodeId?: string, targetSlot?: number): boolean => {
    const resolvedTargetId = targetNodeId ?? (connectTarget === "image" ? imageNodeId : videoNodeId);
    const targetConnectedAssets = edges.filter((edge) => edge.target === resolvedTargetId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset));
    const targetImageReferences = targetConnectedAssets.filter((node) => node.asset?.media === "image");
    const targetRuntime = generatorStatesRef.current[resolvedTargetId] ?? defaultGeneratorRuntime(connectTarget ?? "video");
    const targetVideoParams = targetRuntime.videoParams ?? DEFAULT_VIDEO_PARAMS;
    if (connectTarget === "image") {
      if (item.kind !== "image") { setNotice("图片生成节点只接受图片参考。"); return false; }
      if (targetImageReferences.some((reference) => reference.asset?.remoteId === item.id)) return true;
      const nextCount = targetImageReferences.length + 1;
      const requested = targetRuntime.profileId === "auto" ? undefined : profiles.find((profile) => profile.id === targetRuntime.profileId);
      const nextProfile = requested ?? profiles.find((profile) => profile.available && imageProfileAcceptsReferenceCount(profile, nextCount));
      if (!nextProfile || !imageProfileAcceptsReferenceCount(nextProfile, nextCount)) { setNotice(`当前模型不接受第 ${nextCount} 张参考图；请先选择支持多图的 Profile。`); return false; }
    }
    if (connectTarget === "video") {
      if (targetConnectedAssets.some((node) => node.asset?.remoteId === item.id)) return true;
      if (targetConnectedAssets.length >= H3_REFERENCE_BUDGET) { setNotice(`H3 单任务最多连接 ${H3_REFERENCE_BUDGET} 个参考文件。`); return false; }
      if (targetSlot && edges.some((edge) => edge.target === resolvedTargetId && edge.data.reference_index === targetSlot - 1 && nodes.find((node) => node.id === edge.source)?.asset?.media === item.kind)) { setNotice(`${item.kind} ${targetSlot} 槽位已被占用。`); return false; }
    }
    let role: AssetRole = DEFAULT_ROLE[item.kind];
    if (connectTarget === "image") role = targetImageReferences.length ? "reference" : "init_image";
    if (connectTarget === "video" && item.kind === "image") {
      const endpoints = targetConnectedAssets.filter((node) => node.asset?.media === "image" && ["first_frame", "last_frame"].includes(referenceRoleForTarget(edges, resolvedTargetId, node.id, node.asset.role)));
      const endpointMode = ["auto", "i2v", "fl2v"].includes(targetVideoParams.directorMode);
      if (endpointMode && !targetConnectedAssets.length) role = "first_frame";
      else if (endpointMode && targetConnectedAssets.length === 1 && endpoints.length === 1 && referenceRoleForTarget(edges, resolvedTargetId, endpoints[0].id, endpoints[0].asset?.role) === "first_frame") role = "last_frame";
      else role = "identity";
    }
    const needsReferenceMode = connectTarget === "video" && (item.kind !== "image" || role === "identity");
    if (needsReferenceMode) {
      const endpointIds = new Set(targetConnectedAssets.filter((node) => node.asset?.media === "image" && ["first_frame", "last_frame"].includes(referenceRoleForTarget(edges, resolvedTargetId, node.id, node.asset.role))).map((node) => node.id));
      if (endpointIds.size) {
        setNodes((current) => current.map((node) => node.asset && endpointIds.has(node.id) ? { ...node, asset: { ...node.asset, role: "identity" } } : node));
        setEdges((current) => current.map((edge) => endpointIds.has(edge.source) ? { ...edge, role: "identity", data: { ...edge.data, role: "identity" } } : edge));
      }
    }
    const id = `library-${item.id}-${crypto.randomUUID()}`;
    setNodes((current) => [...current, {
      id,
      kind: "asset",
      title: item.kind === "image" ? "Image Reference" : item.kind === "video" ? "Video Reference" : "Audio Reference",
      position: { x: 60 + (current.length % 3) * 36, y: 430 + (current.length % 4) * 28 },
      asset: {
        media: item.kind,
        fileName: item.filename,
        localUrl: item.contentUrl,
        thumbnailUrl: item.thumbnailUrl,
        remoteId: item.id,
        uploadState: "ready",
        role,
        restored: true,
        mediaMeta: item.media,
      },
    }]);
    if (connectTarget) {
      setEdges((current) => [...current, { id: `${id}-${resolvedTargetId}-${Date.now()}`, source: id, target: resolvedTargetId, role, data: { role, reference_index: targetSlot ? targetSlot - 1 : connectTarget === "image" ? targetImageReferences.length : undefined, ...(item.kind === "video" ? { include_audio: false } : {}) } }]);
      updateGenerator(resolvedTargetId, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    }
    setSelectedId(id);
    setRailPanel(null);
    setNotice(connectTarget === "image" ? `已将 ${item.filename} 作为图${targetImageReferences.length + 1}连到图片生成节点。` : connectTarget === "video" ? `已引用 ${item.filename} 并连接到 H3 视频。` : `已将 ${item.filename} 添加到画布。可拖动素材右侧圆点到生成节点左侧圆点完成连线。`);
    return true;
  }, [edges, imageNodeId, nodes, profiles, updateGenerator, videoNodeId]);

  const importJobOutput = useCallback(async (jobId: string): Promise<LibraryAsset> => {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/assets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: 0 }),
    });
    const data = await response.json() as { asset?: unknown; error?: { message?: string } };
    if (!response.ok) throw new Error(data.error?.message ?? `结果持久化失败 (${response.status})`);
    const library = remoteAssetToLibraryItem(data.asset ?? data);
    if (!library) throw new Error("服务端未返回有效的素材记录");
    setAssetLibrary((current) => [library, ...current.filter((item) => item.id !== library.id)]);
    setSavedResultAssets((current) => ({ ...current, [jobId]: library.id }));
    return library;
  }, []);

  const materializeJobOutput = useCallback(async (jobId: string): Promise<LibraryAsset> => {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/assets`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ index: 0, visibility: "internal" }) });
    const data = await response.json() as { asset?: unknown; error?: { message?: string } };
    if (!response.ok) throw new Error(data.error?.message ?? `上游结果物化失败 (${response.status})`);
    const materialized = remoteAssetToLibraryItem(data.asset ?? data);
    if (!materialized) throw new Error("服务端未返回有效的临时上游媒体");
    return materialized;
  }, []);

  const saveResultToAssets = useCallback(async (result: Job): Promise<LibraryAsset | undefined> => {
    if (!result.id || result.status !== "completed" || !result.previewUrl) {
      setNotice("只能保存已完成的生成结果。");
      return undefined;
    }
    const existingId = savedResultAssets[result.id];
    const existing = existingId ? assetLibrary.find((item) => item.id === existingId) : undefined;
    if (existing) return existing;
    setNotice("正在将生成结果保存到资产…");
    try {
      const library = await importJobOutput(result.id);
      setNotice(`已将生成结果保存到资产：${library.filename}`);
      return library;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "生成结果保存失败");
      return undefined;
    }
  }, [assetLibrary, importJobOutput, savedResultAssets]);

  const addResultToCanvas = useCallback(async (result: Job) => {
    if (!result.id || result.status !== "completed" || !result.previewUrl || !result.media) { setNotice("只能将已完成的结果添加到画布。"); return; }
    const existingId = savedResultAssets[result.id];
    const existing = existingId ? assetLibrary.find((item) => item.id === existingId) : undefined;
    if (existing) { addLibraryAsset(existing); return; }
    const jobId = result.id; const media = result.media; const previewUrl = result.previewUrl;
    const id = `result-${jobId}-${crypto.randomUUID()}`;
    const width = Number(result.parameters?.width ?? 0) || undefined;
    const height = Number(result.parameters?.height ?? 0) || undefined;
    const duration = Number(result.parameters?.duration_actual ?? 0) || undefined;
    setNodes((current) => [...current, { id, kind: "asset", title: media === "image" ? "Generated Image" : "Generated Video", position: { x: 100 + (current.length % 3) * 40, y: 460 + (current.length % 4) * 30 }, asset: { media, fileName: `generation-${jobId.slice(0, 8)}.${media === "image" ? "png" : "mp4"}`, localUrl: previewUrl, sourceJobId: jobId, thumbnailUrl: `/api/jobs/${encodeURIComponent(jobId)}/thumbnail?index=0`, uploadState: "ready", role: DEFAULT_ROLE[media], mediaMeta: { width, height, duration } } }]);
    setSelectedId(id);
    setRailPanel(null);
    setNotice("结果已作为未保存节点加入画布；不会自动进入资产。");
  }, [addLibraryAsset, assetLibrary, savedResultAssets]);

  const previewResultOnCanvas = useCallback((result: Job) => {
    if (result.media !== "image" && result.media !== "video") return;
    const target = resolveResultPreviewTarget(nodes, edges, Object.fromEntries(Object.entries(generatorStates).map(([id, runtime]) => [id, runtime.job.id])), result.id, result.media);
    if (!target) {
      const hasGenerator = nodes.some((node) => node.kind === result.media);
      setNotice(hasGenerator ? "当前同类型生成节点尚未连接 Output；请先连线，或点“添加到画布”新建媒体节点。" : `当前画布没有${result.media === "image" ? "图片" : "视频"}生成节点；请先新建并连接 Output，或点“添加到画布”。`);
      return;
    }
    setJobForNode(target.generatorId, result);
    setSelectedId(target.outputId);
    setRailPanel(null);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => focusCanvasNode(target.outputId)));
  }, [edges, focusCanvasNode, generatorStates, nodes, setJobForNode]);

  const addDerivedResultToCanvas = useCallback((derived: DerivedMedia, sourceNodeId?: string) => {
    const id = `derived-${derived.id}-${crypto.randomUUID()}`;
    const sourceNode = sourceNodeId ? nodes.find((node) => node.id === sourceNodeId) : undefined;
    const duplicateCount = nodes.filter((node) => node.asset?.derivationId === derived.id).length;
    const position = sourceNode
      ? { x: sourceNode.position.x + NODE_SIZE[sourceNode.kind].w + 56, y: sourceNode.position.y + duplicateCount * 32 }
      : { x: lastCanvasPointerRef.current.x + duplicateCount * 28, y: lastCanvasPointerRef.current.y + duplicateCount * 28 };
    const asset: Asset = {
      media: derived.kind,
      fileName: derived.displayName,
      localUrl: derived.contentUrl,
      thumbnailUrl: derived.thumbnailUrl,
      derivationId: derived.id,
      uploadState: "ready",
      role: DEFAULT_ROLE[derived.kind],
      mediaMeta: derived.media,
    };
    setNodes((current) => [...current, { id, kind: "asset", title: derived.kind === "image" ? "Derived Frame" : derived.kind === "video" ? "Derived Video" : "Derived Audio", position, asset }]);
    setSelectedId(id);
    setRailPanel(null);
    const host = canvasViewportRef.current;
    if (host) {
      const rect = host.getBoundingClientRect();
      setViewport((current) => ({ ...current, x: rect.width / 2 - (position.x + NODE_SIZE.asset.w / 2) * current.zoom, y: rect.height / 2 - (position.y + NODE_SIZE.asset.h / 2) * current.zoom }));
    }
  }, [nodes]);

  const deriveAssetNode = useCallback(async (nodeId: string, request: MediaDeriveRequest, options?: MediaDeriveOptions) => {
    const source = nodes.find((node) => node.id === nodeId)?.asset;
    const deriveSource: MediaDeriveSource | undefined = source?.remoteId
      ? { type: "asset", asset_id: source.remoteId }
      : source?.sourceJobId
        ? { type: "job", job_id: source.sourceJobId, index: 0 }
        : source?.derivationId
          ? { type: "derivation", receipt_id: source.derivationId }
          : undefined;
    if (!deriveSource) { setNotice("当前节点没有可剪辑的远程媒体来源。"); return; }
    setNotice("正在生成派生媒体…");
    try {
      const derived: DerivedMedia = await deriveLibraryMedia(deriveSource, request, options);
      if (!derived.contentUrl) throw new Error("派生媒体缺少预览地址");
      setDerivedResults((current) => [derived, ...current.filter((item) => item.id !== derived.id)]);
      addDerivedResultToCanvas(derived, nodeId);
      setNotice("已生成新节点并存入结果；需要复用时可在节点右键“保存到资产”。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "媒体派生失败");
    }
  }, [addDerivedResultToCanvas, nodes]);

  const saveDerivedNode = useCallback(async (nodeId: string) => {
    const source = nodes.find((node) => node.id === nodeId)?.asset;
    if (!source?.derivationId && !source?.sourceJobId) return;
    try {
      const library = source.derivationId ? await saveDerivedMedia(source.derivationId, source.fileName) : await importJobOutput(source.sourceJobId!);
      setAssetLibrary((current) => [library, ...current.filter((item) => item.id !== library.id)]);
      if (source.derivationId) setDerivedResults((current) => current.map((item) => item.id === source.derivationId ? { ...item, assetId: library.id } : item));
      setNodes((current) => current.map((node) => node.asset && (node.id === nodeId || Boolean(source.derivationId && node.asset.derivationId === source.derivationId)) ? { ...node, asset: { ...node.asset, remoteId: library.id, derivationId: undefined, sourceJobId: undefined, fileName: library.filename, localUrl: library.contentUrl, thumbnailUrl: library.thumbnailUrl, restored: true } } : node));
      setNotice(`已保存到资产：${library.filename}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "派生媒体保存失败");
    }
  }, [importJobOutput, nodes]);

  const saveDerivedResultToAssets = useCallback(async (derived: DerivedMedia) => {
    try {
      const library = await saveDerivedMedia(derived.id, derived.displayName);
      setAssetLibrary((current) => [library, ...current.filter((item) => item.id !== library.id)]);
      setDerivedResults((current) => current.map((item) => item.id === derived.id ? { ...item, assetId: library.id } : item));
      setNodes((current) => current.map((node) => node.asset?.derivationId === derived.id ? { ...node, asset: { ...node.asset, remoteId: library.id, derivationId: undefined, fileName: library.filename, localUrl: library.contentUrl, thumbnailUrl: library.thumbnailUrl, restored: true } } : node));
      setNotice(`已保存到资产：${library.filename}`);
      return library;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "派生结果保存失败");
      return undefined;
    }
  }, []);

  const deleteDerivedResult = useCallback(async (derived: DerivedMedia) => {
    try {
      await deleteDerivedMedia(derived.id);
      const removedNodeIds = new Set(nodes.filter((node) => node.asset?.derivationId === derived.id).map((node) => node.id));
      setDerivedResults((current) => current.filter((item) => item.id !== derived.id));
      setNodes((current) => current.filter((node) => !removedNodeIds.has(node.id)));
      setEdges((current) => current.filter((edge) => !removedNodeIds.has(edge.source) && !removedNodeIds.has(edge.target)));
      setNotice(derived.assetId ? "已删除派生结果；已保存的资产仍保留。" : "已删除派生结果及其未保存节点。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "派生结果删除失败");
      throw error;
    }
  }, [nodes]);

  const pinAsset = useCallback(async (item: LibraryAsset, pinned: boolean) => {
    try {
      const updated = await updateLibraryAsset(item.id, { pinned });
      setAssetLibrary((current) => current.map((asset) => asset.id === updated.id ? updated : asset));
      setNotice(pinned ? `已置顶资产：${updated.filename}` : `已取消置顶：${updated.filename}`);
    } catch (error) {
      setNotice(`更新资产置顶失败：${error instanceof Error ? error.message : "未知错误"}`);
      throw error;
    }
  }, []);

  const pinJobResult = useCallback(async (item: Job, pinned: boolean) => {
    if (!item.id) return;
    try {
      await updateJobResult(item.id, pinned);
      setJobHistory((current) => current.map((jobItem) => jobItem.id === item.id ? { ...jobItem, pinned } : jobItem));
      setNotice(pinned ? "已置顶结果。" : "已取消结果置顶。");
    } catch (error) {
      setNotice(`更新结果置顶失败：${error instanceof Error ? error.message : "未知错误"}`);
      throw error;
    }
  }, []);

  const pinDerivedResult = useCallback(async (item: DerivedMedia, pinned: boolean) => {
    try {
      const updated = await updateDerivedMedia(item.id, pinned);
      setDerivedResults((current) => current.map((derived) => derived.id === updated.id ? updated : derived));
      setNotice(pinned ? "已置顶派生结果。" : "已取消派生结果置顶。");
    } catch (error) {
      setNotice(`更新派生结果置顶失败：${error instanceof Error ? error.message : "未知错误"}`);
      throw error;
    }
  }, []);

  const mentionItems = useCallback((target: "video" | "image", targetNodeId = target === "video" ? videoNodeId : imageNodeId): PromptMentionItem[] => {
    const connected = edges.filter((edge) => edge.target === targetNodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset) || node?.kind === "image" || (target === "video" && node?.kind === "video"));
    const seen = new Set<string>();
    const current = connected.flatMap((node) => {
      const asset = node.asset;
      const referenceId = asset?.remoteId ?? node.id;
      if (seen.has(referenceId)) return [];
      seen.add(referenceId);
      const kind: MediaKind = asset?.media ?? (node.kind === "image" ? "image" : "video");
      return [{ id: referenceId, label: asset?.fileName ?? `${node.title} 输出`, kind, previewUrl: kind === "audio" ? undefined : asset?.thumbnailUrl, connected: true } satisfies PromptMentionItem];
    });
    const available = assetLibrary.flatMap((item) => {
      if (seen.has(item.id) || (target === "image" && item.kind !== "image")) return [];
      return [{ id: item.id, label: item.filename, kind: item.kind, previewUrl: item.kind === "audio" ? undefined : item.thumbnailUrl, connected: false } satisfies PromptMentionItem];
    });
    return [...current, ...available];
  }, [assetLibrary, edges, imageNodeId, nodes, videoNodeId]);

  const selectMentionItem = useCallback((target: "video" | "image", item: PromptMentionItem, targetNodeId?: string): boolean => {
    if (item.connected) return true;
    const library = assetLibrary.find((asset) => asset.id === item.id);
    if (!library) { setNotice("素材已不在资产库，请刷新后重试。"); return false; }
    return addLibraryAsset(library, target, targetNodeId);
  }, [addLibraryAsset, assetLibrary]);

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    setDragOver(false);
    if (!dataTransferContainsFiles(event.dataTransfer) || !event.dataTransfer.files.length) return;
    event.preventDefault();
    const canvas = canvasRef.current;
    addFiles(event.dataTransfer.files, canvas ? canvasPointFromClient(canvas, event.clientX, event.clientY) : lastCanvasPointerRef.current);
  }
  function handleCanvasDragOver(event: DragEvent<HTMLDivElement>) {
    if (!dataTransferContainsFiles(event.dataTransfer)) {
      setDragOver(false);
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDragOver(true);
  }
  function handleCanvasWheel(event: React.WheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const deltaUnit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Math.max(1, event.currentTarget.clientHeight) : 1;
    let deltaX = event.deltaX * deltaUnit;
    let deltaY = event.deltaY * deltaUnit;
    if (event.ctrlKey || event.metaKey) {
      const factor = Math.exp(-deltaY * 0.002);
      zoomCanvas(viewport.zoom * factor, { x: event.clientX, y: event.clientY });
      return;
    }
    if (event.shiftKey && deltaX === 0) {
      deltaX = deltaY;
      deltaY = 0;
    }
    setViewport((current) => ({ ...current, x: current.x - deltaX, y: current.y - deltaY }));
  }
  function startCanvasPan(event: ReactPointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    const clickedBlankCanvas = !target.closest(".studio-node, .wire-group, .canvas-toolbar, .canvas-context-menu, .connection-tip");
    if (event.button === 0 && clickedBlankCanvas && connecting) {
      if (dragFrameRef.current !== undefined) {
        window.cancelAnimationFrame(dragFrameRef.current);
        dragFrameRef.current = undefined;
      }
      dragPointerRef.current = undefined;
      setConnecting(undefined);
      setConnectionPointer(undefined);
    }
    const wantsSpacePan = event.button === 0 && spacePressedRef.current && !isEditableEventTarget(target);
    const wantsPan = event.button === 1 || wantsSpacePan || (event.button === 0 && clickedBlankCanvas);
    if (!wantsPan || target.closest(".canvas-toolbar, .canvas-context-menu")) return;
    event.preventDefault();
    const captureTarget = event.currentTarget;
    panRef.current = { clientX: event.clientX, clientY: event.clientY, originX: viewport.x, originY: viewport.y, pointerId: event.pointerId, captureTarget };
    captureTarget.setPointerCapture(event.pointerId);
  }
  function moveCanvasPan(event: ReactPointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (pan) {
      setViewport((current) => ({ ...current, x: pan.originX + event.clientX - pan.clientX, y: pan.originY + event.clientY - pan.clientY }));
      return;
    }
    const canvas = canvasRef.current;
    if (canvas) lastCanvasPointerRef.current = canvasPointFromClient(canvas, event.clientX, event.clientY);
    moveDrag(event);
  }
  function finishCanvasPan(event?: ReactPointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (pan?.captureTarget.hasPointerCapture(pan.pointerId)) pan.captureTarget.releasePointerCapture(pan.pointerId);
    panRef.current = undefined;
    if (!pan) finishDrag();
    if (event && canvasRef.current) lastCanvasPointerRef.current = canvasPointFromClient(canvasRef.current, event.clientX, event.clientY);
  }
  function navigateFromOverview(clientX: number, clientY: number) {
    const overview = canvasOverviewRef.current;
    const host = canvasViewportRef.current;
    if (!overview || !host) return;
    const rect = overview.getBoundingClientRect();
    const ratioX = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    const ratioY = Math.max(0, Math.min(1, (clientY - rect.top) / Math.max(1, rect.height)));
    const extent = overviewGestureExtentRef.current ?? overviewExtent;
    const canvasX = extent.left + ratioX * extent.width;
    const canvasY = extent.top + ratioY * extent.height;
    setViewport((current) => ({
      ...current,
      x: host.clientWidth / 2 - canvasX * current.zoom,
      y: host.clientHeight / 2 - canvasY * current.zoom,
    }));
  }
  function startOverviewNavigation(event: ReactPointerEvent<HTMLButtonElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    overviewPointerRef.current = event.pointerId;
    overviewGestureExtentRef.current = overviewExtent;
    setOverviewGestureExtent(overviewExtent);
    event.currentTarget.setPointerCapture(event.pointerId);
    navigateFromOverview(event.clientX, event.clientY);
  }
  function moveOverviewNavigation(event: ReactPointerEvent<HTMLButtonElement>) {
    if (overviewPointerRef.current !== event.pointerId) return;
    navigateFromOverview(event.clientX, event.clientY);
  }
  function finishOverviewNavigation(event: ReactPointerEvent<HTMLButtonElement>) {
    if (overviewPointerRef.current !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    overviewPointerRef.current = undefined;
    overviewGestureExtentRef.current = undefined;
    setOverviewGestureExtent(undefined);
  }
  function startDrag(event: ReactPointerEvent, node: StudioNode) {
    if (event.button !== 0) return;
    if (spacePressedRef.current) return;
    if ((event.target as HTMLElement).closest(NON_DRAGGABLE_SELECTOR)) return;
    const canvas = canvasRef.current; if (!canvas) return;
    const point = canvasPointFromClient(canvas, event.clientX, event.clientY);
    const captureTarget = event.currentTarget as HTMLElement;
    dragRef.current = { id: node.id, dx: point.x - node.position.x, dy: point.y - node.position.y, pointerId: event.pointerId, captureTarget };
    captureTarget.setPointerCapture(event.pointerId); setSelectedId(node.id);
  }
  function applyPointerUpdate(pointer: { clientX: number; clientY: number; connecting?: string }) {
    const active = dragRef.current; const canvas = canvasRef.current;
    if (!canvas) return;
    const point = canvasPointFromClient(canvas, pointer.clientX, pointer.clientY);
    if (pointer.connecting) setConnectionPointer(point);
    if (!active) return;
    setNodes((current) => {
      const target = current.find((node) => node.id === active.id); if (!target) return current;
      const position = { x: point.x - active.dx, y: point.y - active.dy };
      if (target.position.x === position.x && target.position.y === position.y) return current;
      return current.map((node) => node.id === active.id ? { ...node, position } : node);
    });
  }
  function moveDrag(event: ReactPointerEvent) {
    if (!dragRef.current && !connecting) return;
    dragPointerRef.current = { clientX: event.clientX, clientY: event.clientY, connecting };
    if (dragFrameRef.current !== undefined) return;
    dragFrameRef.current = window.requestAnimationFrame(() => {
      dragFrameRef.current = undefined;
      const pointer = dragPointerRef.current;
      dragPointerRef.current = undefined;
      if (pointer) applyPointerUpdate(pointer);
    });
  }
  function finishDrag() {
    if (dragFrameRef.current !== undefined) {
      window.cancelAnimationFrame(dragFrameRef.current);
      dragFrameRef.current = undefined;
    }
    const pointer = dragPointerRef.current;
    dragPointerRef.current = undefined;
    if (pointer) applyPointerUpdate(pointer);
    const active = dragRef.current;
    dragRef.current = undefined;
    if (active?.captureTarget.hasPointerCapture(active.pointerId)) active.captureTarget.releasePointerCapture(active.pointerId);
  }
  function beginConnection(event: ReactPointerEvent<HTMLButtonElement>, node: StudioNode) {
    event.stopPropagation();
    if (event.button !== 0) return;
    if (connecting === node.id) {
      setConnecting(undefined); setConnectionPointer(undefined); return;
    }
    const canvas = canvasRef.current;
    setSelectedId(node.id); setConnecting(node.id);
    setConnectionPointer(canvas ? canvasPointFromClient(canvas, event.clientX, event.clientY) : endpoint(node, "out", nodePortAnchors));
  }
  function beginConnectionFromKeyboard(node: StudioNode) {
    const cancel = connecting === node.id;
    setSelectedId(node.id); setConnecting(cancel ? undefined : node.id); setConnectionPointer(cancel ? undefined : endpoint(node, "out", nodePortAnchors));
  }
  function finishConnection(event: ReactPointerEvent<HTMLButtonElement>, targetId: string) {
    event.stopPropagation();
    if (!connecting) return;
    connectTo(targetId);
  }
  function validateConnection(source: StudioNode, target: StudioNode) {
    if (source.id === target.id) return "节点不能连接自身";
    const allowed = (source.kind === "asset" && target.kind === "video") || (source.kind === "asset" && source.asset?.media === "image" && target.kind === "image") || (source.kind === "image" && (target.kind === "image" || target.kind === "video")) || (source.kind === "video" && target.kind === "video") || (["video", "image"].includes(source.kind) && target.kind === "output");
    if (!allowed) return source.kind === "asset" && target.kind === "image" ? "图片生成节点只接受图片，不能连接视频或音频" : "这两个端口类型不兼容";
    if (edges.some((edge) => edge.source === source.id && edge.target === target.id)) return "这两个节点已经连接";
    if (createsCycle(edges, source.id, target.id)) return "连接会形成循环，已阻止";
    if (source.kind === "asset") {
      if (source.asset?.uploadState !== "ready" || !source.asset.remoteId) return "素材未上传成功，不能连接";
    }
    if (target.kind === "video" || target.kind === "image") {
      const document = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
      const kind: MediaKind = source.asset?.media ?? (source.kind === "image" ? "image" : "video");
      const transaction = connectMedia(document, source.id, target.id, { kind, sourceHandle: source.asset ? "media" : kind });
      if (!transaction.ok) return transaction.issues[0]?.message ?? "连接失败";
    }
    return "";
  }
  function connectTo(targetId: string) {
    if (!connecting || connecting === targetId) { setConnecting(undefined); setConnectionPointer(undefined); return; }
    const source = nodes.find((node) => node.id === connecting); const target = nodes.find((node) => node.id === targetId);
    if (!source || !target) { setConnecting(undefined); setConnectionPointer(undefined); return; }
    const error = validateConnection(source, target); if (error) { setNotice(error); setConnecting(undefined); setConnectionPointer(undefined); return; }
    const targetConnected = edges.filter((edge) => edge.target === target.id).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node));
    const targetRuntime = generatorStatesRef.current[target.id] ?? defaultGeneratorRuntime(target.kind === "image" ? "image" : "video");
    let role: AssetRole | "output" = source.asset ? (target.kind === "image" ? (targetConnected.length ? "reference" : "init_image") : source.asset.role) : target.kind === "video" || target.kind === "image" ? "reference" : "output";
    if (source.asset && target.kind === "video") {
      const canUseEndpoints = ["auto", "i2v", "fl2v"].includes(targetRuntime.videoParams?.directorMode ?? "auto") && source.asset.media === "image" && targetConnected.every((node) => node.asset?.media === "image" && ["first_frame", "last_frame"].includes(referenceRoleForTarget(edges, target.id, node.id, node.asset.role)));
      role = canUseEndpoints && targetConnected.length === 0 ? "first_frame" : canUseEndpoints && targetConnected.length === 1 ? "last_frame" : "reference";
      if (role === "reference") {
        const existingIds = new Set(targetConnected.map((node) => node.id));
        setNodes((current) => current.map((node) => node.asset && existingIds.has(node.id) ? { ...node, asset: { ...node.asset, role: "reference" } } : node));
        setEdges((current) => current.map((edge) => existingIds.has(edge.source) && edge.target === target.id ? { ...edge, role: "reference", data: { ...edge.data, role: "reference" } } : edge));
      }
      updateAsset(source.id, { role: role as AssetRole });
    }
    if (target.kind === "video" || target.kind === "image") {
      const document = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
      const kind: MediaKind = source.asset?.media ?? (source.kind === "image" ? "image" : "video");
      const transaction = connectMedia(document, source.id, target.id, { kind, sourceHandle: source.asset ? "media" : kind, role });
      if (!transaction.ok) { setNotice(transaction.issues[0]?.message ?? "连接失败"); setConnecting(undefined); setConnectionPointer(undefined); return; }
      setEdges(legacyEdgesFromDocument(transaction.document));
      updateGenerator(target.id, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    } else {
      setEdges((current) => current.some((edge) => edge.source === source.id && edge.target === target.id) ? current : [...current, { id: `${source.id}-${target.id}-${Date.now()}`, source: source.id, target: target.id, role, data: { role } }]);
    }
    setNotice("连接已建立；双击连线可断开。"); setConnecting(undefined); setConnectionPointer(undefined);
  }
  function updateAsset(nodeId: string, patch: Partial<Asset>) {
    const affectedTargetIds = [...new Set(edges.filter((edge) => edge.source === nodeId).map((edge) => edge.target).filter((targetId) => {
      const target = nodes.find((node) => node.id === targetId);
      return target?.kind === "video" || target?.kind === "image";
    }))];
    setNodes((current) => current.map((node) => node.id === nodeId && node.asset ? { ...node, asset: { ...node.asset, ...patch } } : node));
    setEdges((current) => current.map((edge) => edge.source === nodeId ? { ...edge, role: String(patch.role ?? edge.role), data: { ...edge.data, ...(patch.role ? { role: String(patch.role) } : {}) } } : edge));
    if (patch.role !== undefined) {
      for (const targetId of affectedTargetIds) updateGenerator(targetId, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    }
  }
  function updateReferenceAudio(targetNodeId: string, sourceNodeId: string, enabled: boolean) {
    const source = nodes.find((node) => node.id === sourceNodeId);
    const edge = edges.find((item) => item.target === targetNodeId && item.source === sourceNodeId);
    if (!edge || source?.asset?.media !== "video") return;
    if (enabled) {
      if (source.asset.mediaMeta?.has_audio !== true) { setNotice("该视频没有可用音轨，无法添加 Audio 参考。"); return; }
      const document = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
      const target = document.nodes.find((node) => node.id === targetNodeId);
      if (target?.kind !== "video-generator") return;
      if (target.bindings.filter((binding) => binding.kind === "audio").length >= 3) { setNotice("Audio 参考槽已满，无法再开启视频音轨。"); return; }
    }
    setEdges((current) => current.map((item) => item.id === edge.id ? { ...item, data: { ...item.data, include_audio: enabled } } : item));
    updateGenerator(targetNodeId, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    setNotice(enabled ? `已仅为 ${nodes.find((node) => node.id === targetNodeId)?.title ?? "当前节点"} 开启视频配对音轨。` : "已关闭当前节点的视频配对音轨。");
  }
  function disconnectEdge(edgeId: string) {
    const legacy = edges.find((edge) => edge.id === edgeId);
    if (!legacy) return;
    const target = nodes.find((node) => node.id === legacy.target);
    if (target?.kind !== "video" && target?.kind !== "image") { setEdges((current) => current.filter((edge) => edge.id !== edgeId)); return; }
    const document = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
    const targetNode = document.nodes.find((node) => node.id === target.id);
    if (targetNode?.kind !== "video-generator" && targetNode?.kind !== "image-generator") return;
    const binding = targetNode.bindings.find((item) => item.sourceNodeId === legacy.source);
    if (!binding) { setNotice("找不到该连线对应的媒体绑定。"); return; }
    const transaction = disconnectMedia(document, target.id, binding.id);
    if (!transaction.ok) { setNotice(transaction.issues[0]?.message ?? "断开失败"); return; }
    setEdges(legacyEdgesFromDocument(transaction.document));
    updateGenerator(target.id, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
  }
  function moveImageReference(sourceId: string, direction: -1 | 1) {
    const from = imageReferences.findIndex((item) => item.node.id === sourceId);
    const to = from + direction;
    if (from < 0 || to < 0 || to >= imageReferences.length) return;
    const ordered = imageReferences.map((item) => item.edge.id);
    [ordered[from], ordered[to]] = [ordered[to], ordered[from]];
    const referenceIndex = new Map(ordered.map((id, index) => [id, index]));
    const sourceByEdge = new Map(imageReferences.map((item) => [item.edge.id, item.node.id]));
    const roleBySource = new Map(ordered.map((id, index) => [sourceByEdge.get(id), index === 0 ? "init_image" : "reference"]));
    setEdges((current) => current.map((edge) => referenceIndex.has(edge.id) ? { ...edge, role: String(roleBySource.get(edge.source)), data: { ...edge.data, role: String(roleBySource.get(edge.source)), reference_index: referenceIndex.get(edge.id) } } : edge));
    setNodes((current) => current.map((node) => node.asset && roleBySource.has(node.id) ? { ...node, asset: { ...node.asset, role: roleBySource.get(node.id) as AssetRole } } : node));
    updateGenerator(imageNodeId, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    setNotice(`已调整多图参考顺序；${imageReferences[to].node.asset?.fileName ?? "素材"}与前后位置已交换。`);
  }
  function disconnectImageReference(edgeId: string) {
    const ordered = imageReferences.filter((item) => item.edge.id !== edgeId);
    const referenceIndex = new Map(ordered.map((item, index) => [item.edge.id, index]));
    const roleBySource = new Map(ordered.map((item, index) => [item.node.id, index === 0 ? "init_image" : "reference"]));
    setEdges((current) => {
      const retained = current.filter((edge) => edge.id !== edgeId);
      return retained.map((edge) => referenceIndex.has(edge.id) ? { ...edge, role: String(roleBySource.get(edge.source)), data: { ...edge.data, role: String(roleBySource.get(edge.source)), reference_index: referenceIndex.get(edge.id) } } : edge);
    });
    setNodes((current) => current.map((node) => node.asset && roleBySource.has(node.id) ? { ...node, asset: { ...node.asset, role: roleBySource.get(node.id) as AssetRole } } : node));
    updateGenerator(imageNodeId, (current) => ({ ...current, configRevision: current.configRevision + 1 }));
    setNotice("已断开该图片参考，其他参考已按当前顺序重新编号。");
  }
  // Retained for ordered-reference keyboard/UI controls reintroduced by the graph adapter.
  void moveImageReference;
  void disconnectImageReference;
  function createNode(kind: CoreNodeKind, position: XY) {
    const template = BASE_NODES.find((node) => node.kind === kind);
    if (!template) return;
    const id = `${kind}-${crypto.randomUUID()}`;
    const count = nodes.filter((node) => node.kind === kind).length + 1;
    setNodes((current) => [...current, { ...template, id, title: `${template.title} ${count}`, position: { ...position } }]);
    if (kind === "video" || kind === "image") setGeneratorStates((current) => ({ ...current, [id]: defaultGeneratorRuntime(kind) }));
    setSelectedId(id);
    setNotice(`已新建 ${template.title} 节点；请通过端口连线组成工作流。`);
    closeContextMenu();
  }

  const removeNode = useCallback((node: StudioNode) => {
    if (node.asset?.localUrl.startsWith("blob:")) {
      URL.revokeObjectURL(node.asset.localUrl);
      assetUrlsRef.current.delete(node.asset.localUrl);
    }
    uploadControllersRef.current.get(node.id)?.abort(); uploadControllersRef.current.delete(node.id);
    const ordered = imageReferences.filter((item) => item.node.id !== node.id);
    const referenceIndex = new Map(ordered.map((item, index) => [item.edge.id, index]));
    const roleBySource = new Map(ordered.map((item, index) => [item.node.id, index === 0 ? "init_image" : "reference"]));
    setNodes((current) => current.filter((item) => item.id !== node.id).map((item) => item.asset && roleBySource.has(item.id) ? { ...item, asset: { ...item.asset, role: roleBySource.get(item.id) as AssetRole } } : item));
    if (node.kind === "video" || node.kind === "image") setGeneratorStates((current) => { const next = { ...current }; delete next[node.id]; return next; });
    setEdges((current) => current.filter((edge) => edge.source !== node.id && edge.target !== node.id).map((edge) => referenceIndex.has(edge.id) ? { ...edge, role: String(roleBySource.get(edge.source)), data: { ...edge.data, role: String(roleBySource.get(edge.source)), reference_index: referenceIndex.get(edge.id) } } : edge));
    setConnecting((current) => current === node.id ? undefined : current);
    setConnectionPointer((current) => connecting === node.id ? undefined : current);
    setSelectedId("");
    closeContextMenu();
    setNotice(node.kind === "asset" ? node.asset?.derivationId ? "已从画布移除派生节点；结果仍保留，可从“结果”重新添加。" : "已从画布移除素材节点；远程素材仍保留在资产库，可随时再次添加。" : `已删除 ${node.title} 节点及其连线；可在画布空白处右键重新创建。`);
  }, [closeContextMenu, connecting, imageReferences]);

  const deleteAssetFromLibrary = useCallback(async (item: LibraryAsset) => {
    try {
      await deleteLibraryAsset(item.id);
      const removedNodeIds = new Set(nodes.filter((node) => node.asset?.remoteId === item.id).map((node) => node.id));
      setAssetLibrary((current) => current.filter((asset) => asset.id !== item.id));
      setNodes((current) => current.filter((node) => !removedNodeIds.has(node.id)));
      setEdges((current) => current.filter((edge) => !removedNodeIds.has(edge.source) && !removedNodeIds.has(edge.target)));
      setSavedResultAssets((current) => Object.fromEntries(Object.entries(current).filter(([, assetId]) => assetId !== item.id)));
      setSelectedId((current) => removedNodeIds.has(current) ? "" : current);
      setNotice(`已删除资产：${item.filename}`);
    } catch (error) {
      setNotice(`删除资产失败：${error instanceof Error ? error.message : "未知错误"}`);
      throw error;
    }
  }, [nodes]);

  const deleteResult = useCallback(async (item: Job) => {
    if (!item.id) return;
    const jobId = item.id;
    deletedJobIdsRef.current.add(jobId);
    trustedJobIdsRef.current.delete(jobId);
    setJobHistory((current) => current.filter((jobItem) => jobItem.id !== jobId));
    setNotice("结果已从列表移除，正在后台清理生成文件…");
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      const body = await response.json().catch(() => ({})) as { error?: { message?: string } };
      // A repeated request is idempotent from the UI's point of view: the
      // result is already absent, so a 404 must not resurrect its card.
      if (!response.ok && response.status !== 404) throw new Error(body.error?.message ?? `删除结果失败 (${response.status})`);
      const removedNodeIds = new Set(nodes.filter((node) => node.asset?.sourceJobId === jobId).map((node) => node.id));
      setNodes((current) => current.filter((node) => !removedNodeIds.has(node.id)));
      setEdges((current) => current.filter((edge) => !removedNodeIds.has(edge.source) && !removedNodeIds.has(edge.target)));
      setGeneratorStates((current) => {
        const next = Object.fromEntries(Object.entries(current).map(([id, runtime]) => [id, {
          ...runtime,
          job: runtime.job.id === jobId ? { ...IDLE_JOB, media: runtime.kind } : runtime.job,
          resultVersions: runtime.resultVersions.filter((result) => result.id !== jobId),
        }]));
        generatorStatesRef.current = next;
        return next;
      });
      setSelectedId((current) => removedNodeIds.has(current) ? "" : current);
      setNotice(savedResultAssets[jobId] ? "已删除结果记录及生成文件；保存到资产库的副本仍保留。" : "已删除结果记录及生成文件。");
    } catch (error) {
      deletedJobIdsRef.current.delete(jobId);
      trustedJobIdsRef.current.add(jobId);
      setJobHistory((current) => mergeJobHistory(current, item, 100));
      setNotice(`删除结果失败：${error instanceof Error ? error.message : "未知错误"}`);
      throw error;
    }
  }, [nodes, savedResultAssets]);

  useEffect(() => {
    const handleNodeDelete = (event: KeyboardEvent) => {
      const deletePressed = event.key === "Delete" || event.code === "Backspace";
      if (!deletePressed || event.repeat || !canvasInteractionActiveRef.current) return;
      const target = event.target instanceof Element ? event.target : null;
      if (isEditableEventTarget(target) || target?.closest(".canvas-context-menu")) return;
      const node = nodes.find((item) => item.id === selectedId);
      if (!node) return;
      event.preventDefault();
      removeNode(node);
    };
    window.addEventListener("keydown", handleNodeDelete);
    return () => window.removeEventListener("keydown", handleNodeDelete);
  }, [nodes, removeNode, selectedId]);

  function openCanvasContextMenu(event: ReactMouseEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest(".studio-node")) return;
    event.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    contextMenuInvokerRef.current = null;
    setContextMenu({
      kind: "canvas",
      screenPosition: { x: Math.max(8, Math.min(window.innerWidth - 228, event.clientX)), y: Math.max(8, Math.min(window.innerHeight - 260, event.clientY)) },
      canvasPosition: canvasPointFromClient(canvas, event.clientX, event.clientY),
    });
  }

  function openCanvasContextMenuFromKeyboard(event: React.KeyboardEvent<HTMLDivElement>) {
    const requestsContextMenu = event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
    if (!requestsContextMenu || event.target !== event.currentTarget) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const viewport = event.currentTarget.parentElement?.getBoundingClientRect() ?? rect;
    const screenPosition = {
      x: Math.max(8, Math.min(window.innerWidth - 228, viewport.left + Math.min(viewport.width / 2, 320))),
      y: Math.max(8, Math.min(window.innerHeight - 260, viewport.top + Math.min(viewport.height / 2, 220))),
    };
    contextMenuInvokerRef.current = event.currentTarget;
    setContextMenu({
      kind: "canvas",
      screenPosition,
      canvasPosition: canvasPointFromClient(event.currentTarget, screenPosition.x, screenPosition.y),
    });
  }

  function openNodeContextMenu(event: ReactMouseEvent<HTMLElement>, node: StudioNode) {
    const target = event.target instanceof Element ? event.target : null;
    if (isEditableEventTarget(target) || target?.closest("button, a")) return;
    event.preventDefault();
    event.stopPropagation();
    setSelectedId(node.id);
    contextMenuInvokerRef.current = null;
    setContextMenu({
      kind: "node",
      nodeId: node.id,
      screenPosition: { x: Math.max(8, Math.min(window.innerWidth - 228, event.clientX)), y: Math.max(8, Math.min(window.innerHeight - 110, event.clientY)) },
    });
  }
  function activeCanvasIsBusy() {
    return uploadControllersRef.current.size > 0 || Object.values(generatorStatesRef.current).some((runtime) => ["submitting", "queued", "running"].includes(runtime.job.status));
  }
  function canvasDocumentIsBusy(document: CanvasDocumentV7) {
    return document.nodes.some((node) => (node.kind === "video-generator" || node.kind === "image-generator") && ["submitting", "queued", "running"].includes(node.job.status));
  }
  function createCanvasTab() {
    if (activeCanvasIsBusy()) { setNotice("当前画布仍有上传或生成任务；完成或取消后再新建画布，避免任务结果串台。"); return; }
    if (!flushWorkflowSnapshot()) return;
    const current = canvasWorkspaceRef.current;
    if (!current) return;
    const next = addCanvasWorkspaceTab(current, createFreshStudioCanvasDocument());
    const active = next.canvases.find((canvas) => canvas.id === next.activeCanvasId)!;
    if (!persistCanvasWorkspace(next, active.document)) return;
    restoreCanvasState(active.document);
    canvasWorkspaceRef.current = next;
    activeCanvasIdRef.current = next.activeCanvasId;
    setCanvasWorkspace(next);
    setNotice(`已新建“${active.title}”；原画布仍保留在上方标签中。`);
  }
  function selectCanvasTab(canvasId: string): boolean {
    if (canvasId === activeCanvasIdRef.current) return true;
    if (activeCanvasIsBusy()) { setNotice("当前画布仍有上传或生成任务；完成或取消后再切换画布。"); return false; }
    if (!flushWorkflowSnapshot()) return false;
    const current = canvasWorkspaceRef.current;
    const active = current?.canvases.find((canvas) => canvas.id === canvasId);
    if (!current || !active) return false;
    const next = { ...current, activeCanvasId: canvasId };
    if (!persistCanvasWorkspace(next, active.document)) return false;
    restoreCanvasState(active.document);
    canvasWorkspaceRef.current = next;
    activeCanvasIdRef.current = canvasId;
    setCanvasWorkspace(next);
    setNotice(`已切换到“${active.title}”。`);
    return true;
  }
  function handleCanvasTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, canvasId: string) {
    const canvases = canvasWorkspaceRef.current?.canvases ?? [];
    const currentIndex = canvases.findIndex((canvas) => canvas.id === canvasId);
    if (currentIndex < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? canvases.length - 1 : event.key === "ArrowLeft" ? (currentIndex - 1 + canvases.length) % canvases.length : (currentIndex + 1) % canvases.length;
    const nextId = canvases[nextIndex]?.id;
    if (nextId && selectCanvasTab(nextId)) window.requestAnimationFrame(() => document.getElementById(canvasTabElementId(nextId))?.focus());
  }
  function removeCanvasTab(canvasId: string) {
    if (activeCanvasIsBusy()) { setNotice("当前画布仍有上传或生成任务，暂不能关闭标签。"); return; }
    const target = canvasWorkspaceRef.current?.canvases.find((canvas) => canvas.id === canvasId);
    if (target && canvasDocumentIsBusy(target.document)) { setNotice(`“${target.title}”仍有生成任务，暂不能关闭。`); return; }
    if (target && !window.confirm(`从本地工作区移除“${target.title}”？该画布内容将不再保留。`)) return;
    if (!flushWorkflowSnapshot()) return;
    const current = canvasWorkspaceRef.current;
    if (!current) return;
    const next = removeCanvasWorkspaceTab(current, canvasId);
    if (next === current) { setNotice("至少保留一个画布；最后一个标签不能关闭。"); return; }
    const active = next.canvases.find((canvas) => canvas.id === next.activeCanvasId)!;
    if (!persistCanvasWorkspace(next, active.document)) return;
    if (canvasId === current.activeCanvasId) restoreCanvasState(active.document);
    canvasWorkspaceRef.current = next;
    activeCanvasIdRef.current = next.activeCanvasId;
    setCanvasWorkspace(next);
    setNotice(`已关闭画布标签；当前为“${active.title}”。`);
    window.requestAnimationFrame(() => document.getElementById(canvasTabElementId(active.id))?.focus());
  }
  function chooseProfile(kind: "video" | "image", id: string, targetNodeId = kind === "video" ? videoNodeId : imageNodeId) {
    updateGenerator(targetNodeId, (current) => ({ ...current, profileId: id, configRevision: current.configRevision + 1 }));
    if (id === "auto") {
      autoProfileRef.current[targetNodeId] = undefined;
      return;
    }
    const targetMode = generatorStatesRef.current[targetNodeId]?.videoParams?.directorMode ?? "auto";
    const targetInputs = edges.filter((edge) => edge.target === targetNodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset));
    const targetCompiler = buildVideoDirectorContract(targetMode, generatorStatesRef.current[targetNodeId]?.videoParams?.sourceVideoId ?? "", targetInputs.map((node) => ({ nodeId: node.id, assetId: node.asset?.remoteId, kind: node.asset!.media, label: node.asset!.fileName, role: referenceRoleForTarget(edges, targetNodeId, node.id, node.asset!.role), includeAudio: referenceIncludesAudio(edges, targetNodeId, node.id) }))).compiler;
    const selectedProfile = kind === "video" ? profiles.find((profile) => profile.output_type === "video" && profile.compiler === targetCompiler && profile.sampling_mode === (id === "base20" ? "base" : "turbo4")) : profiles.find((profile) => profile.id === id);
    if (!selectedProfile) return;
    updateGenerator(targetNodeId, (current) => kind === "video" ? {
      ...current,
      videoParams: { ...(current.videoParams ?? DEFAULT_VIDEO_PARAMS), duration: Number(selectedProfile.defaults.duration ?? current.videoParams?.duration ?? DEFAULT_VIDEO_PARAMS.duration), steps: Number(selectedProfile.defaults.steps ?? current.videoParams?.steps ?? DEFAULT_VIDEO_PARAMS.steps), loraStrength: Number(selectedProfile.defaults.lora_strength ?? current.videoParams?.loraStrength ?? DEFAULT_VIDEO_PARAMS.loraStrength), denoise: Number(selectedProfile.defaults.denoise ?? current.videoParams?.denoise ?? DEFAULT_VIDEO_PARAMS.denoise) },
    } : {
      ...current,
      imageParams: { ...(current.imageParams ?? DEFAULT_IMAGE_PARAMS), steps: Number(selectedProfile.defaults.steps ?? current.imageParams?.steps ?? DEFAULT_IMAGE_PARAMS.steps), cfg: Number(selectedProfile.defaults.cfg ?? current.imageParams?.cfg ?? DEFAULT_IMAGE_PARAMS.cfg), loraStrength: Number(selectedProfile.defaults.lora_strength ?? current.imageParams?.loraStrength ?? DEFAULT_IMAGE_PARAMS.loraStrength), denoise: Number(selectedProfile.defaults.denoise ?? current.imageParams?.denoise ?? DEFAULT_IMAGE_PARAMS.denoise) },
    });
  }

  const graphSnapshot = useCallback((output: "video" | "image", targetNodeId?: string, materializedAssets: Record<string, LibraryAsset> = {}) => {
    const generatorNodeId = targetNodeId ?? (output === "video" ? videoNodeId : imageNodeId);
    const runtime = generatorStatesRef.current[generatorNodeId] ?? defaultGeneratorRuntime(output);
    const targetAssets = edges.filter((edge) => edge.target === generatorNodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset) || node?.kind === "video" || node?.kind === "image");
    const targetModeAssets: VideoModeAsset[] = targetAssets.map((node) => { const materialized = materializedAssets[node.id]; return { nodeId: node.id, assetId: node.asset?.remoteId ?? materialized?.id, kind: node.asset?.media ?? materialized?.kind ?? (node.kind === "image" ? "image" : "video"), label: node.asset?.fileName ?? materialized?.filename ?? `${node.title} 输出`, role: referenceRoleForTarget(edges, generatorNodeId, node.id, node.asset?.role), includeAudio: referenceIncludesAudio(edges, generatorNodeId, node.id) }; });
    const targetVideoParams = runtime.videoParams ?? DEFAULT_VIDEO_PARAMS;
    const targetContract = buildVideoDirectorContract(targetVideoParams.directorMode, targetVideoParams.sourceVideoId, targetModeAssets);
    const targetImageReferences = targetAssets.filter((node) => (node.asset?.media ?? materializedAssets[node.id]?.kind ?? (node.kind === "image" ? "image" : "video")) === "image").map((node, index) => ({ node, referenceIndex: index + 1 }));
    const referenceOrder = new Map(targetContract.orderedAssets.map((asset, index) => [asset.nodeId, index]));
    const relevantEdges = dedupeEdges(edges.filter((edge) => edge.target === generatorNodeId || (edge.source === generatorNodeId && nodes.find((node) => node.id === edge.target)?.kind === "output")))
      .sort((left, right) => (referenceOrder.get(left.source) ?? Number.MAX_SAFE_INTEGER) - (referenceOrder.get(right.source) ?? Number.MAX_SAFE_INTEGER));
    const relevantIds = new Set<string>([generatorNodeId, ...relevantEdges.flatMap((edge) => [edge.source, edge.target])]);
    const referenceIndexBySource = new Map(targetImageReferences.map((item) => [item.node.id, item.referenceIndex - 1 + imageReferenceContract.indexBase]));
    const videoReferenceIndexBySource = new Map(targetContract.orderedAssets.map((item, index) => [item.nodeId, index]));
    const videoAssetByNode = new Map(targetModeAssets.map((asset) => [asset.nodeId, asset]));
    const executionDocument = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
    const executionNode = executionDocument.nodes.find((node) => node.id === generatorNodeId);
    const graphPrompt = executionNode?.kind === "video-generator" || executionNode?.kind === "image-generator" ? compilePromptDocument(executionNode.prompt, executionNode.bindings).text : runtime.prompt;
    const serializedNodes: Array<{ id: string; type: string; data: Record<string, unknown> }> = nodes.filter((node) => relevantIds.has(node.id)).map((node) => {
      const materialized = materializedAssets[node.id];
      return {
        id: node.id,
        type: materialized ? "asset" : node.kind === "video" || node.kind === "image" ? "generator" : node.kind,
        data: materialized ? { kind: materialized.kind, assetId: materialized.id, label: materialized.filename, role: "reference" } : node.asset ? { kind: node.asset.media, assetId: node.asset.remoteId, role: referenceRoleForTarget(edges, generatorNodeId, node.id, node.asset.role), ...(output === "video" && videoAssetByNode.has(node.id) ? { role: graphRoleForVideoAsset(targetContract, videoAssetByNode.get(node.id)!) } : {}), label: node.asset.fileName, include_audio: referenceIncludesAudio(edges, generatorNodeId, node.id), ...(node.asset.media !== "audio" ? { voice_speaker: node.asset.voiceSpeaker, voice_subject: node.asset.voiceSubject } : {}), ...(output === "video" && videoReferenceIndexBySource.has(node.id) ? { reference_index: videoReferenceIndexBySource.get(node.id) } : referenceIndexBySource.has(node.id) ? { reference_index: referenceIndexBySource.get(node.id) } : {}) } : { output_type: node.kind === "video" || node.kind === "image" ? node.kind : undefined },
      };
    });
    const serializedEdges = relevantEdges.map((edge) => {
      const videoAsset = videoAssetByNode.get(edge.source);
      const role = output === "video" && videoAsset ? graphRoleForVideoAsset(targetContract, videoAsset) : edge.role;
      return { id: edge.id, source: edge.source, target: edge.target, role, data: { ...edge.data, role, ...(output === "video" && videoReferenceIndexBySource.has(edge.source) ? { reference_index: videoReferenceIndexBySource.get(edge.source) } : referenceIndexBySource.has(edge.source) ? { reference_index: referenceIndexBySource.get(edge.source) } : {}) } };
    });
    const promptNodeId = output === "image" ? "image-prompt" : "video-embedded-prompt";
    serializedNodes.push({ id: promptNodeId, type: "prompt", data: { prompt: graphPrompt } });
    if (output === "image") serializedEdges.unshift({ id: "image-prompt-input", source: promptNodeId, target: generatorNodeId, role: "prompt", data: { role: "prompt" } });
    else serializedEdges.unshift({ id: "video-embedded-prompt-input", source: promptNodeId, target: generatorNodeId, role: "prompt", data: { role: "prompt" } });
    return {
      nodes: serializedNodes,
      edges: serializedEdges,
    };
  }, [edges, imageNodeId, imageReferenceContract.indexBase, nodes, videoNodeId, viewport]);
  const requestParameters = useCallback((output: "video" | "image", targetNodeId?: string, materializedAssets: Record<string, LibraryAsset> = {}) => {
    const nodeId = targetNodeId ?? (output === "video" ? videoNodeId : imageNodeId);
    const runtime = generatorStatesRef.current[nodeId] ?? defaultGeneratorRuntime(output);
    const targetAssets = edges.filter((edge) => edge.target === nodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset) || node?.kind === "video" || node?.kind === "image");
    const targetModeAssets: VideoModeAsset[] = targetAssets.map((node) => { const materialized = materializedAssets[node.id]; return { nodeId: node.id, assetId: node.asset?.remoteId ?? materialized?.id, kind: node.asset?.media ?? materialized?.kind ?? (node.kind === "image" ? "image" : "video"), label: node.asset?.fileName ?? materialized?.filename ?? node.title, role: referenceRoleForTarget(edges, nodeId, node.id, node.asset?.role), includeAudio: referenceIncludesAudio(edges, nodeId, node.id) }; });
    const targetVideo = runtime.videoParams ?? DEFAULT_VIDEO_PARAMS;
    const targetImage = runtime.imageParams ?? DEFAULT_IMAGE_PARAMS;
    const targetContract = buildVideoDirectorContract(targetVideo.directorMode, targetVideo.sourceVideoId, targetModeAssets);
    const choices = profiles.filter((profile) => profile.output_type === output && (output === "image" || profile.compiler === targetContract.compiler));
    const imageCount = targetAssets.filter((node) => (node.asset?.media ?? materializedAssets[node.id]?.kind ?? (node.kind === "image" ? "image" : "video")) === "image").length;
    const resolvedProfile = output === "video" ? choices.find((profile) => profile.available && profile.sampling_mode === (runtime.profileId === "base20" ? "base" : "turbo4")) : runtime.profileId === "auto" ? choices.find((profile) => profile.available && imageProfileAcceptsReferenceCount(profile, imageCount)) : choices.find((profile) => profile.id === runtime.profileId);
    if (output === "video") return { aspect_ratio: targetVideo.aspectRatio, duration: targetVideo.duration, steps: targetVideo.steps, lora_strength: resolvedProfile?.sampling_mode === "base" ? 0 : targetVideo.loraStrength, denoise: targetVideo.denoise, seed: targetVideo.seed, ...videoDirectorPayload(targetContract) };
    const imageSize = imageDimensions(targetImage.quality, targetImage.aspectRatio);
    const acceptsNegativePrompt = !isFlux2Profile(resolvedProfile) && !isZImageProfile(resolvedProfile);
    return {
      aspect_ratio: targetImage.aspectRatio, width: imageSize.width, height: imageSize.height,
      steps: targetImage.steps, cfg: targetImage.cfg,
      ...(profileSupportsParameter(resolvedProfile, "lora_strength") ? { lora_strength: targetImage.loraStrength } : {}),
      ...(profileSupportsParameter(resolvedProfile, "denoise") ? { denoise: targetImage.denoise } : {}),
      ...(acceptsNegativePrompt ? { negative_prompt: targetImage.negativePrompt } : {}),
      seed: targetImage.seed,
    };
  }, [edges, imageNodeId, nodes, profiles, videoNodeId]);
  const profilePayload = useCallback((output: "video" | "image", targetNodeId?: string, materializedAssets: Record<string, LibraryAsset> = {}) => {
    const nodeId = targetNodeId ?? (output === "video" ? videoNodeId : imageNodeId);
    const runtime = generatorStatesRef.current[nodeId] ?? defaultGeneratorRuntime(output);
    const targetAssets = edges.filter((edge) => edge.target === nodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset) || node?.kind === "video" || node?.kind === "image");
    const targetModeAssets: VideoModeAsset[] = targetAssets.map((node) => { const materialized = materializedAssets[node.id]; return { nodeId: node.id, assetId: node.asset?.remoteId ?? materialized?.id, kind: node.asset?.media ?? materialized?.kind ?? (node.kind === "image" ? "image" : "video"), label: node.asset?.fileName ?? materialized?.filename ?? node.title, role: referenceRoleForTarget(edges, nodeId, node.id, node.asset?.role), includeAudio: referenceIncludesAudio(edges, nodeId, node.id) }; });
    const targetVideo = runtime.videoParams ?? DEFAULT_VIDEO_PARAMS;
    const targetContract = buildVideoDirectorContract(targetVideo.directorMode, targetVideo.sourceVideoId, targetModeAssets);
    const requested = output === "video" ? profiles.find((profile) => profile.output_type === "video" && profile.compiler === targetContract.compiler && profile.sampling_mode === (runtime.profileId === "base20" ? "base" : "turbo4") && profile.available) : runtime.profileId === "auto" ? undefined : profiles.find((profile) => profile.id === runtime.profileId);
    const imageCount = targetModeAssets.filter((asset) => asset.kind === "image").length;
    const profile = requested ?? profiles.find((profile) => profile.output_type === output && profile.available && (output === "image" ? imageProfileAcceptsReferenceCount(profile, imageCount) : profile.compiler === targetContract.compiler));
    return profile ? { profile_id: profile.id, profile_version: profile.version, profile_digest: profile.manifest_sha256 } : { profile_id: runtime.profileId };
  }, [edges, imageNodeId, nodes, profiles, videoNodeId]);

  useEffect(() => {
    const requestId = ++promptCompileRequestRef.current;
    const outputPrompt = promptForOutput(generator, prompt, imagePrompt);
    const outputParts = EMPTY_PARTS;
    if (!hasPromptForOutput(generator, prompt, imagePrompt, EMPTY_PARTS)) { setCompiledPrompt(""); setCompileError(""); setCompileState("idle"); return; }
    if (assets.some((node) => node.asset?.uploadState === "uploading")) { setCompiledPrompt(""); setCompileError(""); setCompileState("idle"); return; }
    setCompiledPrompt(""); setCompileError(""); setCompileState("loading");
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void fetch("/api/prompts/compile", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ output_type: generator, prompt: outputPrompt, parts: outputParts, parameters: requestParameters(generator), graph: graphSnapshot(generator), ...promptModePayload(generator, outputPrompt), ...profilePayload(generator) }),
      }).then(async (response) => {
        const data = await response.json().catch(() => undefined) as { prompt?: string; error?: { message?: string }; message?: string } | undefined;
        if (!response.ok || !data?.prompt) throw new Error(data?.error?.message ?? data?.message ?? `提示词编译失败 (${response.status})`);
        if (controller.signal.aborted || requestId !== promptCompileRequestRef.current) return;
        setCompiledPrompt(data.prompt); setCompileError(""); setCompileState("ready");
      }).catch((error) => {
        if (controller.signal.aborted || requestId !== promptCompileRequestRef.current || (error instanceof Error && error.name === "AbortError")) return;
        setCompiledPrompt(""); setCompileError(error instanceof Error ? error.message : "提示词编译失败"); setCompileState("error");
      });
    }, 350);
    return () => { window.clearTimeout(timer); controller.abort(); };
    // Nodes are included because changing a reference role changes the compiled labels.
  }, [assets, compileRetryToken, generator, graphSnapshot, imagePrompt, profilePayload, prompt, requestParameters, route, setCompileError, setCompileState, setCompiledPrompt]);

  function preflight(kind: "video" | "image", targetNodeId?: string, requireOutput = true, materializedAssets: Record<string, LibraryAsset> = {}) {
    const generatorNodeId = targetNodeId ?? (kind === "video" ? videoNodeId : imageNodeId);
    const runtime = generatorStatesRef.current[generatorNodeId] ?? defaultGeneratorRuntime(kind);
    const targetPrompt = runtime.prompt;
    const targetAssets = edges.filter((edge) => edge.target === generatorNodeId).map((edge) => nodes.find((node) => node.id === edge.source)).filter((node): node is StudioNode => Boolean(node?.asset) || node?.kind === "video" || node?.kind === "image");
    const targetImageRefs = targetAssets.filter((node) => (node.asset?.media ?? materializedAssets[node.id]?.kind ?? (node.kind === "image" ? "image" : "video")) === "image");
    const targetVideoParams = runtime.videoParams ?? DEFAULT_VIDEO_PARAMS;
    const targetModeAssets: VideoModeAsset[] = targetAssets.map((node) => { const materialized = materializedAssets[node.id]; return { nodeId: node.id, assetId: node.asset?.remoteId ?? materialized?.id, kind: node.asset?.media ?? materialized?.kind ?? (node.kind === "image" ? "image" : "video"), label: node.asset?.fileName ?? materialized?.filename ?? node.title, role: referenceRoleForTarget(edges, generatorNodeId, node.id, node.asset?.role), includeAudio: referenceIncludesAudio(edges, generatorNodeId, node.id) }; });
    const targetContract = buildVideoDirectorContract(targetVideoParams.directorMode, targetVideoParams.sourceVideoId, targetModeAssets);
    const targetDocument = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport).nodes.find((node) => node.id === generatorNodeId);
    const targetBindings = targetDocument?.kind === "video-generator" || targetDocument?.kind === "image-generator" ? targetDocument.bindings : [];
    if (!targetPrompt.trim()) return kind === "image" ? "请在 Image Generation 节点填写图片正向提示词；Negative Prompt 不能代替正向提示词" : "请先填写视频提示词";
    if (new Set(targetBindings.map((binding) => binding.sourceNodeId)).size > H3_REFERENCE_BUDGET) return `H3 单任务最多绑定 ${H3_REFERENCE_BUDGET} 个参考文件；请移除素材`;
    if (targetBindings.filter((binding) => binding.kind === "audio").length > 3) return "Audio 参考最多 3 个；请关闭配对音轨或移除音频";
    if (targetAssets.some((node) => node.asset?.uploadState === "uploading")) return "请等待素材上传完成";
    if (edges.some((edge) => edge.target === generatorNodeId && nodes.find((node) => node.id === edge.source)?.asset?.uploadState === "error")) return "工作流包含上传失败的素材，请删除或重新上传";
    if (requireOutput && !edges.some((edge) => edge.source === generatorNodeId && nodes.find((node) => node.id === edge.target)?.kind === "output")) return `请先连接 ${kind === "video" ? "H3 Video" : "Image Generation"} → Output`;
    const imageEdges = edges.filter((edge) => edge.target === generatorNodeId && edge.source !== "image-prompt");
    const rawImageReferences = imageEdges.map((edge) => ({ edge, node: nodes.find((node) => node.id === edge.source) }));
    if (kind === "image" && rawImageReferences.some((item) => !item.node || ((item.node.kind !== "image" || !materializedAssets[item.node.id]) && (item.node.kind !== "asset" || item.node.asset?.media !== "image" || !item.node.asset.remoteId)))) return "图片生成存在悬空或非图片参考；请断开无效连线后重试";
    const imageAssetIds = rawImageReferences.map((item) => item.node?.asset?.remoteId).filter((id): id is string => Boolean(id));
    if (kind === "image" && new Set(imageAssetIds).size !== imageAssetIds.length) return "同一张远程图片不能重复作为多个参考";
    if (kind === "video" && targetContract.errors.length) return targetContract.errors[0];
    if (kind === "video" && targetContract.compiler === "h3_fl") {
      const roles = targetContract.orderedAssets.map((asset) => graphRoleForVideoAsset(targetContract, asset));
      if (new Set(roles).size !== roles.length) return "FL2VA 的首帧/尾帧角色不能重复";
    }
    if (kind === "video" && targetAssets.some((node) => node.asset?.media === "video" && referenceIncludesAudio(edges, generatorNodeId, node.id) && node.asset.mediaMeta?.has_audio !== true)) return "已勾选视频音轨，但该素材没有可用音轨或缺少媒体元数据";
    const selection = runtime.profileId;
    const compiler = targetContract.compiler;
    const choices = profiles.filter((profile) => profile.output_type === kind && (kind === "image" || profile.compiler === compiler));
    const compatible = choices.filter((profile) => kind === "video" || imageProfileAcceptsReferenceCount(profile, targetImageRefs.length));
    const selectedProfile = kind === "video" ? compatible.find((profile) => profile.available && profile.sampling_mode === (selection === "base20" ? "base" : "turbo4")) : selection === "auto" ? compatible.find((profile) => profile.available) : choices.find((profile) => profile.id === selection);
    if (!profiles.length) return "尚未读取到远程模型能力，请检查后端连接";
    if (!selectedProfile) return "当前模型 Profile 与已连接素材不兼容，请重新选择";
    if (!selectedProfile.available) return `模型 Profile 不可用：${[...(selectedProfile.missing_model_files ?? []), ...(selectedProfile.missing_models ?? []), ...(selectedProfile.missing_nodes ?? []), ...(selectedProfile.missing_options ?? [])].join("、")}`;
    if (kind === "image") {
      const referencePolicy = imageReferencePolicy(selectedProfile);
      if (targetImageRefs.length < referencePolicy.min || targetImageRefs.length > referencePolicy.max) return `${selectedProfile.display_name} 允许 ${referencePolicy.min}..${referencePolicy.max} 张参考图，当前为 ${targetImageRefs.length} 张`;
      const danglingPromptReference = referencePolicy.ordered && selectedProfile.reference_contract?.prompt_reference_format
        ? promptImageReferenceNumbers(targetPrompt).find((number) => number > targetImageRefs.length)
        : undefined;
      if (danglingPromptReference) return `提示词引用了图${danglingPromptReference}，但当前只连接了 ${targetImageRefs.length} 张参考图`;
      const indices = targetImageRefs.map((_, index) => index + 1);
      if (new Set(indices).size !== indices.length || indices.some((index, offset) => index !== offset + 1)) return "多图参考编号重复或不连续，请调整顺序后重试";
    }
    const targetCompile = compileByNode[generatorNodeId];
    if (targetCompile?.state === "error") return `最终提示词校验失败：${targetCompile.error || "请重新校验后再试"}`;
    return "";
  }
  const persistDownstreamInvalidation = useCallback((sourceNodeId: string) => {
    const transaction = invalidateDownstreamGenerators(canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport), sourceNodeId);
    if (!transaction.invalidatedNodeIds.length) return;
    const revisions = new Map(transaction.document.nodes.flatMap((node) => node.kind === "video-generator" || node.kind === "image-generator" ? [[node.id, node.configRevision] as const] : []));
    setGeneratorStates((current) => {
      const next = { ...current };
      for (const nodeId of transaction.invalidatedNodeIds) {
        const runtime = next[nodeId]; const revision = revisions.get(nodeId);
        if (runtime && revision !== undefined) next[nodeId] = { ...runtime, configRevision: revision };
      }
      generatorStatesRef.current = next;
      return next;
    });
  }, [edges, nodes, viewport]);
  async function runGeneratorNode(output: "video" | "image", nodeId: string, materializedAssets: Record<string, LibraryAsset>, requireOutput: boolean): Promise<Job> {
    const runtime = generatorStatesRef.current[nodeId] ?? defaultGeneratorRuntime(output);
    const error = preflight(output, nodeId, requireOutput, materializedAssets);
    if (error) { setJobForNode(nodeId, { status: "failed", progress: 0, message: error, media: output }); throw new Error(error); }
    const outputPrompt = runtime.prompt;
    const outputParts = EMPTY_PARTS;
    const createdAt = new Date().toISOString();
    setJobForNode(nodeId, { status: "submitting", progress: 2, message: "正在验证并匹配工作流…", media: output, prompt: outputPrompt, createdAt });
    try {
      const parameters = requestParameters(output, nodeId, materializedAssets);
      const requestId = crypto.randomUUID().replaceAll("-", "");
      const resolvedProfile = runtime.profileId === "auto" ? undefined : profiles.find((profile) => profile.id === runtime.profileId);
      const acceptsNegativePrompt = !isFlux2Profile(resolvedProfile) && !isZImageProfile(resolvedProfile);
      const targetImageParams = runtime.imageParams ?? DEFAULT_IMAGE_PARAMS;
      const response = await fetch("/api/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: requestId, output_type: output, prompt: outputPrompt, parts: outputParts, ...(output === "image" && acceptsNegativePrompt ? { negative_prompt: targetImageParams.negativePrompt } : {}), parameters, graph: graphSnapshot(output, nodeId, materializedAssets), ...promptModePayload(output, outputPrompt), ...profilePayload(output, nodeId, materializedAssets) }) });
      const data = await response.json().catch(() => ({})) as { id?: string; job_id?: string; prompt_id?: string; parameters?: Record<string, unknown>; workflow_sha256?: string; workflow_evidence?: Record<string, unknown>; error?: { message?: string }; message?: string };
      if (!response.ok) throw new Error(data.error?.message ?? data.message ?? `提交失败 (${response.status})`);
      const id = data.id ?? data.job_id ?? data.prompt_id; if (!id) throw new Error("服务未返回任务 ID");
      orchestratedJobsRef.current.add(id);
      const queued: Job = { id, status: "queued", progress: 0, message: "已进入生成队列", media: output, snapshot: snapshotText(data.parameters), parameters: serverParameters(data.parameters), ...workflowReceiptFields(data), prompt: outputPrompt, createdAt };
      setJobForNode(nodeId, queued);
      recordTrustedJob(queued);
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        const statusResponse = await fetch(`/api/status?id=${encodeURIComponent(id)}`, { cache: "no-store" });
        const statusData = await statusResponse.json().catch(() => ({})) as { status?: string; state?: string; progress?: number; message?: string; parameters?: Record<string, unknown>; workflow_sha256?: string; workflow_evidence?: Record<string, unknown>; prompt?: string; error?: string | { message?: string }; preview_url?: string; thumbnail_url?: string; download_url?: string; url?: string };
        if (!statusResponse.ok) throw new Error(typeof statusData.error === "object" ? statusData.error.message : statusData.message ?? `状态查询失败 (${statusResponse.status})`);
        const status = String(statusData.status ?? statusData.state ?? "").toLowerCase();
        if (["failed", "error", "cancelled", "canceled"].includes(status)) throw new Error(typeof statusData.error === "string" ? statusData.error : statusData.error?.message ?? statusData.message ?? "生成失败");
        if (["completed", "success", "done"].includes(status)) {
          const previewUrl = currentOriginApiUrl(statusData.preview_url ?? statusData.url); const downloadUrl = currentOriginApiUrl(statusData.download_url ?? statusData.url);
          if (!previewUrl && !downloadUrl) throw new Error("任务已完成，但服务未返回媒体 URL");
          const completed: Job = { ...queued, status: "completed", progress: 100, message: "生成完成", previewUrl: previewUrl ?? downloadUrl, thumbnailUrl: currentOriginApiUrl(statusData.thumbnail_url, `/api/jobs/${encodeURIComponent(id)}/thumbnail?index=0`), downloadUrl: downloadUrl ?? previewUrl, snapshot: snapshotText(statusData.parameters) ?? queued.snapshot, parameters: serverParameters(statusData.parameters) ?? queued.parameters, ...workflowReceiptFields(statusData), prompt: statusData.prompt ?? queued.prompt };
          updateGenerator(nodeId, (current) => { const result: NodeResult = { id, mediaKind: output, createdAt: Date.now(), previewUrl: completed.previewUrl, thumbnailUrl: completed.thumbnailUrl, downloadUrl: completed.downloadUrl, prompt: completed.prompt, revision: current.configRevision, receipt: completed.parameters as Record<string, unknown> | undefined }; return { ...current, job: completed, lastSuccessfulRevision: current.configRevision, resultVersions: [result, ...current.resultVersions.filter((item) => item.id !== id)].slice(0, 20) }; });
          recordTrustedJob(completed);
          persistDownstreamInvalidation(nodeId);
          orchestratedJobsRef.current.delete(id);
          return completed;
        }
        setJobForNode(nodeId, (current) => ({ ...current, status: status === "queued" ? "queued" : "running", progress: clampProgress(statusData.progress), message: statusData.message ?? "生成中…" }));
      }
    } catch (caught) {
      const failed: Job = { status: "failed", progress: 0, message: caught instanceof Error ? caught.message : "提交任务失败", media: output, prompt: outputPrompt, createdAt };
      const activeId = generatorStatesRef.current[nodeId]?.job.id; if (activeId) orchestratedJobsRef.current.delete(activeId);
      setJobForNode(nodeId, failed); throw caught;
    }
  }
  async function generate(kind?: "video" | "image", targetNodeId?: string) {
    const output = kind ?? generator;
    const nodeId = targetNodeId ?? (output === "video" ? videoNodeId : imageNodeId);
    const document = canvasDocumentFromState(nodes, edges, generatorStatesRef.current, viewport);
    const plan = buildGeneratorExecutionPlan(document, nodeId);
    if (plan.issues.length) { const message = plan.issues[0].message; setNotice(message); setJobForNode(nodeId, { status: "failed", progress: 0, message, media: output }); return; }
    const materializedAssets: Record<string, LibraryAsset> = {};
    try {
      for (const step of plan.steps) {
        const stepOutput = step.kind === "video-generator" ? "video" : "image";
        const completedStep = step.action === "run" ? await runGeneratorNode(stepOutput, step.nodeId, materializedAssets, step.nodeId === nodeId) : generatorStatesRef.current[step.nodeId]?.job;
        if (step.nodeId !== nodeId) {
          const jobId = completedStep?.id ?? step.result?.id;
          if (!jobId) throw new Error(`上游节点 ${step.nodeId} 没有可物化的结果`);
          materializedAssets[step.nodeId] = await materializeJobOutput(jobId);
        }
      }
      setNotice(plan.steps.length > 1 ? `已按拓扑顺序完成 ${plan.steps.length} 个生成节点。` : "生成任务已完成。");
    } catch (error) {
      const message = `依赖链已停止：${error instanceof Error ? error.message : "上游生成失败"}`;
      setNotice(message);
      if (generatorStatesRef.current[nodeId]?.job.status === "idle") setJobForNode(nodeId, { status: "failed", progress: 0, message, media: output });
    }
  }
  async function cancelJob(targetNodeId = generator === "image" ? imageNodeId : videoNodeId) {
    const targetJob = generatorStatesRef.current[targetNodeId]?.job;
    if (!targetJob?.id || !["submitting", "queued", "running"].includes(targetJob.status)) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(targetJob.id)}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const data = await response.json() as { error?: { message?: string } };
      if (!response.ok) throw new Error(data.error?.message ?? `取消失败 (${response.status})`);
      setJobForNode(targetNodeId, (current) => ({ ...current, status: "failed", progress: 100, message: "任务已取消" }));
    } catch (error) { setNotice(error instanceof Error ? error.message : "取消任务失败"); }
  }

  useEffect(() => {
    const activeJobs = Object.entries(generatorStatesRef.current).filter(([, runtime]) => runtime.job.id && trustedJobIdsRef.current.has(runtime.job.id) && !orchestratedJobsRef.current.has(runtime.job.id) && ["queued", "running"].includes(runtime.job.status));
    if (!activeJobs.length) return;
    let stopped = false; let timer = 0;
    const poll = async () => {
      await Promise.all(activeJobs.map(async ([targetNodeId, runtime]) => {
       try {
        const targetJob = runtime.job;
        const response = await fetch(`/api/status?id=${encodeURIComponent(targetJob.id!)}`, { cache: "no-store" });
        const data = await response.json().catch(() => ({})) as { status?: string; state?: string; progress?: number; message?: string; parameters?: Record<string, unknown>; workflow_sha256?: string; workflow_evidence?: Record<string, unknown>; prompt?: string; error?: string | { code?: string; message?: string }; preview_url?: string; thumbnail_url?: string; download_url?: string; url?: string };
        if (stopped) return;
        const status = (data.status ?? data.state ?? "").toLowerCase(); const errorCode = typeof data.error === "object" ? data.error.code : "";
        if (response.status === 404 || status === "not_found" || errorCode === "not_found") {
          setJobForNode(targetNodeId, (current) => ({ ...current, status: "failed", progress: 0, message: "任务不存在或已过期" })); return;
        }
        if (!response.ok) throw new Error(typeof data.error === "object" ? data.error.message : data.message ?? `状态查询失败 (${response.status})`);
        pollFailuresRef.current = 0;
        const previewUrl = currentOriginApiUrl(data.preview_url ?? data.url); const downloadUrl = currentOriginApiUrl(data.download_url ?? data.url);
        if (["completed", "success", "done"].includes(status)) {
          if (!previewUrl && !downloadUrl) throw new Error("任务已完成，但服务未返回可预览或下载的媒体 URL");
          const completedJob: Job = { ...targetJob, status: "completed", progress: 100, message: "生成完成", previewUrl: previewUrl ?? downloadUrl, thumbnailUrl: currentOriginApiUrl(data.thumbnail_url, targetJob.id ? `/api/jobs/${encodeURIComponent(targetJob.id)}/thumbnail?index=0` : undefined), downloadUrl: downloadUrl ?? previewUrl, snapshot: snapshotText(data.parameters) ?? targetJob.snapshot, parameters: serverParameters(data.parameters) ?? targetJob.parameters, ...workflowReceiptFields(data), prompt: data.prompt ?? targetJob.prompt };
          updateGenerator(targetNodeId, (current) => {
            const result: NodeResult = { id: completedJob.id ?? crypto.randomUUID(), mediaKind: current.kind, createdAt: Date.now(), previewUrl: completedJob.previewUrl, thumbnailUrl: completedJob.thumbnailUrl, downloadUrl: completedJob.downloadUrl, prompt: completedJob.prompt, revision: current.configRevision, receipt: completedJob.parameters as Record<string, unknown> | undefined };
            return { ...current, job: completedJob, lastSuccessfulRevision: current.configRevision, resultVersions: [result, ...current.resultVersions.filter((item) => item.id !== result.id)].slice(0, 20) };
          });
          recordTrustedJob(completedJob);
          persistDownstreamInvalidation(targetNodeId);
          return;
        }
        if (["failed", "error", "cancelled", "canceled"].includes(status)) {
          setJobForNode(targetNodeId, (current) => ({ ...current, status: "failed", progress: clampProgress(data.progress), message: typeof data.error === "string" ? data.error : data.error?.message ?? data.message ?? "生成失败", parameters: serverParameters(data.parameters) ?? current.parameters, ...workflowReceiptFields(data), prompt: data.prompt ?? current.prompt })); return;
        }
        setJobForNode(targetNodeId, (current) => ({ ...current, status: status === "queued" ? "queued" : "running", progress: clampProgress(data.progress), message: data.message ?? (status === "queued" ? "排队中…" : "H3 正在生成…"), parameters: serverParameters(data.parameters) ?? current.parameters, ...workflowReceiptFields(data), prompt: data.prompt ?? current.prompt }));
      } catch (caught) {
        if (stopped) return;
        pollFailuresRef.current += 1;
        if (pollFailuresRef.current >= 5) setJobForNode(targetNodeId, (current) => ({ ...current, status: "failed", message: `连续 5 次无法查询状态：${caught instanceof Error ? caught.message : "网络错误"}` }));
        else {
          setJobForNode(targetNodeId, (current) => ({ ...current, message: `连接暂时中断，正在重试 (${pollFailuresRef.current}/5)…` }));
        }
      }
      }));
      if (!stopped) timer = window.setTimeout(poll, 1500);
    };
    timer = window.setTimeout(poll, 600); return () => { stopped = true; window.clearTimeout(timer); };
  }, [generatorStates, persistDownstreamInvalidation, recordTrustedJob, setJobForNode, updateGenerator]);

  const resolvedContract = useMemo<GenerationParameters | undefined>(() => {
    if (!activeProfile) return undefined;
    if (generator === "image") {
      const imageSize = imageDimensions(imageParams.quality, imageParams.aspectRatio);
      const flux2 = isFlux2Profile(activeProfile);
      const zImage = isZImageProfile(activeProfile);
      const imageLora = profileSupportsParameter(activeProfile, "lora_strength");
      const imageDenoise = profileSupportsParameter(activeProfile, "denoise");
      return {
        output_type: "image", profile_id: activeProfile.id, profile_version: activeProfile.version,
        profile_digest: activeProfile.manifest_sha256, mode: imageReferenceCount > 1 ? "multi-image-reference" : imageInput ? "image-to-image" : "text-to-image",
        width: imageSize.width, height: imageSize.height,
        steps: imageParams.steps,
        sampler: flux2 ? "euler" : zImage ? "res_multistep" : activeProfile.compiler.startsWith("qwen_image") ? "euler" : "euler_ancestral",
        scheduler: flux2 ? "flux2" : zImage || activeProfile.compiler.startsWith("qwen_image") ? "simple" : "normal",
        ...(imageLora ? { lora: "Profile 配套 LoRA", lora_strength: imageParams.loraStrength } : {}),
        ...(imageDenoise ? { denoise: imageInput ? imageParams.denoise : 1 } : {}), seed: imageParams.seed,
      };
    }
    const turbo = activeProfile.sampling_mode === "turbo4";
    return {
      output_type: "video", profile_id: activeProfile.id, profile_version: activeProfile.version,
      profile_digest: activeProfile.manifest_sha256, sampling_mode: activeProfile.sampling_mode,
      ...videoDirectorPayload(videoDirectorContract),
      width: videoParams.aspectRatio === "16:9" ? 1344 : 768, height: videoParams.aspectRatio === "16:9" ? 768 : 1344,
      duration_actual: effectiveDuration(videoParams.duration), frames: Math.round(effectiveDuration(videoParams.duration) * 24),
      steps: videoParams.steps,
      sampler: turbo ? "sa_solver" : "res_multistep", scheduler: "simple",
      lora: turbo ? "配套 Turbo LoRA" : null, lora_strength: turbo ? videoParams.loraStrength : 0,
      denoise: videoParams.denoise, seed: videoParams.seed,
    };
  }, [activeProfile, generator, imageInput, imageParams, imageReferenceCount, videoDirectorContract, videoParams]);
  const renderDocument = useMemo(() => canvasDocumentFromState(nodes, edges, generatorStates, viewport), [edges, generatorStates, nodes, viewport]);

  return <main ref={uiRootRef} className="studio-shell" data-i18n-ready="false" data-ui-language={uiLanguage}>
    <header className="topbar">
      <div className="brand"><span className="brand-mark">H3</span><div><strong>MiniMax H3 Video Studio</strong><small>MiniMax H3 + ComfyUI</small></div></div>
      <div className="canvas-tabs-wrap">
        <div className="project-title canvas-workspace-status"><span className="status-dot" /> Local workflow <span className="saved">自动保存</span></div>
        <div className="canvas-tablist" role="tablist" aria-label="画布标签">
          {canvasWorkspace?.canvases.map((canvas) => <div key={canvas.id} className={`canvas-tab-shell ${canvas.id === canvasWorkspace.activeCanvasId ? "active" : ""}`}>
            <button id={canvasTabElementId(canvas.id)} type="button" role="tab" aria-selected={canvas.id === canvasWorkspace.activeCanvasId} aria-controls="studio-canvas-panel" tabIndex={canvas.id === canvasWorkspace.activeCanvasId ? 0 : -1} onKeyDown={(event) => handleCanvasTabKeyDown(event, canvas.id)} onClick={() => selectCanvasTab(canvas.id)} title={canvas.title}><span>{canvas.title}</span></button>
            {canvasWorkspace.canvases.length > 1 && <button type="button" className="canvas-tab-close" aria-label={`关闭并移除 ${canvas.title}`} title="关闭并从本地工作区移除" onClick={() => removeCanvasTab(canvas.id)}>×</button>}
          </div>)}
          <button type="button" className="canvas-tab-add" aria-label="新建画布标签" onClick={createCanvasTab}>＋</button>
        </div>
      </div>
      <div className="top-actions"><button className="language-toggle" data-i18n-ignore type="button" onClick={toggleUiLanguage} aria-label={uiLanguage === "en" ? "Switch interface to Chinese" : "将界面切换为英文"} title={uiLanguage === "en" ? "中文界面" : "English interface"}><span className={uiLanguage === "en" ? "active" : ""}>EN</span><i aria-hidden="true">/</i><span className={uiLanguage === "zh-CN" ? "active" : ""}>中文</span></button><button className="ghost-button" type="button" onClick={createCanvasTab}>新建</button><button className="ghost-button" type="button" onClick={() => inputRef.current?.click()}><Icon>＋</Icon> 添加素材</button></div>
    </header>
    <div className="workspace">
      <aside className="left-rail" aria-label="工作区导航">
        <button className={`rail-button ${railPanel === null ? "active" : ""}`} type="button" aria-pressed={railPanel === null} onClick={() => setRailPanel(null)}><Icon>◇</Icon><span>画布</span></button>
        <button className={`rail-button ${railPanel === "assets" ? "active" : ""}`} type="button" aria-controls="asset-library-drawer" aria-expanded={railPanel === "assets"} onClick={() => setRailPanel((current) => current === "assets" ? null : "assets")}><Icon>▣</Icon><span>资产</span></button>
        <button className={`rail-button ${railPanel === "results" ? "active" : ""}`} type="button" aria-controls="result-library-drawer" aria-expanded={railPanel === "results"} onClick={() => setRailPanel((current) => current === "results" ? null : "results")}><Icon>✓</Icon><span>结果</span></button>
        <button ref={timelineRailButtonRef} className={`rail-button ${railPanel === "timeline" ? "active" : ""}`} type="button" aria-controls="video-timeline-drawer" aria-expanded={railPanel === "timeline"} onClick={() => setRailPanel((current) => current === "timeline" ? null : "timeline")}><Icon>☷</Icon><span>长视频</span></button>
        <button className="rail-button" type="button" onClick={() => inputRef.current?.click()}><Icon>＋</Icon><span>上传</span></button>
        <div className="rail-spacer"/><button className="rail-button" type="button" title="双击连线可删除"><Icon>?</Icon><span>帮助</span></button>
      </aside>
      {railPanel === "assets" && <AssetLibrary items={assetPickerTarget ? assetLibrary.filter((item) => item.kind === assetPickerTarget.media) : assetLibrary} folders={assetFolders} state={assetLibraryState} onAdd={(item, connectTarget) => {
        if (!assetPickerTarget) return addLibraryAsset(item, connectTarget);
        const targetKind = nodes.find((node) => node.id === assetPickerTarget.nodeId)?.kind;
        const added = addLibraryAsset(item, targetKind === "image" ? "image" : "video", assetPickerTarget.nodeId, assetPickerTarget.slot);
        if (added) setAssetPickerTarget(undefined);
        return added;
      }} onUpload={() => inputRef.current?.click()} onRefresh={() => { void loadAssetLibrary(); void loadAssetFolders(); }} onCreateFolder={async (name) => {
        try {
          const folder = await createLibraryFolder(name);
          setAssetFolders((current) => [...current, folder]);
          setNotice(`已创建文件夹：${folder.name}`);
        } catch (error) {
          setNotice(`创建文件夹失败：${error instanceof Error ? error.message : "未知错误"}`);
        }
      }} onRename={async (item, displayName) => {
        try {
          const updated = await updateLibraryAsset(item.id, { display_name: displayName });
          setAssetLibrary((current) => current.map((asset) => asset.id === updated.id ? updated : asset));
          setNotice(`已将资产改名为：${updated.filename}`);
        } catch (error) {
          setNotice(`资产改名失败：${error instanceof Error ? error.message : "未知错误"}`);
        }
      }} onMove={async (item, folderId) => {
        try {
          const updated = await updateLibraryAsset(item.id, { folder_id: folderId || null });
          setAssetLibrary((current) => current.map((asset) => asset.id === updated.id ? updated : asset));
          setNotice(`已移动资产：${updated.filename}`);
        } catch (error) {
          setNotice(`移动资产失败：${error instanceof Error ? error.message : "未知错误"}`);
        }
      }} onPin={pinAsset} onDeleteFolder={async (folder) => {
        try {
          const moved = await deleteLibraryFolder(folder.id);
          await Promise.all([loadAssetLibrary(), loadAssetFolders()]);
          setNotice(`已删除文件夹：${folder.name}${moved.assetsMoved || moved.subfoldersMoved ? `（${moved.assetsMoved} 个资产、${moved.subfoldersMoved} 个子文件夹已移到上一级）` : ""}`);
        } catch (error) {
          setNotice(`删除文件夹失败：${error instanceof Error ? error.message : "未知错误"}`);
          throw error;
        }
      }} onDelete={deleteAssetFromLibrary} onClose={() => { setAssetPickerTarget(undefined); setRailPanel(null); }}/>} {/* typed slot picker */}
      {railPanel === "results" && <ResultLibrary
        jobs={jobHistory}
        derivedResults={derivedResults}
        state={jobHistoryState}
        verified={jobHistoryInstanceVerified}
        error={jobHistoryError}
        pageError={jobHistoryPageError}
        savedResultAssets={confirmedSavedResultAssets}
        currentId={job.id}
        hasMore={Boolean(jobHistoryCursor)}
        onRetry={() => loadJobHistory(true)}
        onLoadMore={() => loadJobHistory(false, jobHistoryCursor)}
        onSelect={previewResultOnCanvas}
        onAdd={addResultToCanvas}
        onSave={saveResultToAssets}
        onDelete={deleteResult}
        onPin={pinJobResult}
        onResume={resumeResultJob}
        onAddDerived={async (derived) => { addDerivedResultToCanvas(derived); }}
        onSaveDerived={saveDerivedResultToAssets}
        onDeleteDerived={deleteDerivedResult}
        onPinDerived={pinDerivedResult}
        onClose={() => setRailPanel(null)}
      />}
      {railPanel === "timeline" && <VideoTimeline assets={assetLibrary} results={jobHistory} profiles={profiles} onUploadVideo={uploadTimelineVideo} onImportResult={importJobOutput} onAssetCreated={handleTimelineAssetCreated} onResultCreated={handleTimelineResultCreated} onClose={() => setRailPanel(null)}/>}
      <section id="studio-canvas-panel" role="tabpanel" className="canvas-wrap" aria-label="节点画布" aria-labelledby={canvasWorkspace ? canvasTabElementId(canvasWorkspace.activeCanvasId) : undefined} aria-hidden={railPanel === "timeline" ? true : undefined} inert={railPanel === "timeline" ? true : undefined} style={{ "--canvas-grid-size": `${28 * viewport.zoom}px`, "--canvas-grid-x": `${viewport.x}px`, "--canvas-grid-y": `${viewport.y}px` } as CSSProperties}>
        <div className={`drop-hint ${dragOver ? "visible" : ""}`}>松开以添加图片、视频或音频</div>
        <div className="canvas-scroll" ref={canvasViewportRef} onWheel={handleCanvasWheel} onPointerDown={startCanvasPan} onPointerMove={moveCanvasPan} onPointerUp={finishCanvasPan} onPointerCancel={finishCanvasPan} onContextMenu={openCanvasContextMenu} onDragOver={handleCanvasDragOver} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragOver(false); }} onDrop={handleDrop}>
          {/* A graph canvas is an application-style composite widget, not a button. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
          <div className="node-canvas" ref={canvasRef} role="application" tabIndex={0} aria-label="节点画布。右键新建，空白区域左键拖动平移，Ctrl 加滚轮缩放" style={{ transform: `translate3d(${viewport.x}px, ${viewport.y}px, 0) scale(${viewport.zoom})` }} onKeyDown={openCanvasContextMenuFromKeyboard}>
          <svg className="wires" width="1" height="1" aria-hidden="true"><defs><linearGradient id="wire-gradient"><stop stopColor="#8576ff"/><stop offset="1" stopColor="#3ed6c0"/></linearGradient></defs>{edges.map((edge) => { const from = nodes.find((node) => node.id === edge.source); const to = nodes.find((node) => node.id === edge.target); if (!from || !to) return null; const fromPoint = endpoint(from, "out", nodePortAnchors); const toPoint = endpoint(to, "in", nodePortAnchors); return <g key={edge.id} className="wire-group" onDoubleClick={() => disconnectEdge(edge.id)}><path className="wire-hit" d={curve(fromPoint, toPoint)}/><path className="wire" d={curve(fromPoint, toPoint)}/></g>; })}{connecting && connectionPointer && (() => { const from = nodes.find((node) => node.id === connecting); return from ? <path className="wire wire-preview" d={curve(endpoint(from, "out", nodePortAnchors), connectionPointer)}/> : null; })()}</svg>
          {nodes.map((node) => {
            const nodeRuntime = generatorStates[node.id] ?? (node.kind === "image" ? defaultGeneratorRuntime("image") : defaultGeneratorRuntime("video"));
            const nodePrompt = nodeRuntime.prompt;
            const nodeVideoParams = nodeRuntime.videoParams ?? DEFAULT_VIDEO_PARAMS;
            const nodeImageParams = nodeRuntime.imageParams ?? DEFAULT_IMAGE_PARAMS;
            const outputPlan = node.kind === "output" ? buildOutputCollectionPlan(renderDocument, node.id) : undefined;
            const sourceGeneratorIds = outputPlan?.sources.map((source) => source.sourceGeneratorId) ?? [];
            const sourceGeneratorId = sourceGeneratorIds[0];
            const nodeJob = sourceGeneratorId ? generatorStates[sourceGeneratorId]?.job ?? IDLE_JOB : nodeRuntime.job;
            const nodeBusy = ["submitting", "queued", "running"].includes(nodeJob.status);
            const nodeCompile = compileByNode[node.id] ?? { prompt: "", state: "idle" as const, error: "" };
            const documentNode = renderDocument.nodes.find((item) => item.id === node.id);
            const nodeBindings = documentNode?.kind === "video-generator" || documentNode?.kind === "image-generator" ? documentNode.bindings : [];
            const nodeConnected = edges.filter((edge) => edge.target === node.id).map((edge, index) => ({ edge, index, source: nodes.find((item) => item.id === edge.source) })).filter((item): item is { edge: Edge; index: number; source: StudioNode } => Boolean(item.source?.asset)).sort((left, right) => (left.edge.data.reference_index ?? left.index) - (right.edge.data.reference_index ?? right.index)).map((item) => item.source);
            const nodeModeAssets: VideoModeAsset[] = nodeConnected.map((item) => ({ nodeId: item.id, assetId: item.asset?.remoteId, kind: item.asset!.media, label: item.asset!.fileName, role: referenceRoleForTarget(edges, node.id, item.id, item.asset!.role), includeAudio: referenceIncludesAudio(edges, node.id, item.id) }));
            const nodeContract = buildVideoDirectorContract(nodeVideoParams.directorMode, nodeVideoParams.sourceVideoId, nodeModeAssets);
            const nodeSources = nodeModeAssets.filter((item) => item.kind === "video" && Boolean(item.assetId));
            const nodeLabels = videoDirectorReferenceLabels(nodeContract);
            const nodeVideoProfileChoices = profiles.filter((profile) => profile.output_type === "video" && profile.compiler === nodeContract.compiler);
            const nodeImageProfileChoices = profiles.filter((profile) => profile.output_type === "image" && imageProfileAcceptsReferenceCount(profile, nodeConnected.filter((item) => item.asset?.media === "image").length));
            const nodeVideoProfile = nodeVideoProfileChoices.find((profile) => profile.available && profile.sampling_mode === (nodeRuntime.profileId === "base20" ? "base" : "turbo4"));
            const nodeImageProfile = nodeRuntime.profileId === "auto" ? nodeImageProfileChoices.find((profile) => profile.available) : nodeImageProfileChoices.find((profile) => profile.id === nodeRuntime.profileId);
            const updateNodeVideo = (action: SetStateAction<VideoParams>) => updateGenerator(node.id, (current) => ({ ...current, videoParams: applyStateAction(current.videoParams ?? DEFAULT_VIDEO_PARAMS, action), configRevision: current.configRevision + 1 }));
            const updateNodeImage = (action: SetStateAction<ImageParams>) => updateGenerator(node.id, (current) => ({ ...current, imageParams: applyStateAction(current.imageParams ?? DEFAULT_IMAGE_PARAMS, action), configRevision: current.configRevision + 1 }));
            return <article key={node.id} className={`studio-node ${node.kind} ${selectedId === node.id ? "selected" : ""}`} data-node-id={node.id} data-selected={selectedId === node.id} aria-label={`${node.title} 节点`} style={{ left: node.position.x, top: node.position.y }} onContextMenu={(event) => openNodeContextMenu(event, node)} onDragStart={(event) => event.preventDefault()} onPointerDownCapture={(event) => { if (event.button === 0) { setSelectedId(node.id); canvasInteractionActiveRef.current = true; } }} onPointerDown={(event) => startDrag(event, node)}>
            {node.kind !== "asset" && <button type="button" className="port input-port" aria-label={`连接到 ${node.title}`} onPointerUp={(event) => finishConnection(event, node.id)} onClick={(event) => { event.stopPropagation(); if (event.detail === 0) connectTo(node.id); }}/>} {/* input port */}
            {node.kind !== "output" && <button type="button" className={`port output-port ${connecting === node.id ? "connecting" : ""}`} aria-label={`从 ${node.title} 开始连接`} onPointerDown={(event) => beginConnection(event, node)} onClick={(event) => { event.stopPropagation(); if (event.detail === 0) beginConnectionFromKeyboard(node); }}/>} {/* output port */}
            <header className="node-header"><div className={`node-badge ${node.kind}`}><Icon>{node.kind === "video" ? "▶" : node.kind === "image" ? "▧" : node.kind === "output" ? "✓" : "◆"}</Icon></div><div><strong>{node.title}</strong><small>{node.kind === "asset" ? node.asset?.media.toUpperCase() : node.kind === "video" ? "MiniMax H3 Video + Audio" : node.kind === "image" ? "High-quality Text / Image generation" : "Preview & download"}</small></div>{node.kind === "asset" && <button className="node-close" type="button" aria-label="删除素材" onClick={() => removeNode(node)}>×</button>}</header>
            {node.kind === "asset" && node.asset && <AssetPreview nodeId={node.id} asset={node.asset} connectedTarget={edges.find((edge) => edge.source === node.id)?.target} referenceIndex={imageReferenceBySource.get(node.id)} onDerive={deriveAssetNode} onSaveDerivation={saveDerivedNode}/>} {/* asset */}
            {node.kind === "video" && <div className="node-body generator-node-body">
              <PromptEditor inputId={`video-prompt-${node.id}`} prompt={nodePrompt} setPrompt={(value) => updateGenerator(node.id, (current) => ({ ...current, prompt: value, configRevision: current.configRevision + 1 }))} mentionItems={mentionItems("video", node.id)} onSelectMention={(item) => selectMentionItem("video", item, node.id)}/>
              <div className="generator-node-section"><VideoDirectorControls mode={nodeVideoParams.directorMode} sourceVideoId={nodeVideoParams.sourceVideoId} sources={nodeSources} contract={nodeContract} onModeChange={(directorMode) => updateNodeVideo((current) => ({ ...current, directorMode }))} onSourceChange={(sourceVideoId) => updateNodeVideo((current) => ({ ...current, sourceVideoId }))}/></div>
              <p className={nodeContract.errors.length ? "video-mode-summary has-error" : "video-mode-summary"}>{nodeContract.source ? <>源视频 <b title={nodeContract.source.label}>{nodeContract.source.label}</b> · &lt;Video 1&gt;{nodeContract.references.length ? <> · 引用 {nodeLabels.filter((item) => !item.source).map((item) => item.tag).join(" / ")}</> : null}</> : nodeLabels.length ? <>引用 {nodeLabels.map((item) => item.tag).join(" / ")}</> : "无参考素材"}</p>{nodeContract.errors[0] && <small className="video-mode-node-error" role="alert">{nodeContract.errors[0]}</small>}
              <label className="profile-picker embedded-profile-picker"><span>采样方案 <em>模式 × 档位自动解析具体 Profile</em></span><select value={nodeRuntime.profileId === "base20" ? "base20" : "turbo4"} onChange={(event) => chooseProfile("video", event.target.value, node.id)}><option value="turbo4" disabled={!nodeVideoProfileChoices.some((profile) => profile.available && profile.sampling_mode === "turbo4")}>Turbo4 · 4 步蒸馏 LoRA</option><option value="base20" disabled={!nodeVideoProfileChoices.some((profile) => profile.available && profile.sampling_mode === "base")}>Base20 Direct · 优先成片</option></select><small>{nodeVideoProfileChoices.find((profile) => profile.available && profile.sampling_mode === (nodeRuntime.profileId === "base20" ? "base" : "turbo4")) ? `${nodeVideoProfileChoices.find((profile) => profile.available && profile.sampling_mode === (nodeRuntime.profileId === "base20" ? "base" : "turbo4"))!.id}@${nodeVideoProfileChoices.find((profile) => profile.available && profile.sampling_mode === (nodeRuntime.profileId === "base20" ? "base" : "turbo4"))!.version}` : "当前组合没有可用 Profile"}</small></label>
              <VideoReferenceSlots sources={nodes} bindings={nodeBindings} budget={H3_REFERENCE_BUDGET} onChoose={(media, slot) => { setAssetPickerTarget({ nodeId: node.id, media, slot }); setRailPanel("assets"); }} onRemove={(sourceNodeId, media) => { const source = nodes.find((item) => item.id === sourceNodeId); if (media === "audio" && source?.asset?.media === "video") { updateReferenceAudio(node.id, sourceNodeId, false); return; } const edge = edges.find((item) => item.source === sourceNodeId && item.target === node.id); if (edge) disconnectEdge(edge.id); }} onToggleAudio={(sourceNodeId, enabled) => updateReferenceAudio(node.id, sourceNodeId, enabled)}/>
              <details className="generator-advanced" open><summary>尺寸、时长与高级参数</summary><ParameterSummary title="当前解析配置" parameters={selectedId === node.id ? resolvedContract : undefined} compact/><ParameterPanel kind="video" hasImageInput={false} video={nodeVideoParams} setVideo={updateNodeVideo} image={nodeImageParams} setImage={updateNodeImage} profile={nodeVideoProfile}/></details>
              <details className={`prompt-preview ${nodeCompile.state === "error" ? "has-error" : ""}`} open={selectedId === node.id || nodeCompile.state === "error" ? true : undefined}><summary>H3 最终提示词预览（只读） · {nodeCompile.state === "loading" ? "校验中" : nodeCompile.state === "ready" ? "服务端已确认" : nodeCompile.state === "error" ? "校验失败" : "本地预览"}</summary>{nodeCompile.error && <div className="prompt-compile-error" role="alert"><span>最终提示词校验失败：{nodeCompile.error}</span><button type="button" onClick={() => { setSelectedId(node.id); setCompileRetryToken((value) => value + 1); }}>重新校验</button></div>}<pre data-i18n-ui-copy={!nodeCompile.prompt && !nodePrompt.trim() ? true : undefined}>{nodeCompile.prompt || nodePrompt.trim() || "填写提示词后显示实际提交文本"}</pre></details>
              <GeneratorNodeStatus job={nodeJob} busy={nodeBusy} onCancel={() => void cancelJob(node.id)}/>
              <nav className="node-workflow-actions"><a href={["r2v", "v2v", "rv2v"].includes(nodeContract.resolvedMode) ? `/api/workflows/director/${nodeContract.resolvedMode}` : "/api/workflows/director"} target="_blank" rel="noreferrer">查看模式合同</a></nav>
              <button type="button" className="node-generate" disabled={nodeBusy || nodeContract.errors.length > 0} onClick={() => { setSelectedId(node.id); void generate("video", node.id); }}>{nodeBusy ? "生成中…" : "生成视频"} <span>▶</span></button>
            </div>}
            {node.kind === "image" && <div className="node-body generator-node-body">
              {/* v6 canonical-node migration equivalent: onFocus={() => setSelectedId("image")} */}
              <div className="image-positive-prompt"><span>图片 Prompt</span><PromptMentionComposer value={nodePrompt} onChange={(value) => updateGenerator(node.id, (current) => ({ ...current, prompt: value, configRevision: current.configRevision + 1 }))} items={mentionItems("image", node.id)} onSelectItem={(item) => selectMentionItem("image", item, node.id)} onFocus={() => setSelectedId(node.id)} ariaLabel="图片正向提示词" placeholder={nodeConnected.length > 1 ? "例：保持图1人物，把图2服装应用到图1…" : "描述要生成或如何修改画面…"}/><small>{nodePrompt.length} 字 · 输入 @ 引用本节点素材</small></div>
              <label className="profile-picker embedded-profile-picker"><span>图片模型 / 工作流</span><select value={nodeRuntime.profileId} onChange={(event) => chooseProfile("image", event.target.value, node.id)}><option value="auto">Auto · 按已连参考图匹配</option>{nodeImageProfileChoices.map((profile) => <option key={`${profile.id}@${profile.version}`} value={profile.id} disabled={!profile.available}>{profile.display_name} · v{profile.version} · {imageProfileMode(profile)}</option>)}</select><small>{nodeImageProfileChoices.length ? `${nodeImageProfileChoices[0].id}@${nodeImageProfileChoices[0].version}` : "没有与当前连线兼容的 Profile"}</small></label>
              {(unavailableZImageEdit || !announcedZImageEdit || !announcedZImageEdit.available) && <div className="unreleased-profile-note" role="note"><strong>{unavailableZImageEdit?.display_name ?? "Z-Image-Edit · 尚未发布（不可用，不是 latent img2img）"}</strong><p>{unavailableZImageEdit?.reason ?? announcedZImageEdit?.use_notice ?? "当前 Z-Image 单图流程是 latent img2img，不具备 Z-Image-Edit 的指令式语义编辑能力。"}</p></div>}
              <div className="image-reference-summary embedded-references" aria-label={imageReferenceContract.max === 1 ? "单图底图连线" : "多图参考顺序"}>{nodeConnected.length ? nodeConnected.map((item, index) => <span key={item.id} title={item.asset?.fileName}><b>图{index + 1}</b>{item.asset?.fileName}</span>) : <button type="button" onClick={() => { setAssetPickerTarget({ nodeId: node.id, media: "image", slot: 1 }); setRailPanel("assets"); }}>＋ 选择参考图</button>}</div><small className="image-reference-help">直接使用“图1”、“图2”描述多图关系；系统按上方顺序绑定。</small>
              <details className="generator-advanced" open><summary>尺寸、质量与高级参数</summary><ParameterSummary title="当前解析配置" parameters={selectedId === node.id ? resolvedContract : undefined} compact/><ParameterPanel kind="image" hasImageInput={nodeConnected.length > 0} video={nodeVideoParams} setVideo={updateNodeVideo} image={nodeImageParams} setImage={updateNodeImage} profile={nodeImageProfile}/></details>
              <details className={`prompt-preview ${nodeCompile.state === "error" ? "has-error" : ""}`}><summary>图片模型实际 Prompt（只读）</summary><pre>{nodeCompile.prompt || nodePrompt.trim() || "填写图片提示词"}</pre></details>
              <GeneratorNodeStatus job={nodeJob} busy={nodeBusy} onCancel={() => void cancelJob(node.id)}/>
              <button type="button" className="node-generate secondary" disabled={nodeBusy} onClick={() => { setSelectedId(node.id); void generate("image", node.id); }}>{nodeBusy ? "生成中…" : "生成图片"} <span>▧</span></button>
            </div>}
            {node.kind === "output" && <><div className="output-source-grid">{sourceGeneratorIds.length ? sourceGeneratorIds.map((sourceId) => { const sourceJob = generatorStates[sourceId]?.job ?? IDLE_JOB; return <section key={sourceId} className="output-source-card"><small>{nodes.find((item) => item.id === sourceId)?.title ?? sourceId}</small><OutputPreview job={sourceJob}/><ParameterSummary title="来源任务回执" parameters={sourceJob.parameters}/></section>; }) : <OutputPreview job={nodeJob}/>}</div><TaskHistory jobs={jobHistory} currentId={nodeJob.id} onSelect={(item) => sourceGeneratorId && setJobForNode(sourceGeneratorId, item)} onClear={() => setJobHistory(nodeJob.id ? [nodeJob] : [])}/></>} {/* result */}
          </article>; })}
          {connecting && <div className="connection-tip">拖到兼容节点左侧圆点松开，或直接点击圆点 · 双击连线可断开</div>}
        </div></div>
        <div className="canvas-toolbar" role="toolbar" aria-label="画布缩放与定位"><button type="button" aria-label="缩小画布" onClick={() => zoomCanvas(viewport.zoom - .1)}>−</button><button type="button" className="canvas-zoom-value" aria-label="重置为百分之百" onClick={() => zoomCanvas(1)}>{Math.round(viewport.zoom * 100)}%</button><button type="button" aria-label="放大画布" onClick={() => zoomCanvas(viewport.zoom + .1)}>+</button><button type="button" className="canvas-fit" onClick={fitCanvas}>适配全部</button><button type="button" className="canvas-fit" onClick={() => setViewport({ x: 32, y: 32, zoom: 1 })}>回到原点</button></div>
        <button
          ref={canvasOverviewRef}
          type="button"
          aria-label="画布导航概览，拖动以定位视口"
          onPointerDown={startOverviewNavigation}
          onPointerMove={moveOverviewNavigation}
          onPointerUp={finishOverviewNavigation}
          onPointerCancel={finishOverviewNavigation}
          style={{ position: "absolute", zIndex: 50, right: 18, bottom: 16, width: 220, height: 151, padding: 0, overflow: "hidden", border: "1px solid #41495b", borderRadius: 9, background: "#0d111acc", boxShadow: "0 8px 24px #0008", cursor: "crosshair", touchAction: "none" }}
        >
          <span aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none", backgroundImage: "linear-gradient(#202635 1px, transparent 1px), linear-gradient(90deg, #202635 1px, transparent 1px)", backgroundSize: "11px 11px" }}/>
          {nodes.map((node) => <span key={`overview-${node.id}`} aria-hidden="true" style={{ position: "absolute", pointerEvents: "none", left: `${(node.position.x - renderedOverviewExtent.left) / renderedOverviewExtent.width * 100}%`, top: `${(node.position.y - renderedOverviewExtent.top) / renderedOverviewExtent.height * 100}%`, width: `${Math.max(1.5, NODE_SIZE[node.kind].w / renderedOverviewExtent.width * 100)}%`, height: `${Math.max(2, NODE_SIZE[node.kind].h / renderedOverviewExtent.height * 100)}%`, borderRadius: 2, background: node.id === selectedId ? "#9185ff" : node.kind === "output" ? "#d0a85c" : node.kind === "asset" ? "#69758c" : "#438f8a" }}/>) }
          <span aria-hidden="true" style={{ position: "absolute", pointerEvents: "none", boxSizing: "border-box", left: `${overviewViewport.left}%`, top: `${overviewViewport.top}%`, width: `${overviewViewport.width}%`, height: `${overviewViewport.height}%`, border: "2px solid #b3aaff", borderRadius: 3, background: "#8678ff22", boxShadow: "0 0 8px #8678ff88" }}/>
        </button>
        {contextMenu && <CanvasContextMenu
          state={contextMenu}
          nodes={nodes}
          onCreate={createNode}
          onDelete={removeNode}
          onSaveAsset={(node) => { closeContextMenu(false); void saveDerivedNode(node.id); }}
          onUpload={() => { closeContextMenu(); inputRef.current?.click(); }}
          onClose={closeContextMenu}
        />}
        <div className="canvas-notice" role="status">{notice}</div><input ref={inputRef} className="visually-hidden" type="file" accept="image/*,video/*,audio/*" multiple onChange={(event: ChangeEvent<HTMLInputElement>) => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }}/>
      </section>
    </div>
  </main>;
}

function CanvasContextMenu({ state, nodes, onCreate, onDelete, onSaveAsset, onUpload, onClose }: {
  state: CanvasContextMenuState;
  nodes: StudioNode[];
  onCreate: (kind: CoreNodeKind, position: XY) => void;
  onDelete: (node: StudioNode) => void;
  onSaveAsset: (node: StudioNode) => void;
  onUpload: () => void;
  onClose: (restoreFocus?: boolean) => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const targetNode = state.kind === "node" ? nodes.find((node) => node.id === state.nodeId) : undefined;

  const moveMenuFocus = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    if (!items.length) return;
    event.preventDefault();
    const activeIndex = items.findIndex((item) => item === document.activeElement);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : event.key === "ArrowDown"
          ? activeIndex < 0 ? 0 : (activeIndex + 1) % items.length
          : activeIndex < 0 ? items.length - 1 : (activeIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  useEffect(() => {
    menuRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) onClose(false);
    };
    const dismissWithKeyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose(true);
    };
    window.addEventListener("pointerdown", dismiss);
    window.addEventListener("keydown", dismissWithKeyboard);
    return () => {
      window.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("keydown", dismissWithKeyboard);
    };
  }, [onClose, state]);

  return <div ref={menuRef} className="canvas-context-menu" role="menu" tabIndex={-1} aria-label={state.kind === "canvas" ? "新建节点" : `${targetNode?.title ?? "节点"}操作`} style={{ left: state.screenPosition.x, top: state.screenPosition.y }} onKeyDown={moveMenuFocus}>
    {state.kind === "canvas" ? <>
      <span className="context-menu-title">新建节点</span>
      {CREATABLE_NODE_KINDS.map((kind) => {
        const template = BASE_NODES.find((node) => node.kind === kind)!;
        return <button key={kind} type="button" role="menuitem" onClick={() => onCreate(kind, state.canvasPosition)}><Icon>{kind === "video" ? "▶" : kind === "image" ? "▧" : "✓"}</Icon><span>{template.title}</span></button>;
      })}
      <div className="context-menu-separator" role="separator"/>
      <button type="button" role="menuitem" onClick={onUpload}><Icon>＋</Icon><span>素材节点…</span></button>
    </> : <>
      <span className="context-menu-title" title={targetNode?.title}>{targetNode?.title ?? "节点"}</span>
      {targetNode?.asset && (targetNode.asset.derivationId || targetNode.asset.sourceJobId) && <button type="button" role="menuitem" onClick={() => onSaveAsset(targetNode)}><Icon>↓</Icon><span>保存到资产</span></button>}
      <button className="context-menu-delete" type="button" role="menuitem" disabled={!targetNode} onClick={() => targetNode && onDelete(targetNode)}><Icon>×</Icon><span>删除节点</span><kbd>Delete</kbd></button>
    </>}
  </div>;
}

function VideoReferenceSlots({ sources, bindings, budget, onChoose, onRemove, onToggleAudio }: { sources: StudioNode[]; bindings: Array<{ kind: MediaKind; slot: number; sourceNodeId: string }>; budget: number; onChoose: (kind: MediaKind, slot: number) => void; onRemove: (sourceNodeId: string, kind: MediaKind) => void; onToggleAudio: (sourceNodeId: string, enabled: boolean) => void }) {
  const slots: Array<{ kind: MediaKind; count: number; label: string }> = [
    { kind: "image", count: 9, label: "Picture" },
    { kind: "video", count: 3, label: "Video" },
    { kind: "audio", count: 3, label: "Audio" },
  ];
  const usedCount = new Set(bindings.map((binding) => binding.sourceNodeId)).size;
  return <section className="reference-slots" aria-label="H3 参考素材槽位"><header><div><strong>参考素材</strong><small>点击空槽从资产选择</small></div><span className={usedCount >= budget ? "full" : ""}>已用 {usedCount}/{budget} 文件</span></header>{slots.map((group) => {
    const occupied = bindings.filter((binding) => binding.kind === group.kind);
    return <div className="reference-slot-group" key={group.kind}><b>{group.label}</b><div>{Array.from({ length: group.count }, (_, index) => {
      const binding = occupied.find((item) => item.slot === index + 1); const source = binding ? sources.find((node) => node.id === binding.sourceNodeId) : undefined; const asset = source?.asset; const label = asset?.fileName ?? source?.title;
      const pairedVideoAudio = group.kind === "audio" && asset?.media === "video";
      return <button type="button" key={`${group.kind}-${index}`} className={`${binding ? "occupied" : ""}${pairedVideoAudio ? " paired-audio" : ""}`} disabled={!binding && usedCount >= budget} onClick={() => binding ? onRemove(binding.sourceNodeId, group.kind) : onChoose(group.kind, index + 1)} aria-label={binding ? `移除 ${group.label} ${index + 1}: ${label ?? binding.sourceNodeId}` : `选择 ${group.label} ${index + 1}`} title={binding ? `${label ?? binding.sourceNodeId}${pairedVideoAudio ? " · 视频配对音轨" : ""} · 点击移除` : `选择 ${group.label} ${index + 1}`}>{asset?.thumbnailUrl && (group.kind !== "audio" || pairedVideoAudio) ? <img src={asset.thumbnailUrl} alt="" loading="lazy" decoding="async"/> : null}<span aria-hidden="true">{binding ? group.kind === "audio" ? "♫" : "×" : "+"}</span>{pairedVideoAudio && <em>配对</em>}<small>{index + 1}</small></button>;
    })}</div></div>;
  })}<div className="reference-audio-toggles">{bindings.filter((binding) => binding.kind === "video").map((binding) => { const source = sources.find((node) => node.id === binding.sourceNodeId); if (source?.asset?.media !== "video") return null; const paired = bindings.some((candidate) => candidate.kind === "audio" && candidate.sourceNodeId === binding.sourceNodeId); return <label key={`${binding.sourceNodeId}-${binding.slot}`}><input type="checkbox" checked={paired} disabled={!paired && source.asset.mediaMeta?.has_audio !== true} onChange={(event) => onToggleAudio(binding.sourceNodeId, event.target.checked)}/><span title={source.asset.fileName}>{source.asset.fileName}</span><small>{source.asset.mediaMeta?.has_audio === true ? "当前节点的配对音轨" : "无可用音轨"}</small></label>; })}</div><p>H3 容量：Picture 9 / Video 3 / Audio 3；混合输入合计最多 {H3_REFERENCE_BUDGET} 个文件，视频配对音轨不重复计文件数。</p></section>;
}

function GeneratorNodeStatus({ job, busy, onCancel }: { job: Job; busy: boolean; onCancel: () => void }) {
  return <div className="generator-node-status" aria-live="polite"><div><span>任务状态</span><strong className={`job-${job.status}`}>{job.message}</strong>{busy && <button type="button" onClick={onCancel}>取消</button>}</div><div className="progress"><i style={{ width: `${job.progress}%` }}/></div>{job.parameters && <ParameterSummary title="实际执行回执" parameters={job.parameters} compact/>}<ActualWorkflowActions job={job}/></div>;
}

function PromptEditor({ inputId, prompt, setPrompt, mentionItems, onSelectMention }: { inputId: string; prompt: string; setPrompt: (value: string) => void; mentionItems: PromptMentionItem[]; onSelectMention: (item: PromptMentionItem) => boolean }) {
  const hasNonEnglishBody = /[^\p{ASCII}]/u.test(prompt);
  return <div className="prompt-body embedded-prompt"><label htmlFor={inputId}>H3 视频 Prompt</label><PromptMentionComposer id={inputId} value={prompt} onChange={setPrompt} items={mentionItems} onSelectItem={onSelectMention} ariaLabel="H3 视频提示词" placeholder="粘贴完整 H3 提示词；输入 @ 选择素材"/><span className="embedded-char-count">{prompt.length} 字 · 输入 @ 引用本节点素材</span><div className="prompt-readonly-mode" role="note"><strong>只读提交</strong><span>只替换素材 ID 为 H3 标签，不改写 Prompt。</span></div>{hasNonEnglishBody && <p className="language-warning" role="alert">H3 官方模板建议视觉正文使用英文；系统不会翻译。</p>}<details className="prompt-helper"><summary>查看 H3 Ref2VA 参考模板（只读）</summary><pre aria-label="H3 Ref2VA 参考模板">{H3_REFERENCE_PROMPT_TEMPLATE}</pre></details></div>;
}
function AssetPreview({ nodeId, asset, connectedTarget, referenceIndex, onDerive, onSaveDerivation }: { nodeId: string; asset: Asset; connectedTarget?: string; referenceIndex?: number; onDerive: (nodeId: string, request: MediaDeriveRequest, options?: MediaDeriveOptions) => Promise<void>; onSaveDerivation: (nodeId: string) => Promise<void> }) {
  const [videoPreviewLoaded, setVideoPreviewLoaded] = useState(false);
  const [imagePreviewLoaded, setImagePreviewLoaded] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const imageThumbnail = asset.thumbnailUrl ?? (!asset.remoteId ? asset.localUrl : undefined);
  const displayOrientation = mediaDisplayOrientation(asset.mediaMeta ?? {});
  return <div className="asset-node-body" onDragStart={(event) => event.preventDefault()}><div className="asset-preview" data-media-kind={asset.media} data-orientation={displayOrientation}>{referenceIndex && <b className="picture-index">图{referenceIndex} · Image {referenceIndex}</b>}{asset.media === "image" && asset.localUrl ? imagePreviewLoaded ? <img src={asset.localUrl} alt={asset.fileName} draggable={false}/> : <button type="button" className="asset-video-load" data-no-drag onClick={() => setImagePreviewLoaded(true)} aria-label={`加载 ${asset.fileName} 的原图预览`}>{imageThumbnail && <img src={imageThumbnail} alt="" loading="lazy" decoding="async" draggable={false}/>}<span>⌕</span><small>点击加载原图</small></button> : null}{asset.media === "video" && asset.localUrl ? videoPreviewLoaded ? (
    // Remote video bytes are fetched only after the user explicitly asks for a preview.
    // eslint-disable-next-line jsx-a11y/media-has-caption
    <video ref={videoRef} src={asset.localUrl} controls playsInline preload="metadata" data-no-drag draggable={false} aria-label={`${asset.fileName} 视频预览`}/>
  ) : <button type="button" className="asset-video-load" data-no-drag onClick={() => setVideoPreviewLoaded(true)} aria-label={`加载 ${asset.fileName} 的视频预览`}>{asset.thumbnailUrl && <img src={asset.thumbnailUrl} alt="" loading="lazy" decoding="async" draggable={false}/>}<span>▶</span><small>加载视频预览</small></button> : null}{asset.media === "audio" && <div className="audio-preview" role="img" aria-label="音频素材"><span>♫</span><div className="waveform">▂▄▆▃▇▅▂▆▄▃</div></div>} {!asset.localUrl && <div className="missing-preview">需要重新上传</div>}<div className="asset-meta"><span title={asset.fileName}>{asset.fileName}</span><i className={asset.uploadState}>{asset.derivationId || asset.sourceJobId ? "未保存" : asset.uploadState === "uploading" ? "上传中" : asset.uploadState === "ready" ? "已就绪" : "失败"}</i></div></div>
    <div className="asset-role"><span>标签映射</span><strong>{connectedTarget === "image" ? referenceIndex ? `图${referenceIndex} · 由生图工作流程按顺序绑定` : "连接后由生图工作流程绑定" : asset.media === "image" ? "按媒体类型映射 <Picture N>" : asset.media === "video" ? "按媒体类型映射 <Video N>" : "按媒体类型映射 <Audio N>"}</strong></div>
    {asset.media === "video" && <><div className="asset-tech"><span>{asset.mediaMeta?.duration ? `${asset.mediaMeta.duration.toFixed(2)}s` : "时长未知"}</span><span>{asset.mediaMeta?.reference_fps ? `${asset.mediaMeta.reference_fps}fps 参考` : "帧率未知"}</span></div><p className="audio-toggle">{asset.mediaMeta?.has_audio === true ? "音轨参考请在每个视频生成节点中独立开启。" : "素材没有可用音轨"}</p></>}
    {asset.media === "image" && asset.mediaMeta?.width && <div className="asset-tech"><span>{asset.mediaMeta.width}×{asset.mediaMeta.height}</span><span>已解码验证</span></div>}
    {(asset.derivationId || asset.sourceJobId) && <button type="button" className="save-derived" data-no-drag onClick={() => void onSaveDerivation(nodeId)}>保存到资产</button>}
    {(asset.media === "video" || asset.media === "audio") && <MediaTools nodeId={nodeId} asset={asset} onDerive={onDerive} videoPreviewLoaded={videoPreviewLoaded} getPlaybackTime={() => videoRef.current?.currentTime}/>}
    {asset.restored && <p className="restore-note">从远程素材 ID 恢复；若预览无效，请重新上传。</p>}
  </div>;
}

function MediaTools({ nodeId, asset, onDerive, videoPreviewLoaded, getPlaybackTime }: { nodeId: string; asset: Asset; onDerive: (nodeId: string, request: MediaDeriveRequest, options?: MediaDeriveOptions) => Promise<void>; videoPreviewLoaded: boolean; getPlaybackTime: () => number | undefined }) {
  const duration = Math.max(0, Number(asset.mediaMeta?.duration ?? 0));
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(duration || 1);
  const [time, setTime] = useState(0);
  const [running, setRunning] = useState(false);
  const [runningOperation, setRunningOperation] = useState<MediaDeriveRequest["operation"]>();
  const [processingProgress, setProcessingProgress] = useState(0);
  const mediaAbortRef = useRef<AbortController | undefined>(undefined);
  const [referenceAudio, setReferenceAudio] = useState<"keep" | "remove">("remove");
  const run = async (request: MediaDeriveRequest) => {
    if (running) return;
    setRunning(true);
    setRunningOperation(request.operation);
    setProcessingProgress(0);
    const controller = request.operation === "prepare_h3_reference" ? new AbortController() : undefined;
    mediaAbortRef.current = controller;
    try {
      await onDerive(nodeId, request, {
        signal: controller?.signal,
        onProgress: (progress) => setProcessingProgress(progress),
      });
    } finally {
      mediaAbortRef.current = undefined;
      setRunning(false);
      setRunningOperation(undefined);
    }
  };
  useEffect(() => () => mediaAbortRef.current?.abort(), []);
  const capturePlaybackFrame = async () => {
    const current = getPlaybackTime();
    if (typeof current !== "number" || !Number.isFinite(current)) return;
    setTime(current);
    await run({ operation: "frame", time: current });
  };
  const prepareReference = async () => {
    const planned = estimateH3ReferenceCanvas(Number(asset.mediaMeta?.width), Number(asset.mediaMeta?.height), Number(asset.mediaMeta?.rotation ?? 0));
    const original = asset.mediaMeta?.width && asset.mediaMeta?.height ? `${asset.mediaMeta.width}×${asset.mediaMeta.height}` : "尺寸未知";
    const expected = planned ? `${planned.width}×${planned.height}` : "由服务端探测";
    const durationText = asset.mediaMeta?.duration ? `${asset.mediaMeta.duration.toFixed(2)} 秒` : "时长未知";
    if (!window.confirm(`优化为 H3 视频参考？\n\n原始：${original} / ${durationText} / ${asset.mediaMeta?.fps ?? asset.mediaMeta?.reference_fps ?? "?"} FPS\n预计：${expected} / 最长 15.00 秒 / 24 FPS / ${referenceAudio === "keep" ? "保留音频" : "移除音频"}\n\n原始素材不会被修改。`)) return;
    await run({ operation: "prepare_h3_reference", preset: "h3-low-token", audio: referenceAudio });
  };
  return <details className="media-tools" data-no-drag>
    <summary>剪辑与派生</summary>
    <div className="media-tool-times"><label><span>开始秒</span><input type="number" min="0" max={duration || undefined} step="0.01" value={start} onChange={(event) => setStart(Number(event.target.value))}/></label><label><span>结束秒</span><input type="number" min="0.01" max={duration || undefined} step="0.01" value={end} onChange={(event) => setEnd(Number(event.target.value))}/></label></div>
    {asset.media === "video" ? <>
      <button type="button" disabled={running || end <= start} onClick={() => void run({ operation: "video_trim", start, end })}>裁剪视频</button>
      <div className="media-tool-grid"><button type="button" disabled={running} onClick={() => void run({ operation: "frame", position: "first" })}>获取首帧</button><button type="button" disabled={running} onClick={() => void run({ operation: "frame", position: "last" })}>获取尾帧</button></div>
      <button type="button" disabled={running || !videoPreviewLoaded} onClick={() => void capturePlaybackFrame()}>获取播放器当前帧</button>
      <label className="media-current-frame"><span>指定时间（高级）</span><input type="number" min="0" max={duration || undefined} step="0.01" value={time} onChange={(event) => setTime(Number(event.target.value))}/><button type="button" disabled={running} onClick={() => void run({ operation: "frame", time })}>按时间获取</button></label>
      <div className="media-tool-grid"><button type="button" disabled={running || asset.mediaMeta?.has_audio === false} onClick={() => void run({ operation: "extract_audio" })}>分离音频</button><button type="button" disabled={running || asset.mediaMeta?.has_audio === false} onClick={() => void run({ operation: "remove_audio" })}>移除音轨</button></div>
      <div className="h3-reference-prepare"><label><span>H3 参考音频</span><select value={referenceAudio} disabled={running || asset.mediaMeta?.has_audio === false} onChange={(event) => setReferenceAudio(event.target.value as "keep" | "remove")}><option value="remove">移除</option><option value="keep" disabled={asset.mediaMeta?.has_audio === false}>保留</option></select></label><button type="button" disabled={running} onClick={() => void prepareReference()}>优化为 H3 视频参考</button>{runningOperation === "prepare_h3_reference" && <><progress max="100" value={processingProgress}/><button type="button" className="cancel-media-task" onClick={() => mediaAbortRef.current?.abort()}>取消处理</button></>}<small>降低 sm120 + SageAttention 长序列灰屏风险；生成独立派生，不修改原素材。</small></div>
    </> : <button type="button" disabled={running || end <= start} onClick={() => void run({ operation: "audio_trim", start, end })}>裁剪音频</button>}
    <small>{running ? `处理中… ${processingProgress > 0 ? `${processingProgress}%` : ""}` : "产物会作为新节点加入画布，由你决定是否保存到资产。"}</small>
  </details>;
}
function ResultVideo({ job }: { job: Job }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const retryTimerRef = useRef<number | undefined>(undefined);
  const [attempt, setAttempt] = useState(0);
  const [playbackState, setPlaybackState] = useState<"loading" | "ready" | "error">("loading");
  const identity = job.id ?? job.updatedAt ?? job.createdAt ?? "generated-result";
  const source = job.previewUrl!;

  useEffect(() => {
    // A newly completed job replaces the waiting state without a page load.
    // Explicitly reset Chromium's media state so the first click works just
    // like it does after a manual refresh.
    videoRef.current?.load();
    return () => {
      if (retryTimerRef.current !== undefined) window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = undefined;
    };
  }, [source, identity, attempt]);

  const clearRetry = () => {
    if (retryTimerRef.current !== undefined) window.clearTimeout(retryTimerRef.current);
    retryTimerRef.current = undefined;
    setPlaybackState("ready");
  };
  const retryTransientFailure = () => {
    if (attempt >= 3) { setPlaybackState("error"); return; }
    if (retryTimerRef.current !== undefined) return;
    setPlaybackState("loading");
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = undefined;
      setAttempt((value) => Math.min(3, value + 1));
    }, 400 * (attempt + 1));
  };

  /* Generated clips do not have a separate captions file at preview time. */
  /* eslint-disable jsx-a11y/media-has-caption */
  return <div className={`result-video-shell ${playbackState}`}>
    <video ref={videoRef} src={source} poster={job.thumbnailUrl} controls playsInline preload="metadata" data-no-drag onLoadStart={() => setPlaybackState("loading")} onWaiting={() => setPlaybackState("loading")} onPlaying={clearRetry} onLoadedMetadata={clearRetry} onCanPlay={clearRetry} onError={retryTransientFailure}/>
    {playbackState === "loading" && <span className="result-video-status">正在缓冲视频…</span>}
    {playbackState === "error" && <button type="button" className="result-video-status error" onClick={() => { setPlaybackState("loading"); setAttempt(0); }}>视频加载失败，点击重试</button>}
  </div>;
  /* eslint-enable jsx-a11y/media-has-caption */
}

function OutputPreview({ job }: { job: Job }) {
  if (!job.previewUrl) return <div className="output-empty"><span className="output-orbit">✦</span><strong>{job.status === "failed" ? "任务出现问题" : "等待生成结果"}</strong><p>{job.message}</p>{["queued", "running"].includes(job.status) && <div className="output-progress"><i style={{ width: `${job.progress}%` }}/></div>}</div>;
  return <div className="result-preview">{job.media === "image" ? <img src={job.previewUrl} alt="AI 生成结果"/> : <ResultVideo key={`${job.id ?? "result"}:${job.previewUrl}`} job={job}/>}{job.snapshot && <small className="result-snapshot">{job.snapshot}</small>}<DownloadAnchor className="download-button" href={job.downloadUrl ?? job.previewUrl} suggestedName={`${job.id ?? "h3-result"}.${job.media === "image" ? "png" : "mp4"}`}/></div>;
}
function ActualWorkflowActions({ job, compact = false }: { job: Job; compact?: boolean }) {
  if (!job.id || (!job.workflowSha256 && !job.workflowEvidence)) return null;
  const href = `/api/jobs/${encodeURIComponent(job.id)}/workflow`;
  return <nav className={`actual-workflow-actions ${compact ? "compact" : ""}`} aria-label="实际执行工作流"><a href={href} target="_blank" rel="noreferrer">查看实际工作流</a><a href={`${href}?download=1`} download>下载实际工作流</a>{job.workflowSha256 && <small title={job.workflowSha256}>SHA {job.workflowSha256.slice(0, 8)}</small>}</nav>;
}
type DownloadWritable = { write: (chunk: Uint8Array) => Promise<void>; close: () => Promise<void>; abort?: () => Promise<void> };
type DownloadFileHandle = { createWritable: () => Promise<DownloadWritable> };
type DownloadPickerWindow = Window & { showSaveFilePicker?: (options: { suggestedName: string }) => Promise<DownloadFileHandle> };

function DownloadAnchor({ href, className, ariaLabel = "下载到本地", label = "下载到本地", startedLabel = "下载已开始", suggestedName = "h3-result.mp4" }: { href: string; className?: string; ariaLabel?: string; label?: string; startedLabel?: string; suggestedName?: string }) {
  const [phase, setPhase] = useState<"idle" | "downloading" | "handed" | "done" | "error">("idle");
  const [progress, setProgress] = useState(0);
  const controllerRef = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => controllerRef.current?.abort(), []);

  const startDownload = async (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (phase === "downloading") {
      controllerRef.current?.abort();
      setPhase("idle");
      return;
    }
    const picker = (window as DownloadPickerWindow).showSaveFilePicker;
    if (!picker) {
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "";
      anchor.rel = "noreferrer";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setPhase("handed");
      window.setTimeout(() => setPhase("idle"), 2400);
      return;
    }
    let writable: DownloadWritable | undefined;
    try {
      const handle = await picker({ suggestedName });
      writable = await handle.createWritable();
      const controller = new AbortController();
      controllerRef.current = controller;
      setPhase("downloading");
      setProgress(0);
      const response = await fetch(href, { signal: controller.signal, cache: "no-cache" });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      const total = Number(response.headers.get("Content-Length")) || 0;
      const reader = response.body.getReader();
      let received = 0;
      let lastUpdate = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        await writable.write(value);
        received += value.byteLength;
        const now = performance.now();
        if (now - lastUpdate > 200) {
          setProgress(total ? Math.min(99, Math.round(received / total * 100)) : 0);
          lastUpdate = now;
        }
      }
      await writable.close();
      writable = undefined;
      setProgress(100);
      setPhase("done");
      window.setTimeout(() => setPhase("idle"), 2400);
    } catch (error) {
      if (writable?.abort) await writable.abort().catch(() => undefined);
      if (error instanceof Error && error.name === "AbortError") setPhase("idle");
      else setPhase("error");
    } finally {
      controllerRef.current = undefined;
    }
  };
  const text = phase === "downloading" ? (progress ? `${progress}%（点击取消）` : "下载中…") : phase === "done" ? "已保存" : phase === "handed" ? startedLabel : phase === "error" ? "重试下载" : label;
  return <button type="button" className={className} data-no-drag onClick={(event) => void startDownload(event)} aria-label={`${ariaLabel}${phase === "downloading" ? `，${progress}%` : ""}`}><span>↓</span> {text}</button>;
}
function ParameterSummary({ title, parameters, compact = false }: { title: string; parameters?: GenerationParameters; compact?: boolean }) {
  const rows = jobParameterRows(parameters);
  if (!rows.length) return null;
  return <section className={`parameter-summary ${compact ? "compact" : ""}`} aria-label={title}>
    <div className="parameter-summary-title"><strong>{title}</strong><span>{parameters?.profile_digest ? String(parameters.profile_digest).slice(0, 8) : "preview"}</span></div>
    <dl>{rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd title={row.value}>{row.value}</dd></div>)}</dl>
  </section>;
}
function TaskHistory({ jobs, currentId, onSelect, onClear }: { jobs: Job[]; currentId?: string; onSelect: (job: Job) => void; onClear: () => void }) {
  const statusLabel: Record<Job["status"], string> = { idle: "空闲", submitting: "提交中", queued: "排队", running: "生成中", completed: "已完成", failed: "失败" };
  return <section className="job-history" aria-label="生成历史">
    <div className="job-history-title"><div><strong>生成历史</strong><small>{jobs.length ? `本机保留最近 ${jobs.length} 条` : "暂无历史任务"}</small></div>{jobs.length > 1 && <button type="button" onClick={onClear}>清除旧记录</button>}</div>
    <div className="job-history-list">{jobs.map((item) => {
      const rows = jobParameterRows(item.parameters);
      const profile = rows.find((row) => row.label === "Resolved Profile")?.value ?? "等待解析 Profile";
      const steps = rows.find((row) => row.label === "Steps")?.value ?? "—";
      const sampling = rows.find((row) => row.label === "Sampler / Scheduler")?.value ?? "—";
      const lora = rows.find((row) => row.label === "LoRA")?.value ?? "—";
      return <article key={item.id} className={`job-history-card ${currentId === item.id ? "current" : ""}`}>
        <button type="button" className="job-history-select" onClick={() => onSelect(item)} aria-label={`查看任务 ${item.id}`}>
          <span className="job-history-meta"><i className={`history-${item.status}`}>{statusLabel[item.status]}</i><time dateTime={item.createdAt}>{formatJobTime(item.createdAt)}</time><b>{item.media === "image" ? "IMG" : "VID"}</b></span>
          <strong title={profile}>{profile}</strong>
          <span className="job-history-contract"><em>{steps} steps</em><em>{sampling}</em><em title={lora}>{lora}</em><em>{formatJobElapsed(item.createdAt, item.updatedAt)}</em></span>
          {item.prompt && <small title={item.prompt}>{item.prompt}</small>}
        </button>
        {item.downloadUrl && <a href={item.downloadUrl} download aria-label={`下载任务 ${item.id}`}>↓</a>}
      </article>;
    })}</div>
  </section>;
}

function LazyAudioPlayer({ source, label }: { source: string; label: string }) {
  const [activated, setActivated] = useState(false);
  const [paused, setPaused] = useState(true);
  const audioRef = useRef<HTMLAudioElement>(null);
  const togglePlayback = () => {
    const audio = audioRef.current;
    if (!activated || !audio) {
      setActivated(true);
      return;
    }
    if (audio.paused || audio.ended) void audio.play(); else audio.pause();
  };
  return <div className={`library-audio-player${paused ? " paused" : " playing"}`}>
    {/* Audio bytes are mounted only after an explicit user action. */}
    {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
    {activated && <audio ref={audioRef} src={source} autoPlay preload="metadata" onPlay={() => setPaused(false)} onPause={() => setPaused(true)} onEnded={() => setPaused(true)} aria-label={`${label} 音频播放器`}/>}
    <button type="button" className="library-audio-toggle" onClick={togglePlayback} aria-label={`${paused ? "播放" : "暂停"} ${label}`}>
      <span className="library-audio" aria-hidden="true">♫</span>
      <span className="library-audio-state" aria-hidden="true">{paused ? "▶" : "Ⅱ"}</span>
    </button>
  </div>;
}

function LazyVideoPlayer({ source, label, thumbnailUrl }: { source: string; label: string; thumbnailUrl?: string }) {
  const [activated, setActivated] = useState(false);
  const [paused, setPaused] = useState(true);
  const videoRef = useRef<HTMLVideoElement>(null);
  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused || video.ended) void video.play(); else video.pause();
  };
  if (!activated) return <button type="button" className="library-video-play result-library-video-play" onClick={() => setActivated(true)} aria-label={`播放 ${label}`}>
    {thumbnailUrl ? <img src={thumbnailUrl} alt="" loading="lazy" decoding="async" draggable={false}/> : <span className="result-library-video-placeholder" aria-hidden="true"><b>▶</b><small>点击加载原媒体</small></span>}
    <span className="library-video-overlay" aria-hidden="true">▶</span>
  </button>;
  return <div className={`library-video-player result-library-video-player${paused ? " paused" : " playing"}`}>
    {/* Derived video bytes are mounted only after the user explicitly presses play. */}
    {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
    <video ref={videoRef} src={source} poster={thumbnailUrl} autoPlay playsInline preload="metadata" onClick={togglePlayback} onPlay={() => setPaused(false)} onPause={() => setPaused(true)} onEnded={() => setPaused(true)} aria-label={`${label} 视频播放器`}/>
    <button type="button" className="library-video-toggle result-library-video-toggle" onClick={togglePlayback} aria-label={`${paused ? "播放" : "暂停"} ${label}`}><span aria-hidden="true">{paused ? "▶" : "Ⅱ"}</span></button>
  </div>;
}

function LibraryAssetPreview({ item, selecting, selected, duplicate, onToggleSelection }: { item: LibraryAsset; selecting: boolean; selected: boolean; duplicate: boolean; onToggleSelection: () => void }) {
  const [activated, setActivated] = useState(false);
  const [paused, setPaused] = useState(true);
  const videoRef = useRef<HTMLVideoElement>(null);
  const displayOrientation = mediaDisplayOrientation(item.media);
  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused || video.ended) void video.play(); else video.pause();
  };
  return <div className="library-preview" data-media-kind={item.kind} data-orientation={displayOrientation}>
    {selecting && <label className="library-select"><input type="checkbox" checked={selected} onChange={onToggleSelection} aria-label={`选择资产 ${item.filename}`}/><span>选择</span></label>}
    {item.kind === "video" ? activated ? <div className={`library-video-player${paused ? " paused" : " playing"}`}>
      {/* The authenticated video route is mounted only after this explicit user action. */}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <video ref={videoRef} src={item.contentUrl} poster={item.thumbnailUrl || undefined} autoPlay playsInline preload="metadata" onClick={togglePlayback} onPlay={() => setPaused(false)} onPause={() => setPaused(true)} onEnded={() => setPaused(true)} aria-label={`${item.filename} 视频播放器`}/>
      <button type="button" className="library-video-toggle" onClick={togglePlayback} aria-label={`${paused ? "播放" : "暂停"} ${item.filename}`}><span aria-hidden="true">{paused ? "▶" : "Ⅱ"}</span></button>
    </div> : <button type="button" className="library-video-play" onClick={() => setActivated(true)} aria-label={`播放 ${item.filename}`}>
      {item.thumbnailUrl ? <img src={item.thumbnailUrl} alt="" loading="lazy" decoding="async"/> : <span className="library-video-placeholder" aria-hidden="true">▶</span>}
      <span className="library-video-overlay" aria-hidden="true">▶</span>
    </button> : item.kind === "image" && item.thumbnailUrl ? <img src={item.thumbnailUrl} alt={`${item.filename} 缩略图`} loading="lazy" decoding="async"/> : item.kind === "image" ? <span className="library-audio" role="img" aria-label="缩略图待生成">▧</span> : <LazyAudioPlayer source={item.contentUrl} label={item.filename}/>}
    <b>{item.kind.toUpperCase()}</b>{duplicate && <em className="library-duplicate-badge">重复</em>}
  </div>;
}

function AssetLibrary({ items, folders, state, onAdd, onUpload, onRefresh, onCreateFolder, onRename, onMove, onPin, onDeleteFolder, onDelete, onClose }: { items: LibraryAsset[]; folders: LibraryFolder[]; state: "loading" | "ready" | "error"; onAdd: (item: LibraryAsset, connectTarget?: "image") => void; onUpload: () => void; onRefresh: () => void; onCreateFolder: (name: string) => Promise<void>; onRename: (item: LibraryAsset, displayName: string) => Promise<void>; onMove: (item: LibraryAsset, folderId: string) => Promise<void>; onPin: (item: LibraryAsset, pinned: boolean) => Promise<void>; onDeleteFolder: (folder: LibraryFolder) => Promise<void>; onDelete: (item: LibraryAsset) => Promise<void>; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [folderId, setFolderId] = useState("");
  const [newFolderName, setNewFolderName] = useState("");
  const [busyId, setBusyId] = useState<string>();
  const [selecting, setSelecting] = useState(false);
  const [showDuplicates, setShowDuplicates] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const filtered = [...items].sort((left, right) => Number(right.pinned) - Number(left.pinned)).filter((item) => (!folderId || item.folderId === folderId) && (!query.trim() || item.filename.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  const seenHashes = new Set<string>();
  const duplicateIds = new Set<string>();
  const unique = filtered.filter((item) => {
    if (!item.contentHash) return true;
    if (seenHashes.has(item.contentHash)) { duplicateIds.add(item.id); return false; }
    seenHashes.add(item.contentHash);
    return true;
  });
  const displayed = showDuplicates ? filtered : unique;
  const existingIds = new Set(items.map((item) => item.id));
  const effectiveSelectedIds = new Set([...selectedIds].filter((id) => existingIds.has(id)));
  const createFolder = async () => {
    const name = newFolderName.trim();
    if (!name || busyId) return;
    setBusyId("folder");
    try { await onCreateFolder(name); setNewFolderName(""); } finally { setBusyId(undefined); }
  };
  const rename = async (item: LibraryAsset) => {
    const value = window.prompt("资产新名称", item.filename)?.trim();
    if (!value || value === item.filename) return;
    setBusyId(item.id);
    try { await onRename(item, value); } finally { setBusyId(undefined); }
  };
  const remove = async (item: LibraryAsset) => {
    if (busyId || !window.confirm(`确定删除资产“${item.filename}”吗？\n\n服务器中的素材文件会被删除；如果它仍被任务或长视频项目引用，系统会阻止删除。`)) return;
    setBusyId(item.id);
    try { await onDelete(item); } finally { setBusyId(undefined); }
  };
  const removeSelected = async () => {
    const targets = items.filter((item) => effectiveSelectedIds.has(item.id));
    if (!targets.length || busyId || !window.confirm(`确定删除选中的 ${targets.length} 个资产吗？\n\n仍被任务或长视频项目引用的资产会保留并提示失败。`)) return;
    setBusyId("batch");
    const settled = await Promise.allSettled(targets.map((item) => onDelete(item)));
    const failed = new Set(targets.filter((_, index) => settled[index]?.status === "rejected").map((item) => item.id));
    setSelectedIds(failed);
    if (!failed.size) setSelecting(false);
    setBusyId(undefined);
  };
  const removeFolder = async () => {
    const folder = folders.find((item) => item.id === folderId);
    if (!folder || busyId || !window.confirm(`确定删除文件夹“${folder.name}”吗？\n\n不会删除任何资产；其中的资产和子文件夹会移到上一级。`)) return;
    setBusyId("delete-folder");
    try { await onDeleteFolder(folder); setFolderId(""); } catch { /* parent reports the server error */ } finally { setBusyId(undefined); }
  };
  const toggleSelection = (id: string) => setSelectedIds((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  return <aside id="asset-library-drawer" className="rail-drawer" aria-label="远程资产库">
    <header className="rail-drawer-header"><div><strong>资产</strong><small>服务器已上传素材</small></div><button type="button" aria-label="关闭资产抽屉" onClick={onClose}>×</button></header>
    <div className="library-actions"><button type="button" onClick={onUpload}>＋ 上传新素材</button><button type="button" onClick={onRefresh} disabled={state === "loading"}>↻ 刷新</button><button type="button" onClick={() => { setSelecting((value) => !value); setSelectedIds(new Set()); }}>{selecting ? "取消多选" : "多选管理"}</button></div>
    <div className="library-search"><label><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索资产名称" aria-label="搜索资产"/></label><select value={folderId} onChange={(event) => setFolderId(event.target.value)} aria-label="按文件夹筛选"><option value="">全部文件夹</option>{folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></div>
    <div className="library-folder-create"><input value={newFolderName} onChange={(event) => setNewFolderName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createFolder(); }} placeholder="新文件夹名称"/><button type="button" disabled={!newFolderName.trim() || Boolean(busyId)} onClick={() => void createFolder()}>＋ 建文件夹</button>{folderId && <button type="button" className="library-delete-folder" disabled={Boolean(busyId)} onClick={() => void removeFolder()}>{busyId === "delete-folder" ? "删除中…" : "删除当前文件夹"}</button>}</div>
    <p className="library-state" role="status" aria-live="polite">{state === "loading" ? "正在读取资产…" : state === "error" ? "资产读取失败，可重试。" : `${items.length} 个资产 · 显示 ${displayed.length} 个${duplicateIds.size && !showDuplicates ? ` · 已收起 ${duplicateIds.size} 个重复项` : ""}`}</p>
    {(selecting || duplicateIds.size > 0) && <div className="library-bulk-actions"><button type="button" onClick={() => setSelectedIds(new Set(displayed.map((item) => item.id)))}>全选当前</button>{duplicateIds.size > 0 && <button type="button" onClick={() => { setShowDuplicates(true); setSelecting(true); setSelectedIds(new Set(duplicateIds)); }}>选择重复项 ({duplicateIds.size})</button>}<button type="button" onClick={() => setShowDuplicates((value) => !value)}>{showDuplicates ? "收起重复" : "显示重复"}</button>{selecting && <button type="button" className="library-delete" disabled={!effectiveSelectedIds.size || Boolean(busyId)} onClick={() => void removeSelected()}>{busyId === "batch" ? "删除中…" : `删除所选 (${effectiveSelectedIds.size})`}</button>}</div>}
    <div className="library-grid">{displayed.map((item) => <article className={`library-card${effectiveSelectedIds.has(item.id) ? " selected" : ""}${item.pinned ? " pinned" : ""}`} key={item.id}>
      <LibraryAssetPreview item={item} selecting={selecting} selected={effectiveSelectedIds.has(item.id)} duplicate={duplicateIds.has(item.id)} onToggleSelection={() => toggleSelection(item.id)}/>
      <div className="library-card-body"><strong title={item.filename}>{item.pinned ? "★ " : ""}{item.filename}</strong><small>{item.media.width && item.media.height ? `${item.media.width}×${item.media.height}` : item.media.duration ? `${item.media.duration.toFixed(2)}s` : formatJobTime(item.createdAt)}</small><div className="library-card-tools"><button type="button" disabled={busyId === item.id} onClick={() => void rename(item)}>改名</button><select value={item.folderId ?? ""} disabled={busyId === item.id} onChange={(event) => { setBusyId(item.id); void onMove(item, event.target.value).catch(() => undefined).finally(() => setBusyId(undefined)); }} aria-label={`移动 ${item.filename} 到文件夹`}><option value="">未分类</option>{folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select><button type="button" className="library-pin" disabled={busyId === item.id} onClick={() => { setBusyId(item.id); void onPin(item, !item.pinned).catch(() => undefined).finally(() => setBusyId(undefined)); }} aria-label={`${item.pinned ? "取消置顶" : "置顶"}资产 ${item.filename}`}>{item.pinned ? "取消置顶" : "置顶"}</button><button type="button" className="library-delete" disabled={busyId === item.id} onClick={() => void remove(item)} aria-label={`删除资产 ${item.filename}`}>{busyId === item.id ? "处理中…" : "删除"}</button></div><button type="button" onClick={() => onAdd(item)} aria-label={`将 ${item.filename} 添加到画布`}>添加到画布</button>{item.kind === "image" && <button type="button" className="library-connect-image" onClick={() => onAdd(item, "image")} aria-label={`将 ${item.filename} 连到图片生成`}>连到生图</button>}</div>
    </article>)}</div>
    {state === "ready" && !filtered.length && <div className="library-empty"><span>◇</span><strong>{items.length ? "没有匹配资产" : "还没有资产"}</strong><p>{items.length ? "换个搜索词或文件夹试试。" : "生成结果不会自动进入资产；请在“结果”中点“保存到资产”，或上传本地素材。"}</p></div>}
  </aside>;
}
function ResultThumbnail({ job }: { job: Job }) {
  const fallback = job.id ? `/api/jobs/${encodeURIComponent(job.id)}/thumbnail?index=0` : undefined;
  const primary = currentOriginApiUrl(job.thumbnailUrl, fallback);
  const videoSource = currentOriginApiUrl(job.previewUrl, currentOriginApiUrl(job.downloadUrl));
  const videoLabel = job.id ?? "视频结果";
  const candidates = useMemo(() => Array.from(new Set([primary, fallback].filter((candidate): candidate is string => Boolean(candidate)))), [fallback, primary]);
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [retry, setRetry] = useState(0);
  const [coolingDown, setCoolingDown] = useState(false);
  const [recoveryCycle, setRecoveryCycle] = useState(0);
  const [activated, setActivated] = useState(false);
  const [paused, setPaused] = useState(true);
  const videoRef = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (!coolingDown || recoveryCycle >= 4) return;
    const timer = window.setTimeout(() => {
      setCandidateIndex(0);
      setRetry(0);
      setRecoveryCycle((current) => current + 1);
      setCoolingDown(false);
    }, 12_000);
    return () => window.clearTimeout(timer);
  }, [coolingDown, recoveryCycle]);
  const thumbnail = coolingDown ? undefined : candidates[candidateIndex];
  // Thumbnail responses are immutable for efficient browsing. Include the
  // renderer recipe in the request URL so a fixed recipe cannot be masked by
  // a browser's year-long cache of an older (for example, black-frame) image.
  const versionedThumbnail = thumbnail ? `${thumbnail}${thumbnail.includes("?") ? "&" : "?"}thumbnail_recipe=2` : undefined;
  const retryUrl = versionedThumbnail ? retry > 0 ? `${versionedThumbnail}&thumb_retry=${retry}` : versionedThumbnail : undefined;
  const thumbnailFailed = () => {
    if (retry < 2) {
      window.setTimeout(() => setRetry((current) => current + 1), 500 * (retry + 1));
      return;
    }
    if (candidateIndex + 1 < candidates.length) {
      setCandidateIndex((current) => current + 1);
      setRetry(0);
      return;
    }
    setCoolingDown(true);
  };
  if (job.media === "video" && activated && videoSource) {
    const togglePlayback = () => {
      const video = videoRef.current;
      if (!video) return;
      if (video.paused || video.ended) void video.play(); else video.pause();
    };
    return <div className={`library-video-player result-library-video-player${paused ? " paused" : " playing"}`}>
      {/* Result video bytes are mounted only after the user explicitly presses play. */}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <video ref={videoRef} src={videoSource} poster={retryUrl} autoPlay playsInline preload="metadata" onClick={togglePlayback} onPlay={() => setPaused(false)} onPause={() => setPaused(true)} onEnded={() => setPaused(true)} aria-label={`${videoLabel} 视频播放器`}/>
      <button type="button" className="library-video-toggle result-library-video-toggle" onClick={togglePlayback} aria-label={`${paused ? "播放" : "暂停"} ${videoLabel}`}><span aria-hidden="true">{paused ? "▶" : "Ⅱ"}</span></button>
    </div>;
  }
  const placeholder = <span className="result-library-video-placeholder" aria-hidden="true"><b>{job.media === "video" ? "▶" : "▧"}</b><small>{coolingDown && recoveryCycle < 4 ? "缩略图恢复中…" : "点击加载原媒体"}</small></span>;
  if (job.media === "video" && videoSource) return <button type="button" className="library-video-play result-library-video-play" onClick={() => setActivated(true)} aria-label={`播放 ${videoLabel}`}>
    {retryUrl ? <img src={retryUrl} alt="" loading="lazy" decoding="async" onError={thumbnailFailed}/> : placeholder}
    <span className="library-video-overlay" aria-hidden="true">▶</span>
  </button>;
  if (!retryUrl) return placeholder;
  return <img src={retryUrl} alt={`${job.media === "video" ? "视频" : "图片"}结果缩略图`} loading="lazy" decoding="async" onError={thumbnailFailed}/>;
}
function ResultLibrary({ jobs, derivedResults, state, verified, error, pageError, savedResultAssets, currentId, hasMore, onRetry, onLoadMore, onPin, onResume, onPinDerived, onSelect, onAdd, onSave, onDelete, onAddDerived, onSaveDerived, onDeleteDerived, onClose }: { jobs: Job[]; derivedResults: DerivedMedia[]; state: "loading" | "refreshing" | "ready" | "error"; verified: boolean; error: string; pageError: string; savedResultAssets: Record<string, string>; currentId?: string; hasMore: boolean; onRetry: () => Promise<number>; onLoadMore: () => Promise<number>; onSelect: (job: Job) => void; onAdd: (job: Job) => Promise<void>; onSave: (job: Job) => Promise<LibraryAsset | undefined>; onDelete: (job: Job) => Promise<void>; onPin: (job: Job, pinned: boolean) => Promise<void>; onResume: (job: Job, additionalSteps: number) => Promise<void>; onAddDerived: (derived: DerivedMedia) => Promise<void>; onSaveDerived: (derived: DerivedMedia) => Promise<LibraryAsset | undefined>; onDeleteDerived: (derived: DerivedMedia) => Promise<void>; onPinDerived: (derived: DerivedMedia, pinned: boolean) => Promise<void>; onClose: () => void }) {
  const [visibleCount, setVisibleCount] = useState(RESULT_PAGE_SIZE);
  const [addingId, setAddingId] = useState<string>();
  const [savingId, setSavingId] = useState<string>();
  const [pinningId, setPinningId] = useState<string>();
  const [resumingId, setResumingId] = useState<string>();
  const [resumeSteps, setResumeSteps] = useState<Record<string, number>>({});
  const [deletingId, setDeletingId] = useState<string>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [localPageError, setLocalPageError] = useState("");
  const [selecting, setSelecting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const completed = verified ? jobs.filter((item) => item.status === "completed" && item.previewUrl).sort((left, right) => Number(Boolean(right.pinned)) - Number(Boolean(left.pinned))) : [];
  const orderedDerivedResults = [...derivedResults].sort((left, right) => Number(right.pinned) - Number(left.pinned));
  const visible = completed.slice(0, visibleCount);
  const resultKey = (kind: "job" | "derived", id: string) => `${kind}:${id}`;
  const existingResultIds = new Set([...completed.flatMap((item) => item.id ? [resultKey("job", item.id)] : []), ...orderedDerivedResults.map((item) => resultKey("derived", item.id))]);
  const effectiveSelectedIds = new Set([...selectedIds].filter((id) => existingResultIds.has(id)));
  const addToCanvas = async (item: Job) => {
    if (addingId) return;
    setAddingId(item.id);
    try { await onAdd(item); } finally { setAddingId(undefined); }
  };
  const saveToAssets = async (item: Job) => {
    if (savingId) return;
    setSavingId(item.id);
    try { await onSave(item); } finally { setSavingId(undefined); }
  };
  const removeResult = async (item: Job) => {
    if (deletingId || !item.id || !window.confirm(`确定删除这个${item.media === "image" ? "图片" : "视频"}结果吗？\n\n任务记录和生成文件会被删除；已保存到资产库的副本不会删除。`)) return;
    setDeletingId(item.id);
    try { await onDelete(item); } catch { /* parent reports the server error */ } finally { setDeletingId(undefined); }
  };
  const addDerived = async (item: DerivedMedia) => {
    if (addingId) return;
    setAddingId(item.id);
    try { await onAddDerived(item); } finally { setAddingId(undefined); }
  };
  const saveDerived = async (item: DerivedMedia) => {
    if (savingId || item.assetId) return;
    setSavingId(item.id);
    try { await onSaveDerived(item); } finally { setSavingId(undefined); }
  };
  const removeDerived = async (item: DerivedMedia) => {
    if (deletingId || !window.confirm(`确定删除派生结果“${item.displayName}”吗？\n\n画布中仍使用该未保存结果的节点会一并移除；已保存到资产的副本不会删除。`)) return;
    setDeletingId(item.id);
    try { await onDeleteDerived(item); } catch { /* parent reports the server error */ } finally { setDeletingId(undefined); }
  };
  const removeSelected = async () => {
    const selectedJobs = completed.filter((item) => item.id && effectiveSelectedIds.has(resultKey("job", item.id)));
    const selectedDerived = orderedDerivedResults.filter((item) => effectiveSelectedIds.has(resultKey("derived", item.id)));
    const count = selectedJobs.length + selectedDerived.length;
    if (!count || deletingId || !window.confirm(`确定删除选中的 ${count} 个结果吗？\n\n对应结果记录和生成/派生文件会被删除；已保存到资产库的副本保留。`)) return;
    setDeletingId("batch");
    const targets = [
      ...selectedJobs.map((item) => ({ key: resultKey("job", item.id!), run: () => onDelete(item) })),
      ...selectedDerived.map((item) => ({ key: resultKey("derived", item.id), run: () => onDeleteDerived(item) })),
    ];
    const settled = await Promise.allSettled(targets.map((target) => target.run()));
    const failed = new Set(targets.filter((_, index) => settled[index]?.status === "rejected").map((target) => target.key));
    setSelectedIds(failed);
    if (!failed.size) setSelecting(false);
    setDeletingId(undefined);
  };
  const toggleSelection = (id: string) => setSelectedIds((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const loadMore = async () => {
    if (visibleCount < completed.length) {
      setVisibleCount((count) => Math.min(count + RESULT_PAGE_SIZE, completed.length));
      return;
    }
    if (!hasMore || loadingMore) return;
    setLoadingMore(true);
    try {
      const loaded = await onLoadMore();
      setLocalPageError("");
      if (loaded > 0) setVisibleCount((count) => count + loaded);
    } catch (error) {
      setLocalPageError(error instanceof Error ? error.message : "下一页加载失败");
    } finally {
      setLoadingMore(false);
    }
  };
  const totalResults = completed.length + orderedDerivedResults.length;
  return <aside id="result-library-drawer" className="rail-drawer" aria-label="生成结果">
    <header className="rail-drawer-header"><div><strong>结果</strong><small>生成与剪辑派生结果</small></div><button type="button" aria-label="关闭结果抽屉" onClick={onClose}>×</button></header>
    <div className="library-actions"><button type="button" onClick={() => { setSelecting((value) => !value); setSelectedIds(new Set()); }}>{selecting ? "取消多选" : "多选管理"}</button><button type="button" onClick={() => void onRetry().catch(() => undefined)} disabled={state === "loading"}>↻ 刷新</button></div>
    <p className="library-state" role="status" aria-live="polite">{addingId ? "正在添加到画布…" : state === "loading" && !totalResults ? "正在验证服务器并加载结果…" : state === "refreshing" ? `已加载 ${totalResults} 个结果 · 正在后台刷新` : totalResults ? `已加载 ${totalResults} 个结果${derivedResults.length ? ` · 含 ${derivedResults.length} 个派生结果` : ""}` : "暂无已完成结果"}</p>
    {state === "error" && <div className="library-error" role="alert"><span>{error || "结果加载失败"}</span><button type="button" onClick={() => void onRetry().catch(() => undefined)}>重试</button></div>}
    {(localPageError || pageError) && <div className="library-error" role="alert"><span>下一页加载失败：{localPageError || pageError}</span><button type="button" onClick={() => void loadMore()}>重试下一页</button></div>}
    {selecting && <div className="library-bulk-actions"><button type="button" onClick={() => setSelectedIds(new Set([...orderedDerivedResults.map((item) => resultKey("derived", item.id)), ...visible.flatMap((item) => item.id ? [resultKey("job", item.id)] : [])]))}>全选当前</button><button type="button" className="library-delete" disabled={!effectiveSelectedIds.size || Boolean(deletingId)} onClick={() => void removeSelected()}>{deletingId === "batch" ? "删除中…" : `删除所选 (${effectiveSelectedIds.size})`}</button></div>}
    {state === "loading" && !totalResults && <div className="result-library-skeleton" aria-hidden="true"><i/><i/><i/></div>}
    <div className="result-library-list">{orderedDerivedResults.map((item) => <article key={`derived-${item.id}`} className={`result-library-card derived-result-card${item.pinned ? " pinned" : ""}${effectiveSelectedIds.has(resultKey("derived", item.id)) ? " selected" : ""}`}>
      {selecting && <label className="result-library-select"><input type="checkbox" checked={effectiveSelectedIds.has(resultKey("derived", item.id))} onChange={() => toggleSelection(resultKey("derived", item.id))} aria-label={`选择派生结果 ${item.displayName}`}/><span>选择</span></label>}
      <div className="result-library-preview" aria-label={`派生结果 ${item.displayName}`}>{item.kind === "audio" ? <LazyAudioPlayer source={item.contentUrl} label={item.displayName}/> : item.kind === "video" ? <LazyVideoPlayer source={item.contentUrl} label={item.displayName} thumbnailUrl={item.thumbnailUrl}/> : item.thumbnailUrl ? <img src={item.thumbnailUrl} alt="图片派生结果缩略图" loading="lazy" decoding="async" draggable={false}/> : <span className="result-library-video-placeholder" aria-hidden="true"><b>▧</b><small>派生结果</small></span>}<span className="result-library-preview-label">剪辑派生</span></div>
      <div className="result-library-meta"><strong title={item.displayName}>{item.pinned ? "★ " : ""}{item.displayName}</strong><small>{item.createdAt ? formatJobTime(item.createdAt) : "已持久化结果"}{item.assetId ? " · 已保存到资产" : " · 未保存到资产"}</small></div>
      <div className="result-library-actions">
        <button type="button" disabled={Boolean(addingId || savingId || deletingId)} onClick={() => void addDerived(item)} aria-label={`将派生结果 ${item.displayName} 添加到画布`}>{addingId === item.id ? "添加中…" : "＋ 添加到画布"}</button>
        <button type="button" className="result-save-asset" disabled={Boolean(addingId || savingId || deletingId || item.assetId)} onClick={() => void saveDerived(item)} aria-label={`将派生结果 ${item.displayName} 保存到资产`}>{item.assetId ? "✓ 已保存到资产" : savingId === item.id ? "保存中…" : "保存到资产"}</button>
        <DownloadAnchor href={item.downloadUrl ?? item.contentUrl} ariaLabel={`下载派生结果 ${item.displayName}`} label="下载" startedLabel="已开始" suggestedName={item.displayName}/>
        <button type="button" className="result-pin" disabled={Boolean(addingId || savingId || deletingId || pinningId)} onClick={() => { setPinningId(item.id); void onPinDerived(item, !item.pinned).catch(() => undefined).finally(() => setPinningId(undefined)); }} aria-label={`${item.pinned ? "取消置顶" : "置顶"}派生结果 ${item.displayName}`}>{pinningId === item.id ? "更新中…" : item.pinned ? "取消置顶" : "置顶"}</button>
        <button type="button" className="result-delete" disabled={Boolean(addingId || savingId || deletingId)} onClick={() => void removeDerived(item)} aria-label={`删除派生结果 ${item.displayName}`}>{deletingId === item.id ? "删除中…" : "删除结果"}</button>
      </div>
    </article>)}{visible.map((item) => <article key={item.id} className={`result-library-card ${item.id === currentId ? "current" : ""}${item.pinned ? " pinned" : ""}${item.id && effectiveSelectedIds.has(resultKey("job", item.id)) ? " selected" : ""}`}>
      {selecting && item.id && <label className="result-library-select"><input type="checkbox" checked={effectiveSelectedIds.has(resultKey("job", item.id))} onChange={() => toggleSelection(resultKey("job", item.id!))} aria-label={`选择任务结果 ${item.id}`}/><span>选择</span></label>}
      {item.media === "video" ? <div className="result-library-preview" aria-label={`视频任务结果 ${item.id}`}><ResultThumbnail job={item} key={`${item.id}:${item.thumbnailUrl ?? ""}`}/><button type="button" className="result-library-preview-label result-library-open-canvas" onClick={() => onSelect(item)} aria-label={`在画布预览任务 ${item.id}`}>在画布预览</button></div> : <button type="button" className="result-library-preview" onClick={() => onSelect(item)} aria-label={`在画布预览任务 ${item.id}`}><ResultThumbnail job={item} key={`${item.id}:${item.thumbnailUrl ?? ""}`}/><span className="result-library-preview-label">在画布预览</span></button>}
      <div className="result-library-meta"><strong>{item.pinned ? "★ " : ""}{item.parameters?.width && item.parameters?.height ? `${item.parameters.width}×${item.parameters.height}` : item.media === "image" ? "图片" : "视频"}</strong><small>{formatJobTime(item.createdAt)} · {formatJobElapsed(item.createdAt, item.updatedAt)}</small></div>
      {item.resume?.supported && (() => { const maximumAdditional = Math.max(0, item.resume.max_total_steps - item.resume.current_steps); const additional = Math.min(Math.max(1, resumeSteps[item.id!] ?? 1), Math.max(1, maximumAdditional)); const reason = item.resume.reason === "chain_busy" ? "同一任务链正在续跑" : item.resume.reason === "checkpoint_expired" ? "续跑点已过期" : item.resume.reason === "checkpoint_corrupt" ? "续跑点已损坏" : item.resume.reason === "checkpoint_state_mismatch" ? "任务与续跑点状态不一致" : item.resume.reason === "checkpoint_missing" ? "续跑点不存在" : item.resume.reason === "max_steps_reached" ? "已达到最大总步数" : item.resume.reason === "checkpoint_pending" ? "正在保存续跑点" : "续跑点暂不可用"; return <div className="result-resume-panel"><span>当前总步数：{item.resume.current_steps} / {item.resume.max_total_steps}</span>{item.resume.checkpoint_expires_at && <small>续跑点有效至：{new Date(item.resume.checkpoint_expires_at * 1000).toLocaleString("zh-CN")}</small>}<label><span>追加步数</span><input type="number" min="1" max={Math.max(1, maximumAdditional)} value={additional} onChange={(event) => setResumeSteps((current) => ({ ...current, [item.id!]: Math.min(Math.max(1, Math.floor(Number(event.target.value) || 1)), Math.max(1, maximumAdditional)) }))}/></label><small>续跑后总步数：{item.resume.current_steps + additional}</small><button type="button" disabled={!item.resume.can_resume || maximumAdditional < 1 || Boolean(resumingId)} onClick={() => { setResumingId(item.id); void onResume(item, additional).catch(() => undefined).finally(() => setResumingId(undefined)); }}>{resumingId === item.id ? "提交中…" : "继续生成"}</button>{!item.resume.can_resume && <small>{reason}</small>}</div>; })()}
      <div className="result-library-actions">
        <button type="button" title="添加到画布" disabled={Boolean(addingId || savingId || deletingId)} onClick={() => void addToCanvas(item)} aria-label={`将任务 ${item.id} 的${item.media === "image" ? "图片" : "视频"}添加到画布`}>{addingId === item.id ? "添加中…" : "＋ 添加到画布"}</button>
        <button type="button" title="保存到资产" className="result-save-asset" disabled={Boolean(addingId || savingId || deletingId || savedResultAssets[item.id!])} onClick={() => void saveToAssets(item)} aria-label={`将任务 ${item.id} 的${item.media === "image" ? "图片" : "视频"}保存到资产`}>{savedResultAssets[item.id!] ? "✓ 已保存到资产" : savingId === item.id ? "保存中…" : "保存到资产"}</button>
        {item.downloadUrl && <DownloadAnchor href={item.downloadUrl} ariaLabel={`下载任务 ${item.id}`} label="下载" startedLabel="已开始" suggestedName={`${item.id ?? "h3-result"}.${item.media === "image" ? "png" : "mp4"}`}/>}
        <button type="button" className="result-pin" disabled={Boolean(addingId || savingId || deletingId || pinningId)} onClick={() => { setPinningId(item.id); void onPin(item, !item.pinned).catch(() => undefined).finally(() => setPinningId(undefined)); }} aria-label={`${item.pinned ? "取消置顶" : "置顶"}任务结果 ${item.id}`}>{pinningId === item.id ? "更新中…" : item.pinned ? "取消置顶" : "置顶"}</button>
        <button type="button" className="result-delete" disabled={Boolean(addingId || savingId || deletingId)} onClick={() => void removeResult(item)} aria-label={`删除任务 ${item.id}`}>{deletingId === item.id ? "删除中…" : "删除结果"}</button>
      </div>
    </article>)}</div>
    {!pageError && !localPageError && verified && (visibleCount < completed.length || hasMore) && <button type="button" className="result-library-more" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在加载…" : visibleCount < completed.length ? `加载更多（剩余 ${completed.length - visible.length}）` : "加载下一页结果"}</button>}
    {!totalResults && state === "ready" && <div className="library-empty"><span>✦</span><strong>结果会出现在这里</strong><p>完成的生成任务与剪辑派生媒体都会保留在这里。</p></div>}
  </aside>;
}
function ParameterPanel({ kind, hasImageInput, video, setVideo, image, setImage, profile }: { kind: "video" | "image"; hasImageInput: boolean; video: VideoParams; setVideo: React.Dispatch<React.SetStateAction<VideoParams>>; image: ImageParams; setImage: React.Dispatch<React.SetStateAction<ImageParams>>; profile?: ProfileCapability }) {
  const aspect = kind === "video" ? video.aspectRatio : image.aspectRatio;
  const imageSize = imageDimensions(image.quality, image.aspectRatio);
  const bounds = (key: string, fallback: [number, number]): [number, number] => {
    const value = profile?.limits[key];
    return Array.isArray(value) && value.length === 2 ? [Number(value[0]), Number(value[1])] : fallback;
  };
  const durationBounds = bounds("duration", [5, H3_MAX_GENERATION_DURATION]);
  const stepBounds = bounds("steps", [1, 100]);
  const cfgBounds = bounds("cfg", [1, 30]);
  const denoiseBounds = bounds("denoise", [0.05, 1]);
  const loraBounds = bounds("lora_strength", [0, 2]);
  const turbo = profile?.sampling_mode === "turbo4";
  const fixedImageSteps = kind === "image" && stepBounds[0] === stepBounds[1];
  const fixedImageCfg = kind === "image" && cfgBounds[0] === cfgBounds[1];
  const imageLora = kind === "image" && profileSupportsParameter(profile, "lora_strength");
  const imageDenoise = kind === "image" && profileSupportsParameter(profile, "denoise");
  const zImage = isZImageProfile(profile);
  const profileReferencePolicy = imageReferencePolicy(profile);
  const zImageLatentImg2Img = zImage && imageDenoise && profileReferencePolicy.min === 1 && profileReferencePolicy.max === 1;
  const flux2 = isFlux2Profile(profile);
  const setAspect = (value: ImageAspectRatio) => {
    if (kind === "video") {
      if (value === "16:9" || value === "9:16") setVideo((current) => ({ ...current, aspectRatio: value }));
      return;
    }
    setImage((current) => ({ ...current, aspectRatio: value }));
  };
  return <div className="parameter-panel">
    <fieldset><legend>画面比例</legend><div className={`segmented ${kind === "image" ? "image-ratios" : ""}`}><button className={aspect === "16:9" ? "active" : ""} type="button" aria-pressed={aspect === "16:9"} onClick={() => setAspect("16:9")}><i className="ratio landscape"/>16:9</button><button className={aspect === "9:16" ? "active" : ""} type="button" aria-pressed={aspect === "9:16"} onClick={() => setAspect("9:16")}><i className="ratio portrait"/>9:16</button>{kind === "image" && <><button className={aspect === "3:4" ? "active" : ""} type="button" aria-pressed={aspect === "3:4"} onClick={() => setAspect("3:4")}><i className="ratio classic"/>3:4</button><button className={aspect === "1:1" ? "active" : ""} type="button" aria-pressed={aspect === "1:1"} onClick={() => setAspect("1:1")}><i className="ratio square"/>1:1</button></>}</div></fieldset>
    {kind === "video" ? <>
      <label className="control-label"><span>有效时长 <b>{effectiveDuration(video.duration).toFixed(2)}s</b></span><select value={video.duration} onChange={(event) => setVideo((current) => ({ ...current, duration: Number(event.target.value) }))}>{H3_DURATION_OPTIONS.filter((seconds) => seconds >= durationBounds[0] && seconds <= durationBounds[1]).map((seconds) => <option key={seconds} value={seconds}>{seconds.toFixed(2)} 秒 · {Math.round(seconds * 24)} 帧</option>)}</select><small>H3 只支持 17k+5 帧网格；这里显示真实输出时长。</small></label>
      <div className="two-controls"><label className="control-label" htmlFor="video-steps"><span>{turbo ? "Turbo LoRA 步数（4 推荐）" : "基础模型步数"}</span><BoundedNumberInput key={`video-steps-${video.steps}`} id="video-steps" min={stepBounds[0]} max={stepBounds[1]} value={video.steps} ariaDescribedBy="sampling-note" onCommit={(steps) => setVideo((current) => ({ ...current, steps }))}/></label><label className="control-label" htmlFor="video-lora-strength"><span>模型强度（LoRA）</span><BoundedNumberInput key={`video-lora-${turbo ? video.loraStrength : 0}`} id="video-lora-strength" min={loraBounds[0]} max={loraBounds[1]} step={0.05} value={turbo ? video.loraStrength : 0} disabled={!turbo} onCommit={(loraStrength) => setVideo((current) => ({ ...current, loraStrength }))}/></label></div>
      <label className="control-label"><span>调度去噪比例（实验） <b>{video.denoise.toFixed(2)}</b></span><input type="range" min={denoiseBounds[0]} max={denoiseBounds[1]} step="0.05" value={video.denoise} onChange={(event) => setVideo((current) => ({ ...current, denoise: Number(event.target.value) }))}/><div className="range-labels"><small>截断调度前段</small><small>1.00：完整调度</small></div><small>直接对应 H3 BasicScheduler.denoise；不是 CFG 或参考权重。官方模板默认 1.00，其他值请视为实验参数。</small></label>
      <Seed value={video.seed} update={(seed) => setVideo((current) => ({ ...current, seed }))}/><div className="model-note" id="sampling-note"><span>i</span><p>{turbo ? <><strong>Turbo LoRA 模式</strong>默认并推荐 4 步，但可在当前 Profile 的 {stepBounds[0]}..{stepBounds[1]} 内调整；模型强度独立对应 ComfyUI 的 LoraLoaderModelOnly.strength_model。增加步数不保证画质一定更好。</> : <><strong>基础质量模式</strong>不加载蒸馏 LoRA；步数可在当前 Profile 允许范围内调整。</>}</p></div>
    </> : <>
      <fieldset><legend>图片质量 <span>{imageSize.width}×{imageSize.height}</span></legend><div className="segmented quality-options"><button className={image.quality === "1K" ? "active" : ""} type="button" aria-pressed={image.quality === "1K"} onClick={() => setImage((current) => ({ ...current, quality: "1K" }))}>1K</button><button className={image.quality === "2K" ? "active" : ""} type="button" aria-pressed={image.quality === "2K"} onClick={() => setImage((current) => ({ ...current, quality: "2K" }))}>2K</button></div><small className="quality-note">2K 像素数约为 1K 的 4 倍，显存与生成时间会更高。</small></fieldset>
      <div className="two-controls"><label className="control-label" htmlFor="image-steps"><span>图片步数{fixedImageSteps ? "（模型固定）" : ""}</span><BoundedNumberInput key={`image-steps-${image.steps}`} id="image-steps" min={stepBounds[0]} max={stepBounds[1]} value={image.steps} disabled={fixedImageSteps} onCommit={(steps) => setImage((current) => ({ ...current, steps }))}/></label><label className="control-label" htmlFor="image-cfg"><span>CFG{fixedImageCfg ? "（模型固定）" : ""}</span><BoundedNumberInput key={`image-cfg-${image.cfg}`} id="image-cfg" min={cfgBounds[0]} max={cfgBounds[1]} step={0.5} value={image.cfg} disabled={fixedImageCfg} onCommit={(cfg) => setImage((current) => ({ ...current, cfg }))}/></label></div>
      {imageLora && <label className="control-label image-lora-control"><span>LoRA 模型强度 <b>{image.loraStrength.toFixed(2)}</b></span><input type="range" min={loraBounds[0]} max={loraBounds[1]} step="0.05" value={image.loraStrength} onChange={(event) => setImage((current) => ({ ...current, loraStrength: Number(event.target.value) }))}/><div className="range-labels"><small>弱 LoRA 效果</small><small>强 LoRA 效果</small></div><small>只调节 Profile 所配 LoRA 的模型权重；不是底图重绘强度。</small></label>}
      {hasImageInput && imageDenoise && <label className="control-label image-denoise-control"><span>img2img 重绘强度（denoise） <b>{image.denoise.toFixed(2)}</b></span><input type="range" min={denoiseBounds[0]} max={denoiseBounds[1]} step="0.05" value={image.denoise} onChange={(event) => setImage((current) => ({ ...current, denoise: Number(event.target.value) }))}/><div className="range-labels"><small>保留底图</small><small>大幅重绘</small></div><small>只控制底图被重绘的幅度；与上方 LoRA 模型强度互不关联。</small></label>}
      {imageLora && <div className="adult-content-notice" role="note"><strong>成人内容使用边界</strong><p>仅限合法、自愿且可确认年满 18 岁的成年人内容。严禁未成年人、非自愿私密内容和未经授权的真实人物色情深伪；请确认素材权利与当地法律。</p></div>}
      {!zImage && !flux2 && <label className="control-label"><span>Negative Prompt</span><textarea className="negative-prompt" value={image.negativePrompt} onChange={(event) => setImage((current) => ({ ...current, negativePrompt: event.target.value }))}/></label>}<Seed value={image.seed} update={(seed) => setImage((current) => ({ ...current, seed }))}/><div className="model-note"><span>i</span><p>{zImageLatentImg2Img ? <><strong>Z-Image Turbo latent img2img{imageLora ? " + LoRA" : ""}</strong>只接收一张底图，经 VAE 编码后由 denoise 控制重绘幅度。这是 latent 重绘，不是 Z-Image-Edit，也不应当作指令式语义编辑。</> : zImage ? imageLora ? <><strong>Z-Image Turbo + LoRA</strong>参数与模型由当前 Profile 提供；文生图与单图 img2img 会按参考图能力自动区分。</> : <><strong>Z-Image Turbo</strong>官方蒸馏工作流使用 8 步、CFG 1，负向条件归零；适合快速写实和中英文字。</> : flux2 ? <><strong>FLUX.2 Klein</strong>同一 Profile 支持纯文本生图和 1..4 张有序参考。用“图1 / 图2”指定人物、服装、场景或风格；Klein 不使用 Negative Prompt 或 denoise 重绘强度。Distilled 4B / 9B Profile 均固定为 4 步 / CFG 1。</> : profile?.compiler === "qwen_image_t2i" ? <><strong>Qwen-Image 2512</strong>官方基础工作流默认 50 步 / CFG 4；适合人像、自然细节和图文排版。</> : profile?.compiler === "qwen_image_edit" ? <><strong>Qwen-Image Edit 2511</strong>用一张底图做指令式编辑；直接写“保留人物，把背景改为…”。重绘强度对应 KSampler.denoise。</> : <><strong>兼容模式</strong>使用普通 Checkpoint 工作流。</>}{profile?.use_notice ? <em>{profile.use_notice}</em> : null}{profile?.license_url ? <a href={profile.license_url} target="_blank" rel="noreferrer">查看 {profile.license_id ?? "模型"} 许可</a> : null}</p></div>
    </>}
  </div>;
}
function BoundedNumberInput({ id, value, min, max, step, disabled, ariaDescribedBy, onCommit }: { id: string; value: number; min: number; max: number; step?: number; disabled?: boolean; ariaDescribedBy?: string; onCommit: (value: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  const commit = () => {
    const parsed = draft.trim() === "" ? Number.NaN : Number(draft);
    const next = Number.isFinite(parsed) ? Math.max(min, Math.min(max, parsed)) : value;
    setDraft(String(next));
    if (next !== value) onCommit(next);
  };
  return <input id={id} type="number" min={min} max={max} step={step} value={draft} disabled={disabled} aria-describedby={ariaDescribedBy} onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={(event) => {
    if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); }
    if (event.key === "Escape") { event.preventDefault(); setDraft(String(value)); event.currentTarget.blur(); }
  }}/>;
}
function Seed({ value, update }: { value: number; update: (value: number) => void }) { return <label className="control-label"><span>Seed <em>−1 为随机</em></span><div className="seed-input"><input type="number" value={value} onChange={(event) => update(Number(event.target.value))}/><button type="button" title="随机 Seed" aria-label="生成随机 Seed" onClick={() => update(Math.floor(Math.random() * 2147483647))}>↻</button></div></label>; }
