import assert from "node:assert/strict";
import test from "node:test";

import { createCanvasNode } from "../app/studio-document.ts";
import {
  buildGeneratorExecutionPlan,
  buildOutputCollectionPlan,
  compilePromptDocument,
  connectMedia,
  disconnectMedia,
  firstAvailableSlot,
  invalidateDownstreamGenerators,
  orderedGeneratorInputs,
  planOutputPropagation,
} from "../app/studio-graph.ts";

function ids(prefix = "id") {
  let next = 0;
  return () => `${prefix}-${++next}`;
}

function emptyDocument(nodes = []) {
  return { version: 7, viewport: { x: 0, y: 0, zoom: 1 }, nodes, edges: [], groups: [], unassignedResults: [], migration: { issues: [] } };
}

function node(kind, id, x = 0) {
  const created = createCanvasNode(kind, { x, y: 0 }, () => id);
  return created;
}

function asset(id, mediaKind) {
  const created = node("asset", id);
  created.mediaKind = mediaKind;
  created.title = id;
  created.asset.fileName = `${id}.${mediaKind}`;
  created.asset.remoteId = id.padEnd(32, id[0] || "a").slice(0, 32);
  created.asset.uploadState = "ready";
  return created;
}

function mustConnect(document, sourceId, targetId, options = {}) {
  const result = connectMedia(document, sourceId, targetId, { ...options, createId: options.createId ?? ids(`${sourceId}-${targetId}`) });
  assert.equal(result.ok, true, result.issues.map((issue) => issue.message).join("\n"));
  return result;
}

test("clicking generator A plans only A's reverse subgraph and never touches disconnected B", () => {
  const imageAsset = asset("asset-a", "image");
  const videoA = node("video-generator", "video-a");
  const videoB = node("video-generator", "video-b");
  let document = emptyDocument([imageAsset, videoA, videoB]);
  document = mustConnect(document, imageAsset.id, videoA.id).document;

  const plan = buildGeneratorExecutionPlan(document, videoA.id);
  assert.deepEqual(plan.nodeIds, [imageAsset.id, videoA.id]);
  assert.deepEqual(plan.steps.map((step) => [step.nodeId, step.action]), [[videoA.id, "run"]]);
  assert.equal(plan.nodes.some((candidate) => candidate.id === videoB.id), false);
  assert.equal(plan.edges.every((edge) => edge.targetNodeId === videoA.id), true);
});

test("image→video runs stale upstream but reuses a fresh revision and always runs the clicked target", () => {
  const image = node("image-generator", "image-gen");
  const video = node("video-generator", "video-gen");
  image.configRevision = 3;
  image.lastSuccessfulRevision = 2;
  image.resultVersions = [{ id: "image-v2", mediaKind: "image", revision: 2, createdAt: 1 }];
  let document = emptyDocument([image, video]);
  document = mustConnect(document, image.id, video.id).document;

  const stale = buildGeneratorExecutionPlan(document, video.id);
  assert.deepEqual(stale.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [image.id, "run", "stale-result"],
    [video.id, "run", "target-requested"],
  ]);

  const freshImage = document.nodes.find((candidate) => candidate.id === image.id);
  freshImage.lastSuccessfulRevision = freshImage.configRevision;
  freshImage.resultVersions.push({ id: "image-v3", mediaKind: "image", revision: freshImage.configRevision, createdAt: 2 });
  const fresh = buildGeneratorExecutionPlan(document, video.id);
  assert.deepEqual(fresh.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [image.id, "reuse", "fresh-result"],
    [video.id, "run", "target-requested"],
  ]);
  assert.equal(fresh.steps[0].result.id, "image-v3");
});

