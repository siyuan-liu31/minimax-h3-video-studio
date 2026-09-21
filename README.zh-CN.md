# MiniMax H3 Video Studio

<p align="center">
  <a href="README.md">English</a> · 简体中文 · <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <strong>面向 Agent 的视频、图片与换声创作工作台</strong>
</p>

<p align="center">
  MiniMax H3 视频 · Qwen-Image 2.1 BF16 · 换声 · 可视化界面 + h3ctl CLI
</p>

<p align="center">
  <a href="#三步启动">三步启动</a> ·
  <a href="docs/installation.md">完整安装指南</a> ·
  <a href="docs/h3-prompt-guide.md">H3 提示词指南</a> ·
  <a href="docs/long-video.md">长视频说明</a> ·
  <a href="docs/releasing.md">发布规范</a>
</p>

MiniMax H3 Video Studio 是自托管的视频、图片和换声创作工作台。前端提供持久化节点画布、MiniMax H3 视频、多模型生图与独立换声工作区；面向 Agent 的 `h3ctl` CLI 可直接调用同一后端，使用持久任务 ID 和 JSON/JSONL 回执。ComfyUI 可运行在本地或远程 GPU 上。

> MiniMax H3 Video Studio 是独立社区项目，与 MiniMax 和 ComfyUI 没有隶属或官方背书关系。

> 既有界面截图使用经用户授权公开的演示素材；Qwen-Image 2.1 与换声面板截图来自不含用户素材的干净演示工作区。仓库不包含原始素材、模型权重或生成视频文件。

<p align="center">
  <img src="docs/assets/readme/canvas-workflow.png" width="100%" alt="MiniMax H3 Video Studio 节点画布示例：参考图、H3 视频节点和输出节点组成生成工作流">
</p>

```mermaid
flowchart LR
  P[图片参考] --> V[H3 视频节点]
  M[视频 / 音频参考] --> V
  P --> I[图片生成节点]
  V --> O[输出节点]
  I --> O
  O --> R[结果库]
  R -. 显式保存 .-> A[资产库]
```

## 生成效果示例

下面的轻量动图截取自 MiniMax H3 Video Studio 生成的 9:16、15.08 秒带音频演示视频，展示有效画面开始后的连续 5 秒。这里只公开无声动图示例，完整生成文件不进入代码仓库。

[![MiniMax H3 Video Studio 生成视频动图示例：舞台上的动画歌手](docs/assets/readme/generated-video-preview.gif)](docs/assets/readme/generated-video-preview.gif?raw=1)

[动图未显示？点击这里直接打开原始 GIF。](docs/assets/readme/generated-video-preview.gif?raw=1)

## 核心能力

### 节点画布

- 在同一画布编排图片、视频、音频参考、H3 视频、生图和输出节点。
- 输入 `@` 引用当前节点素材；连接线与显式模式共同决定实际工作流。
- 图像、视频剪辑和抽帧会生成新的结果节点，不会自动进入资产库；需要复用时再从节点右键保存到资产。
- 本地图片、视频和音频可以拖入画布；节点内媒体不会被误判为本地上传。
- 画布、资产和任务状态都可刷新恢复，结果支持预览和下载。

### 七种视频创作模式

<p align="center">
  <img src="docs/assets/readme/video-modes.png" width="760" alt="H3 Video 节点支持 Auto、T2V、I2V、FL2V、R2V、V2V 和 RV2V 模式">
</p>

| 模式 | 用途 | 输入约束 |
| --- | --- | --- |
| `Auto` | 根据节点连线自动选择工作流 | 适合快速编排 |
| `T2V` | 文生视频 | 只使用文本提示词 |
| `I2V` | 单图生视频 | 1 张起始参考图 |
| `FL2V` | 首尾帧生视频 | 1–2 张端点图 |
| `R2V` | 多模态参考生视频 | 图片最多 9、视频最多 3、音频最多 3，混合文件合计最多 12 项 |
| `V2V` | 源视频重制 | 显式选择 1 条源视频 |
| `RV2V` | 源视频 + 多模态参考 | 源视频与额外参考分开绑定 |

