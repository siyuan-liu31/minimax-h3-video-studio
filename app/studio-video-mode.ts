export const VIDEO_DIRECTOR_MODES = ["auto", "t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v"] as const;
export const H3_MAX_REFERENCE_FILES = 12;

export type VideoDirectorMode = typeof VIDEO_DIRECTOR_MODES[number];
export type VideoLowLevelMode = "text" | "fl2va" | "ref2va";
export type VideoModeMediaKind = "image" | "video" | "audio";

export type VideoModeAsset = {
  nodeId: string;
  assetId?: string;
  kind: VideoModeMediaKind;
  label: string;
  role: string;
  includeAudio?: boolean;
};

export type VideoDirectorContract = {
  requestedMode: VideoDirectorMode;
  resolvedMode: Exclude<VideoDirectorMode, "auto">;
  lowLevelMode: VideoLowLevelMode;
  compiler: "h3_fl" | "h3_ref";
  source?: VideoModeAsset;
  references: VideoModeAsset[];
  orderedAssets: VideoModeAsset[];
  errors: string[];
};

export const VIDEO_DIRECTOR_MODE_LABELS: Record<VideoDirectorMode, string> = {
  auto: "Auto · 按连线判断",
  t2v: "T2V · 文生视频",
  i2v: "I2V · 单图生视频",
  fl2v: "FL2V · 端点帧生视频",
  r2v: "R2V · 多模态参考生视频",
  v2v: "V2V · 源视频重制",
  rv2v: "RV2V · 源视频 + 多模态参考",
};

export const VIDEO_DIRECTOR_MODE_HELP: Record<VideoDirectorMode, string> = {
  auto: "根据当前连线选择 T2V、I2V、FL2V 或 R2V；不会自动把某条视频认作源视频。V2V/RV2V 必须显式选择。",
  t2v: "只使用文字提示词，不允许连接图片、视频或音频参考。",
  i2v: "连接且只连接一张图片，作为首帧驱动视频。",
  fl2v: "连接 1–2 张端点图片：可仅首帧、仅尾帧，或同时提供首帧与尾帧；保留素材现有端点角色。",
  r2v: "允许图片、视频和音频参考；不支持只有音频的组合。",
  v2v: "显式选择一条已连视频作为源视频；不允许额外参考。源视频固定映射为 <Video 1>。",
  rv2v: "显式源视频固定为 <Video 1>，可额外连接图片或音频参考；不允许第二条视频。",
};

export function normalizeVideoDirectorMode(value: unknown, legacyModelMode?: unknown): VideoDirectorMode {
  if (typeof value === "string" && (VIDEO_DIRECTOR_MODES as readonly string[]).includes(value)) return value as VideoDirectorMode;
  if (legacyModelMode === "Ref2VA") return "r2v";
  // Legacy FL2VA included both one-frame and first/last-frame routing, so Auto
  // is the only lossless migration until the restored connections are known.
  return "auto";
}

function inferAutoMode(assets: VideoModeAsset[]): Exclude<VideoDirectorMode, "auto"> {
  if (!assets.length) return "t2v";
  if (assets.length === 1 && assets[0].kind === "image" && assets[0].role === "first_frame") return "i2v";
  if (assets.length === 1 && assets[0].kind === "image" && assets[0].role === "last_frame") return "fl2v";
  if (assets.length === 2 && assets.every((asset) => asset.kind === "image" && ["first_frame", "last_frame"].includes(asset.role)) && new Set(assets.map((asset) => asset.role)).size === 2) return "fl2v";
  return "r2v";
}

function lowLevelMode(mode: Exclude<VideoDirectorMode, "auto">): VideoLowLevelMode {
  if (mode === "t2v") return "text";
  if (mode === "i2v" || mode === "fl2v") return "fl2va";
  return "ref2va";
}

