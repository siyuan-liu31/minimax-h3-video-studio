import assert from "node:assert/strict";
import test from "node:test";

import { imageDimensions } from "../app/studio-config.ts";
import { deriveLibraryMedia, estimateH3ReferenceCanvas, mediaDisplayOrientation, remoteAssetToLibraryItem, remoteDerivationToResult } from "../app/studio-library.ts";

test("image quality and aspect ratio map to the exact requested dimensions", () => {
  const expected = {
    "1K/16:9": [1024, 576],
    "1K/9:16": [576, 1024],
    "1K/3:4": [768, 1024],
    "1K/1:1": [1024, 1024],
    "2K/16:9": [2048, 1152],
    "2K/9:16": [1152, 2048],
    "2K/3:4": [1536, 2048],
    "2K/1:1": [2048, 2048],
  };
  for (const [key, dimensions] of Object.entries(expected)) {
    const [quality, aspect] = key.split("/");
    assert.deepEqual(imageDimensions(quality, aspect), { width: dimensions[0], height: dimensions[1] });
  }
});

test("Qwen-Image 2.1 uses native 2K sizes without changing legacy profile dimensions", () => {
  assert.deepEqual(imageDimensions("2K", "16:9", true), { width: 2752, height: 1536 });
  assert.deepEqual(imageDimensions("2K", "9:16", true), { width: 1536, height: 2752 });
  assert.deepEqual(imageDimensions("2K", "3:4", true), { width: 1792, height: 2400 });
  assert.deepEqual(imageDimensions("2K", "1:1", true), { width: 2048, height: 2048 });
  assert.deepEqual(imageDimensions("2K", "16:9"), { width: 2048, height: 1152 });
  assert.deepEqual(imageDimensions("1K", "16:9", true), { width: 1024, height: 576 });
});

test("remote asset receipts become same-origin reusable library items", () => {
  const id = "a".repeat(32);
  assert.deepEqual(remoteAssetToLibraryItem({
    id,
    kind: "video",
    filename: "reference.mp4",
    content_url: "https://untrusted.example/leak",
    content_hash: "f".repeat(64),
    size: 1234,
    created_at: 1_700_000_000,
    media: { duration: 5.25, has_audio: true, reference_fps: 24 },
  }), {
    id,
    kind: "video",
    filename: "reference.mp4",
    contentUrl: `/api/assets/${id}/content`,
    thumbnailUrl: `/api/assets/${id}/thumbnail`,
    contentHash: "f".repeat(64),
    size: 1234,
    pinned: false,
    createdAt: "2023-11-14T22:13:20.000Z",
    media: { duration: 5.25, has_audio: true, reference_fps: 24 },
  });
});

