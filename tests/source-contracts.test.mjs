import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);
const capabilitiesPath = new URL("../app/studio-capabilities.ts", import.meta.url);
const timelinePath = new URL("../app/video-timeline.tsx", import.meta.url);
const videoProjectPath = new URL("../app/video-project.ts", import.meta.url);
const promptMentionsPath = new URL("../app/prompt-mentions.tsx", import.meta.url);
const gatewayPath = new URL("../app/api/[...path]/route.ts", import.meta.url);

test("studio uses the versioned profile and durable job API contracts", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /fetch\("\/api\/capabilities"/);
  assert.match(source, /profile_id:\s*profile\.id/);
  assert.match(source, /profile_version:\s*profile\.version/);
  assert.match(source, /profile_digest:\s*profile\.manifest_sha256/);
  assert.match(source, /request_id:\s*requestId/);
  assert.match(source, /\/api\/jobs\/\$\{encodeURIComponent\(targetJob\.id\)\}\/cancel/);
});

test("read-only prompt preview ignores stale responses and exposes the actual validation error", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const promptCompileRequestRef = useRef\(0\)/);
  assert.match(source, /requestId !== promptCompileRequestRef\.current/);
  assert.match(source, /controller\.signal\.aborted/);
  assert.match(source, /setCompileError\(error instanceof Error \? error\.message/);
  assert.match(source, /\u91cd\u65b0\u6821\u9a8c/);
  assert.match(source, /\u6700\u7ec8\u63d0\u793a\u8bcd\u6821\u9a8c\u5931\u8d25：\$\{targetCompile\.error/);
  assert.match(source, /nodeCompile\.error && <div className="prompt-compile-error"/);
  assert.match(source, /H3 \u6700\u7ec8\u63d0\u793a\u8bcd\u9884\u89c8（\u53ea\u8bfb）/);
});

test("canvas connections and restored workflows cannot submit duplicate graph edges", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /function dedupeEdges\(edges: Edge\[\]\)/);
  assert.match(source, /setEdges\(dedupeEdges\(legacyEdgesFromDocument\(document\)\)\)/);
  assert.match(source, /edge\.source === source\.id && edge\.target === target\.id\)\) return "这两个节点已经连接"/);
  assert.match(source, /const transaction = connectMedia\(document, source\.id, target\.id/);
  assert.match(source, /const transaction = connectMedia\(document, source\.id, target\.id/);
  assert.match(source, /setEdges\(legacyEdgesFromDocument\(transaction\.document\)\)/);
  assert.match(source, /const relevantEdges = dedupeEdges\(edges\.filter/);
});

test("studio serializes canonical typed graph references", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /type:\s*materialized \? "asset" : node\.kind === "video" \|\| node\.kind === "image" \? "generator"/);
  assert.match(source, /assetId:\s*node\.asset\.remoteId/);
  assert.match(source, /include_audio:\s*referenceIncludesAudio\(edges, generatorNodeId, node\.id\)/);
  assert.match(source, /targetHandle: `\$\{binding\.kind\}:\$\{binding\.slot\}`/);
  assert.match(source, /referenceIndexBySource\.has\(edge\.source\)/);
  assert.match(source, /role:\s*referenceRoleForTarget\(edges, generatorNodeId, node\.id, node\.asset\.role\)/);
  assert.match(source, /compilePromptDocument\(executionNode\.prompt, executionNode\.bindings\)/);
  assert.match(source, /new Set\(roles\)\.size !== roles\.length/);
});

test("canvas ports support visible pointer-drag connections as well as keyboard selection", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const \[connectionPointer, setConnectionPointer\] = useState<XY>/);
  assert.match(source, /function beginConnection\(event: ReactPointerEvent<HTMLButtonElement>/);
  assert.match(source, /function finishConnection\(event: ReactPointerEvent<HTMLButtonElement>/);
  assert.match(source, /onPointerDown=\{\(event\) => beginConnection\(event, node\)\}/);
  assert.match(source, /onPointerUp=\{\(event\) => finishConnection\(event, node\.id\)\}/);
  assert.match(source, /className="wire wire-preview"/);
  assert.match(source, /拖到兼容节点左侧圆点松开/);
});