export function buildVideoDirectorContract(
  requestedMode: VideoDirectorMode,
  sourceVideoId: string,
  assets: VideoModeAsset[],
): VideoDirectorContract {
  const resolvedMode = requestedMode === "auto" ? inferAutoMode(assets) : requestedMode;
  const sourceMode = resolvedMode === "v2v" || resolvedMode === "rv2v";
  const source = sourceMode && sourceVideoId
    ? assets.find((asset) => asset.kind === "video" && asset.assetId === sourceVideoId)
    : undefined;
  const references = source ? assets.filter((asset) => asset.nodeId !== source.nodeId) : [...assets];
  const orderedAssets = source ? [source, ...references] : [...references];
  const errors: string[] = [];

  if (assets.length > H3_MAX_REFERENCE_FILES) errors.push(`H3 单任务最多连接 ${H3_MAX_REFERENCE_FILES} 个参考文件。`);
  const imageCount = assets.filter((asset) => asset.kind === "image").length;
  const videoCount = assets.filter((asset) => asset.kind === "video").length;
  const audioCount = assets.filter((asset) => asset.kind === "audio").length + assets.filter((asset) => asset.kind === "video" && asset.includeAudio).length;
  if (imageCount > 9) errors.push("H3 单任务最多连接 9 张图片。");
  if (videoCount > 3) errors.push("H3 单任务最多连接 3 个视频。");
  if (audioCount > 3) errors.push("H3 单任务最多连接 3 个音频输入（含视频配对音轨）。");
  if (resolvedMode === "t2v" && assets.length) errors.push("T2V 不能连接参考素材；请断开素材或切换创作模式。");
  if (resolvedMode === "i2v" && (assets.length !== 1 || assets[0]?.kind !== "image")) errors.push("I2V 需要且只允许一张连接到 H3 Video 的图片。");
  if (resolvedMode === "fl2v") {
    if (assets.length < 1 || assets.length > 2 || assets.some((asset) => asset.kind !== "image")) errors.push("FL2V 需要 1–2 张端点图片。");
    else if (assets.some((asset) => !["first_frame", "last_frame"].includes(asset.role))) errors.push("FL2V 图片必须保留 first_frame 或 last_frame 端点角色；请重新按端点模式连接。");
    else if (assets.length === 2 && new Set(assets.map((asset) => asset.role)).size !== 2) errors.push("FL2V 同时连接两张图时必须分别为首帧和尾帧，角色不能重复。");
  }
  if (resolvedMode === "r2v") {
    if (!assets.length) errors.push("R2V 至少需要一个图片、视频或音频参考。");
    if (assets.length && assets.every((asset) => asset.kind === "audio")) errors.push("R2V 不支持只有音频的参考组合。");
  }
  if (sourceMode && !sourceVideoId) errors.push(`${resolvedMode.toUpperCase()} 必须明确选择一条源视频。`);
  else if (sourceMode && !source) errors.push("所选源视频未连接、尚未上传完成或已被移除，请重新选择。");
  if (resolvedMode === "v2v" && source && references.length) errors.push("V2V 只允许源视频；请断开其他参考，或切换到 RV2V/R2V。");
  if (resolvedMode === "rv2v" && source) {
    if (references.some((asset) => asset.kind === "video")) errors.push("RV2V 只允许一条源视频；额外参考只能是图片或音频。");
  }
  if (sourceMode && source?.includeAudio) errors.push("V2V/RV2V 不支持把源视频音轨作为参考；请关闭“同时参考视频音轨”。模型会生成新的音频。");

  const mode = lowLevelMode(resolvedMode);
  return {
    requestedMode,
    resolvedMode,
    lowLevelMode: mode,
    compiler: mode === "ref2va" ? "h3_ref" : "h3_fl",
    source,
    references,
    orderedAssets,
    errors,
  };
}

export function graphRoleForVideoAsset(contract: VideoDirectorContract, asset: VideoModeAsset): string {
  if (contract.resolvedMode === "i2v") return "first_frame";
  if (contract.resolvedMode === "fl2v") return asset.role;
  if (["r2v", "v2v", "rv2v"].includes(contract.resolvedMode)) return "reference";
  return asset.role;
}

export function videoDirectorReferenceLabels(contract: VideoDirectorContract): Array<{ nodeId: string; source: boolean; tag: string }> {
  const counts: Record<VideoModeMediaKind, number> = { image: 0, video: 0, audio: 0 };
  // Native H3 numbers audio extracted from selected video soundtracks before
  // standalone audio, regardless of the interleaved canvas edge order.
  const soundtrackIndex = new Map(contract.orderedAssets.filter((item) => item.kind === "video" && item.includeAudio).map((item, index) => [item.nodeId, index + 1]));
  const soundtrackCount = soundtrackIndex.size;
  return contract.orderedAssets.map((item) => {
    counts[item.kind] += 1;
    const family = item.kind === "image" ? "Picture" : item.kind === "video" ? "Video" : "Audio";
    const primaryIndex = item.kind === "audio" ? soundtrackCount + counts.audio : counts[item.kind];
    const soundtrack = soundtrackIndex.has(item.nodeId) ? ` · <Audio ${soundtrackIndex.get(item.nodeId)}> (视频音轨)` : "";
    return {
      nodeId: item.nodeId,
      source: item.nodeId === contract.source?.nodeId,
      tag: `<${family} ${primaryIndex}>${soundtrack}`,
    };
  });
}

export function videoDirectorPayload(contract: VideoDirectorContract): {
  director_mode: VideoDirectorMode;
  mode: VideoLowLevelMode;
  source_asset_id?: string;
} {
  return {
    director_mode: contract.requestedMode,
    mode: contract.lowLevelMode,
    ...(contract.source?.assetId ? { source_asset_id: contract.source.assetId } : {}),
  };
}
