# MiniMax H3 Video Studio 架构规格

> 状态：早期设计基线；更新日期：2026-08-19。本文保留设计背景，可能含已演进的容量或实现描述。当前代码地图、事实来源与修改入口请先看 [LLM Wiki](llm-wiki.md)，最终以源码、测试和 capability 回执为准。

## 1. 产品目标

MiniMax H3 Video Studio 是运行在远程开发机上的可维护 Web 应用。用户通过本地浏览器：

1. 把提示词、图片、视频、音频、参数、生成器和输出节点拖入画布并连线；
2. 由应用根据输入关系自动选择 H3 FL2VA 或 Ref2VA 工作流；
3. 设置视频画幅、时长、Seed，以及经过 profile 校验的步数、Turbo LoRA 强度等高级参数；
4. 提交到同机 ComfyUI，实时看到排队、进度、失败原因和结果；
5. 预览并把 MP4/图片下载到浏览器本地；
6. 使用独立的可配置图片模型工作流完成静态图片生成，而不是冒充 H3 生图。

不把用户的浏览器画布直接翻译成任意 ComfyUI 节点图。画布是受类型约束的创作 DSL；后端把经过验证的 DSL 编译到版本化工作流模板，避免用户输入任意 ComfyUI `class_type` 或服务器路径。

## 2. 建议技术分层

```text
Browser
  └─ React/Next.js canvas + typed node editor
       ├─ local file preview (Object URL; never exposed to ComfyUI directly)
       ├─ graph validation and final material-label preview
       └─ HTTPS/SSH tunnel
            ↓
MiniMax H3 Video Studio server
  ├─ REST API (projects, assets, jobs, downloads, capabilities)
  ├─ graph schema + compiler + prompt compiler
  ├─ upload preflight (type, size, dimensions, duration, codecs)
  ├─ model/workflow profile registry
  ├─ job/event store
  └─ ComfyUI adapter
       ├─ POST /upload/image and controlled media staging
       ├─ POST /prompt
       ├─ WS /ws
       ├─ GET /history/{prompt_id}
       └─ GET /view
            ↓
ComfyUI :6006
  ├─ H3 FL2VA profile
  ├─ H3 Ref2VA profile
  └─ configured image profile(s)
```

ComfyUI 官方说明：客户端通过 `/prompt` 提交完整工作流，WebSocket `/ws` 接收实时消息，`/history/{prompt_id}` 获取特定任务历史，`/view` 读取输出，`/upload/image` 上传图片，`/object_info` 用于发现节点能力。适配层应把这些接口藏在 MiniMax H3 Video Studio 服务端之后，不让浏览器持有服务器文件路径或任意工作流执行权。

## 3. 画布 DSL

### 3.1 节点

| 节点 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `PromptNode` | 文本、`@素材` 提及 | `PromptSpec` | 支持原始模式与官方结构增强预览 |
| `ImageAssetNode` | 本地文件 | `ImageAsset` | 可选择 `first_frame`、`last_frame`、`identity`、`object`、`scene`、`style`、`composition`、`storyboard` 等角色 |
| `VideoAssetNode` | 本地文件 | `VideoAsset`、可选 `PairedAudio` | 可选择 `motion`、`camera`、`pacing`、`edit_source`、`continuation`、`subject_source` 等角色 |
| `AudioAssetNode` | 本地文件 | `AudioAsset` | `copy`、`partial_copy`、`voice`、`rhythm`、`music_style`、`sound_texture` |
| `VideoSettingsNode` | 控件值 | `VideoSettings` | 宽高/画幅、名义时长、Seed、profile、高级采样参数 |
| `ImageSettingsNode` | 控件值 | `ImageSettings` | 图片模型 profile、宽高、步数、Seed、批量数 |
| `GenerateVideoNode` | Prompt、0..N 素材、VideoSettings | `VideoJob` | 自动路由 H3 工作流 |
| `GenerateImageNode` | Prompt、可选图片、ImageSettings | `ImageJob` | 自动路由配置的图片工作流 |
| `OutputNode` | Job | Preview/Download | 显示任务状态、媒体元数据、下载按钮 |

每个端口有静态类型；循环、未连接的必填输入、一个资产接入冲突的互斥角色、错误模态连线必须在浏览器端即时提示，并在服务端再次校验。

