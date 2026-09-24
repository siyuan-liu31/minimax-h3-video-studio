export type ContinuationMode = "none" | "tail_frame" | "previous_video" | "motion_context";
export const H3_MAX_REFERENCE_FILES = 12;
export type TimelineStatus = "draft" | "pending" | "submitting" | "queued" | "running" | "partial" | "stopping" | "stopped" | "canceled" | "stale" | "completed" | "failed" | "merging";

export type TimelineParts = Record<string, string>;
export type TimelineParameters = {
  aspect_ratio: "16:9" | "9:16";
  duration: number;
  steps: number;
  lora_strength: number;
  denoise: number;
  seed: number;
};
export type TimelineReference = {
  id?: string;
  asset_id?: string;
  role: string;
  include_audio?: boolean;
  voice_speaker?: string;
  voice_subject?: number;
};
export type TimelineReferenceKind = "image" | "video" | "audio";
export type TimelineReferenceAsset = { id: string; kind: TimelineReferenceKind; media: { duration?: number; has_audio?: boolean } };
export type TimelinePromptMode = "preserve_tags_only";
export type VideoStoryboard = {
  source_asset_id: string;
  fps: number;
  frame_count: number;
  cut_frames: number[];
};
export type VideoSourceRange = {
  asset_id: string;
  start_frame: number;
  end_frame: number;
  fps: number;
};
export type VideoContinuationRange = {
  start_frame: number;
  end_frame: number;
  fps: number;
};
export type VideoMotionContext = {
  video_frames: 5 | 22 | 39 | 56;
  audio_frames: number;
};
export type VideoMediaSource = {
  type: "asset" | "job";
  asset_id?: string;
  job_id?: string;
  index?: number;
  start_frame: number;
  end_frame: number;
  fps: number;
  keep_audio: boolean;
};
export type VideoSegmentRequest = {
  prompt: string;
  prompt_mode: TimelinePromptMode;
  parts?: TimelineParts;
  parameters: TimelineParameters;
  profile_id: string;
  profile_version: string;
  profile_digest: string;
  references: TimelineReference[];
};
export type VideoSegment = {
  id: string;
  /** Missing on legacy projects, where every segment is a generation clip. */
  kind?: "generation" | "media";
  index?: number;
  continuation: ContinuationMode;
  status: TimelineStatus;
  request: VideoSegmentRequest;
  /** A direct timeline clip. It is trimmed/normalized only during merge and never submitted to H3. */
  media_source?: VideoMediaSource;
  source_range?: VideoSourceRange;
  continuation_range?: VideoContinuationRange;
  motion_context?: VideoMotionContext;
  job_id?: string;
  attempts: VideoAttempt[];
  preview_url?: string;
  thumbnail_url?: string;
  download_url?: string;
  error?: string;
};
export type WorkflowEvidence = {
  steps?: number;
  sampler?: string;
  scheduler?: string;
  denoise?: number;
  lora?: string | null;
  lora_strength?: number;
  diffusion_model?: string;
};
export type VideoAttempt = {
  id?: string;
  status?: TimelineStatus;
  job_id?: string;
  workflow_evidence?: WorkflowEvidence;
  continuation?: {
    mode?: ContinuationMode;
    asset_id?: string;
    asset_kind?: TimelineReferenceKind;
    source_segment_id?: string;
    trimmed_for_reference?: boolean;
  };
};
export type MergedVideoResult = {
  status: TimelineStatus;
  progress?: number;
  result_job_id?: string;
  preview_url?: string;
  thumbnail_url?: string;
  download_url?: string;
  sha256?: string;
  size?: number;
  media?: Record<string, unknown>;
  error?: string;
};
export type VideoProject = {
  recipe?: Record<string, unknown>;
  id?: string;
  title: string;
  status: TimelineStatus;
  current_index?: number;
  selected_segment_ids?: string[];
  storyboard?: VideoStoryboard;
  stop_requested?: boolean;
  created_at?: number;
  updated_at?: number;
  segments: VideoSegment[];
  merged?: MergedVideoResult;
  error?: string;
};
export type TimelineProfile = {
  id: string;
  version: string;
  display_name: string;
  output_type: "video" | "image";
  compiler: string;
  manifest_sha256: string;
  compatible_identities?: Array<{ version: string; manifest_sha256: string }>;
  sampling_mode?: "turbo4" | "base" | "default";
  available: boolean;
  defaults: Record<string, string | number>;
  limits: Record<string, number | [number, number]>;
};

export type NumericBounds = readonly [number, number];
export type TimelineSamplingPreset = "turbo4" | "base";
export type TimelineRunPlanStep = {
  id: string;
  index: number;
  status: TimelineStatus;
  autoIncluded: boolean;
  reason: "selected" | "unfinished_predecessor";
};

export type SerializedVideoProject = {
  title: string;
  recipe?: Record<string, unknown>;
  storyboard?: VideoStoryboard;
  segments: Array<{
    id?: string;
    kind?: "generation";
    continuation: ContinuationMode;
    source_range?: VideoSourceRange;
    continuation_range?: VideoContinuationRange;
    motion_context?: VideoMotionContext;
    request: VideoSegmentRequest;
  } | {
    id?: string;
    kind: "media";
    media_source: VideoMediaSource;
  }>;
};

const ACTIVE_STATUSES = new Set(["submitting", "queued", "running", "stopping", "merging"]);
const CONTINUATIONS = new Set<ContinuationMode>(["none", "tail_frame", "previous_video", "motion_context"]);
const ASSET_ID = /^[0-9a-f]{32}$/;

export const H3_GENERATION_FPS = 24;
export const H3_MAX_GENERATION_FRAMES = 362;
export const H3_MAX_GENERATION_DURATION = H3_MAX_GENERATION_FRAMES / H3_GENERATION_FPS;
export const H3_MAX_CONTINUATION_FRAMES = 360;
export const H3_DURATION_OPTIONS = Array.from(
  { length: Math.floor((H3_MAX_GENERATION_FRAMES - 124) / 17) + 1 },
  (_, index) => (124 + index * 17) / H3_GENERATION_FPS,
);

/** A select value must pin both fields; profile ids are not version identities. */
export function timelineProfileKey(profile: Pick<TimelineProfile, "id" | "version">): string {
  return `${profile.id}@${profile.version}`;
}

