"use client";
/* eslint-disable jsx-a11y/media-has-caption */
import { useRef, useState } from "react";
import type { LibraryAsset } from "./studio-library";
import { remixVoiceTask, voiceDownloadUrl, voicePreviewUrl, type VoiceTask, type VoiceTrack } from "./voice-studio-api";
const LABELS: Record<VoiceTrack, string> = { mix: "生成混音", dry_vocal: "换声干声", accompaniment: "原伴奏", remix: "调整后的混音" };

export default function VoiceTaskResult({ task, source, onUpdated }: { task: VoiceTask; source?: LibraryAsset; onUpdated: (task: VoiceTask) => void }) {
  const [track, setTrack] = useState<VoiceTrack>(task.outputs?.remix ? "remix" : "mix");
  const original = useRef<HTMLAudioElement>(null), result = useRef<HTMLAudioElement>(null);
  const [vocal, setVocal] = useState(task.mixParameters?.vocal_gain_db ?? 0);
  const [backing, setBacking] = useState(task.mixParameters?.accompaniment_gain_db ?? 0);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const available = (["mix", "remix", "dry_vocal", "accompaniment"] as VoiceTrack[]).filter(item => item === "mix" ? Boolean(task.outputs?.mix ?? task.output) : Boolean(task.outputs?.[item]));
  if (!available.length) return null;
  const selected = available.includes(track) ? track : "mix";
  const version = task.outputs?.[selected]?.sha256 ?? "";
  const preview = voicePreviewUrl(task.id, selected) + (selected === "mix" ? "?" : "&") + `v=${version}`;
  function switchTo(target: HTMLAudioElement | null, from: HTMLAudioElement | null) {
    if (!target || !from) return;
    const time = from.currentTime;
    from.pause();
    const start = () => { target.currentTime = Math.min(time, Number.isFinite(target.duration) ? Math.max(0, target.duration - 0.05) : time); void target.play().catch(() => setError("音频暂时无法播放，请重试。")); };
    if (target.readyState >= 1) start();
    else { target.addEventListener("loadedmetadata", start, { once: true }); target.load(); }
  }
  async function remix() {
    setBusy(true); setError(""); result.current?.pause();
    try { const updated = await remixVoiceTask(task.id, vocal, backing); onUpdated(updated); setTrack("remix"); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "混音失败，请重试。"); }
    finally { setBusy(false); }
  }
  return <div className="voice-task-result">
    {source && <div className="voice-compare-player"><strong>原曲</strong><audio ref={original} controls preload="none" src={source.contentUrl} aria-label="原曲对照试听" onPlay={() => result.current?.pause()}/><button type="button" onClick={() => switchTo(original.current, result.current)}>同位置切到原曲</button></div>}
    <div className="voice-compare-player"><label className="voice-track-select">改后音频<select aria-label="试听与导出音轨" value={selected} onChange={event => { result.current?.pause(); setTrack(event.target.value as VoiceTrack); }}>{available.map(item => <option key={item} value={item}>{LABELS[item]}</option>)}</select></label>
      <audio key={`${task.id}:${selected}:${version}`} ref={result} controls preload="none" src={preview} aria-label={`${LABELS[selected]}试听`} onPlay={() => original.current?.pause()}/>
      {source && <button type="button" onClick={() => switchTo(result.current, original.current)}>同位置切到结果</button>}
    </div>
    {task.outputs?.dry_vocal && task.outputs?.accompaniment && <details className="voice-mix-controls"><summary>调整人声与伴奏</summary>
      <p>伴奏不明显时，降低人声或提高伴奏。只重新混音，无需重新生成。</p>
      <label>人声 <output>{vocal > 0 ? "+" : ""}{vocal} dB</output><input aria-label="人声音量" type="range" min={-18} max={12} step={1} value={vocal} disabled={busy} onChange={event => setVocal(Number(event.target.value))}/></label>
      <label>伴奏 <output>{backing > 0 ? "+" : ""}{backing} dB</output><input aria-label="伴奏音量" type="range" min={-18} max={12} step={1} value={backing} disabled={busy} onChange={event => setBacking(Number(event.target.value))}/></label>
      <div className="lyrics-editor-actions"><button type="button" disabled={busy} onClick={() => { setVocal(-3); setBacking(3); }}>突出伴奏</button><button type="button" disabled={busy} onClick={() => void remix()}>{busy ? "正在混音…" : "应用并生成混音"}</button></div>
      <small>保留原始结果；音量过高时会整体衰减以避免削波。本次混音不添加回声或混响。</small>
    </details>}
    {error && <p role="alert" className="voice-error">{error}</p>}
    <a href={voiceDownloadUrl(task.id, selected)} download={`voice-${task.id.slice(0, 8)}-${selected}.wav`}>导出{LABELS[selected]} WAV</a>
    <small>试听与导出使用同一份音轨文件。</small>
  </div>;
}