test("A→B→C propagates dirty execution through a fresh-looking intermediate generator", () => {
  const upstream = node("image-generator", "a");
  const intermediate = node("image-generator", "b");
  const target = node("video-generator", "c");
  let document = emptyDocument([upstream, intermediate, target]);
  document = mustConnect(document, upstream.id, intermediate.id, { createId: ids("a-b") }).document;
  document = mustConnect(document, intermediate.id, target.id, { createId: ids("b-c") }).document;

  const a = document.nodes.find((candidate) => candidate.id === upstream.id);
  const b = document.nodes.find((candidate) => candidate.id === intermediate.id);
  a.configRevision = 2;
  a.lastSuccessfulRevision = 1;
  a.resultVersions = [{ id: "a-old", mediaKind: "image", revision: 1 }];
  b.configRevision = 5;
  b.lastSuccessfulRevision = 5;
  b.resultVersions = [{ id: "b-fresh-against-old-a", mediaKind: "image", revision: 5 }];

  const dirty = buildGeneratorExecutionPlan(document, target.id);
  assert.deepEqual(dirty.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [upstream.id, "run", "stale-result"],
    [intermediate.id, "run", "upstream-changed"],
    [target.id, "run", "target-requested"],
  ]);

  a.lastSuccessfulRevision = a.configRevision;
  a.resultVersions.push({ id: "a-current", mediaKind: "image", revision: a.configRevision });
  const allFresh = buildGeneratorExecutionPlan(document, target.id);
  assert.deepEqual(allFresh.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [upstream.id, "reuse", "fresh-result"],
    [intermediate.id, "reuse", "fresh-result"],
    [target.id, "run", "target-requested"],
  ]);
});

test("dirty propagation follows only dependent branches and excludes unrelated subgraphs", () => {
  const dirtyRoot = node("image-generator", "dirty-root");
  const dirtyBranch = node("image-generator", "dirty-branch");
  const cleanBranch = node("image-generator", "clean-branch");
  const target = node("video-generator", "target");
  const unrelatedRoot = node("image-generator", "unrelated-root");
  const unrelatedTarget = node("video-generator", "unrelated-target");
  let document = emptyDocument([dirtyRoot, dirtyBranch, cleanBranch, target, unrelatedRoot, unrelatedTarget]);
  document = mustConnect(document, dirtyRoot.id, dirtyBranch.id, { createId: ids("dirty-link") }).document;
  document = mustConnect(document, dirtyBranch.id, target.id, { createId: ids("dirty-target") }).document;
  document = mustConnect(document, cleanBranch.id, target.id, { createId: ids("clean-target") }).document;
  document = mustConnect(document, unrelatedRoot.id, unrelatedTarget.id, { createId: ids("unrelated") }).document;

  const dirty = document.nodes.find((candidate) => candidate.id === dirtyRoot.id);
  dirty.configRevision = 3;
  dirty.lastSuccessfulRevision = 2;
  dirty.resultVersions = [{ id: "dirty-old", mediaKind: "image", revision: 2 }];
  for (const id of [dirtyBranch.id, cleanBranch.id]) {
    const current = document.nodes.find((candidate) => candidate.id === id);
    current.configRevision = 4;
    current.lastSuccessfulRevision = 4;
    current.resultVersions = [{ id: `${id}-fresh`, mediaKind: "image", revision: 4 }];
  }
  const unrelated = document.nodes.find((candidate) => candidate.id === unrelatedRoot.id);
  unrelated.configRevision = 9;
  unrelated.lastSuccessfulRevision = 1;
  unrelated.resultVersions = [{ id: "unrelated-old", mediaKind: "image", revision: 1 }];

  const plan = buildGeneratorExecutionPlan(document, target.id);
  assert.deepEqual(plan.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [dirtyRoot.id, "run", "stale-result"],
    [dirtyBranch.id, "run", "upstream-changed"],
    [cleanBranch.id, "reuse", "fresh-result"],
    [target.id, "run", "target-requested"],
  ]);
  assert.equal(plan.nodeIds.includes(unrelatedRoot.id), false);
  assert.equal(plan.nodeIds.includes(unrelatedTarget.id), false);
});

