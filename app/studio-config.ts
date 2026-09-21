export type ImageQuality = "1K" | "2K";
export type ImageAspectRatio = "16:9" | "9:16" | "3:4" | "1:1";

const IMAGE_DIMENSIONS: Record<ImageQuality, Record<ImageAspectRatio, { width: number; height: number }>> = {
  "1K": {
    "16:9": { width: 1024, height: 576 },
    "9:16": { width: 576, height: 1024 },
    "3:4": { width: 768, height: 1024 },
    "1:1": { width: 1024, height: 1024 },
  },
  "2K": {
    "16:9": { width: 2048, height: 1152 },
    "9:16": { width: 1152, height: 2048 },
    "3:4": { width: 1536, height: 2048 },
    "1:1": { width: 2048, height: 2048 },
  },
};

const QWEN_IMAGE_21_2K_DIMENSIONS: Record<ImageAspectRatio, { width: number; height: number }> = {
  "16:9": { width: 2752, height: 1536 },
  "9:16": { width: 1536, height: 2752 },
  "3:4": { width: 1792, height: 2400 },
  "1:1": { width: 2048, height: 2048 },
};

export function imageDimensions(quality: ImageQuality, aspectRatio: ImageAspectRatio, qwenImage21 = false) {
  const dimensions = qwenImage21 && quality === "2K"
    ? QWEN_IMAGE_21_2K_DIMENSIONS[aspectRatio]
    : IMAGE_DIMENSIONS[quality]?.[aspectRatio];
  if (!dimensions) throw new Error(`Unsupported image size: ${quality} ${aspectRatio}`);
  return dimensions;
}
