"use client";

/* Authenticated media routes are intentionally rendered directly. */
/* eslint-disable @next/next/no-img-element */
/* eslint-disable jsx-a11y/media-has-caption */

import { useEffect, useMemo, useRef, useState } from "react";
import type { LibraryAsset } from "./studio-library";
import { VideoProjectApi } from "./video-project-api";
import type { VideoProject } from "./video-project";
import {
  REPLICATION_PRESERVE_OPTIONS,
  REPLICATION_RECIPE_VERSION,
  analyzeReplicationScenes,
  planReplication,
  type ReplicationAudioPolicy,
  type ReplicationContinuity,
  type ReplicationPlan,
  type ReplicationPreserve,
} from "./replication-project";

type Props = {
  assets: LibraryAsset[];
  onUploadVideo: (file: File) => Promise<LibraryAsset>;
  onResultCreated: () => void;
  onOpenTimeline: () => void;
  onClose: () => void;
};

const API = new VideoProjectApi();
const DEFAULT_PRESERVE: ReplicationPreserve[] = ["timing", "motion", "camera", "composition"];
const PRESERVE_LABELS: Record<ReplicationPreserve, string> = {
  timing: "节奏", motion: "动作", camera: "运镜", composition: "构图",
  environment: "环境", lighting: "光线", interactions: "互动",
};