### 3.2 最小图清单

```json
{
  "schemaVersion": 1,
  "nodes": [],
  "edges": [],
  "metadata": {
    "title": "Untitled",
    "createdAt": "ISO-8601",
    "updatedAt": "ISO-8601"
  }
}
```

资产节点只保存 MiniMax H3 Video Studio 的 `assetId`、哈希、媒体元数据、授权/来源字段和用户角色，不保存浏览器绝对路径。生成记录保存规范化图快照、编译后的 prompt、模型 profile 版本、ComfyUI prompt、`prompt_id`、输出元数据和错误，确保克隆机器或重启服务后仍能复现参数。

## 4. 视频自动路由

路由必须确定性执行，结果在提交前展示：

```text
if 存在 video/audio/reference-role image:
    route = H3_REF2VA
else if images == 0:
    route = H3_FL2VA_T2VA
else if images == 1 and role == first_frame:
    route = H3_FL2VA_I2VA
else if images == 1 and role == last_frame:
    route = H3_FL2VA_L2VA
else if images == 2 and roles exactly {first_frame,last_frame}:
    route = H3_FL2VA
else:
    validation error with an actionable fix
```

禁止隐式改变用户语义：例如一张 `identity` 图片不能因为“只有一张图”而被当作首帧；一段 `motion` 视频不能被当作需要逐帧保留的编辑源。若混用了精确首尾帧与 Ref2VA 素材，UI 要求用户选择一种模式或拆成两个生成任务，不能静默丢掉素材。

### 4.1 Ref2VA 限制

- 图片最多 9 张。
- 视频最多 3 段，每段 2–15 秒，视频总时长不超过 15 秒。
- 音频最多 3 段，每段 2–15 秒，音频总时长不超过 15 秒。
- 图片、视频、音频合计最多 12 个文件。
- 独立音频不能成为唯一参考；至少要有一张图片或一段视频。
- 标签按实际连接顺序、每种模态独立编号。提交前展示 `<Picture N>`、`<Video N>`、`<Audio N>` 解析表。

官方限制由图校验器、上传预检器和工作流编译器三层重复保证。不要只依赖 ComfyUI 运行时错误。

## 5. H3 视频参数规范

### 5.1 尺寸

官方 H3 支持多种画幅，H3-Base 默认短边 768，输出 24 FPS。当前本地首批 profile：

| 画幅 | 宽×高 | 说明 |
| --- | --- | --- |
| 16:9 | 1344×768 | Comfy-Org 模板标注的本地 768p 官方尺寸 |
| 9:16 | 768×1344 | 横屏预设转置，工程决策 |
| 1:1 | 768×768 | 方形基础预设，工程决策 |

专家自定义宽高必须是 32 的倍数，并受管理员配置的最大像素数限制。生成器返回最终宽高，下载文件元数据必须由 `ffprobe` 再验证，不能只信请求值。

### 5.2 时长和帧数

官方 H3 总能力标称为 4–15 秒。ComfyUI 原生本地节点采用 24 FPS、`17k+5` 帧网格，训练区间约 124–362 帧。本项目开放完整的 124–362 帧真实网格；最后一档的生成输出为 362 帧，即约 15.083 秒（界面显示 15.08 秒）：

```text
raw = requestedSeconds * 24
frames = min(362, ceil((raw - 5) / 17) * 17 + 5)
effectiveSeconds = frames / 24
```

UI 同时显示“请求 5 秒 → 实际 124 帧 / 约 5.17 秒”。Prompt 的关键帧对齐时间使用 `effectiveSeconds`，不是请求值。

生成输出上限与输入参考预算是两套合同：362 帧档只放宽新生成视频的输出；上节所述用户上传视频/音频参考仍是单段及各自合计不超过 15 秒。

### 5.3 采样 profile

参数不以彼此独立的自由输入提交，而以版本化 profile 为基础：

```json
{
  "id": "h3-fl2va-turbo4-v1",
  "modelFamily": "h3-fl2va",
  "diffusionModel": "...safetensors",
  "textEncoder": "...safetensors",
  "videoVae": "...safetensors",
  "audioVae": "...safetensors",
  "lora": "...safetensors",
  "stepsDefault": 4,
  "stepsRange": [4, 50],
  "sampler": "profile-owned",
  "scheduler": "profile-owned",
  "modelStrengthDefault": 0.75,
  "modelStrengthRange": [0.0, 1.0]
}
```