/** Resolve exact versioned values while accepting old saved id-only selections. */
export function findTimelineProfile(profiles: TimelineProfile[], selection: string, version?: string): TimelineProfile | undefined {
  if (version) return profiles.find((profile) => (
    profile.id === selection
    && (profile.version === version || profile.compatible_identities?.some((identity) => identity.version === version))
  ));
  const exact = profiles.find((profile) => timelineProfileKey(profile) === selection);
  return exact ?? profiles.find((profile) => profile.id === selection);
}

export function h3EffectiveDuration(seconds: number): number {
  if (!Number.isFinite(seconds)) return H3_DURATION_OPTIONS[0];
  return H3_DURATION_OPTIONS.reduce((nearest, option) => (
    Math.abs(option - seconds) < Math.abs(nearest - seconds) ? option : nearest
  ), H3_DURATION_OPTIONS[0]);
}

export function videoContinuationFrameCount(previousDuration: number): number {
  const duration = Number.isFinite(previousDuration) ? Math.max(0, previousDuration) : 0;
  return Math.max(1, Math.round(duration * H3_GENERATION_FPS));
}

export function defaultVideoContinuationRange(previousDuration: number): VideoContinuationRange {
  return {
    start_frame: 0,
    end_frame: Math.min(videoContinuationFrameCount(previousDuration), H3_MAX_CONTINUATION_FRAMES),
    fps: H3_GENERATION_FPS,
  };
}

export function normalizeVideoContinuationRange(range: VideoContinuationRange | undefined, previousDuration: number): VideoContinuationRange {
  const totalFrames = videoContinuationFrameCount(previousDuration);
  const fallback = defaultVideoContinuationRange(previousDuration);
  if (!range || !Number.isFinite(range.start_frame) || !Number.isFinite(range.end_frame)) return fallback;
  const startFrame = Math.max(0, Math.min(totalFrames - 1, Math.round(range.start_frame)));
  const endFrame = Math.max(startFrame + 1, Math.min(totalFrames, startFrame + H3_MAX_CONTINUATION_FRAMES, Math.round(range.end_frame)));
  return { start_frame: startFrame, end_frame: endFrame, fps: H3_GENERATION_FPS };
}

export type VideoSequencePosition = {
  index: number;
  segment?: VideoSegment;
  time: number;
  localTime: number;
  segmentStart: number;
  segmentEnd: number;
  totalDuration: number;
};

/** Duration of the generated storyboard sequence, independent of source cuts. */
export function videoSequenceDuration(segments: VideoSegment[]): number {
  return segments.reduce((total, segment) => total + videoSegmentDuration(segment), 0);
}

export function isVideoMediaSegment(segment: Pick<VideoSegment, "kind" | "media_source">): boolean {
  return segment.kind === "media" && Boolean(segment.media_source);
}

/** Timeline duration, without applying H3's 17k+5 grid to direct media. */
export function videoSegmentDuration(segment: VideoSegment): number {
  if (isVideoMediaSegment(segment)) {
    const source = segment.media_source!;
    const frames = Number(source.end_frame) - Number(source.start_frame);
    const fps = Number(source.fps);
    return Number.isFinite(frames) && frames > 0 && Number.isFinite(fps) && fps > 0 ? frames / fps : 0;
  }
  return h3EffectiveDuration(segment.request.parameters.duration);
}

/** Resolve one project-level playhead to the segment and local media time it addresses. */
export function resolveVideoSequencePosition(segments: VideoSegment[], time: number): VideoSequencePosition {
  const totalDuration = videoSequenceDuration(segments);
  const normalized = Number.isNaN(time) ? 0 : time;
  const clamped = Math.max(0, Math.min(totalDuration, normalized));
  if (!segments.length) return { index: -1, time: 0, localTime: 0, segmentStart: 0, segmentEnd: 0, totalDuration: 0 };
  let segmentStart = 0;
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    const segmentEnd = segmentStart + videoSegmentDuration(segment);
    if (clamped < segmentEnd || index === segments.length - 1) {
      return {
        index,
        segment,
        time: clamped,
        localTime: Math.max(0, Math.min(segmentEnd - segmentStart, clamped - segmentStart)),
        segmentStart,
        segmentEnd,
        totalDuration,
      };
    }
    segmentStart = segmentEnd;
  }
  return { index: -1, time: clamped, localTime: 0, segmentStart: totalDuration, segmentEnd: totalDuration, totalDuration };
}

export function videoSequenceTimeForSegment(segments: VideoSegment[], index: number): number {
  return videoSequenceDuration(segments.slice(0, Math.max(0, Math.min(index, segments.length))));
}

export function isH3DurationOption(seconds: number): boolean {
  return H3_DURATION_OPTIONS.some((option) => Math.abs(option - seconds) < 1e-9);
}

export function timelineAssetMentionToken(assetId: string): string {
  return `@{${assetId}}`;
}

export function appendTimelineReference(
  references: TimelineReference[],
  asset: { id: string; kind: TimelineReferenceKind },
  continuation: ContinuationMode,
): TimelineReference[] {
  if (references.some((reference) => (reference.asset_id ?? reference.id) === asset.id)) return references;
  const maximum = continuation === "none" ? H3_MAX_REFERENCE_FILES : H3_MAX_REFERENCE_FILES - 1;
  if (references.length >= maximum || (continuation === "tail_frame" && (asset.kind !== "image" || references.length > 0))) return references;
  // In preserve_tags_only mode the media kind controls only the H3 tag
  // family.  Do not silently turn a video into motion transfer or audio into
  // music direction when the user merely @-mentions an asset.
  const role = continuation === "tail_frame" ? "last_frame" : "reference";
  return [...references, { asset_id: asset.id, role, ...(asset.kind === "video" ? { include_audio: false } : {}) }];
}

export function profileBounds(profile: TimelineProfile | undefined, key: string, fallback: NumericBounds): [number, number] {
  const value = profile?.limits[key];
  if (Array.isArray(value) && value.length === 2) {
    const minimum = Number(value[0]);
    const maximum = Number(value[1]);
    if (Number.isFinite(minimum) && Number.isFinite(maximum) && minimum <= maximum) return [minimum, maximum];
  }
  if (typeof value === "number" && Number.isFinite(value)) return [value, value];
  return [fallback[0], fallback[1]];
}

export function clampToProfile(value: number, profile: TimelineProfile | undefined, key: string, fallback: NumericBounds): number {
  const [minimum, maximum] = profileBounds(profile, key, fallback);
  return Math.max(minimum, Math.min(maximum, value));
}

