# 长视频分段编排与验收

H3 的一次生成仍然是短片：**每个 segment 的生成请求必须在 5 秒到 `362 / 24` 秒内**。最后一档对应 362 帧、24 FPS，即 `15.083333...` 秒（界面显示 15.08 秒）。长视频不是继续调大单次 H3 上限，而是把短段按顺序生成并合并；因此项目总时长没有 15.08 秒的模型级上限，可按 segment 继续组合。当前单项目最多 1000 段（使用最长档时超过 4 小时），这只是防止异常请求失控的 body-complexity 安全边界；跨项目继续合并时，总成片时长可继续扩展。

这里要区分生成输出与参考素材：362 帧只适用于 H3 新生成的 segment。用户上传的视频或音频参考仍须遵守每段最长 15 秒、各自合计最长 15 秒的素材预算；`previous_video` 使用的是系统记录的上一段生成输出，不改变用户上传素材的限制。

## 续接方式

项目至少包含三段，前三段用来覆盖完整验收路径：

1. `none`：首段独立生成。
2. `tail_frame`：服务端从上一段永久输出提取尾帧，并把其资产回执绑定到本次 attempt。
3. `previous_video`：服务端把上一段永久视频作为参考，并把来源段、来源任务、来源 SHA-256 和派生资产写入 attempt 回执。
4. `motion_context`：复用上一段 H3 视频/音频 latent，按 `video_frames` 与 `audio_frames` 构造下一段上下文，并在保存前自动裁掉重复头部。Turbo LoRA 和 Base Direct Profile 都可使用；采样步数仍按当前 Profile 范围独立设置。

后续段可以继续使用任一种续接方式。`tail_frame` / `previous_video` 消耗一个 H3 像素参考槽；`motion_context` 不消耗参考槽，但相邻生成段必须保持完全相同的输出尺寸。最终合并仍使用裁剪后的完整分段输出；直接导入的不同尺寸视频由原有合并管线按目标画布规范化，不能作为 Motion Context latent 前驱。客户端会拒绝并发激活多个段、后段早于前段启动、续接来源不匹配，以及缺少来源哈希的“看似完成”回执。

Motion Context 的视频窗口只能是 5、22、39 或 56 帧，音频窗口为 0..240 帧，默认分别为 22 与 24。上下文文件采用独立磁盘配额、原子复制与 SHA-256 校验；保存失败不会把已经落盘的 MP4 改判失败，但会明确阻断依赖它的下一段。安装版本、CLI Spec 和恢复流程见 [Motion Context 长视频说明](motion-context-long-video.md)。

## Go CLI 一键成片

推荐给 Agent 的主入口是：

```bash
h3ctl video compose --spec ./trilogy.json --to ./final.mp4 --timeout 0
```

它依次完成 Profile 身份补全、项目创建、顺序生成、续接裁头、合并等待和最终原子下载。连接中断不会取消服务端项目；回执中的 `project_id` 可继续交给 `h3ctl project get|run|wait|merge|download`。`h3ctl video trim` 是现有媒体裁剪的领域别名，`h3ctl video concat PROJECT_ID` 是项目合并的领域别名。

## 准备 manifest

复制 [example-manifest.json](../scripts/long_video/example-manifest.json)，然后至少修改：

- 每段 `prompt`、`parts`、`duration` 和 `seed`；
- `duration` 最大可写精确值 `15.083333333333334`（`362 / 24`）；不要用更大的近似值；
- 可选的视频 `parameters.denoise`（`0.05..1`，默认 `1.0`）；这是去噪幅度，不是图片 CFG，长视频 segment 不接受 CFG；
- 从当前 `/api/capabilities` 取得的真实 `profile_id`、`profile_version`、`profile_digest`；
- 已上传素材的 32 位 `id`/`asset_id` 及严格角色；H3 Ref2VA 每段最多 9 张图片、3 个视频、3 个音频，混合文件合计最多 12 个；续接段会为派生尾帧/上一段视频预留 1 个对应类型及总量槽，因此显式混合引用最多 11 个；
- `acceptance` 中与画幅对应的 H3 尺寸：16:9 是 `1344×768`，9:16 是 `768×1344`。