test("a completed A invalidates persistent B/C state once while an independent D branch remains reusable", () => {
  const a = node("image-generator", "a");
  const b = node("image-generator", "b");
  const d = node("image-generator", "d");
  const c = node("video-generator", "c");
  let document = emptyDocument([a, b, d, c]);
  document = mustConnect(document, a.id, b.id, { createId: ids("a-b") }).document;
  document = mustConnect(document, b.id, c.id, { createId: ids("b-c") }).document;
  document = mustConnect(document, d.id, c.id, { createId: ids("d-c") }).document;

  for (const current of document.nodes) {
    if (current.kind !== "image-generator" && current.kind !== "video-generator") continue;
    current.configRevision = 7;
    current.lastSuccessfulRevision = 7;
    current.resultVersions = [{ id: `${current.id}-result`, mediaKind: current.kind === "video-generator" ? "video" : "image", revision: 7 }];
  }
  const before = buildGeneratorExecutionPlan(document, c.id);
  assert.deepEqual(before.steps.map((step) => [step.nodeId, step.action]), [
    [a.id, "reuse"], [b.id, "reuse"], [d.id, "reuse"], [c.id, "run"],
  ]);

  // A has just completed a new result for the same config. A itself remains
  // reusable, while every generator consuming that new result becomes stale.
  const invalidation = invalidateDownstreamGenerators(document, a.id);
  assert.deepEqual(invalidation.invalidatedNodeIds, [b.id, c.id]);
  assert.equal(invalidation.document.nodes.find((current) => current.id === a.id).configRevision, 7);
  assert.equal(invalidation.document.nodes.find((current) => current.id === b.id).configRevision, 8);
  assert.equal(invalidation.document.nodes.find((current) => current.id === c.id).configRevision, 8);
  assert.equal(invalidation.document.nodes.find((current) => current.id === d.id).configRevision, 7);

  const after = buildGeneratorExecutionPlan(invalidation.document, c.id);
  assert.deepEqual(after.steps.map((step) => [step.nodeId, step.action, step.reason]), [
    [a.id, "reuse", "fresh-result"],
    [b.id, "run", "stale-result"],
    [d.id, "reuse", "fresh-result"],
    [c.id, "run", "target-requested"],
  ]);

  const repeated = invalidateDownstreamGenerators(invalidation.document, a.id);
  assert.equal(repeated.document, invalidation.document, "retrying invalidation is an idempotent no-op");
  assert.deepEqual(repeated.invalidatedNodeIds, []);
  assert.equal(repeated.document.nodes.find((current) => current.id === b.id).configRevision, 8);
});

test("execution planning detects existing cycles and connection transaction refuses new cycles", () => {
  const first = node("image-generator", "first");
  const second = node("image-generator", "second");
  let document = emptyDocument([first, second]);
  document = mustConnect(document, first.id, second.id).document;
  const rejected = connectMedia(document, second.id, first.id, { createId: ids("cycle") });
  assert.equal(rejected.ok, false);
  assert.ok(rejected.issues.some((issue) => issue.code === "cycle"));
  assert.equal(rejected.document, document, "failed transactions return the untouched document");

  const corrupt = structuredClone(document);
  corrupt.edges.push({ id: "cycle-edge", sourceNodeId: second.id, sourceHandle: "image", targetNodeId: first.id, targetHandle: "image:1", order: 0 });
  first.bindings = [];
  const plan = buildGeneratorExecutionPlan(corrupt, second.id);
  assert.ok(plan.issues.some((issue) => issue.code === "cycle"));
  assert.deepEqual(plan.steps, []);
});

