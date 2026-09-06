import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);
const cssPath = new URL("../app/globals.css", import.meta.url);

test("generation configuration lives inside generator nodes without a standalone prompt node or inspector", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const CREATABLE_NODE_KINDS: CoreNodeKind\[\] = \["video", "image", "output"\]/);
  assert.doesNotMatch(source, /kind:\s*"prompt"/);
  assert.doesNotMatch(source, /<aside className=\{`inspector/);
  assert.match(source, /node\.kind === "video"[\s\S]{0,900}<PromptEditor/);
  assert.match(source, /node\.kind === "video"[\s\S]{0,3000}<VideoDirectorControls/);
  assert.match(source, /node\.kind === "video"[\s\S]{0,6000}<ParameterPanel kind="video"/);
  assert.match(source, /node\.kind === "image"[\s\S]{0,5000}<ParameterPanel kind="image"/);
  assert.match(source, /H3 最终提示词预览（只读）/);
});

test("video node exposes the official H3 slot capacities and separates mode from sampling", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /\{ kind: "image", count: 9, label: "Picture" \}/);
  assert.match(source, /\{ kind: "video", count: 3, label: "Video" \}/);
  assert.match(source, /\{ kind: "audio", count: 3, label: "Audio" \}/);
  assert.match(source, /已用 \{usedCount\}\/\{budget\}/);
  assert.match(source, /采样方案/);
  assert.match(source, /Turbo4 · 4 步蒸馏 LoRA/);
  assert.match(source, /Base20 Direct · 优先成片/);
});

test("canvas supports repeatable UUID nodes and a 25 to 200 percent pan zoom viewport", async () => {
  const [source, css] = await Promise.all([readFile(studioPath, "utf8"), readFile(cssPath, "utf8")]);

  assert.match(source, /const id = `\$\{kind\}-\$\{crypto\.randomUUID\(\)\}`/);
  assert.doesNotMatch(source, /同类核心节点只能创建一个/);
  assert.match(source, /const MIN_ZOOM = 0\.25/);
  assert.match(source, /const MAX_ZOOM = 2/);
  assert.match(source, /function handleCanvasWheel/);
  assert.match(source, /function startCanvasPan/);
  assert.match(source, /translate3d\(\$\{viewport\.x\}px, \$\{viewport\.y\}px, 0\) scale\(\$\{viewport\.zoom\}\)/);
  assert.match(source, /适配全部/);
  assert.match(source, /canvasPointFromClient\(canvas, event\.clientX, event\.clientY\)/);
  assert.match(css, /transform-origin:\s*0 0/);
  assert.match(css, /will-change:\s*transform/);
});

test("generator prompt, parameters, profile, job and results are isolated by node id", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /generatorStates.*Record<string, GeneratorRuntime>/);
  assert.match(source, /updateGenerator\(node\.id/);
  assert.match(source, /generate\("video", node\.id\)/);
  assert.match(source, /generate\("image", node\.id\)/);
  assert.match(source, /setJobForNode\(targetNodeId/);
  assert.match(source, /resultVersions: \[result,/);
  assert.match(source, /cancelJob\(node\.id\)/);
});

test("V7 document restore migrates with backup and typed slot selection targets the clicked generator", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /raw \? parseCanvasDocument\(raw\) : undefined/);
  assert.match(source, /restored\?\.backup[\s\S]{0,120}localStorage\.setItem\(restored\.backup\.key/);
  assert.match(source, /serializeCanvasDocument\(snapshot\)/);
  assert.match(source, /runtimesFromDocument\(document\)/);
  assert.match(source, /assetPickerTarget.*nodeId: string; media: MediaKind; slot: number/);
  assert.match(source, /addLibraryAsset\(item, targetKind === "image" \? "image" : "video", assetPickerTarget\.nodeId, assetPickerTarget\.slot\)/);
  assert.match(source, /targetConnectedAssets\.length >= H3_REFERENCE_BUDGET/);
});

test("image to video execution uses the V7 graph plan and materializes upstream output", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /connectMedia\(document, source\.id, target\.id/);
  assert.match(source, /disconnectMedia\(document, target\.id, binding\.id\)/);
  assert.match(source, /compilePromptDocument\(executionNode\.prompt, executionNode\.bindings\)/);
  assert.match(source, /const plan = buildGeneratorExecutionPlan\(document, nodeId\)/);
  assert.match(source, /for \(const step of plan\.steps\)/);
  assert.match(source, /await runGeneratorNode\(stepOutput, step\.nodeId, materializedAssets/);
  assert.match(source, /materializedAssets\[step\.nodeId\] = await materializeJobOutput\(jobId\)/);
  assert.match(source, /graphSnapshot\(output, nodeId, materializedAssets\)/);
});

test("typed bindings survive the legacy canvas adapter and paired soundtracks stay explicit", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /binding_id\?: string; paired_audio_binding_id\?: string/);
  assert.match(source, /binding_id: binding\.id/);
  assert.match(source, /paired_audio_binding_id: binding\.pairedBindingId/);
  assert.match(source, /targetHandle: `\$\{binding\.kind\}:\$\{binding\.slot\}`/);
  assert.match(source, /lastSuccessfulRevision: runtime\.lastSuccessfulRevision/);
  assert.doesNotMatch(source, /lastSuccessfulRevision:\s*runtime\.job\.status/);
});

test("sampling restores as the two product presets and Output collects every connected generator", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /profileId: node\.config\.samplingPreset/);
  assert.match(source, /value="turbo4"/);
  assert.match(source, /value="base20"/);
  assert.match(source, /buildOutputCollectionPlan\(/);
  assert.match(source, /sourceGeneratorIds\.map\(\(sourceId\)/);
});

test("paired video soundtracks occupy Audio slots but count once in the mixed file budget", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.doesNotMatch(source, /\}\)\.slice\(0, 6\)/);
  assert.match(source, /const usedCount = new Set\(bindings\.map\(\(binding\) => binding\.sourceNodeId\)\)\.size/);
  assert.match(source, /group\.kind === "audio" && asset\?\.media === "video"/);
  assert.match(source, /updateReferenceAudio\(node\.id, sourceNodeId, false\)/);
  assert.match(source, /referenceIncludesAudio\(edges, node\.id, item\.id\)/);
  assert.doesNotMatch(source, /asset\.includeAudio/);
  assert.doesNotMatch(source, /update\(nodeId, \{ includeAudio:/);
  assert.doesNotMatch(source, /target\.bindings\.length >= 6/);
  assert.match(source, /new Set\(targetBindings\.map\(\(binding\) => binding\.sourceNodeId\)\)\.size > H3_REFERENCE_BUDGET/);
  assert.match(source, /item\.id === edge\.id[\s\S]{0,120}include_audio: enabled/);
});

test("graph edits invalidate only the target revision and internal dependency media stays out of Assets", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /updateGenerator\(resolvedTargetId, \(current\) => \(\{ \.\.\.current, configRevision: current\.configRevision \+ 1 \}\)\)/);
  assert.match(source, /for \(const targetId of affectedTargetIds\) updateGenerator\(targetId/);
  assert.match(source, /updateGenerator\(target\.id, \(current\) => \(\{ \.\.\.current, configRevision: current\.configRevision \+ 1 \}\)\)/);
  assert.match(source, /visibility: "internal"/);
  const internalMaterialize = source.slice(source.indexOf("const materializeJobOutput"), source.indexOf("const saveResultToAssets"));
  assert.doesNotMatch(internalMaterialize, /setAssetLibrary|setSavedResultAssets/);
  assert.match(source, /reason: "upstream-changed"|step\.action === "run"/);
});

test("a successful generator persistently invalidates every downstream generator", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /invalidateDownstreamGenerators\(canvasDocumentFromState\(nodes, edges, generatorStatesRef\.current, viewport\), sourceNodeId\)/);
  assert.match(source, /for \(const nodeId of transaction\.invalidatedNodeIds\)/);
  assert.match(source, /next\[nodeId\] = \{ \.\.\.runtime, configRevision: revision \}/);
  assert.equal(source.match(/persistDownstreamInvalidation\((?:nodeId|targetNodeId)\)/g)?.length, 2);
});
