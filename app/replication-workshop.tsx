"use client";

/* Authenticated media routes are intentionally rendered directly. */
/* eslint-disable @next/next/no-img-element */
/* eslint-disable jsx-a11y/media-has-caption */

import { useEffect, useMemo, useRef, useState } from "react";
import PromptMentionComposer, { type PromptMentionItem } from "./prompt-mentions";
import type { LibraryAsset } from "./studio-library";
import { VideoProjectApi, VideoProjectApiError } from "./video-project-api";
import { serializeVideoProject, timelineProfileKey, type TimelineProfile, type VideoProject } from "./video-project";
import {
  REPLICATION_MAX_IMAGE_REFERENCES, REPLICATION_PRESERVE_OPTIONS, REPLICATION_RECIPE_VERSION,
  analyzeReplicationScenes, planReplication, replicationBriefAssetMentions,
  isReplicationProject, replicationIsActive, replicationEditImpact, replicationStatusLabel,
  type ReplicationAudioPolicy, type ReplicationContinuity, type ReplicationPlan, type ReplicationPreserve,
} from "./replication-project";

type Props = {
  initialSourceId?: string;
  assets: LibraryAsset[];
  profiles: TimelineProfile[];
  onUploadVideo: (file: File) => Promise<LibraryAsset>;
  onResultCreated: () => void;
  onOpenTimeline: (projectId: string) => void;
  onClose: () => void;
};
const API = new VideoProjectApi();
const SELECTED_KEY = "h3-replication-selected-v1";
const DEFAULT_PRESERVE: ReplicationPreserve[] = ["timing", "motion", "camera", "composition"];
const PRESERVE_LABELS: Record<ReplicationPreserve, string> = {
  timing: "节奏", motion: "动作", camera: "运镜", composition: "构图", environment: "环境", lighting: "光线", interactions: "互动",
};
function durationLabel(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}s` : "时长未知";
}
function exportJSON(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export default function ReplicationWorkshop({ initialSourceId, assets, profiles, onUploadVideo, onResultCreated, onOpenTimeline, onClose }: Props) {
  const videos = useMemo(() => assets.filter((asset) => asset.kind === "video"), [assets]);
  const images = useMemo(() => assets.filter((asset) => asset.kind === "image"), [assets]);
  const models = useMemo(() => profiles.filter((profile) => profile.compiler === "h3_ref" && profile.available), [profiles]);
  const [sourceId, setSourceId] = useState(initialSourceId ?? "");
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
  const [modelId, setModelId] = useState("");
  const [plan, setPlan] = useState<ReplicationPlan>();
  const [project, setProject] = useState<VideoProject>();
  const [projects, setProjects] = useState<VideoProject[]>([]);
  const [busy, setBusy] = useState("loading");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState<"setup" | "shots" | "result">("setup");
  const [segmentId, setSegmentId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [editVersion, setEditVersion] = useState<number>();
  const [page, setPage] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const notified = useRef(new Set<string>());
  const model = models.find((item) => timelineProfileKey(item) === modelId) ?? models[0];
  const effectiveSourceId = project?.storyboard?.source_asset_id || sourceId || videos.find((asset) => {
    const duration = asset.media.duration;
    return typeof duration === "number" && Number.isFinite(duration) && Math.round(duration * 24) >= 1;
  })?.id || "";
  const source = videos.find((asset) => asset.id === effectiveSourceId);
  const active = replicationIsActive(project);
  const selected = project?.segments.find((segment) => segment.id === segmentId);
  const dirty = Boolean(selected && selected.request.prompt !== prompt);
  const completed = project?.segments.filter((segment) => segment.status === "completed").length ?? 0;
  const complete = Boolean(project?.segments.length && completed === project.segments.length);
  const affected = project ? replicationEditImpact(project.segments, segmentId) : [];
  const locked = Boolean(busy) || active;
  const briefMentionItems: PromptMentionItem[] = images.map((asset) => ({
    id: asset.id, label: asset.filename, kind: "image", previewUrl: asset.thumbnailUrl || asset.contentUrl,
    connected: referenceIds.includes(asset.id),
  }));

  const accept = (value: VideoProject, selectFirst = false) => {
    setProject(value);
    setProjects((items) => [value, ...items.filter((item) => item.id !== value.id)]);
    if (value.id) { try { localStorage.setItem(SELECTED_KEY, value.id); } catch { /* optional preference */ } }
    if (selectFirst) { setSegmentId(value.segments[0]?.id ?? ""); setPrompt(value.segments[0]?.request.prompt ?? ""); setPage(0); }
  };

  useEffect(() => {
    let canceled = false;
    void API.list().then(async (items) => {
      if (canceled) return;
      const replicas = items.filter(isReplicationProject);
      setProjects(replicas);
      let preferred = "";
      try { preferred = localStorage.getItem(SELECTED_KEY) ?? ""; } catch { /* optional preference */ }
      // The server list validates the local id against the current dataset.
      const found = initialSourceId ? undefined : replicas.find((item) => item.id === preferred);
      if (found?.id) {
        const value = await API.get(found.id);
        if (!canceled && isReplicationProject(value)) {
          setProject(value); setSegmentId(value.segments[0]?.id ?? ""); setPrompt(value.segments[0]?.request.prompt ?? ""); setTab("shots");
        }
      }
    }).catch((caught) => { if (!canceled) setError(caught instanceof Error ? caught.message : "项目读取失败"); })
      .finally(() => { if (!canceled) setBusy(""); });
    return () => { canceled = true; };
  }, [initialSourceId]);

  useEffect(() => {
    if (!project?.id || busy) return;
    let canceled = false;
    let timer: number;
    const poll = async () => {
      try {
        const next = await API.get(project.id!);
        if (canceled) return;
        setProject(next);
        setProjects((items) => items.map((item) => item.id === next.id ? next : item));
        const key = next.merged?.status === "completed" ? `${next.id}:${next.merged.sha256 ?? next.merged.preview_url}` : "";
        if (key && !notified.current.has(key)) { notified.current.add(key); onResultCreated(); }
      } catch (caught) { if (!canceled) setError(caught instanceof Error ? caught.message : "项目状态更新失败"); }
      if (!canceled) timer = window.setTimeout(() => void poll(), 2500);
    };
    timer = window.setTimeout(() => void poll(), 1800);
    return () => { canceled = true; window.clearTimeout(timer); };
  }, [busy, onResultCreated, project?.id]);

  const resetPlan = () => { setPlan(undefined); setError(""); setNotice(""); };
  const togglePreserve = (item: ReplicationPreserve) => { resetPlan(); setPreserve((items) => items.includes(item) ? items.filter((value) => value !== item) : [...items, item]); };
  const toggleReference = (id: string) => {
    resetPlan();
    setReferenceIds((items) => items.includes(id) ? items.filter((value) => value !== id)
      : items.length < REPLICATION_MAX_IMAGE_REFERENCES ? [...items, id] : items);
  };
  const selectBriefReference = (item: PromptMentionItem) => {
    if (brief.length + item.id.length + 4 > 4000) {
      setError("复刻说明已接近 4000 字上限；请先删减文字再引用素材。");
      return false;
    }
    if (referenceIds.includes(item.id)) return true;
    if (referenceIds.length >= REPLICATION_MAX_IMAGE_REFERENCES) {
      setError(`最多选择 ${REPLICATION_MAX_IMAGE_REFERENCES} 张图片参考；请先取消一张。`);
      return false;
    }
    resetPlan();
    setReferenceIds((items) => [...items, item.id]);
    return true;
  };
  const action = async (name: string, task: () => Promise<VideoProject>, message: string) => {
    if (busy) return;
    setBusy(name); setError("");
    try { const next = await task(); accept(next); setNotice(message); return next; }
    catch (caught) {
      if (caught instanceof VideoProjectApiError && caught.code === "project_changed" && project?.id) {
        try { accept(await API.get(project.id)); } catch { /* keep the conflict visible if refresh fails */ }
      }
      setError(caught instanceof VideoProjectApiError && caught.code === "project_changed"
        ? "项目已在其他位置更新。请撤销未保存修改，读取最新内容后再编辑。"
        : caught instanceof Error ? caught.message : "项目操作失败");
    }
    finally { setBusy(""); }
  };
  const open = async (id: string) => {
    await action("open", async () => {
      const value = await API.get(id);
      if (!isReplicationProject(value)) throw new Error("这不是复刻项目");
      setPlan(undefined); setSegmentId(value.segments[0]?.id ?? ""); setPrompt(value.segments[0]?.request.prompt ?? ""); setPage(0); setTab("shots");
      return value;
    }, "项目已恢复");
  };
  const createPlan = async () => {
    if (!source || !model || busy) return;
    if (!brief.trim()) { setError("请说明要复刻什么，以及哪些内容要替换。"); return; }
    if (replicationBriefAssetMentions(brief).some((id) => !referenceIds.includes(id))) {
      setError("复刻说明中的 @ 素材尚未选入“替换参考”；请重新选择该素材，或删除对应标签。");
      return;
    }
    if (referenceIds.some((id) => !images.some((asset) => asset.id === id))) {
      setError("已选的图片参考不在当前资产库中；请重新选择后再规划。");
      return;
    }
    setBusy("plan"); setError(""); setNotice("");
    try {
      let cutFrames: number[] = []; let analysisNotice = "镜头切点已分析。";
      try { cutFrames = await analyzeReplicationScenes(source.id); }
      catch { analysisNotice = "镜头分析不可用，已使用合法均衡分段。"; }
      const next = await planReplication({
        version: REPLICATION_RECIPE_VERSION, source_asset_id: source.id, title: title.trim() || "复刻工坊项目", brief: brief.trim(), preserve,
        replace: { subject: replaceSubject, product: replaceProduct, setting: replaceSetting, style: replaceStyle },
        references: referenceIds.map((assetId) => ({ asset_id: assetId, role: "replacement identity or product reference" })),
        audio_policy: audioPolicy, continuity, cut_frames: cutFrames,
        profile_id: model.id, profile_version: model.version, profile_digest: model.manifest_sha256,
      });
      setPlan(next); setNotice(`${analysisNotice} 方案尚未提交生成，请保存草稿后逐段审阅。`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "复刻方案生成失败"); }
    finally { setBusy(""); }
  };
  const saveDraft = async () => {
    if (!plan) return;
    const next = await action("save", () => API.create(plan.project), "草稿已保存，尚未提交生成");
    if (next) { accept(next, true); setTab("shots"); }
  };
  const savePrompt = async () => {
    if (!project?.id || !selected || !project.updated_at || !dirty || active) return;
    await action("edit", () => API.editReplicationSegment(project.id!, selected.id, { expected_updated_at: editVersion ?? project.updated_at!, prompt }), "分段已保存；受影响的连续片段需要重新生成");
  };
  const newProject = () => {
    setProject(undefined); setPlan(undefined); setSegmentId(""); setPrompt(""); setError(""); setNotice(""); setTab("setup");
    try { localStorage.removeItem(SELECTED_KEY); } catch { /* optional preference */ }
  };

  return <aside id="replication-workshop-drawer" className="rail-drawer replication-drawer" aria-label="复刻工坊">
    <header className="rail-drawer-header"><div><strong>复刻工坊</strong><small>来源视频不限总时长 · H3 分段生成</small></div><button type="button" aria-label="关闭复刻工坊" disabled={dirty || Boolean(busy)} onClick={onClose}>×</button></header>
    <div className="replication-project-bar">
      <label>复刻项目<select aria-label="复刻项目" value={project?.id ?? ""} disabled={Boolean(busy) || dirty} onChange={(event) => { if (event.target.value) void open(event.target.value); }}>
        <option value="">新建项目</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title} · {replicationStatusLabel(item.status)}</option>)}
      </select></label>
      <button type="button" className="ghost-button" disabled={Boolean(busy) || dirty} onClick={newProject}>新建复刻</button>
      {project?.id ? <button type="button" className="ghost-button" disabled={Boolean(busy) || dirty} onClick={() => exportJSON(serializeVideoProject(project), `replication-${project.id}.json`)}>导出项目 JSON</button> : null}
      <nav aria-label="复刻工作步骤">{([['setup', '素材与方案'], ['shots', '分段审阅'], ['result', '成片']] as const).map(([key, label]) => <button key={key} type="button" aria-pressed={tab === key} disabled={key !== "setup" && !project} onClick={() => setTab(key)}>{label}</button>)}</nav>
    </div>
    {error ? <p className="replication-error" role="alert">{error}</p> : null}
    {notice ? <p className="replication-notice" role="status">{notice}</p> : null}
    {tab === "setup" ? <div className="replication-layout">
      {project ? <section className="replication-form">
        <h3 data-i18n-ignore>{project.title}</h3>
        {source ? <video className="replication-source-preview" src={source.contentUrl} controls preload="metadata"/> : <p>来源视频暂不可用</p>}
        <label>复刻说明<p data-i18n-ignore>{String(project.recipe?.brief ?? "")}</p></label>
        <dl className="replication-summary"><div><dt>来源时长</dt><dd>{durationLabel(source?.media.duration)}</dd></div><div><dt>H3 生成段</dt><dd>{project.segments.length}</dd></div></dl>
        <details><summary>已保存的复刻方案</summary><pre data-i18n-ignore>{JSON.stringify(project.recipe, null, 2)}</pre></details>
      </section> : <section className="replication-form"><fieldset disabled={Boolean(project) || Boolean(busy)} className="replication-form-fields">
        <div className="replication-section-heading"><b>1. 来源视频</b><span>动作、节奏与镜头结构的参考</span></div>
        <label>视频资产<select value={effectiveSourceId} onChange={(event) => { setSourceId(event.target.value); resetPlan(); }}>
          <option value="">选择来源视频…</option>
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
        <div className="replication-brief-field"><span>复刻说明</span><PromptMentionComposer value={brief} onChange={(value) => { setBrief(value.slice(0, 4000)); resetPlan(); }} items={briefMentionItems} onSelectItem={selectBriefReference} ariaLabel="复刻说明" placeholder="例如：保持原片节奏，把黄发女孩替换为 @ 资产库角色" disabled={Boolean(busy)}/><small>输入 @ 或点右侧 @ 选择图片；选中后会自动加入下方“替换参考”。{brief.length}/4000 字</small></div>
        <fieldset><legend>需要保留</legend><div className="replication-checks">{REPLICATION_PRESERVE_OPTIONS.map((item) => <label key={item}><input type="checkbox" checked={preserve.includes(item)} onChange={() => togglePreserve(item)}/><span>{PRESERVE_LABELS[item]}</span></label>)}</div></fieldset>
        <div className="replication-replace-grid">
          <label>替换人物<input value={replaceSubject} onChange={(event) => { setReplaceSubject(event.target.value); resetPlan(); }} placeholder="人物身份、服装、外观"/></label>
          <label>替换产品<input value={replaceProduct} onChange={(event) => { setReplaceProduct(event.target.value); resetPlan(); }} placeholder="产品名称与外观"/></label>
          <label>替换场景<input value={replaceSetting} onChange={(event) => { setReplaceSetting(event.target.value); resetPlan(); }} placeholder="地点、布景、时间"/></label>
          <label>视觉风格<input value={replaceStyle} onChange={(event) => { setReplaceStyle(event.target.value); resetPlan(); }} placeholder="广告、写实、动漫等"/></label>
        </div>

        <div className="replication-section-heading"><b>3. 替换参考</b><span>可选，最多 9 张图片</span></div>
        <div className="replication-reference-grid">{images.length ? images.map((asset) => <button key={asset.id} type="button" aria-pressed={referenceIds.includes(asset.id)} onClick={() => toggleReference(asset.id)}><img src={asset.thumbnailUrl || asset.contentUrl} alt=""/><span>{asset.filename}</span></button>) : <div className="replication-empty">资产库中还没有图片参考。</div>}</div>

        <label>生成模型<select value={model ? timelineProfileKey(model) : ""} onChange={(event) => { setModelId(event.target.value); resetPlan(); }}>
          {!models.length ? <option value="">没有可用的 H3 Ref2VA 模型</option> : models.map((item) => <option key={timelineProfileKey(item)} value={timelineProfileKey(item)}>{item.display_name} · {item.version}</option>)}
        </select></label>
        <div className="replication-settings">
          <label>音频<select value={audioPolicy} onChange={(event) => { setAudioPolicy(event.target.value as ReplicationAudioPolicy); resetPlan(); }}>
            <option value="copy-source">成片保留原声</option><option value="reference-source">每段参考原声</option><option value="generate">重新生成音频</option><option value="mute">静音成片</option>
          </select></label>
          <label>连续性<select value={continuity} onChange={(event) => { setContinuity(event.target.value as ReplicationContinuity); resetPlan(); }}>
            <option value="auto">自动</option><option value="motion_context">Motion Context</option><option value="none">独立分段</option>
          </select></label>
        </div>
        <button className="primary-button replication-primary" type="button" disabled={Boolean(busy) || !model || !source} onClick={() => void createPlan()}>{busy === "plan" ? "正在分析与规划…" : "分析并生成复刻方案"}</button>      </fieldset></section>}
      <section className="replication-plan">
        <div className="replication-section-heading"><b>执行方案</b><span>确认后才会提交付费/耗时生成</span></div>
        <p className="replication-explanation">镜头分析检测切点，不会自动理解剧情、对白或字幕。请在分段审阅中检查并调整每段提示词。</p>
        {project ? <><h3 data-i18n-ignore>{project.title}</h3><p>项目已保存，可继续审阅分段或查看成片。</p><button className="primary-button" type="button" onClick={() => setTab("shots")}>审阅分段</button></> : !plan ? <div className="replication-plan-empty"><span>◇</span><strong>等待复刻方案</strong><p>H3 每段不超过 15.1 秒，按合法时长分段生成，短视频补足一段后裁回原时长。总时长不设固定上限，上传大小、项目容量与可用存储仍受服务器配置约束。</p></div> : <>
          <dl className="replication-summary"><div><dt>来源 / 成片</dt><dd>{durationLabel(plan.summary.source_duration)} / {durationLabel(plan.summary.output_duration)}</dd></div><div><dt>H3 生成段</dt><dd>{plan.summary.segment_count}</dd></div><div><dt>连续性</dt><dd>{plan.summary.continuity}</dd></div><div><dt>末尾裁切</dt><dd>{plan.summary.final_trim_frames}</dd></div></dl>
          <details><summary>查看编译提示词</summary><p className="replication-prompt-preview" data-i18n-ignore>{plan.prompt}</p></details>
          <button className="primary-button replication-primary" type="button" disabled={Boolean(busy)} onClick={() => void saveDraft()}>保存草稿并审阅分段</button>
          <button className="ghost-button" type="button" onClick={() => exportJSON(plan, "replication-plan.json")}>导出方案 JSON</button>
        </>}
      </section>
    </div> : project ? <div className="replication-review">
      <div className="replication-runbar">
        <div><strong data-i18n-ignore>{project.title}</strong><span>{replicationStatusLabel(project.status)} · {completed} / {project.segments.length}</span></div>
        <progress max={project.segments.length || 1} value={completed}/>
        <div className="replication-actions">
          {active ? <button className="ghost-button" type="button" disabled={Boolean(busy) || project.status === "stopping"} onClick={() => void action("stop", () => API.stop(project.id!), "已请求停止，完成结果会保留")}>停止任务</button> : <button className="primary-button" type="button" disabled={locked || dirty || complete} onClick={() => void action("run", () => API.run(project.id!), "已开始生成未完成分段")}>生成未完成分段</button>}
          <button className="ghost-button" type="button" disabled={locked || dirty || !complete || project.merged?.status === "completed"} onClick={() => void action("merge", () => API.merge(project.id!), "正在合并成片")}>合并复刻成片</button>
          <button className="ghost-button" type="button" disabled={Boolean(busy) || dirty} onClick={() => onOpenTimeline(project.id!)}>在长视频中精修</button>
        </div>
        {project.error || project.merged?.error ? <p className="replication-error">{project.error || project.merged?.error}</p> : null}
      </div>
      {tab === "shots" ? <div className="replication-shot-layout">
        <section className="replication-shot-list" aria-label="分段列表">
          {project.segments.slice(page * 20, (page + 1) * 20).map((segment, index) => <button key={segment.id} type="button" disabled={dirty || Boolean(busy)} aria-pressed={segment.id === segmentId} onClick={() => { setSegmentId(segment.id); setPrompt(segment.request.prompt); }}>
            <b>#{page * 20 + index + 1}</b><span>{durationLabel((segment.source_range?.start_frame ?? 0) / 24)}–{durationLabel((segment.source_range?.end_frame ?? 0) / 24)}</span><small>{replicationStatusLabel(segment.status)}</small>
          </button>)}
          <div className="replication-actions"><button type="button" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>上一页</button><span>{page + 1} / {Math.max(1, Math.ceil(project.segments.length / 20))}</span><button type="button" disabled={(page + 1) * 20 >= project.segments.length} onClick={() => setPage((value) => value + 1)}>下一页</button></div>
        </section>
        {selected ? <section className="replication-shot-editor">
          <div className="replication-comparison"><div><h4>来源片段</h4>{source && selected.source_range ? <video key={`${selected.id}-source`} src={`${source.contentUrl}#t=${selected.source_range.start_frame / 24},${selected.source_range.end_frame / 24}`} controls preload="none"/> : <p>来源视频暂不可用</p>}</div><div><h4>生成片段</h4>{selected.preview_url ? <video key={selected.preview_url} src={selected.preview_url} controls preload="none"/> : <div className="replication-empty">此段尚无生成结果</div>}</div></div>
          <label>分段提示词<textarea aria-label="分段提示词" rows={9} value={prompt} maxLength={12000} disabled={locked} onChange={(event) => { if (!dirty) setEditVersion(project.updated_at); setPrompt(event.target.value); }}/></label>
          {dirty ? <p className="replication-notice" role="status">有未保存的分段修改，请先保存或撤销。</p> : null}
          <p className="replication-explanation">保存或重跑会使当前段和依赖它的连续片段需要重新生成。<span> {affected.length} 段</span></p>
          <div className="replication-actions">
            <button className="primary-button" type="button" disabled={locked || !dirty || !prompt.trim()} onClick={() => void savePrompt()}>保存分段修改</button>
            <button className="ghost-button" type="button" disabled={!dirty || Boolean(busy)} onClick={() => { setPrompt(selected.request.prompt); setError(""); }}>撤销未保存修改</button>
            <button className="ghost-button" type="button" disabled={locked || dirty} onClick={() => void action("rerun", () => API.runSegment(project.id!, selected.id), "已提交当前分段")}>生成 / 重跑此段</button>
          </div>
          {selected.error ? <p className="replication-error">{selected.error}</p> : null}
          <details><summary>CLI 与项目标识</summary><code>{project.id}</code><br/><code>{selected.id}</code><pre>{`h3ctl replication inspect ${project.id} --json\nh3ctl replication resume ${project.id} --to final.mp4`}</pre></details>
        </section> : null}
      </div> : <section className="replication-result-panel">{project.merged?.preview_url ? <><video src={project.merged.preview_url} controls preload="metadata"/><a className="primary-button" href={project.merged.download_url}>下载成片</a></> : <div className="replication-plan-empty"><strong>等待成片</strong><p>全部分段完成后，点击“合并复刻成片”。</p></div>}</section>}
    </div> : null}
  </aside>;
}