function durationLabel(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}s` : "时长未知";
}

function activeProject(project: VideoProject | undefined): boolean {
  return Boolean(project && ["running", "stopping", "merging", "submitting", "queued"].includes(project.status));
}

export default function ReplicationWorkshop({
  assets, onUploadVideo, onResultCreated, onOpenTimeline, onClose,
}: Props) {
  const videos = useMemo(() => assets.filter((asset) => asset.kind === "video"), [assets]);
  const images = useMemo(() => assets.filter((asset) => asset.kind === "image"), [assets]);
  const [sourceId, setSourceId] = useState("");
  const [title, setTitle] = useState("复刻工坊项目");
  const [brief, setBrief] = useState("");
  const [preserve, setPreserve] = useState<ReplicationPreserve[]>(DEFAULT_PRESERVE);
  const [replaceSubject, setReplaceSubject] = useState("");
  const [replaceProduct, setReplaceProduct] = useState("");
  const [replaceSetting, setReplaceSetting] = useState("");
  const [replaceStyle, setReplaceStyle] = useState("");
  const [referenceIds, setReferenceIds] = useState<string[]>([]);
  const [audioPolicy, setAudioPolicy] = useState<ReplicationAudioPolicy>("copy-source");
  const [continuity, setContinuity] = useState<ReplicationContinuity>("auto");
  const [plan, setPlan] = useState<ReplicationPlan>();
  const [project, setProject] = useState<VideoProject>();
  const [busy, setBusy] = useState<"upload" | "plan" | "run" | "merge" | "">("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const defaultSource = videos.find((asset) => {
    const duration = asset.media.duration;
    return typeof duration === "number" && duration >= 15 && duration <= 60;
  });
  const effectiveSourceId = sourceId || defaultSource?.id || "";
  const source = videos.find((asset) => asset.id === effectiveSourceId);

  useEffect(() => {
    if (!project?.id || !activeProject(project)) return;
    let canceled = false;
    const timer = window.setInterval(() => {
      void API.get(project.id!).then((next) => {
        if (canceled) return;
        setProject(next);
        if (next.merged?.status === "completed") {
          setNotice("复刻成片已完成，可以预览或下载。 ");
          onResultCreated();
        }
      }).catch((caught) => !canceled && setError(caught instanceof Error ? caught.message : "复刻任务状态读取失败"));
    }, 1800);
    return () => { canceled = true; window.clearInterval(timer); };
  }, [onResultCreated, project]);

  const resetPlan = () => { setPlan(undefined); setProject(undefined); setError(""); setNotice(""); };
  const togglePreserve = (item: ReplicationPreserve) => {
    resetPlan();
    setPreserve((current) => current.includes(item) ? current.filter((value) => value !== item) : [...current, item]);
  };
  const toggleReference = (assetId: string) => {
    resetPlan();
    setReferenceIds((current) => current.includes(assetId)
      ? current.filter((value) => value !== assetId)
      : current.length < 4 ? [...current, assetId] : current);
  };

  const createPlan = async () => {
    if (!source) { setError("请先选择 15–60 秒来源视频。"); return; }
    const duration = source.media.duration;
    if (typeof duration !== "number" || duration < 15 || duration > 60) {
      setError("来源视频必须是 15–60 秒；更长视频请先在长视频模块中裁剪。");
      return;
    }
    if (!brief.trim()) { setError("请说明要复刻什么，以及哪些内容要替换。"); return; }
    setBusy("plan"); setError(""); setNotice("正在分析镜头并编译 H3 分段方案…");
    try {
      let cutFrames: number[] = [];
      try { cutFrames = await analyzeReplicationScenes(source.id); }
      catch { setNotice("镜头分析不可用，已改用均衡合法分段。 "); }
      const next = await planReplication({
        version: REPLICATION_RECIPE_VERSION,
        source_asset_id: source.id,
        title: title.trim() || "复刻工坊项目",
        brief: brief.trim(),
        preserve,
        replace: {
          ...(replaceSubject.trim() ? { subject: replaceSubject.trim() } : {}),
          ...(replaceProduct.trim() ? { product: replaceProduct.trim() } : {}),
          ...(replaceSetting.trim() ? { setting: replaceSetting.trim() } : {}),
          ...(replaceStyle.trim() ? { style: replaceStyle.trim() } : {}),
        },
        references: referenceIds.map((assetId) => ({ asset_id: assetId, role: "replacement identity or product reference" })),
        audio_policy: audioPolicy,
        continuity,
        ...(cutFrames.length ? { cut_frames: cutFrames } : {}),
      });
      setPlan(next);
      setProject(undefined);
      setNotice(`方案已生成：${next.summary.segment_count} 段，每段不超过 15.1 秒。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "复刻方案生成失败");
    } finally { setBusy(""); }
  };

  const createAndRun = async () => {
    if (!plan || busy) return;
    setBusy("run"); setError("");
    try {
      const created = await API.create(plan.project);
      const running = await API.run(created.id!);
      setProject(running);
      setNotice("复刻项目已保存并开始逐段生成；关闭面板不会中断任务。 ");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "复刻项目启动失败");
    } finally { setBusy(""); }
  };

  const merge = async () => {
    if (!project?.id || busy) return;
    setBusy("merge"); setError("");
    try {
      setProject(await API.merge(project.id));
      setNotice("正在合并并精确裁切到来源时长…");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "复刻成片合并失败");
    } finally { setBusy(""); }
  };

  const completedSegments = project?.segments.filter((segment) => segment.status === "completed").length ?? 0;
  const projectComplete = Boolean(project?.segments.length && completedSegments === project.segments.length);

  return <aside id="replication-workshop-drawer" className="rail-drawer replication-drawer" aria-label="复刻工坊">
    <header className="rail-drawer-header">
      <div><strong>复刻工坊</strong><small>15–60 秒参考视频 · H3 原生分段生成</small></div>
      <button type="button" aria-label="关闭复刻工坊" onClick={onClose}>×</button>
    </header>
    <div className="replication-layout">
      <section className="replication-form">
        <div className="replication-section-heading"><b>1. 来源视频</b><span>动作、节奏与镜头结构的参考</span></div>
        <label>视频资产<select value={effectiveSourceId} onChange={(event) => { setSourceId(event.target.value); resetPlan(); }}>
          <option value="">选择 15–60 秒视频…</option>
          {videos.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename} · {durationLabel(asset.media.duration)}</option>)}
        </select></label>
        <button className="ghost-button" type="button" disabled={busy === "upload"} onClick={() => fileRef.current?.click()}>{busy === "upload" ? "上传中…" : "上传本地视频"}</button>
        <input ref={fileRef} hidden type="file" accept="video/*" onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (!file) return;
          setBusy("upload"); setError("");
          void onUploadVideo(file).then((asset) => { setSourceId(asset.id); resetPlan(); }).catch((caught) => setError(caught instanceof Error ? caught.message : "上传失败")).finally(() => setBusy(""));
        }}/>
        {source ? <video className="replication-source-preview" src={source.contentUrl} poster={source.thumbnailUrl} controls preload="metadata"/> : <div className="replication-empty">选择资产，或先用 <code>h3ctl douyin download</code> 下载抖音视频再上传。</div>}

        <div className="replication-section-heading"><b>2. 复刻意图</b><span>说清楚成片用途与变化目标</span></div>
        <label>项目名称<input value={title} maxLength={200} onChange={(event) => { setTitle(event.target.value); resetPlan(); }}/></label>
        <label>复刻说明<textarea rows={4} maxLength={4000} placeholder="例如：保持原片的口播节奏和推镜，人物换成参考角色，产品换成 H3 Studio，环境改为未来感直播间。" value={brief} onChange={(event) => { setBrief(event.target.value); resetPlan(); }}/></label>
        <fieldset><legend>需要保留</legend><div className="replication-checks">{REPLICATION_PRESERVE_OPTIONS.map((item) => <label key={item}><input type="checkbox" checked={preserve.includes(item)} onChange={() => togglePreserve(item)}/><span>{PRESERVE_LABELS[item]}</span></label>)}</div></fieldset>
        <div className="replication-replace-grid">
          <label>替换人物<input value={replaceSubject} onChange={(event) => { setReplaceSubject(event.target.value); resetPlan(); }} placeholder="人物身份、服装、外观"/></label>
          <label>替换产品<input value={replaceProduct} onChange={(event) => { setReplaceProduct(event.target.value); resetPlan(); }} placeholder="产品名称与外观"/></label>
          <label>替换场景<input value={replaceSetting} onChange={(event) => { setReplaceSetting(event.target.value); resetPlan(); }} placeholder="地点、布景、时间"/></label>
          <label>视觉风格<input value={replaceStyle} onChange={(event) => { setReplaceStyle(event.target.value); resetPlan(); }} placeholder="广告、写实、动漫等"/></label>
        </div>

        <div className="replication-section-heading"><b>3. 替换参考</b><span>可选，最多 4 张图片</span></div>
        <div className="replication-reference-grid">{images.length ? images.map((asset) => <button key={asset.id} type="button" aria-pressed={referenceIds.includes(asset.id)} onClick={() => toggleReference(asset.id)}><img src={asset.thumbnailUrl || asset.contentUrl} alt=""/><span>{asset.filename}</span></button>) : <div className="replication-empty">资产库中还没有图片参考。</div>}</div>

        <div className="replication-settings">
          <label>音频<select value={audioPolicy} onChange={(event) => { setAudioPolicy(event.target.value as ReplicationAudioPolicy); resetPlan(); }}>
            <option value="copy-source">成片保留原声</option><option value="reference-source">每段参考原声</option><option value="generate">重新生成音频</option><option value="mute">静音成片</option>
          </select></label>
          <label>连续性<select value={continuity} onChange={(event) => { setContinuity(event.target.value as ReplicationContinuity); resetPlan(); }}>
            <option value="auto">自动</option><option value="motion_context">Motion Context</option><option value="none">独立分段</option>
          </select></label>
        </div>
        <button className="primary-button replication-primary" type="button" disabled={Boolean(busy)} onClick={() => void createPlan()}>{busy === "plan" ? "正在分析与规划…" : "分析并生成复刻方案"}</button>
      </section>

      <section className="replication-plan">
        <div className="replication-section-heading"><b>执行方案</b><span>确认后才会提交付费/耗时生成</span></div>
        {!plan ? <div className="replication-plan-empty"><span>◇</span><strong>等待复刻方案</strong><p>H3 会把 15–60 秒视频拆成多个合法片段，并在完成后自动衔接与精确裁切。</p></div> : <>
          <dl className="replication-summary">
            <div><dt>来源 / 成片</dt><dd>{plan.summary.source_duration.toFixed(1)}s / {plan.summary.output_duration.toFixed(1)}s</dd></div>
            <div><dt>H3 生成段</dt><dd>{plan.summary.segment_count} 段</dd></div>
            <div><dt>连续性</dt><dd>{plan.summary.continuity === "motion_context" ? "Motion Context" : "独立参考"}</dd></div>
            <div><dt>末尾裁切</dt><dd>{plan.summary.final_trim_frames} 帧</dd></div>
          </dl>
          <details><summary>查看编译提示词</summary><p className="replication-prompt-preview">{plan.prompt}</p></details>
          {!project ? <button className="primary-button replication-primary" type="button" disabled={Boolean(busy)} onClick={() => void createAndRun()}>{busy === "run" ? "正在创建…" : "创建项目并开始生成"}</button> : <div className="replication-runtime">
            <div><strong>{project.status === "failed" ? "生成失败" : project.merged?.status === "completed" ? "成片完成" : project.status === "merging" ? "正在合并" : projectComplete ? "分段已完成" : "正在生成"}</strong><span>{completedSegments} / {project.segments.length} 段</span></div>
            <progress max={project.segments.length || 1} value={completedSegments}/>
            {project.error || project.merged?.error ? <p className="replication-error">{project.error || project.merged?.error}</p> : null}
            {projectComplete && project.merged?.status !== "completed" && project.status !== "merging" ? <button className="primary-button" type="button" disabled={Boolean(busy)} onClick={() => void merge()}>{busy === "merge" ? "正在提交合并…" : "合并复刻成片"}</button> : null}
            {project.merged?.preview_url ? <video src={project.merged.preview_url} controls preload="metadata"/> : null}
            <div className="replication-actions">
              {project.merged?.download_url ? <a className="ghost-button" href={project.merged.download_url}>下载成片</a> : null}
              <button className="ghost-button" type="button" onClick={onOpenTimeline}>在长视频中精修</button>
            </div>
          </div>}
        </>}
        {notice ? <p className="replication-notice">{notice}</p> : null}
        {error ? <p className="replication-error" role="alert">{error}</p> : null}
      </section>
    </div>
  </aside>;
}
