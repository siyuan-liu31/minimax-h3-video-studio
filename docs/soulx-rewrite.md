# SoulX 中文保留旋律改词

H3 使用 ComfyUI-MIDI-Edit 的音频转谱和 SoulX 合成 core，歌词对齐由 H3 保留原始音高边界与停顿，运行于独立 Worker，复用现有 GPU 排队、取消、持久任务及分轨下载接口。无需先启动 ComfyUI 工作流；这不是向现有 ComfyUI 安装节点的操作。上游节点可另行使用同一模型目录。

## CLI 和前端

```sh
h3ctl voice rewrite song.wav --lyrics-file new-lyrics.txt \
  --original-lyrics-file original-lyrics.txt --steps 32 --cfg 3 --seed 42 \
  --to rewritten.wav
h3ctl voice rewrite song.wav --lyrics-file new-lyrics.txt --reference singer.wav --detach
h3ctl voice status TASK_ID
h3ctl voice wait TASK_ID
h3ctl voice download TASK_ID --track dry_vocal --to vocal.wav
h3ctl voice download TASK_ID --track accompaniment --to backing.wav
```

歌词文件为 UTF-8。原歌词和参考音频可省略；未指定参考时使用原唱。前端入口为侧栏「换声」→「SoulX · 保留旋律改词」。选择源音频、填写新歌词即可提交；可调整步数/引导/种子，查看历史歌词、复用参数、试听和下载三轨。

当前支持中文。源音频需要有可识别的演唱；纯伴奏无法自动提供原唱的歌词节奏。字数和句长尽量接近原词；大幅增加字数可能挤字，旋律约束不代表演唱能逐样本复刻。长音频由上游分段处理并按原始时间位置拼回。

## 独立运行时

固定以下 revision，不直接跟随 main：

- [ComfyUI-MIDI-Edit](https://github.com/ahkimkoo/ComfyUI-MIDI-Edit): `801cb858464dd33312dcec27ed7151d130ae93ee`
- SoulX-Singer 子模块：`81aeb3ae772c70093c3de74dc23c92d983801ae4`
- [SoulX-Singer 模型](https://huggingface.co/Soul-AILab/SoulX-Singer): `40493ad90286056c7a9095035164434a79daa8c9`
- [预处理模型](https://huggingface.co/Soul-AILab/SoulX-Singer-Preprocess): `83dc50289d22a81b1e9998f5b9e111aef7c1fdcd`

5090 验收环境以已支持 Blackwell 的 Python 3.10 / PyTorch 2.12.1+cu130 环境建立独立 `venv --system-site-packages`，新增依赖只装在 SoulX venv，不能修改现有换声/ComfyUI 环境。额外依赖：

```text
funasr==1.3.0
modelscope==1.40.1
g2p-en==2.1.0
g2pM==0.1.2.5
onnxruntime==1.23.2
beartype==0.14.1
rotary-embedding-torch==0.3.5
praat-parselmouth==0.4.7
pretty_midi==0.2.11.post0
pyloudnorm==0.2.0
webrtcvad==2.0.10
ToJyutping==3.2.0
ml_collections==1.1.0
loralib==0.1.2
einops-exts==0.0.4
protobuf==4.25.8
```

该列表是已有 h3-voice 基础环境的增量，非从空环境安装的完整锁文件。protobuf 7 会破坏该基础环境的 wandb 导入；应固定为上述版本。保留官方 Singer FP32，使用 score 音符控制（上游同时参考原 F0 轮廓细分音符），关闭自动移调。此选择在验收样本的歌词识别中优于 melody/hybrid；不保证原唱细微滑音完全一致。

模型放置于 `$H3_STUDIO_SOULX_MODELS/Soul-AILab/` 下的两个同名仓库目录。Singer 下载 `model.pt`、`config.yaml`；预处理下载 `mel-band-*/*`、`dereverb*/*`、`rmvpe/*`、`rosvot/*/*`、`speech_seaco*/*`。**ROSVOT 自带的 `rosvot/rmvpe/model.pt` 也必须下载**，不能只下载顶层 rmvpe。中文模式无需 Parakeet 英文权重。

提前安装 NLTK 的 cmudict、averaged_perceptron_tagger、averaged_perceptron_tagger_eng、punkt、punkt_tab 数据至运行用户的私有目录（包括上游默认 `~/.cache/nltk_data`）；避免首任务联网下载。目录所有者应为运行用户。

服务配置：

```sh
H3_STUDIO_SOULX_ROOT=/path/to/ComfyUI-MIDI-Edit
H3_STUDIO_SOULX_PYTHON=/path/to/soulx-env/bin/python
H3_STUDIO_SOULX_MODELS=/path/to/models
```

模型、运行时、素材与输出均在代码 release 外。`GET /api/voice/capabilities` 显示文件是否完整；最终可用性仍需真实 GPU 任务验证。Worker 日志位于数据根的 `logs/voice-worker-soulx.log`，stdout 专供 JSON 协议。

## 验收

先运行 `go test ./...`、`go vet ./...`（cli 目录）及根目录 `npm test`。GPU 验收同时覆盖 CLI 提交和浏览器提交、任务刷新恢复、三轨下载；检查 WAV 时长、有限值、非静音、人声歌词，以及有起唱偏移的音频。用短片段先检查歌词节奏后再跑整曲。失败和取消不得留下可下载半成品。