export function profileDurationOptions(profile?: TimelineProfile): number[] {
  const [minimum, maximum] = profileBounds(
    profile,
    "duration",
    [H3_DURATION_OPTIONS[0], H3_DURATION_OPTIONS.at(-1) ?? H3_DURATION_OPTIONS[0]],
  );
  return H3_DURATION_OPTIONS.filter((duration) => duration >= minimum - 1e-9 && duration <= maximum + 1e-9);
}

export function clampDurationToProfile(value: number, profile?: TimelineProfile): number {
  const options = profileDurationOptions(profile);
  if (!options.length) return h3EffectiveDuration(value);
  return options.reduce((nearest, option) => (
    Math.abs(option - value) < Math.abs(nearest - value) ? option : nearest
  ), options[0]);
}

/** Retarget a segment without replacing user-tuned sampling values with profile defaults. */
export function retargetSegmentCompiler(
  segment: VideoSegment,
  profiles: TimelineProfile[],
  compiler: string,
): VideoSegment {
  if (isVideoMediaSegment(segment)) return segment;
  const current = findTimelineProfile(profiles, segment.request.profile_id, segment.request.profile_version);
  if (current?.compiler === compiler) return segment;
  const candidates = profiles.filter((profile) => profile.available && profile.output_type === "video" && profile.compiler === compiler);
  const selected = candidates.find((profile) => profile.sampling_mode === current?.sampling_mode) ?? candidates[0];
  if (!selected) return segment;
  const parameters = segment.request.parameters;
  return {
    ...segment,
    request: {
      ...segment.request,
      profile_id: selected.id,
      profile_version: selected.version,
      profile_digest: selected.manifest_sha256,
      parameters: {
        ...parameters,
        duration: clampDurationToProfile(parameters.duration, selected),
        steps: clampToProfile(parameters.steps, selected, "steps", [1, 100]),
        lora_strength: clampToProfile(parameters.lora_strength, selected, "lora_strength", selected.sampling_mode === "base" ? [0, 0] : [0, 2]),
        denoise: clampToProfile(parameters.denoise, selected, "denoise", [0.05, 1]),
      },
    },
  };
}

/** Keep the pinned profile compiler aligned with the actual bound inputs. */
export function retargetSegmentForReferences(segment: VideoSegment, profiles: TimelineProfile[]): VideoSegment {
  if (isVideoMediaSegment(segment)) return segment;
  if (segment.continuation === "tail_frame") return retargetSegmentCompiler(segment, profiles, "h3_fl");
  if (segment.source_range || segment.continuation === "previous_video" || segment.request.references.length > 0) return retargetSegmentCompiler(segment, profiles, "h3_ref");
  return retargetSegmentCompiler(segment, profiles, "h3_fl");
}

/** The compiler is a trusted consequence of the segment's bound inputs. */
export function timelineRequiredCompiler(segment: VideoSegment): "h3_fl" | "h3_ref" {
  if (isVideoMediaSegment(segment)) return "h3_fl";
  return segment.continuation === "tail_frame"
    ? "h3_fl"
    : segment.source_range || segment.continuation === "previous_video" || segment.request.references.length > 0
      ? "h3_ref"
      : "h3_fl";
}

export function timelineWorkflowModeLabel(segment: VideoSegment): string {
  if (isVideoMediaSegment(segment)) return "直接素材 · 不生成";
  if (segment.continuation === "tail_frame") return "尾帧约束 · FL2V";
  if (segment.continuation === "previous_video") return "上一段视频参考 · Ref2VA";
  if (segment.continuation === "motion_context") return "潜空间动作与声音续接 · Motion Context";
  if (segment.source_range) return "源视频参考 · Ref2VA";
  if (segment.request.references.length > 0) return "多模态参考 · Ref2VA";
  return "独立生成 · T2V";
}

/** Switch only sampling quality while retaining the compiler required by inputs. */
export function retargetSegmentSampling(
  segment: VideoSegment,
  profiles: TimelineProfile[],
  sampling: TimelineSamplingPreset,
): VideoSegment {
  if (isVideoMediaSegment(segment)) return segment;
  const compiler = timelineRequiredCompiler(segment);
  const target = profiles.find((profile) => (
    profile.available
    && profile.output_type === "video"
    && profile.compiler === compiler
    && profile.sampling_mode === sampling
  ));
  if (!target) return segment;
  const current = findTimelineProfile(profiles, segment.request.profile_id, segment.request.profile_version);
  const changedSampling = current?.sampling_mode !== target.sampling_mode;
  const turbo = target.sampling_mode === "turbo4";
  const parameters = segment.request.parameters;
  return {
    ...segment,
    request: {
      ...segment.request,
      profile_id: target.id,
      profile_version: target.version,
      profile_digest: target.manifest_sha256,
      parameters: {
        ...parameters,
        duration: clampDurationToProfile(parameters.duration, target),
        steps: clampToProfile(
          changedSampling ? Number(target.defaults.steps ?? (turbo ? 4 : 20)) : parameters.steps,
          target, "steps", [1, 100],
        ),
        lora_strength: clampToProfile(
          turbo ? (changedSampling ? Number(target.defaults.lora_strength ?? 0.75) : parameters.lora_strength) : 0,
          target, "lora_strength", turbo ? [0, 2] : [0, 0],
        ),
        denoise: clampToProfile(parameters.denoise, target, "denoise", [0.05, 1]),
      },
    },
  };
}

export type H3ReferenceTags = { primary: string; pairedAudio?: string };

/** Mirror H3's independent Picture/Video/Audio numbering for visible UI evidence. */
export function h3ReferenceTagMap(
  references: TimelineReference[],
  kindByAssetId: ReadonlyMap<string, TimelineReferenceKind>,
  continuation: ContinuationMode = "none",
): Map<string, H3ReferenceTags> {
  const tags = new Map<string, H3ReferenceTags>();
  let picture = continuation === "tail_frame" ? 1 : 0;
  let video = 0;
  let audio = 0;

  // H3 presents enabled video soundtracks before standalone audio inputs.
  for (const reference of references) {
    const id = reference.asset_id ?? reference.id ?? "";
    if (kindByAssetId.get(id) === "video" && reference.include_audio) {
      audio += 1;
      tags.set(id, { primary: "", pairedAudio: `<Audio ${audio}>` });
    }
  }
  for (const reference of references) {
    const id = reference.asset_id ?? reference.id ?? "";
    const kind = kindByAssetId.get(id);
    const previous = tags.get(id);
    if (kind === "image") {
      picture += 1;
      tags.set(id, { ...previous, primary: `<Picture ${picture}>` });
    } else if (kind === "video") {
      video += 1;
      tags.set(id, { ...previous, primary: `<Video ${video}>` });
    } else if (kind === "audio") {
      audio += 1;
      tags.set(id, { primary: `<Audio ${audio}>` });
    }
  }
  return tags;
}

