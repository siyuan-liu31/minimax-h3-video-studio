export type UiLanguage = "en" | "zh-CN";

export const UI_LANGUAGE_STORAGE_KEY = "h3-studio-ui-language-v1";

const ENGLISH_COPY: Readonly<Record<string, string>> = {
  "画布": "Canvas",
  "资产": "Assets",
  "结果": "Results",
  "长视频": "Long Video",
  "上传": "Upload",
  "换声": "Voice Conversion",
  "音频换声": "Voice Conversion",
  "换声工作区": "Voice Conversion Workspace",
  "参考音色转换 · 持久任务": "Reference voice conversion · Durable tasks",
  "关闭换声抽屉": "Close voice conversion drawer",
  "换声模式": "Conversion Mode",
  "Vevo2 · 语音/清唱换音色": "Vevo2 · Speech / Solo Vocal Conversion",
  "YingMusic-SVC · 完整歌曲换音色": "YingMusic-SVC · Full Song Conversion",
  "给一段说话或清唱，换成参考音频的音色。": "Convert speech or solo vocals to the reference voice.",
  "分离歌曲人声、换成参考音色，再与原伴奏重混。": "Separate vocals, convert to the reference voice, then remix the original accompaniment.",
  "正在检查模型可用性…": "Checking model availability…",
  "无法读取模型能力，暂不能提交。": "Could not load model availability; submission is disabled.",
  "模型已就绪": "Model ready",
  "原音频": "Source Audio",
  "参考音频": "Reference Audio",
  "要转换的说话或歌曲": "Speech or song to convert",
  "目标人物的清晰声音": "Clear recording of the target voice",
  "拖拽音频到这里": "Drop audio here",
  "上传中…": "Uploading…",
  "替换音频": "Replace Audio",
  "选择本地音频": "Choose Local Audio",
  "上传原音频": "Upload source audio",
  "上传参考音频": "Upload reference audio",
  "或从资产库选择": "Or select from Assets",
  "原音频资产": "Source audio asset",
  "参考音频资产": "Reference audio asset",
  "原音频试听": "Preview source audio",
  "参考音频试听": "Preview reference audio",
  "选择已上传音频…": "Choose uploaded audio…",
  "话筒录音": "Microphone Recording",
  "录完试听，确认后上传为原音频": "Preview before uploading as source audio",
  "开始录音": "Start Recording",
  "正在请求话筒权限…": "Requesting microphone permission…",
  "取消录音": "Cancel Recording",
  "录音中": "Recording",
  "停止录音": "Stop Recording",
  "丢弃录音": "Discard Recording",
  "正在整理录音…": "Preparing recording…",
  "正在丢弃录音…": "Discarding recording…",
  "试听本地录音": "Preview local recording",
  "正在上传录音…": "Uploading recording…",
  "使用这段录音": "Use This Recording",
  "最长 5 分钟。仅在点击“使用这段录音”后上传；离开面板会关闭话筒并丢弃未上传录音。": "Maximum 5 minutes. Uploads only after you choose Use This Recording. Closing the panel stops the microphone and discards unuploaded audio.",
  "当前浏览器无法录音；请使用 HTTPS 或 localhost，并检查浏览器话筒支持": "Recording is unavailable. Use HTTPS or localhost and check browser microphone support.",
  "未获得话筒权限，请在浏览器中允许使用话筒": "Microphone access was denied. Allow access in your browser.",
  "未找到可用话筒，请检查设备连接": "No microphone found. Check the device connection.",
  "话筒被其他程序占用，请关闭后重试": "The microphone is in use by another app. Close it and retry.",
  "无法开始录音": "Could not start recording",
  "录音中断，请重试": "Recording was interrupted. Please retry.",
  "录音音频数据无效": "Invalid recorded audio data",
  "录音过大，请缩短录制时间": "Recording is too large. Record a shorter clip.",
  "没有录到声音，请重试": "No audio was recorded. Please retry.",
  "支持 WAV、FLAC、OGG、MP3；服务端会校验真实格式。结果输出 WAV。": "WAV, FLAC, OGG, and MP3 are supported. The server checks the actual format. Output is WAV.",
  "开始换声": "Convert Voice",
  "换声任务": "Voice Tasks",
  "换声任务历史": "Voice Task History",
  "正在读取任务…": "Loading tasks…",
  "任务读取失败，点击刷新重试。": "Could not load tasks. Refresh to retry.",
  "还没有换声任务。": "No voice conversion tasks yet.",
  "排队中": "Queued",
  "运行中": "Running",
  "取消中": "Cancelling",
  "等待 GPU": "Waiting for GPU",
  "加载模型": "Loading model",
  "正在换声": "Converting voice",
  "已中断": "Interrupted",
  "等待视频/图像任务完成": "Waiting for video/image task",
  "等待另一换声任务完成": "Waiting for another voice task",
  "等待驻留模型释放": "Waiting for resident model release",
  "每个音频区域一次只能放入一个文件": "Drop one audio file at a time in each slot",
  "只接受 WAV、FLAC、OGG 或 MP3 音频文件": "Only WAV, FLAC, OGG, or MP3 audio files are accepted",
  "音频上传失败": "Audio upload failed",
  "换声提交失败": "Voice task submission failed",
  "任务操作失败": "Voice task action failed",
  "换声进度": "Conversion progress",
  "换声结果试听": "Preview converted audio",
  "下载 WAV": "Download WAV",
  "取消任务": "Cancel Task",
  "删除记录": "Delete Record",
  "删除这条换声任务及其输出音频？此操作不可恢复。": "Delete this voice task and its output audio? This cannot be undone.",
  "帮助": "Help",
  "新建": "New",
  "添加素材": "Add Media",
  "自动保存": "Autosaved",
  "保存": "Save",
  "保存中…": "Saving…",
  "取消": "Cancel",
  "关闭": "Close",
  "删除": "Delete",
  "删除中…": "Deleting…",
  "重试": "Retry",
  "刷新": "Refresh",
  "搜索": "Search",
  "选择": "Select",
  "全选": "Select All",
  "反选": "Invert Selection",
  "查看": "View",
  "下载": "Download",
  "编辑": "Edit",
  "改名": "Rename",
  "置顶": "Pin",
  "取消置顶": "Unpin",
  "移除": "Remove",
  "上移分段": "Move Segment Up",
  "下移分段": "Move Segment Down",
  "播放": "Play",
  "模式": "Mode",
  "尺寸": "Dimensions",
  "时长": "Duration",
  "暂停": "Pause",
  "上一帧": "Previous Frame",
  "下一帧": "Next Frame",
  "视频": "Video",
  "图片": "Image",
  "音频": "Audio",
  "素材": "Media",
  "项目": "Project",
  "分段": "Segments",
  "成片": "Final Video",
  "草稿": "Draft",
  "空闲": "Idle",
  "排队": "Queued",
  "失败": "Failed",
  "已完成": "Completed",
  "已取消": "Canceled",
  "未保存": "Unsaved",
  "已保存": "Saved",
  "已就绪": "Ready",
  "准备就绪": "Ready",
  "处理中…": "Processing…",
  "生成中…": "Generating…",
  "提交中…": "Submitting…",
  "排队中…": "Queued…",
  "下载中…": "Downloading…",
  "上传中": "Uploading",
  "更新中…": "Updating…",
  "未分类": "Uncategorized",
  "未加载": "Not Loaded",
  "未知错误": "Unknown error",
  "时长未知": "Unknown duration",
  "尺寸未知": "Unknown dimensions",
  "帧率未知": "Unknown frame rate",
  "帧": "frames",
  "秒 ·": "sec ·",
  "时间未知": "Unknown time",
  "耗时未知": "Unknown elapsed time",
  "无可用音轨": "No usable audio track",
  "无参考素材": "No reference media",
  "没有可用素材": "No media available",
  "没有匹配资产": "No matching assets",
  "还没有资产": "No assets yet",
  "暂无历史任务": "No task history",
  "暂无已完成结果": "No completed results",
  "本地工作流": "Local workflow",
  "工作区导航": "Workspace navigation",
  "节点画布": "Node Canvas",
  "节点画布。右键新建，空白区域左键拖动平移，Ctrl 加滚轮缩放": "Node canvas. Right-click to create, drag empty space to pan, and use Ctrl + wheel to zoom.",
  "画布标签": "Canvas tabs",
  "新建画布标签": "New canvas tab",
  "关闭并从本地工作区移除": "Close and remove from local workspace",
  "新建节点": "New Node",
  "删除节点": "Delete Node",
  "删除素材": "Delete Media",
  "删除结果": "Delete Result",
  "添加到画布": "Add to Canvas",
  "连到生图": "Connect to Image Generation",
  "＋ 添加到画布": "+ Add to Canvas",
  "＋ 新建": "+ New",
  "＋ 上传新素材": "+ Upload Media",
  "＋ 建文件夹": "+ New Folder",
  "＋ 空白片段": "+ Blank Segment",
  "＋ 选择参考图": "+ Choose Reference Image",
  "↻ 刷新": "↻ Refresh",
  "远程资产库": "Remote Asset Library",
  "服务器已上传素材": "Media uploaded to the server",
  "搜索资产名称": "Search asset names",
  "按文件夹筛选": "Filter by folder",
  "全部文件夹": "All Folders",
  "新文件夹名称": "New folder name",
  "删除当前文件夹": "Delete Current Folder",
  "多选管理": "Select Multiple",
  "取消多选": "Cancel Selection",
  "全选当前": "Select Current Page",
  "显示重复": "Show Duplicates",
  "收起重复": "Hide Duplicates",
  "重复": "Duplicate",
  "选择重复项 (": "Select Duplicates (",
  "资产读取失败，可重试。": "Could not load assets. Try again.",
  "正在读取资产…": "Loading assets…",
  "换个搜索词或文件夹试试。": "Try another search term or folder.",
  "生成结果不会自动进入资产；请在“结果”中点“保存到资产”，或上传本地素材。": "Generated output is not added to Assets automatically. Save it from Results or upload local media.",
  "保存到资产": "Save to Assets",
  "✓ 已保存到资产": "✓ Saved to Assets",
  "已保存到资产": "Saved to Assets",
  "添加中…": "Adding…",
  "正在添加到画布…": "Adding to canvas…",
  "关闭资产抽屉": "Close assets drawer",
  "关闭结果抽屉": "Close results drawer",
  "关闭长视频抽屉": "Close long-video drawer",
  "生成与剪辑派生结果": "Generated and derived media",
  "完成的生成任务与剪辑派生媒体都会保留在这里。": "Completed generations and derived media are kept here.",
  "生成结果": "Generated Output",
  "派生结果": "Derived Output",
  "历史结果": "History",
  "生成历史": "Generation History",
  "清除旧记录": "Clear Old Records",
  "加载下一页结果": "Load More Results",
  "重试下一页": "Retry Next Page",
  "下一页加载失败": "Could not load the next page",
  "等待生成结果": "Waiting for generated output",
  "任务出现问题": "The task encountered a problem",
  "在画布预览": "Preview on Canvas",
  "预览、下载或重新生成当前结果": "Preview, download, or regenerate the current output",
  "点击加载原媒体": "Click to load original media",
  "缩略图恢复中…": "Recovering thumbnail…",
  "缩略图暂不可用": "Thumbnail unavailable",
  "正在缓冲视频…": "Buffering video…",
  "视频加载失败，点击重试": "Video failed to load. Click to retry.",
  "下载到本地": "Download",
  "下载已开始": "Download started",
  "重试下载": "Retry download",
  "查看实际工作流": "View Actual Workflow",
  "下载实际工作流": "Download Actual Workflow",
  "实际执行工作流": "Actual Workflow",
  "实际执行回执": "Execution Receipt",
  "来源任务回执": "Source Task Receipt",
  "任务状态": "Task Status",
  "生成视频": "Generate Video",
  "生成图片": "Generate Image",
  "采样方案": "Sampling Preset",
  "模式 × 档位自动解析具体 Profile": "Mode × preset resolves the exact profile",
  "Turbo4 · 4 步蒸馏 LoRA": "Turbo4 · 4-step distilled LoRA",
  "Base20 Direct · 优先成片": "Base20 Direct · Output First",
  "当前组合没有可用 Profile": "No profile is available for this combination",
  "Auto · 按连线判断": "Auto · Resolve from Connections",
  "Auto · 按已连参考图匹配": "Auto · Match Connected References",
  "没有与当前连线兼容的 Profile": "No Profile is compatible with the current connections",
  "已解析为 T2V · 文生视频": "Resolved to T2V · Text to Video",
  "尺寸、时长与高级参数": "Size, Duration & Advanced Settings",
  "尺寸、质量与高级参数": "Size, Quality & Advanced Settings",
  "当前解析配置": "Resolved Configuration",
  "图片 Prompt": "Image Prompt",
  "图片模型 / 工作流": "Image Model / Workflow",
  "图片模型实际 Prompt（只读）": "Actual Image Prompt (Read-only)",
  "填写图片提示词": "Enter an image prompt",
  "描述要生成或如何修改画面…": "Describe what to generate or how to edit the image…",
  "选择参考图": "Choose Reference Image",
  "直接使用“图1”、“图2”描述多图关系；系统按上方顺序绑定。": "Use “Image 1” and “Image 2” to describe multi-image relationships. References follow the order above.",
  "H3 视频 Prompt": "H3 Video Prompt",
  "字 · 输入 @ 引用本节点素材": "characters · Type @ to reference this node's media",
  "H3 视频提示词": "H3 video prompt",
  "粘贴完整 H3 提示词；输入 @ 选择素材": "Paste a complete H3 prompt; type @ to choose media",
  "输入 @ 选择素材": "Type @ to choose media",
  "只读提交": "Read-only Submission",
  "只替换素材 ID 为 H3 标签，不改写 Prompt。": "Only media IDs are replaced with H3 tags. The prompt is not rewritten.",
  "H3 官方模板建议视觉正文使用英文；系统不会翻译。": "The official H3 template recommends English for visual descriptions. The system does not translate prompts.",
  "查看 H3 Ref2VA 参考模板（只读）": "View H3 Ref2VA Template (Read-only)",
  "H3 Ref2VA 参考模板": "H3 Ref2VA Reference Template",
  "H3 创作模式": "H3 Creation Mode",
  "Director 工作流": "Director Workflow",
  "只改变工作流，不修改 Prompt": "Changes the workflow only; the prompt is unchanged",
  "根据当前连线选择 T2V、I2V、FL2V 或 R2V；不会自动把某条视频认作源视频。V2V/RV2V 必须显式选择。": "Selects T2V, I2V, FL2V, or R2V from the current connections. A video is never treated as the source automatically; V2V/RV2V must be selected explicitly.",
  "导出工作流模板": "Export Workflow Template",
  "R2V / V2V / RV2V 可导出工作流模板": "Workflow templates can be exported for R2V / V2V / RV2V",
  "当前模式由 Profile 编译；Director 模板适用于 R2V / V2V / RV2V。": "The current mode is compiled by the Profile. Director templates apply to R2V / V2V / RV2V.",
  "H3 最终提示词预览（只读）": "Final H3 Prompt Preview (Read-only)",
  "H3 最终提示词预览（只读） ·": "Final H3 Prompt Preview (Read-only) ·",
  "填写提示词后显示实际提交文本": "Enter a prompt to preview the submitted text",
  "校验中": "Validating",
  "服务端已确认": "Confirmed by server",
  "校验失败": "Validation failed",
  "本地预览": "Local preview",
  "重新校验": "Validate Again",
  "查看模式合同": "View Mode Contract",
  "查看工作流模式合同": "View Workflow Mode Contract",
  "创作模式": "Creation Mode",
  "Director 模式": "Director Mode",
  "工作流模式": "Workflow Mode",
  "T2V · 文生视频": "T2V · Text to Video",
  "I2V · 单图生视频": "I2V · Image to Video",
  "FL2V · 端点帧生视频": "FL2V · First/Last Frame to Video",
  "R2V · 多模态参考生视频": "R2V · Multimodal Reference to Video",
  "V2V · 源视频重制": "V2V · Video Restyle",
  "RV2V · 源视频 + 多模态参考": "RV2V · Source Video + References",
  "源视频": "Source Video",
  "视频来源": "Video Source",
  "请选择已连接的视频…": "Choose a connected video…",
  "从资产选择视频…": "Choose a video from Assets…",
  "引用": "References",
  "引用素材": "Reference Media",
  "参考素材": "Reference Media",
  "H3 容量：Picture 9 / Video 3 / Audio 3；混合输入合计最多 12 个文件，视频配对音轨不重复计文件数。": "H3 capacity: Picture 9 / Video 3 / Audio 3; mixed inputs allow up to 12 files, and a video's paired soundtrack does not count as a second file.",
  "H3 参考素材槽位": "H3 Reference Slots",
  "点击空槽从资产选择": "Click an empty slot to choose from Assets",
  "已用": "Used",
  "配对": "Paired",
  "当前节点的配对音轨": "Paired audio for this node",
  "素材没有可用音轨": "This media has no usable audio track",
  "音轨参考请在每个视频生成节点中独立开启。": "Enable audio reference separately on each video generator node.",
  "标签映射": "Tag Mapping",
  "按媒体类型映射 <Picture N>": "Map by media type to <Picture N>",
  "按媒体类型映射 <Video N>": "Map by media type to <Video N>",
  "按媒体类型映射 <Audio N>": "Map by media type to <Audio N>",
  "连接后由生图工作流程绑定": "Bound by the image workflow after connection",
  "已解码验证": "Decode Verified",
  "需要重新上传": "Re-upload Required",
  "点击加载原图": "Click to load full image",
  "加载视频预览": "Load Video Preview",
  "从远程素材 ID 恢复；若预览无效，请重新上传。": "Restored from the remote asset ID. Re-upload if the preview is unavailable.",
  "许可": "License",
  "（模型固定）": "(Model Fixed)",
  "Turbo LoRA 模式": "Turbo LoRA Mode",
  "Z-Image-Edit（尚未发布）": "Z-Image-Edit (Not Released)",
  "Z-Image-Edit 尚无已发布且经审核的本地模型绑定与官方工作流；实验性 latent img2img 不等同于 Z-Image-Edit。": "Z-Image-Edit has no released and reviewed local model binding or official workflow. Experimental latent img2img is not Z-Image-Edit.",
  "剪辑与派生": "Trim & Derive",
  "开始秒": "Start (seconds)",
  "结束秒": "End (seconds)",
  "裁剪视频": "Trim Video",
  "裁剪音频": "Trim Audio",
  "获取首帧": "Extract First Frame",
  "获取尾帧": "Extract Last Frame",
  "获取播放器当前帧": "Capture Current Frame",
  "指定时间（高级）": "Specific Time (Advanced)",
  "按时间获取": "Extract at Time",
  "分离音频": "Extract Audio",
  "移除音轨": "Remove Audio Track",
  "移除音频": "Remove Audio",
  "H3 参考音频": "H3 Reference Audio",
  "保留": "Keep",
  "优化为 H3 视频参考": "Optimize for H3 Video Reference",
  "取消处理": "Cancel Processing",
  "产物会作为新节点加入画布，由你决定是否保存到资产。": "The output is added to the canvas as a new node. You decide whether to save it to Assets.",
  "松开以添加图片、视频或音频": "Drop to add an image, video, or audio file",
  "画面比例": "Aspect Ratio",
  "有效时长": "Effective Duration",
  "H3 只支持 17k+5 帧网格；这里显示真实输出时长。": "H3 supports only the 17k+5 frame grid; the actual output duration is shown here.",
  "基础模型步数": "Base Model Steps",
  "Turbo LoRA 步数（4 推荐）": "Turbo LoRA Steps (4 Recommended)",
  "模型强度（LoRA）": "Model Strength (LoRA)",
  "调度去噪比例（实验）": "Scheduler Denoise (Experimental)",
  "截断调度前段": "Trim the start of the schedule",
  "1.00：完整调度": "1.00: Full schedule",
  "直接对应 H3 BasicScheduler.denoise；不是 CFG 或参考权重。官方模板默认 1.00，其他值请视为实验参数。": "Maps directly to H3 BasicScheduler.denoise; it is not CFG or reference weight. Official templates default to 1.00; treat other values as experimental.",
  "−1 为随机": "−1 for random",
  "生成随机 Seed": "Generate a random seed",
  "随机 Seed": "Random seed",
  "基础质量模式": "Base Quality Mode",
  "不加载蒸馏 LoRA；步数可在当前 Profile 允许范围内调整。": "Does not load the distilled LoRA. Steps can be adjusted within the current Profile limits.",
  "Z-Image-Edit · 尚未发布（不可用，不是 latent img2img）": "Z-Image-Edit · Not Released (Unavailable; not latent img2img)",
  "当前 Z-Image 单图流程是 latent img2img，不具备 Z-Image-Edit 的指令式语义编辑能力。": "The current single-image Z-Image workflow is latent img2img and does not provide Z-Image-Edit instruction-based semantic editing.",
  "图片质量": "Image Quality",
  "2K 像素数约为 1K 的 4 倍，显存与生成时间会更高。": "2K has about four times as many pixels as 1K and requires more VRAM and generation time.",
  "图片步数": "Image Steps",
  "兼容模式": "Compatibility Mode",
  "使用普通 Checkpoint 工作流。": "Uses a standard Checkpoint workflow.",
  "画布缩放与定位": "Canvas Zoom and Position",
  "缩小画布": "Zoom Out",
  "放大画布": "Zoom In",
  "重置为百分之百": "Reset to 100%",
  "适配全部": "Fit All",
  "回到原点": "Reset View",
  "画布导航概览，拖动以定位视口": "Canvas overview; drag to reposition the viewport",
  "拖到兼容节点左侧圆点松开，或直接点击圆点 · 双击连线可断开": "Drop on a compatible input port, or click a port. Double-click a connection to disconnect.",
  "双击连线可删除": "Double-click a connection to remove it",
  "长视频时间线": "Long Video Timeline",
  "多分段顺序生成 · 持久化项目": "Sequential multi-segment generation · Persistent project",
  "项目名称": "Project Name",
  "未命名长视频": "Untitled Long Video",
  "保存项目": "Save Project",
  "删除项目": "Delete Project",
  "＋ 导入视频": "+ Import Video",
  "导入已有视频": "Import Existing Video",
  "智能分镜": "Smart Storyboard",
  "分析中…": "Analyzing…",
  "均分": "Split Evenly",
  "当前帧切分": "Split at Current Frame",
  "在当前帧切分": "Split at Current Frame",
  "顺序生成全部": "Generate All in Order",
  "按计划生成": "Generate Plan",
  "停止队列": "Stop Queue",
  "合并长视频": "Merge Long Video",
  "↓ 下载合并长视频": "↓ Download Merged Video",
  "成片序列监视器": "Final Sequence Monitor",
  "完整分镜序列监视器": "Full Storyboard Sequence Monitor",
  "源视频监视器": "Source Video Monitor",
  "成片分镜时间线": "Final Storyboard Timeline",
  "时间线操作": "Timeline Actions",
  "删除分段": "Delete Segment",
  "单独运行本段": "Run This Segment",
  "重新生成本段": "Regenerate Segment",
  "填写提示词和参数后生成这一段": "Enter a prompt and settings to generate this segment",
  "请填写本段提示词。": "Enter a prompt for this segment.",
  "续接方式": "Continuation",
  "不续接": "No Continuation",
  "上一段尾帧": "Previous Segment Tail Frame",
  "上一段视频": "Previous Segment Video",
  "Motion Context 潜空间续接": "Motion Context Latent Continuation",
  "潜空间动作与声音续接 · Motion Context": "Latent Motion & Audio Continuation · Motion Context",
  "直接复用上一段视频与声音 latent，生成后自动裁掉重复头部，再参与最终拼接。相邻分段必须保持相同尺寸。": "Reuses the previous segment's video and audio latent, trims the duplicated head after generation, then includes the result in the final concat. Adjacent generated segments must use the same dimensions.",
  "视频上下文帧": "Video Context Frames",
  "音频上下文帧": "Audio Context Frames",
  "22（推荐）": "22 (Recommended)",
  "24 帧为 1 秒；0 表示跟随视频窗口。": "24 frames equal one second; 0 follows the video context window.",
  "直接拼接": "Direct Append",
  "直接素材 · 不生成": "Direct Media · No Generation",
  "生成任务已完成。": "Generation completed.",
  "结果会出现在这里": "Output will appear here",
  "还没有分镜": "No storyboard yet",
  "请先添加分段": "Add a segment first",
  "选择一个视频开始分镜": "Choose a video to start storyboarding",
  "把本地视频拖到这里": "Drop a local video here",
  "选择本地视频": "Choose Local Video",
};

