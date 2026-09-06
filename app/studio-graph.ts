import {
  H3_REFERENCE_BUDGET,
  H3_REFERENCE_CAPACITY,
  type CanvasDocumentV7,
  type CanvasEdge,
  type CanvasNode,
  type GeneratorNodeKind,
  type IdFactory,
  type ImageGeneratorNode,
  type MediaBinding,
  type MediaKind,
  type NodeResult,
  type OutputNode,
  type PromptDocument,
  type VideoGeneratorNode,
  createCanvasUuid,
} from "./studio-document.ts";

export type GeneratorNode = VideoGeneratorNode | ImageGeneratorNode;

export type GraphIssueCode =
  | "target-not-generator"
  | "dangling-node"
  | "cycle"
  | "source-has-no-media-output"
  | "target-rejects-media"
  | "duplicate-binding"
  | "slot-occupied"
  | "slot-out-of-range"
  | "reference-budget-exceeded"
  | "media-capacity-exceeded"
  | "binding-not-found"
  | "missing-binding-edge"
  | "dangling-mention";

export type GraphIssue = {
  code: GraphIssueCode;
  message: string;
  nodeId?: string;
  edgeId?: string;
  bindingId?: string;
};

export type GeneratorExecutionStep = {
  nodeId: string;
  kind: GeneratorNodeKind;
  action: "run" | "reuse";
  reason: "target-requested" | "missing-result" | "stale-result" | "upstream-changed" | "fresh-result";
  result?: NodeResult;
};

export type GeneratorExecutionPlan = {
  targetNodeId: string;
  nodeIds: string[];
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  topologicalNodeIds: string[];
  steps: GeneratorExecutionStep[];
  issues: GraphIssue[];
};

export type OrderedGeneratorInput = {
  binding: MediaBinding;
  edge?: CanvasEdge;
  source?: CanvasNode;
  h3Tag: string;
  order: number;
};

export type ConnectMediaOptions = {
  kind?: MediaKind;
  sourceHandle?: string;
  role?: string;
  slot?: number;
  createId?: IdFactory;
};

export type GraphTransactionResult = {
  ok: boolean;
  document: CanvasDocumentV7;
  issues: GraphIssue[];
  binding?: MediaBinding;
  edge?: CanvasEdge;
  removedBindingIds?: string[];
  removedEdgeIds?: string[];
};

export type CompiledPrompt = {
  text: string;
  tagsByBindingId: Map<string, string>;
  issues: GraphIssue[];
};

export type OutputSourcePlan = {
  outputNodeId: string;
  edgeId: string;
  order: number;
  sourceGeneratorId: string;
  mediaKind: "image" | "video";
  result?: NodeResult;
};

export type OutputCollectionPlan = {
  output: OutputNode;
  sources: OutputSourcePlan[];
};

export type DownstreamInvalidationResult = {
  document: CanvasDocumentV7;
  invalidatedNodeIds: string[];
};

function isGenerator(node: CanvasNode | undefined): node is GeneratorNode {
  return node?.kind === "video-generator" || node?.kind === "image-generator";
}

function nodeMap(document: CanvasDocumentV7): Map<string, CanvasNode> {
  return new Map(document.nodes.map((node) => [node.id, node]));
}

function outputMediaKind(node: CanvasNode | undefined, sourceHandle?: string): MediaKind | undefined {
  if (!node) return undefined;
  if (node.kind === "asset") {
    if (sourceHandle === "audio" && node.mediaKind === "video") return "audio";
    return node.mediaKind;
  }
  if (node.kind === "image-generator") return sourceHandle && sourceHandle !== "image" ? undefined : "image";
  if (node.kind === "video-generator") {
    if (sourceHandle === "audio") return "audio";
    return sourceHandle && sourceHandle !== "video" ? undefined : "video";
  }
  return undefined;
}

function outputHandle(kind: MediaKind): string {
  return kind;
}

function inputHandle(kind: MediaKind, slot: number): string {
  return `${kind}:${slot}`;
}

function h3Tag(binding: MediaBinding): string {
  const family = binding.kind === "image" ? "Picture" : binding.kind === "video" ? "Video" : "Audio";
  return `<${family} ${binding.slot}>`;
}

