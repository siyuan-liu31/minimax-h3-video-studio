"use client";

/* Audio preview is optional and bytes load only after a user presses play. */
/* eslint-disable jsx-a11y/media-has-caption */

import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import type { LibraryAsset } from "./studio-library";
import VoiceTaskResult from "./voice-task-result";
import MicrophoneRecorder from "./microphone-recorder";
import {
  cancelVoiceTask, deleteVoiceTask, getVoiceCapabilities, isSupportedVoiceAudio,
  listVoiceTasks, submitVoiceTask, uploadVoiceAudio,
  SOULX_DEFAULTS, submitRewriteTask, transcribeVoiceLyrics, validateRewriteParameters, validateYingMusicParameters, YINGMUSIC_DEFAULTS, YINGMUSIC_OUTPUT_DEFAULTS,
  type VoiceCapability, type VoiceEngine, type VoiceTask, type YingMusicParameters,
} from "./voice-studio-api";

import { applyTranscription, DRAFT_KEY, emptyLyricsDraft, lyricLines, readLyricsDraft, saveLyricsDraft } from "./lyrics-draft";

type Slot = "source" | "reference";
type Props = { assets: LibraryAsset[]; onAssetCreated: (asset: LibraryAsset) => void; onClose: () => void };

const ENGINE_LABELS: Record<VoiceEngine, string> = { yingsinger: "分句改词 · 保留原伴奏（YingMusic）", acestep: "ACE-Step 1.5 XL-SFT · 整曲重创作（实验）", soulx: "SoulX · 保留旋律改词", vevo2: "Vevo2 · 语音/清唱换音色", yingmusic: "YingMusic-SVC · 完整歌曲换音色" };
const PHRASE_DEFAULTS = { ...SOULX_DEFAULTS, diffusion_steps: 64 };
const STATUS_LABELS: Record<VoiceTask["status"], string> = {
  queued: "排队中", running: "运行中", cancelling: "取消中", completed: "已完成", failed: "失败", canceled: "已取消",
};
const STAGE_LABELS: Record<string, string> = {
  waiting_for_gpu: "等待 GPU", loading_model: "加载模型", inference: "正在生成", transcribing: "正在识别歌词", completed: "已完成",
  cancelling: "取消中", canceled: "已取消", failed: "失败", interrupted: "已中断",
};
const QUEUE_REASONS: Record<string, string> = {
  waiting_for_comfy_task: "等待视频/图像任务完成",
  waiting_for_voice_task: "等待另一换声任务完成",
  waiting_for_model_release: "等待驻留模型释放",
  waiting_for_gpu: "等待 GPU",
};

const GUIDANCE_HELP = "控制扩散生成对条件信息的引导幅度，不是音量或参考音频占比。官方默认 0.7，通常建议 0.6–0.8。提高可能强化参考音色，也可能带来颤音、金属感或咬字不稳；降低通常更自然，但音色相似度可能下降。不是越高越好。";

function taskLabel(assetId: string, assets: LibraryAsset[]): string {
  return assets.find((asset) => asset.id === assetId)?.filename ?? assetId.slice(0, 8);
}