H3 视频支持 16:9、9:16 和 24 FPS，时长使用真实的 `17k+5` 帧网格：124–362 帧，约 5.17–15.08 秒。采样可选择 Turbo LoRA 或官方基础 Profile；界面展示最终解析的模型、采样器、步数、LoRA 与调度参数，不把工作流预览冒充实际任务证据。

### 多模型生图与图像编辑

<p align="center">
  <img src="docs/assets/readme/image-models.png" width="760" alt="Image Generation 节点的图片模型选择器示例">
</p>

<p align="center">
  <img src="docs/assets/readme/qwen-image-21-panel.png" width="610" alt="Qwen-Image 2.1 BF16 图片节点及有序参考图输入">
</p>

| 模型 / 工作流 | 支持方式 | 适合场景 |
| --- | --- | --- |
| Z-Image Turbo BF16 / INT8 | 文生图、实验性单图 latent img2img | 快速写实、中英文字；BF16 为默认高画质档 |
| Z-Image Turbo + 社区 LoRA | 文生图、实验性单图 latent img2img | 独立 Profile，参数与模型绑定可审计 |
| Qwen-Image 2512 | 高质量文生图 | 人像、自然细节、图文排版 |
| Qwen-Image Edit 2511 | 单图指令编辑 | 保持主体并修改背景、服装或局部语义 |
| Qwen-Image 2.1 BF16 | 文生图、1–10 张有序图片指令编辑 | 原生 2K；使用完整 BF16 权重，不静默量化 |
| FLUX.2 Klein 4B / 9B | 文生图、1–4 张有序图片参考 | 多图人物、服装、场景和风格组合 |
| Anything V5 | Checkpoint 文生图 / 图生图 | 兼容回退 |

生图支持 1K/2K 与 16:9、9:16、3:4、1:1。Qwen-Image 2.1 使用原生 2K 尺寸，默认 40 步、CFG 1；不连接图片时文生图，连接 1–10 张图片时按槽位顺序进行指令编辑。可在提示词中写 `<image1>`、`<image2>` 或“图1”“图2”。其权重采用 [Qwen Research License](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/LICENSE)，非商业研究/评估以外的商业使用需要另行授权。尚未发布的 Z-Image-Edit 只显示为不可用能力，不会用 latent img2img 冒充指令编辑。模型许可与精确工作流见 [图片工作流文档](docs/image-workflows.md)。

### 音频换声与歌曲翻唱

<p align="center">
  <img src="docs/assets/readme/voice-studio-yingmusic.png" width="485" alt="YingMusic 换声工作区：上传、录音、步数与随机种子、分轨、回声及混响选项">
</p>

换声工作区支持拖拽上传、复用现有音频资产以及浏览器话筒录音；录音可明确指定为原音频或参考音频。Vevo2 FM-only 按参考音频替换说话或演唱音色。YingMusic-SVC 执行人声分离、转换和伴奏重混完整流程，支持调整步数、引导强度与随机种子以多次尝试。提交任务前选择是否保留分轨、是否加入回声和混响；完成后，最终混音、换声干声、伴奏可分别试听和导出。完成后要改变效果需新建任务。试听与下载使用同一持久结果。音频、图片和视频任务共用 GPU 独占队列。

```bash
h3ctl voice convert ./speech.wav --reference ./voice-reference.wav --engine vevo2
h3ctl voice convert ./song.wav --reference ./voice-reference.wav --engine yingmusic \
  --steps 75 --cfg 0.9 --seed 42 --keep-stems --echo=false --reverb=false
h3ctl voice download TASK_ID --track dry_vocal --to ./dry-vocal.wav
h3ctl generate image --profile qwen-image-2.1-bf16 \
  --prompt '蓝色陶瓷茶壶，产品摄影' --width 2048 --height 2048 --steps 40 --cfg 1 \
  --wait --download ./teapot.png
```

RTX 5090 实测：完整 BF16 权重下，2048 × 2048、40 步的 Qwen-Image 2.1 文生图约 75 秒、指令图生图约 120 秒；同一预发布网关也已跑通 CLI 文生图和图生图。一个已完成的 YingMusic 任务中，最终混音、干声、伴奏各自的试听与下载 WAV 字节一致。以上是实际用例而非性能保证；生成媒体和用户音频不进入仓库。