profile 在启动时用 `/object_info` 和模型列表验证。文件缺失或节点版本不兼容时，capability 返回 `unavailable`，前端禁用该选项并显示原因。高级面板可以覆盖已声明可覆盖的 Seed、步数和 LoRA 模型强度；后端按 Profile 边界校验。采样器、scheduler、sigma shift 等只有在专门验证过的 profile 中才开放。

内置 H3 Profile 分为两套采样合同：历史兼容标识 `turbo4` 使用开发机的 LightX2V Turbo LoRA、`sa_solver` 与 `simple`，4 步是默认/推荐值而非前端硬锁，步数和 `strength_model` 均在 Profile 边界内独立可调；`base` 完全绕过 LoRA Loader，使用当前 ComfyUI 官方模板的 20-step `res_multistep` + `simple` 基线，并允许调步数。Base 默认解析到 Direct Profile，不在成片关键路径上写断点；显式 `-base-resumable` Profile 才使用 H3 专用 NestedTensor 检查点节点。这里的 Base 仅表示关闭 Turbo LoRA；发布 checkpoint 本身仍是 CFG-distilled，不能称为 non-distilled。

续采工作流中，`H3StudioSaveLatent` 同时依赖采样器当前状态和 `SaveVideo` 返回值，因此只能在 MP4 已落盘后执行。它将 H3 的视频/音频 `NestedTensor` 以版本化 safetensors 键保存，且所有写入失败都降级为无检查点回执；服务端保留 completed 视频并设置 `checkpoint_error`，不得将成片改判为失败。

## 6. 提示词编译器

编译分三层：

1. `PromptNode` 保存用户原始文本和结构化段落；
2. 引用解析器把 `@图片1` 等别名解析到稳定的 `assetId`，再按最终连线顺序生成尖括号标签；
3. H3 编译器根据路由生成基础三段式或 Ref2VA 六段式，并保存“原始文本 → 增强文本 → 最终模型文本”的追踪记录。

默认规则来自 `docs/h3-prompt-guide.md`。提示词增强必须允许用户预览和编辑，不能不可见地改写指定对白、歌词或画面文字；这些文本需逐字保留。若没有接入官方托管 H3-Context-IR，界面必须标注“本地规则增强”，不能写成“官方 Context-IR”。

## 7. 上传与资产生命周期

### 7.1 浏览器

- 拖入后立即用 Object URL 本地预览；上传完成后释放 Object URL。
- 前端 MIME 只作体验提示，不作安全边界。
- 每项资产显示类型、大小、宽高、时长、帧率、编解码器、哈希、参考角色和最终 H3 标签。

### 7.2 服务端

- 使用随机生成的 asset ID 和服务器决定的文件名；忽略客户端路径，清理扩展名，禁止 `..`、绝对路径和符号链接逃逸。
- 流式写入临时文件，计算 SHA-256，校验完成后原子移动；失败清理临时文件。
- 图片解码验证尺寸；视频/音频用 `ffprobe` 验证时长、流、编解码器和可读性。
- 上传限制、允许格式和最大总量由 profile 配置并在响应中返回。
- 同哈希可去重；删除项目时采用引用计数或延迟垃圾回收，不得误删仍被任务引用的资产。
- 所有源素材保存来源、授权与用户声明字段；这对真实人物、影视片段、音乐和品牌素材尤其重要。

ComfyUI 只官方提供通用 `/upload/image` 路由；视频和音频的文件落盘必须由 MiniMax H3 Video Studio 的受控媒体 staging 层完成，写入预先配置的 ComfyUI input 子目录，随后模板只引用服务端返回的安全相对名。不得让 API 调用者指定任意服务器路径。

## 8. 任务与结果

任务状态机：

```text
submitting → queued → running → completed
      └────────┴────────┴────→ failed / canceled
```