test("a binding without its typed edge blocks execution instead of silently dropping the input", () => {
  const source = asset("lonely", "image");
  const target = node("video-generator", "target");
  target.bindings.push({ id: "orphan-binding", kind: "image", slot: 1, sourceNodeId: source.id, sourceOutputHandle: "image", role: "reference" });
  const plan = buildGeneratorExecutionPlan(emptyDocument([source, target]), target.id);
  assert.ok(plan.issues.some((issue) => issue.code === "missing-binding-edge" && issue.bindingId === "orphan-binding"));
  assert.deepEqual(plan.steps, []);
});

test("typed slots allocate the first hole, enforce 9/3/3 capacity and the combined twelve-file budget", () => {
  const video = node("video-generator", "target");
  video.bindings = [
    { id: "p1", kind: "image", slot: 1, sourceNodeId: "a", sourceOutputHandle: "image", role: "reference" },
    { id: "p3", kind: "image", slot: 3, sourceNodeId: "c", sourceOutputHandle: "image", role: "reference" },
  ];
  assert.equal(firstAvailableSlot(video.bindings, "image"), 2);
  assert.equal(firstAvailableSlot(Array.from({ length: 3 }, (_, index) => ({ id: `v${index}`, kind: "video", slot: index + 1, sourceNodeId: `s${index}`, sourceOutputHandle: "video", role: "reference" })), "video"), undefined);

  const videoSources = Array.from({ length: 4 }, (_, index) => asset(`clip-${index}`, "video"));
  let videoCapacityDocument = emptyDocument([...videoSources, node("video-generator", "video-capacity")]);
  for (let index = 0; index < 3; index += 1) videoCapacityDocument = mustConnect(videoCapacityDocument, videoSources[index].id, "video-capacity", { createId: ids(`clip-edge-${index}`) }).document;
  const fourthVideo = connectMedia(videoCapacityDocument, videoSources[3].id, "video-capacity", { createId: ids("fourth-video") });
  assert.equal(fourthVideo.ok, false);
  assert.ok(fourthVideo.issues.some((issue) => issue.code === "media-capacity-exceeded"));

  const sources = Array.from({ length: 13 }, (_, index) => asset(`asset-${index}`, index < 9 ? "image" : index < 12 ? "audio" : "video"));
  let document = emptyDocument([...sources, node("video-generator", "generator")]);
  for (let index = 0; index < 12; index += 1) document = mustConnect(document, sources[index].id, "generator", { createId: ids(`edge-${index}`) }).document;
  const thirteenth = connectMedia(document, sources[12].id, "generator", { createId: ids("thirteenth") });
  assert.equal(thirteenth.ok, false);
  assert.ok(thirteenth.issues.some((issue) => issue.code === "reference-budget-exceeded"));
  assert.equal(document.nodes.find((candidate) => candidate.id === "generator").bindings.length, 12);

  const imageGenerator = node("image-generator", "image-target");
  const audioAsset = asset("sound", "audio");
  const wrongType = connectMedia(emptyDocument([imageGenerator, audioAsset]), audioAsset.id, imageGenerator.id);
  assert.equal(wrongType.ok, false);
  assert.ok(wrongType.issues.some((issue) => issue.code === "target-rejects-media"));
});

test("connection and disconnection are immutable transactions with typed generator-output inputs", () => {
  const source = node("image-generator", "image-source");
  const target = node("video-generator", "video-target");
  const original = emptyDocument([source, target]);
  const connected = mustConnect(original, source.id, target.id, { role: "first_frame", createId: ids("txn") });
  assert.equal(original.edges.length, 0);
  assert.deepEqual(connected.edge, {
    id: "txn-2", sourceNodeId: source.id, sourceHandle: "image", targetNodeId: target.id, targetHandle: "image:1", order: 0,
  });
  assert.equal(connected.binding.kind, "image");
  assert.equal(connected.document.nodes.find((candidate) => candidate.id === target.id).configRevision, 1);

  const disconnected = disconnectMedia(connected.document, target.id, connected.binding.id);
  assert.equal(disconnected.ok, true);
  assert.deepEqual(disconnected.removedBindingIds, [connected.binding.id]);
  assert.deepEqual(disconnected.removedEdgeIds, [connected.edge.id]);
  assert.equal(disconnected.document.edges.length, 0);
  assert.equal(disconnected.document.nodes.find((candidate) => candidate.id === target.id).bindings.length, 0);
  assert.equal(disconnected.document.nodes.find((candidate) => candidate.id === target.id).configRevision, 2);
});

