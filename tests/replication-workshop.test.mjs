import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const model = readFileSync(new URL("../app/replication-project.ts", import.meta.url), "utf8");
const workshop = readFileSync(new URL("../app/replication-workshop.tsx", import.meta.url), "utf8");
const mentions = readFileSync(new URL("../app/prompt-mentions.tsx", import.meta.url), "utf8");
const studio = readFileSync(new URL("../app/studio.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/video-project-api.ts", import.meta.url), "utf8");

test("replication workshop uses the versioned server planner and scene analysis", () => {
  assert.match(model, /h3\.replication\/v1/);
  assert.match(model, /\/api\/media\/analyze-scenes/);
  assert.match(model, /\/api\/video\/replication\/plan/);
  assert.match(workshop, /analyzeReplicationScenes/);
  assert.match(workshop, /planReplication/);
});

test("replication brief @ mentions bind selected image references", async () => {
  const { replicationBriefAssetMentions } = await import("../app/replication-project.ts");
  const first = "a".repeat(32);
  const second = "b".repeat(32);
  assert.deepEqual(replicationBriefAssetMentions(`黄发女孩替换为@{${first}}，女仆替换为@{${second}}，再看@{${first}}`), [first, second]);
  assert.match(workshop, /<PromptMentionComposer value=\{brief\}/);
  assert.match(workshop, /onSelectItem=\{selectBriefReference\}/);
  assert.match(workshop, /replicationBriefAssetMentions\(brief\)/);
  assert.match(workshop, /referenceIds\.map\(\(assetId\) =>/);
  assert.doesNotMatch(mentions, /item\.previewUrl \?\? ""\}:\$\{item\.connected\}/);
});

test("replication execution is durable and reuses video projects", () => {
  assert.match(workshop, /API\.create\(plan\.project\)/);
  assert.match(workshop, /API\.run\(project\.id!/);
  assert.match(workshop, /API\.merge\(project\.id!\)/);
  assert.match(api, /\/api\/video-projects/);
});

test("studio exposes replication as an independent top-level module", () => {
  assert.match(studio, /import ReplicationWorkshop from "\.\/replication-workshop"/);
  assert.match(studio, /railPanel === "replication"/);
  assert.match(studio, /<ReplicationWorkshop/);
  assert.match(studio, /<span>复刻<\/span>/);
});

test("workshop explains uncapped source duration and the Douyin handoff", () => {
  assert.match(workshop, /来源视频不限总时长/);
  assert.doesNotMatch(workshop, /duration [<>]=? (15|60)|15–60/);
  assert.match(workshop, /Number\.isFinite\(duration\)/);
  assert.match(workshop, /h3ctl douyin download/);
  assert.match(workshop, /每段不超过 15\.1 秒/);
  assert.match(workshop, /确认后才会提交付费\/耗时生成/);
});


test("editing reports only downstream continuation dependencies", async () => {
  const { replicationEditImpact, isReplicationProject, replicationIsActive } = await import("../app/replication-project.ts");
  const segments = [
    { id: "a", continuation: "none" }, { id: "b", continuation: "motion_context" },
    { id: "c", continuation: "previous_video" }, { id: "d", continuation: "none" },
  ];
  assert.deepEqual(replicationEditImpact(segments, "b"), ["b", "c"]);
  assert.deepEqual(replicationEditImpact(segments, "a"), ["a", "b", "c"]);
  assert.deepEqual(replicationEditImpact(segments, "missing"), []);
  assert.equal(isReplicationProject({ recipe: { type: "replication", version: "h3.replication/v1" } }), true);
  assert.equal(isReplicationProject({}), false);
  assert.equal(replicationIsActive({status: "merging"}), true);
  assert.equal(replicationIsActive({status: "partial"}), false);
});
