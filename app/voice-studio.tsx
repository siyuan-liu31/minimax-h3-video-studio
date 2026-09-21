"use client";

/* Audio preview is optional and bytes load only after a user presses play. */
/* eslint-disable jsx-a11y/media-has-caption */

import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import type { LibraryAsset } from "./studio-library";
import MicrophoneRecorder from "./microphone-recorder";
import {
  cancelVoiceTask, deleteVoiceTask, getVoiceCapabilities, isSupportedVoiceAudio,
  listVoiceTasks, submitVoiceTask, uploadVoiceAudio, voiceDownloadUrl, voicePreviewUrl,
  validateYingMusicParameters, YINGMUSIC_DEFAULTS, YINGMUSIC_OUTPUT_DEFAULTS,
  type VoiceCapability, type VoiceEngine, type VoiceTask, type VoiceTrack, type YingMusicParameters,
} from "./voice-studio-api";

type Slot = "source" | "reference";
type Props = { assets: LibraryAsset[]; onAssetCreated: (asset: LibraryAsset) => void; onClose: () => void };

const ENGINE_LABELS: Record<VoiceEngine, string> = { vevo2: "Vevo2 · 语音/清唱换音色", yingmusic: "YingMusic-SVC · 完整歌曲换音色" };
const STATUS_LABELS: Record<VoiceTask["status"], string> = {
  queued: "排队中", running: "运行中", cancelling: "取消中", completed: "已完成", failed: "失败", canceled: "已取消",
};
const STAGE_LABELS: Record<string, string> = {
  waiting_for_gpu: "等待 GPU", loading_model: "加载模型", inference: "正在换声", completed: "已完成",
  cancelling: "取消中", canceled: "已取消", failed: "失败", interrupted: "已中断",
};
const QUEUE_REASONS: Record<string, string> = {
  waiting_for_comfy_task: "等待视频/图像任务完成",
  waiting_for_voice_task: "等待另一换声任务完成",
  waiting_for_model_release: "等待驻留模型释放",
  waiting_for_gpu: "等待 GPU",
};
const TRACK_LABELS: Record<VoiceTrack, string> = { mix: "最终混音", dry_vocal: "换声干声", accompaniment: "原伴奏" };

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

function VoiceTaskResult({ task }: { task: VoiceTask }) {
  const [track, setTrack] = useState<VoiceTrack>("mix");
  const available = (["mix", "dry_vocal", "accompaniment"] as VoiceTrack[]).filter((item) => item === "mix" ? Boolean(task.outputs?.mix ?? task.output) : Boolean(task.outputs?.[item]));
  if (available.length === 0) return null;
  const selected = available.includes(track) ? track : "mix";
  return <div className="voice-task-result">
    {available.length > 1 && <label className="voice-track-select">试听与导出音轨<select aria-label="试听与导出音轨" value={selected} onChange={(event) => setTrack(event.target.value as VoiceTrack)}>
      {available.map((item) => <option key={item} value={item}>{TRACK_LABELS[item]}</option>)}
    </select></label>}
    <audio key={`${task.id}:${selected}`} controls preload="none" src={voicePreviewUrl(task.id, selected)} aria-label={`${TRACK_LABELS[selected]}试听`}/>
    <a href={voiceDownloadUrl(task.id, selected)} download={`voice-${task.id.slice(0, 8)}-${selected}.wav`}>导出{TRACK_LABELS[selected]} WAV</a>
    <small>试听与导出使用同一份音轨文件。</small>
  </div>;
}