function nextEdgeOrder(document: CanvasDocumentV7, targetNodeId: string): number {
  const orders = document.edges.filter((edge) => edge.targetNodeId === targetNodeId).map((edge) => edge.order);
  return orders.length ? Math.max(...orders) + 1 : 0;
}

function cloneDocument(document: CanvasDocumentV7): CanvasDocumentV7 {
  return structuredClone(document);
}

function latestResult(node: GeneratorNode): NodeResult | undefined {
  if (!node.resultVersions.length) return undefined;
  return node.resultVersions.reduce((latest, result) => {
    if (result.createdAt !== undefined && (latest.createdAt === undefined || result.createdAt > latest.createdAt)) return result;
    return latest;
  }, node.resultVersions[node.resultVersions.length - 1]);
}

export function hasFreshGeneratorResult(node: GeneratorNode): boolean {
  return node.lastSuccessfulRevision === node.configRevision && Boolean(latestResult(node));
}

/**
 * Persists the effect of a newly completed generator result across later runs.
 * Only fresh downstream generators need a revision bump: a generator that is
 * already stale must not accumulate revisions when this transaction is retried.
 */
export function invalidateDownstreamGenerators(
  document: CanvasDocumentV7,
  sourceGeneratorId: string,
): DownstreamInvalidationResult {
  const nodes = nodeMap(document);
  if (!isGenerator(nodes.get(sourceGeneratorId))) return { document, invalidatedNodeIds: [] };
  const outgoing = new Map<string, CanvasEdge[]>();
  for (const edge of document.edges) outgoing.set(edge.sourceNodeId, [...(outgoing.get(edge.sourceNodeId) ?? []), edge]);
  for (const edges of outgoing.values()) edges.sort((left, right) => left.order - right.order);

  const visited = new Set([sourceGeneratorId]);
  const queue = [sourceGeneratorId];
  const downstreamGeneratorIds: string[] = [];
  while (queue.length) {
    const current = queue.shift()!;
    for (const edge of outgoing.get(current) ?? []) {
      if (visited.has(edge.targetNodeId)) continue;
      visited.add(edge.targetNodeId);
      queue.push(edge.targetNodeId);
      if (isGenerator(nodes.get(edge.targetNodeId))) downstreamGeneratorIds.push(edge.targetNodeId);
    }
  }

  const invalidatedNodeIds = downstreamGeneratorIds.filter((nodeId) => {
    const node = nodes.get(nodeId);
    return isGenerator(node) && hasFreshGeneratorResult(node);
  });
  if (!invalidatedNodeIds.length) return { document, invalidatedNodeIds: [] };

  const invalidated = new Set(invalidatedNodeIds);
  const next = cloneDocument(document);
  for (const node of next.nodes) {
    if (isGenerator(node) && invalidated.has(node.id)) node.configRevision += 1;
  }
  return { document: next, invalidatedNodeIds };
}

function reverseClosure(document: CanvasDocumentV7, targetNodeId: string): { nodeIds: Set<string>; edges: CanvasEdge[]; issues: GraphIssue[] } {
  const nodes = nodeMap(document);
  const incoming = new Map<string, CanvasEdge[]>();
  for (const edge of document.edges) incoming.set(edge.targetNodeId, [...(incoming.get(edge.targetNodeId) ?? []), edge]);
  const relevantNodeIds = new Set<string>();
  const relevantEdgeIds = new Set<string>();
  const issues: GraphIssue[] = [];
  const stack = [targetNodeId];
  while (stack.length) {
    const current = stack.pop()!;
    if (relevantNodeIds.has(current)) continue;
    relevantNodeIds.add(current);
    if (!nodes.has(current)) {
      issues.push({ code: "dangling-node", nodeId: current, message: "执行依赖指向不存在的节点。" });
      continue;
    }
    for (const edge of incoming.get(current) ?? []) {
      relevantEdgeIds.add(edge.id);
      if (!nodes.has(edge.sourceNodeId)) issues.push({ code: "dangling-node", edgeId: edge.id, nodeId: edge.sourceNodeId, message: "连线来源节点不存在。" });
      stack.push(edge.sourceNodeId);
    }
  }
  return {
    nodeIds: relevantNodeIds,
    edges: document.edges.filter((edge) => relevantEdgeIds.has(edge.id)),
    issues,
  };
}

