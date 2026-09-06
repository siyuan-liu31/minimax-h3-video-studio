import { findTimelineProfile, isVideoMediaSegment, retargetSegmentCompiler, videoSegmentDuration, type TimelineProfile, type VideoSegment, type VideoStoryboard } from "./video-project.ts";

const H3_FPS = 24;
const H3_DURATION_OPTIONS = Array.from(
  { length: Math.floor((362 - 124) / 17) + 1 },
  (_, index) => (124 + index * 17) / H3_FPS,
);

function effectiveDuration(seconds: number): number {
  if (!Number.isFinite(seconds)) return H3_DURATION_OPTIONS[0];
  return H3_DURATION_OPTIONS.reduce((nearest, option) => (
    Math.abs(option - seconds) < Math.abs(nearest - seconds) ? option : nearest
  ), H3_DURATION_OPTIONS[0]);
}

export type SceneCutSuggestion = {
  seconds: number;
  confidence?: number;
  label?: string;
};

function finite(value: unknown): number | undefined {
  const number = typeof value === "string" && value.trim() ? Number(value) : value;
  return typeof number === "number" && Number.isFinite(number) ? number : undefined;
}

/** Accept the common scene-analysis response shapes without coupling the UI to one detector. */
export function parseSceneCutSuggestions(value: unknown, duration = Number.POSITIVE_INFINITY): SceneCutSuggestion[] {
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  const raw = [record.suggestions, record.cut_points, record.cutPoints, record.cuts, record.scenes]
    .find(Array.isArray) as unknown[] | undefined;
  if (!raw) return [];
  const suggestions = raw.flatMap((item, index): SceneCutSuggestion[] => {
    if (typeof item === "number" || typeof item === "string") {
      const seconds = finite(item);
      return seconds === undefined ? [] : [{ seconds, label: `建议 ${index + 1}` }];
    }
    if (!item || typeof item !== "object") return [];
    const scene = item as Record<string, unknown>;
    const seconds = finite(scene.seconds ?? scene.time ?? scene.at ?? scene.cut_time ?? scene.start_sec ?? scene.start);
    if (seconds === undefined) return [];
    const confidence = finite(scene.confidence ?? scene.score);
    const label = typeof scene.label === "string" && scene.label.trim() ? scene.label.trim() : `建议 ${index + 1}`;
    return [{ seconds, ...(confidence === undefined ? {} : { confidence }), label }];
  });
  const unique = new Map<number, SceneCutSuggestion>();
  for (const item of suggestions) {
    if (item.seconds <= 0 || item.seconds >= duration) continue;
    const rounded = Math.round(item.seconds * 1000) / 1000;
    if (!unique.has(rounded)) unique.set(rounded, { ...item, seconds: rounded });
  }
  return [...unique.values()].sort((left, right) => left.seconds - right.seconds);
}

export function normalizeRunSelection(segments: VideoSegment[], selected: ReadonlySet<string>): Set<string> {
  const valid = new Set(segments.filter((segment) => !isVideoMediaSegment(segment)).map((segment) => segment.id));
  return new Set([...selected].filter((id) => valid.has(id)));
}

export function allRunSelection(segments: VideoSegment[]): Set<string> {
  return new Set(segments.filter((segment) => !isVideoMediaSegment(segment)).map((segment) => segment.id));
}

export function invertRunSelection(segments: VideoSegment[], selected: ReadonlySet<string>): Set<string> {
  return new Set(segments.filter((segment) => !isVideoMediaSegment(segment) && !selected.has(segment.id)).map((segment) => segment.id));
}

/** Partial runs may not skip an unfinished predecessor needed for continuation. */
export function selectedRunDependencyError(segments: VideoSegment[], selected: ReadonlySet<string>): string | undefined {
  for (let index = 1; index < segments.length; index += 1) {
    const segment = segments[index];
    if (!selected.has(segment.id) || segment.continuation === "none") continue;
    const previous = segments[index - 1];
    if (previous.status !== "completed" && !selected.has(previous.id)) {
      return `分段 ${index + 1} 使用续接，但未选中尚未完成的前驱分段 ${index}。请勾选前驱或取消本段续接。`;
    }
  }
  return undefined;
}

export function segmentIndexAtTime(segments: VideoSegment[], seconds: number): number {
  if (!segments.length) return -1;
  const target = Math.max(0, Number(seconds) || 0);
  let cursor = 0;
  for (let index = 0; index < segments.length; index += 1) {
    cursor += videoSegmentDuration(segments[index]);
    if (target < cursor || index === segments.length - 1) return index;
  }
  return segments.length - 1;
}