test("production gateway keeps the API key server-side and streams all used methods", async () => {
  const source = await readFile(gatewayPath, "utf8");

  assert.match(source, /process\.env\.H3_STUDIO_PROXY_API_KEY/);
  assert.match(source, /headers\.set\("X-API-Key", apiKey\)/);
  assert.match(source, /"x-api-key"/);
  assert.match(source, /body:\s*BODYLESS_METHODS\.has\(request\.method\) \? undefined : request\.body/);
  for (const method of ["GET", "HEAD", "POST", "DELETE"]) {
    assert.match(source, new RegExp(`export const ${method} = proxy`));
  }
});

test("auto profile resolves to the available concrete sampling profile", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.doesNotMatch(
    source,
    /const id = profileSelection\[output\];\s*const profile = id === "auto" \? undefined/,
    "Auto must submit the concrete available profile rather than letting the backend reselect unavailable Turbo",
  );
});

test("video and image keep independent resolved profiles and parameter state", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /generatorStates.*Record<string, GeneratorRuntime>/);
  assert.match(source, /updateGenerator\(targetNodeId/);
  assert.match(source, /const nodeVideoProfile = nodeVideoProfileChoices\.find/);
  assert.match(source, /const nodeImageProfile = nodeRuntime\.profileId === "auto"/);
  assert.match(source, /<small>\{nodeImageProfile \? `\$\{nodeImageProfile\.id\}@\$\{nodeImageProfile\.version\}`/);
  assert.match(source, /<ParameterPanel kind="video"[\s\S]{0,300}profile=\{nodeVideoProfile\}/);
  assert.match(source, /<ParameterPanel kind="image"[\s\S]{0,300}profile=\{nodeImageProfile\}/);
  assert.match(source, /steps:\s*targetVideo\.steps/);
  assert.doesNotMatch(source, /sampling_mode === "turbo4" \? Number\(resolvedProfile\.defaults\.steps \?\? 4\) : videoParams\.steps/);
  assert.match(source, /sampling_mode === "base" \? 0 : targetVideo\.loraStrength/);
  assert.match(source, /Turbo LoRA 步数（4 推荐）/);
  assert.match(source, /模型强度（LoRA）/);
  assert.match(source, /LoraLoaderModelOnly\.strength_model/);
  assert.match(source, /denoise:\s*targetVideo\.denoise/);
  assert.match(source, /调度去噪比例（实验）/);
  assert.match(source, /不是 CFG 或参考权重/);
  assert.match(source, /官方模板默认 1\.00/);
  assert.doesNotMatch(source, /值越低[^\n]{0,80}保留参考内容/);
  assert.doesNotMatch(source, /video\.cfg|videoParams\.cfg/);
});

test("single generation and the timeline share the H3 17k+5 duration grid", async () => {
  const [studio, timeline, videoProject] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(timelinePath, "utf8"),
    readFile(videoProjectPath, "utf8"),
  ]);

  assert.match(videoProject, /H3_MAX_GENERATION_FRAMES = 362/);
  assert.match(videoProject, /H3_MAX_GENERATION_DURATION = H3_MAX_GENERATION_FRAMES \/ H3_GENERATION_FPS/);
  assert.match(videoProject, /H3_DURATION_OPTIONS = Array\.from/);
  assert.match(videoProject, /function h3EffectiveDuration/);
  assert.match(studio, /Array\.from\(\{ length: 15 \}/);
  assert.match(studio, /Math\.min\(H3_MAX_GENERATION_FRAMES/);
  assert.match(timeline, /profileDurationOptions\(profile\)/);
  assert.match(timeline, /value=\{h3EffectiveDuration\(request\.parameters\.duration\)\}/);
  assert.doesNotMatch(timeline, /<input type="number"[^>]+value=\{request\.parameters\.duration\}/);
});

test("bounded sampling fields allow multi-digit typing before clamping on commit", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /function BoundedNumberInput/);
  assert.match(source, /onChange=\{\(event\) => setDraft\(event\.target\.value\)\}/);
  assert.match(source, /onBlur=\{commit\}/);
  assert.match(source, /draft\.trim\(\) === "" \? Number\.NaN : Number\(draft\)/);
  assert.match(source, /<BoundedNumberInput[^>]+id="video-steps"[^>]+min=\{stepBounds\[0\]\}[^>]+max=\{stepBounds\[1\]\}[^>]+value=\{video\.steps\}/);
  assert.doesNotMatch(source, /steps: Math\.max\(stepBounds\[0\], Math\.min\(stepBounds\[1\], Number\(event\.target\.value\)\)\)/);
});

