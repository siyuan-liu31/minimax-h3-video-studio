# MiniMax H3 Video Studio LLM Wiki

> 最后校准：2026-09-23（Asia/Shanghai，本地源码与全套测试；已核对开发机 ComfyUI 0.37 的 Qwen-Image 2.1 原生节点与 H3 必需节点）。面向后续开发 Agent 的代码地图；具体发布版本以 Git 和开发机 `current` 软链接为准。实现事实优先级：源码与测试 > capability/API 回执 > 本文 > 历史 evidence 文档。

## 1. 先看这里

MiniMax H3 Video Studio 是一个 React/Vinext 前端加 Python 标准库 API 的单机创作工作台。浏览器保存节点画布和多个画布标签；服务端保存资产、任务、派生媒体与长视频项目，并把受约束请求编译为 ComfyUI 工作流。

品牌名已更新，但兼容标识不变：CLI 仍为 `h3ctl`，环境变量仍使用 `H3_STUDIO_*`，既有 `h3-studio` 数据路径、API 类名与浏览器持久化键不做迁移。

```text
Browser :3013
  scripts/start.mjs + scripts/gateway.mjs
    ├─ 页面 -> Vinext :3014
    └─ /api/* -> Python API :6020
                       └─ GPU Resource Manager (single-card FIFO lease)
                          ├─ ComfyUI :6009（本次 Qwen-Image 2.1 部署目标；旧 :6006 在切换前保持运行）
                          └─ Vevo2 / YingMusic persistent worker
```

快速定位：

| 想修改的能力 | 首要文件 | 主要回归测试 |
| --- | --- | --- |
| 画布交互、节点 UI、生成轮询、结果抽屉 | `app/studio.tsx`, `app/globals.css` | `tests/canvas-interactions-contract.test.mjs`, `tests/studio-performance-interactions.test.mjs`, `tests/result-*.test.mjs` |
| 前端中英文切换与持久化 | `app/ui-language.ts`, `app/studio.tsx` | `tests/ui-language.test.mjs` |
| 画布持久化、迁移、节点合同 | `app/studio-document.ts`, `app/studio-workspace.ts` | `tests/studio-document.test.mjs`, `tests/cycle20-infinite-*.test.mjs` |
| 连线、引用编号、执行依赖 | `app/studio-graph.ts` | `tests/studio-graph.test.mjs` |
| H3 T2V/I2V/FL2V/R2V/V2V/RV2V | `app/studio-video-mode.ts`, `server/workflows.py` | `tests/studio-video-mode.test.mjs`, `server/tests/test_workflows.py` |
| Prompt 与 `@素材` 标签 | `app/prompt-mentions.tsx`, `app/studio-prompt.ts`, `server/prompting.py` | `tests/studio-prompt.test.mjs`, `server/tests/test_prompting.py` |
| Profile、模型可用性、参数边界 | `app/studio-capabilities.ts`, `server/profiles.py`, `server/comfy.py` | `tests/studio-capabilities.test.mjs`, `server/tests/test_profiles.py`, `server/tests/test_comfy.py` |
| ComfyUI 工作流图 | `server/workflows.py` | `server/tests/test_workflows.py`, `tests/source-contracts.test.mjs` |
| 资产库、缩略图、剪辑派生 | `app/studio-library.ts`, `server/storage.py`, `server/media.py` | `tests/studio-library.test.mjs`, `server/tests/test_media_library.py` |
| H3 参考视频低 Token 预处理与风险策略 | `server/h3_reference.py`, `server/media.py`, `server/app.py` | `server/tests/test_h3_reference.py`, `tests/studio-library.test.mjs` |
| 普通任务历史、预览、下载、删除 | `app/studio-history.ts`, `app/result-preview.ts`, `server/app.py` | `tests/studio-history.test.mjs`, `tests/result-preview.test.mjs`, `server/tests/test_app.py` |
| H3 Base latent 断点续采 | `server/checkpoints.py`, `server/workflows.py`, `server/profiles.py` | `server/tests/test_checkpoints.py`, `tests/studio-history.test.mjs` |
| 长视频模型与 UI | `app/video-project.ts`, `app/video-timeline.tsx`, `app/video-director-*.tsx` | `tests/video-timeline*.test.mjs`, `tests/video-director-model.test.mjs` |
| 复刻工坊 | `app/replication-*.ts*`, `server/replication.py` | `tests/replication-workshop.test.mjs`, `server/tests/test_replication.py` |
| 长视频执行、续接、合并 | `server/video_projects.py` | `server/tests/test_video_projects.py` |
| 音色转换、话筒录音与换声 Worker | `app/voice-studio.tsx`, `app/microphone-recorder.tsx`, `app/microphone-audio.ts`, `app/voice-studio-api.ts`, `server/voice.py`, `server/voice_worker.py` | `tests/microphone-audio.test.mjs`, `tests/voice-studio.test.mjs`, `server/tests/test_voice.py` |
| GPU 独占租约、驻留模型和队列 | `server/gpu_resources.py`, `server/comfy_tasks.py` | `server/tests/test_gpu_resources.py`, `server/tests/test_comfy_tasks.py` |
| 启动、网关、远端运维 | `scripts/h3studio.py`, `scripts/start.mjs`, `scripts/gateway.mjs` | `scripts/ops/tests/test_h3studio.py`, `tests/gateway.test.mjs` |
| 本地抖音解析、下载与 Swagger API | `cli/internal/douyin/`, `cli/internal/command/douyin.go` | `cli/internal/douyin/*_test.go`, `cli/internal/command/command_test.go` |

## 2. 目录与入口