function topologicalSort(document: CanvasDocumentV7, selectedNodeIds: Set<string>, edges: CanvasEdge[]): { order: string[]; issues: GraphIssue[] } {
  const index = new Map(document.nodes.map((node, position) => [node.id, position]));
  const indegree = new Map([...selectedNodeIds].map((id) => [id, 0]));
  const outgoing = new Map<string, CanvasEdge[]>();
  for (const edge of edges) {
    if (!selectedNodeIds.has(edge.sourceNodeId) || !selectedNodeIds.has(edge.targetNodeId)) continue;
    indegree.set(edge.targetNodeId, (indegree.get(edge.targetNodeId) ?? 0) + 1);
    outgoing.set(edge.sourceNodeId, [...(outgoing.get(edge.sourceNodeId) ?? []), edge]);
  }
  const queue = [...indegree].filter(([, degree]) => degree === 0).map(([id]) => id).sort((left, right) => (index.get(left) ?? Infinity) - (index.get(right) ?? Infinity));
  const order: string[] = [];
  while (queue.length) {
    const current = queue.shift()!;
    order.push(current);
    for (const edge of [...(outgoing.get(current) ?? [])].sort((left, right) => left.order - right.order)) {
      const degree = (indegree.get(edge.targetNodeId) ?? 0) - 1;
      indegree.set(edge.targetNodeId, degree);
      if (degree === 0) {
        queue.push(edge.targetNodeId);
        queue.sort((left, right) => (index.get(left) ?? Infinity) - (index.get(right) ?? Infinity));
      }
    }
  }
  if (order.length === selectedNodeIds.size) return { order, issues: [] };
  const cyclic = [...selectedNodeIds].filter((id) => !order.includes(id));
  return { order, issues: [{ code: "cycle", message: `依赖图存在环：${cyclic.join(" → ")}` }] };
}

function relevantInputIssues(document: CanvasDocumentV7, selectedNodeIds: Set<string>, edges: CanvasEdge[]): GraphIssue[] {
  const nodes = nodeMap(document);
  const edgeKeys = new Set(edges.map((edge) => `${edge.sourceNodeId}\u0000${edge.sourceHandle}\u0000${edge.targetNodeId}\u0000${edge.targetHandle}`));
  const issues: GraphIssue[] = [];
  for (const nodeId of selectedNodeIds) {
    const target = nodes.get(nodeId);
    if (!isGenerator(target)) continue;
    for (const binding of target.bindings) {
      const key = `${binding.sourceNodeId}\u0000${binding.sourceOutputHandle}\u0000${target.id}\u0000${inputHandle(binding.kind, binding.slot)}`;
      if (!edgeKeys.has(key)) issues.push({
        code: "missing-binding-edge", nodeId: target.id, bindingId: binding.id,
        message: `素材绑定 ${binding.id} 缺少对应连线。`,
      });
      const source = nodes.get(binding.sourceNodeId);
      if (!source) continue;
      if (outputMediaKind(source, binding.sourceOutputHandle) !== binding.kind) issues.push({
        code: "source-has-no-media-output", nodeId: source.id, bindingId: binding.id,
        message: `来源节点不能提供 ${binding.kind} 输出。`,
      });
    }
  }
  return issues;
}