export default function VoiceStudio({ assets, onAssetCreated, onClose }: Props) {
  const audioAssets = useMemo(() => assets.filter((asset) => asset.kind === "audio"), [assets]);
  const [engine, setEngine] = useState<VoiceEngine>("vevo2");
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
  const [steps, setSteps] = useState(String(YINGMUSIC_DEFAULTS.diffusion_steps));
  const [cfg, setCfg] = useState(String(YINGMUSIC_DEFAULTS.inference_cfg_rate));
  const [seed, setSeed] = useState(String(YINGMUSIC_DEFAULTS.seed));
  const [includeStems, setIncludeStems] = useState(YINGMUSIC_OUTPUT_DEFAULTS.include_stems);
  const [echoEnabled, setEchoEnabled] = useState(YINGMUSIC_OUTPUT_DEFAULTS.echo);
  const [reverbEnabled, setReverbEnabled] = useState(YINGMUSIC_OUTPUT_DEFAULTS.reverb);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const refreshRequestRef = useRef(0);

  const refreshTasks = useCallback(async (signal?: AbortSignal) => {
    const requestNumber = ++refreshRequestRef.current;
    try {
      const items = await listVoiceTasks(signal);
      if (signal?.aborted || requestNumber !== refreshRequestRef.current) return;
      setTasks(items);
      setTasksState("ready");
    } catch (failure) {
      if (signal?.aborted || requestNumber !== refreshRequestRef.current) return;
      setTasksState("error");
      setError(failure instanceof Error ? failure.message : "任务读取失败");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getVoiceCapabilities(controller.signal).then((items) => {
      if (!controller.signal.aborted) { setCapabilities(items); setCapabilityState("ready"); }
    }).catch((failure) => {
      if (!controller.signal.aborted) { setCapabilityState("error"); setError(failure instanceof Error ? failure.message : "能力读取失败"); }
    });
    const initialRefresh = window.setTimeout(() => { void refreshTasks(controller.signal); }, 0);
    return () => { window.clearTimeout(initialRefresh); controller.abort(); uploadControllerRef.current?.abort(); };
  }, [refreshTasks]);

  useEffect(() => {
    if (!tasks.some((task) => ["queued", "running", "cancelling"].includes(task.status))) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => { void refreshTasks(controller.signal); }, 2500);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [refreshTasks, tasks]);

  const selectedCapability = capabilities.find((item) => item.id === engine);
  function currentParameters(): YingMusicParameters | undefined {
    if (engine !== "yingmusic") return undefined;
    if (!steps.trim() || !cfg.trim() || !seed.trim()) throw new Error("请填写全部歌曲换声参数");
    return validateYingMusicParameters({ diffusion_steps: Number(steps), inference_cfg_rate: Number(cfg), seed: Number(seed) });
  }
  let parametersValid = true;
  try { currentParameters(); } catch { parametersValid = false; }
  const canSubmit = capabilityState === "ready" && selectedCapability?.available === true && (engine !== "yingmusic" || Boolean(selectedCapability.tuning)) && parametersValid && audioAssets.some((asset) => asset.id === sourceId) && audioAssets.some((asset) => asset.id === referenceId) && !uploading && !microphonePending && !submitting;

  async function upload(slot: Slot, file: File): Promise<boolean> {
    if (uploading) return false;
    if (!isSupportedVoiceAudio(file)) { setError("只接受 WAV、FLAC、OGG 或 MP3 音频文件"); return false; }
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    setError(""); setUploading(slot);
    try {
      const asset = await uploadVoiceAudio(file, controller.signal);
      onAssetCreated(asset);
      if (slot === "source") setSourceId(asset.id); else setReferenceId(asset.id);
      return true;
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "音频上传失败");
      return false;
    } finally {
      if (uploadControllerRef.current === controller) uploadControllerRef.current = null;
      setUploading(null);
    }
  }

  async function submit() {
    if (!canSubmit) return;
    setSubmitting(true); setError("");
    try {
      const task = await submitVoiceTask(engine, sourceId, referenceId, currentParameters(), engine === "yingmusic" && selectedCapability?.outputOptions ? { include_stems: includeStems, echo: echoEnabled, reverb: reverbEnabled } : undefined);
      refreshRequestRef.current += 1;
      setTasks((current) => [task, ...current.filter((item) => item.id !== task.id)]);
      setTasksState("ready");
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

  return <aside id="voice-studio-drawer" className="rail-drawer voice-drawer" aria-label="换声工作区">
    <div className="rail-drawer-header"><div><strong>音频换声</strong><small>参考音色转换 · 持久任务</small></div><button type="button" aria-label="关闭换声抽屉" onClick={onClose}>×</button></div>
    <div className="voice-drawer-content">
      <div className="voice-engine-picker"><label htmlFor="voice-engine">换声模式</label><select id="voice-engine" value={engine} onChange={(event) => setEngine(event.target.value as VoiceEngine)}>
        <option value="vevo2">{ENGINE_LABELS.vevo2}</option><option value="yingmusic">{ENGINE_LABELS.yingmusic}</option>
      </select><p>{engine === "vevo2" ? "给一段说话或清唱，换成参考音频的音色。" : "分离歌曲人声、换成参考音色，再与原伴奏重混。"}</p>
      {capabilityState === "loading" ? <small>正在检查模型可用性…</small> : capabilityState === "error" ? <small className="voice-unavailable">无法读取模型能力，暂不能提交。</small> : selectedCapability?.available ? <small className="voice-available">模型已就绪</small> : <small className="voice-unavailable">模型未就绪：{selectedCapability?.reason ?? "服务端未提供该引擎"}</small>}
      {engine === "yingmusic" && selectedCapability?.available && !selectedCapability.tuning && <small className="voice-unavailable">服务端尚未提供歌曲参数能力，请更新后再提交。</small>}
      </div>
      <AudioSlot slot="source" label="原音频" selectedId={sourceId} assets={audioAssets} uploading={uploading !== null || microphonePending} onSelect={setSourceId} onFile={(file) => void upload("source", file)} onError={setError}/>
      <MicrophoneRecorder disabled={uploading !== null || submitting} onPendingChange={setMicrophonePending} onRecorded={(file, destination) => upload(destination, file)}/>
      <AudioSlot slot="reference" label="参考音频" selectedId={referenceId} assets={audioAssets} uploading={uploading !== null} onSelect={setReferenceId} onFile={(file) => void upload("reference", file)} onError={setError}/>
      {engine === "yingmusic" && selectedCapability?.tuning && <section className="voice-tuning" aria-label="歌曲换声参数">
        <strong>歌曲换声参数</strong>
        <div className="voice-tuning-fields">
          <label>采样步数<input type="number" min="10" max="200" step="1" value={steps} onChange={(event) => setSteps(event.target.value)}/></label>
          <label>引导强度<input type="number" min="0" max="2" step="0.05" value={cfg} onChange={(event) => setCfg(event.target.value)}/></label>
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
      <button className="voice-submit" type="button" disabled={!canSubmit} onClick={() => void submit()}>{submitting ? "提交中…" : "开始换声"}</button>
      <section className="voice-history" aria-label="换声任务历史"><div className="voice-history-heading"><strong>换声任务</strong><button type="button" onClick={() => void refreshTasks()}>刷新</button></div>
        {tasksState === "loading" && <p>正在读取任务…</p>}
        {tasksState === "error" && <p>任务读取失败，点击刷新重试。</p>}
        {tasksState === "ready" && tasks.length === 0 && <p>还没有换声任务。</p>}
        {tasks.map((task) => <article className={`voice-task status-${task.status}`} key={task.id}>
          <div className="voice-task-top"><strong>{ENGINE_LABELS[task.engine]}</strong><span>{STATUS_LABELS[task.status]}</span></div>
          <small title={task.id}>{taskLabel(task.sourceAssetId, audioAssets)} → {taskLabel(task.referenceAssetId, audioAssets)}</small>
          {task.parameters && <small className="voice-task-parameters">{task.parameters.diffusion_steps} <span>步</span> · <span>引导</span> {task.parameters.inference_cfg_rate} · <span>种子</span> {task.parameters.seed} <button type="button" onClick={() => { setEngine("yingmusic"); setSteps(String(task.parameters?.diffusion_steps)); setCfg(String(task.parameters?.inference_cfg_rate)); setSeed(String(task.parameters?.seed)); }}>复用参数</button></small>}
          {task.outputOptions && <small className="voice-task-parameters">{task.outputOptions.include_stems ? "保留分轨" : "仅最终混音"} · 回声{task.outputOptions.echo ? "开" : "关"} · 混响{task.outputOptions.reverb ? "开" : "关"}</small>}
          <div className="voice-task-progress"><progress max="100" value={task.progress} aria-label="换声进度"/><span>{Math.round(task.progress)}%</span></div>
          <p><span>{STAGE_LABELS[task.stage] ?? task.stage}</span>{task.status === "queued" && typeof task.queuePosition === "number" && <> · <span>{`队列第 ${task.queuePosition} 位`}</span></>}{task.queueReason && <> · <span>{QUEUE_REASONS[task.queueReason] ?? task.queueReason}</span></>}</p>
          {task.error && <p className="voice-task-error">{task.error}</p>}
          {task.status === "completed" && <VoiceTaskResult task={task}/>}
          <div className="voice-task-actions">{["queued", "running", "cancelling"].includes(task.status) ? <button type="button" disabled={Boolean(actionId) || task.status === "cancelling"} onClick={() => void act(task, "cancel")}>取消任务</button> : <button type="button" disabled={Boolean(actionId)} onClick={() => void act(task, "delete")}>删除记录</button>}</div>
        </article>)}
      </section>
    </div>
  </aside>;
}