- `POST /api/generate` 接受规范化 graph snapshot 和 `request_id`；相同 ID 与相同 payload 只排队一次，不同 payload 返回 409。
- 提交 ComfyUI 后保存 `prompt_id`；当前前端轮询 `/history/{prompt_id}`，并可取消队列/运行任务。
- 服务重启后从持久 JSON 元数据恢复非终态任务；提交确认窗口会按 ComfyUI queue 中的 `client_id` 尝试对账。
- 结果记录 ComfyUI 输出的 `filename/subfolder/type`，下载接口只允许任务记录中的永久 output 并安全解析路径。当前是 SSH loopback 单用户部署，不宣称多用户对象隔离。
- `Content-Disposition` 使用安全文件名；支持断点下载；视频响应为 `video/mp4`，图片响应使用实际 MIME。
- 任务详情保存原始参数、规范化参数、有效帧数、模型/LoRA/profile 版本、编译 prompt、耗时、输出 SHA-256 和 `ffprobe` 结果。

## 9. 可扩展模型与工作流 Profile

MiniMax H3 Video Studio 的画布只表达受类型约束的创作意图，不把模型名、ComfyUI `class_type` 或文件路径写死在前端。每个可安装能力由版本化 Profile 清单声明：

```json
{
  "id": "my-h3-ref-profile",
  "version": "1.0",
  "display_name": "My H3 Ref model",
  "output_type": "video",
  "input_modalities": ["text", "image", "video", "audio"],
  "compiler": "h3_ref",
  "required_nodes": ["MiniMaxH3ReferenceToVideo"],
  "required_models": ["ref_model", "text_encoder", "video_vae", "audio_vae", "ref_lora"],
  "parameter_schema": {"duration": "number", "steps": "integer", "lora_strength": "number"},
  "defaults": {"duration": 5.1667, "steps": 4, "lora_strength": 0.75},
  "limits": {"duration": [5, 15.083333333333334], "references": 12, "steps": [4, 50], "lora_strength": [0, 2]},
  "model_bindings": {"ref_model": "my-folder/my-ref-model.safetensors"}
}
```

- 后端注册表加载内置 Profile，并可从受控目录读取管理员安装的清单；`id + version` 是任务证据的一部分。
- 前端通过 `/api/capabilities` 获取已安装 Profile、输入模态和参数 schema，生成模型选择器与参数控件。画布节点不随模型增加而重写。
- 自动路由先按当前连线筛选输入模态兼容的 Profile，再按角色、优先级和部署配置确定唯一结果；歧义时要求用户选择，不静默猜测。
- 外部清单只能引用代码中已注册、经过测试的 `compiler`；清单不能直接注入任意 ComfyUI 节点、服务器路径或命令。
- 原始 ComfyUI workflow JSON 只能作为管理员导入源，经校验和适配后转成受控 Profile，不能由浏览器直接执行。
- 新模型必须声明许可证、内容政策、模型文件、输入/输出能力、参数边界、已验证尺寸和对应 dry-run/实机测试矩阵。

首批内置 Profile 是 H3 T2VA/FL2VA/Ref2VA 的 Turbo LoRA（兼容标识 `turbo4`）与 Base（no Turbo），以及 Anything V5 的 text-to-image/image-to-image；后续可增加其他视频模型、图片编辑模型和自定义工作流而不改变画布协议。

## 10. 静态图片生成

H3 官方输出是 Video + Audio，不是静态图片。本项目通过同一个 ComfyUI 后端接入独立的图像模型注册表：

```ts
type ImageProfile = {
  id: string;
  displayName: string;
  license: string;
  capabilities: Array<"text-to-image" | "image-edit" | "multi-reference">;
  workflowTemplateVersion: string;
  requiredModels: string[];
  widthRange: [number, number];
  heightRange: [number, number];
  defaults: { steps: number; guidance?: number };
};
```

首个建议 profile 是 `flux1_schnell`，依据 ComfyUI 官方教程的本地工作流；模型缺失时 capability 必须显示不可用并提供管理员安装清单。若图片连接到只支持 text-to-image 的 profile，必须报错，不能忽略图片。参考图编辑应路由到明确声明 `image-edit` 的 profile（例如后续配置的 Kontext），并单独完成端到端测试。

图片输出走与视频一致的任务、历史、权限和下载链路，并通过实际文件解码验证宽高、格式和非空像素。

## 11. API 草案

```text
GET    /api/capabilities
POST   /api/assets
GET    /api/assets/:id
DELETE /api/assets/:id
POST   /api/projects
GET    /api/projects/:id
PUT    /api/projects/:id/graph
POST   /api/graphs/validate
POST   /api/prompts/compile
POST   /api/jobs
GET    /api/jobs/:id
GET    /api/jobs/:id/events     # SSE or WS gateway
POST   /api/jobs/:id/cancel
GET    /api/jobs/:id/result
```

