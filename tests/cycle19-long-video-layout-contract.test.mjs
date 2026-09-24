import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
const studio = await readFile(new URL("../app/studio.tsx", import.meta.url), "utf8");
const timeline = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");

function escaped(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function ruleBodies(source, selector) {
  return [...source.matchAll(new RegExp(`${escaped(selector)}\\s*\\{([^{}]*)\\}`, "g"))].map((match) => match[1]);
}

function declarations(source, selector) {
  return ruleBodies(source, selector).join("\n");
}

function firstDeclarations(source, selector) {
  const [body] = ruleBodies(source, selector);
  assert.ok(body !== undefined, `${selector} must have a base rule`);
  return body;
}

function balancedBlocks(source, header) {
  const blocks = [];
  const pattern = new RegExp(header.source, header.flags.includes("g") ? header.flags : `${header.flags}g`);
  for (const match of source.matchAll(pattern)) {
    const open = source.indexOf("{", match.index + match[0].length);
    assert.notEqual(open, -1, `missing block for ${match[0]}`);
    let depth = 0;
    for (let cursor = open; cursor < source.length; cursor += 1) {
      if (source[cursor] === "{") depth += 1;
      if (source[cursor] === "}") depth -= 1;
      if (depth === 0) {
        blocks.push(source.slice(open + 1, cursor));
        break;
      }
    }
  }
  return blocks;
}

function numericZ(source, selector) {
  const match = declarations(source, selector).match(/z-index\s*:\s*(-?\d+)/);
  assert.ok(match, `${selector} must expose a numeric z-index`);
  return Number(match[1]);
}

function minWidthBlocks(source) {
  const header = /@media\s*\(\s*min-width\s*:\s*(\d+)px\s*\)/gi;
  const widths = [...source.matchAll(header)].map((match) => Number(match[1]));
  const blocks = balancedBlocks(source, /@media\s*\(\s*min-width\s*:\s*\d+px\s*\)/i);
  assert.equal(blocks.length, widths.length, "every min-width query must have a balanced block");
  return blocks.map((body, index) => ({ width: widths[index], body }));
}

function maxWidthBlocks(source) {
  const header = /@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)/gi;
  const widths = [...source.matchAll(header)].map((match) => Number(match[1]));
  const blocks = balancedBlocks(source, /@media\s*\(\s*max-width\s*:\s*\d+px\s*\)/i);
  assert.equal(blocks.length, widths.length, "every max-width query must have a balanced block");
  return blocks.map((body, index) => ({ width: widths[index], body }));
}

