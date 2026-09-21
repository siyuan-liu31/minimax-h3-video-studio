"use client";

/* The local preview is user-controlled; no recording is uploaded on stop. */
/* eslint-disable jsx-a11y/media-has-caption */

import { useCallback, useEffect, useRef, useState } from "react";
import { MAX_MICROPHONE_SECONDS, recordedMicrophoneFile } from "./microphone-audio";

type Phase = "idle" | "requesting" | "recording" | "processing" | "discarding" | "ready" | "uploading";
export type RecordingDestination = "source" | "reference";
type Props = {
  disabled: boolean;
  onPendingChange: (pending: boolean) => void;
  onRecorded: (file: File, destination: RecordingDestination) => Promise<boolean>;
};

function stopStream(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}

function microphoneError(failure: unknown): string {
  const name = failure instanceof Error ? failure.name : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") return "未获得话筒权限，请在浏览器中允许使用话筒";
  if (name === "NotFoundError" || name === "DevicesNotFoundError") return "未找到可用话筒，请检查设备连接";
  if (name === "NotReadableError" || name === "TrackStartError") return "话筒被其他程序占用，请关闭后重试";
  return failure instanceof Error ? failure.message : "无法开始录音";
}

export default function MicrophoneRecorder({ disabled, onPendingChange, onRecorded }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [recordedFile, setRecordedFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [error, setError] = useState("");
  const [destination, setDestination] = useState<RecordingDestination>("source");
  const mountedRef = useRef(false);
  const canceledRequestRef = useRef(false);
  const discardRef = useRef(false);
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const previewUrlRef = useRef("");
  const startedAtRef = useRef(0);
  const elapsedTimerRef = useRef<number | null>(null);
  const limitTimerRef = useRef<number | null>(null);

  const clearTimers = useCallback(() => {
    if (elapsedTimerRef.current !== null) window.clearInterval(elapsedTimerRef.current);
    if (limitTimerRef.current !== null) window.clearTimeout(limitTimerRef.current);
    elapsedTimerRef.current = null;
    limitTimerRef.current = null;
  }, []);

  const clearPreview = useCallback(() => {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = "";
    setPreviewUrl("");
    setRecordedFile(null);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      canceledRequestRef.current = true;
      discardRef.current = true;
      clearTimers();
      const recorder = recorderRef.current;
      if (recorder) {
        recorder.onstop = null;
        recorder.onerror = null;
        recorder.ondataavailable = null;
        try { if (recorder.state !== "inactive") recorder.stop(); } catch { /* Tracks are still released below. */ }
      }
      stopStream(streamRef.current);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    };
  }, [clearTimers]);

  function stopRecording(discard: boolean) {
    discardRef.current = discard;
    clearTimers();
    setSeconds(Math.min(MAX_MICROPHONE_SECONDS, Math.floor((Date.now() - startedAtRef.current) / 1000)));
    const recorder = recorderRef.current;
    let stopped = false;
    try {
      if (recorder?.state === "recording") { recorder.stop(); stopped = true; }
    } catch { /* Release the tracks and report failure below. */ }
    stopStream(streamRef.current);
    streamRef.current = null;
    if (stopped) { setPhase(discard ? "discarding" : "processing"); return; }
    if (recorder) { recorder.onstop = null; recorder.onerror = null; recorder.ondataavailable = null; }
    recorderRef.current = null;
    chunksRef.current = [];
    setError("录音中断，请重试");
    setPhase("idle");
    onPendingChange(false);
  }

  async function startRecording() {
    if (disabled || (phase !== "idle" && phase !== "ready")) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined" || typeof AudioContext === "undefined") {
      setError("当前浏览器无法录音；请使用 HTTPS 或 localhost，并检查浏览器话筒支持");
      return;
    }
    const hadPreview = phase === "ready";
    canceledRequestRef.current = false;
    discardRef.current = false;
    setError("");
    setPhase("requesting");
    onPendingChange(true);
    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (!mountedRef.current || canceledRequestRef.current) {
        stopStream(stream);
        return;
      }
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      streamRef.current = stream;
      chunksRef.current = [];
      recorder.ondataavailable = (event) => { if (event.data.size > 0) chunksRef.current.push(event.data); };
      recorder.onerror = () => {
        discardRef.current = true;
        clearTimers();
        recorder.onstop = null;
        recorder.onerror = null;
        recorder.ondataavailable = null;
        try { if (recorder.state === "recording") recorder.stop(); } catch { /* Tracks are still released below. */ }
        stopStream(streamRef.current);
        streamRef.current = null;
        recorderRef.current = null;
        chunksRef.current = [];
        if (mountedRef.current) {
          setError("录音中断，请重试");
          setPhase("idle");
          onPendingChange(false);
        }
      };
      recorder.onstop = () => {
        clearTimers();
        stopStream(streamRef.current);
        streamRef.current = null;
        recorderRef.current = null;
        const recorded = new Blob(chunksRef.current, { type: recorder.mimeType });
        chunksRef.current = [];
        if (!mountedRef.current) return;
        if (discardRef.current) {
          setPhase("idle");
          onPendingChange(false);
          return;
        }
        void recordedMicrophoneFile(recorded).then((file) => {
          if (!mountedRef.current) return;
          clearPreview();
          const url = URL.createObjectURL(file);
          previewUrlRef.current = url;
          setPreviewUrl(url);
          setRecordedFile(file);
          setPhase("ready");
        }).catch((failure) => {
          if (!mountedRef.current) return;
          setError(microphoneError(failure));
          setPhase("idle");
          onPendingChange(false);
        });
      };
      recorder.start(1000);
      clearPreview();
      startedAtRef.current = Date.now();
      setSeconds(0);
      setPhase("recording");
      elapsedTimerRef.current = window.setInterval(() => {
        setSeconds(Math.min(MAX_MICROPHONE_SECONDS, Math.floor((Date.now() - startedAtRef.current) / 1000)));
      }, 500);
      limitTimerRef.current = window.setTimeout(() => { stopRecording(false); }, MAX_MICROPHONE_SECONDS * 1000);
    } catch (failure) {
      discardRef.current = true;
      const recorder = recorderRef.current;
      if (recorder) { recorder.onstop = null; recorder.onerror = null; recorder.ondataavailable = null; }
      try { if (recorder?.state === "recording") recorder.stop(); } catch { /* Tracks are still released below. */ }
      clearTimers();
      stopStream(stream);
      streamRef.current = null;
      recorderRef.current = null;
      chunksRef.current = [];
      if (!mountedRef.current || canceledRequestRef.current) return;
      setError(microphoneError(failure));
      setPhase(hadPreview ? "ready" : "idle");
      onPendingChange(hadPreview);
    }
  }

  function discard() {
    if (phase === "requesting") {
      canceledRequestRef.current = true;
      setPhase(recordedFile ? "ready" : "idle");
      onPendingChange(Boolean(recordedFile));
    } else if (phase === "recording") {
      stopRecording(true);
    } else if (phase === "ready") {
      clearPreview();
      setPhase("idle");
      onPendingChange(false);
    }
    setError("");
  }

  async function acceptRecording() {
    if (!recordedFile || phase !== "ready" || disabled) return;
    setPhase("uploading");
    try {
      if (await onRecorded(recordedFile, destination)) {
        if (!mountedRef.current) return;
        clearPreview();
        setPhase("idle");
        onPendingChange(false);
      } else if (mountedRef.current) {
        setPhase("ready");
      }
    } catch (failure) {
      if (!mountedRef.current) return;
      setError(microphoneError(failure));
      setPhase("ready");
    }
  }

  const duration = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  return <section className="voice-microphone" aria-label="话筒录音">
    <div className="voice-microphone-heading"><strong>话筒录音</strong><small>录完试听，选择用途后上传</small></div>
    {phase === "idle" && <button type="button" disabled={disabled} onClick={() => void startRecording()}>开始录音</button>}
    {phase === "requesting" && <div className="voice-microphone-actions"><span>正在请求话筒权限…</span><button type="button" onClick={discard}>取消录音</button></div>}
    {phase === "recording" && <div className="voice-microphone-actions"><span role="status">录音中 {duration}</span><button type="button" onClick={() => stopRecording(false)}>停止录音</button><button type="button" onClick={discard}>丢弃录音</button></div>}
    {phase === "processing" && <span role="status">正在整理录音…</span>}
    {phase === "discarding" && <span role="status">正在丢弃录音…</span>}
    {(phase === "ready" || phase === "uploading") && recordedFile && <>
      <audio controls preload="none" src={previewUrl} aria-label="试听本地录音"/>
      <fieldset className="voice-microphone-destination" disabled={disabled || phase === "uploading"}><legend>这段录音用作</legend>
        <label><input type="radio" name="voice-recording-destination" value="source" checked={destination === "source"} onChange={() => setDestination("source")}/>原音频</label>
        <label><input type="radio" name="voice-recording-destination" value="reference" checked={destination === "reference"} onChange={() => setDestination("reference")}/>参考音频</label>
      </fieldset>
      <div className="voice-microphone-actions"><span>{duration}</span><button type="button" disabled={disabled || phase === "uploading"} onClick={() => void acceptRecording()}>{phase === "uploading" ? "正在上传录音…" : "使用这段录音"}</button><button type="button" disabled={phase === "uploading"} onClick={discard}>丢弃录音</button></div>
    </>}
    <small>最长 5 分钟。仅在点击“使用这段录音”后上传；离开面板会关闭话筒并丢弃未上传录音。</small>
    {error && <p className="voice-microphone-error" role="alert">{error}</p>}
  </section>;
}
