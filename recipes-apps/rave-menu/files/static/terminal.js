/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC, BM
 * Maintainer: KC
 */

const TAB_MARKER = "=-=-=-=";
// Bound both rendering work and retained history, including very long lines
// and scripts that emit an unexpected number of stage markers.
const TERM_MAX_LINES = 1000;
const TERM_MAX_CHARS = 128 * 1024;
const TERM_MAX_LINE_CHARS = 4096;
const TERM_MAX_TABS = 24;

async function runAction(action, title, showTerminal, args) {
  const termModal = document.getElementById("termModal");
  const terminal = document.getElementById("terminal");
  const termTitle = document.getElementById("termTitle");
  const termStatus = document.getElementById("termStatus");
  const termTabs = document.getElementById("termTabs");

  let es = null;
  let frameId = null;
  let closed = false;
  let finished = false;
  let outputDirty = false;
  let tabsDirty = false;
  let status = "starting...";

  let tabCounter = 0;
  const tabs = [];
  let outputTabId = null;
  let viewedTabId = null;

  let markerState = 0;
  let markerName = "";
  let markerBuffer = [];

  function getTabById(tabId) {
    return tabs.find((t) => t.id === tabId) || null;
  }

  function renderTabButtons() {
    termTabs.textContent = "";
    tabs.forEach((tab) => {
      const btn = document.createElement("button");
      const isActive = tab.id === viewedTabId;
      const isLive = tab.id === outputTabId;
      btn.className = "term-tab" + (isActive ? " active" : "") + (isLive ? " live" : "");
      btn.type = "button";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
      btn.setAttribute("aria-label", isLive ? `${tab.name} (live output)` : tab.name);
      btn.textContent = tab.name;
      if (isLive) {
        btn.dataset.live = "1";
      }

      btn.onclick = () => {
        viewedTabId = tab.id;
        tabsDirty = true;
        outputDirty = true;
        scheduleRender();
      };
      termTabs.appendChild(btn);
    });
  }

  function renderViewedTab() {
    const tab = getTabById(viewedTabId);
    if (!tab || tab.lines.length === 0) {
      terminal.textContent = "";
      return;
    }
    const notice = tab.dropped ? `[${tab.dropped} earlier lines omitted]\n` : "";
    terminal.textContent = notice + tab.lines.join("\n") + "\n";
    terminal.scrollTop = terminal.scrollHeight;
  }

  function createTab(name) {
    tabCounter += 1;
    const tabName = (name || "").trim().slice(0, 120) || `Tab ${tabCounter}`;
    const tab = { id: tabCounter, name: tabName, lines: [], chars: 0, dropped: 0 };
    tabs.push(tab);
    if (tabs.length > TERM_MAX_TABS) tabs.shift();
    outputTabId = tab.id;
    viewedTabId = tab.id;
    if (!finished && status !== "starting...") status = `running: ${tabName}`;
    tabsDirty = true;
    outputDirty = true;
  }

  function appendOutputLine(line) {
    const outTab = getTabById(outputTabId);
    if (!outTab) return;
    outTab.lines.push(line);
    outTab.chars += line.length + 1;
    while (outTab.lines.length > TERM_MAX_LINES || outTab.chars > TERM_MAX_CHARS) {
      outTab.chars -= outTab.lines.shift().length + 1;
      outTab.dropped += 1;
    }

    if (viewedTabId === outputTabId) {
      outputDirty = true;
    }
  }

  function resetTabs() {
    tabs.length = 0;
    tabCounter = 0;
    outputTabId = null;
    viewedTabId = null;
    markerState = 0;
    markerName = "";
    markerBuffer = [];
    termTabs.textContent = "";
    createTab("Main");
  }

  function openTerm() {
    termTitle.textContent = title;
    terminal.textContent = "";
    termStatus.textContent = "starting...";
    resetTabs();
    termModal.classList.remove("hidden");
    scheduleRender();
  }

  function closeTerm() {
    closed = true;
    if (es) {
      es.close();
      es = null;
    }
    if (frameId !== null) cancelAnimationFrame(frameId);
    frameId = null;
    tabs.length = 0;
    markerBuffer = [];
    terminal.textContent = "";
    termTabs.textContent = "";
    termModal.classList.add("hidden");
  }

  function flushMalformedMarker(asLine) {
    markerBuffer.forEach((ln) => appendOutputLine(ln));
    markerBuffer = [];
    markerState = 0;
    markerName = "";
    if (asLine !== null) {
      appendOutputLine(asLine);
    }
  }

  function processIncomingLine(line) {
    if (markerState === 0) {
      if (line.trim() === TAB_MARKER) {
        markerState = 1;
        markerBuffer = [line];
        return;
      }
      appendOutputLine(line);
      return;
    }

    if (markerState === 1) {
      markerName = line;
      markerBuffer.push(line);
      markerState = 2;
      return;
    }

    if (markerState === 2) {
      if (line.trim() === TAB_MARKER) {
        createTab(markerName);
        markerState = 0;
        markerName = "";
        markerBuffer = [];
        return;
      }

      markerBuffer.push(line);
      flushMalformedMarker(null);
    }
  }

  function flushTerminal() {
    frameId = null;
    if (closed) return;
    if (tabsDirty) renderTabButtons();
    if (outputDirty) renderViewedTab();
    if (termStatus.textContent !== status) termStatus.textContent = status;
    tabsDirty = false;
    outputDirty = false;
  }

  function scheduleRender() {
    if (!closed && frameId === null) {
      frameId = requestAnimationFrame(flushTerminal);
    }
  }

  function queueLine(line) {
    if (closed) return;
    // Parse stage markers as events arrive, but do no DOM work here. Keeping
    // only bounded tab histories avoids an unbounded raw-event backlog (also
    // when requestAnimationFrame is paused in a background browser tab).
    if (line.length > TERM_MAX_LINE_CHARS) {
      line = line.slice(0, TERM_MAX_LINE_CHARS) + " [line truncated]";
    }
    processIncomingLine(line);
    scheduleRender();
  }

  function finishStream(result) {
    finished = true;
    status = result;
    if (!closed && showTerminal && markerState !== 0) flushMalformedMarker(null);
    outputTabId = null;
    tabsDirty = true;
    if (es) es.close();
    es = null;
    scheduleRender();
  }

  if (showTerminal) openTerm();
  document.getElementById("termClose").onclick = closeTerm;

  try {
    const resp = await fetch(`/api/run/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args: args || [] }),
    });

    if (!resp.ok) {
      const t = await resp.text();
      if (showTerminal) queueLine(`ERROR starting action: ${t}`);
      finishStream("error: could not start action");
      return;
    }

    const { job_id, cmd } = await resp.json();
    status = "running";
    if (showTerminal) {
      queueLine(`$ ${cmd.join(" ")}`);
      queueLine("");
    }

    es = new EventSource(`/api/stream/${job_id}`);
    // Even if dismissed while the POST was pending, let the stream connect so
    // the backend starts its reader and continues draining the running job.
    es.onopen = () => {
      if (closed && es) {
        es.close();
        es = null;
      }
    };

    es.onmessage = (ev) => {
      if (showTerminal && !finished) queueLine(ev.data);
    };

    es.addEventListener("status", (ev) => {
      if (ev.data.startsWith("exited:") || ev.data.startsWith("error:")) {
        finishStream(ev.data);
      } else {
        status = ev.data;
        scheduleRender();
      }
    });

    es.onerror = () => {
      if (showTerminal) queueLine("ERROR: stream disconnected");
      finishStream("error:stream");
    };
  } catch (err) {
    if (showTerminal) {
      queueLine(`ERROR starting action: ${err.message}`);
    }
    finishStream("error: could not start action");
  }
}