### 长视频：分段生成与续接

长视频工作区把现有视频和待生成片段放进统一时间线，可预览、切分、调整入出点、创建空白段，并按选中片段或依赖顺序执行。

<p align="center">
  <img src="docs/assets/readme/long-video-editor.png" width="100%" alt="MiniMax H3 Video Studio 长视频编辑器示例：监视器、分镜时间线和已有素材片段">
</p>

每个待生成片段都可以选择独立生成、使用上一段尾帧续接、把上一段视频作为 Ref2VA 参考，或通过 Motion Context 继承上一段的 H3 音视频 latent。续接配置、画面比例、有效时长、采样档、LoRA 强度、步数与 Seed 都会随项目保存。

<p align="center">
  <img src="docs/assets/readme/long-video-continuation.png" width="100%" alt="长视频片段选择上一段视频进行续接生成的界面示例">
</p>

```mermaid
flowchart LR
  S1[分段 1] --> C{分段 2 续接方式}
  C -->|不续接| N[独立生成]
  C -->|上一段尾帧| F[尾帧作为 Picture 1]
  C -->|上一段视频| V[视频作为 Ref2VA 参考]
  C -->|Motion Context| L[音视频 latent 与自动裁头]
  N --> S2[分段 2]
  F --> S2
  V --> S2
  L --> S2
  S2 --> S3[后续分段]
  S1 --> Merge[按顺序合并]
  S2 --> Merge
  S3 --> Merge
```

- 单段支持约 5.17–15.08 秒，失败后可重跑，前序变化会使依赖的下游片段失效并重新计算。
- 362 帧成片作为下一段视频参考时，只裁剪系统派生的 15 秒参考副本；最终合并仍使用完整成片。
- Motion Context 同时支持 Base 与 Turbo LoRA Profile，保留 Profile 允许范围内的自定义步数，并在拼接前自动移除复用的片头帧；相邻 latent 续接片段必须保持相同输出尺寸。
- `h3ctl video migrate-character` 可在实用上不限时长的源视频中替换一个明确指定的人物：按 24 FPS 精确分窗，用 Motion Context 传递音视频 latent；尾窗优先向前扩展到更大的合法重叠，仅在网格无法精确覆盖时使用最少补帧，并支持 `copy-source`、`reference-source`、`generate`、`mute` 音频策略。
- 合并由 FFmpeg 做可审计的硬切拼接，不宣称自动实现无缝音画衔接。
- 可用 `h3ctl video compose` 跑完整流程，也可分别调用项目、裁剪和拼接原子命令。完整合同见 [长视频文档](docs/long-video.md) 与 [Motion Context 合成长视频](docs/motion-context-long-video.md)。

### 资产与结果管理

- 资产库跨画布复用图片、视频和音频，支持搜索、文件夹、置顶、多选和批量删除。
- 文件夹删除只移除文件夹本身；其中的资产和子文件夹自动移动到上一级，不会误删媒体。
- 结果库统一展示生成结果与剪辑派生结果，支持置顶、混合多选、全选当前项、批量删除、预览和下载。
- 同内容素材按 SHA-256 复用并折叠展示，减少重复上传与存储占用。

### 面向 Agent 的 Go CLI

`h3ctl` 把素材传输、生图生视频、任务恢复、媒体派生、长视频项目和不限时长人物迁移拆成稳定的原子命令。`video.character_migration.plan`、`video.character_migration.produce` 和 `media.mux_audio` 为 Agent 提供严格的 Draft 2020-12 合同。它还提供基于 `yt-dlp` 的隔离本地 `douyin parse|download|serve` 工具与仅回环可访问的 Swagger API，不会打开 H3 SSH context。CLI 支持本地文件、远端资产 locator、机器地址可变的 SSH context，以及适合 Agent 解析的 JSON/JSONL 输出。构建、连接、Cookie 安全和完整命令说明见 [Go CLI 文档](docs/cli.md)。

