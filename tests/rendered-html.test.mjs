import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the default-English MiniMax H3 Video Studio shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html lang="en">/i);
  assert.match(html, /<title>MiniMax H3 Video Studio · Agent-ready AI Video Workspace<\/title>/i);
  assert.match(html, /class="studio-shell" data-i18n-ready="false" data-ui-language="en"/i);
  assert.match(html, /MiniMax H3 Video Studio/);
  assert.match(html, /aria-controls="voice-studio-drawer"/);
  assert.doesNotMatch(html, /Your site is taking shape|react-loading-skeleton/);
});

test("ships English metadata and the persistent language-switch client entry", async () => {
  const response = await render();
  const html = await response.text();
  assert.match(html, /Agent-ready AI Video Workspace/);
  assert.match(html, /A visual MiniMax H3 and ComfyUI workspace/);
  assert.match(html, /class="language-toggle"/);
  assert.match(html, /Switch interface to Chinese/);
});
