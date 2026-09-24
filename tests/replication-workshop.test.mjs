import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const model = readFileSync(new URL("../app/replication-project.ts", import.meta.url), "utf8");
const workshop = readFileSync(new URL("../app/replication-workshop.tsx", import.meta.url), "utf8");
const studio = readFileSync(new URL("../app/studio.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/video-project-api.ts", import.meta.url), "utf8");

test("replication workshop uses the versioned server planner and scene analysis", () => {
  assert.match(model, /h3\.replication\/v1/);
  assert.match(model, /\/api\/media\/analyze-scenes/);
  assert.match(model, /\/api\/video\/replication\/plan/);
  assert.match(workshop, /analyzeReplicationScenes/);
  assert.match(workshop, /planReplication/);
});

test("replication execution is durable and reuses video projects", () => {
  assert.match(workshop, /API\.create\(plan\.project\)/);
  assert.match(workshop, /API\.run\(created\.id!/);
  assert.match(workshop, /API\.merge\(project\.id\)/);
  assert.match(api, /\/api\/video-projects/);
});

test("studio exposes replication as an independent top-level module", () => {
  assert.match(studio, /import ReplicationWorkshop from "\.\/replication-workshop"/);
  assert.match(studio, /railPanel === "replication"/);
  assert.match(studio, /<ReplicationWorkshop/);
  assert.match(studio, /<span>复刻<\/span>/);
});

test("workshop explains the 15 to 60 second contract and the Douyin handoff", () => {
  assert.match(workshop, /15–60 秒/);
  assert.match(workshop, /h3ctl douyin download/);
  assert.match(workshop, /每段不超过 15\.1 秒/);
  assert.match(workshop, /确认后才会提交付费\/耗时生成/);
});