function withoutMediaBlocks(source) {
  const ranges = [];
  const header = /@media\b[^{]*/g;
  for (const match of source.matchAll(header)) {
    const open = source.indexOf("{", match.index + match[0].length);
    if (open < 0) continue;
    let depth = 0;
    for (let cursor = open; cursor < source.length; cursor += 1) {
      if (source[cursor] === "{") depth += 1;
      if (source[cursor] === "}") depth -= 1;
      if (depth === 0) {
        ranges.push([match.index, cursor + 1]);
        break;
      }
    }
  }
  let result = source;
  for (const [start, end] of ranges.reverse()) result = `${result.slice(0, start)}${result.slice(end)}`;
  return result;
}

test("desktop long-video drawer fills the workspace area beside navigation", () => {
  const drawer = declarations(css, ".timeline-drawer");
  assert.match(drawer, /right\s*:\s*0\b/, "drawer must terminate at the workspace right edge");
  assert.match(
    drawer,
    /(?:width\s*:\s*(?:auto|calc\(100%\s*-\s*62px\))|inset-inline-end\s*:\s*0)/,
    "drawer must use the available workspace width instead of a capped desktop width",
  );
  assert.doesNotMatch(drawer, /width\s*:\s*min\(\s*900px/i, "the previous 900px cap leaves canvas exposed");
  assert.match(css, /\.workspace\s*\{[^}]*position\s*:\s*relative[^}]*isolation\s*:\s*isolate/);
});

test("desktop Director grid has a real content-bearing right column", () => {
  assert.match(firstDeclarations(css, ".director-workspace"), /display\s*:\s*grid/);
  const wideLayouts = minWidthBlocks(css).filter(({ width, body }) => width > 1024 && /\.director-workspace\s*\{[^}]*grid-template-columns/.test(body));
  assert.ok(wideLayouts.length, "a desktop-wide breakpoint must activate the two-column Director layout");
  const wide = wideLayouts[0].body;
  const workspace = declarations(wide, ".director-workspace");
  assert.match(
    workspace,
    /grid-template-columns\s*:\s*minmax\(\s*0\s*,[^)]+\)\s+minmax\(\s*420px\s*,\s*560px\s*\)/,
    "desktop Director workspace must define a fluid main column and a bounded editing column",
  );
  assert.match(workspace, /grid-template-rows\s*:\s*minmax\(\s*0\s*,\s*1fr\s*\)/, "desktop columns must be constrained to the real remaining workbench height");
  assert.match(declarations(wide, ".director-overview-column"), /grid-column\s*:\s*1\b/);
  assert.match(declarations(wide, ".director-workspace > .director-segment-inspector"), /grid-column\s*:\s*2\b/);
  assert.match(firstDeclarations(css, ".director-overview-column"), /display\s*:\s*contents/);
  assert.match(declarations(wide, ".director-overview-column"), /display\s*:\s*grid/);
  assert.match(
    timeline,
    /<div className="director-overview-column">[\s\S]*?<SequenceVideoMonitor\b[\s\S]*?sourceTime=\{sourceTime\}[\s\S]*?<StoryboardTimeline\b/,
    "the left column must keep the unified monitor/source tools and storyboard independent from inspector height",
  );
  assert.doesNotMatch(timeline, /<details className="director-source-tools"/, "source cutting is now part of the unified monitor");
  assert.match(
    timeline,
    /<div className="director-overview-column">[\s\S]*?project\.merged[\s\S]*?aria-label="选中分段执行计划"[\s\S]*?<\/div>\s*<section className="director-segment-inspector"/,
    "project-level merged output and execution plan must use the left sequence column",
  );
  assert.match(
    timeline,
    /<section className="director-segment-inspector"[^>]*>[\s\S]*?<SegmentCard\b/,
    "the right column must render the selected segment editor, not an empty decorative rail",
  );
});

test("desktop grid is shrink-safe and cannot create horizontal control occlusion", () => {
  assert.match(
    css,
    /\.director-(?:monitor|storyboard|segment-inspector)[^{}]*\{[^}]*min-width\s*:\s*0[^}]*overflow\s*:\s*hidden/,
  );
  assert.match(declarations(css, ".director-storyboard-scroll"), /overflow-x\s*:\s*auto/);
  const wide = minWidthBlocks(css).find(({ width, body }) => width > 1024 && /\.director-workspace\s*\{[^}]*grid-template-columns/.test(body));
  assert.ok(wide, "wide Director layout must exist");
  assert.match(declarations(wide.body, ".director-segment-inspector .timeline-controls"), /minmax\(\s*0\s*,\s*1fr\s*\)/);
  assert.match(declarations(wide.body, ".director-segment-inspector .timeline-reference-row"), /minmax\(\s*0\s*,\s*1fr\s*\)/);
  assert.match(firstDeclarations(css, ".timeline-project-shell"), /grid-template-rows\s*:\s*auto\s+minmax\(\s*0\s*,\s*1fr\s*\)\s+auto/);
  assert.match(firstDeclarations(css, ".timeline-actions"), /position\s*:\s*relative/);
  assert.match(declarations(wide.body, ".director-workspace > .director-segment-inspector"), /overflow-y\s*:\s*auto/);
  assert.match(declarations(wide.body, ".director-workspace > .director-segment-inspector"), /height\s*:\s*100%/);
  assert.match(declarations(wide.body, ".director-overview-column"), /overflow-y\s*:\s*auto/);
  assert.match(declarations(wide.body, ".director-workspace"), /overflow\s*:\s*hidden/);
  assert.match(declarations(wide.body, ".director-overview-column"), /min-height\s*:\s*0/);
  assert.match(declarations(wide.body, ".director-workspace > .director-segment-inspector > header"), /position\s*:\s*sticky/);
  assert.match(declarations(css, ".timeline-actions"), /flex-wrap\s*:\s*wrap/);
  assert.match(declarations(css, ".timeline-actions > .director-run-summary"), /flex\s*:\s*1\s+1\s+280px/);
});

