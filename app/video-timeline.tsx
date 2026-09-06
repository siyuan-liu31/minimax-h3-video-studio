"use client";

/* Authenticated preview routes are intentionally rendered directly. */
/* eslint-disable @next/next/no-img-element */
/* eslint-disable jsx-a11y/media-has-caption */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StudioJob } from "./studio-history";
import { deleteDerivedMedia, deriveLibraryMedia, saveDerivedMedia, type LibraryAsset } from "./studio-library";
import { H3_REFERENCE_PROMPT_TEMPLATE } from "./studio-prompt";
import PromptMentionComposer, { type PromptMentionItem } from "./prompt-mentions";
import {
  allRunSelection,
  buildStoryboardDraft,
  invertRunSelection,
  normalizeRunSelection,
  parseSceneCutSuggestions,
  type SceneCutSuggestion,
} from "./video-director-model";
import { SequenceVideoMonitor, SourceRangePanel, StoryboardTimeline } from "./video-director-workspace";
import { VideoProjectApi } from "./video-project-api";
import {
  appendTimelineReference,
  continuationChoices,
  continuationSourceReady,
  defaultVideoContinuationRange,
  draftVideoMediaSegment,
  draftVideoProject,
  draftVideoSegment,
  findTimelineProfile,
  h3EffectiveDuration,
  H3_GENERATION_FPS,
  H3_MAX_REFERENCE_FILES,
  H3_MAX_CONTINUATION_FRAMES,
  isVideoMediaSegment,
  h3ReferenceTagMap,
  mergeVideoProject,
  latestResolvedWorkflow,
  moveVideoSegment,
  profileBounds,
  profileDurationOptions,
  projectMergeBlockReason,
  resolvedWorkflowSummary,
  retargetSegmentCompiler,
  retargetSegmentForReferences,
  retargetSegmentSampling,
  notifyMergedResultOnce,
  normalizeVideoContinuationRange,
  projectCanMerge,
  projectIsActive,
  removeVideoProjectSegment,
  resolveVideoSequencePosition,
  selectedTimelineRunPlan,
  serializeVideoProject,
  timelineRequiredCompiler,
  timelineStatusLabel,
  timelinePromptPreview,
  timelineWorkflowModeLabel,
  validateVideoProject,
  videoSequenceTimeForSegment,
  videoSegmentDuration,
  type ContinuationMode,
  type TimelineProfile,
  type TimelineReference,
  type H3ReferenceTags,
  type VideoProject,
  type VideoContinuationRange,
  type VideoSegment,
} from "./video-project";

type Props = {
  assets: LibraryAsset[];
  results: StudioJob[];
  profiles: TimelineProfile[];
  onUploadVideo: (file: File) => Promise<LibraryAsset>;
  onImportResult: (jobId: string) => Promise<LibraryAsset>;
  onAssetCreated: (asset: LibraryAsset) => void | Promise<void>;
  onResultCreated: () => void;
  onClose: () => void;
};
const API = new VideoProjectApi();
type DisplayAsset = LibraryAsset & {
  name?: string;
  folder?: string;
  thumbnail_url?: string;
  thumbnailUrl?: string;
};

function displayAsset(asset: LibraryAsset): DisplayAsset { return asset as DisplayAsset; }
function assetLabel(asset: LibraryAsset): string { return displayAsset(asset).name?.trim() || asset.filename; }
function assetThumbnail(asset: LibraryAsset): string | undefined {
  return displayAsset(asset).thumbnail_url || displayAsset(asset).thumbnailUrl;
}
function mentionItem(asset: LibraryAsset, connected: boolean): PromptMentionItem {
  return {
    id: asset.id,
    label: assetLabel(asset),
    kind: asset.kind,
    previewUrl: assetThumbnail(asset),
    connected,
  };
}

function AssetThumb({ asset, size = 42 }: { asset: LibraryAsset; size?: number }) {
  const thumbnail = assetThumbnail(asset);
  const style = { width: size, height: size, borderRadius: 7, objectFit: "cover" as const, flex: "0 0 auto" };
  if (thumbnail) return <img src={thumbnail} alt="" loading="lazy" decoding="async" style={style}/>;
  return <span aria-hidden="true" style={{ ...style, display: "grid", placeItems: "center", background: "#252934", color: "#aaa2ff", fontSize: 18 }}>{asset.kind === "video" ? "▶" : asset.kind === "audio" ? "♪" : "▧"}</span>;
}

function LazyVideoPreview({ src, thumbnailUrl, label }: { src: string; thumbnailUrl?: string; label: string }) {
  const [loaded, setLoaded] = useState(false);
  if (loaded) {
    // Generated media has no separate caption track at preview time.
    return <video src={src} controls autoPlay playsInline preload="metadata" aria-label={label}/>;
  }
  return <button
    type="button"
    className="timeline-video-load"
    aria-label={`加载${label}`}
    onClick={() => setLoaded(true)}
    style={{ position: "relative", display: "grid", placeItems: "center", minHeight: 108, overflow: "hidden", background: "#05070b", color: "white" }}
  >
    {thumbnailUrl ? <img src={thumbnailUrl} alt="" loading="lazy" decoding="async" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }}/> : null}
    <span style={{ position: "relative", display: "grid", placeItems: "center", width: 42, height: 42, borderRadius: "50%", background: "rgba(5,7,11,.72)", fontSize: 18 }}>▶</span>
  </button>;
}

type ImportTab = "assets" | "results" | "local";