const DYNAMIC_COPY: ReadonlyArray<readonly [RegExp, (...groups: string[]) => string]> = [
  [/^换声请求失败 \((\d+)\)$/, (status) => `Voice request failed (${status})`],
  [/^模型未就绪：(.*)$/, (reason) => `Model unavailable: ${reason}`],
  [/^队列第 (\d+) 位$/, (position) => `Queue position ${position}`],
  [/^画布 (\d+)$/, (index) => `Canvas ${index}`],
  [/^(\d+) 字 · 输入 @ 引用本节点素材$/, (count) => `${count} characters · Type @ to reference this node's media`],
  [/^(\d+(?:\.\d+)?) 秒 · (\d+) 帧$/, (seconds, frames) => `${seconds} sec · ${frames} frames`],
  [/^已解析为 (.*)$/, (value) => `Resolved to ${translateUiText(value, "en")}`],
  [/^H3 最终提示词预览（只读） · (.*)$/, (state) => `Final H3 Prompt Preview (Read-only) · ${translateUiText(state, "en")}`],
  [/^无法恢复远程任务历史：(.*)$/, (message) => `Could not restore remote task history: ${message.replace(/^任务历史读取失败/, "Task history request failed")}`],
  [/^(.+) 节点$/, (name) => `${translateUiText(name, "en")} Node`],
  [/^连接到 (.+)$/, (name) => `Connect to ${translateUiText(name, "en")}`],
  [/^从 (.+) 开始连接$/, (name) => `Start a connection from ${translateUiText(name, "en")}`],
  [/^选择 (Picture|Video|Audio) (\d+)$/, (kind, index) => `Select ${kind} ${index}`],
  [/^已用 (\d+)\/(\d+)$/, (used, total) => `Used ${used}/${total}`],
  [/^(\d+) 个资产 · 显示 (\d+) 个(?: · 已收起 (\d+) 个重复项)?$/, (total, shown, hidden) => `${total} assets · ${shown} shown${hidden ? ` · ${hidden} duplicates hidden` : ""}`],
  [/^(\d+(?:\.\d+)?)fps 参考$/, (fps) => `${fps}fps reference`],
  [/^播放 (.+)$/, (name) => `Play ${name}`],
  [/^暂停 (.+)$/, (name) => `Pause ${name}`],
  [/^(.+) 视频播放器$/, (name) => `${name} video player`],
  [/^(.+) 音频播放器$/, (name) => `${name} audio player`],
  [/^配套 Turbo LoRA @ (.+)$/, (strength) => `Bundled Turbo LoRA @ ${strength}`],
  [/^(.+ · v\d+\.\d+) · 文生图$/, (profile) => `${profile} · Text to Image`],
  [/^(.+ · v\d+\.\d+) · (\d+\.\.\d+) 张图片参考$/, (profile, count) => `${profile} · ${count} image references`],
  [/^耗时 (\d+) 秒$/, (seconds) => `Elapsed ${seconds} sec`],
  [/^耗时 (\d+) 分$/, (minutes) => `Elapsed ${minutes} min`],
  [/^耗时 (\d+) 分 (\d+) 秒$/, (minutes, seconds) => `Elapsed ${minutes} min ${seconds} sec`],
  [/^耗时 (\d+) 小时$/, (hours) => `Elapsed ${hours} hr`],
  [/^耗时 (\d+) 小时 (\d+) 分$/, (hours, minutes) => `Elapsed ${hours} hr ${minutes} min`],
  [/^本机保留最近 (\d+) 条$/, (count) => `${count} recent tasks kept locally`],
  [/^(\d+) 字 · 输入 @ 引用本节点素材$/, (count) => `${count} characters · Type @ to reference this node's media`],
  [/^图(\d+)$/, (index) => `Image ${index}`],
  [/^图(\d+) · Image (\d+)$/, (index) => `Image ${index}`],
  [/^第 (\d+) 段$/, (index) => `Segment ${index}`],
  [/^(\d+(?:\.\d+)?) 秒$/, (seconds) => `${seconds} sec`],
  [/^(\d+) 帧$/, (frames) => `${frames} frames`],
  [/^删除所选 \((\d+)\)$/, (count) => `Delete Selected (${count})`],
  [/^选择重复项 \((\d+)\)$/, (count) => `Select Duplicates (${count})`],
  [/^已解析：(.*)$/, (value) => `Resolved: ${value}`],
  [/^当前总步数：(.*)$/, (value) => `Current total steps: ${value}`],
  [/^续跑后总步数：(.*)$/, (value) => `Steps after continuation: ${value}`],
  [/^续跑点有效至：(.*)$/, (value) => `Checkpoint valid until: ${value}`],
  [/^源视频 (.*)$/, (value) => `Source video ${value}`],
  [/^引用 (.*)$/, (value) => `References ${value}`],
  [/^已新建“(.*)”；原画布仍保留在上方标签中。$/, (name) => `Created “${name}”. The previous canvas remains in the tab bar.`],
  [/^已恢复 (\d+) 个画布；当前为“(.*)”。$/, (count, name) => `Restored ${count} canvas${count === "1" ? "" : "es"}; current: “${translateUiText(name, "en")}”.`],
  [/^已恢复 (\d+) 个画布；(\d+) 个损坏项已隔离并备份，当前为“(.*)”。$/, (count, issues, name) => `Restored ${count} canvas${count === "1" ? "" : "es"}; quarantined and backed up ${issues} damaged item${issues === "1" ? "" : "s"}; current: “${translateUiText(name, "en")}”.`],
  [/^已切换到“(.*)”。$/, (name) => `Switched to “${name}”.`],
  [/^已创建文件夹：(.*)$/, (name) => `Created folder: ${name}`],
  [/^已删除文件夹：(.*)$/, (name) => `Deleted folder: ${name}`],
  [/^已将资产改名为：(.*)$/, (name) => `Renamed asset to: ${name}`],
  [/^已移动资产：(.*)$/, (name) => `Moved asset: ${name}`],
  [/^已保存到资产：(.*)$/, (name) => `Saved to Assets: ${name}`],
  [/^已将生成结果保存到资产：(.*)$/, (name) => `Saved generated output to Assets: ${name}`],
  [/^已提交续跑：(\d+) \+ (\d+) 步$/, (current, added) => `Continuation submitted: ${current} + ${added} steps`],
  [/^续跑完成：当前共 (.*) 步$/, (steps) => `Continuation completed: ${steps} total steps`],
  [/^无法读取模型能力：(.*)$/, (message) => `Could not load model capabilities: ${message}`],
  [/^上传失败 \((\d+)\)$/, (status) => `Upload failed (${status})`],
  [/^提交失败 \((\d+)\)$/, (status) => `Submission failed (${status})`],
];