test("asset library exposes content-level duplicate cleanup and batch deletion", async () => {
  const source = await import("node:fs/promises").then(({ readFile }) => readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"));
  const start = source.indexOf("function AssetLibrary(");
  const end = source.indexOf("function ResultThumbnail(", start);
  const assetLibrary = source.slice(start, end);
  assert.match(assetLibrary, /seenHashes/);
  assert.match(assetLibrary, /item\.contentHash/);
  assert.match(assetLibrary, /选择重复项/);
  assert.match(assetLibrary, /Promise\.allSettled/);
  assert.match(assetLibrary, /删除所选/);
});

test("asset library rejects malformed ids and unsupported media kinds", () => {
  assert.equal(remoteAssetToLibraryItem({ id: "../escape", kind: "image", filename: "x.png" }), undefined);
  assert.equal(remoteAssetToLibraryItem({ id: "b".repeat(32), kind: "document", filename: "x.pdf" }), undefined);
});

test("derivation receipts become durable result entries without becoming assets", () => {
  const id = "d".repeat(32);
  const assetId = "e".repeat(32);
  assert.deepEqual(remoteDerivationToResult({
    id,
    kind: "image",
    display_name: "last-frame.jpg",
    preview_url: `/api/derivations/${id}/content`,
    download_url: `/api/derivations/${id}/download`,
    thumbnail_url: `/api/derivations/${id}/thumbnail`,
    created_at: 1_700_000_000,
    asset_id: assetId,
    stored_name: "must-not-leak.jpg",
    media: { width: 1920, height: 1080 },
  }), {
    id,
    kind: "image",
    displayName: "last-frame.jpg",
    contentUrl: `/api/derivations/${id}/content`,
    thumbnailUrl: `/api/derivations/${id}/thumbnail`,
    downloadUrl: `/api/derivations/${id}/download`,
    createdAt: "2023-11-14T22:13:20.000Z",
    assetId,
    pinned: false,
    media: { width: 1920, height: 1080 },
  });
  assert.equal(remoteDerivationToResult({ id: "../escape", kind: "video" }), undefined);
});

test("asset and derivation receipts preserve durable pin state", () => {
  const assetId = "1".repeat(32);
  const derivationId = "2".repeat(32);
  assert.equal(remoteAssetToLibraryItem({ id: assetId, kind: "image", filename: "pin.png", pinned: true })?.pinned, true);
  assert.equal(remoteDerivationToResult({ id: derivationId, kind: "video", display_name: "pin.mp4", pinned: true })?.pinned, true);
});

test("normalized video receipts preserve source fps for Director frame contracts", () => {
  const id = "c".repeat(32);
  const asset = remoteAssetToLibraryItem({
    id, kind: "video", filename: "30fps.mp4",
    media: { duration: 10, fps: 24, source_fps: 30, reference_fps: 24, frame_count: 300, normalized_to_24fps: true },
  });
  assert.deepEqual(asset?.media, { duration: 10, fps: 24, source_fps: 30, reference_fps: 24, frame_count: 300, normalized_to_24fps: true });
});

test("H3 reference preview preserves orientation, aspect family, rotation, and 32-pixel alignment", () => {
  assert.deepEqual(estimateH3ReferenceCanvas(1080, 1920), { width: 480, height: 864 });
  assert.deepEqual(estimateH3ReferenceCanvas(1920, 1080), { width: 864, height: 480 });
  assert.deepEqual(estimateH3ReferenceCanvas(1440, 1080), { width: 640, height: 480 });
  assert.deepEqual(estimateH3ReferenceCanvas(1080, 1440), { width: 480, height: 640 });
  assert.deepEqual(estimateH3ReferenceCanvas(1920, 1080, 90), { width: 480, height: 864 });
  assert.deepEqual(estimateH3ReferenceCanvas(320, 180), { width: 320, height: 192 });
  assert.equal(estimateH3ReferenceCanvas(0, 1080), undefined);
});

test("media display orientation honors dimensions and container rotation", () => {
  assert.equal(mediaDisplayOrientation({ width: 540, height: 1004 }), "portrait");
  assert.equal(mediaDisplayOrientation({ width: 1920, height: 1080 }), "landscape");
  assert.equal(mediaDisplayOrientation({ width: 1920, height: 1080, rotation: 90 }), "portrait");
  assert.equal(mediaDisplayOrientation({ width: 1080, height: 1920, rotation: -90 }), "landscape");
  assert.equal(mediaDisplayOrientation({ width: 1080, height: 1080 }), "square");
  assert.equal(mediaDisplayOrientation({ width: 0, height: 1080 }), "unknown");
  assert.equal(mediaDisplayOrientation({ width: 1080 }), "unknown");
});

test("prepared reference receipts retain the auditable preprocessing contract", () => {
  const id = "9".repeat(32);
  const preprocessing = {
    algorithm_version: "h3-reference-low-token/v1",
    source: { width: 1920, height: 1080 },
    output: { canvas_width: 864, canvas_height: 480, truncated: true },
  };
  assert.deepEqual(remoteDerivationToResult({
    id, kind: "video", display_name: "prepared.mp4",
    preview_url: `/api/derivations/${id}/content`, preprocessing,
  })?.preprocessing, preprocessing);
});

test("H3 reference preprocessing polls durable progress and returns the completed receipt", async () => {
  const originalFetch = globalThis.fetch;
  const taskId = "8".repeat(32);
  const receiptId = "9".repeat(32);
  const progress = [];
  globalThis.fetch = async (input, init = {}) => {
    const path = String(input);
    if (path === "/api/media/derive" && init.method === "POST") {
      assert.equal(JSON.parse(String(init.body)).background, true);
      return Response.json({ task_id: taskId, status: "queued", progress: 0 }, { status: 202 });
    }
    if (path === `/api/media-tasks/${taskId}`) {
      return Response.json({
        task_id: taskId, status: "completed", progress: 100,
        receipt: { id: receiptId, kind: "video", display_name: "prepared.mp4", preview_url: `/api/derivations/${receiptId}/content`, media: { width: 480, height: 864 } },
      });
    }
    throw new Error(`unexpected fetch ${path}`);
  };
  try {
    const result = await deriveLibraryMedia(
      { type: "asset", asset_id: "7".repeat(32) },
      { operation: "prepare_h3_reference", preset: "h3-low-token", audio: "remove" },
      { onProgress: (value, status) => progress.push([value, status]), pollIntervalMs: 1 },
    );
    assert.equal(result.id, receiptId);
    assert.deepEqual(progress, [[100, "completed"]]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("aborting H3 reference preprocessing requests remote cancellation", async () => {
  const originalFetch = globalThis.fetch;
  const taskId = "8".repeat(32);
  let canceled = 0;
  globalThis.fetch = async (input, init = {}) => {
    const path = String(input);
    if (path === "/api/media/derive") return Response.json({ task_id: taskId, status: "queued" }, { status: 202 });
    if (path === `/api/media-tasks/${taskId}/cancel` && init.method === "POST") {
      canceled += 1;
      return Response.json({ task_id: taskId, status: "cancelling" }, { status: 202 });
    }
    throw new Error(`unexpected fetch ${path}`);
  };
  const controller = new AbortController();
  controller.abort();
  try {
    await assert.rejects(
      deriveLibraryMedia(
        { type: "asset", asset_id: "7".repeat(32) },
        { operation: "prepare_h3_reference", preset: "h3-low-token", audio: "remove" },
        { signal: controller.signal },
      ),
      /取消/,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(canceled, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
