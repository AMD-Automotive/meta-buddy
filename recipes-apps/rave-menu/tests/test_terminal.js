/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: BM
 * Maintainer: BM
 */

// Run with: node --test recipes-apps/rave-menu/tests/test_terminal.js
// Drive the real terminal code with controlled SSE events and animation frames.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../files/static/terminal.js"), "utf8");

function harness(fetchOverride) {
  class Element {
    constructor() {
      this.children = [];
      this.dataset = {};
      this.attributes = {};
      this.text = "";
      this.writes = 0;
      this.layouts = 0;
      const classes = new Set(["hidden"]);
      this.classList = {
        add: name => classes.add(name),
        remove: name => classes.delete(name),
        contains: name => classes.has(name),
      };
    }
    get textContent() { return this.text; }
    set textContent(text) { this.text = text; this.children = []; this.writes++; }
    get scrollHeight() { this.layouts++; return 100; }
    appendChild(child) { this.children.push(child); }
    setAttribute(name, value) { this.attributes[name] = value; }
  }
  const elements = Object.fromEntries(
    ["termModal", "terminal", "termTitle", "termStatus", "termTabs", "termClose"]
      .map(name => [name, new Element()])
  );
  const streams = [];
  class EventSource {
    constructor() { this.listeners = {}; this.closed = false; streams.push(this); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    close() { this.closed = true; }
  }
  const frames = new Map();
  let nextFrame = 0;
  const success = { ok: true, json: async () => ({ job_id: "test", cmd: ["install-app"] }) };
  const context = vm.createContext({
    document: {
      getElementById: id => elements[id],
      createElement: () => new Element(),
    },
    EventSource,
    fetch: fetchOverride || (async () => success),
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  vm.runInContext(source, context);
  return {
    elements, frames, streams, success,
    start: () => context.runAction("install-app", "Install", true, []),
    line: data => streams.at(-1).onmessage({ data }),
    status: data => streams.at(-1).listeners.status({ data }),
    stage(name) { this.line("=-=-=-="); this.line(name); this.line("=-=-=-="); },
    frame() {
      const callbacks = [...frames.values()];
      frames.clear();
      for (const callback of callbacks) callback();
    },
  };
}

test("warning floods do no per-line DOM work and render bounded recent history once", async () => {
  const h = harness();
  await h.start();
  h.stage("Init Partition");
  h.frame();
  const terminal = h.elements.terminal;
  const writes = terminal.writes;
  const layouts = terminal.layouts;
  // Also represents a background tab whose animation frames are suspended.
  for (let i = 0; i < 30000; i++) h.line(`tar: file-${i}: time stamp is in the future`);
  assert.equal(terminal.writes, writes);
  assert.equal(terminal.layouts, layouts);
  assert.equal(h.frames.size, 1);
  h.frame();
  assert.equal(terminal.writes - writes, 1);
  assert.equal(terminal.layouts - layouts, 1);
  assert.match(terminal.textContent, /29000 earlier lines omitted/);
  assert.match(terminal.textContent, /file-29999/);
  assert.doesNotMatch(terminal.textContent, /file-28999:/);
  assert.equal(terminal.textContent.trim().split("\n").length, 1001);
  assert.equal(h.elements.termStatus.textContent, "running: Init Partition");
});

test("line length, history characters, and number of stage tabs are bounded", async () => {
  const h = harness();
  await h.start();
  for (let i = 0; i < 40; i++) h.stage(`Stage ${i}`);
  for (let i = 0; i < 200; i++) h.line("x".repeat(10000));
  h.frame();
  assert.equal(h.elements.termTabs.children.length, 24);
  assert.match(h.elements.terminal.textContent, /line truncated/);
  assert.match(h.elements.terminal.textContent, /earlier lines omitted/);
  assert.ok(h.elements.terminal.textContent.length < 128 * 1024 + 100);
  assert.ok(h.elements.terminal.textContent.split("\n").every(line => line.length < 4200));
});

test("split stage markers and browsing previous tabs preserve the correct output", async () => {
  const h = harness();
  await h.start();
  h.line("first");
  h.line("=-=-=-=");
  h.frame();
  h.line("Init Partition");
  h.frame();
  h.line("=-=-=-=");
  h.line("extracting");
  h.frame();
  h.elements.termTabs.children[0].onclick();
  h.frame();
  assert.match(h.elements.terminal.textContent, /first/);
  h.line("extracted");
  h.frame();
  assert.doesNotMatch(h.elements.terminal.textContent, /extracted/);
  h.elements.termTabs.children[1].onclick();
  h.frame();
  assert.equal(h.elements.terminal.textContent, "extracting\nextracted\n");
});

test("completion preserves final output and incomplete markers without synchronous rendering", async () => {
  const h = harness();
  await h.start();
  h.stage("Done");
  h.line("install complete");
  h.line("=-=-=-=");
  h.line("incomplete marker");
  const writes = h.elements.terminal.writes;
  h.status("exited:0");
  assert.equal(h.elements.terminal.writes, writes);
  assert.equal(h.frames.size, 1);
  h.frame();
  assert.equal(h.elements.terminal.textContent, "install complete\n=-=-=-=\nincomplete marker\n");
  assert.equal(h.elements.termStatus.textContent, "exited:0");
  assert.ok(h.streams[0].closed);
  assert.ok(h.elements.termTabs.children.every(button => !button.dataset.live));
});

test("malformed stage markers remain ordinary log output", async () => {
  const h = harness();
  await h.start();
  h.line("=-=-=-="); h.line("not a stage"); h.line("ordinary line");
  h.frame();
  assert.equal(h.elements.termTabs.children.length, 1);
  assert.match(h.elements.terminal.textContent, /=-=-=-=\nnot a stage\nordinary line/);
});

test("closing cancels queued rendering and ignores late events", async () => {
  const h = harness();
  await h.start();
  h.line("queued");
  h.elements.termClose.onclick();
  h.line("late");
  h.status("exited:0");
  h.frame();
  assert.equal(h.frames.size, 0);
  assert.equal(h.elements.terminal.textContent, "");
  assert.ok(h.elements.termModal.classList.contains("hidden"));
  assert.ok(h.streams[0].closed);
});

test("closing during the start request never reopens the terminal", async () => {
  let respond;
  const h = harness(() => new Promise(resolve => { respond = resolve; }));
  const start = h.start();
  h.elements.termClose.onclick();
  respond(h.success);
  await start;
  h.streams[0].onopen();
  h.frame();
  assert.ok(h.streams[0].closed);
  assert.equal(h.frames.size, 0);
  assert.ok(h.elements.termModal.classList.contains("hidden"));
});

test("HTTP and network start failures update status and leave Close usable", async () => {
  for (const fetch of [
    async () => ({ ok: false, text: async () => "busy" }),
    async () => { throw new Error("offline"); },
  ]) {
    const h = harness(fetch);
    await h.start();
    h.frame();
    assert.equal(h.elements.termStatus.textContent, "error: could not start action");
    assert.match(h.elements.terminal.textContent, /ERROR starting action/);
    h.elements.termClose.onclick();
    assert.ok(h.elements.termModal.classList.contains("hidden"));
  }
});

test("stream disconnection preserves output and displays an error", async () => {
  const h = harness();
  await h.start();
  h.line("last line");
  h.streams[0].onerror();
  h.frame();
  assert.equal(h.elements.termStatus.textContent, "error:stream");
  assert.match(h.elements.terminal.textContent, /last line\nERROR: stream disconnected/);
  assert.ok(h.streams[0].closed);
});