/** Mirror the server's read-only prompt submission for a timeline segment. */
export function timelinePromptPreview(
  prompt: string,
  references: TimelineReference[],
  kindByAssetId: ReadonlyMap<string, TimelineReferenceKind>,
  continuation: ContinuationMode = "none",
  hasSourceRange = false,
): string {
  const tags = h3ReferenceTagMap(references, kindByAssetId, continuation);
  let result = prompt;
  for (const reference of references) {
    const id = reference.asset_id ?? reference.id ?? "";
    const tag = tags.get(id)?.primary;
    if (id && tag) result = result.replaceAll(timelineAssetMentionToken(id), tag);
  }
  let video = references.filter((reference) => kindByAssetId.get(reference.asset_id ?? reference.id ?? "") === "video").length;
  const implicit: string[] = [];
  if (hasSourceRange) implicit.push(`<Video ${++video}>`);
  if (continuation === "tail_frame") implicit.push("<Picture 1>");
  else if (continuation === "previous_video") implicit.push(`<Video ${++video}>`);
  return implicit.length ? `${result}${result ? "; " : ""}${implicit.join("; ")}` : result;
}

export function continuationChoices(index: number): ContinuationMode[] {
  return index <= 0 ? ["none"] : ["none", "tail_frame", "previous_video", "motion_context"];
}

export function draftVideoSegment(id: string, profile?: TimelineProfile): VideoSegment {
  const turbo = profile?.sampling_mode === "turbo4";
  return {
    id,
    continuation: "none",
    status: "draft",
    attempts: [],
    request: {
      prompt: "",
      prompt_mode: "preserve_tags_only",
      parts: {},
      parameters: {
        aspect_ratio: "16:9",
        duration: clampDurationToProfile(Number(profile?.defaults.duration ?? 124 / 24), profile),
        steps: Number(profile?.defaults.steps ?? (turbo ? 4 : 20)),
        lora_strength: Number(profile?.defaults.lora_strength ?? (turbo ? 0.75 : 0)),
        denoise: Number(profile?.defaults.denoise ?? 1),
        seed: -1,
      },
      profile_id: profile?.id ?? "",
      profile_version: profile?.version ?? "",
      profile_digest: profile?.manifest_sha256 ?? "",
      references: [],
    },
  };
}

export function draftVideoMediaSegment(
  id: string,
  asset: {
    id: string;
    media: {
      duration?: number;
      fps?: number;
      source_fps?: number;
      reference_fps?: number;
      frame_count?: number;
      has_audio?: boolean;
    };
  },
): VideoSegment {
  const fps = Number(asset.media.source_fps ?? asset.media.fps ?? asset.media.reference_fps ?? H3_GENERATION_FPS);
  const duration = Number(asset.media.duration ?? 0);
  const recordedFrames = Number(asset.media.frame_count ?? 0);
  const frameCount = Number.isInteger(recordedFrames) && recordedFrames > 0
    ? recordedFrames
    : Number.isFinite(duration) && duration > 0 && Number.isFinite(fps) && fps > 0
      ? Math.max(1, Math.round(duration * fps))
      : 0;
  const fallback = draftVideoSegment(id);
  return {
    ...fallback,
    kind: "media",
    continuation: "none",
    status: "completed",
    media_source: {
      type: "asset",
      asset_id: asset.id,
      start_frame: 0,
      end_frame: frameCount,
      fps: Number.isFinite(fps) && fps > 0 ? fps : H3_GENERATION_FPS,
      keep_audio: asset.media.has_audio === true,
    },
  };
}

export function draftVideoProject(segmentId: string, profile?: TimelineProfile): VideoProject {
  return { title: "未命名长视频", status: "draft", segments: [draftVideoSegment(segmentId, profile)] };
}

export function serializeVideoProject(project: VideoProject): SerializedVideoProject {
  return {
    title: project.title.trim(),
    ...(project.recipe ? { recipe: structuredClone(project.recipe) } : {}),
    ...(project.storyboard ? { storyboard: {
      ...project.storyboard,
      cut_frames: [...project.storyboard.cut_frames],
    } } : {}),
    segments: project.segments.map((segment, index) => isVideoMediaSegment(segment) ? ({
      ...(segment.id ? { id: segment.id } : {}),
      kind: "media" as const,
      media_source: { ...segment.media_source! },
    }) : ({
      ...(segment.id ? { id: segment.id } : {}),
      continuation: segment.continuation,
      ...(segment.source_range ? { source_range: { ...segment.source_range } } : {}),
      ...(segment.continuation === "previous_video" && index > 0 && segment.continuation_range ? {
        continuation_range: normalizeVideoContinuationRange(segment.continuation_range, videoSegmentDuration(project.segments[index - 1])),
      } : {}),
      ...(segment.continuation === "motion_context" ? {
        motion_context: { ...(segment.motion_context ?? { video_frames: 22 as const, audio_frames: 24 }) },
      } : {}),
      request: {
        prompt: segment.request.prompt,
        prompt_mode: "preserve_tags_only",
        parts: {},
        parameters: { ...segment.request.parameters, duration: h3EffectiveDuration(segment.request.parameters.duration) },
        profile_id: segment.request.profile_id,
        profile_version: segment.request.profile_version,
        profile_digest: segment.request.profile_digest,
        references: segment.request.references.map((reference) => ({ ...reference })),
      },
    })),
  };
}