Manifest 采用拒绝未知字段的严格 v1 schema，参考 [manifest.schema.json](../scripts/long_video/manifest.schema.json)。`output_name` 只能是安全的 `.mp4` 文件名，不能包含目录或 `..`。API Key 不允许写进 manifest。

可选的 `rerun` 会在第一次顺序生成完成后，通过 `PUT` 更新指定 segment 的 prompt/seed，再调用单段运行接口。它必须真的改变 prompt 或 seed；旧 attempt 保留，新 attempt 和续接来源继续进入证据。

## 先做离线 dry-run

在项目根目录执行：

```bash
python3 -m scripts.long_video \
  --manifest scripts/long_video/example-manifest.json \
  --output-dir artifacts/long-video \
  --dry-run
```

Dry-run 只验证 manifest 并打印计划，不创建网络客户端，不新建输出目录，也不会提交云任务。

## 远程验收

把 API Key 放在环境变量中，而不是命令参数或文件里：

```bash
export H3_E2E_BASE_URL=http://127.0.0.1:16020
export H3_E2E_API_KEY='从部署环境读取的密钥'

python3 -m scripts.long_video \
  --manifest /safe/path/long-video.json \
  --output-dir /safe/path/acceptance-output \
  --timeout 7200 \
  --interval 3
```

`H3_E2E_BASE_URL` 必须是纯 HTTP(S) origin，不能带账号、密码、query、fragment 或子路径。客户端继承 E2E 层的手动重定向策略：JSON、下载和上传均不自动跟随重定向，绝不会把 `X-API-Key` 带到另一个 origin。远端机器建议继续只监听 loopback，并先用 SSH 隧道把前端同源入口映射到本地。

执行顺序如下：创建项目 → `/run` → 逐段轮询 → 可选更新并重跑单段 → `/merge` → 下载 → `ffprobe` → 原子写入证据。任一阶段发生超时、协议错误、Ctrl-C、下载/哈希或媒体验收失败，客户端都会尽力调用项目 `/stop`，并保留 `.partial.json`，不会悄悄留下正在继续排队的验收任务。

## 停止路径验收

要专门验证停止行为，可在某段完成后请求停止：

```bash
python3 -m scripts.long_video \
  --manifest /safe/path/long-video.json \
  --output-dir /safe/path/stop-evidence \
  --stop-after-index 0
```

索引从 0 开始。此模式只验收停止回执，不执行 manifest 中的 rerun、merge 或下载。

## 验收证据

成功后输出目录包含合并视频和 `long-video-<project_id>.json`。证据保存：

- 输入 manifest 的规范化 SHA-256；
- 创建、初次顺序生成、重跑和最终项目回执；
- 每次有意义的状态变化及 segment/job/attempt 标识；
- `tail_frame`/`previous_video` 的来源段、来源 job、来源文件 SHA-256 和派生资产；
- 每个 segment 的 job 与工作流 SHA-256，以及 `BasicScheduler` 的实际 denoise/steps；Turbo LoRA 的步数须与请求及 Profile 范围一致并有 LoRA/模型强度证据，Base 可调步数且不得加载 Turbo LoRA；
- 合并下载的字节数与 SHA-256；
- `ffprobe` 的精确宽高、24 FPS、音轨、编码、帧数和时长。

最终时长按每段实际 H3 帧网格换算后求和，并允许 manifest 中配置的小容器误差（默认 ±0.25 秒）。服务端回执的尺寸、音轨、时长、SHA-256 与文件大小必须同时和本地下载/`ffprobe` 一致，单纯 HTTP 200 不算通过。