function durationOptionsFor(segment: VideoSegment, profiles: TimelineProfile[]): number[] {
  const profile = findTimelineProfile(profiles, segment.request.profile_id, segment.request.profile_version);
  const limit = profile?.limits.duration;
  const minimum = Array.isArray(limit) ? Number(limit[0]) : typeof limit === "number" ? limit : H3_DURATION_OPTIONS[0];
  const maximum = Array.isArray(limit) ? Number(limit[1]) : typeof limit === "number" ? limit : H3_DURATION_OPTIONS.at(-1)!;
  return H3_DURATION_OPTIONS.filter((duration) => duration >= minimum - 1e-9 && duration <= maximum + 1e-9);
}

function nearestDuration(value: number, segment: VideoSegment, profiles: TimelineProfile[]): number {
  const options = durationOptionsFor(segment, profiles);
  if (!options.length) return effectiveDuration(value);
  return options.reduce((nearest, option) => (
    Math.abs(option - value) < Math.abs(nearest - value) ? option : nearest
  ), options[0]);
}

function draftClone(segment: VideoSegment, id: string, duration: number): VideoSegment {
  return {
    ...segment,
    id,
    status: "draft",
    attempts: [],
    job_id: undefined,
    preview_url: undefined,
    thumbnail_url: undefined,
    download_url: undefined,
    error: undefined,
    request: {
      ...segment.request,
      parts: {},
      prompt_mode: "preserve_tags_only",
      parameters: { ...segment.request.parameters, duration },
      references: segment.request.references.map((reference) => ({ ...reference })),
    },
  };
}

/** Split the shot under the playhead. Both resulting shots stay on an allowed H3 duration. */
export function splitSegmentDraftAtTime(
  segments: VideoSegment[],
  seconds: number,
  profiles: TimelineProfile[],
  createId: () => string,
): VideoSegment[] {
  const index = segmentIndexAtTime(segments, seconds);
  if (index < 0) return segments;
  const segment = segments[index];
  const options = durationOptionsFor(segment, profiles);
  if (!options.length) return segments;
  const minimum = options[0];
  const duration = effectiveDuration(segment.request.parameters.duration);
  let start = 0;
  for (let cursor = 0; cursor < index; cursor += 1) start += effectiveDuration(segments[cursor].request.parameters.duration);
  const local = Math.max(0, Math.min(duration, seconds - start));
  if (local < minimum || duration - local < minimum) return segments;
  const left = draftClone(segment, segment.id, nearestDuration(local, segment, profiles));
  const right = draftClone(segment, createId(), nearestDuration(duration - local, segment, profiles));
  return [...segments.slice(0, index), left, right, ...segments.slice(index + 1)];
}

/** Rebuild a draft into equal-duration shots while preserving the current workflow controls. */
export function equalizeSegmentDrafts(
  segments: VideoSegment[],
  requestedCount: number,
  sourceDuration: number,
  profiles: TimelineProfile[],
  createId: () => string,
): VideoSegment[] {
  if (!segments.length) return segments;
  const template = segments[0];
  const options = durationOptionsFor(template, profiles);
  if (!options.length) return segments;
  const minimum = options[0];
  const maximum = options.at(-1) ?? minimum;
  const duration = Number.isFinite(sourceDuration) && sourceDuration > 0
    ? sourceDuration
    : segments.reduce((total, segment) => total + effectiveDuration(segment.request.parameters.duration), 0);
  const requiredForMaximum = Math.max(1, Math.ceil(duration / maximum));
  const allowedByMinimum = Math.max(1, Math.floor(duration / minimum));
  const count = Math.min(64, allowedByMinimum, Math.max(1, Math.round(requestedCount) || 1, requiredForMaximum));
  const target = duration / count;
  return Array.from({ length: count }, (_, index) => {
    const source = segments[Math.min(index, segments.length - 1)] ?? template;
    return draftClone(source, index < segments.length ? source.id : createId(), nearestDuration(target, source, profiles));
  }).map((segment, index) => index === 0 ? { ...segment, continuation: "none" } : segment);
}

/** Turn user-confirmed source cut points into generation drafts; suggestions never call this directly. */
export function buildSegmentDraftsFromSourceCuts(
  segments: VideoSegment[],
  cutPoints: number[],
  sourceDuration: number,
  profiles: TimelineProfile[],
  createId: () => string,
): VideoSegment[] {
  if (!Number.isFinite(sourceDuration) || sourceDuration <= 0) return segments;
  const fps = H3_FPS;
  const frameCount = Math.max(1, Math.round(sourceDuration * fps));
  return buildStoryboardDraft(
    segments,
    { source_asset_id: "0".repeat(32), fps, frame_count: frameCount, cut_frames: [] },
    cutPoints.map((seconds) => Math.round(seconds * fps)),
    profiles,
    createId,
  ).segments;
}

