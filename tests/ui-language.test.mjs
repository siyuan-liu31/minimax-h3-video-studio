import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  normalizeUiLanguage,
  translateUiText,
  UI_LANGUAGE_STORAGE_KEY,
} from "../app/ui-language.ts";

const studioPath = new URL("../app/studio.tsx", import.meta.url);
const layoutPath = new URL("../app/layout.tsx", import.meta.url);
const languagePath = new URL("../app/ui-language.ts", import.meta.url);
const stylesPath = new URL("../app/globals.css", import.meta.url);

test("English is the safe default and Chinese is the only persisted override", () => {
  assert.equal(normalizeUiLanguage(undefined), "en");
  assert.equal(normalizeUiLanguage(null), "en");
  assert.equal(normalizeUiLanguage("fr"), "en");
  assert.equal(normalizeUiLanguage("zh-CN"), "zh-CN");
  assert.equal(UI_LANGUAGE_STORAGE_KEY, "h3-studio-ui-language-v1");
});

test("known interface copy switches in both directions without rewriting user text", () => {
  assert.equal(translateUiText("生成视频", "en"), "Generate Video");
  assert.equal(translateUiText(" 生成视频 ", "en"), " Generate Video ");
  assert.equal(translateUiText("已用 4/6", "en"), "Used 4/6");
  assert.equal(translateUiText("12.96 秒 · 311 帧", "en"), "12.96 sec · 311 frames");
  assert.equal(translateUiText("画布 2", "en"), "Canvas 2");
  assert.equal(translateUiText("连接到 H3 Video", "en"), "Connect to H3 Video");
  assert.equal(translateUiText("73 个资产 · 显示 67 个 · 已收起 6 个重复项", "en"), "73 assets · 67 shown · 6 duplicates hidden");
  assert.equal(translateUiText("24fps 参考", "en"), "24fps reference");
  assert.equal(translateUiText("从远程素材 ID 恢复；若预览无效，请重新上传。", "en"), "Restored from the remote asset ID. Re-upload if the preview is unavailable.");
  assert.equal(translateUiText("播放 source-24fps-311f.mp4", "en"), "Play source-24fps-311f.mp4");
  assert.equal(translateUiText("暂停 source-24fps-311f.mp4", "en"), "Pause source-24fps-311f.mp4");
  assert.equal(translateUiText("source-audio.wav 音频播放器", "en"), "source-audio.wav audio player");
  assert.equal(translateUiText("排队中", "en"), "Queued");
  assert.equal(translateUiText("队列第 2 位", "en"), "Queue position 2");
  assert.equal(translateUiText("5.3 秒", "en"), "5.3 sec");
  assert.equal(translateUiText("换声请求失败 (502)", "en"), "Voice request failed (502)");
  assert.equal(translateUiText("连到生图", "en"), "Connect to Image Generation");
  assert.equal(translateUiText("耗时 9 分 36 秒", "en"), "Elapsed 9 min 36 sec");
  assert.equal(translateUiText("已恢复 1 个画布；当前为“画布 1”。", "en"), "Restored 1 canvas; current: “Canvas 1”.");
  assert.equal(translateUiText("生成视频", "zh-CN"), "生成视频");
  assert.equal(
    translateUiText("用户自己写的中文 Prompt，不应被自动翻译。", "en"),
    "用户自己写的中文 Prompt，不应被自动翻译。",
  );
});

test("the Studio exposes a persistent accessible language toggle and hides Chinese SSR copy until localization", async () => {
  const [studio, layout, language, styles] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(layoutPath, "utf8"),
    readFile(languagePath, "utf8"),
    readFile(stylesPath, "utf8"),
  ]);

  assert.match(layout, /<html lang="en">/);
  assert.match(studio, /useState<UiLanguage>\("en"\)/);
  assert.match(studio, /localStorage\.getItem\(UI_LANGUAGE_STORAGE_KEY\)/);
  assert.match(studio, /localStorage\.setItem\(UI_LANGUAGE_STORAGE_KEY, next\)/);
  assert.match(studio, /className="language-toggle"/);
  assert.match(studio, /Switch interface to Chinese/);
  assert.match(studio, /将界面切换为英文/);
  assert.match(studio, /data-i18n-ignore/);
  assert.match(studio, /data-i18n-ui-copy/);
  assert.match(styles, /\.studio-shell\[data-i18n-ready="false"\]\s*\{\s*visibility:\s*hidden/);
  assert.match(language, /MutationObserver/);
  assert.match(language, /pre, code, script, style, textarea/);
});
