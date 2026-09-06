# MiniMax H3 Video Studio Agent Guide

## 开始前

- 先读 `docs/llm-wiki.md`，再读与任务直接相关的源码和测试。
- 执行 `git status --short --branch`，保护现有改动。
- 当前实现以源码、测试和 `/api/capabilities` 为准；不要把私有验收证据、用户素材或生成结果加入仓库。
- 检查 LLM Wiki 页头的“最后更新”。超过 7 天时，先按当前源码、API 和部署状态校准代码地图并更新日期；不能只改日期。

## 核心约束

- 视频输出：24 FPS、124–362 帧、`17k+5` 网格；H3 Ref2VA 单次最多 9 张图片、3 个视频、3 个音频，混合文件合计最多 12 项。
- `first_frame` / `last_frame` 是工作流角色；`<Picture N>` 是提示词标签，不要混为一谈。
- 用户视频 Prompt 采用只读提交语义：只解析素材标签，不擅自翻译、扩写或重组。
- 模型能力由版本化 Profile 与 ComfyUI capability 检测共同决定；前端不得写死未验证模型能力。
- 不通过降分辨率、降精度或更换模型来掩盖性能和内存问题，除非用户明确要求。
- 不提交 `.env.local`、密码、Token、模型文件、素材、生成结果、`node_modules`、构建产物或远端数据目录。

## 开发流程

1. 定位现有合同和回归测试。
2. 做最小且可维护的修改。
3. 先跑相关测试，再按风险跑 TypeScript、ESLint、构建或完整测试。
4. 检查用户可见行为、错误信息、持久化与刷新恢复。
5. 架构/API/数据合同变化时更新 `docs/llm-wiki.md`。

影响代码、配置、依赖、数据、API、构建、部署或运行行为的开发任务，至少完成一整轮“开发 → 测试 → 修复 → 复测 → 复核”，确认测试发现的问题已修复并通过复测后才能交付。高风险跨层修改应按需要增加轮次。只有工作范围确实独立时才并行分工；不满足并行条件时，按最小范围串行完成修复和测试。

仅修改 README/Markdown、注释、文案、拼写或排版，且明显不影响运行行为时，可跳过运行测试；交付前仍需复核 diff，并按需检查链接、格式和示例一致性，交付时说明未运行测试及原因。

## Changelog

- 每次发布都必须更新 `CHANGELOG.md`：把本次用户可见变化从 `Unreleased` 归档到带日期的版本段。
- 只记录功能、修复、兼容性、数据迁移、性能与运维变化；纯重构可省略。
- Changelog、Git 提交、实际 release 和健康检查结果必须一致，未部署内容保留在 `Unreleased`。

## 版本与 GitHub 发布

- 版本号采用 SemVer，以 `package.json` 为唯一版本源；修改版本时同步更新 `package-lock.json`。
- GitHub 默认分支固定为 `main`，不另建或维护 `master` 镜像分支。
- 正式版本必须同时具备：`main` 上的版本提交、`CHANGELOG.md` 对应版本段、注释 Tag `vX.Y.Z` 和同名 GitHub Release，四者指向同一发布内容。
- 完整发布检查清单见 `docs/releasing.md`。不得只改版本号、只打 Tag 或只创建 Release。
- GitHub 发布必须获得用户在当前任务中的明确授权。未明确要求“发布到 GitHub”时，只能在本地开发、测试和准备发布内容；禁止执行 `git push`、推送 Tag、创建或修改 GitHub Release，也不得自行创建仓库或改变仓库可见性。

## 部署

- 开发机使用不可变 release 目录和 `current` 软链接；不要直接修改正在运行的 release。
- 新 release 独立构建并校验后再原子切换，保留旧 release 以便回滚。
- 数据、Profile、日志、ComfyUI input/output 与代码 release 分离，不随部署覆盖。
- 切换后检查 `/health`、前端首页和关键 API；失败时立即恢复旧软链接。