function mergeSegment(local: VideoSegment | undefined, remote: Partial<VideoSegment>, index: number): VideoSegment {
  const remoteMediaSource = remote.media_source && typeof remote.media_source === "object" ? remote.media_source : undefined;
  if (remote.kind === "media" || remoteMediaSource) {
    const fallback = local ?? draftVideoSegment(typeof remote.id === "string" ? remote.id : `segment-${index + 1}`);
    return {
      ...fallback,
      ...remote,
      id: typeof remote.id === "string" && remote.id ? remote.id : fallback.id,
      kind: "media",
      index: typeof remote.index === "number" ? remote.index : fallback.index,
      continuation: "none",
      continuation_range: undefined,
      motion_context: undefined,
      source_range: undefined,
      media_source: remoteMediaSource ?? fallback.media_source,
      status: typeof remote.status === "string" ? remote.status as TimelineStatus : "completed",
      attempts: Array.isArray(remote.attempts) ? remote.attempts : [],
      request: fallback.request,
    };
  }
  const fallback = local ?? draftVideoSegment(typeof remote.id === "string" ? remote.id : `segment-${index + 1}`);
  const remoteRequest = remote.request && typeof remote.request === "object" ? remote.request as Partial<VideoSegmentRequest> : undefined;
  const continuation = CONTINUATIONS.has(remote.continuation as ContinuationMode) ? remote.continuation as ContinuationMode : fallback.continuation;
  const remoteContinuationRange = remote.continuation_range && typeof remote.continuation_range === "object" ? remote.continuation_range : undefined;
  const continuationRange = continuation === "previous_video" ? remoteContinuationRange ?? fallback.continuation_range : undefined;
  const remoteMotionContext = remote.motion_context && typeof remote.motion_context === "object" ? remote.motion_context : undefined;
  return {
    ...fallback,
    ...remote,
    id: typeof remote.id === "string" && remote.id ? remote.id : fallback.id,
    index: typeof remote.index === "number" ? remote.index : fallback.index,
    continuation,
    continuation_range: continuationRange,
    motion_context: continuation === "motion_context"
      ? remoteMotionContext ?? fallback.motion_context ?? { video_frames: 22, audio_frames: 24 }
      : undefined,
    status: typeof remote.status === "string" ? remote.status as TimelineStatus : fallback.status,
    attempts: Array.isArray(remote.attempts) ? remote.attempts : fallback.attempts,
    request: {
      ...fallback.request,
      ...remoteRequest,
      prompt_mode: "preserve_tags_only",
      parts: {},
      parameters: {
        ...fallback.request.parameters,
        ...(remoteRequest?.parameters ?? {}),
        duration: h3EffectiveDuration(Number(remoteRequest?.parameters?.duration ?? fallback.request.parameters.duration)),
      },
      references: Array.isArray(remoteRequest?.references) ? remoteRequest.references : fallback.request.references,
    },
  };
}

export function mergeVideoProject(local: VideoProject, remote: Partial<VideoProject>): VideoProject {
  const localById = new Map(local.segments.map((segment) => [segment.id, segment]));
  const remoteSegments = Array.isArray(remote.segments) ? remote.segments : undefined;
  const mergedSegments = remoteSegments
    ? remoteSegments.map((segment, index) => mergeSegment(localById.get(segment.id) ?? local.segments[index], segment, index))
    : local.segments;
  const segments = mergedSegments.map((segment, index) => !isVideoMediaSegment(segment) && segment.continuation === "previous_video" && index > 0 && segment.continuation_range
    ? { ...segment, continuation_range: normalizeVideoContinuationRange(segment.continuation_range, videoSegmentDuration(mergedSegments[index - 1])) }
    : segment.continuation_range ? { ...segment, continuation_range: undefined } : segment);
  return {
    ...local,
    ...remote,
    title: typeof remote.title === "string" ? remote.title : local.title,
    status: typeof remote.status === "string" ? remote.status as TimelineStatus : local.status,
    segments,
    merged: remote.merged ? { ...(local.merged ?? { status: "draft" }), ...remote.merged } : local.merged,
  };
}

export function mergedResultNotificationKey(project: VideoProject): string | undefined {
  if (project.merged?.status !== "completed") return undefined;
  const resultIdentity = project.merged.result_job_id ?? project.merged.sha256 ?? project.merged.download_url;
  return resultIdentity ? `${project.id ?? "draft"}:${resultIdentity}` : undefined;
}

export function notifyMergedResultOnce(
  project: VideoProject,
  notified: Set<string>,
  notify: () => void,
): boolean {
  const key = mergedResultNotificationKey(project);
  if (!key || notified.has(key)) return false;
  notified.add(key);
  notify();
  return true;
}

export function latestResolvedWorkflow(segment: VideoSegment): WorkflowEvidence | undefined {
  for (let index = segment.attempts.length - 1; index >= 0; index -= 1) {
    const evidence = segment.attempts[index]?.workflow_evidence;
    if (evidence && typeof evidence === "object") return evidence;
  }
  return undefined;
}

export function resolvedWorkflowSummary(evidence: WorkflowEvidence | undefined): string | undefined {
  if (!evidence) return undefined;
  const steps = Number.isFinite(evidence.steps) ? `${evidence.steps} steps` : undefined;
  const sampling = [evidence.sampler, evidence.scheduler].filter(Boolean).join(" / ") || undefined;
  const denoise = Number.isFinite(evidence.denoise) ? `denoise ${Number(evidence.denoise).toFixed(2)}` : undefined;
  const lora = evidence.lora
    ? `LoRA ${evidence.lora_strength === undefined ? "on" : Number(evidence.lora_strength).toFixed(2)}`
    : "LoRA off";
  return [steps, sampling, denoise, lora].filter(Boolean).join(" · ");
}

export function moveVideoSegment(segments: VideoSegment[], id: string, offset: -1 | 1): VideoSegment[] {
  const index = segments.findIndex((segment) => segment.id === id);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= segments.length) return segments;
  const reordered = [...segments];
  [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
  return reordered.map((segment, position) => position === 0 && segment.continuation !== "none" ? { ...segment, continuation: "none", continuation_range: undefined } : segment);
}

/**
 * Remove one shot while keeping the project structurally valid. A storyboard
 * owns a continuous source range, so deleting a cut merges that range into an
 * adjacent shot instead of leaving a gap. The merged shot must be regenerated.
 */