const LOCALIZED_ATTRIBUTES = ["aria-label", "placeholder", "title"] as const;
const IGNORED_SELECTOR = "[data-i18n-ignore], [translate='no'], pre, code, script, style, textarea";

export function normalizeUiLanguage(value: string | null | undefined): UiLanguage {
  return value === "zh-CN" ? "zh-CN" : "en";
}

export function translateUiText(value: string, language: UiLanguage): string {
  if (language === "zh-CN" || !/[\u3400-\u9fff]/.test(value)) return value;
  const leading = value.match(/^\s*/)?.[0] ?? "";
  const trailing = value.match(/\s*$/)?.[0] ?? "";
  const source = value.trim();
  const exact = ENGLISH_COPY[source];
  if (exact) return `${leading}${exact}${trailing}`;
  for (const [pattern, render] of DYNAMIC_COPY) {
    const match = source.match(pattern);
    if (match) return `${leading}${render(...match.slice(1))}${trailing}`;
  }
  return value;
}

function ignored(element: Element | null): boolean {
  if (element?.closest("[data-i18n-ui-copy]")) return false;
  return Boolean(element?.closest(IGNORED_SELECTOR));
}

export class UiLocalizer {
  private readonly root: HTMLElement;
  private language: UiLanguage = "en";
  private readonly textSources = new WeakMap<Text, string>();
  private readonly attributeSources = new WeakMap<Element, Map<string, string>>();
  private observer?: MutationObserver;