test("studio retains durable job receipts and downloadable history", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const \[jobHistory, setJobHistory\] = useState<Job\[]>\(\[\]\)/);
  assert.match(source, /mergeJobHistoryPage\(base, remoteJobs, Boolean\(cursor\), Boolean\(nextCursor\), 100, cursor\)/);
  assert.match(source, /new URLSearchParams\(\{ limit: "20", summary: "1", results: "1" \}\)/);
  assert.match(source, /parseJobHistoryCacheEnvelope\(sessionStorage\.getItem\(JOB_HISTORY_CACHE_KEY\)\)/);
  assert.match(source, /fetch\(`\/api\/jobs\?\$\{query\.toString\(\)\}`/);
  assert.match(source, /parameters:\s*serverParameters\(data\.parameters\)/);
  assert.match(source, /<ParameterSummary title="\u6765\u6e90\u4efb\u52a1\u56de\u6267" parameters=\{sourceJob\.parameters\}/);
  assert.match(source, /<TaskHistory jobs=\{jobHistory\}/);
  assert.match(source, /item\.downloadUrl && <a href=\{item\.downloadUrl\} download/);
});

test("results stay hidden until the current server dataset is verified", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const \[jobHistoryInstanceVerified, setJobHistoryInstanceVerified\] = useState\(false\)/);
  assert.match(source, /setJobHistoryInstanceVerified\(true\)/);
  assert.match(source, /trustedJobIdsRef\.current\.has\(runtime\.job\.id\)/);
  assert.match(source, /recordTrustedJob\(queued\)/);
  assert.match(source, /const completed = verified \? jobs\.filter/);
  assert.doesNotMatch(source, /const restoredJobs = Object\.values\(nextRuntimes\)/);
  assert.doesNotMatch(source, /Object\.values\(generatorStates\)\.forEach/);
  const cacheBootstrap = source.slice(source.indexOf("const cached = parseJobHistoryCacheEnvelope"), source.indexOf("void loadJobHistory().catch", source.indexOf("const cached = parseJobHistoryCacheEnvelope")));
  assert.doesNotMatch(cacheBootstrap, /jobHistoryCacheReadyRef\.current = true/);
});

test("image generation submits explicit quality dimensions for every supported ratio", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /quality:\s*"1K"/);
  assert.match(source, /imageDimensions\(targetImage\.quality, targetImage\.aspectRatio, isQwenImage21Profile\(resolvedProfile\)\)/);
  assert.match(source, /width:\s*imageSize\.width/);
  assert.match(source, /height:\s*imageSize\.height/);
  assert.match(source, /imageParams\?: ImageParams/);
  assert.match(source, /const targetImage = runtime\.imageParams \?\? DEFAULT_IMAGE_PARAMS/);
  assert.match(source, /图片正向提示词/);
  assert.match(source, /prompt:\s*outputPrompt/);
  assert.match(source, /parts:\s*outputParts/);
  assert.match(source, /Negative Prompt 不能代替正向提示词/);
  assert.match(source, /id: "image-prompt-input"/);
  assert.match(source, /generate\("image", node\.id\)/);
  assert.match(source, /imageProfileAcceptsReferenceCount\(profile, imageReferenceCount\)/);
  assert.match(source, /Z-Image Turbo/);
  assert.match(source, /Qwen-Image 2512/);
  assert.match(source, /Qwen-Image Edit 2511/);
  assert.match(source, /onFocus=\{\(\) => setSelectedId\(node\.id\)\}/);
  assert.match(source, /fixedImageSteps/);
  for (const ratio of ["16:9", "9:16", "3:4", "1:1"]) {
    assert.match(source, new RegExp(`setAspect\\("${ratio}"\\)`));
  }
});

