import type { VoiceTask } from "./voice-studio-api.ts";

export type LyricsDraft = { sourceId: string; original: string; lyrics: string; edited: boolean; originalEdited: boolean; recognizedTaskId: string };
export function emptyLyricsDraft(sourceId = ""): LyricsDraft {
  return { sourceId, original: "", lyrics: "", edited: false, originalEdited: false, recognizedTaskId: "" };
}
export function applyTranscription(draft: LyricsDraft, task: VoiceTask): LyricsDraft {
  if (task.operation !== "transcribe" || task.status !== "completed" || task.sourceAssetId !== draft.sourceId || !task.detectedLyrics || draft.recognizedTaskId === task.id) return draft;
  return { ...draft, original: draft.originalEdited ? draft.original : task.detectedLyrics,
    lyrics: draft.edited ? draft.lyrics : task.detectedLyrics, recognizedTaskId: task.id };
}
export function lyricLines(original: string, lyrics: string) {
  const before = original.split("\n"), after = lyrics.split("\n");
  const count = (text: string) => [...text].filter(char => /[\p{L}\p{N}]/u.test(char)).length;
  return Array.from({ length: Math.max(before.length, after.length) }, (_, index) => ({ index,
    original: before[index] ?? "", lyrics: after[index] ?? "", before: count(before[index] ?? ""), after: count(after[index] ?? "") }));
}
export const DRAFT_KEY = "h3-studio.lyrics-drafts.v1";
export function readLyricsDraft(raw: string | null, sourceId?: string): LyricsDraft {
  try {
    const data = JSON.parse(raw ?? "{}");
    const id = sourceId ?? data.lastSourceId;
    const value = data.drafts?.[id];
    if (!/^[a-f0-9]{32}$/.test(id) || !value || typeof value.original !== "string" || typeof value.lyrics !== "string" || value.original.length > 20000 || value.lyrics.length > 20000) return emptyLyricsDraft(sourceId);
    return { sourceId: id, original: value.original, lyrics: value.lyrics, edited: value.edited === true, originalEdited: value.originalEdited === true, recognizedTaskId: typeof value.recognizedTaskId === "string" ? value.recognizedTaskId : "" };
  } catch { return emptyLyricsDraft(sourceId); }
}
export function saveLyricsDraft(raw: string | null, draft: LyricsDraft): string {
  let drafts: Record<string, LyricsDraft> = {};
  try { drafts = JSON.parse(raw ?? "{}").drafts ?? {}; } catch { /* fresh storage */ }
  const entries = Object.entries(drafts).filter(([id]) => id !== draft.sourceId).slice(-19);
  return JSON.stringify({ lastSourceId: draft.sourceId, drafts: { ...Object.fromEntries(entries), [draft.sourceId]: draft } });
}