test("Prompt compilation replaces only stable mention tokens and preserves all other bytes", () => {
  const bindings = [
    { id: "hero", kind: "image", slot: 2, sourceNodeId: "asset", sourceOutputHandle: "image", role: "identity" },
    { id: "motion", kind: "video", slot: 1, sourceNodeId: "clip", sourceOutputHandle: "video", role: "motion" },
  ];
  const prompt = { tokens: [
    { kind: "text", text: "  Keep " },
    { kind: "mention", bindingId: "hero", label: "hero.png" },
    { kind: "text", text: " exactly.\nMotion: " },
    { kind: "mention", bindingId: "motion", label: "move.mp4" },
    { kind: "text", text: "  <Picture 9> stays literal." },
  ] };
  const compiled = compilePromptDocument(prompt, bindings);
  assert.equal(compiled.text, "  Keep <Picture 2> exactly.\nMotion: <Video 1>  <Picture 9> stays literal.");
  assert.equal(compiled.tagsByBindingId.get("hero"), "<Picture 2>");
  assert.equal(compiled.issues.length, 0);

  const dangling = compilePromptDocument({ tokens: [{ kind: "text", text: "x" }, { kind: "mention", bindingId: "missing", label: "gone" }, { kind: "text", text: "y" }] }, bindings);
  assert.equal(dangling.text, "xy");
  assert.ok(dangling.issues.some((issue) => issue.code === "dangling-mention"));
});

test("ordered inputs follow explicit edge order while retaining stable H3 slot labels", () => {
  const first = asset("first", "image");
  const second = asset("second", "image");
  const generator = node("video-generator", "video");
  let document = emptyDocument([first, second, generator]);
  document = mustConnect(document, first.id, generator.id, { slot: 2, createId: ids("first") }).document;
  document = mustConnect(document, second.id, generator.id, { slot: 1, createId: ids("second") }).document;
  const inputs = orderedGeneratorInputs(document, generator.id);
  assert.deepEqual(inputs.map((input) => [input.source.id, input.order, input.h3Tag]), [
    [first.id, 0, "<Picture 2>"],
    [second.id, 1, "<Picture 1>"],
  ]);
});

test("Output plans preserve multiple generator sources and direct propagation order", () => {
  const image = node("image-generator", "image");
  const video = node("video-generator", "video");
  const output = node("output", "output");
  image.configRevision = 1;
  image.lastSuccessfulRevision = 1;
  image.resultVersions = [{ id: "img-result", mediaKind: "image", createdAt: 2 }];
  video.configRevision = 1;
  video.lastSuccessfulRevision = 1;
  video.resultVersions = [{ id: "vid-result", mediaKind: "video", createdAt: 3 }];
  const document = emptyDocument([image, video, output]);
  document.edges = [
    { id: "v-o", sourceNodeId: video.id, sourceHandle: "video", targetNodeId: output.id, targetHandle: "input", order: 2 },
    { id: "i-o", sourceNodeId: image.id, sourceHandle: "image", targetNodeId: output.id, targetHandle: "input", order: 1 },
  ];

  assert.deepEqual(planOutputPropagation(document, video.id).map((item) => [item.outputNodeId, item.result.id]), [[output.id, "vid-result"]]);
  const collection = buildOutputCollectionPlan(document, output.id);
  assert.deepEqual(collection.sources.map((item) => [item.sourceGeneratorId, item.mediaKind, item.result.id]), [
    [image.id, "image", "img-result"],
    [video.id, "video", "vid-result"],
  ]);
});
