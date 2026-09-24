export type DouyinFailure = {
  code?: string;
  message?: string;
  stage?: string;
  source: "local" | "server";
};

export type DouyinFeedback = {
  title: string;
  detail: string;
  nextStep: string;
  diagnostic: string;
};

const STAGES: Record<string, string> = {
  parsing: "解析链接",
  parse: "解析链接",
  downloading: "下载视频",
  download: "下载视频",
  importing: "导入资产",
  import: "导入视频",
  upload: "上传资产",
  status: "读取任务状态",
  retry: "重新提交任务",
  cancel: "取消任务",
};

export function explainDouyinFailure(failure: DouyinFailure): DouyinFeedback {
  const stage = STAGES[failure.stage || ""] || "处理请求";
  const diagnostic = `${failure.source === "local" ? "本机" : "开发机"} · ${stage} · ${failure.code || "unknown_error"}`;
  switch (failure.code) {
    case "rate_limited":
      return { title: "抖音暂时限制了请求", detail: "下载请求过于频繁或当前访问受到限制。", nextStep: "停止连续重试，过一段时间在浏览器确认视频可播放后再试一次。", diagnostic };
    case "bridge_rate_limited":
      return { title: "本机导入请求过于频繁", detail: "本机辅助服务暂时拒绝新的请求。", nextStep: "停止重复点击，稍后再提交一次。", diagnostic };
    case "access_restricted":
      return { title: "抖音拒绝了视频访问", detail: "请求收到 HTTP 403；这不能单独说明是账号、浏览器会话还是视频访问限制。", nextStep: "在这台电脑的 Chrome 中确认原视频可播放，稍后再试；持续失败时可上传有权使用的本地视频。", diagnostic };
    case "cookie_refresh_required":
      return {
        title: "抖音拒绝了视频请求",
        detail: "这次请求被拒绝，但不能据此断定 Cookie 已过期。",
        nextStep: failure.source === "local"
          ? "先停止连续重试，在这台电脑的 Chrome 中确认原视频能播放，稍后再试一次。仍失败时，可上传有权使用的本地视频。"
          : "先尝试本机导入；如果本机也失败，在 Chrome 中确认原视频能播放，稍后再试一次。",
        diagnostic,
      };
    case "invalid_link":
      return { title: "没有识别出有效抖音链接", detail: "分享文本中的链接无法解析。", nextStep: "请复制完整的抖音分享链接后重新粘贴。", diagnostic };
    case "timeout":
    case "download_timeout":
      return { title: "抖音请求超时", detail: `未能在限定时间内完成${stage}。`, nextStep: "检查网络和视频在浏览器中是否可播放，稍后再试。", diagnostic };
    case "download_queue_full":
      return { title: "下载队列已满", detail: "当前有太多导入任务。", nextStep: "等待现有任务完成后再提交。", diagnostic };
    case "download_too_large":
      return { title: "视频超过大小限制", detail: "Studio 无法导入这个大小的视频。", nextStep: "使用符合资产库限制的本地视频文件。", diagnostic };
    case "bridge_unavailable":
      return { title: "无法连接本机导入服务", detail: "Studio 没有连接到本机辅助服务。", nextStep: "确认本机辅助服务正在运行，然后重新打开抖音素材面板。", diagnostic };
    case "server_unavailable":
      return { title: "无法连接 Studio 服务器", detail: "任务状态暂时无法读取。", nextStep: "检查 Studio 连接；恢复后重新打开面板，已有下载任务会继续显示。", diagnostic };
    case "upload_failed":
      return { title: "视频已下载，但上传资产库失败", detail: failure.message || "Studio 未能保存下载的视频。", nextStep: "检查 Studio 服务和资产库空间，然后重新导入。", diagnostic };
    case "extractor_failed":
      return { title: "抖音视频提取失败", detail: "解析器未能取得可下载的视频。", nextStep: "确认链接在浏览器中可播放，稍后再试；持续失败时可上传有权使用的本地视频。", diagnostic };
    default:
      return { title: `${stage}失败`, detail: failure.message || "请求未能完成。", nextStep: "检查链接、网络和 Studio 服务状态后再试。", diagnostic };
  }
}