export function buildGeneratorExecutionPlan(document: CanvasDocumentV7, targetNodeId: string): GeneratorExecutionPlan {
  const nodes = nodeMap(document);
  const target = nodes.get(targetNodeId);
  if (!isGenerator(target)) return {
    targetNodeId, nodeIds: [], nodes: [], edges: [], topologicalNodeIds: [], steps: [],
    issues: [{ code: "target-not-generator", nodeId: targetNodeId, message: "只有图片或视频生成节点可以运行。" }],
  };
  const closure = reverseClosure(document, targetNodeId);
  const topology = topologicalSort(document, closure.nodeIds, closure.edges);
  const orderedNodes = topology.order.map((id) => nodes.get(id)).filter((node): node is CanvasNode => Boolean(node));
  const issues = [...closure.issues, ...topology.issues, ...relevantInputIssues(document, closure.nodeIds, closure.edges)];
  const steps: GeneratorExecutionStep[] = [];
  if (!issues.length) {
    const dirtyGenerators = new Set<string>();
    const incomingGeneratorEdges = new Map<string, CanvasEdge[]>();
    for (const edge of closure.edges) {
      if (!isGenerator(nodes.get(edge.sourceNodeId))) continue;
      incomingGeneratorEdges.set(edge.targetNodeId, [...(incomingGeneratorEdges.get(edge.targetNodeId) ?? []), edge]);
    }
    for (const node of orderedNodes) {
      if (!isGenerator(node)) continue;
      if (node.id === targetNodeId) {
        dirtyGenerators.add(node.id);
        steps.push({ nodeId: node.id, kind: node.kind, action: "run", reason: "target-requested" });
        continue;
      }
      const result = latestResult(node);
      if (!result) {
        dirtyGenerators.add(node.id);
        steps.push({ nodeId: node.id, kind: node.kind, action: "run", reason: "missing-result" });
        continue;
      }
      if (!hasFreshGeneratorResult(node)) {
        dirtyGenerators.add(node.id);
        steps.push({ nodeId: node.id, kind: node.kind, action: "run", reason: "stale-result" });
        continue;
      }
      const upstreamWillChange = (incomingGeneratorEdges.get(node.id) ?? []).some((edge) => dirtyGenerators.has(edge.sourceNodeId));
      if (upstreamWillChange) {
        dirtyGenerators.add(node.id);
        steps.push({ nodeId: node.id, kind: node.kind, action: "run", reason: "upstream-changed" });
        continue;
      }
      steps.push({ nodeId: node.id, kind: node.kind, action: "reuse", reason: "fresh-result", result });
    }
  }
  return {
    targetNodeId,
    nodeIds: orderedNodes.map((node) => node.id),
    nodes: orderedNodes,
    edges: [...closure.edges].sort((left, right) => left.order - right.order),
    topologicalNodeIds: topology.order,
    steps,
    issues,
  };
}

export function firstAvailableSlot(bindings: MediaBinding[], kind: MediaKind): number | undefined {
  const occupied = new Set(bindings.filter((binding) => binding.kind === kind).map((binding) => binding.slot));
  for (let slot = 1; slot <= H3_REFERENCE_CAPACITY[kind]; slot += 1) if (!occupied.has(slot)) return slot;
  return undefined;
}

export function orderedGeneratorInputs(document: CanvasDocumentV7, targetNodeId: string): OrderedGeneratorInput[] {
  const nodes = nodeMap(document);
  const target = nodes.get(targetNodeId);
  if (!isGenerator(target)) return [];
  const edgeByInput = new Map(document.edges
    .filter((edge) => edge.targetNodeId === targetNodeId)
    .map((edge) => [`${edge.sourceNodeId}\u0000${edge.sourceHandle}\u0000${edge.targetHandle}`, edge]));
  return target.bindings.map((binding, index) => {
    const edge = edgeByInput.get(`${binding.sourceNodeId}\u0000${binding.sourceOutputHandle}\u0000${inputHandle(binding.kind, binding.slot)}`);
    return { binding, edge, source: nodes.get(binding.sourceNodeId), h3Tag: h3Tag(binding), order: edge?.order ?? Number.MAX_SAFE_INTEGER - target.bindings.length + index };
  }).sort((left, right) => left.order - right.order || left.binding.slot - right.binding.slot);
}

function pathExists(document: CanvasDocumentV7, sourceId: string, targetId: string): boolean {
  const outgoing = new Map<string, string[]>();
  for (const edge of document.edges) outgoing.set(edge.sourceNodeId, [...(outgoing.get(edge.sourceNodeId) ?? []), edge.targetNodeId]);
  const seen = new Set<string>();
  const stack = [sourceId];
  while (stack.length) {
    const current = stack.pop()!;
    if (current === targetId) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(outgoing.get(current) ?? []));
  }
  return false;
}

