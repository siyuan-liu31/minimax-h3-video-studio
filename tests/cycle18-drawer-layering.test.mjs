import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
const studio = await readFile(new URL("../app/studio.tsx", import.meta.url), "utf8");
const timeline = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");

function zIndexFor(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{[^}]*z-index:\\s*(\\d+)`));
  assert.ok(match, `${selector} must declare an explicit z-index`);
  return Number(match[1]);
}

function matchingDivEnd(source, start) {
  const tags = /<div\b[^>]*>|<\/div>/g;
  tags.lastIndex = start;
  let depth = 0;
  for (const match of source.matchAll(tags)) {
    if (match[0].startsWith("</")) depth -= 1;
    else depth += 1;
    if (depth === 0) return match.index + match[0].length;
  }
  return -1;
}

test("drawers and canvas chrome live in isolated sibling stacking layers", () => {
  const drawerZ = zIndexFor(".rail-drawer");
  const canvasZ = zIndexFor(".canvas-wrap");
  const noticeZ = zIndexFor(".canvas-notice");
  const toolbarDeclarations = [...css.matchAll(/\.canvas-toolbar\s*\{[^}]*z-index:\s*(\d+)/g)];
  assert.ok(toolbarDeclarations.length, "canvas toolbar must declare its stacking layer");
  const toolbarZ = Math.max(...toolbarDeclarations.map((match) => Number(match[1])));
  const overview = studio.match(/aria-label="画布导航概览，拖动以定位视口"[\s\S]{0,500}?zIndex:\s*(\d+)/);
  assert.ok(overview, "canvas overview must keep an explicit stacking layer");
  const overviewZ = Number(overview[1]);

  assert.match(css, /\.workspace\s*\{[^}]*position:\s*relative;[^}]*isolation:\s*isolate;/);
  assert.match(css, /\.canvas-wrap\s*\{[^}]*position:\s*relative;[^}]*isolation:\s*isolate;/);
  assert.ok(drawerZ > canvasZ, `drawer z-index ${drawerZ} must exceed the isolated canvas layer ${canvasZ}`);
  assert.ok(noticeZ > canvasZ, "canvas notice should remain ordered within the isolated canvas layer");
  assert.ok(toolbarZ > canvasZ, "canvas toolbar should remain ordered within the isolated canvas layer");
  assert.ok(overviewZ > canvasZ, "canvas overview should remain ordered within the isolated canvas layer");
  assert.match(
    studio,
    /<div className="workspace">[\s\S]*?railPanel === "timeline"\s*&&\s*<VideoTimeline[\s\S]*?\/>}\s*<section[^>]*className="canvas-wrap"/,
    "the timeline drawer and isolated canvas must remain sibling children of workspace",
  );
});

test("long-video actions occupy a dedicated non-overlapping drawer row", () => {
  assert.match(css, /\.timeline-drawer\s*\{[^}]*overflow:\s*hidden;[^}]*display:\s*grid;[^}]*grid-template-rows:\s*auto auto minmax\(0,\s*1fr\)/);
  assert.match(css, /\.timeline-project-shell\s*\{[^}]*display:\s*grid;[^}]*grid-template-rows:\s*auto minmax\(0,\s*1fr\) auto/);
  assert.match(css, /\.director-workspace\s*\{[^}]*overflow-y:\s*auto/);
  assert.match(css, /\.timeline-actions\s*\{[^}]*position:\s*relative;[^}]*bottom:\s*auto;[^}]*flex-wrap:\s*wrap;[^}]*background:\s*#[0-9a-f]{8}/i);
  const shellStart = timeline.indexOf('<div className="timeline-project-shell">');
  const shellEnd = matchingDivEnd(timeline, shellStart);
  const metaStart = timeline.indexOf('<div className="timeline-project-meta">', shellStart);
  const workspaceStart = timeline.indexOf('<div className="director-workspace">', shellStart);
  const actionsStart = timeline.indexOf('<footer className="timeline-actions">', shellStart);
  assert.ok(shellStart >= 0 && shellEnd > shellStart, "timeline project shell must be structurally balanced");
  assert.ok(
    shellStart < metaStart && metaStart < workspaceStart && workspaceStart < actionsStart && actionsStart < shellEnd,
    "project metadata, the scrollable workspace, and actions must remain ordered inside the same shell",
  );
  assert.match(css, /@media\s*\(max-width:\s*680px\)[\s\S]*?\.rail-drawer\s*\{[^}]*position:\s*fixed;[^}]*bottom:\s*58px;/);
  assert.match(css, /@media\s*\(max-width:\s*680px\)[\s\S]*?\.timeline-actions\s*\{[^}]*flex-wrap:\s*wrap;/);
  assert.match(studio, /railPanel === "timeline"\s*&&\s*<VideoTimeline/);
});

test("an open full-workspace drawer disables hidden canvas interactions and restores navigation focus", () => {
  assert.match(studio, /aria-hidden=\{railPanel === "timeline" \|\| railPanel === "replication" \? true : undefined\}\s+inert=\{railPanel === "timeline" \|\| railPanel === "replication" \? true : undefined\}/);
  assert.match(studio, /if \(!railPanel\)[\s\S]{0,300}previous === "timeline"[\s\S]{0,200}timelineRailButtonRef\.current\?\.focus\(\)/);
  assert.match(studio, /if \(!railPanel\)[\s\S]{0,700}closeContextMenu\(false\);[\s\S]{0,200}setConnecting\(undefined\);[\s\S]{0,150}setConnectionPointer\(undefined\)/);
  assert.match(studio, /ref=\{timelineRailButtonRef\}[\s\S]{0,250}aria-controls="video-timeline-drawer"/);
});

test("mobile keeps the drawer above canvas chrome and below the bottom navigation", () => {
  const mobile = css.match(/@media\s*\(max-width:\s*680px\)\s*\{[\s\S]*?\.rail-drawer\s*\{[^}]*position:\s*fixed;[^}]*z-index:\s*(\d+);[^}]*bottom:\s*58px;[\s\S]*?\n\}/);
  assert.ok(mobile, "mobile drawer must expose an explicit safe-area stacking layer");
  const mobileDrawerZ = Number(mobile[1]);
  const mobileRail = css.match(/@media\s*\(max-width:\s*680px\)\s*\{[\s\S]*?\.left-rail\s*\{[^}]*position:\s*fixed;[^}]*z-index:\s*(\d+);/);
  assert.ok(mobileRail, "mobile bottom navigation must expose an explicit stacking layer");
  assert.ok(Number(mobileRail[1]) > mobileDrawerZ, "mobile navigation must stay above the drawer");
  assert.ok(mobileDrawerZ > zIndexFor(".canvas-wrap"), "mobile drawer must stay above the isolated canvas layer");
});