  constructor(root: HTMLElement) {
    this.root = root;
  }

  start(language: UiLanguage): void {
    this.language = language;
    this.localizeTree(this.root, false);
    this.observer = new MutationObserver((records) => this.handleMutations(records));
    this.observe();
  }

  setLanguage(language: UiLanguage): void {
    if (language === this.language) return;
    this.observer?.disconnect();
    this.language = language;
    this.localizeTree(this.root, false);
    this.observe();
  }

  stop(): void {
    this.observer?.disconnect();
  }

  private observe(): void {
    this.observer?.observe(this.root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...LOCALIZED_ATTRIBUTES],
    });
  }

  private handleMutations(records: MutationRecord[]): void {
    this.observer?.disconnect();
    for (const record of records) {
      if (record.type === "characterData" && record.target instanceof Text) {
        this.localizeText(record.target, true);
      } else if (record.type === "attributes" && record.target instanceof Element && record.attributeName) {
        this.localizeAttribute(record.target, record.attributeName, true);
      } else if (record.type === "childList") {
        record.addedNodes.forEach((node) => this.localizeTree(node, false));
      }
    }
    this.observe();
  }

  private localizeTree(node: Node, refreshSource: boolean): void {
    if (node instanceof Text) {
      this.localizeText(node, refreshSource);
      return;
    }
    if (!(node instanceof Element) || ignored(node)) return;
    for (const attribute of LOCALIZED_ATTRIBUTES) this.localizeAttribute(node, attribute, refreshSource);
    node.childNodes.forEach((child) => this.localizeTree(child, refreshSource));
  }

  private localizeText(node: Text, refreshSource: boolean): void {
    if (ignored(node.parentElement)) return;
    let source = this.textSources.get(node);
    if (source === undefined) {
      source = node.data;
      this.textSources.set(node, source);
    } else if (refreshSource && node.data !== translateUiText(source, this.language) && node.data !== source) {
      source = node.data;
      this.textSources.set(node, source);
    }
    const localized = translateUiText(source, this.language);
    if (node.data !== localized) node.data = localized;
  }

  private localizeAttribute(element: Element, attribute: string, refreshSource: boolean): void {
    if (ignored(element) || !element.hasAttribute(attribute)) return;
    let sources = this.attributeSources.get(element);
    if (!sources) {
      sources = new Map();
      this.attributeSources.set(element, sources);
    }
    const current = element.getAttribute(attribute) ?? "";
    let source = sources.get(attribute);
    if (source === undefined) {
      source = current;
      sources.set(attribute, source);
    } else if (refreshSource && current !== translateUiText(source, this.language) && current !== source) {
      source = current;
      sources.set(attribute, source);
    }
    const localized = translateUiText(source, this.language);
    if (current !== localized) element.setAttribute(attribute, localized);
  }
}