test("profile-driven image references support ordered FLUX.2 multi-image generation without regressing legacy profiles", async () => {
  const [source, capabilities] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(capabilitiesPath, "utf8"),
  ]);

  assert.match(capabilities, /reference_contract\?: ReferenceContract/);
  assert.match(capabilities, /index_base\?: number/);
  assert.match(capabilities, /imageProfileAcceptsReferenceCount/);
  assert.match(capabilities, /min:\s*optionalMultiImage \? 0 : 1/);
  assert.match(capabilities, /max:\s*optionalMultiImage \? Math\.min\(4, STUDIO_REFERENCE_BUDGET\) : 1/);
  assert.match(source, /targetSlot \? targetSlot - 1 : connectTarget === "image" \? targetImageReferences\.length/);
  assert.match(source, /item\.referenceIndex - 1 \+ imageReferenceContract\.indexBase/);
  assert.match(source, />图\{referenceIndex\} · Image \{referenceIndex\}</);
  assert.match(source, /reference_index:\s*referenceIndexBySource\.get\(node\.id\)/);
  assert.match(source, /moveImageReference/);
  assert.match(source, /disconnectImageReference/);
  assert.match(source, /同一张远程图片不能重复作为多个参考/);
  assert.match(source, /提示词引用了图\$\{danglingPromptReference\}/);
  assert.match(source, /直接使用“图1”、“图2”/);
  assert.match(source, /图\{referenceIndex\} · Image \{referenceIndex\}/);
  assert.match(source, /Klein 不使用 Negative Prompt 或 denoise/);
  assert.match(source, /profileSupportsParameter\(resolvedProfile, "denoise"\) \? \{ denoise: targetImage\.denoise \}/);
  assert.match(source, /item\.kind === "image"[\s\S]{0,300}onAdd\(item, "image"\)/);
});

test("image LoRA and img2img controls are capability-driven and visually distinct", async () => {
  const [source, capabilities] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(capabilitiesPath, "utf8"),
  ]);

  assert.match(capabilities, /function profileSupportsParameter/);
  assert.match(source, /profileSupportsParameter\(profile, "lora_strength"\)/);
  assert.match(source, /profileSupportsParameter\(profile, "denoise"\)/);
  assert.match(source, /lora_strength: imageParams\.loraStrength/);
  assert.match(source, /sampler: flux2 \? "euler" : zImage \? "res_multistep"/);
  assert.match(source, /scheduler: flux2 \? "flux2" : zImage \|\| activeProfile\.compiler\.startsWith\("qwen_image"\) \? "simple"/);
  assert.match(source, /LoRA 模型强度/);
  assert.match(source, /img2img 重绘强度（denoise）/);
  assert.match(source, /不是底图重绘强度/);
  assert.match(source, /与上方 LoRA 模型强度互不关联/);
  assert.match(source, /仅限合法、自愿且可确认年满 18 岁的成年人内容/);
  assert.doesNotMatch(source, /models\/(?:loras|diffusion_models)\//);
});

test("an explicit single-image profile remains selectable before its img2img input is connected", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /output === "image" \|\| profile\.compiler === compiler/);
  assert.match(source, /selectedProfile === "auto" \? compatible\.find/);
  assert.match(source, /: choices\.find\(\(profile\) => profile\.id === selectedProfile\)/);
  assert.match(source, /nodeImageProfileChoices\.map\(\(profile\) => <option/);
  assert.match(source, /单图 img2img/);
  assert.match(source, /aria-label=\{imageReferenceContract\.max === 1 \? "单图底图连线"/);
});

test("original Z-Image latent img2img stays distinct from unreleased Z-Image-Edit", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /isZImageProfile\(profile\) && profileSupportsParameter\(profile, "denoise"\) \? "单图 latent img2img"/);
  assert.match(source, /const zImageLatentImg2Img = zImage && imageDenoise && profileReferencePolicy\.min === 1 && profileReferencePolicy\.max === 1/);
  assert.match(source, /imageLora = kind === "image" && profileSupportsParameter\(profile, "lora_strength"\)/);
  assert.match(source, /imageLora && <label className="control-label image-lora-control"/);
  assert.match(source, /hasImageInput && imageDenoise && <label className="control-label image-denoise-control"/);
  assert.match(source, /Z-Image Turbo latent img2img/);
  assert.match(source, /这是 latent 重绘，不是 Z-Image-Edit/);
  assert.match(source, /Z-Image-Edit · 尚未发布（不可用，不是 latent img2img）/);
  assert.match(source, /不具备 Z-Image-Edit 的指令式语义编辑能力/);
  assert.match(source, /image\?: \{ unavailable_profiles\?: UnavailableProfileCapability\[\] \}/);
  assert.match(source, /data\.image\?\.unavailable_profiles/);
  assert.match(source, /unavailableZImageEdit\?\.reason/);
});