function VideoImportDialog({
  assets,
  results,
  busy,
  onChooseAsset,
  onChooseResult,
  onUpload,
  onClose,
}: {
  assets: LibraryAsset[];
  results: StudioJob[];
  busy: boolean;
  onChooseAsset: (asset: LibraryAsset) => void;
  onChooseResult: (result: StudioJob) => void;
  onUpload: (files: File[]) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<ImportTab>("assets");
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const videoAssets = useMemo(() => assets.filter((asset) => asset.kind === "video" && assetLabel(asset).toLocaleLowerCase().includes(query.toLocaleLowerCase())), [assets, query]);
  const videoResults = useMemo(() => results.filter((item) => item.status === "completed" && item.media === "video" && Boolean(item.id) && `${item.id} ${item.prompt ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())), [query, results]);
  const acceptFiles = (files: FileList | File[]) => {
    const videos = Array.from(files).filter((file) => file.type.startsWith("video/"));
    if (videos.length) onUpload(videos);
  };
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [busy, onClose]);
  return <div className="timeline-import-backdrop" onPointerDown={(event) => { if (event.currentTarget === event.target && !busy) onClose(); }}>
    <section className="timeline-import-dialog" role="dialog" aria-modal="true" aria-labelledby="timeline-import-title">
      <header><div><strong id="timeline-import-title">导入已有视频</strong><small>直接加入成片时间线，不会触发 H3 重新生成</small></div><button type="button" disabled={busy} aria-label="关闭导入视频" onClick={onClose}>×</button></header>
      <div className="timeline-import-tabs" role="tablist" aria-label="视频来源">
        <button type="button" role="tab" aria-selected={tab === "assets"} onClick={() => setTab("assets")}>资产库视频</button>
        <button type="button" role="tab" aria-selected={tab === "results"} onClick={() => setTab("results")}>历史结果</button>
        <button type="button" role="tab" aria-selected={tab === "local"} onClick={() => setTab("local")}>本地视频</button>
      </div>
      {tab !== "local" && <label className="timeline-import-search"><span>搜索</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tab === "assets" ? "搜索资产名称" : "搜索任务或提示词"}/></label>}
      <div className="timeline-import-content">
        {tab === "assets" && <div className="timeline-import-grid">{videoAssets.map((asset) => <button key={asset.id} type="button" disabled={busy} onClick={() => onChooseAsset(asset)}><AssetThumb asset={asset} size={96}/><span><strong>{assetLabel(asset)}</strong><small>{asset.media.width && asset.media.height ? `${asset.media.width}×${asset.media.height}` : "视频"}{asset.media.duration ? ` · ${asset.media.duration.toFixed(2)}s` : ""}</small></span></button>)}{!videoAssets.length && <p>资产库里还没有可用视频。</p>}</div>}
        {tab === "results" && <div className="timeline-import-grid">{videoResults.map((result) => <button key={result.id} type="button" disabled={busy} onClick={() => onChooseResult(result)}>{result.thumbnailUrl ? <img src={result.thumbnailUrl} alt="" loading="lazy" decoding="async"/> : <span className="timeline-import-result-placeholder">▶</span>}<span><strong>{result.id?.slice(0, 12)}</strong><small>{result.parameters?.width && result.parameters?.height ? `${result.parameters.width}×${result.parameters.height}` : "生成视频"}{result.parameters?.duration_actual ? ` · ${Number(result.parameters.duration_actual).toFixed(2)}s` : ""}</small></span></button>)}{!videoResults.length && <p>结果库里还没有已完成的视频。</p>}</div>}
        {tab === "local" && <div className="timeline-local-drop" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (!busy) acceptFiles(event.dataTransfer.files); }}><span>⇧</span><strong>{busy ? "正在上传并读取视频信息…" : "把本地视频拖到这里"}</strong><small>仅接受视频文件；上传成功后会保存到资产库并插入时间线。</small><button type="button" disabled={busy} onClick={() => inputRef.current?.click()}>选择本地视频</button><input ref={inputRef} className="visually-hidden" type="file" accept="video/*" multiple onChange={(event) => { if (event.target.files) acceptFiles(event.target.files); event.target.value = ""; }}/></div>}
      </div>
    </section>
  </div>;
}

function ContinuationRangeEditor({ previous, index, range, disabled, videoTag, onChange }: { previous: VideoSegment; index: number; range?: VideoContinuationRange; disabled: boolean; videoTag: string; onChange: (range: VideoContinuationRange) => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const duration = previous.request.parameters.duration;
  const totalFrames = Math.max(1, Math.round(duration * H3_GENERATION_FPS));
  const normalized = normalizeVideoContinuationRange(range, duration);
  const selectedFrames = normalized.end_frame - normalized.start_frame;
  const seekPreview = (frame: number) => {
    if (videoRef.current) videoRef.current.currentTime = Math.min(duration, Math.max(0, frame / H3_GENERATION_FPS));
  };
  const updateStart = (frame: number) => {
    const startFrame = Math.max(0, Math.min(normalized.end_frame - 1, Math.round(frame)));
    const next = { ...normalized, start_frame: startFrame, end_frame: Math.min(normalized.end_frame, startFrame + H3_MAX_CONTINUATION_FRAMES) };
    seekPreview(startFrame);
    onChange(next);
  };
  const updateEnd = (frame: number) => {
    const endFrame = Math.max(normalized.start_frame + 1, Math.min(totalFrames, normalized.start_frame + H3_MAX_CONTINUATION_FRAMES, Math.round(frame)));
    seekPreview(endFrame - 1);
    onChange({ ...normalized, end_frame: endFrame });
  };
  return <section className="continuation-range-editor" aria-label="上一段视频续接选区">
    <div className="continuation-range-preview">
      {previous.preview_url ? <>
        <video ref={videoRef} src={previous.preview_url} controls muted playsInline preload="metadata" aria-label="上一段视频选区预览"/>
      </> : previous.thumbnail_url ? <img src={previous.thumbnail_url} alt="上一段视频缩略图" loading="lazy" decoding="async"/> : <span><b>▶</b>上一段完成后可预览</span>}
    </div>
    <div className="continuation-range-controls">
      <header><div><strong>隐式视频：分段 {index} → <code>{videoTag}</code></strong><small>选择作为 H3 有序视频参考的范围</small></div><b>{(selectedFrames / H3_GENERATION_FPS).toFixed(2)}s · {selectedFrames} 帧</b></header>
      <div className="continuation-dual-range" style={{ "--range-start": `${normalized.start_frame / totalFrames * 100}%`, "--range-end": `${normalized.end_frame / totalFrames * 100}%` } as React.CSSProperties}>
        <span aria-hidden="true"/>
        <input type="range" min="0" max={Math.max(0, totalFrames - 1)} step="1" value={normalized.start_frame} disabled={disabled} onChange={(event) => updateStart(Number(event.target.value))} aria-label="上一段视频续接入点"/>
        <input type="range" min="1" max={totalFrames} step="1" value={normalized.end_frame} disabled={disabled} onChange={(event) => updateEnd(Number(event.target.value))} aria-label="上一段视频续接出点"/>
      </div>
      <div className="continuation-range-numbers">
        <label><span>入点帧</span><input type="number" inputMode="numeric" min="0" max={Math.max(0, normalized.end_frame - 1)} step="1" value={normalized.start_frame} disabled={disabled} onChange={(event) => updateStart(Number(event.target.value))} aria-label="上一段视频续接入点帧号"/></label>
        <label><span>出点帧（不含）</span><input type="number" inputMode="numeric" min={normalized.start_frame + 1} max={Math.min(totalFrames, normalized.start_frame + H3_MAX_CONTINUATION_FRAMES)} step="1" value={normalized.end_frame} disabled={disabled} onChange={(event) => updateEnd(Number(event.target.value))} aria-label="上一段视频续接出点帧号（不含）"/></label>
      </div>
      <div className="continuation-range-readout"><span>入点 <b>{(normalized.start_frame / H3_GENERATION_FPS).toFixed(2)}s</b> · F{normalized.start_frame}</span><span>出点（不含） <b>{(normalized.end_frame / H3_GENERATION_FPS).toFixed(2)}s</b> · F{normalized.end_frame}</span><span>最后包含帧 <b>F{normalized.end_frame - 1}</b> · 上限 {H3_MAX_CONTINUATION_FRAMES} 帧</span></div>
      <p><strong>运行时自动裁剪静音副本，不修改上一段。</strong> 音频：关闭（默认）。拖动入点或出点时，左侧预览会定位到对应时间；副本不携带 Audio 标签，也不改写本段 Prompt。</p>
    </div>
  </section>;
}

function clientId() { return crypto.randomUUID().replaceAll("-", ""); }

function projectBase(profiles: TimelineProfile[]) {
  return draftVideoProject(clientId(), profiles.find((profile) => profile.output_type === "video" && profile.available));
}

function hydrateProject(remote: VideoProject, profiles: TimelineProfile[], current?: VideoProject) {
  const base = current ?? { ...projectBase(profiles), segments: [] };
  return mergeVideoProject(base, remote);
}

export default function VideoTimeline({ assets, results, profiles, onUploadVideo, onImportResult, onAssetCreated, onResultCreated, onClose }: Props) {
  const videoProfiles = useMemo(() => profiles.filter((profile) => profile.output_type === "video" && profile.available), [profiles]);
  const [projects, setProjects] = useState<VideoProject[]>([]);
  const [project, setProject] = useState<VideoProject>();
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [runSelection, setRunSelection] = useState<Set<string>>(new Set());
  const [sourceId, setSourceId] = useState("");
  const [sourceTime, setSourceTime] = useState(0);
  const [sequenceTime, setSequenceTime] = useState(0);
  const [sceneSuggestions, setSceneSuggestions] = useState<SceneCutSuggestion[]>([]);
  const [analyzingScenes, setAnalyzingScenes] = useState(false);
  const [assetAction, setAssetAction] = useState("");
  const [assetNotice, setAssetNotice] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const assetActionRef = useRef("");
  const importAfterIndexRef = useRef(0);
  const projectRef = useRef<VideoProject | undefined>(undefined);
  const editRevisionRef = useRef(0);
  const notifiedResultsRef = useRef(new Set<string>());
  const selectionProjectRef = useRef("");
  useEffect(() => { projectRef.current = project; }, [project]);

  const segmentIdentity = project?.segments.map((segment) => segment.id).join("|") ?? "";
  useEffect(() => {
    if (!project) return;
    const identity = project.id ?? project.segments[0]?.id ?? "draft";
    if (selectionProjectRef.current !== identity) {
      selectionProjectRef.current = identity;
      const restored = Array.isArray(project.selected_segment_ids) && project.selected_segment_ids.length
        ? normalizeRunSelection(project.segments, new Set(project.selected_segment_ids))
        : allRunSelection(project.segments);
      setRunSelection(restored);
      setSelectedIndex(0);
      setSequenceTime(0);
      return;
    }
    setRunSelection((current) => normalizeRunSelection(project.segments, current));
    setSelectedIndex((current) => Math.max(0, Math.min(current, project.segments.length - 1)));
    setSequenceTime((current) => resolveVideoSequencePosition(project.segments, current).time);
  }, [project, segmentIdentity]);

  const notifyResultCreated = useCallback((value: VideoProject) => {
    notifyMergedResultOnce(value, notifiedResultsRef.current, onResultCreated);
  }, [onResultCreated]);

  const remember = useCallback((value: VideoProject) => {
    projectRef.current = value;
    setProject(value);
    setProjects((current) => [value, ...current.filter((item) => item.id !== value.id)]);
    setDirty(false);
    setSourceId(value.storyboard?.source_asset_id ?? "");
    notifyResultCreated(value);
  }, [notifyResultCreated]);

  const beginDraft = useCallback(() => {
    const value = projectBase(videoProfiles);
    editRevisionRef.current += 1;
    projectRef.current = value;
    setProject(value);
    setDirty(true);
    setError("");
    setSourceId("");
    setSourceTime(0);
    setSequenceTime(0);
    setSceneSuggestions([]);
    setAssetNotice("");
  }, [videoProfiles]);

  const openProject = useCallback(async (id: string) => {
    setLoading(true); setError("");
    try {
      const remote = await API.get(id);
      remember(hydrateProject(remote, videoProfiles));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "项目读取失败"); }
    finally { setLoading(false); }
  }, [remember, videoProfiles]);

  useEffect(() => {
    let canceled = false;
    void API.list().then(async (items) => {
      if (canceled) return;
      setProjects(items);
      const first = items.find((item) => item.id);
      if (first?.id) {
        const detail = await API.get(first.id);
        if (!canceled) remember(hydrateProject(detail, videoProfiles));
      } else if (!canceled) setProject(projectBase(videoProfiles));
    }).catch((caught) => { if (!canceled) { setProject(projectBase(videoProfiles)); setError(caught instanceof Error ? caught.message : "无法恢复长视频项目"); } }).finally(() => { if (!canceled) setLoading(false); });
    return () => { canceled = true; };
  }, [remember, videoProfiles]);

  useEffect(() => {
    if (!project?.id || dirty) return;
    let stopped = false;
    const poll = async () => {
      const revision = editRevisionRef.current;
      try {
        const remote = await API.get(project.id!);
        if (!stopped && revision === editRevisionRef.current) {
          const next = hydrateProject(remote, videoProfiles, projectRef.current);
          projectRef.current = next;
          setProject(next);
          setProjects((current) => [next, ...current.filter((item) => item.id !== next.id)]);
          notifyResultCreated(next);
        }
      } catch (caught) { if (!stopped) setError(caught instanceof Error ? caught.message : "项目状态更新失败"); }
    };
    const timer = window.setInterval(() => void poll(), 2500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [dirty, notifyResultCreated, project?.id, videoProfiles]);

  const edit = useCallback((update: (current: VideoProject) => VideoProject) => {
    editRevisionRef.current += 1;
    setProject((current) => {
      if (!current) return current;
      const next = update(current);
      projectRef.current = next;
      return next;
    });
    setDirty(true); setError("");
  }, []);

  const persist = useCallback(async () => {
    const current = projectRef.current;
    if (!current) throw new Error("没有可保存的长视频项目");
    const remote = current.id ? await API.save(current.id, serializeVideoProject(current)) : await API.create(serializeVideoProject(current));
    const hydrated = hydrateProject(remote, videoProfiles, current);
    remember(hydrated);
    return hydrated;
  }, [remember, videoProfiles]);

  const perform = useCallback(async (label: string, operation: (saved: VideoProject) => Promise<VideoProject>, validate = true) => {
    const current = projectRef.current;
    if (!current) return;
    if (validate) {
      const problems = validateVideoProject(current, videoProfiles, assets);
      if (problems.length) { setError(problems[0]); return; }
    }
    setAction(label); setError("");
    try {
      const saved = !current.id || dirty ? await persist() : current;
      const remote = await operation(saved);
      remember(hydrateProject(remote, videoProfiles, saved));
    } catch (caught) { setError(caught instanceof Error ? caught.message : `${label}失败`); }
    finally { setAction(""); }
  }, [assets, dirty, persist, remember, videoProfiles]);

  const save = async () => {
    setAction("saving"); setError("");
    try { await persist(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "保存失败"); }
    finally { setAction(""); }
  };

  const deleteProject = async () => {
    const current = projectRef.current;
    if (!current?.id || action || !window.confirm(`确定删除长视频项目“${current.title}”吗？\n\n项目定义和项目合并文件会被删除；生成历史中的单段结果仍保留。`)) return;
    setAction("deleting-project"); setError("");
    try {
      await API.delete(current.id);
      const remaining = projects.filter((item) => item.id !== current.id);
      setProjects(remaining);
      const next = remaining.find((item) => item.id);
      if (next?.id) await openProject(next.id);
      else beginDraft();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除项目失败");
    } finally { setAction(""); }
  };

  const selectStoryboardSegment = (index: number) => {
    const current = projectRef.current;
    if (!current || index < 0 || index >= current.segments.length) return;
    setSelectedIndex(index);
    const range = current.segments[index]?.source_range;
    if (range) setSourceTime(range.start_frame / range.fps);
  };

  const seekSequence = useCallback((seconds: number) => {
    const position = resolveVideoSequencePosition(projectRef.current?.segments ?? [], seconds);
    setSequenceTime((current) => Math.abs(current - position.time) < (1 / 240) ? current : position.time);
    if (position.index >= 0) setSelectedIndex((current) => current === position.index ? current : position.index);
  }, []);

  const sourceDescriptor = (assetId: string) => {
    const source = assets.find((asset) => asset.id === assetId && asset.kind === "video");
    if (!source) return undefined;
    const fps = Number(source.media.source_fps ?? source.media.fps ?? source.media.reference_fps ?? 24);
    const frameCount = Number(source.media.frame_count ?? Math.round(Number(source.media.duration ?? 0) * fps));
    if (!Number.isFinite(fps) || fps <= 0 || !Number.isInteger(frameCount) || frameCount < 1) return undefined;
    return { source_asset_id: source.id, fps, frame_count: frameCount };
  };

  const bindSource = (assetId: string) => {
    setSourceId(assetId);
    setSourceTime(0);
    setSceneSuggestions([]);
    setAssetNotice("");
    if (!assetId) {
      edit((current) => ({ ...current, storyboard: undefined, segments: current.segments.map((segment) => ({ ...segment, source_range: undefined })) }));
      return;
    }
    const source = sourceDescriptor(assetId);
    const current = projectRef.current;
    if (!source || !current) {
      setError("该视频缺少可用的时长/帧率信息，无法建立帧级分镜。");
      return;
    }
    const draft = buildStoryboardDraft(current.segments, source, [], videoProfiles, clientId);
    setRunSelection(allRunSelection(draft.segments));
    setSelectedIndex(0);
    edit((value) => ({ ...value, storyboard: draft.storyboard, segments: draft.segments }));
  };

  const splitAtCurrentFrame = () => {
    const current = projectRef.current;
    if (!current?.storyboard) return;
    const frame = Math.round(sourceTime * current.storyboard.fps);
    const cuts = [...current.storyboard.cut_frames, frame];
    const draft = buildStoryboardDraft(current.segments, current.storyboard, cuts, videoProfiles, clientId);
    if (draft.storyboard.cut_frames.length === current.storyboard.cut_frames.length) {
      setError("当前帧已是分镜边界，请移动播放头后重试。");
      return;
    }
    setRunSelection(allRunSelection(draft.segments));
    setSelectedIndex(Math.max(0, draft.segments.findIndex((segment) => segment.source_range && frame >= segment.source_range.start_frame && frame < segment.source_range.end_frame)));
    edit((value) => ({ ...value, storyboard: draft.storyboard, segments: draft.segments }));
  };

  const equalizeDraft = (count: number) => {
    const current = projectRef.current;
    if (!current?.storyboard) return;
    const safeCount = Math.max(1, Math.min(64, Math.round(count) || 1));
    const cuts = Array.from({ length: safeCount - 1 }, (_, index) => Math.round(current.storyboard!.frame_count * (index + 1) / safeCount));
    const draft = buildStoryboardDraft(current.segments, current.storyboard, cuts, videoProfiles, clientId);
    setRunSelection(allRunSelection(draft.segments));
    setSelectedIndex(0);
    edit((value) => ({ ...value, storyboard: draft.storyboard, segments: draft.segments }));
  };

  const cropSelectedSourceRange = async () => {
    const current = projectRef.current;
    const segment = current?.segments[selectedIndex];
    const range = segment?.source_range;
    if (!segment || !range || assetActionRef.current) return;
    if (segment.continuation === "tail_frame") {
      setError("尾帧续接只接受图像参考；请先将续接方式改为“无续接”再保存源视频片段。");
      return;
    }
    assetActionRef.current = segment.id;
    setAssetAction(segment.id); setAssetNotice(""); setError("");
    let derivedId = "";
    try {
      const derived = await deriveLibraryMedia(
        { type: "asset", asset_id: range.asset_id },
        { operation: "video_trim", start: range.start_frame / range.fps, end: range.end_frame / range.fps },
      );
      derivedId = derived.id;
      const source = assets.find((asset) => asset.id === range.asset_id);
      const saved = await saveDerivedMedia(derived.id, `${source?.filename.replace(/\.[^.]+$/, "") || "source"}-shot-${selectedIndex + 1}`);
      derivedId = "";
      await onAssetCreated(saved);
      const sourceDuration = (range.end_frame - range.start_frame) / range.fps;
      if (sourceDuration < 2) {
        setAssetNotice("已保存为新视频资产。该片段不足 2 秒，不符合 H3 视频参考的 2..15 秒范围；Prompt 和参考保持不变。");
        return;
      }
      // source_range is already an implicit Ref2VA video input. Adding the
      // saved copy again would double-count the same clip and can exceed H3's
      // 15-second video-reference budget.
      setAssetNotice("已保存为可复用视频资产。本段已通过来源区间隐式引用该视频，Prompt 和参考保持不变。");
    } catch (caught) {
      if (derivedId) await deleteDerivedMedia(derivedId).catch(() => undefined);
      setError(caught instanceof Error ? caught.message : "裁剪并保存失败");
    } finally { assetActionRef.current = ""; setAssetAction(""); }
  };

  const analyzeScenes = async () => {
    const analysisSourceId = projectRef.current?.storyboard?.source_asset_id ?? sourceId;
    if (!analysisSourceId || analyzingScenes) return;
    setAnalyzingScenes(true);
    setError("");
    setSceneSuggestions([]);
    try {
      const response = await fetch("/api/media/analyze-scenes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: analysisSourceId }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = body && typeof body === "object" && "error" in body
          ? (body as { error?: { message?: string } }).error?.message
          : undefined;
        throw new Error(message || `智能分镜分析失败 (${response.status})`);
      }
      const source = assets.find((asset) => asset.id === analysisSourceId);
      const suggestions = parseSceneCutSuggestions(body, Number(source?.media.duration ?? Number.POSITIVE_INFINITY));
      setSceneSuggestions(suggestions);
      if (!suggestions.length) setError("分析完成，但没有找到可用的建议切点。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "智能分镜分析失败");
    } finally {
      setAnalyzingScenes(false);
    }
  };

  const removeSegment = (segmentId: string) => {
    const current = projectRef.current;
    if (!current) return;
    const removedIndex = current.segments.findIndex((item) => item.id === segmentId);
    const next = removeVideoProjectSegment(current, segmentId);
    if (next === current) {
      setError(projectIsActive(current) ? "项目运行中，不能删除分段。" : "长视频项目至少需要保留一个分段。");
      return;
    }
    const nextIndex = Math.max(0, Math.min(removedIndex, next.segments.length - 1));
    setRunSelection((selection) => normalizeRunSelection(next.segments, selection));
    setSelectedIndex(nextIndex);
    setSequenceTime(videoSequenceTimeForSegment(next.segments, nextIndex));
    edit(() => next);
  };

  const insertBlankSegment = (afterIndex: number) => {
    const current = projectRef.current;
    if (!current || projectIsActive(current) || action) return;
    const id = clientId();
    const insertIndex = Math.max(0, Math.min(current.segments.length, afterIndex + 1));
    const profile = videoProfiles.find((item) => item.compiler === "h3_fl") ?? videoProfiles[0];
    const blank = draftVideoSegment(id, profile);
    const nextSegments = [
      ...current.segments.slice(0, insertIndex),
      blank,
      ...current.segments.slice(insertIndex),
    ];
    setSelectedIndex(insertIndex);
    setSequenceTime(videoSequenceTimeForSegment(nextSegments, insertIndex));
    setRunSelection((selection) => new Set([...selection, id]));
    edit((value) => ({ ...value, status: "draft", merged: undefined, segments: nextSegments }));
  };

  const openVideoImport = (afterIndex: number) => {
    if (!projectRef.current || projectIsActive(projectRef.current) || action) return;
    importAfterIndexRef.current = Math.max(-1, afterIndex);
    setImportOpen(true);
    setError("");
  };

  const insertVideoAsset = useCallback((asset: LibraryAsset) => {
    const current = projectRef.current;
    if (!current || asset.kind !== "video") return;
    const insertIndex = Math.max(0, Math.min(current.segments.length, importAfterIndexRef.current + 1));
    const clip = draftVideoMediaSegment(clientId(), asset);
    if (!videoSegmentDuration(clip)) {
      setError("该视频缺少可用的时长或帧率信息，暂时无法加入时间线。");
      return;
    }
    const nextSegments = [...current.segments.slice(0, insertIndex), clip, ...current.segments.slice(insertIndex)];
    setSelectedIndex(insertIndex);
    setSequenceTime(videoSequenceTimeForSegment(nextSegments, insertIndex));
    setRunSelection((selection) => normalizeRunSelection(nextSegments, selection));
    edit((value) => ({ ...value, status: "draft", merged: undefined, segments: nextSegments }));
    // Keep a stable insertion cursor so a multi-file drop preserves file order
    // instead of repeatedly inserting every upload at the same stale index.
    importAfterIndexRef.current = insertIndex;
    setImportOpen(false);
  }, [edit]);

  const importResult = useCallback(async (result: StudioJob) => {
    if (!result.id || importBusy) return;
    // Studio materializes the durable job_id into a LibraryAsset before the
    // timeline stores it, so restored projects never depend on a preview URL.
    setImportBusy(true); setError("");
    try {
      const asset = await onImportResult(result.id);
      await onAssetCreated(asset);
      insertVideoAsset(asset);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "结果导入失败"); }
    finally { setImportBusy(false); }
  }, [importBusy, insertVideoAsset, onAssetCreated, onImportResult]);

  const uploadVideos = useCallback(async (files: File[]) => {
    if (importBusy) return;
    setImportBusy(true); setError("");
    try {
      for (const file of files) {
        const asset = await onUploadVideo(file);
        await onAssetCreated(asset);
        insertVideoAsset(asset);
      }
    } catch (caught) { setError(caught instanceof Error ? caught.message : "本地视频上传失败"); }
    finally { setImportBusy(false); }
  }, [importBusy, insertVideoAsset, onAssetCreated, onUploadVideo]);

  const active = project ? projectIsActive(project) : false;
  const problems = project ? validateVideoProject(project, videoProfiles, assets) : [];
  const runPlan = project ? selectedTimelineRunPlan(project.segments, runSelection) : [];
  const autoIncludedCount = runPlan.filter((step) => step.autoIncluded).length;
  const mergeBlockReason = dirty ? "项目有未保存修改，保存后系统会重新计算过期分段" : project ? projectMergeBlockReason(project) : undefined;
  const boundSourceId = project?.storyboard?.source_asset_id ?? sourceId;
  const sourceAsset = assets.find((asset) => asset.id === boundSourceId && asset.kind === "video");
  const validSourceId = sourceAsset?.id ?? "";
  const selectedSegment = project?.segments[selectedIndex];
  const inspectorMode = selectedSegment && isVideoMediaSegment(selectedSegment)
    ? "media"
    : selectedSegment?.source_range
    ? "source"
    : selectedSegment?.status === "completed"
      ? "generated"
      : "generation";
  return <aside id="video-timeline-drawer" className="rail-drawer timeline-drawer" aria-label="长视频时间线" onDragOver={(event) => { if (Array.from(event.dataTransfer.types).includes("Files")) event.preventDefault(); }} onDrop={(event) => { const files = Array.from(event.dataTransfer.files).filter((file) => file.type.startsWith("video/")); if (!files.length) return; event.preventDefault(); openVideoImport(selectedIndex); void uploadVideos(files); }}>
    {importOpen && <VideoImportDialog assets={assets} results={results} busy={importBusy} onChooseAsset={insertVideoAsset} onChooseResult={(result) => void importResult(result)} onUpload={(files) => void uploadVideos(files)} onClose={() => setImportOpen(false)}/>}
    <header className="rail-drawer-header timeline-header"><div><strong>长视频</strong><small>多分段顺序生成 · 持久化项目</small></div><button type="button" aria-label="关闭长视频抽屉" onClick={onClose}>×</button></header>
    <div className="timeline-project-bar">
      <label><span>项目</span><select value={project?.id ?? "draft"} onChange={(event) => event.target.value === "draft" ? beginDraft() : void openProject(event.target.value)}><option value="draft">新项目草稿</option>{projects.filter((item) => item.id).map((item) => <option key={item.id} value={item.id}>{item.title} · {timelineStatusLabel(item.status)}</option>)}</select></label>
      <div className="timeline-project-actions"><button type="button" onClick={beginDraft}>＋ 新建</button><button type="button" className="danger" disabled={!project?.id || active || Boolean(action)} onClick={() => void deleteProject()}>{action === "deleting-project" ? "删除中…" : "删除项目"}</button></div>
    </div>
    <div className="timeline-project-shell">
      <div className="timeline-project-meta">
        {loading && <p className="timeline-loading" role="status">正在恢复持久项目…</p>}
        {project && <>
          <section className="timeline-project-summary">
            <label><span>项目名称</span><input value={project.title} disabled={active} onChange={(event) => edit((current) => ({ ...current, title: event.target.value }))}/></label>
            <div><span className={`timeline-status status-${project.status}`}>{timelineStatusLabel(project.status)}</span><small>{project.current_index === undefined || project.current_index < 0 ? `${project.segments.length} 个分段` : `当前分段 ${project.current_index + 1}/${project.segments.length}`}{dirty ? " · 有未保存修改" : " · 已持久化"}</small></div>
          </section>
          {error && <div className="timeline-error" role="alert">{error}</div>}
          {!error && problems.length > 0 && <div className="timeline-warning" role="status">{problems[0]}</div>}
        </>}
      </div>
      {project && <>
      <div className="director-workspace">
        <div className="director-overview-column">
          <SequenceVideoMonitor segments={project.segments} merged={dirty ? undefined : project.merged} currentTime={sequenceTime} onTimeChange={seekSequence} onSelect={selectStoryboardSegment} assets={assets} sourceId={validSourceId} sourceTime={sourceTime} disabled={active || Boolean(action)} analyzing={analyzingScenes} suggestions={sceneSuggestions} onSourceChange={bindSource} onSourceTimeChange={setSourceTime} onAnalyze={() => void analyzeScenes()} onSplit={splitAtCurrentFrame} onEqualize={equalizeDraft}/>
          <StoryboardTimeline segments={project.segments} assets={assets} selectedIndex={selectedIndex} runSelection={runSelection} currentTime={sequenceTime} disabled={active || Boolean(action)} onSelect={selectStoryboardSegment} onTimeChange={seekSequence} onDelete={removeSegment} onImportAfter={openVideoImport} onInsertAfter={insertBlankSegment} onToggleRun={(id) => setRunSelection((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return normalizeRunSelection(project.segments, next); })} onSelectAll={() => setRunSelection(allRunSelection(project.segments))} onInvert={() => setRunSelection((current) => invertRunSelection(project.segments, current))}/>
          {project.merged && <section className="timeline-merged"><div><strong>合并结果</strong><span className={`timeline-status status-${project.merged.status}`}>{timelineStatusLabel(project.merged.status)}{project.merged.status === "merging" && Number.isFinite(project.merged.progress) ? ` · ${project.merged.progress}%` : ""}</span></div>{project.merged.status === "merging" && <progress max="100" value={project.merged.progress ?? 0} aria-label="长视频合并进度"/>}{project.merged.error && <p>{project.merged.error}</p>}{project.merged.preview_url && <LazyVideoPreview src={project.merged.preview_url} thumbnailUrl={project.merged.thumbnail_url} label="合并长视频预览"/>}{project.merged.download_url && <a href={project.merged.download_url} download>↓ 下载合并长视频</a>}</section>}
          {runPlan.length > 0 && <section className="director-selection-warning" aria-label="选中分段执行计划" style={{ display: "grid", gap: 8 }}><strong>执行计划 · 按分段顺序</strong><div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>{runPlan.map((step) => <span key={step.id} style={{ padding: "4px 8px", borderRadius: 8, border: "1px solid #43485a" }}>分段 {step.index + 1}{step.autoIncluded ? " · 自动补齐前驱" : " · 已选"}</span>)}</div><small>{autoIncludedCount ? `将先生成 ${autoIncludedCount} 个未完成前驱，再按顺序生成所选分段。` : "所需前驱已完成，只执行所选分段。"}</small></section>}
        </div>
        <section className="director-segment-inspector" aria-label="当前分镜检查器" data-editor-mode={inspectorMode}>
          <header><div><strong>{inspectorMode === "media" ? "已有素材片段" : inspectorMode === "source" ? "素材参考片段" : inspectorMode === "generated" ? "已生成片段" : "待生成片段"}</strong><small>{inspectorMode === "media" ? "直接进入成片；可调整入点、出点和音频" : inspectorMode === "source" ? "裁剪源区间并配置 H3 参考生成；不会修改原素材" : inspectorMode === "generated" ? "预览、下载或重新生成当前结果" : "填写提示词和参数后生成这一段"}</small></div></header>
          {selectedSegment && <>
            {inspectorMode === "source" && (
              <SourceRangePanel
                segment={selectedSegment}
                source={sourceAsset}
                busy={assetAction === selectedSegment.id}
                disabled={active || Boolean(action)}
                notice={assetNotice}
                onCropAndSave={() => void cropSelectedSourceRange()}
              />
            )}
            {inspectorMode === "media" ? <MediaSegmentInspector segment={selectedSegment} asset={assets.find((asset) => asset.id === selectedSegment.media_source?.asset_id)} disabled={active || Boolean(action)} onChange={(next) => edit((current) => ({ ...current, merged: undefined, segments: current.segments.map((item) => item.id === next.id ? next : item) }))} onRemove={() => removeSegment(selectedSegment.id)}/> : <div className={`director-segment-editor mode-${inspectorMode}`}>
              {inspectorMode === "source" && <div className="director-inspector-note"><strong>这是参考生成片段，不是直接拼接素材</strong><span>上方负责源视频裁剪；下方 Prompt 和模型参数决定 H3 生成结果。</span></div>}
              <SegmentCard key={selectedSegment.id} segment={selectedSegment} index={selectedIndex} total={project.segments.length} previous={project.segments[selectedIndex - 1]} continuationReady={continuationSourceReady(project.segments, selectedIndex)} assets={assets} profiles={videoProfiles} disabled={active || Boolean(action)} structureLocked={Boolean(project.storyboard)} onChange={(next) => edit((current) => ({ ...current, segments: current.segments.map((item) => item.id === next.id ? next : item) }))} onMove={(offset) => { const reordered = moveVideoSegment(project.segments, selectedSegment.id, offset); edit((current) => ({ ...current, segments: reordered })); const nextIndex = Math.max(0, Math.min(reordered.length - 1, selectedIndex + offset)); setSelectedIndex(nextIndex); setSequenceTime(videoSequenceTimeForSegment(reordered, nextIndex)); }} onRemove={() => removeSegment(selectedSegment.id)} onRun={() => void perform("rerun", async (saved) => API.runSegment(saved.id!, saved.segments[selectedIndex]?.id ?? selectedSegment.id), false)}/>
            </div>}
          </>}
        </section>
      </div>
      <footer className="timeline-actions"><span className="director-run-summary">已选 {runSelection.size}/{project.segments.length} 段{autoIncludedCount ? ` · 自动补齐 ${autoIncludedCount} 段` : ""}</span><button type="button" disabled={!dirty || active || Boolean(action)} onClick={() => void save()}>{action === "saving" ? "保存中…" : "保存项目"}</button>{active ? <button className="danger" type="button" disabled={Boolean(action) || !project.id} onClick={() => void perform("stop", (saved) => API.stop(saved.id!), false)}>停止队列</button> : <><button type="button" disabled={Boolean(action) || problems.length > 0} onClick={() => void perform("run", (saved) => API.run(saved.id!))}>顺序生成全部</button><button className="primary" type="button" disabled={Boolean(action) || problems.length > 0 || runPlan.length === 0} onClick={() => void perform("run-selected", (saved) => API.runSelected(saved.id!, runPlan.map((step) => step.id)))}>按计划生成 {runPlan.length} 段</button></>}<button type="button" title={mergeBlockReason} disabled={Boolean(action) || !project.id || dirty || !projectCanMerge(project)} onClick={() => void perform("merge", (saved) => API.merge(saved.id!))}>合并长视频</button>{mergeBlockReason && <small className="director-run-summary">合并门禁：{mergeBlockReason}</small>}</footer>
      </>}
    </div>
  </aside>;
}

function MediaSegmentInspector({ segment, asset, disabled, onChange, onRemove }: { segment: VideoSegment; asset?: LibraryAsset; disabled: boolean; onChange: (segment: VideoSegment) => void; onRemove: () => void }) {
  const source = segment.media_source!;
  const fps = Math.max(1, Number(source.fps) || 24);
  const detectedFrames = Math.max(source.end_frame, Number(asset?.media.frame_count ?? Math.round(Number(asset?.media.duration ?? 0) * fps)) || source.end_frame);
  const setRange = (start: number, end: number) => onChange({ ...segment, media_source: { ...source, start_frame: Math.max(0, Math.min(Math.round(start), Math.round(end) - 1)), end_frame: Math.max(Math.round(start) + 1, Math.min(detectedFrames, Math.round(end))) } });
  return <article className="timeline-media-inspector">
    <div className="timeline-media-preview timeline-media-poster" aria-label={`${asset?.filename ?? "已有视频"} 轻量预览`}>{asset?.thumbnailUrl ? <img src={asset.thumbnailUrl} alt={`${asset.filename} 缩略图`} loading="lazy" decoding="async"/> : <div><span>▶</span><strong>缩略图暂不可用</strong></div>}<small>播放、暂停和逐帧定位请使用左侧成片序列监视器</small></div>
    <header><div><strong>{asset?.filename ?? source.asset_id ?? "已有视频"}</strong><small>直接拼接 · 不提交 H3 · {(videoSegmentDuration(segment)).toFixed(2)} 秒</small></div><button type="button" disabled={disabled} onClick={onRemove}>删除片段</button></header>
    <div className="timeline-media-range"><label><span>入点（帧）</span><input type="number" min="0" max={Math.max(0, source.end_frame - 1)} value={source.start_frame} disabled={disabled} onChange={(event) => setRange(Number(event.target.value), source.end_frame)}/><small>{(source.start_frame / fps).toFixed(2)}s</small></label><label><span>出点（不含）</span><input type="number" min={source.start_frame + 1} max={detectedFrames} value={source.end_frame} disabled={disabled} onChange={(event) => setRange(source.start_frame, Number(event.target.value))}/><small>{(source.end_frame / fps).toFixed(2)}s</small></label><label className="timeline-media-audio"><input type="checkbox" checked={source.keep_audio} disabled={disabled || asset?.media.has_audio === false} onChange={(event) => onChange({ ...segment, media_source: { ...source, keep_audio: event.target.checked } })}/><span>保留原视频音频</span></label></div>
    <p>调整只影响合并时使用的区间，不修改资产库中的原视频。</p>
  </article>;
}

function SegmentCard({ segment, index, total, previous, continuationReady, assets, profiles, disabled, structureLocked, onChange, onMove, onRemove, onRun }: { segment: VideoSegment; index: number; total: number; previous?: VideoSegment; continuationReady: boolean; assets: LibraryAsset[]; profiles: TimelineProfile[]; disabled: boolean; structureLocked: boolean; onChange: (segment: VideoSegment) => void; onMove: (offset: -1 | 1) => void; onRemove: () => void; onRun: () => void }) {
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number }>();
  const contextDeleteRef = useRef<HTMLButtonElement>(null);
  const request = segment.request;
  const profile = findTimelineProfile(profiles, request.profile_id, request.profile_version);
  const stepBounds = profileBounds(profile, "steps", [1, 100]);
  const loraBounds = profileBounds(profile, "lora_strength", profile?.sampling_mode === "base" ? [0, 0] : [0, 2]);
  const denoiseBounds = profileBounds(profile, "denoise", [0.05, 1]);
  const durationOptions = profileDurationOptions(profile);
  const requiredCompiler = timelineRequiredCompiler(segment);
  const compatibleProfiles = profiles.filter((item) => item.available && item.compiler === requiredCompiler);
  const sampling = profile?.sampling_mode === "base" ? "base" : "turbo4";
  const turbo = profile?.sampling_mode === "turbo4";
  const referenceBudget = request.references.length + (["tail_frame", "previous_video"].includes(segment.continuation) ? 1 : 0) + (segment.source_range ? 1 : 0);
  const compatibleAssets = segment.continuation === "tail_frame" ? assets.filter((asset) => asset.kind === "image") : assets;
  const availableAssets = segment.continuation === "tail_frame" && request.references.length > 0 ? [] : compatibleAssets;
  const promptMentionItems = compatibleAssets.map((asset) => mentionItem(asset, request.references.some((reference) => (reference.asset_id ?? reference.id) === asset.id)));
  const assetKinds = new Map(assets.map((asset) => [asset.id, asset.kind] as const));
  const referenceTags = h3ReferenceTagMap(request.references, assetKinds, segment.continuation);
  const finalPromptPreview = timelinePromptPreview(request.prompt, request.references, assetKinds, segment.continuation, Boolean(segment.source_range));
  const previousVideoTag = `<Video ${request.references.filter((reference) => assetKinds.get(reference.asset_id ?? reference.id ?? "") === "video").length + (segment.source_range ? 2 : 1)}>`;
  const resolvedWorkflow = latestResolvedWorkflow(segment);
  const resolvedSummary = resolvedWorkflowSummary(resolvedWorkflow);
  const continuationEvidence = [...segment.attempts].reverse().find((attempt) => attempt.continuation?.asset_id)?.continuation;
  const continuationThumbnail = continuationEvidence?.asset_id ? `/api/assets/${encodeURIComponent(continuationEvidence.asset_id)}/thumbnail` : undefined;
  const setRequest = (patch: Partial<VideoSegment["request"]>) => onChange({ ...segment, request: { ...request, ...patch } });
  const setParameters = (patch: Partial<VideoSegment["request"]["parameters"]>) => setRequest({ parameters: { ...request.parameters, ...patch } });
  const selectSampling = (nextSampling: "turbo4" | "base") => onChange(retargetSegmentSampling(segment, profiles, nextSampling));
  const setContinuation = (continuation: ContinuationMode) => {
    if (continuation === "tail_frame" && segment.source_range) return;
    const compiler = continuation === "tail_frame" ? "h3_fl" : (segment.source_range || continuation === "previous_video" || request.references.length > 0) ? "h3_ref" : "h3_fl";
    const continuationRange = continuation === "previous_video" && previous
      ? defaultVideoContinuationRange(previous.request.parameters.duration)
      : undefined;
    const next = {
      ...segment,
      continuation,
      continuation_range: continuationRange,
      motion_context: continuation === "motion_context"
        ? segment.motion_context ?? { video_frames: 22 as const, audio_frames: 24 }
        : undefined,
      request: {
        ...request,
        parameters: {
          ...request.parameters,
          ...(["tail_frame", "motion_context"].includes(continuation) && previous ? { aspect_ratio: previous.request.parameters.aspect_ratio } : {}),
        },
      },
    };
    onChange(retargetSegmentCompiler(next, profiles, compiler));
  };
  const addReference = (assetId: string): boolean => {
    if (request.references.some((item) => (item.asset_id ?? item.id) === assetId)) return true;
    const asset = availableAssets.find((item) => item.id === assetId);
    if (!asset || referenceBudget >= H3_MAX_REFERENCE_FILES) return false;
    const references = appendTimelineReference(request.references, asset, segment.continuation);
    if (references === request.references) return false;
    onChange(retargetSegmentForReferences({ ...segment, request: { ...request, references } }, profiles));
    return true;
  };
  useEffect(() => {
    if (!contextMenu) return;
    contextDeleteRef.current?.focus();
    const close = () => setContextMenu(undefined);
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", closeOnEscape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", closeOnEscape); };
  }, [contextMenu]);
  const deleteBlockedReason = disabled ? "项目运行或操作期间不能删除分段" : total <= 1 ? "长视频项目至少需要保留一个分段" : undefined;
  return <article className={`timeline-segment status-${segment.status}`} onContextMenu={(event) => { event.preventDefault(); setContextMenu({ x: event.clientX, y: event.clientY }); }}>
    {contextMenu && <div role="menu" aria-label={`分段 ${index + 1} 菜单`} style={{ position: "fixed", zIndex: 1000, left: Math.min(contextMenu.x, window.innerWidth - 190), top: Math.min(contextMenu.y, window.innerHeight - 64), minWidth: 180, padding: 6, border: "1px solid #42495a", borderRadius: 8, background: "#171b24", boxShadow: "0 12px 32px #000a" }} onPointerDown={(event) => event.stopPropagation()}><button ref={contextDeleteRef} role="menuitem" type="button" disabled={Boolean(deleteBlockedReason)} title={deleteBlockedReason} onClick={() => { setContextMenu(undefined); onRemove(); }} style={{ width: "100%", minHeight: 34, border: 0, borderRadius: 6, background: "#321c23", color: "#f1a1ad", textAlign: "left", padding: "0 10px" }}>删除分段</button>{deleteBlockedReason && <small style={{ display: "block", maxWidth: 220, padding: "5px 8px 2px", color: "#8d96a7" }}>{deleteBlockedReason}</small>}</div>}
    <header><div><span>{String(index + 1).padStart(2, "0")}</span><div><strong>分段 {index + 1}</strong><small>{timelineStatusLabel(segment.status)} · 尝试 {segment.attempts.length} 次 · 右键可管理</small></div></div><nav aria-label={`调整分段 ${index + 1}`}><button type="button" disabled={disabled || structureLocked || index === 0} onClick={() => onMove(-1)} aria-label="上移分段">↑</button><button type="button" disabled={disabled || structureLocked || index === total - 1} onClick={() => onMove(1)} aria-label="下移分段">↓</button><button type="button" disabled={disabled || total === 1} onClick={onRemove} aria-label="删除分段">×</button></nav></header>
    <div className="timeline-field"><span>提示词 <small>输入 @ 选择素材</small></span><PromptMentionComposer value={request.prompt} onChange={(prompt) => setRequest({ prompt })} items={promptMentionItems} onSelectItem={(item) => addReference(item.id)} placeholder="详细描述这一段的主体、动作、场景、镜头和声音；输入 @ 引用素材…" ariaLabel={`分段 ${index + 1} 提示词`} disabled={disabled}/></div>
    <div className="prompt-readonly-mode timeline-readonly-mode" role="note"><strong>只读提交模式</strong><span>每个长视频分段只把 @素材ID 解析为 H3 类型标签，不翻译、不扩写、不重组；历史结构化字段不参与提交。</span></div>
    <details className="prompt-preview timeline-prompt-preview" open><summary>H3 最终提示词预览（只读）</summary><pre aria-label={`分段 ${index + 1} H3 最终提示词只读预览`}>{finalPromptPreview || "请填写本段提示词。"}</pre><small>预览与服务端只读标签映射规则一致；续接素材的隐式标签已包含在末尾。</small></details>
    <details className="prompt-helper timeline-prompt-helper"><summary>查看 H3 Ref2VA 参考模板（只读）</summary><pre aria-label="长视频 H3 Ref2VA 参考模板">{H3_REFERENCE_PROMPT_TEMPLATE}</pre><p>模板只供参考，不会自动填入或改写本段提示词。</p></details>
    <div className="timeline-controls"><label><span>工作流模式</span><strong>{timelineWorkflowModeLabel(segment)}</strong><small>由续接和参考素材解析 · {requiredCompiler}</small></label><label><span>采样档</span><select value={sampling} disabled={disabled} onChange={(event) => selectSampling(event.target.value as "turbo4" | "base")}><option value="turbo4" disabled={!compatibleProfiles.some((item) => item.sampling_mode === "turbo4")}>Turbo4（4 步推荐）</option><option value="base" disabled={!compatibleProfiles.some((item) => item.sampling_mode === "base")}>Base20（基础质量）</option></select><small>已解析：{profile ? `${profile.id}@${profile.version}` : "无可用 Profile"}</small></label><label><span>续接方式</span><select value={segment.continuation} disabled={disabled || index === 0} onChange={(event) => setContinuation(event.target.value as ContinuationMode)}>{continuationChoices(index).filter((choice) => !(segment.source_range && choice === "tail_frame")).map((choice) => <option key={choice} value={choice}>{choice === "none" ? "不续接" : choice === "tail_frame" ? "上一段尾帧" : choice === "previous_video" ? "上一段视频" : "Motion Context 潜空间续接"}</option>)}</select></label><label><span>画面比例</span><select value={request.parameters.aspect_ratio} disabled={disabled || ["tail_frame", "motion_context"].includes(segment.continuation)} onChange={(event) => setParameters({ aspect_ratio: event.target.value as "16:9" | "9:16" })}><option value="16:9">16:9</option><option value="9:16">9:16</option></select></label><label><span>有效时长 <b>{h3EffectiveDuration(request.parameters.duration).toFixed(2)}s</b></span><select value={h3EffectiveDuration(request.parameters.duration)} disabled={disabled} onChange={(event) => setParameters({ duration: Number(event.target.value) })}>{durationOptions.map((duration) => <option key={duration} value={duration}>{duration.toFixed(2)} 秒 · {Math.round(duration * 24)} 帧</option>)}</select><small>与当前 Profile 一致 · H3 17k+5 帧网格。</small></label><label><span>{turbo ? "Turbo LoRA Steps（4 推荐）" : "Base Steps"}</span><input type="number" min={stepBounds[0]} max={stepBounds[1]} value={request.parameters.steps} disabled={disabled} onChange={(event) => setParameters({ steps: Number(event.target.value) })}/></label><label><span>模型强度（LoRA）</span><input type="number" min={loraBounds[0]} max={loraBounds[1]} step="0.05" value={request.parameters.lora_strength} disabled={disabled || profile?.sampling_mode === "base"} onChange={(event) => setParameters({ lora_strength: Number(event.target.value) })}/></label><label className="timeline-denoise"><span>调度去噪比例（实验） <b>{Number(request.parameters.denoise ?? 1).toFixed(2)}</b></span><input type="range" min={denoiseBounds[0]} max={denoiseBounds[1]} step="0.05" value={request.parameters.denoise ?? 1} disabled={disabled} onChange={(event) => setParameters({ denoise: Number(event.target.value) })}/><small>对应 H3 BasicScheduler.denoise，不是 CFG 或参考权重；模板默认 1.00，非默认值属实验。</small></label><label><span>Seed</span><input type="number" value={request.parameters.seed} disabled={disabled} onChange={(event) => setParameters({ seed: Number(event.target.value) })}/></label></div>
    {segment.continuation === "tail_frame" && <div className="continuation-note" style={{ display: "grid", gridTemplateColumns: "64px 1fr", gap: 10, alignItems: "center" }}>{continuationThumbnail ? <img src={continuationThumbnail} alt={`分段 ${index} 真实尾帧`} loading="lazy" decoding="async" style={{ width: 64, height: 48, objectFit: "cover", borderRadius: 7 }}/> : <span aria-hidden="true" style={{ display: "grid", placeItems: "center", width: 64, height: 48, borderRadius: 7, background: "#252934" }}>尾帧</span>}<div><strong>隐式首帧：分段 {index} 尾帧 → <code>&lt;Picture 1&gt;</code></strong><p>运行时服务端解码上一段最后一个可用画面；{continuationThumbnail ? "左侧为本次实际提取结果。" : "完成提取后会在此显示真实尾帧。"}可选目标尾帧将映射为 <code>&lt;Picture 2&gt;</code>。系统严格传递上一段尾帧作为下一段首帧输入；模型连续性需预览并按需重跑。系统不改写 Prompt；两段比例必须一致。</p></div></div>}
    {segment.continuation === "previous_video" && previous && (
      <ContinuationRangeEditor previous={previous} index={index} range={segment.continuation_range} disabled={disabled} videoTag={previousVideoTag} onChange={(continuationRange) => onChange({ ...segment, continuation_range: continuationRange })}/>
    )}
    {segment.continuation === "motion_context" && <div className="continuation-note"><strong>Motion Context 潜空间续接</strong><p>直接复用上一段视频与声音 latent，生成后自动裁掉重复头部，再参与最终拼接。相邻分段必须保持相同尺寸。</p><div className="timeline-controls"><label><span>视频上下文帧</span><select value={segment.motion_context?.video_frames ?? 22} disabled={disabled} onChange={(event) => onChange({ ...segment, motion_context: { video_frames: Number(event.target.value) as 5 | 22 | 39 | 56, audio_frames: segment.motion_context?.audio_frames ?? 24 } })}><option value={5}>5</option><option value={22}>22（推荐）</option><option value={39}>39</option><option value={56}>56</option></select></label><label><span>音频上下文帧</span><input type="number" min={0} max={240} value={segment.motion_context?.audio_frames ?? 24} disabled={disabled} onChange={(event) => onChange({ ...segment, motion_context: { video_frames: segment.motion_context?.video_frames ?? 22, audio_frames: Number(event.target.value) } })}/><small>24 帧为 1 秒；0 表示跟随视频窗口。</small></label></div></div>}
    <div className="model-note timeline-sampling-note"><span>i</span><p>{turbo ? <><strong>Turbo LoRA 模式</strong>默认并推荐 4 步，Steps 可在 {stepBounds[0]}..{stepBounds[1]} 内调整；模型强度对应 LoraLoaderModelOnly.strength_model，增加步数不保证画质一定更好。</> : <><strong>Base 质量模式</strong>不加载 Turbo LoRA；Steps 可在当前 Profile 的 {stepBounds[0]}..{stepBounds[1]} 内调整。</>}</p></div>
    {resolvedSummary && <div className="segment-workflow-evidence" aria-label="本段实际工作流参数"><strong>实际执行</strong><span>{resolvedSummary}</span>{resolvedWorkflow?.diffusion_model && <small title={resolvedWorkflow.diffusion_model}>模型：{resolvedWorkflow.diffusion_model}</small>}</div>}
    <section className="timeline-references"><div><strong>参考资产</strong><small>{referenceBudget}/{H3_MAX_REFERENCE_FILES}（含续接）· @ 仅映射为下方 H3 标签，不改写创意语义</small></div><AssetPicker assets={availableAssets.filter((asset) => !request.references.some((reference) => (reference.asset_id ?? reference.id) === asset.id))} disabled={disabled || referenceBudget >= H3_MAX_REFERENCE_FILES} onSelect={(asset) => addReference(asset.id)}/>{request.references.map((reference) => { const id = reference.asset_id ?? reference.id ?? ""; return <ReferenceRow key={id} reference={reference} tags={referenceTags.get(id)} asset={assets.find((asset) => asset.id === id)} continuation={segment.continuation} disabled={disabled} onChange={(next) => setRequest({ references: request.references.map((item) => item === reference ? next : item) })} onRemove={() => onChange(retargetSegmentForReferences({ ...segment, request: { ...request, references: request.references.filter((item) => item !== reference) } }, profiles))}/>; })}</section>
    {segment.error && <p className="segment-error" role="alert">{segment.error}</p>}
    {(segment.preview_url || segment.download_url) && <div className="segment-result">{segment.preview_url && <LazyVideoPreview src={segment.preview_url} thumbnailUrl={segment.thumbnail_url} label={`分段 ${index + 1} 视频预览`}/>}<div>{segment.download_url && <a href={segment.download_url} download>↓ 下载本段</a>}<button type="button" disabled={disabled || !continuationReady} onClick={onRun}>{segment.status === "completed" ? "重新生成本段" : "单独运行本段"}</button></div>{!continuationReady && segment.continuation !== "none" && <small>需要先完成上一段才能单独重跑。</small>}</div>}
    {!segment.preview_url && <button className="segment-run" type="button" disabled={disabled || !continuationReady} onClick={onRun}>单独运行本段</button>}
  </article>;
}

function AssetPicker({ assets, disabled, onSelect }: { assets: LibraryAsset[]; disabled: boolean; onSelect: (asset: LibraryAsset) => boolean }) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const [search, setSearch] = useState("");
  const query = search.trim().toLocaleLowerCase();
  const filtered = query ? assets.filter((asset) => `${assetLabel(asset)} ${displayAsset(asset).folder ?? ""} ${asset.kind}`.toLocaleLowerCase().includes(query)) : assets;
  return <details className="timeline-reference-add" ref={detailsRef}>
    <summary aria-disabled={disabled} onClick={(event) => { if (disabled) event.preventDefault(); }}>从资产库选择…</summary>
    {!disabled && <div style={{ display: "grid", gap: 8, padding: 8, maxHeight: 300, overflowY: "auto", background: "#17191f", border: "1px solid #3a4050", borderRadius: 10 }}>
      <input aria-label="搜索参考资产" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索素材…"/>
      {filtered.map((asset) => <button key={asset.id} type="button" onClick={() => { if (onSelect(asset) && detailsRef.current) detailsRef.current.open = false; }} style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, textAlign: "left" }}>
        <AssetThumb asset={asset}/><span style={{ minWidth: 0, display: "grid" }}><strong title={assetLabel(asset)} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{assetLabel(asset)}</strong><small>{asset.kind === "image" ? "图片" : asset.kind === "video" ? "视频" : "音频"}{displayAsset(asset).folder ? ` · ${displayAsset(asset).folder}` : ""}</small></span>
      </button>)}
      {!filtered.length && <small>没有可用素材</small>}
    </div>}
  </details>;
}

function ReferenceRow({ reference, tags, asset, continuation, disabled, onChange, onRemove }: { reference: TimelineReference; tags?: H3ReferenceTags; asset?: LibraryAsset; continuation: ContinuationMode; disabled: boolean; onChange: (reference: TimelineReference) => void; onRemove: () => void }) {
  const kind = asset?.kind ?? "image";
  const role = continuation === "tail_frame" ? "可选目标尾帧 → Picture 2" : "按媒体类型标签引用";
  return <div className="timeline-reference-row">{asset ? <AssetThumb asset={asset} size={38}/> : <span aria-hidden="true">▧</span>}<span title={asset ? assetLabel(asset) : undefined}>{asset ? assetLabel(asset) : (reference.asset_id ?? reference.id)}<small style={{ display: "block" }}>{kind === "image" ? "图片" : kind === "video" ? "视频" : "音频"} · {role}</small><code>{tags?.primary ?? "标签待解析"}{tags?.pairedAudio ? ` · ${tags.pairedAudio} (视频音轨)` : ""}</code></span>{kind === "video" && <label><input type="checkbox" checked={Boolean(reference.include_audio)} disabled={disabled || asset?.media.has_audio !== true} onChange={(event) => onChange({ ...reference, include_audio: event.target.checked })}/>音轨</label>}{kind === "audio" && reference.role === "voice" && <><input aria-label="说话人" value={reference.voice_speaker ?? "S1"} disabled={disabled} onChange={(event) => onChange({ ...reference, voice_speaker: event.target.value.toUpperCase() })}/><input aria-label="Subject 编号" type="number" min="1" max="9" value={reference.voice_subject ?? 1} disabled={disabled} onChange={(event) => onChange({ ...reference, voice_subject: Number(event.target.value) })}/></>}<button type="button" disabled={disabled} onClick={onRemove} aria-label={`移除 ${asset ? assetLabel(asset) : "参考"}`}>×</button></div>;
}