export function connectMedia(
  document: CanvasDocumentV7,
  sourceNodeId: string,
  targetNodeId: string,
  options: ConnectMediaOptions = {},
): GraphTransactionResult {
  const nodes = nodeMap(document);
  const source = nodes.get(sourceNodeId);
  const target = nodes.get(targetNodeId);
  const issues: GraphIssue[] = [];
  if (!isGenerator(target)) issues.push({ code: "target-not-generator", nodeId: targetNodeId, message: "媒体输入只能连接到生成节点。" });
  const derivedKind = outputMediaKind(source, options.sourceHandle);
  const kind = options.kind ?? derivedKind;
  if (!source || !kind || !derivedKind || kind !== derivedKind) issues.push({ code: "source-has-no-media-output", nodeId: sourceNodeId, message: "来源节点没有匹配的媒体输出。" });
  if (isGenerator(target) && target.kind === "image-generator" && kind && kind !== "image") issues.push({ code: "target-rejects-media", nodeId: targetNodeId, message: "图片生成节点只接受图片输入。" });
  if (isGenerator(target) && kind && target.bindings.some((binding) => binding.sourceNodeId === sourceNodeId && binding.sourceOutputHandle === (options.sourceHandle ?? outputHandle(kind)))) {
    issues.push({ code: "duplicate-binding", nodeId: targetNodeId, message: "该媒体输出已经连接到此生成节点。" });
  }
  if (isGenerator(source) && isGenerator(target) && pathExists(document, target.id, source.id)) issues.push({ code: "cycle", nodeId: target.id, message: "此连接会形成生成依赖环。" });
  if (!isGenerator(target) || !kind || issues.length) return { ok: false, document, issues };
  const referenceFiles = new Set(target.bindings.map((binding) => binding.sourceNodeId)).size;
  if (referenceFiles >= H3_REFERENCE_BUDGET) return { ok: false, document, issues: [{ code: "reference-budget-exceeded", nodeId: target.id, message: `每个生成节点最多绑定 ${H3_REFERENCE_BUDGET} 个参考文件。` }] };
  const capacity = H3_REFERENCE_CAPACITY[kind];
  const slot = options.slot ?? firstAvailableSlot(target.bindings, kind);
  if (slot === undefined) return { ok: false, document, issues: [{ code: "media-capacity-exceeded", nodeId: target.id, message: `${kind} 输入最多 ${capacity} 个。` }] };
  if (!Number.isInteger(slot) || slot < 1 || slot > capacity) return { ok: false, document, issues: [{ code: "slot-out-of-range", nodeId: target.id, message: `${kind} 槽位必须在 1..${capacity}。` }] };
  if (target.bindings.some((binding) => binding.kind === kind && binding.slot === slot)) return { ok: false, document, issues: [{ code: "slot-occupied", nodeId: target.id, message: `${kind} ${slot} 已被占用。` }] };

  const createId = options.createId ?? createCanvasUuid;
  const binding: MediaBinding = {
    id: createId(), kind, slot, sourceNodeId, sourceOutputHandle: options.sourceHandle ?? outputHandle(kind), role: options.role ?? "reference",
  };
  const edge: CanvasEdge = {
    id: createId(), sourceNodeId, sourceHandle: binding.sourceOutputHandle, targetNodeId, targetHandle: inputHandle(kind, slot), order: nextEdgeOrder(document, targetNodeId),
  };
  const next = cloneDocument(document);
  const nextTarget = next.nodes.find((node) => node.id === targetNodeId);
  if (!isGenerator(nextTarget)) return { ok: false, document, issues: [{ code: "target-not-generator", nodeId: targetNodeId, message: "目标生成节点已不存在。" }] };
  nextTarget.bindings.push(binding);
  nextTarget.configRevision += 1;
  next.edges.push(edge);
  return { ok: true, document: next, issues: [], binding, edge };
}