export type StoryboardDraft = { storyboard: VideoStoryboard; segments: VideoSegment[] };

/** Build contiguous, independently verifiable integer-frame source ranges. */
export function buildStoryboardDraft(
  segments: VideoSegment[],
  source: Omit<VideoStoryboard, "cut_frames"> & { cut_frames?: number[] },
  requestedCutFrames: number[],
  profiles: TimelineProfile[],
  createId: () => string,
): StoryboardDraft {
  if (!segments.length || !Number.isFinite(source.fps) || source.fps <= 0 || !Number.isInteger(source.frame_count) || source.frame_count < 1) {
    return { storyboard: { ...source, cut_frames: [] }, segments };
  }
  const maxFrames = Math.max(1, Math.floor(source.fps * 15));
  const requested = [...new Set(requestedCutFrames
    .map((frame) => Math.round(Number(frame)))
    .filter((frame) => Number.isInteger(frame) && frame > 0 && frame < source.frame_count))]
    .sort((left, right) => left - right);
  const requestedBounds = [0, ...requested, source.frame_count];
  const effectiveBounds: number[] = [0];
  for (let index = 0; index < requestedBounds.length - 1; index += 1) {
    const start = requestedBounds[index];
    const end = requestedBounds[index + 1];
    const count = Math.max(1, Math.ceil((end - start) / maxFrames));
    for (let part = 1; part <= count; part += 1) {
      const frame = part === count ? end : Math.round(start + (end - start) * part / count);
      if (frame > effectiveBounds.at(-1)!) effectiveBounds.push(frame);
    }
  }
  const ranges = effectiveBounds.slice(0, -1).map((start, index) => ({ start, end: effectiveBounds[index + 1] }));
  if (!ranges.length || ranges.length > 64 || ranges.some((range) => range.end - range.start > maxFrames)) {
    return { storyboard: { ...source, cut_frames: requested }, segments };
  }
  const rangedTemplates = segments.filter((segment) => {
    const range = segment.source_range;
    return Boolean(range && range.asset_id === source.source_asset_id && Math.abs(range.fps - source.fps) < 1e-6);
  });
  // Before a source is first attached, the only unbound segment is the draft
  // template used to seed the storyboard. Once ranged shots exist, however,
  // unbound shots are explicit blank generation clips and must survive re-cuts.
  const unboundSegments = rangedTemplates.length
    ? segments.filter((segment) => !segment.source_range)
    : [];
  const claimedIds = new Set<string>();
  const next = ranges.map((range, index) => {
    const overlapping = rangedTemplates.map((segment) => {
      const previous = segment.source_range!;
      return { segment, overlap: Math.max(0, Math.min(range.end, previous.end_frame) - Math.max(range.start, previous.start_frame)) };
    }).filter((candidate) => candidate.overlap > 0).sort((left, right) => right.overlap - left.overlap);
    const template = overlapping[0]?.segment ?? segments[Math.min(index, segments.length - 1)] ?? segments[0];
    const duration = nearestDuration((range.end - range.start) / source.fps, template, profiles);
    const keepTemplateId = Boolean(overlapping[0]) && !claimedIds.has(template.id);
    const fallbackId = !overlapping[0] && index < segments.length ? template.id : undefined;
    const id = keepTemplateId ? template.id : fallbackId ?? createId();
    claimedIds.add(id);
    const segment = draftClone(template, id, duration);
    return retargetSegmentCompiler({
      ...segment,
      ...((index === 0 || segment.continuation === "tail_frame") ? { continuation: "none" as const } : {}),
      source_range: {
        asset_id: source.source_asset_id,
        start_frame: range.start,
        end_frame: range.end,
        fps: source.fps,
      },
    }, profiles, "h3_ref");
  });
  return {
    storyboard: {
      source_asset_id: source.source_asset_id,
      fps: source.fps,
      frame_count: source.frame_count,
      cut_frames: effectiveBounds.slice(1, -1),
    },
    // User-created blank generation shots live on the same sequence timeline,
    // but are not part of the source video's frame partition. Preserve them
    // when the user refines source cuts instead of silently deleting them.
    segments: [...next, ...unboundSegments],
  };
}