```text
app/
  page.tsx                     页面入口，挂载 Studio
  studio.tsx                   主画布与大部分用户交互（当前最大前端文件）
  voice-studio.tsx             侧栏换声工作区、音频拖入/选择/试听、任务状态
  voice-studio-api.ts          换声前端 API 合同、回执解析和上传格式预检
  microphone-recorder.tsx      浏览器话筒录音、预览、显式上传与权限/生命周期清理
  microphone-audio.ts          浏览器录音解码并封装为服务端接受的 PCM16 WAV
  studio-document.ts           CanvasDocument V7、迁移、Profile 解析
  studio-graph.ts              类型化连线、依赖计划、Prompt 标签编号
  studio-workspace.ts          多画布标签和 localStorage 原子提交
  studio-video-mode.ts         Director 模式合同与标签映射
  video-project.ts             长视频纯数据模型、校验和运行计划
  replication-project.ts       复刻工坊前端版本合同与规划 API
  replication-workshop.tsx     15–60 秒复刻表单、计划、执行与成片
  video-timeline.tsx           长视频抽屉编排
  video-director-workspace.tsx 监视器、时间线与分镜编辑组件
  api/[...path]/route.ts        开发/RSC 环境的 API 代理兼容入口

server/
  __main__.py                  Python API 进程入口
  app.py                       HTTP 路由、Runtime 组装、普通任务生命周期
  workflows.py                 请求解析、Prompt/工作流编译、工作流证据
  profiles.py                  内置/外部 Profile 注册表与声明校验
  comfy.py                     ComfyUI HTTP、能力检测、队列、取消、内存治理
  comfy_tasks.py               把 Comfy prompt 完整生命期绑定到 GPU 租约
  gpu_resources.py             Comfy/换声统一 FIFO 调度、驻留、释放与显存状态
  voice.py                     换声持久任务、外部 Worker 进程与能力检测
  voice_worker.py              Vevo2 FM-only / YingMusic 分离-转换-重混进程入口
  storage.py                   JSON 元数据与资产持久化
  media.py                     ffmpeg 派生、缩略图、派生资产生命周期
  media_tasks.py               H3 参考预处理后台任务、进度、取消与重启回执
  h3_reference.py              H3 参考尺寸、Token 估算与 sm120/Sage 安全策略
  checkpoints.py               最新 checkpoint、TTL/GC、续采身份校验与 staging
  motion_context.py            长视频 Motion Context latent 原子存储、完整性校验与回收
  character_migration.py       人物迁移版本化 recipe、24 FPS 分窗、Prompt 绑定与存储预检
  replication.py               复刻工坊 `h3.replication/v1` recipe、镜头分段与 Prompt 编译
  video_projects.py            长视频项目执行器、续接、停止、合并、恢复
  prompting.py                 H3 Prompt 标签替换及 FL/Ref 模板编译
  security.py                  ID、文件名、路径与媒体签名安全边界

scripts/
  h3studio.py                  doctor/install/start 的唯一推荐管理入口
  start.mjs                    生产前端子进程与网关生命周期
  gateway.mjs                  同源网关；服务端注入 API Key，不暴露给浏览器

cli/
  cmd/h3ctl/                   Go CLI 进程入口
  internal/api/                短连接 HTTP、流式上传、原子下载和 API 错误
  internal/command/            人类命令树与 --help（薄层）
  internal/douyin/             本地 yt-dlp 适配、缓存任务与回环 Swagger API
  internal/operation/          Agent/workflow 可复用原子能力
  internal/resource/          file:/asset:/job:/media:/h3:// locator
  internal/contract,output/    版本化 JSON/JSONL 回执与稳定退出码

skills/
  h3-ref2va-prompt-compiler/   项目附带的 H3 提示词编译 Skill
  h3-character-dialogue-video/ 人物参考图与对白生成有声视频 Skill
  h3-dance-replication/        多人、编队或换背景的舞蹈复刻 Skill
  h3-character-migration/      人物迁移规划、生成、恢复与交付 Skill

tests/                         Node 合同/渲染/回归测试
server/tests/                  Python API、工作流、存储与长视频测试
CHANGELOG.md                   用户可见版本变化；未部署内容放在 Unreleased
```

### 2.1 本地抖音工具边界

`h3ctl douyin parse|download|serve` 只在运行 CLI 的本机执行，不创建 H3
HTTP 客户端，也不打开当前 SSH context。`internal/douyin` 是受控的 `yt-dlp`
进程适配层：只接受精确白名单上的 Douyin HTTPS host，用 `--` 终止参数解析，
限制子进程输出和运行时间，并只在用户显式提供 `--cookies-from-browser`
时读取浏览器会话。原始签名视频下载 URL 不进入解析回执，Cookie 内容不落盘。

`douyin serve` 是 CLI 内部的独立 HTTP 服务，不并入 Python `server/app.py` 的 H3 API
合同。它只允许监听 loopback，对 `POST /api/parse` 做请求体上限、IP 限流、
URL 去重、有界并发和 TTL 缓存；`GET /api/tasks/{id}` 不暴露本地路径，
`GET /api/download/{token}` 只能访问受管缓存目录内的限时文件。`/docs` 是 Swagger UI，
`/openapi.json` 是实时 OpenAPI 合同。改动路由、任务字段或过期语义时，必须同步修改
OpenAPI、`docs/cli.md` 和 API 测试。

## 3. 前端状态模型

### 3.1 当前存在两层表示

`app/studio.tsx` 为兼容现有 UI，仍使用轻量 `StudioNode`、`Edge`、`GeneratorRuntime`；持久化和图算法使用 `CanvasDocumentV7`。关键转换函数集中在 `studio.tsx`：

- `runtimesFromDocument`
- `legacyNodesFromDocument`
- `legacyEdgesFromDocument`
- `canvasDocumentFromState`

修改节点、连线、角色或结果字段时，必须检查双向转换和刷新恢复，不能只改 React 状态。长期方向是让 V7 文档成为唯一运行时来源，但在完成迁移前不要删除兼容层。

### 3.2 CanvasDocument V7

定义在 `app/studio-document.ts`：