export function disconnectMedia(document: CanvasDocumentV7, targetNodeId: string, bindingId: string): GraphTransactionResult {
  const target = document.nodes.find((node) => node.id === targetNodeId);
  if (!isGenerator(target)) return { ok: false, document, issues: [{ code: "target-not-generator", nodeId: targetNodeId, message: "目标不是生成节点。" }] };
  const binding = target.bindings.find((candidate) => candidate.id === bindingId);
  if (!binding) return { ok: false, document, issues: [{ code: "binding-not-found", nodeId: targetNodeId, bindingId, message: "素材绑定不存在。" }] };
  const removeIds = new Set([binding.id, ...(binding.pairedBindingId ? [binding.pairedBindingId] : [])]);
  const removedBindings = target.bindings.filter((candidate) => removeIds.has(candidate.id));
  for (const paired of removedBindings) if (paired.pairedBindingId) removeIds.add(paired.pairedBindingId);
  const removedHandles = new Set(removedBindings.map((candidate) => `${candidate.sourceNodeId}\u0000${candidate.sourceOutputHandle}\u0000${inputHandle(candidate.kind, candidate.slot)}`));
  const removedEdges = document.edges.filter((edge) => edge.targetNodeId === targetNodeId && removedHandles.has(`${edge.sourceNodeId}\u0000${edge.sourceHandle}\u0000${edge.targetHandle}`));
  const next = cloneDocument(document);
  const nextTarget = next.nodes.find((node) => node.id === targetNodeId);
  if (!isGenerator(nextTarget)) return { ok: false, document, issues: [{ code: "target-not-generator", nodeId: targetNodeId, message: "目标生成节点已不存在。" }] };
  nextTarget.bindings = nextTarget.bindings.filter((candidate) => !removeIds.has(candidate.id));
  nextTarget.configRevision += 1;
  const removedEdgeIds = new Set(removedEdges.map((edge) => edge.id));
  next.edges = next.edges.filter((edge) => !removedEdgeIds.has(edge.id));
  return { ok: true, document: next, issues: [], removedBindingIds: [...removeIds], removedEdgeIds: [...removedEdgeIds] };
}

export function compilePromptDocument(prompt: PromptDocument, bindings: MediaBinding[]): CompiledPrompt {
  const bindingMap = new Map(bindings.map((binding) => [binding.id, binding]));
  const tagsByBindingId = new Map<string, string>();
  const issues: GraphIssue[] = [];
  let text = "";
  for (const token of prompt.tokens) {
    if (token.kind === "text") {
      text += token.text;
      continue;
    }
    const binding = bindingMap.get(token.bindingId);
    if (!binding) {
      issues.push({ code: "dangling-mention", bindingId: token.bindingId, message: `Prompt 中的素材“${token.label}”已断开。` });
      continue;
    }
    const tag = h3Tag(binding);
    tagsByBindingId.set(binding.id, tag);
    text += tag;
  }
  return { text, tagsByBindingId, issues };
}

export function planOutputPropagation(document: CanvasDocumentV7, sourceGeneratorId: string): OutputSourcePlan[] {
  const nodes = nodeMap(document);
  const source = nodes.get(sourceGeneratorId);
  if (!isGenerator(source)) return [];
  const media = source.kind === "video-generator" ? "video" : "image";
  const result = latestResult(source);
  return document.edges
    .filter((edge) => edge.sourceNodeId === sourceGeneratorId && nodes.get(edge.targetNodeId)?.kind === "output")
    .sort((left, right) => left.order - right.order)
    .map((edge) => ({ outputNodeId: edge.targetNodeId, edgeId: edge.id, order: edge.order, sourceGeneratorId, mediaKind: media, result }));
}

export function buildOutputCollectionPlan(document: CanvasDocumentV7, outputNodeId: string): OutputCollectionPlan | undefined {
  const nodes = nodeMap(document);
  const output = nodes.get(outputNodeId);
  if (output?.kind !== "output") return undefined;
  const sources = document.edges
    .filter((edge) => edge.targetNodeId === outputNodeId)
    .sort((left, right) => left.order - right.order)
    .flatMap((edge): OutputSourcePlan[] => {
      const source = nodes.get(edge.sourceNodeId);
      if (!isGenerator(source)) return [];
      return [{
        outputNodeId, edgeId: edge.id, order: edge.order, sourceGeneratorId: source.id,
        mediaKind: source.kind === "video-generator" ? "video" : "image",
        result: latestResult(source),
      }];
    });
  return { output, sources };
}