test("asset and result rail drawers restore reusable server state without deleting assets", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /fetch\("\/api\/assets",\s*\{ cache:\s*"no-store"/);
  assert.match(source, /remoteAssetToLibraryItem/);
  assert.match(source, /addLibraryAsset/);
  assert.match(source, /aria-controls="asset-library-drawer"/);
  assert.match(source, /aria-controls="result-library-drawer"/);
  assert.match(source, /<ResultLibrary\s+jobs=\{jobHistory\}/);
  const addLibraryStart = source.indexOf("const addLibraryAsset = useCallback");
  const addLibraryEnd = source.indexOf("const importJobOutput = useCallback", addLibraryStart);
  const addLibraryBody = source.slice(addLibraryStart, addLibraryEnd);
  assert.notEqual(addLibraryStart, -1, "addLibraryAsset must exist");
  assert.notEqual(addLibraryEnd, -1, "addLibraryAsset must end before importJobOutput");
  assert.match(addLibraryBody, /remoteId:\s*item\.id/);
  assert.doesNotMatch(addLibraryBody, /fetch\(|uploadAsset/);
  const removeNodeBody = source.match(/const removeNode = useCallback\(\(node: StudioNode\) => \{([\s\S]*?)\n {2}\}, \[[^\]]*\]\);/)?.[1] ?? "";
  assert.ok(removeNodeBody, "removeNode implementation must be present");
  assert.doesNotMatch(removeNodeBody, /fetch\(|method:\s*"DELETE"/);
});

test("prompt editors provide a thumbnail @ picker backed by stable asset IDs", async () => {
  const [source, composer] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(promptMentionsPath, "utf8"),
  ]);

  assert.match(composer, /return `@\{\$\{assetId\}\}`/);
  assert.match(composer, /token\.dataset\.promptMention = promptMentionToken\(item\.id\)/);
  assert.match(composer, /token\.className = "prompt-mention-token"/);
  assert.match(composer, /className="prompt-mention-picker"/);
  assert.match(composer, /placeholder="搜索素材"/);
  assert.match(composer, /已引用/);
  assert.match(composer, /素材引用/);
  assert.match(composer, /item\.previewUrl \? <img/);
  assert.doesNotMatch(composer, /<video|mediaUrl|preload="metadata"/);
  assert.match(composer, /if \(event\.key === "@"\)/);
  assert.match(composer, /await onSelectItem\(item\)/);
  assert.match(source, /mentionItems\("video", node\.id\)/);
  assert.match(source, /mentionItems\("image", node\.id\)/);
  assert.match(source, /selectMentionItem\("video", item, node\.id\)/);
  assert.match(source, /selectMentionItem\("image", item, node\.id\)/);
  assert.match(source, /return addLibraryAsset\(library, target, targetNodeId\)/);
});

test("long-video rail mounts the durable multi-segment timeline", async () => {
  const source = await readFile(studioPath, "utf8");
  const timeline = await readFile(timelinePath, "utf8");

  assert.match(source, /railPanel === "timeline"/);
  assert.match(source, /aria-controls="video-timeline-drawer"/);
  assert.match(source, /<VideoTimeline[\s\S]*assets=\{assetLibrary\}[\s\S]*profiles=\{profiles\}/);
  assert.match(source, /onResultCreated=\{handleTimelineResultCreated\}/);
  assert.match(source, /handleTimelineResultCreated[\s\S]{0,180}loadJobHistory\(true\)/);
  assert.match(timeline, /notifyMergedResultOnce\(value, notifiedResultsRef\.current, onResultCreated\)/);
  assert.match(timeline, /notifyResultCreated\(next\)/);
});