function AudioSlot({ slot, label, selectedId, assets, uploading, onSelect, onFile, onError }: {
  slot: Slot; label: string; selectedId: string; assets: LibraryAsset[]; uploading: boolean;
  onSelect: (id: string) => void; onFile: (file: File) => void; onError: (message: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const selected = assets.find((asset) => asset.id === selectedId);
  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (uploading) return;
    const files = Array.from(event.dataTransfer.files);
    if (files.length === 1) onFile(files[0]);
    else if (files.length > 1) onError("每个音频区域一次只能放入一个文件");
  }
  function change(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) onFile(file);
    event.target.value = "";
  }
  return <section className="voice-slot" aria-label={label}>
    <div className="voice-slot-heading"><strong>{label}</strong><small>{slot === "source" ? "要转换的说话或歌曲" : "目标人物的清晰声音"}</small></div>
    <div className={`voice-drop-zone${dragging ? " dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; setDragging(true); }} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={drop}>
      <span aria-hidden="true">♫</span>
      <p>{uploading ? "上传中…" : selected ? selected.filename : "拖拽音频到这里"}</p>
      <button type="button" disabled={uploading} onClick={() => inputRef.current?.click()}>{selected ? "替换音频" : "选择本地音频"}</button>
      <input ref={inputRef} className="visually-hidden" type="file" accept=".wav,.flac,.ogg,.mp3,audio/wav,audio/flac,audio/ogg,audio/mpeg" aria-label={`上传${label}`} onChange={change}/>
    </div>
    <label className="voice-existing-label"><span>或从资产库选择</span><select value={selectedId} disabled={uploading} onChange={(event) => onSelect(event.target.value)} aria-label={`${label}资产`}>
      <option value="">选择已上传音频…</option>
      {assets.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}
    </select></label>
    {selected && <small className="voice-selected-meta">{selected.media.duration ? `${selected.media.duration.toFixed(1)} 秒` : "时长未知"} · {selected.id.slice(0, 8)}</small>}
    {selected && <audio className="voice-input-preview" controls preload="none" src={selected.contentUrl} aria-label={`${label}试听`}/>}
  </section>;
}

export default function VoiceStudio({ assets, onAssetCreated, onClose }: Props) {
  const audioAssets = useMemo(() => assets.filter((asset) => asset.kind === "audio"), [assets]);
  const [engine, setEngine] = useState<VoiceEngine>("vevo2");
  const isRewrite = engine === "soulx" || engine === "acestep" || engine === "yingsinger";
  const [coverSettings, setCoverSettings] = useState({ caption: "", audio_cover_strength: 1 });
  const [draft, setDraft] = useState(emptyLyricsDraft());
  const lyrics = draft.lyrics, originalLyrics = draft.original;
  const setLyrics = (value: string) => setDraft(current => ({ ...current, lyrics: value, edited: true }));
  const setOriginalLyrics = (value: string) => setDraft(current => ({ ...current, original: value, originalEdited: true }));
  const [draftReady, setDraftReady] = useState(false);
  const [recognizing, setRecognizing] = useState(false);
  const recognitionAttempts = useRef(new Set<string>());
  const [rewriteUseReference, setRewriteUseReference] = useState(false);
  const [rewriteParameters, setRewriteParameters] = useState(SOULX_DEFAULTS);
  const [sourceId, setSourceId] = useState("");
  const [referenceId, setReferenceId] = useState("");
  const [capabilities, setCapabilities] = useState<VoiceCapability[]>([]);
  const [capabilityState, setCapabilityState] = useState<"loading" | "ready" | "error">("loading");
  const [tasks, setTasks] = useState<VoiceTask[]>([]);
  const [tasksState, setTasksState] = useState<"loading" | "ready" | "error">("loading");
  const [uploading, setUploading] = useState<Slot | null>(null);
  const [microphonePending, setMicrophonePending] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [actionId, setActionId] = useState("");
  const [error, setError] = useState("");
  const [taskError, setTaskError] = useState("");
  const tasksReadingRef = useRef(false);
  const [steps, setSteps] = useState(String(YINGMUSIC_DEFAULTS.diffusion_steps));
  const [cfg, setCfg] = useState(String(YINGMUSIC_DEFAULTS.inference_cfg_rate));
  const [seed, setSeed] = useState(String(YINGMUSIC_DEFAULTS.seed));
  const [includeStems, setIncludeStems] = useState(YINGMUSIC_OUTPUT_DEFAULTS.include_stems);
  const [echoEnabled, setEchoEnabled] = useState(YINGMUSIC_OUTPUT_DEFAULTS.echo);
  const [reverbEnabled, setReverbEnabled] = useState(YINGMUSIC_OUTPUT_DEFAULTS.reverb);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const refreshRequestRef = useRef(0);

  function selectSource(id: string) {
    setSourceId(id);
    try { setDraft(readLyricsDraft(localStorage.getItem(DRAFT_KEY), id)); }
    catch { setDraft(emptyLyricsDraft(id)); }
    setError("");
  }
  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        if (["soulx", "acestep", "yingsinger"].includes(localStorage.getItem("h3-studio.voice-mode") ?? "")) {
          const restored = readLyricsDraft(localStorage.getItem(DRAFT_KEY));
          const restoredEngine = localStorage.getItem("h3-studio.voice-mode") as "soulx" | "acestep" | "yingsinger"; setEngine(restoredEngine); if (restoredEngine === "yingsinger") setRewriteParameters(PHRASE_DEFAULTS); if (restoredEngine === "acestep") setRewriteParameters({ diffusion_steps: 50, inference_cfg_rate: 7, seed: -1 }); setSourceId(restored.sourceId); setDraft(restored);
        }
      } catch { /* storage is optional */ }
      setDraftReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => {
    if (!draftReady) return;
    try {
      localStorage.setItem("h3-studio.voice-mode", engine);
      if (draft.sourceId) localStorage.setItem(DRAFT_KEY, saveLyricsDraft(localStorage.getItem(DRAFT_KEY), draft));
    } catch { /* retain the in-memory draft */ }
  }, [draft, draftReady, engine]);
  const recognize = useCallback(async (id: string) => {
    recognitionAttempts.current.add(id);
    setRecognizing(true); setError("");
    try {
      const task = await transcribeVoiceLyrics(id, engine === "yingsinger" ? "yingsinger" : "soulx");
      refreshRequestRef.current += 1;
      setTasks(current => [task, ...current.filter(item => item.id !== task.id)]);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "歌词识别失败，请重试或手动填写。"); }
    finally { setRecognizing(false); }
  }, [engine]);

  const refreshTasks = useCallback(async (signal?: AbortSignal) => {
    if (tasksReadingRef.current) return;
    tasksReadingRef.current = true;
    const requestNumber = ++refreshRequestRef.current;
    try {
      const items = await listVoiceTasks(signal);
      if (signal?.aborted || requestNumber !== refreshRequestRef.current) return;
      setTasks(items);
      setTasksState("ready");
      setTaskError("");
    } catch (failure) {
      if (signal?.aborted || requestNumber !== refreshRequestRef.current) return;
      setTasksState("error");
      setTaskError(failure instanceof Error ? failure.message : "任务读取失败");
    } finally { tasksReadingRef.current = false; }
  }, []);

  const refreshCapabilities = useCallback(async (signal?: AbortSignal) => {
    try {
      const items = await getVoiceCapabilities(signal);
      if (!signal?.aborted) { setCapabilities(items); setCapabilityState("ready"); }
    } catch {
      if (!signal?.aborted) setCapabilityState("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const initialRefresh = window.setTimeout(() => { void refreshTasks(controller.signal); void refreshCapabilities(controller.signal); }, 0);
    return () => { window.clearTimeout(initialRefresh); controller.abort(); uploadControllerRef.current?.abort(); };
  }, [refreshTasks, refreshCapabilities]);

  useEffect(() => {
    if (capabilityState !== "error") return;
    const controller = new AbortController();
    let busy = false;
    const retry = window.setInterval(() => {
      if (busy) return;
      busy = true;
      void refreshCapabilities(controller.signal).finally(() => { busy = false; });
    }, 5000);
    return () => { window.clearInterval(retry); controller.abort(); };
  }, [capabilityState, refreshCapabilities]);

  useEffect(() => {
    if (tasksState === "ready" && !tasks.some((task) => ["queued", "running", "cancelling"].includes(task.status))) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => { void refreshTasks(controller.signal); }, 2500);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [refreshTasks, tasks, tasksState]);

  const selectedCapability = capabilities.find((item) => item.id === engine);
  const transcription = tasks.find(task => task.operation === "transcribe" && task.sourceAssetId === sourceId);
  const transcriptionBusy = recognizing || Boolean(transcription && ["queued", "running", "cancelling"].includes(transcription.status));
  useEffect(() => {
    if (!transcription) return;
    const timer = window.setTimeout(() => setDraft(current => applyTranscription(current, transcription)), 0);
    return () => window.clearTimeout(timer);
  }, [transcription]);
  useEffect(() => {
    if (!draftReady || !["soulx", "yingsinger"].includes(engine) || !sourceId || tasksState !== "ready" || !selectedCapability?.available || !selectedCapability.transcription || transcription || originalLyrics.trim() || recognitionAttempts.current.has(sourceId) || !audioAssets.some(asset => asset.id === sourceId)) return;
    const timer = window.setTimeout(() => { void recognize(sourceId); }, 0);
    return () => window.clearTimeout(timer);
  }, [draftReady, engine, sourceId, tasksState, selectedCapability, transcription, originalLyrics, recognize, audioAssets]);
  function currentParameters(): YingMusicParameters | undefined {
    if (isRewrite) return validateRewriteParameters(rewriteParameters);
    if (engine !== "yingmusic") return undefined;
    if (!steps.trim() || !cfg.trim() || !seed.trim()) throw new Error("请填写全部歌曲换声参数");
    return validateYingMusicParameters({ diffusion_steps: Number(steps), inference_cfg_rate: Number(cfg), seed: Number(seed) });
  }
  let parametersValid = true;
  try { currentParameters(); } catch { parametersValid = false; }
  const aceValid = engine !== "acestep" || (rewriteParameters.inference_cfg_rate >= 1 && [...lyrics].length <= 4096 && Number.isFinite(coverSettings.audio_cover_strength) && coverSettings.audio_cover_strength >= 0 && coverSettings.audio_cover_strength <= 1 && (audioAssets.find(asset => asset.id === sourceId)?.media.duration ?? 0) >= 10 && (audioAssets.find(asset => asset.id === sourceId)?.media.duration ?? 0) <= 180);
  const canSubmit = (engine !== "yingsinger" || (originalLyrics.trim() && originalLyrics.split("\n").filter(line => line.trim()).length === lyrics.split("\n").filter(line => line.trim()).length)) && aceValid && capabilityState === "ready" && selectedCapability?.available === true && (engine !== "yingmusic" || Boolean(selectedCapability.tuning)) && (!isRewrite || (Boolean(selectedCapability.tuning) && Boolean(lyrics.trim()) && [...lyrics].length <= 10000 && [...originalLyrics].length <= 10000)) && parametersValid && audioAssets.some((asset) => asset.id === sourceId) && (isRewrite && (!rewriteUseReference || engine === "yingsinger") || audioAssets.some((asset) => asset.id === referenceId)) && !uploading && !microphonePending && !submitting && (!isRewrite || !transcriptionBusy);

  async function upload(slot: Slot, file: File): Promise<boolean> {
    if (uploading) return false;
    if (!isSupportedVoiceAudio(file)) { setError("只接受 WAV、FLAC、OGG 或 MP3 音频文件"); return false; }
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    setError(""); setUploading(slot);
    try {
      const asset = await uploadVoiceAudio(file, controller.signal);
      onAssetCreated(asset);
      if (slot === "source") selectSource(asset.id); else setReferenceId(asset.id);
      return true;
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "音频上传失败");
      return false;
    } finally {
      if (uploadControllerRef.current === controller) uploadControllerRef.current = null;
      setUploading(null);
    }
  }

  async function submit(preview = false) {
    if (!canSubmit) return;
    setSubmitting(true); setError("");
    try {
      const task = isRewrite
        ? await submitRewriteTask(sourceId, rewriteUseReference && engine !== "yingsinger" ? referenceId : sourceId, lyrics, originalLyrics, rewriteParameters, preview, engine as "soulx" | "acestep" | "yingsinger", coverSettings)
        : await submitVoiceTask(engine, sourceId, referenceId, currentParameters(), engine === "yingmusic" && selectedCapability?.outputOptions ? { include_stems: includeStems, echo: echoEnabled, reverb: reverbEnabled } : undefined);
      refreshRequestRef.current += 1;
      setTasks((current) => [task, ...current.filter((item) => item.id !== task.id)]);
      setTasksState("ready");
      setTaskError("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "换声提交失败");
    } finally { setSubmitting(false); }
  }

  async function act(task: VoiceTask, action: "cancel" | "delete") {
    if (action === "delete" && !window.confirm("删除这条换声任务及其输出音频？此操作不可恢复。")) return;
    refreshRequestRef.current += 1;
    setActionId(task.id); setError("");
    try {
      if (action === "cancel") {
        const updated = await cancelVoiceTask(task.id);
        refreshRequestRef.current += 1;
        setTasks((current) => current.map((item) => item.id === updated.id ? updated : item));
      } else {
        await deleteVoiceTask(task.id);
        refreshRequestRef.current += 1;
        setTasks((current) => current.filter((item) => item.id !== task.id));
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "任务操作失败");
    } finally { setActionId(""); }
  }

  return <aside id="voice-studio-drawer" className={`rail-drawer voice-drawer${isRewrite ? " lyrics-workspace" : ""}`} aria-label="换声工作区">
    <div className="rail-drawer-header"><div><strong>{engine === "acestep" ? "整曲重创作（实验）" : isRewrite ? "改歌词" : "音频换声"}</strong><small>{engine === "acestep" ? "普通 Cover · 不锁定旋律与伴奏" : isRewrite ? "保留旋律与伴奏 · 编辑新的演唱内容" : "参考音色转换 · 持久任务"}</small></div><button type="button" aria-label="关闭换声抽屉" onClick={onClose}>×</button></div>
    <div className="voice-drawer-content">
      <div className="voice-engine-picker"><label htmlFor="voice-engine">音频处理</label><select id="voice-engine" value={engine} onChange={(event) => { const next = event.target.value as VoiceEngine; setEngine(next); if (next === "yingsinger") setRewriteUseReference(false); if (next === "acestep" || next === "soulx" || next === "yingsinger") setRewriteParameters(next === "acestep" ? { diffusion_steps: 50, inference_cfg_rate: 7, seed: -1 } : next === "yingsinger" ? PHRASE_DEFAULTS : SOULX_DEFAULTS); }}>
        <option value="yingsinger">{ENGINE_LABELS.yingsinger}</option><option value="acestep">{ENGINE_LABELS.acestep}</option><option value="soulx">{ENGINE_LABELS.soulx}</option><option value="vevo2">{ENGINE_LABELS.vevo2}</option><option value="yingmusic">{ENGINE_LABELS.yingmusic}</option>
      </select><p>{engine === "acestep" ? "此模式重新生成整首混音，旋律、伴奏和唱词都可能偏离输入；尚未通过保留原曲精准换词验收。支持 10–180 秒音频。" : isRewrite ? "按原旋律演唱新歌词，保留原伴奏。当前支持中文改词。" : engine === "vevo2" ? "给一段说话或清唱，换成参考音频的音色。" : "分离歌曲人声、换成参考音色，再与原伴奏重混。"}</p>
      {capabilityState === "loading" ? <small>正在检查模型可用性…</small> : capabilityState === "error" ? <small className="voice-unavailable">无法读取模型能力，暂不能提交。</small> : selectedCapability?.available ? <small className="voice-available">模型已就绪</small> : <small className="voice-unavailable">模型未就绪：{selectedCapability?.reason ?? "服务端未提供该引擎"}</small>}
      {engine === "yingmusic" && selectedCapability?.available && !selectedCapability.tuning && <small className="voice-unavailable">服务端尚未提供歌曲参数能力，请更新后再提交。</small>}
      </div>
      {engine === "acestep" && capabilities.some(item => item.id === "yingsinger" && item.available) && <button type="button" onClick={() => { setEngine("yingsinger"); setRewriteUseReference(false); setRewriteParameters(PHRASE_DEFAULTS); }}>改用分句改词 · 保留原伴奏</button>}
      {engine === "yingsinger" && <p className="lyrics-hint">先试听第一句，确认唱词与旋律后再生成全曲。原词、新词须非空且行数相同；数字请写成汉字。</p>}
      {isRewrite && <div className="lyrics-step"><b>1</b><strong>选择歌曲</strong><span>{engine === "acestep" ? "原歌词可选，无需先识别" : "自动识别原歌词"}</span></div>}
      <AudioSlot slot="source" label={isRewrite ? "原歌曲" : "原音频"} selectedId={sourceId} assets={audioAssets} uploading={uploading !== null || microphonePending} onSelect={selectSource} onFile={(file) => void upload("source", file)} onError={setError}/>
      {!isRewrite && <MicrophoneRecorder disabled={uploading !== null || submitting} onPendingChange={setMicrophonePending} onRecorded={(file, destination) => upload(destination, file)}/>}
      {!isRewrite && <AudioSlot slot="reference" label="参考音频" selectedId={referenceId} assets={audioAssets} uploading={uploading !== null} onSelect={setReferenceId} onFile={(file) => void upload("reference", file)} onError={setError}/> }
      {isRewrite && <>
        <section className="lyrics-edit-section" aria-label="歌词对照编辑">
          <div className="lyrics-step"><b>2</b><strong>识别并修改歌词</strong><button type="button" disabled={!sourceId || !selectedCapability?.available || !capabilities.some(item => item.id === (engine === "yingsinger" ? "yingsinger" : "soulx") && item.available && item.transcription) || transcriptionBusy || uploading !== null} onClick={() => void recognize(sourceId)}>{transcriptionBusy ? "正在识别…" : transcription ? "重新识别原词" : "识别原歌词"}</button></div>
          <p className="lyrics-hint">{engine === "acestep" ? "直接在右侧填写完整新歌词；左侧原词仅供对照，不参与 ACE-Step 生成。可选用 SoulX 识别辅助填写。" : engine === "yingsinger" ? "原词与新词逐行对应，一行一个乐句。先校正左侧原词，再试听首句；生成仅替换人声，伴奏沿用分离原轨。当前使用原唱参考，支持 1–180 秒中文音频。" : "原词自动填入右侧，直接修改想替换的句子。识别可能有误，可在左侧校正。"}</p>
          {transcriptionBusy && <div role="status" className="lyrics-recognition-status">{transcription?.status === "queued" ? "歌词识别正在排队，完成后自动填入。" : "正在分离人声并识别歌词，请稍候…"}</div>}
          {transcription?.error && <p role="alert" className="voice-error">{transcription.error}</p>}
          <div className="lyrics-columns">
            <label><span>原歌词 · 可校正</span><textarea aria-label="原歌词" data-i18n-ignore maxLength={10000} rows={8} value={originalLyrics} onChange={event => setOriginalLyrics(event.target.value)} placeholder="选歌后自动识别，也可直接粘贴原词"/></label>
            <label><span>新歌词 · 在这里修改</span><textarea aria-label="新歌词" data-i18n-ignore maxLength={engine === "acestep" ? 4096 : 10000} rows={8} value={lyrics} onChange={event => setLyrics(event.target.value)} placeholder="识别完成后自动填入原词"/></label>
          </div>
          <div className="lyrics-editor-actions"><button type="button" disabled={!originalLyrics.trim()} onClick={() => { if (!draft.edited || window.confirm("用原词替换当前新歌词？已修改的新词会被覆盖。")) setLyrics(originalLyrics); }}>将原词复制到新词</button><small>草稿保存在此浏览器</small></div>
          {(originalLyrics || lyrics) && <div className="lyrics-line-counts" aria-label="逐句字数对照">{lyricLines(originalLyrics, lyrics).map(line => <div key={line.index} className={line.before !== line.after ? "changed" : ""}><span>{line.index + 1}</span><span data-i18n-ignore>{line.original || "—"}</span><span data-i18n-ignore>{line.lyrics || "—"}</span><small>{line.before} → {line.after} 字</small></div>)}</div>}
          <small>每行对应一句，字数越接近原词，节奏通常越自然。</small>
        </section>
        <details className="voice-tuning lyrics-advanced"><summary>高级设置 · 音色与生成参数</summary>
          <label className="voice-effect-option"><input type="checkbox" disabled={engine === "yingsinger"} checked={engine !== "yingsinger" && rewriteUseReference} onChange={event => setRewriteUseReference(event.target.checked)}/>使用其他参考音色</label>
          {!rewriteUseReference && <small>{engine === "acestep" ? "参考原歌曲；不保证完全复刻原唱音色。" : "当前使用原唱音色，无需上传参考音频。"}</small>}
          {rewriteUseReference && engine !== "yingsinger" && <AudioSlot slot="reference" label="参考音频" selectedId={referenceId} assets={audioAssets} uploading={uploading !== null} onSelect={setReferenceId} onFile={file => void upload("reference", file)} onError={setError}/>}
          {engine === "acestep" && <div className="voice-tuning-fields"><label>曲风描述（可选）<input maxLength={512} value={coverSettings.caption} onChange={event => setCoverSettings(value => ({ ...value, caption: event.target.value }))}/></label><label>原曲参考强度<input type="number" min="0" max="1" step="0.05" value={coverSettings.audio_cover_strength} onChange={event => setCoverSettings(value => ({ ...value, audio_cover_strength: Number(event.target.value) }))}/><small>0–1；降低后会减少原曲约束，可能变成另一首曲子，不是只降低原唱音量。</small></label></div>}
          <div className="voice-tuning-fields">
            <label>采样步数<input type="number" min="16" max="100" value={rewriteParameters.diffusion_steps} onChange={event => setRewriteParameters(value => ({ ...value, diffusion_steps: Number(event.target.value) }))}/></label>
            <label>引导强度<input type="number" min={engine === "acestep" ? 1 : 0} max="10" step="0.1" value={rewriteParameters.inference_cfg_rate} onChange={event => setRewriteParameters(value => ({ ...value, inference_cfg_rate: Number(event.target.value) }))}/></label>
            {engine === "acestep" && <small role="note">引导强度 1–10：1 关闭 CFG，大于 1 才启用；先用 7。它不能锁住原曲旋律。{rewriteParameters.inference_cfg_rate < 1 && " 当前值低于 1，请改为 7 后再提交。"}</small>}
            <label>随机种子<input type="number" min="-1" max="4294967295" value={rewriteParameters.seed} onChange={event => setRewriteParameters(value => ({ ...value, seed: Number(event.target.value) }))}/></label>
          </div>
        </details>
        <div className="lyrics-step"><b>3</b><strong>试听与生成</strong><span>{engine === "acestep" ? "生成后对比原曲与重唱结果" : "保留原伴奏，输出三轨"}</span></div>
      </>}
      {engine === "yingmusic" && selectedCapability?.tuning && <section className="voice-tuning" aria-label="歌曲换声参数">
        <strong>歌曲换声参数</strong>
        <div className="voice-tuning-fields">
          <label>采样步数<input type="number" min="10" max="200" step="1" value={steps} onChange={(event) => setSteps(event.target.value)}/></label>
          <div className="voice-tuning-field">
            <div className="voice-tuning-label"><label htmlFor="voice-guidance">引导强度</label><span className="voice-parameter-help">
              <button type="button" aria-label="引导强度说明" aria-describedby="voice-guidance-help">?</button>
              <span id="voice-guidance-help" role="tooltip">{GUIDANCE_HELP}</span>
            </span></div>
            <input id="voice-guidance" type="number" min="0" max="2" step="0.05" value={cfg} aria-describedby="voice-guidance-help" onChange={(event) => setCfg(event.target.value)}/>
          </div>
          <label>随机种子<input type="number" min="-1" max="4294967295" step="1" value={seed} onChange={(event) => setSeed(event.target.value)}/></label>
        </div>
        <small>官方流程推荐 100 步作为质量与速度折中，并非所有歌曲的最优值。-1 表示每次随机；任务中会记录实际种子。改用记录的种子可复现抽卡条件，GPU 运行仍可能有微小差异。</small>
      </section>}
      {engine === "yingmusic" && selectedCapability?.outputOptions && <section className="voice-tuning" aria-label="歌曲输出与效果">
        <strong>输出与效果</strong>
        <label className="voice-effect-option"><input type="checkbox" checked={includeStems} onChange={(event) => setIncludeStems(event.target.checked)}/>保留换声干声与原伴奏，供试听和导出</label>
        <label className="voice-effect-option"><input type="checkbox" checked={echoEnabled} onChange={(event) => setEchoEnabled(event.target.checked)}/>最终混音加入回声</label>
        <label className="voice-effect-option"><input type="checkbox" checked={reverbEnabled} onChange={(event) => setReverbEnabled(event.target.checked)}/>最终混音加入混响</label>
        <small>效果开关只作用于最终混音；干声与伴奏保持原样。旧任务仍可试听和下载原有成品。</small>
      </section>}
      <p className="voice-format-note">支持 WAV、FLAC、OGG、MP3；服务端会校验真实格式。结果输出 WAV。</p>
      {error && <div className="voice-error" role="alert">{error}</div>}
      {(engine === "soulx" || engine === "yingsinger") && <button className="voice-preview" type="button" disabled={!canSubmit || !selectedCapability?.rewritePreview} onClick={() => void submit(true)}>先试听第一段</button>}
      <button className="voice-submit" type="button" disabled={!canSubmit} onClick={() => void submit()}>{submitting ? "提交中…" : isRewrite ? (engine === "acestep" ? "生成整曲重创作（实验）" : "生成完整改词歌曲") : "开始换声"}</button>
      <section className="voice-history" aria-label="换声任务历史"><div className="voice-history-heading"><strong>{isRewrite ? "改词与试听记录" : "换声任务"}</strong><button type="button" onClick={() => { void refreshTasks(); void refreshCapabilities(); }}>刷新</button></div>
        {tasksState === "loading" && <p>正在读取任务…</p>}
        {tasksState === "error" && <p>{taskError || "任务读取失败"} 正在自动重试。</p>}
        {tasksState === "ready" && tasks.length === 0 && <p>还没有换声任务。</p>}
        {tasks.filter(task => isRewrite ? ["soulx", "acestep", "yingsinger"].includes(task.engine) : !["soulx", "acestep", "yingsinger"].includes(task.engine)).map((task) => <article className={`voice-task status-${task.status}`} key={task.id}>
          <div className="voice-task-top"><strong>{task.operation === "transcribe" ? "原歌词识别" : task.preview ? "改词试听 · 第一段" : ENGINE_LABELS[task.engine]}</strong><span>{STATUS_LABELS[task.status]}</span></div>
          <small title={task.id}>{taskLabel(task.sourceAssetId, audioAssets)} → {taskLabel(task.referenceAssetId, audioAssets)}</small>
          {task.detectedLyrics && <details className="voice-task-lyrics"><summary>查看识别歌词</summary><p data-i18n-ignore>{task.detectedLyrics}</p></details>}
          {task.lyrics && <details className="voice-task-lyrics"><summary>查看新歌词</summary><p data-i18n-ignore>{task.lyrics}</p><button type="button" onClick={() => { setEngine(task.engine); if (task.coverSettings) setCoverSettings(task.coverSettings); setSourceId(task.sourceAssetId); setReferenceId(task.referenceAssetId); setRewriteUseReference(task.referenceAssetId !== task.sourceAssetId); setDraft({ sourceId: task.sourceAssetId, original: task.originalLyrics ?? "", lyrics: task.lyrics ?? "", edited: true, originalEdited: true, recognizedTaskId: "" }); if (task.parameters) setRewriteParameters(task.parameters); }}>复用改词设置</button></details>}
          {task.parameters && <small className="voice-task-parameters">{task.parameters.diffusion_steps} <span>步</span> · <span>引导</span> {task.parameters.inference_cfg_rate} · <span>种子</span> {task.parameters.seed} <button type="button" onClick={() => { if ((task.engine === "soulx" || task.engine === "acestep" || task.engine === "yingsinger") && task.parameters) { setEngine(task.engine); if (task.coverSettings) setCoverSettings(task.coverSettings); setRewriteParameters(task.parameters); return; } setEngine("yingmusic"); setSteps(String(task.parameters?.diffusion_steps)); setCfg(String(task.parameters?.inference_cfg_rate)); setSeed(String(task.parameters?.seed)); }}>复用参数</button></small>}
          {task.engine !== "acestep" && task.outputOptions && <small className="voice-task-parameters">{task.outputOptions.include_stems ? "保留分轨" : "仅最终混音"} · 回声{task.outputOptions.echo ? "开" : "关"} · 混响{task.outputOptions.reverb ? "开" : "关"}</small>}
          <div className="voice-task-progress"><progress max="100" value={task.progress} aria-label="换声进度"/><span>{Math.round(task.progress)}%</span></div>
          <p><span>{STAGE_LABELS[task.stage] ?? task.stage}</span>{task.status === "queued" && typeof task.queuePosition === "number" && <> · <span>{`队列第 ${task.queuePosition} 位`}</span></>}{task.queueReason && <> · <span>{QUEUE_REASONS[task.queueReason] ?? task.queueReason}</span></>}</p>
          {task.error && <p className="voice-task-error">{task.error}</p>}
          {task.engine === "acestep" && task.status === "completed" && <p className="lyrics-hint">音频文件已生成；歌词与旋律未经效果验收，请对比原曲试听。</p>}
          {task.status === "completed" && <VoiceTaskResult task={task} source={audioAssets.find(asset => asset.id === task.sourceAssetId)} onUpdated={updated => { refreshRequestRef.current += 1; setTasks(current => current.map(item => item.id === updated.id ? updated : item)); }}/>}
          <div className="voice-task-actions">{["queued", "running", "cancelling"].includes(task.status) ? <button type="button" disabled={Boolean(actionId) || task.status === "cancelling"} onClick={() => void act(task, "cancel")}>取消任务</button> : <button type="button" disabled={Boolean(actionId)} onClick={() => void act(task, "delete")}>删除记录</button>}</div>
        </article>)}
      </section>
    </div>
  </aside>;
}