```bash
h3ctl video compose --spec trilogy.json --to final.mp4 --timeout 0
h3ctl video migrate-character --source performance.mp4 --character hero.png \
  --source-subject "画面中央的舞者" --steps 4 --to migrated.mp4
```

> 品牌更名不影响兼容性：CLI 仍叫 `h3ctl`，现有 `H3_STUDIO_*` 环境变量、`h3-studio` 数据路径、API 合同和浏览器持久化键均保持不变。

仓库同时附带四个本地 Skill：[`H3 提示词编译`](skills/h3-ref2va-prompt-compiler/SKILL.md)、[`H3 人物对白生视频`](skills/h3-character-dialogue-video/SKILL.md)、[`H3 舞蹈复刻`](skills/h3-dance-replication/SKILL.md) 与 [`H3 不限时长人物迁移`](skills/h3-character-migration/SKILL.md)。

## 三步启动

> [!IMPORTANT]
> 一键脚本可以安装项目的锁定 Node 依赖、构建前端并启动服务，但不会代替系统安装 Python、Node.js、FFmpeg、ComfyUI、自定义节点或模型。新机器请先看 [完整安装与运行指南](docs/installation.md)。

环境要求：

- Node.js `>=22.13`
- Python `>=3.11`
- npm、`ffmpeg` 与 `ffprobe`
- 可访问的 ComfyUI，以及所选 Profile 需要的节点和模型
- 可选 `scenedetect>=0.6.4,<0.8`；未安装时智能分镜自动回退到 FFmpeg

在项目根目录执行：

1. 复制配置，确认 ComfyUI URL、数据目录和模型文件名。通过 loopback 或 SSH 隧道使用时无需 API Key；除非明确为公网部署启用鉴权，否则两个 Key 均保持为空。

   ```bash
   cp .env.example .env.local
   # 用编辑器修改 .env.local
   ```

2. 安装锁定的 Node 依赖并生成生产构建。

   ```bash
   python3 scripts/h3studio.py install
   ```

3. 检查依赖与 ComfyUI，然后启动 API 和生产前端。

   ```bash
   python3 scripts/h3studio.py doctor --check-comfy
   python3 scripts/h3studio.py start
   ```

打开 `http://127.0.0.1:3013`。`start` 会监督前后端进程；任一进程退出时会停止另一进程，按 `Ctrl-C` 即可完整关闭。

### 管理命令

```bash
# 只检查，不修改系统
python3 scripts/h3studio.py doctor

# 只显示将执行的安装或启动命令
python3 scripts/h3studio.py install --dry-run
python3 scripts/h3studio.py start --dry-run

# 自定义端口；三个端口必须不同
python3 scripts/h3studio.py start --port 3013 --internal-port 3014 --api-port 6020
```

`doctor` 会检查 Python、Node.js、npm、FFmpeg/FFprobe、项目文件、前端依赖/构建和密钥接线；`--check-comfy` 另检查 ComfyUI `/system_stats`。它不会下载依赖，也不能证明所有 Profile 的节点和模型已经齐全；启动后请以 `/api/capabilities` 和界面的可用性提示为准。等价 npm 命令为 `npm run doctor`、`npm run install:studio` 和 `npm run start:studio`。

## 本地开发

```bash
npm ci
cp .env.example .env.local

# 终端 A：API
set -a && source .env.local && set +a
python3 -m server

# 终端 B：前端；/api 代理到 6020
set -a && source .env.local && set +a
npm run dev -- --host 127.0.0.1 --port 3013
```

如果启用了 API Key，只把同一个值放进服务端的 `H3_STUDIO_API_KEY` 与前端代理进程的 `H3_STUDIO_PROXY_API_KEY`；密钥不会进入浏览器 bundle。

## AutoDL / 远程使用

默认情况下 API、内部前端和公开入口都只监听 loopback。远程机器启动服务后，在本地建立 SSH 隧道：

```bash
# 远端机器
python3 scripts/h3studio.py start

# 本地电脑：只转发同源前端入口
ssh -N -L 16020:127.0.0.1:3013 -p <PORT> <SSH_USER>@<HOST>
```

