"use client";
/* eslint-disable jsx-a11y/media-has-caption */
import { useEffect, useRef, useState } from "react";
import type { LibraryAsset } from "./studio-library";
import { douyinActive, douyinRequest, DouyinApiError, listDouyinTasks, parseDouyinTask, type DouyinCapabilities, type DouyinTask } from "./douyin-api";
import { explainDouyinFailure, type DouyinFailure } from "./douyin-feedback";
import { connectDouyinBridge, DouyinBridgeError, importDouyinViaBridge, inspectDouyinViaBridge } from "./douyin-local-bridge";

type Props = {onClose: () => void; onAssetCreated: (asset: LibraryAsset) => void; onReplicate: (asset: LibraryAsset) => void};
const LABELS: Record<string, string> = {queued: "排队中", parsing: "解析中", downloading: "下载中", importing: "导入资产中", completed: "已完成", failed: "失败", canceled: "已取消", cancelling: "取消中"};
const RETRY_LATER = new Set(["cookie_refresh_required", "access_restricted", "rate_limited"]);
const LOCAL_ALTERNATIVE = new Set(["cookie_refresh_required", "access_restricted"]);
function failureFromError(caught: unknown, source: DouyinFailure["source"], stage: string): DouyinFailure {
  return {
    code: caught instanceof DouyinBridgeError || caught instanceof DouyinApiError ? caught.code : undefined,
    message: caught instanceof Error ? caught.message : "抖音请求失败",
    stage: caught instanceof DouyinBridgeError ? caught.stage : stage,
    source,
  };
}
function FailureNotice({failure}: {failure: DouyinFailure}) {
  const feedback = explainDouyinFailure(failure);
  return <div className="douyin-failure" role="alert">
    <strong>{feedback.title}</strong>
    <p>{feedback.detail}</p>
    <p><b>下一步：</b>{feedback.nextStep}</p>
    <small>诊断：{feedback.diagnostic}</small>
  </div>;
}
export default function DouyinStudio({onClose, onAssetCreated, onReplicate}: Props) {
  const [text, setText] = useState("");
  const [quality, setQuality] = useState("best");
  const [tasks, setTasks] = useState<DouyinTask[]>([]);
  const [capability, setCapability] = useState<DouyinCapabilities>();
  const [error, setError] = useState<DouyinFailure>();
  const [pollError, setPollError] = useState<DouyinFailure>();
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState("");
  const [bridgeToken, setBridgeToken] = useState("");
  const [bridgeIssue, setBridgeIssue] = useState("");
  const [localStatus, setLocalStatus] = useState("");
  const [localAsset, setLocalAsset] = useState<LibraryAsset>();
  const [localMetadata, setLocalMetadata] = useState<{title?: string; uploader?: string; duration?: number}>();
  const notified = useRef(new Set<string>());
  const assetCallback = useRef(onAssetCreated);
  useEffect(() => { assetCallback.current = onAssetCreated; }, [onAssetCreated]);
  useEffect(() => {
    let stopped = false;
    const connect = () => { void connectDouyinBridge().then((token) => { if (!stopped) { setBridgeToken(token); setBridgeIssue(""); } }).catch((caught) => { if (!stopped) { setBridgeToken(""); setBridgeIssue(caught instanceof Error ? caught.message : "本机导入不可用"); } }); };
    connect();
    const timer = setInterval(connect, 10_000);
    return () => { stopped = true; clearInterval(timer); };
  }, []);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const [items, cap] = await Promise.all([listDouyinTasks(), douyinRequest("capabilities")]);
        if (stopped) return;
        setTasks(items); setCapability(cap as DouyinCapabilities); setPollError(undefined);
        for (const task of items) if (task.asset && !notified.current.has(task.id)) {
          notified.current.add(task.id); assetCallback.current(task.asset);
        }
      } catch (caught) { if (!stopped) setPollError(failureFromError(caught, "server", "status")); }
      if (!stopped) timer = setTimeout(refresh, 2000);
    };
    void refresh();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);
  async function action(path: string, body: unknown) {
    setBusy(true); setError(undefined);
    try {
      const task = parseDouyinTask(await douyinRequest(path, body));
      setTasks((items) => [task, ...items.filter((item) => item.id !== task.id)]);
    } catch (caught) {
      const stage = path.endsWith("/retry") ? "retry" : path.endsWith("/cancel") ? "cancel" : (body as {mode?: string})?.mode === "parse" ? "parse" : "download";
      setError(failureFromError(caught, "server", stage));
    }
    finally { setBusy(false); }
  }
  const submit = (mode: "parse" | "download", url = text) => action("tasks", {text: url, mode, quality, request_id: crypto.randomUUID().replaceAll("-", "")});
  async function inspectVideo() {
    if (!bridgeToken) { await submit("parse"); return; }
    setBusy(true); setError(undefined); setLocalStatus(""); setLocalMetadata(undefined);
    try { setLocalMetadata(await inspectDouyinViaBridge(text, bridgeToken)); }
    catch (caught) { setError(failureFromError(caught, "local", "parse")); }
    finally { setBusy(false); }
  }
  async function importVideo(url = text) {
    if (!bridgeToken) { await submit("download", url); return; }
    setBusy(true); setError(undefined); setLocalStatus(""); setLocalAsset(undefined);
    try {
      const asset = await importDouyinViaBridge(url, bridgeToken, setLocalStatus);
      setLocalAsset(asset);
      onAssetCreated(asset);
    } catch (caught) { setLocalStatus(""); setError(failureFromError(caught, "local", "import")); }
    finally { setBusy(false); }
  }
  return <aside className="douyin-drawer" aria-label="抖音素材" id="douyin-studio-drawer">
    <header><div><h2>抖音素材</h2><p>粘贴分享链接，下载并保存到资产库。</p></div><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
    <label>抖音链接或分享文本<textarea value={text} maxLength={8192} onChange={(event) => { setText(event.target.value); setError(undefined); setLocalMetadata(undefined); setLocalAsset(undefined); setLocalStatus(""); }} placeholder="https://v.douyin.com/..." /></label>
    <label>下载清晰度<select value={quality} onChange={(event) => setQuality(event.target.value)}><option value="best">最佳可用画质</option><option value="1080">最高 1080p</option><option value="720">最高 720p</option></select></label>
    <div className="douyin-actions"><button type="button" disabled={busy || !text.trim() || (!bridgeToken && !capability?.available)} onClick={() => void inspectVideo()}>解析链接</button><button type="button" disabled={busy || !text.trim() || (!bridgeToken && !capability?.available)} onClick={() => void importVideo()}>导入到资产库</button></div>
    {capability && !capability.available && !bridgeToken && <p role="status">{capability.reason}</p>}
    {!bridgeToken && capability && !capability.cookie_configured && bridgeIssue && <p role="status">{bridgeIssue}</p>}
    <p>{bridgeToken ? "导入将使用本机浏览器会话，可能受到抖音平台限制。" : "当前由 Studio 服务器尝试下载；如果链接受限，请上传已保存的视频。"}</p>
    {localStatus && <p role="status">{localStatus}</p>}
    {localMetadata && <p role="status">解析成功：{localMetadata.title || "抖音视频"}{localMetadata.uploader ? ` · ${localMetadata.uploader}` : ""}{typeof localMetadata.duration === "number" ? ` · ${localMetadata.duration.toFixed(1)}s` : ""}</p>}
    {error && <FailureNotice failure={error} />}
    {pollError && <FailureNotice failure={pollError} />}
    {localAsset && <article><strong>已导入：{localAsset.filename}</strong><div className="douyin-actions"><button type="button" onClick={() => onReplicate(localAsset)}>用于复刻</button><a href={localAsset.contentUrl} download>下载文件</a></div></article>}
    <h3>下载任务</h3>
    {!tasks.length && <p>暂无下载任务</p>}
    <div className="douyin-tasks">{tasks.map((task) => <article key={task.id}>
      <strong data-i18n-ignore>{task.metadata?.title || task.url}</strong>
      {task.metadata?.uploader && <p data-i18n-ignore>{task.metadata.uploader}{typeof task.metadata.duration === "number" ? ` · ${task.metadata.duration.toFixed(1)}s` : ""}</p>}
      <p><span>{LABELS[task.status === "running" ? task.stage : task.status] || task.stage}</span> · {task.progress}%</p>
      {douyinActive(task) && <progress max={100} value={task.progress} aria-label="下载进度" />}
      {task.error && <FailureNotice failure={{...task.error, stage: task.stage, source: "server"}} />}
      <div className="douyin-actions">
        {douyinActive(task) && <button type="button" disabled={busy || task.stage === "importing" || task.status === "cancelling"} onClick={() => void action(`tasks/${task.id}/cancel`, {})}>取消</button>}
        {["failed", "canceled"].includes(task.status) && !RETRY_LATER.has(task.error?.code || "") && <button type="button" disabled={busy} onClick={() => void action(`tasks/${task.id}/retry`, {})}>重试</button>}
        {LOCAL_ALTERNATIVE.has(task.error?.code || "") && bridgeToken && <button type="button" disabled={busy} onClick={() => void importVideo(task.url)}>在本机导入</button>}
        {task.mode === "parse" && task.status === "completed" && <button type="button" disabled={busy} onClick={() => void submit("download", task.url)}>下载到资产库</button>}
        {task.asset && <><button type="button" onClick={() => setPreview(preview === task.id ? "" : task.id)}>预览视频</button><button type="button" onClick={() => onReplicate(task.asset!)}>用于复刻</button><a href={`/api/assets/${task.asset.id}/content`} download>下载文件</a></>}
      </div>
      {preview === task.id && task.asset && <video controls autoPlay preload="none" src={`/api/assets/${task.asset.id}/content`} />}
    </article>)}</div>
  </aside>;
}