所有可变请求由 Python 显式 schema/边界校验，前端同时做交互预检；服务端响应统一使用机器可读错误码。引入新的跨语言 schema 工具前，不把当前实现描述成 Zod/JSON Schema。

## 12. 安全、隐私和内容政策

- MiniMax H3 Video Studio API 只监听 loopback 或受认证反向代理；SSH 隧道是当前默认访问方式。
- 不在前端代码、仓库、日志或任务记录里保存 AutoDL 密码、API Key。
- 当前默认是 SSH 隧道后的 loopback 单用户部署；生产代理密钥不是多用户会话认证。若未来公网部署，必须增加 HttpOnly/SameSite 会话、CSRF 防护和对象级授权后才能开放。
- 限制并发生成、上传速率、单用户磁盘用量和任务队列长度，防止 GPU 与磁盘耗尽。
- 对提示词、文件名和媒体元数据做日志脱敏；原始素材默认不进入分析日志。
- 不提供“关闭官方托管审核”或“绕过审核”的功能。托管 H3 API 可能拦截违法、色情或侵权内容。
- 本地 profile 不硬编码成“官方 NSFW 支持”。部署者可配置合法成人内容政策，但必须拒绝未成年人、非自愿私密内容、未经授权的真实人物色情深伪、违法与侵权内容，并记录素材授权。

## 13. 测试与验收证据

每轮至少覆盖：

1. 单元：路由矩阵、`17k+5` 帧换算、引用稳定编号、悬空 `@` 引用、9/3/3/12 参考上限、音频唯一参考拒绝、profile 参数耦合。
2. API 集成：上传、图校验、幂等提交、队列/历史恢复、失败映射、下载鉴权、路径穿越、超大文件和损坏媒体。
3. ComfyUI dry-run/contract：`/object_info` 能发现所需节点和模型；模板节点输入与当前版本一致。
4. 实机视频：T2VA、I2VA、FL2VA、图片 Ref2VA、视频＋音频 Ref2VA；用 `ffprobe` 验证 MP4、24 FPS、有效尺寸/时长、音轨。
5. 实机图片：至少一个 text-to-image profile 产出可解码、尺寸正确、可下载图片；若声明 image-edit，再测真实参考图编辑。
6. UI E2E：拖拽、连线、参数、错误反馈、刷新后恢复、进度、预览、下载；桌面与窄屏基本可用。
7. 影视审查：主体一致性、动作连续、镜头动机、构图、节奏、首尾帧落点、声音同步、对白与口型、参考素材关系。

“接口返回 200”不构成视频/图片能力验收。必须保留生成任务 ID、规范化 graph、最终 prompt、工作流版本、输出文件哈希、`ffprobe`/图片解码报告和影视审查结论。

## 14. 一手资料与工程推断

一手资料：

- MiniMax H3 官方仓库：https://github.com/MiniMax-AI/MiniMax-H3
- MiniMax 官方提示词资料：https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
- MiniMax 官方 H3 CLI 约束：https://github.com/MiniMax-AI/cli/blob/main/skill/h3-video/SKILL.md
- ComfyUI 原生 H3 节点：https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py
- Comfy-Org H3 模型页：https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/README.md
- Comfy-Org 官方 H3 工作流模板：https://github.com/Comfy-Org/workflow_templates/tree/main/templates
- ComfyUI Server 路由：https://docs.comfy.org/development/comfyui-server/comms_routes
- ComfyUI Server 通信概览：https://docs.comfy.org/development/comfyui-server/comms_overview
- ComfyUI FLUX.1 生图教程：https://docs.comfy.org/tutorials/flux/flux-1-text-to-image
- MiniMax H3 许可证：https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE

工程推断/决策：自有 DSL 而非任意 Comfy 图、三层校验、124–362 帧 UI 网格、三个首批尺寸预设、profile 参数耦合、轮询/history 对账、任务持久化、受控视频/音频 staging、独立 checkpoint 生图 profile、部署者可配置但不能绕过法律与权利约束的内容政策。这些都需通过五轮开发、测试、CR 和影视审查验证。
