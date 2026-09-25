# 分句改词

`yingsinger` 用中文原词定位原曲的每个乐句，然后逐句演唱新词，按原时间放回原伴奏。
当前支持 1–180 秒歌曲、每句 0.4–20 秒；原词、新词非空且行数相同。
数字须写成汉字，不接受英文、拼音或 `[Verse]` 标签。默认使用原唱参考；可勾选其他参考音色，上传 3–20 秒清晰中文人声或清唱。系统额外分离参考人声、用 SeACo-Paraformer 识别参考文字，再将音频和文字作为每句 Singer 的音色条件。原曲仍提供旋律、句子时间轴与伴奏；参考识别失败会明确停止。

网页选择“分句改词 · 保留原伴奏（YingMusic）”，校正原词、逐行填写新词，先试听第一段，再生成完整歌曲。
默认 64 步、CFG 3。生成记录提供原曲/结果对比与人声/伴奏分轨，支持继续调节混音。
自动识别仍可能误字、误分行；识别结果是编辑辅助，必须核对。

```sh
h3ctl voice rewrite ./song.wav --engine yingsinger \
  --original-lyrics-file ./original.txt --lyrics-file ./new.txt \
  --steps 64 --cfg 3 --seed 666 --preview --to ./preview.wav
# 去掉 --preview 生成完整片段
```

ACE-Step Cover 保留为整曲重创作实验模式；它不满足锁定原曲的改词需求。
现有 SoulX 路径未替换。分句改词以新的 engine 身份持久化，历史、任务幂等和草稿不迁移。

## 部署

服务器环境变量 `H3_STUDIO_LYRICS_RUNTIME` 指向机器私有 JSON，不放入 Git。
配置清单字段均为绝对路径：

| 字段 | 内容 |
| --- | --- |
| `root` / `python` / `deps` | YingMusic-Singer-Plus 代码、可运行的 Python、隔离依赖目录 |
| `models` | 官方 HF `config.json` 与 `model.safetensors` 所在目录 |
| `separator_root` | 已部署 YingMusic-SVC 仓库 |
| `separator_config` / `separator_checkpoint` | BSR 三轨分离 YAML / 权重 |
| `align_python` / `align_deps` | SoulX Python 基础环境 / Qwen ASR 隔离依赖目录 |
| `align_models` | Qwen3-ForcedAligner-0.6B 本地模型目录 |
| `asr_models` | 本地中文 SeACo-Paraformer 模型目录 |
| `repository_revision` | `baa409c2e7e5e775f09b4e92a220808f4827d2cc` |
| `model_revision` | `9b3f444f2bccd77fbc03e32eb7c334fc96040b8d` |
| `aligner_revision` | `c7cbfc2048c462b0d63a45797104fc9db3ad62b7` |

官方来源：[YingMusic-Singer-Plus](https://github.com/ASLP-lab/YingMusic-Singer-Plus)、
[模型](https://huggingface.co/ASLP-lab/YingMusic-Singer-Plus)、
[对齐模型](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)。

依赖增量见 `scripts/voice/yingsinger-deps.txt`，使用 `pip --target` 安装到独立目录，
禁止直接覆盖现有 Torch/Transformers 环境。依赖基础为已验证的 YingMusic-SVC Python 3.10 / CUDA Torch 环境；
系统需要 `ffmpeg`、`ffprobe`、`espeak-ng`。Qwen 对齐独立增量：`qwen-asr==0.0.6`、
`transformers==4.57.6`、`tokenizers==0.22.2`、`soynlp==0.0.493`、`nagisa==0.2.11`、
`qwen-omni-utils==0.0.8`、`DyNet38==2.2`。
模型需完整下载且固定 revision；不允许 `/dev/shm` 临时模型作为正式部署依赖。

`yingsinger_worker.py` 保持 JSON RPC 主进程，将分离、CPU 对齐、演唱隔离为子进程；
同一任务的全部阶段占用既有 GPU 租约，继承进程组，取消/切换时一起终止。
各阶段结束释放模型，避免对齐与演唱依赖冲突，也不修改上游源码。

输出包含 `converted.wav`、`dry-vocal.wav`、`accompaniment.wav`、`alignment.json` 和
`render-report.json`。后两者供服务器审计；伴奏浮点样本摘要在分离后与保存后须相等。
缺字、零时长、重叠、明显未覆盖人声或超过单句时长限制时停止，并返回可操作错误；不会静默丢字或强制拉伸。

## 验收边界

在 RTX 5090 上以实际 19 秒歌曲测试了整段、分句、首句试听、识别和回混。
整段生成出现抢唱及多唱，未采用；分句显著改善新词覆盖，使用不同种子及 32/64 步复测。64 步作为质量优先默认值。
自动 ASR 仍可能混淆同音字及相近唱词，音高检测也存在偏差；这些检查不替代听感验收，
不承诺任意歌曲和任意长度的新词都能逐字完美或完全复制原唱。

结果卡片可将当前选中的 mix / dry_vocal / accompaniment / remix 存到资产库。`POST /api/voice/tasks/:id/assets` 接受 `{ "track": "mix" }`，返回 `{asset,reused}`；按任务、音轨与内容摘要去重。重新混音后保存新资产，删除任务不删除已保存资产。API 复用原认证、配额、媒体校验与变更锁；不经浏览器下载回传。