- 节点：`asset`、`video-generator`、`image-generator`、`output`
- `MediaBinding` 保存媒体类型、槽位、来源、输出 handle 和工作流 `role`
- `CanvasEdge` 保存拓扑和顺序；Prompt mention 绑定稳定的 `bindingId`
- `configRevision` / `lastSuccessfulRevision` 判断结果是否过期
- 画布约束：24 FPS、124–362 帧；[MiniMax H3 官方输入规格](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/README.zh-CN.md#模型变体和输入规格)规定 Ref2VA 类型容量为 Picture 9 / Video 3 / Audio 3，混合文件合计最多 12 个；同一视频启用配对音轨会同时占 Video/Audio 槽，但文件总量只计一次

多个画布由 `app/studio-workspace.ts` 包装为 `CanvasWorkspaceV1`，保存在：

- `h3-studio-canvas-workspace-v1`
- 恢复备份 `h3-studio-canvas-workspace-v1-backup`
- 旧单画布键由 `studio-document.ts` 迁移

### 3.3 界面语言

Studio 首次访问默认英文，用户可在顶栏切换 English / 中文；选择保存在浏览器本地存储键
`h3-studio-ui-language-v1`，不会进入画布文档或服务端合同。视频缩略图与播放器依据服务端
宽高和旋转元数据识别显示方向，并使用完整画面适配；资产库的视频播放按钮只在用户点击后
加载媒体字节并立即播放。结果抽屉采用同样的惰性播放合同：普通任务与剪辑派生视频的中央按钮控制实际播放/暂停，
紧凑视频卡片不启用浏览器原生 controls，避免与中央按钮重复；点击画面或中央按钮都可切换状态，
完整输出节点播放器仍保留原生 controls。资产库与结果抽屉的音频卡片也只在首次点击后加载媒体，并根据真实播放事件同步播放/暂停
图标。“在画布预览”保持为独立动作，打开抽屉不会批量请求视频或音频媒体。`app/ui-language.ts`
只翻译登记过的产品文案和受控动态模板，通过 `MutationObserver` 覆盖延迟加载的抽屉、
任务进度和长视频组件；未知文本保持原样，`pre`、`code`、`textarea` 及
`data-i18n-ignore` / `translate=no` 子树不会被改写，因此用户 Prompt 和代码示例不参与
界面翻译。SSR 的 `<html lang>` 与 Metadata 默认英文，首屏在客户端本地化完成前隐藏，
避免先显示中文再闪换英文。新增用户可见文案时应同步登记英文副本并扩展
`tests/ui-language.test.mjs` 的关键路径覆盖。

### 3.4 连线和标签

`app/studio-graph.ts` 是类型化连线的事实来源：

- `connectMedia` / `disconnectMedia` 修改 binding 和 edge
- `orderedGeneratorInputs` 决定稳定顺序
- `compilePromptDocument` 把 binding 编译为 `<Picture N>` / `<Video N>` / `<Audio N>`
- `buildGeneratorExecutionPlan` 计算上游生成器的运行或复用
- `invalidateDownstreamGenerators` 在上游新结果产生后使下游失效

注意：`first_frame`、`last_frame` 是 binding/工作流角色；`<Picture 1>`、`<Picture 2>` 是 Prompt 标签。FL2V 同时有首尾帧时，通常 Picture 1 对应首帧、Picture 2 对应尾帧，但两类字段不能互换。

当前画布通过 `ResizeObserver` 读取每个 `.port` 相对节点的实际中心，并由 `studio.tsx::endpoint()`
把该锚点换算为画布坐标；固定 `NODE_SIZE` 只用于首次测量前的回退、概览和视口定位。
资产工具展开、语言切换造成文案换行或节点高度变化时，观察器会自动刷新 SVG 曲线。
修改端口 DOM/CSS 时必须保留 `data-node-id` 与输入/输出端口选择器，并回归验证连线起止点。

### 3.5 换声工作区

左侧「换声」入口挂载独立的 `VoiceStudio` 抽屉，不新增 CanvasDocument V7 节点或本地任务副本。原音频和参考音频各有单文件拖拽/选择区，也可选服务端资产库的既有音频；上传经 `POST /api/assets`，成功后立即合入共用资产状态。前端预检扩展名 WAV、FLAC、OGG、MP3，服务端仍检查文件签名；M4A/AAC 当前不在可上传范围。两个输入和完成结果的播放器使用 `preload="none"`，避免打开面板就下载音频。

浏览器话筒也可提供音频：只在用户点「开始录音」后请求权限，最长 5 分钟；停止后先在浏览器本地解码并编码为保持设备采样率的 PCM16 单声道 WAV，供用户试听。用户可把这段录音选为原音频或参考音频；只有再点「使用这段录音」才通过现有 `POST /api/assets` 上传到所选槽位，录音不会自动提交换声任务。取消、丢弃、关闭抽屉及异常路径停止媒体轨道并释放本地预览 URL；上传失败保留本地试听以便重试。浏览器话筒要求安全上下文（HTTPS 或 localhost）及 `getUserMedia`、`MediaRecorder`、`AudioContext` 支持。这是录后转换，不是实时监听变声；无 GPU 时只能测试录音/WAV/UI/API 链路，不能验证模型音质。

引擎选项是 Vevo2 FM-only（语音/清唱）和 YingMusic-SVC（歌曲人声分离、转换、重混）。提交前读取 `GET /api/voice/capabilities`，仅在所选引擎 `available`、两个资产有效且没有上传/提交动作时启用按钮；不能通过前端绕开服务端能力检查。`POST /api/voice/tasks` 带随机 `request_id`，任务历史从服务端 `GET /api/voice/tasks` 恢复；活跃任务每 2.5 秒刷新一次，显示进度、GPU 队列位置与等待原因。取消和删除调用各自的服务端 API；删除仅在终态显示，并会删除输出。完成结果从同一受控任务音轨文件的 `preview`（inline）与 `download`（attachment）端点试听/导出 WAV，前端切换音轨时两者同步。没有配置外部运行时或 GPU 的本地环境只能验证 UI/API 合同，不能据此声称推理已通过。

YingMusic 的 `capabilities.engines[].tuning` 公布范围/默认值；只有服务端支持时前端才展示/提交 `diffusion_steps`（10–200，默认 100）、`inference_cfg_rate`（0–2，默认 0.7）、`seed`（-1 或 0–4294967295，默认 -1）。-1 在服务端按任务生成实际种子，实际值随持久回执 `parameters` 返回；相同 `request_id` 重放复用原任务/种子。Worker 在人声转换前设置 Python、NumPy、PyTorch/CUDA RNG，且每任务重置步数/引导强度，避免驻留 Worker 把前一次参数带入下一次。种子影响扩散初始噪声，但 CUDA 非确定性和环境差异不保证位级一致。100 步为上游 Gradio/脚本的质量-耗时折中，不宣称普适最优；模型 FP16、分离/重混选项维持原完整工作流，不与推理步数一并调整。

`ProcessVoiceWorker.status()` 读取原子发布的驻留状态快照，不获取覆盖模型加载/推理全程的 Worker 运行锁；因此换声任务进行时，能力查询和前端面板不会被长推理阻塞。

YingMusic 的 `output_options`（`include_stems=false`、`echo=true`、`reverb=true`）由 capability 公布；前端仅在服务端声明时显示。关闭回声或混响只把相应混音湿声设为 0，不改变换声干声和原伴奏。`include_stems=true` 时 Worker 在任务目录额外保留 `dry-vocal.wav` 与 `accompaniment.wav`，最终混音仍为 `converted.wav`；任务 `output` 保持旧版最终混音合同，`outputs` 增列 `mix` / `dry_vocal` / `accompaniment` 的大小、SHA-256、试听与下载 URL。旧任务无 `outputs` 时前端只展示最终混音。`GET /api/voice/tasks/:id/preview|download?track=mix|dry_vocal|accompaniment` 根据任务回执和固定文件名解析，不接受任意路径；缺少未保留音轨返回 404。所有任务内文件在删除任务时一并清理；失败、取消和重启清理半成品。试听与导出读同一文件，只有 `Content-Disposition` 不同。

## 4. 视频模式与 Prompt

### 4.1 Director 模式

`app/studio-video-mode.ts` 和 `server/workflows.py` 必须保持同一合同：

| Director | 底层 | 输入合同 |
| --- | --- | --- |
| `t2v` | `h3_fl` / text | 纯文本 |
| `i2v` | `h3_fl` / fl2va | 恰好一张 `first_frame` |
| `fl2v` | `h3_fl` / fl2va | 1–2 张图，角色只能是 `first_frame` / `last_frame`，两张时不能重复 |
| `r2v` | `h3_ref` / ref2va | 图片/视频/音频参考；不能只有音频 |
| `v2v` | `h3_ref` / ref2va | 明确选择一条已连源视频，不允许额外参考 |
| `rv2v` | `h3_ref` / ref2va | 一条明确源视频，加图片或音频参考，不允许第二条视频 |

前端 `buildVideoDirectorContract` 提供即时错误；后端 `_resolve_director_contract` 是安全边界。两处规则变更必须成对修改并成对测试。

### 4.2 Prompt 提交链

```text
PromptMentionComposer
  -> 前端 @{assetId} / bindingId
  -> POST /api/prompts/compile（预览）
  -> POST /api/generate（实际请求）
  -> server.workflows.parse_generation_request
  -> server.prompting.compile_prompt / preserve_tags_only
  -> ComfyUI text encoder 输入
```

当前视频 Prompt 默认是“只读提交”语义：只把稳定素材引用替换成最终 H3 标签，不翻译、不扩写、不重排用户正文。FL2V 编译器可以根据端点角色增加首尾时间对齐说明；这不改变用户素材的角色。

改 Prompt 时至少验证：

- `@` 选择和重复素材去重
- 前端最终预览与实际任务回执一致
- 标签按媒体类型独立编号
- 视频配对音轨先占用 Audio 标签
- 刷新后 bindingId 和 Prompt mention 仍能恢复

## 5. Profile 与工作流编译

### 5.1 Profile 是能力入口

`server/profiles.py` 定义 `WorkflowProfile`、内置 Profile 和外部清单校验。内置能力包括：

- H3 FL2VA / Ref2VA：Turbo LoRA 与 Base
- Z-Image Turbo：T2I、latent img2img、社区 LoRA 变体
- Qwen-Image 2512 T2I、Qwen-Image Edit 2511、Qwen-Image 2.1 BF16 统一 T2I / 1–10 图编辑
- FLUX.2 Klein 4B/9B
- Anything V5 回退

外部 Profile 位于 `$H3_STUDIO_DATA_ROOT/profiles/*.json`，只能选择代码已经审核的 compiler。`server/comfy.py::capabilities` 结合 `/object_info`、模型选择项和 Profile 声明计算 `available`；前端只消费 capability，不自行推测文件是否存在。

`qwen-image-2.1-bf16` 使用受控 compiler `qwen_image_21`，固定绑定完整 BF16 的 diffusion model、Qwen3-VL 8B text encoder 与 VAE，不做隐式量化。能力探测要求新版 ComfyUI 的 `TextEncodeQwenImage21` 和 `QwenImage21Cache` 节点、对应接口以及三份精确权重；旧 ComfyUI 应将 Profile 标为不可用，而不是改用旧版 Qwen 模型。无图时使用 `EmptyLatentImage` 文生图；有图时按 `reference_index` 生成 `images.image_N` Autogrow 输入，首图通过 `ImageScale` 确定编辑尺寸，附加参考图缩至 1MP，文本提示中的“图N / image N / <imageN>”统一为 `<imageN>`。输出尺寸使用独立的原生 2K preset，参数默认 40 步、CFG 1、`euler/simple`，不对外接受或回显 `denoise`。其权重为 Qwen Research License，非商业研究/评估以外用途须单独授权；不得混同旧 Qwen-Image 的 Apache-2.0 许可。

### 5.2 编译顺序

普通生成请求：

```text
Handler._generate
  -> parse_generation_request
  -> registry.choose / profile identity validation
  -> H3 Ref2VA Token 风险预检；必要时 prepare_h3_reference
  -> comfy.ensure_capability
  -> compile_workflow
  -> workflow_evidence + workflow_sha256 持久化
  -> comfy.submit
  -> /api/status 轮询 history/queue
```

`server/workflows.py::compile_workflow` 按 compiler 分发到 H3、checkpoint、Z-Image、Qwen 或 FLUX.2 构造器。不要让外部清单直接提供任意 ComfyUI graph；新增 compiler 必须写受控构造器和测试。

H3 Base 默认 Profile（`minimax-h3-*-base`）是 Direct：按请求步数直接解码并保存视频，不声明 `resume`。只有显式 `minimax-h3-*-base-resumable` Profile 包含固定调度版本、最大总步数和允许追加范围。首次任务用完整固定 sigma schedule 的前段采样；`H3StudioSaveLatent` 保存 H3 视频/音频 NestedTensor 采样状态，预览单独解码 denoised estimate。该节点依赖 `SaveVideo` 输出并采用 best-effort 语义：MP4 必须先落盘，检查点失败只记录 `checkpoint_error`。续采使用 `H3StudioLoadLatent + DisableNoise + SplitSigmas`，只执行新增 sigma 段，禁止把预览视频重新加噪。Turbo LoRA Profile 未经验证，不声明续采。自定义节点源码位于 `comfy_nodes/h3_studio_checkpoint/`，部署时安装到 ComfyUI `custom_nodes` 并重启 ComfyUI。

### 5.3 统一 GPU 资源与内存

`server/gpu_resources.py` 是单机单卡重任务的唯一调度器。普通生成、续采和长视频
分段通过 `ComfyTaskCoordinator` 持有租约到 Comfy prompt 终态；Vevo2/YingMusic 进程也使用
同一队列。一张 GPU 同时只有一个活跃重任务，策略固定为 `fifo_no_preemption`：

- 队列回执包含位置和 `waiting_for_*_task` / `waiting_for_model_release` 原因；
- 完成后记录 backend、model key、驻留时间和最后使用时间，连续同键任务复用已加载模型；
- 切换 backend/模型前先释放旧驻留；Comfy 调 `/free`，换声终止独立 Worker 以释放 CUDA context；
- 取消、崩溃、启动失败及空闲超时都会调用 backend 释放钩子；重启不会盲目重提之前的换声任务。

核心配置是 `H3_STUDIO_GPU_DEVICE_INDEX`、`H3_STUDIO_GPU_IDLE_RELEASE_SECONDS`（默认 180）和
`H3_STUDIO_GPU_POLL_SECONDS`。旧 `H3_STUDIO_COMFY_IDLE_FREE_SECONDS` 仅作超时兼容别名。
`GET /api/resources/gpus` 返回 nvidia-smi 显存快照、当前租约、队列和驻留模型。
当前没有强制插队或清空队列合同。不得通过改变精度、分辨率、VAE 或采样参数换取内存。

## 6. 资产、结果与存储

默认数据根目录由 `server/config.py` 控制为当前工作区下的 `data/`；正式部署应通过环境变量指向独立持久盘。主要持久数据：

```text
$H3_STUDIO_DATA_ROOT/
  metadata/assets/          资产 JSON
  metadata/jobs/            普通生成任务 JSON
  metadata/asset-folders/   文件夹 JSON
  metadata/derivations/     剪辑派生回执
  metadata/checkpoints/     每条续采链的最新 checkpoint 清单
  metadata/video-projects/  长视频项目（由 manager 使用）
  metadata/voice-tasks/     换声任务回执
  derivations/              裁剪、抽帧、分离音频等文件
  checkpoints/              原子保存的最新 latent（每链一个）
  voice-results/            完成的换声 WAV；YingMusic 可选保留换声干声和原伴奏，其余中间 stem 任务结束即清理
  model-cache/              外部换声依赖的持久模型缓存
  logs/                     换声 Worker stderr（stdout 专用 JSON 协议）
  thumbnails/               缩略图缓存
  tmp/                      有界临时文件
  profiles/                 外部 Profile 清单
```

资产上传由 `server/storage.py::AssetStore` 流式接收、识别、探测并规范化。视频参考会生成 24 FPS 兼容副本；原始授权/来源元数据与用户文件不能随代码部署覆盖。

剪辑与抽帧由 `POST /api/media/derive` 产生独立的派生结果回执，不自动进入资产库。`GET /api/derivations` 恢复结果抽屉；删除画布节点不删除回执，只有用户在结果中显式删除才会清理派生文件。画布节点右键或结果卡片的“保存到资产”调用 `POST /api/derivations/:id/assets`，并在回执中记录 `asset_id`。

`prepare_h3_reference` 同样位于媒体派生层：先应用旋转元数据，按 contain 保持比例，不放大小素材，将内容放入 32 对齐的最小 edge-pad 画布，输出最长 15 秒、24 FPS、H.264/YUV420P。幂等键由源 SHA-256、算法版本和受控参数组成；原素材永不覆盖。前端与 CLI 使用 `background=true` 获得 `media-task` 回执，再通过 `GET /api/media-tasks/:id` 恢复进度或 `POST /api/media-tasks/:id/cancel` 协作取消；同步调用仍用于服务端内部安全编排。`sm120 + SageAttention` 长序列安全策略由 capability 公开，阈值默认 150000，可通过环境配置。

`CheckpointManager` 每条链只保留一个最新 latent。新文件复制、哈希和原子换名成功后才交换清单并删除旧文件；失败/取消保留旧点。启动和后台 GC 清理过期、已删除及孤儿文件，跳过活跃链。默认 TTL 48 小时，配置范围 24–72 小时；staging 位于 `ComfyUI/input/h3-studio-checkpoints`，与普通资产上传根隔离。

`GET /api/assets` 的公开资产记录包含 `content_hash`（服务端导入时计算的 SHA-256）。`POST /api/assets` 会对用户直接上传的完全相同字节做轻量去重：命中已有、文件仍存在的 library 资产时返回 `200` 和 `reused: true`，复用既有 ID，不改名、不移动文件夹。新资产仍返回 `201` 和 `reused: false`。内部任务物化与生成结果不参与这个直传去重，避免共享 ID 导致删除和保留规则耦合。

历史重复记录仍保留独立 ID，前端默认收起它们；用户可展开重复项、多选后显式删除，不会自动删除可能仍被项目引用的旧记录。

资产、普通任务结果和派生结果的公开记录都有持久化 `pinned` 布尔值，分别通过 `PATCH /api/assets/:id`、`PATCH /api/jobs/:id` 和 `PATCH /api/derivations/:id` 更新。列表把置顶项排在普通项之前；任务结果首页可用 `include_pinned=1` 额外取回不在当前分页窗口内的置顶任务，不能因为 cursor 对账而丢掉旧置顶项。结果抽屉同时支持普通任务与派生结果的混合多选和批量删除。

资产文件夹只是组织元数据。`DELETE /api/asset-folders/:id` 删除文件夹本身时，不删除资产或子文件夹：直接内容会移动到被删文件夹的父级；若目标层存在同名子文件夹则返回 `folder_name_conflict`，保持原数据不变。

普通结果保存在 ComfyUI output，任务 JSON 只记录受控相对输出与证据。预览、缩略图、下载都通过 Python API 校验任务/资产记录后读取，不能允许客户端传任意服务器路径。

结果刷新恢复重点：

- `app/studio-history.ts` 把旧 host/port 的媒体 URL 重定位到当前同源 `/api/*`
- 结果抽屉优先请求 `GET /api/jobs?summary=1&results=1&include_pinned=1`，只取可展示结果的轻量字段，并在首页附带跨分页置顶结果；服务端返回 ETag，未变化时可用 `304`
- 最近任务会压缩后写入 `sessionStorage`，当前上限 100 条、有效期 10 分钟；缓存只是首屏加速，服务端任务 JSON 仍是事实来源
- 完成任务的 `<video>` 需要在 URL/任务变化时重新装载
- 预览和下载支持 `HEAD`、单段字节 `Range`、`If-Range`、ETag 与 Last-Modified；媒体 URL 指向不可变任务输出，因此可长缓存
- 缩略图失败不能阻止原视频预览和下载
- 同一缩略图的并发生成按摘要加锁，避免重复启动 ffmpeg；不同缩略图仍受媒体操作并发槽约束
- 删除任务应先更新 UI，再处理服务端失败回滚或提示
- 服务端在 `metadata/dataset-id` 保存数据集身份；前端只有在当前 `/api/jobs` 实例验证后才显示 session 缓存。恢复的画布任务不能直接回灌结果库或进入轮询，只有当前服务端列表和当前会话生成回执可以加入可信任务集。
- `GET /api/jobs` 的首页与 cursor 页都是权威窗口：合并时必须删除该窗口内服务端已不存在的缓存项，包括空的最后一页；同时不得让较晚返回的旧 cursor 覆盖新实例或更深分页状态。

## 7. 长视频子系统

长视频不是单次超过 15 秒的 H3 请求，而是多个合法 H3 分段的持久项目。

前端：

- `app/video-project.ts`：类型、序列时间、参数夹取、引用标签、校验、运行计划
- `app/video-director-model.ts`：分镜切点、均分、故事板草稿
- `app/video-director-workspace.tsx`：完整监视器、播放头、源视频与序列视图
- `app/video-timeline.tsx`：项目加载、编辑、保存、运行和结果通知
- `app/video-project-api.ts`：REST 客户端

后端 `server/video_projects.py::VideoProjectManager` 负责：

- 项目 CRUD、选段顺序运行、单段重跑与停止
- `tail_frame`：抽取上一段真实尾帧，作为下一段端点图
- `previous_video`：创建不超过 15 秒的派生视频参考
- `motion_context`：使用锁定的外部 Motion Context 节点复用上一段视频/音频 latent，对新段自动裁头，且不占用像素参考槽
- 人物迁移：持久化 `h3.character-migration/v1` recipe，把源人物与目标角色绑定为 `<Subject 1>` / `<Subject 2>`，以 `17k+5` 合法帧窗和 5/22/39/56 帧重叠构造项目。首段独立，后续段 Motion Context 音画窗口一致；尾窗先向前回填并选取可覆盖余量的最大合法重叠，只有短于最小窗口或不足一个 17 帧网格的余量才在私有模型输入补帧，最终产物回到源 24 FPS 帧数
- 复刻工坊：持久化 `h3.replication/v1` recipe，限定来源视频 15–60 秒，把复刻说明、保留项、替换字段、显式参考及 SHA-256 编译成 Ref2VA 长视频项目。非尾段使用精确 `17k+5` 来源窗，镜头分析只在可行时影响合法边界；尾段补到下一合法帧数，合并后一次性裁回源 24 FPS 帧数。`auto` 在 capability 可用时选 Motion Context，否则各段独立使用源区间。
- 失败/停止恢复、下游失效、派生资产回收
- ffmpeg concat 合并、进度、取消和产物证据；人物迁移合并后再精确裁帧，并按 `copy-source|reference-source|generate|mute` 实施音频策略

续接与合并使用的是不同资产：续接可以裁剪系统派生副本，最终合并必须使用每段完整输出。

Motion Context 依赖不入库的 GPL-3.0 外部节点 `ComfyUI-H3-Motion-Context`，当前锁定 v0.5.1 / `429e952ae5c09b54f44cb6e3bef7331d998f0656`，需 ComfyUI >= 0.34.0。`server/comfy.py` 根据节点及其输入 schema 动态暴露 `video.motion_context`。链上相邻生成段必须保持相同尺寸；Turbo/Base 的 Profile 、步数、精度和显存调度仍走原有合同。`server/motion_context.py` 使用独立配额、原子复制和 SHA-256 保存链状态；MP4 是主产物，latent 保存失败只阻断下一个依赖段。

## 8. API 地图

API 路由集中在 `server/app.py::Handler`：

| 分类 | 入口 |
| --- | --- |
| 健康与能力 | `GET /health`, `GET /api/capabilities`, `GET /api/workflows/director[/MODE]` |
| Prompt/生成 | `POST /api/prompts/compile`, `POST /api/generate`, `GET /api/status?id=...` |
| 任务结果 | `GET /api/jobs`（支持 `summary=1`、`results=1`、`include_pinned=1`、分页与 ETag）, `GET/PATCH/DELETE /api/jobs/:id`, `POST /api/jobs/:id/cancel|resume`, `GET /api/preview`, `GET /api/download`, `GET /api/jobs/:id/thumbnail` |
| 资产 | `GET/POST /api/assets`, `GET/PATCH/DELETE /api/assets/:id`（PATCH 支持名称、文件夹和置顶）, `GET /api/assets/:id/content|thumbnail`, `POST /api/jobs/:id/assets` |
| 文件夹 | `GET/POST /api/asset-folders`, `PATCH/DELETE /api/asset-folders/:id`（删除时内容提升到父级） |
| 派生媒体 | `POST /api/media/derive`, `POST /api/media/mux-audio`, `GET /api/derivations`, `GET/PATCH/DELETE /api/derivations/:id`, `POST /api/derivations/:id/assets`（支持 `visibility=internal|library`） |
| 分镜分析 | `POST /api/media/analyze-scenes` |
| 长视频 | `POST /api/video/character-migration/plan`, `POST /api/video/replication/plan`, `GET/POST /api/video-projects`, `GET/PUT/DELETE /api/video-projects/:id`, `POST .../run|stop|merge`, `POST .../segments/:id/run` |
| 换声 | `GET /api/voice/capabilities`, `GET/POST /api/voice/tasks`, `GET/DELETE /api/voice/tasks/:id`, `POST .../:id/cancel`, `GET .../:id/preview|download?track=...` |
| GPU 资源 | `GET /api/resources/gpus`（显存、租约、驻留模型、队列原因） |
| 维护 | `POST /api/maintenance/gc` |

除健康检查外，API Key 开启后都需认证。浏览器写操作还检查 Origin。同源 gateway 在服务端添加 Key，因此 Key 不进入前端 bundle。

### 8.1 Go CLI 自动化边界

`cli/cmd/h3ctl` 是面向 Agent 与脚本的正式 API 客户端，不替代 Python API，也不复制 `workflows.py` 的编译逻辑。命令层只解析参数，`internal/operation` 承载可供未来 workflow DAG 直接调用的原子能力。

- 生成使用“提交 `job_id` + 短请求轮询”；CLI 断开不取消服务端任务，`Ctrl-C` 默认只停止本地等待。
- `voice convert` 用两个音频 locator 提交持久换声任务，默认等待，`--detach` 只返回 task ID；YingMusic 可选 `--steps`、`--cfg`、`--seed`、`--keep-stems`、`--echo=false`、`--reverb=false`，Agent 的 `voice.convert` 对应字段为 `diffusion_steps`、`inference_cfg_rate`、`seed`、`output_options`；`voice download --track mix|dry_vocal|accompaniment` 与 Agent `voice.download.track` 可取回分轨，默认仍是最终混音。`voice.*` 和 `gpu.status` 也是 Agent 原子 operation。
- `media prepare-reference` 与 `media.prepare_reference` 共用服务端派生；本地输入先上传，CLI 本机不需要 ffmpeg。`job resume` 与 `job.resume` 只提交任务 ID、追加步数和幂等 request ID，可继续等待/下载。
- `video compose` / `video.compose` 是端到端长视频入口：自动补齐 Profile 版本与摘要，再组合项目创建、顺序生成、Motion Context 裁头、合并等待和原子下载。`video trim` 复用 `media trim`，`video concat` 复用 `project merge`，底层原子 operation 仍可独立调用。
- `video migrate-character` 先解析本地/asset/job/media/当前 context 资源，用纯规划器返回分窗、所有权、尾裁与存储估算，再创建持久项目。`--detach` 只返回 project ID；中断后通过现有 project 原子操作恢复，已完成段不重算。Agent 对应严格 Draft 2020-12 operation `video.character_migration.plan` / `.produce`，通用音频置换是 `media.mux_audio`。
- `video replicate` 解析来源与可重复 `--reference` locator，与前端共用 `POST /api/video/replication/plan`。`--plan-only` 只返回 recipe/project，`--detach` 在项目开始后返回 ID，默认等待分段、合并并原子下载。Agent 对应 `video.replication.plan` / `.produce`。抖音下载仍是隔离的本地 `h3ctl douyin download`，不将 Cookie 或 `yt-dlp` 并入 H3 服务端。
- `--control-timeout` 是控制面 HTTP 超时；transfer/media 超时独立且默认无限。`job wait --timeout` 是总等待超时。
- 显式 Profile 先读 `/api/capabilities`，自动附加 `profile_version` 和 `manifest_sha256` 作为 `profile_digest`。
- JSON stdout 使用 `h3ctl.output/v1` 信封，进度/日志写 stderr；JSONL 生成等待先输出 `submitted` 再输出状态事件。提交断连用同一 request ID 和 payload 恢复。
- 资源定位统一为本地路径、`asset:ID`、`job:ID#INDEX`、`media:ID` 与 `h3://CONTEXT/assets/ID`。本地生成输入先流式上传，job/派生结果先物化为 internal asset。
- 下载使用同目录唯一 `.part`；非 force 原子 no-replace，force 完成后原子替换。HTTP 不跟随重定向。
- CLI context 二选一：direct 保存 HTTP(S) URL；SSH 保存 target/可选 SSH port/remote API port。SSH 上下文每条真正需要 API 的命令创建私有临时 ControlMaster：先 `-O check` 确认认证后的 master，再用 `-O forward` 让同一 master 确定性绑定转发；master 仍读取 alias 的 HostName/User/Identity/ProxyJump/Port，但命令行禁用 fork、remote command、TTY 和 alias 预配转发。私有 check/forward/exit 调用使用 `-F none` 隔离用户配置，forward 仅携带一条 CLI 所有的 `-L`。端口冲突有限重分配，只有 forward 成功后才严格验证 H3 `/health` JSON（拒绝重定向、非精确 JSON content-type、超过 64 KiB、尾随内容和第二个 JSON）。forward 阶段 Ctrl-C 返回 `interrupted`，启动截止返回 `ssh_start_timeout`。命令结束后在输出成功信封前有界 exit/stop/wait/reap 并删除控制目录；清理失败返回 `ssh_cleanup_failed`，业务失败时仍保留主错误并附 cleanup details。长时 `job wait` 期间会话保持；不支持 ControlMaster 的运行环境明确失败，不回退到 listener 猜测。所有 help/unknown usage、workflow/completion、operation list/schema 与 context 配置命令都是纯本地；只有 `context test` 主动测试连接。`asset copy` 的 typed/operation 入口都只建立 source/destination 会话并合并双方 cleanup 错误。建议 target 使用 `~/.ssh/config` 别名，租用机地址变化时只改 HostName/Port；`context update --clear-ssh-port` 可恢复 SSH config 的 Port。SSH 固定加 `-n` 避免抢占 spec/input stdin，Agent 再用 `--non-interactive` 加 `BatchMode=yes`；凭据交给 SSH config/ssh-agent，CLI 不保存密码。
- V2V/RV2V 的 `source_asset_id` 在 Go Prepare 层解析为 asset，并以 `motion` 角色去重、置于 references 首位，与 Python workflow 合同一致。
- operation registry、递归 input schema、校验与执行均在 `internal/operation`；`command operation` 只做 stdin/flag/output adapter。全部公开 schema 必须通过 Draft 2020-12 标准编译器测试。
- 服务端资源 ID 统一为 32 位小写十六进制；创建、上传、派生、物化与生成回执在进入下一步前验证。context 写操作使用 Unix/Windows 真正的跨进程文件锁串行化，旧配置在展示前重新规范化且不会回显非法 URL。

完整用法和当前未支持边界见 `docs/cli.md`。

## 9. 测试策略

按改动风险选择，不要求固定轮数：

```bash
# 快速静态检查
npm run typecheck
npm run lint

# Go CLI 门禁
cd cli
gofmt -w .
go test ./...
go vet ./...
go build ./cmd/h3ctl

# 单个前端合同测试
node --test tests/studio-video-mode.test.mjs

# 单个 Python 模块
python3 -m unittest server.tests.test_workflows -v

# 生产构建
npm run build

# 完整门禁
npm test
```

建议最小矩阵：

| 改动 | 最少验证 |
| --- | --- |
| 纯 CSS/文案 | 相关 UI 合同测试 + ESLint；关键布局人工查看 |
| React 状态/交互 | 相关 Node 测试 + TypeScript + ESLint |
| Canvas 文档/图算法 | `studio-document` + `studio-graph` + 迁移/刷新测试 |
| Prompt/模式/Profile | 前后端对应测试同时跑 |
| 工作流节点图 | `server.tests.test_workflows` + capability 测试；有条件再跑远端 dry/real job |
| API/存储/安全 | 对应 Python 测试，必要时完整 `npm test` |
| 长视频执行/合并 | 前端 timeline/复刻测试 + `server.tests.test_video_projects`；人物迁移加 `test_character_migration`，复刻加 `test_replication`，两者都跑 Go operation schema 回归 |
| GPU 调度/换声 | `server.tests.test_gpu_resources` + `test_comfy_tasks` + `test_voice`；有 GPU 时用锁定上游 revision 各跑真实样本 |
| 启动/网关/部署 | ops + gateway 测试 + 生产构建 + 健康检查 |

历史 `cycle*` 测试名称保留用于回归，不代表新改动必须重复对应轮数。

## 10. 开发机与发布

推荐的生产部署布局：

```text
<deployment-root>/
  current -> releases/<git-short-sha>
  releases/<git-short-sha>/    不可变代码与构建
  data/ metadata/ profiles/    持久数据
  logs/                        运行日志
  .tools/                      固定 Node 工具链
  voice-runtimes/              锁定的 Amphion/YingMusic 仓库与 Python 3.10 环境
  voice-models/                换声权重，不进入 Git/release
```

当前进程由 `python3 scripts/h3studio.py start --port 3013 --internal-port 3014 --api-port 6020` 监督：

- Python API：6020
- Vinext 内部服务：3014
- 对浏览器的同源 gateway：3013
- ComfyUI：切换后使用独立升级的 0.37 服务 `:6009`，复用原模型/input/output 持久目录；旧 `:6006` 保留到切换和回归验收结束，不要在发布前停止。
- Vevo2/YingMusic：由 Python API 按需启动的子进程，无监听端口

安全发布顺序：

1. 本地工作树干净并完成相关检查。
2. 用 Git 短 SHA 创建独立 tar/release；不要覆盖 `current` 内容。
3. 在新 release 中复用受控工具链、安装锁定依赖并生产构建。
4. 确认持久目录和 `.env.local` 指向共享位置。
5. 原子切换 `current`，短暂重启 supervisor。
6. 检查 `GET http://127.0.0.1:6020/health`、`http://127.0.0.1:3013/` 和关键 capability。
7. 失败则恢复旧链接和旧服务；不要修改或删除数据目录。

## 11. 常见跨层修改清单

### 新增视频模式

同时检查：`studio-video-mode.ts`、`studio-document.ts`、`studio.tsx` payload、`workflows.py` 合同/编译、Profile compiler、前后端测试、Director preset。

### 新增模型/Profile

同时检查：`profiles.py` 声明与 compiler baseline、`comfy.py` capability 探测、`workflows.py` 受控构造器、前端 reference policy/参数 schema、许可证字段、测试与 `docs/image-workflows.md`。

### 修改资产角色或编号

同时检查：V7 binding、legacy/V7 双向转换、graph slot、Prompt mention、服务端 `_graph_references`、刷新恢复、FL2V 首尾角色测试。

### 修改结果预览

同时检查：任务列表回执、status 合并、同源 URL 重定位、缩略图、video reload、Range 响应、结果节点和结果抽屉。

### 修改长视频

前端纯模型和后端 manager 都有校验。特别检查选段依赖、续接派生资产、停止/重启恢复、失败下游状态、完整输出合并与 GC 引用。

### 修改换声或 GPU 资源治理

同时检查上游 revision/入参、`voice_worker.py` JSON stdout 协议、当前 torchaudio
与 YingMusic SoX remix 适配、Worker 复用/终止、
GPU 租约终态、Comfy prompt 生命期、资产删除引用、API/CLI operation 和真实 GPU 样本。

## 12. 当前技术债与防错提示

- `app/studio.tsx` 体积较大；新增独立领域逻辑优先抽到纯模块并单测，不继续堆入组件。
- 画布存在 legacy React 状态与 V7 文档双表示；任何字段修改都要验证转换和持久化。
- `docs/architecture.md` 含早期设计与部分过时容量描述；当前合同以本 Wiki、源码和测试为准。
- 前端模式校验与后端模式校验重复是刻意的：前者改善体验，后者是安全边界。不要为了 DRY 删除后端验证。
- ComfyUI workflow JSON 是编译产物，不是浏览器可编辑输入；对外部 workflow 的复用必须落到受控 compiler/Profile。
- 远端 release 目录不可作为编辑工作区；修复应回到本地 Git，验证、提交、重新发布。

维护规则：当新增顶层模块、API、数据版本、Profile compiler、远端目录或进程入口时，必须同步更新本页对应章节。若页头最后更新时间距当前超过 7 天，下一次开发前必须对照源码、测试、capability 和远端运行拓扑主动校准本文，并记录真实更新日期，不能仅刷新日期。