浏览器打开 `http://127.0.0.1:16020`。不要为了省略隧道直接暴露 `0.0.0.0`；确需公网访问时，请配置防火墙、TLS 反向代理和强 API Key。模型、素材、生成结果、API Key 和 SSH 密码不得提交到 Git。

## 关键配置

以 [`.env.example`](.env.example) 为基线。`h3studio.py` 会自动读取项目根目录的 `.env.local`，也可用 `--env-file <path>` 指定；已有进程环境变量优先。

| 变量 | 用途 |
| --- | --- |
| `COMFY_URL` | ComfyUI HTTP 地址 |
| `H3_STUDIO_HOST` / `H3_STUDIO_PORT` | Python API 监听地址与端口，默认 `127.0.0.1:6020` |
| `H3_STUDIO_WEB_HOST` / `PORT` / `H3_STUDIO_INTERNAL_WEB_PORT` | 生产前端公开地址、公开端口和内部端口 |
| `H3_STUDIO_DATA_ROOT` | 资产与任务元数据目录，远端建议放在数据盘 |
| `H3_STUDIO_COMFY_INPUT` / `H3_STUDIO_COMFY_OUTPUT` | ComfyUI 输入与输出目录 |
| `H3_STUDIO_*_MODEL` / `H3_STUDIO_*_LORA` | 模型 Profile 使用的文件名 |
| `H3_STUDIO_API_KEY` / `H3_STUDIO_PROXY_API_KEY` | 公网部署可选的同值密钥；loopback/SSH 使用时均留空 |
| `H3_STUDIO_COMFY_IDLE_FREE_SECONDS` | ComfyUI 全局队列空闲后调用 `/free` 的秒数；`0` 表示禁用 |
| `H3_STUDIO_MAX_ASSET_STORAGE_BYTES` | 资产存储上限 |
| `H3_STUDIO_MAX_MOTION_CONTEXT_STORAGE_BYTES` | Motion Context 持久 latent 存储上限 |
| `H3_STUDIO_MAX_ACTIVE_JOBS` | 活跃任务上限 |
| `H3_STUDIO_MAX_PROJECT_JSON_BYTES` | 长视频项目定义上限，默认 32 MiB |
| `H3_STUDIO_ASSET_TTL_DAYS` | 管理员手动垃圾回收使用的默认保留天数 |

外部 Profile 放在 `H3_STUDIO_DATA_ROOT/profiles/*.json`。清单只能选择代码已审查的工作流编译器；新类型必须先增加适配器与测试，不能通过清单执行任意 ComfyUI graph、路径或命令。详细变量、模型目录和故障排查见 [安装指南](docs/installation.md)。

## 项目结构

```text
app/       React 节点画布与工作区 UI
server/    Python 标准库 API、存储、任务与 ComfyUI 工作流编译
scripts/   安装、启动、诊断、长视频与运维工具
skills/    项目附带的 H3 提示词编译与生产 Skills
docs/      安装、架构、模型工作流、发布规范与 LLM 代码地图
tests/     前端构建、渲染和源码合同测试
```

后续开发请先阅读 [AGENTS.md](AGENTS.md) 和 [LLM Wiki](docs/llm-wiki.md)，面向用户的变化见 [CHANGELOG](CHANGELOG.md)。LLM Wiki 是当前实现的导航入口。

## 测试

```bash
npm test
```

该命令依次执行 ESLint、TypeScript、生产构建、渲染测试，以及 Python 单元/API/长视频/运维测试。

## 能力边界

MiniMax H3 Video Studio 不把本地 H3-Base 768p 声称为官方未开源的 Context-IR/2K 全流程，也不提供所谓“官方 NSFW 开关”或审核绕过。部署者可以为合法成人内容配置本地模型政策，但必须拒绝未成年人、非自愿私密内容、未授权真实人物色情深伪、违法与侵权内容。

H3 的调度去噪比例直接映射 `BasicScheduler.denoise`，不是 CFG、LoRA 强度或已证明的参考保留权重。不同模型、节点和许可证的精确信息以 `/api/capabilities`、[图片工作流](docs/image-workflows.md) 和随任务保存的证据为准。