export function removeVideoProjectSegment(project: VideoProject, id: string): VideoProject {
  if (projectIsActive(project) || project.segments.length <= 1) return project;
  const removedIndex = project.segments.findIndex((segment) => segment.id === id);
  if (removedIndex < 0) return project;

  let segments = project.segments.filter((segment) => segment.id !== id).map((segment) => ({ ...segment }));
  const storyboard = project.storyboard ? { ...project.storyboard, cut_frames: [...project.storyboard.cut_frames] } : undefined;
  let invalidatedIndex = -1;
  if (storyboard) {
    const removedRange = project.segments[removedIndex].source_range;
    const nextSourceIndex = segments.findIndex((segment, index) => index >= removedIndex && Boolean(segment.source_range));
    const previousSourceIndex = segments.map((segment, index) => ({ segment, index }))
      .filter(({ segment, index }) => index < removedIndex && Boolean(segment.source_range)).at(-1)?.index ?? -1;
    const mergeIndex = nextSourceIndex >= 0 ? nextSourceIndex : previousSourceIndex;
    const mergeTarget = mergeIndex >= 0 ? segments[mergeIndex] : undefined;
    if (removedRange && mergeTarget?.source_range) {
      const nextRange = nextSourceIndex >= 0
        ? { ...mergeTarget.source_range, start_frame: removedRange.start_frame }
        : { ...mergeTarget.source_range, end_frame: removedRange.end_frame };
      segments[mergeIndex] = {
        ...mergeTarget,
        source_range: nextRange,
        status: mergeTarget.status === "completed" ? "stale" : "draft",
      };
      invalidatedIndex = mergeIndex;
    }
    const sourceSegments = segments.filter((segment) => Boolean(segment.source_range));
    storyboard.cut_frames = sourceSegments.slice(0, -1).map((segment) => segment.source_range!.end_frame);
  }
  if (removedIndex < segments.length && segments[removedIndex].continuation !== "none") {
    invalidatedIndex = invalidatedIndex < 0 ? removedIndex : Math.min(invalidatedIndex, removedIndex);
  }
  segments = segments.map((segment, index) => index === 0 && segment.continuation !== "none"
    ? { ...segment, continuation: "none" as const, continuation_range: undefined, status: segment.status === "completed" ? "stale" as const : "draft" as const }
    : segment);
  if (invalidatedIndex >= 0) {
    for (let index = invalidatedIndex; index < segments.length; index += 1) {
      if (index > invalidatedIndex && segments[index].continuation === "none") break;
      const segment = segments[index];
      segments[index] = { ...segment, status: segment.status === "completed" || segment.status === "stale" ? "stale" : "draft" };
    }
  }
  const selected = new Set(project.selected_segment_ids ?? []);
  selected.delete(id);
  return {
    ...project,
    status: "draft",
    current_index: undefined,
    selected_segment_ids: [...selected],
    storyboard,
    segments,
    merged: undefined,
  };
}

export function projectIsActive(project: VideoProject): boolean {
  return ACTIVE_STATUSES.has(project.status) || project.segments.some((segment) => ACTIVE_STATUSES.has(segment.status));
}

export function projectCanMerge(project: VideoProject): boolean {
  return project.segments.length > 0 && project.segments.every((segment) => segment.status === "completed");
}

export function continuationSourceReady(segments: VideoSegment[], index: number): boolean {
  const segment = segments[index];
  if (!segment || segment.continuation === "none") return true;
  return index > 0 && segments[index - 1]?.status === "completed";
}

/**
 * Produce the exact ordered run plan shown before submission. Any unfinished
 * predecessor required by a continuation is added explicitly instead of being
 * rejected or silently skipped.
 */
export function selectedTimelineRunPlan(
  segments: VideoSegment[],
  selected: ReadonlySet<string>,
): TimelineRunPlanStep[] {
  const requested = new Set(segments.filter((segment) => !isVideoMediaSegment(segment) && selected.has(segment.id)).map((segment) => segment.id));
  const included = new Set(requested);
  for (let index = 0; index < segments.length; index += 1) {
    if (!requested.has(segments[index].id)) continue;
    let cursor = index;
    while (cursor > 0 && segments[cursor].continuation !== "none") {
      const predecessor = segments[cursor - 1];
      if (predecessor.status === "completed") break;
      included.add(predecessor.id);
      cursor -= 1;
    }
  }
  return segments.flatMap((segment, index) => included.has(segment.id) ? [{
    id: segment.id,
    index,
    status: segment.status,
    autoIncluded: !requested.has(segment.id),
    reason: requested.has(segment.id) ? "selected" as const : "unfinished_predecessor" as const,
  }] : []);
}

export function projectMergeBlockReason(project: VideoProject): string | undefined {
  if (!project.segments.length) return "请先添加分段";
  const stale = project.segments.findIndex((segment) => segment.status === "stale");
  if (stale >= 0) return `分段 ${stale + 1} 因前驱重跑已过期，请重新生成依赖链后再合并`;
  const unfinished = project.segments.findIndex((segment) => segment.status !== "completed");
  if (unfinished >= 0) return `分段 ${unfinished + 1} 尚未完成，全部分段完成后才能合并`;
  return undefined;
}