test("at and below 1024px Director collapses to one content column", () => {
  const base = declarations(withoutMediaBlocks(css), ".director-workspace");
  assert.doesNotMatch(base, /grid-template-columns\s*:[^;]*\s+[^;]*/, "the base Director layout must remain one implicit column");
  assert.match(base, /grid-auto-rows\s*:\s*max-content/, "single-column cards must keep max-content rows so overflow-hidden panels are not clipped");
  assert.match(base, /align-content\s*:\s*start/, "single-column rows must keep their intrinsic height instead of being compressed and clipped");
  const twoColumnQueries = minWidthBlocks(css).filter(({ body }) => /\.director-workspace\s*\{[^}]*grid-template-columns/.test(body));
  assert.ok(twoColumnQueries.length, "a wider two-column enhancement is required");
  assert.ok(twoColumnQueries.every(({ width }) => width > 1024), "no two-column Director rule may activate at or below 1024px");
  const coveringMaxWidthQueries = maxWidthBlocks(css).filter(({ width }) => width >= 1024);
  assert.ok(
    coveringMaxWidthQueries.every(({ body }) => !/\.director-workspace\s*\{[^}]*grid-template-columns/.test(body)),
    "a max-width rule covering 1024px must not reactivate two columns",
  );
  assert.match(declarations(css, ".director-overview-column > .director-monitor"), /order\s*:\s*1/);
  assert.match(declarations(css, ".director-overview-column > .director-storyboard"), /order\s*:\s*2/);
  assert.match(declarations(css, ".director-workspace > .director-segment-inspector"), /order\s*:\s*3/);
  assert.match(declarations(css, ".director-overview-column > .director-source-tools"), /order\s*:\s*4/);
});

test("at and below 680px the drawer preserves the bottom-navigation safe area", () => {
  const responsive = balancedBlocks(css, /@media\s*\(\s*max-width\s*:\s*680px\s*\)/i);
  assert.ok(responsive.length, "mobile layout rules are required");
  const mobile = responsive.join("\n");
  assert.match(declarations(mobile, ".workspace"), /height\s*:\s*calc\(\s*100vh\s*-\s*58px\s*\)/);
  assert.match(declarations(mobile, ".rail-drawer"), /position\s*:\s*fixed/);
  assert.match(declarations(mobile, ".rail-drawer"), /bottom\s*:\s*58px/);
  assert.match(declarations(mobile, ".timeline-drawer"), /left\s*:\s*0/);
  assert.match(firstDeclarations(css, ".timeline-drawer"), /right\s*:\s*0/);
  assert.match(declarations(mobile, ".timeline-actions"), /flex-wrap\s*:\s*wrap/);
  assert.ok(numericZ(mobile, ".left-rail") > numericZ(mobile, ".rail-drawer"), "bottom navigation must remain above the drawer");
});

test("a visible full-workspace video drawer keeps the isolated canvas inert and underneath", () => {
  const canvasZ = numericZ(css, ".canvas-wrap");
  const drawerZ = numericZ(css, ".rail-drawer");
  assert.ok(drawerZ > canvasZ, "drawer must remain above the isolated canvas stacking layer");
  assert.match(declarations(css, ".canvas-wrap"), /isolation\s*:\s*isolate/);
  assert.match(
    studio,
    /<section[^>]*className="canvas-wrap"[^>]*aria-hidden=\{railPanel === "timeline" \|\| railPanel === "replication" \? true : undefined\}[^>]*inert=\{railPanel === "timeline" \|\| railPanel === "replication" \? true : undefined\}/,
  );
  assert.match(
    studio,
    /<div className="workspace">[\s\S]*?railPanel === "timeline"\s*&&\s*<VideoTimeline[\s\S]*?\/>}\s*<section[^>]*className="canvas-wrap"/,
  );
});