export function validateVideoProject(project: VideoProject, profiles: TimelineProfile[] = [], assets: TimelineReferenceAsset[] = []): string[] {
  const errors: string[] = [];
  if (!project.title.trim()) errors.push("Project title is required");
  if (!project.segments.length) errors.push("At least one segment is required");
  const storyboard = project.storyboard;
  if (storyboard) {
    if (!ASSET_ID.test(storyboard.source_asset_id)) errors.push("Storyboard source asset id is invalid");
    if (!Number.isFinite(storyboard.fps) || storyboard.fps <= 0) errors.push("Storyboard fps must be positive");
    if (!Number.isInteger(storyboard.frame_count) || storyboard.frame_count < 1) errors.push("Storyboard frame count must be a positive integer");
    const cuts = storyboard.cut_frames;
    if (!Array.isArray(cuts) || cuts.some((frame) => !Number.isInteger(frame) || frame <= 0 || frame >= storyboard.frame_count) || cuts.some((frame, index) => index > 0 && frame <= cuts[index - 1])) errors.push("Storyboard cut frames must be unique ordered interior integers");
    const sourceSegments = project.segments.filter((segment) => Boolean(segment.source_range));
    if (Array.isArray(cuts) && cuts.length !== Math.max(0, sourceSegments.length - 1)) errors.push("Storyboard cut frames must match the source segment boundaries");
  }
  project.segments.forEach((segment, index) => {
    const label = `Segment ${index + 1}`;
    if (isVideoMediaSegment(segment)) {
      const source = segment.media_source!;
      const sourceIds = [source.asset_id, source.job_id].filter(Boolean);
      if ((source.type === "asset" && (!source.asset_id || source.job_id)) || (source.type === "job" && (!source.job_id || source.asset_id)) || sourceIds.length !== 1) errors.push(`${label} direct media source identity is invalid`);
      if ((source.asset_id && !ASSET_ID.test(source.asset_id)) || (source.job_id && !ASSET_ID.test(source.job_id))) errors.push(`${label} direct media source id is invalid`);
      if (!Number.isFinite(source.fps) || source.fps <= 0 || !Number.isInteger(source.start_frame) || !Number.isInteger(source.end_frame) || source.start_frame < 0 || source.end_frame <= source.start_frame) errors.push(`${label} direct media frame range is invalid`);
      if (typeof source.keep_audio !== "boolean") errors.push(`${label} direct media keep_audio must be boolean`);
      if (source.type === "asset" && source.asset_id) {
        const asset = assets.find((item) => item.id === source.asset_id);
        if (asset && asset.kind !== "video") errors.push(`${label} direct media asset must be a video`);
        const duration = Number(asset?.media.duration ?? 0);
        if (asset && Number.isFinite(duration) && duration > 0 && source.end_frame / source.fps > duration + 1 / source.fps) errors.push(`${label} direct media range exceeds the source video`);
      }
      if (segment.continuation !== "none" || segment.source_range || segment.continuation_range || segment.motion_context) errors.push(`${label} direct media cannot use H3 continuation or source references`);
      return;
    }
    if (segment.media_source) errors.push(`${label} generation segment cannot contain a direct media source`);
    const profile = findTimelineProfile(profiles, segment.request.profile_id, segment.request.profile_version);
    if (!segment.request.prompt.trim()) errors.push(`${label} needs a prompt`);
    const duration = Number(segment.request.parameters.duration);
    const allowedDurations = profileDurationOptions(profile);
    if (!Number.isFinite(duration) || !isH3DurationOption(duration)) {
      errors.push(`${label} duration must use the H3 17k+5 frame grid`);
    } else if (!allowedDurations.some((option) => Math.abs(option - duration) < 1e-9)) {
      const [minimumDuration, maximumDuration] = profileBounds(profile, "duration", [H3_DURATION_OPTIONS[0], H3_MAX_GENERATION_DURATION]);
      errors.push(`${label} duration must be in the profile range ${minimumDuration}..${maximumDuration}`);
    }
    if (segment.request.prompt_mode !== "preserve_tags_only") errors.push(`${label} prompt mode must preserve user text and map tags only`);
    const steps = Number(segment.request.parameters.steps);
    const [minimumSteps, maximumSteps] = profileBounds(profile, "steps", [1, 100]);
    if (!Number.isInteger(steps) || steps < minimumSteps || steps > maximumSteps) errors.push(`${label} steps must be an integer in ${minimumSteps}..${maximumSteps}`);
    const lora = Number(segment.request.parameters.lora_strength);
    const [minimumLora, maximumLora] = profileBounds(profile, "lora_strength", profile?.sampling_mode === "base" ? [0, 0] : [0, 2]);
    if (!Number.isFinite(lora) || lora < minimumLora || lora > maximumLora) errors.push(`${label} LoRA strength must be in ${minimumLora}..${maximumLora}`);
    if (profile?.sampling_mode === "base" && lora !== 0) errors.push(`${label} Base profile must not load the Turbo LoRA`);
    const requiredCompiler = segment.continuation === "tail_frame" ? "h3_fl" : (segment.source_range || segment.continuation === "previous_video" || segment.request.references.length > 0) ? "h3_ref" : undefined;
    if (profile && requiredCompiler && profile.compiler !== requiredCompiler) errors.push(`${label} references require a compatible ${requiredCompiler} profile`);
    if (profile?.compiler === "h3_ref" && !segment.source_range && segment.continuation !== "previous_video" && segment.request.references.length === 0) errors.push(`${label} h3_ref profile requires an explicit reference, source range, or previous-video continuation`);
    if (segment.source_range && segment.continuation === "tail_frame") errors.push(`${label} source range cannot be combined with tail-frame continuation`);
    const denoise = Number(segment.request.parameters.denoise);
    const [minimumDenoise, maximumDenoise] = profileBounds(profile, "denoise", [0.05, 1]);
    if (!Number.isFinite(denoise) || denoise < minimumDenoise || denoise > maximumDenoise) errors.push(`${label} denoise must be ${minimumDenoise}..${maximumDenoise}`);
    if (!segment.request.profile_id || !segment.request.profile_version || !/^[0-9a-f]{64}$/.test(segment.request.profile_digest)) errors.push(`${label} needs a pinned profile identity`);
    if (!CONTINUATIONS.has(segment.continuation)) errors.push(`${label} continuation is invalid`);
    if (index === 0 && segment.continuation !== "none") errors.push("The first segment continuation must be none");
    if (segment.continuation_range && segment.continuation !== "previous_video") errors.push(`${label} continuation range requires previous-video continuation`);
    if (segment.continuation === "previous_video" && segment.continuation_range && index > 0) {
      const range = segment.continuation_range;
      const previousFrames = videoContinuationFrameCount(videoSegmentDuration(project.segments[index - 1]));
      if (range.fps !== H3_GENERATION_FPS || !Number.isInteger(range.start_frame) || !Number.isInteger(range.end_frame) || range.start_frame < 0 || range.end_frame <= range.start_frame || range.end_frame > previousFrames || range.end_frame - range.start_frame > H3_MAX_CONTINUATION_FRAMES) errors.push(`${label} continuation range must be 1..${H3_MAX_CONTINUATION_FRAMES} frames within the previous video at ${H3_GENERATION_FPS}fps`);
    }
    const referenceBudget = segment.request.references.length + (["tail_frame", "previous_video"].includes(segment.continuation) ? 1 : 0) + (segment.source_range ? 1 : 0);
    if (referenceBudget > H3_MAX_REFERENCE_FILES) errors.push(`${label} exceeds the ${H3_MAX_REFERENCE_FILES}-file reference budget including continuation`);
    const ids = segment.request.references.map((reference) => reference.asset_id ?? reference.id ?? "");
    if (ids.some((id) => !ASSET_ID.test(id)) || new Set(ids).size !== ids.length) errors.push(`${label} has invalid or duplicate references`);
    if (segment.request.references.some((reference) => !reference.role)) errors.push(`${label} has a reference without a role`);
    const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
    const resolvedReferences = segment.request.references.flatMap((reference) => {
      const id = reference.asset_id ?? reference.id ?? "";
      const asset = assetsById.get(id);
      return asset ? [{ reference, asset }] : [];
    });
    const videoReferences = resolvedReferences.filter((item) => item.asset.kind === "video");
    const audioReferences = resolvedReferences.filter((item) => item.asset.kind === "audio");
    const imageReferences = resolvedReferences.filter((item) => item.asset.kind === "image");
    const selectedVideoAudio = videoReferences.filter((item) => item.reference.include_audio);
    const implicitPreviousVideo = segment.continuation === "previous_video" ? 1 : 0;
    const implicitSourceVideo = segment.source_range ? 1 : 0;
    if (imageReferences.length + (segment.continuation === "tail_frame" ? 1 : 0) > 9) errors.push(`${label} may use at most nine image references including continuation`);
    if (videoReferences.length + implicitPreviousVideo + implicitSourceVideo > 3) errors.push(`${label} may use at most three video references including continuation`);
    if (audioReferences.length + selectedVideoAudio.length > 3) errors.push(`${label} may use at most three selected audio references including video soundtracks`);
    if (resolvedReferences.length === segment.request.references.length && resolvedReferences.length > 0 && resolvedReferences.every((item) => item.asset.kind === "audio") && segment.continuation !== "previous_video" && !segment.source_range) errors.push(`${label} cannot use an audio-only H3 reference set`);
    let videoDurationTotal = segment.continuation === "previous_video" && index > 0
      ? segment.continuation_range
        ? (segment.continuation_range.end_frame - segment.continuation_range.start_frame) / segment.continuation_range.fps
        : Math.min(15, videoSegmentDuration(project.segments[index - 1]))
      : 0;
    if (segment.source_range) videoDurationTotal += (segment.source_range.end_frame - segment.source_range.start_frame) / segment.source_range.fps;
    let audioDurationTotal = 0;
    for (const { reference, asset } of resolvedReferences) {
      if (asset.kind !== "video" && asset.kind !== "audio") continue;
      const referenceDuration = Number(asset.media.duration);
      if (!Number.isFinite(referenceDuration) || referenceDuration <= 0) {
        errors.push(`${label} ${asset.kind} reference is missing duration metadata`);
        continue;
      }
      if (referenceDuration < 2 || referenceDuration > 15) errors.push(`${label} each ${asset.kind} reference must be between 2 and 15 seconds`);
      if (asset.kind === "video") {
        videoDurationTotal += referenceDuration;
        if (reference.include_audio) {
          if (asset.media.has_audio !== true) errors.push(`${label} selected video reference has no audio track`);
          audioDurationTotal += referenceDuration;
        }
      } else audioDurationTotal += referenceDuration;
    }
    if (videoDurationTotal > 15 + 1e-6) errors.push(`${label} reference videos may total at most 15 seconds`);
    if (audioDurationTotal > 15 + 1e-6) errors.push(`${label} selected reference audio may total at most 15 seconds`);
    if (segment.continuation === "tail_frame" && (segment.request.references.length > 1 || segment.request.references.some((reference) => reference.role !== "last_frame"))) errors.push(`${label} tail-frame continuation only accepts one optional last_frame image reference`);
    if (segment.continuation === "previous_video" && segment.request.references.some((reference) => ["first_frame", "last_frame"].includes(reference.role))) errors.push(`${label} previous-video continuation cannot mix first_frame or last_frame roles`);
    if (segment.continuation === "motion_context") {
      const settings = segment.motion_context ?? { video_frames: 22, audio_frames: 24 };
      if (![5, 22, 39, 56].includes(settings.video_frames)) errors.push(`${label} Motion Context video frames must be 5, 22, 39, or 56`);
      if (!Number.isInteger(settings.audio_frames) || settings.audio_frames < 0 || settings.audio_frames > 240) errors.push(`${label} Motion Context audio frames must be an integer in 0..240`);
      if (segment.request.references.some((reference) => reference.role === "first_frame")) errors.push(`${label} Motion Context cannot be combined with a first-frame reference`);
      if (index > 0 && !isVideoMediaSegment(project.segments[index - 1]) && segment.request.parameters.aspect_ratio !== project.segments[index - 1].request.parameters.aspect_ratio) errors.push(`${label} Motion Context must keep the previous aspect ratio`);
    }
    if (index > 0 && segment.continuation === "tail_frame" && !isVideoMediaSegment(project.segments[index - 1]) && segment.request.parameters.aspect_ratio !== project.segments[index - 1].request.parameters.aspect_ratio) errors.push(`${label} tail-frame continuation must keep the previous aspect ratio`);
    if (storyboard && segment.source_range) {
      const range = segment.source_range;
      {
        if (range.asset_id !== storyboard.source_asset_id) errors.push(`${label} source range asset must match the storyboard`);
        if (!Number.isFinite(range.fps) || Math.abs(range.fps - storyboard.fps) > 1e-6) errors.push(`${label} source range fps must match the storyboard`);
        if (!Number.isInteger(range.start_frame) || !Number.isInteger(range.end_frame) || range.start_frame < 0 || range.end_frame <= range.start_frame || range.end_frame > storyboard.frame_count) errors.push(`${label} source range frames are invalid`);
        if (Number.isFinite(range.fps) && (range.end_frame - range.start_frame) / range.fps < 2 - 1e-6) errors.push(`${label} source range must be at least 2 seconds for H3 Ref2VA`);
        if (Number.isFinite(range.fps) && (range.end_frame - range.start_frame) / range.fps > 15 + 1e-6) errors.push(`${label} source range may not exceed 15 seconds`);
        const sourceSegments = project.segments.filter((item) => Boolean(item.source_range));
        const sourceIndex = sourceSegments.findIndex((item) => item.id === segment.id);
        const previous = sourceIndex > 0 ? sourceSegments[sourceIndex - 1]?.source_range : undefined;
        if (sourceIndex === 0 && range.start_frame !== 0) errors.push("The first source range must begin at frame 0");
        if (previous && previous.end_frame !== range.start_frame) errors.push(`${label} source range must continue from the previous segment`);
        if (sourceIndex < sourceSegments.length - 1 && storyboard.cut_frames[sourceIndex] !== range.end_frame) errors.push(`${label} end frame must match the storyboard cut frame`);
        if (sourceIndex === sourceSegments.length - 1 && range.end_frame !== storyboard.frame_count) errors.push("The last source range must end at the storyboard frame count");
      }
    } else if (segment.source_range) errors.push(`${label} source range requires a storyboard`);
  });
  return errors;
}

export function timelineStatusLabel(status: string): string {
  return ({ draft: "草稿", pending: "待生成", submitting: "提交中", queued: "排队", running: "生成中", partial: "部分完成", stopping: "停止中", stopped: "已停止", canceled: "已取消", stale: "需重跑", completed: "已完成", failed: "失败", merging: "合并中" } as Record<string, string>)[status] ?? status;
}
