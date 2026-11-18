/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

(function () {
  const grid = document.getElementById("boardTestGrid");
  const output = document.getElementById("boardTestOutput");
  const outputHead = document.getElementById("boardTestOutputHead");
  const alertBox = document.getElementById("boardTestAlert");
  const backBtn = document.getElementById("boardTestBack");
  const runAllBtn = document.getElementById("boardTestRunAll");

  let selectedTestId = "";
  let pollTimer = null;
  let iperfServerIp = "192.168.0.10";
  let iperfMinBw = 80;
  let iperf10gServerIp = "192.168.0.10";
  let iperf10gMinBw = 8000;

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isValidIpv4(addr) {
    const parts = String(addr || "").trim().split(".");
    if (parts.length !== 4) return false;
    for (const p of parts) {
      if (!/^\d+$/.test(p)) return false;
      const n = Number(p);
      if (!Number.isInteger(n) || n < 0 || n > 255) return false;
    }
    return true;
  }

  function getIperfPayload() {
    const ip = String(iperfServerIp || "").trim();
    const bw = Number(iperfMinBw);

    if (!isValidIpv4(ip)) {
      throw new Error("iperf Server IP must be a valid IPv4 address");
    }
    if (!Number.isFinite(bw) || bw <= 0) {
      throw new Error("iperf required speed must be greater than zero");
    }

    return { server_ip: ip, min_bw: Math.round(bw) };
  }

  function getIperf10gPayload() {
    const ip = String(iperf10gServerIp || "").trim();
    const bw = Number(iperf10gMinBw);

    if (!isValidIpv4(ip)) {
      throw new Error("iperf10g Server IP must be a valid IPv4 address");
    }
    if (!Number.isFinite(bw) || bw <= 0) {
      throw new Error("iperf10g required speed must be greater than zero");
    }

    return { server_ip: ip, min_bw: Math.round(bw) };
  }

  async function fetchJson(url, options) {
    const resp = await fetch(url, options);
    let payload = null;
    try {
      payload = await resp.json();
    } catch (_e) {
      payload = null;
    }
    if (!resp.ok) {
      throw new Error((payload && (payload.error || payload.message)) || ("HTTP " + resp.status));
    }
    return payload;
  }

  function statusLabel(test) {
    if (test.result) return test.result;
    switch (test.status) {
      case "running":
        return "Running";
      case "pending":
        return "Pending";
      case "blocked":
        return "Blocked";
      default:
        return "Pending";
    }
  }


  function formatTestTitle(rawTitle) {
    const base = String(rawTitle || "").replace(/-/g, " ").trim();
    if (!base) return "";
    return base.charAt(0).toUpperCase() + base.slice(1);
  }

  async function loadLog(testId, title) {
    selectedTestId = testId;
    outputHead.textContent = "Output: " + title;
    output.textContent = "Loading...";
    try {
      const data = await fetchJson("/api/board-test/log/" + encodeURIComponent(testId));
      output.textContent = data.output || "(no output captured yet)";
    } catch (err) {
      output.textContent = "Failed to load output: " + err.message;
    }
  }

  function applySnapshotSettings(snapshot) {
    const iperf = snapshot && snapshot.settings && snapshot.settings.iperf;
    if (iperf) {
      if (typeof iperf.server_ip === "string" && iperf.server_ip.trim()) {
        iperfServerIp = iperf.server_ip.trim();
      }
      if (typeof iperf.min_bw !== "undefined" && iperf.min_bw !== null && iperf.min_bw !== "") {
        const bw = Number(iperf.min_bw);
        if (Number.isFinite(bw) && bw > 0) {
          iperfMinBw = Math.round(bw);
        }
      }
    }
    const iperf10g = snapshot && snapshot.settings && snapshot.settings.iperf10g;
    if (iperf10g) {
      if (typeof iperf10g.server_ip === "string" && iperf10g.server_ip.trim()) {
        iperf10gServerIp = iperf10g.server_ip.trim();
      }
      if (typeof iperf10g.min_bw !== "undefined" && iperf10g.min_bw !== null && iperf10g.min_bw !== "") {
        const bw = Number(iperf10g.min_bw);
        if (Number.isFinite(bw) && bw > 0) {
          iperf10gMinBw = Math.round(bw);
        }
      }
    }
  }

  function render(snapshot) {
    if (!grid) return;

    applySnapshotSettings(snapshot);

    if (alertBox) {
      const pcie = snapshot && snapshot.pcie ? snapshot.pcie : null;
      if (pcie && pcie.ok === false && pcie.message) {
        alertBox.innerHTML = '<span>' + escapeHtml(pcie.message) + '</span>' +
          '<a href="/?autorun=board-test" class="board-test-alert-btn">Program board test</a>';
        alertBox.classList.remove("hidden");
      } else {
        alertBox.classList.add("hidden");
        alertBox.textContent = "";
      }
    }

    const tests = (snapshot && snapshot.tests) || [];
    grid.innerHTML = tests
      .map((t) => {
        const canRerun = !!t.has_run && !snapshot.running;
        const canRunSingle = !t.has_run && !snapshot.running && t.status === "pending";
        const cssStatus = ["pass", "fail", "running", "unknown", "blocked"].includes(t.status)
          ? t.status
          : "pending";
        const isIperf = t.id === "iperf-test";
        const isIperf10g = t.id === "iperf10g-test";
        var iperfFields = "";
        if (isIperf) {
          iperfFields =
            '<div class="board-test-iperf">' +
              '<label class="board-test-iperf-label">Server IP' +
                '<input class="board-test-iperf-ip" data-iperf-ip="' + escapeHtml(t.id) + '" type="text" value="' + escapeHtml(iperfServerIp) + '">' +
              '</label>' +
              '<label class="board-test-iperf-label">Required speed (Mbps)' +
                '<input class="board-test-iperf-bw" data-iperf-bw="' + escapeHtml(t.id) + '" type="number" min="1" step="1" value="' + escapeHtml(String(iperfMinBw)) + '">' +
              '</label>' +
            '</div>';
        } else if (isIperf10g) {
          iperfFields =
            '<div class="board-test-iperf">' +
              '<label class="board-test-iperf-label">Server IP' +
                '<input class="board-test-iperf10g-ip" data-iperf10g-ip="' + escapeHtml(t.id) + '" type="text" value="' + escapeHtml(iperf10gServerIp) + '">' +
              '</label>' +
              '<label class="board-test-iperf-label">Required speed (Mbps)' +
                '<input class="board-test-iperf10g-bw" data-iperf10g-bw="' + escapeHtml(t.id) + '" type="number" min="1" step="1" value="' + escapeHtml(String(iperf10gMinBw)) + '">' +
              '</label>' +
            '</div>';
        }

        return (
          '<div class="board-test-card ' + cssStatus + '" data-test-id="' + escapeHtml(t.id) + '">' +
            '<div class="board-test-head">' +
              '<div class=\"board-test-title\">' + escapeHtml(formatTestTitle(t.title || t.id)) + '</div>' +
              '<div class="board-test-status ' + cssStatus + '">' + escapeHtml(statusLabel(t)) + '</div>' +
            '</div>' +
            iperfFields +
            '<button class="board-test-run-single" data-run-single="' + escapeHtml(t.id) + '" title="Run" ' +
              (canRunSingle ? "" : "hidden") + '><svg viewBox="0 0 24 24" width="14" height="14"><path d="M8 5v14l11-7z" fill="currentColor"/></svg></button>' +
            '<button class="board-test-rerun" data-rerun="' + escapeHtml(t.id) + '" title="Re-run" ' +
              (canRerun ? "" : "hidden") + '><svg viewBox="0 0 24 24" width="14" height="14"><path d="M17.65 6.35A7.96 7.96 0 0012 4a8 8 0 108 8h-2a6 6 0 11-1.76-4.24L14 10h7V3l-3.35 3.35z" fill="currentColor"/></svg></button>' +
          '</div>'
        );
      })
      .join("");

    grid.querySelectorAll(".board-test-card").forEach((card) => {
      card.addEventListener("click", () => {
        const testId = card.getAttribute("data-test-id") || "";
        const t = tests.find((x) => x.id === testId);
        if (!t || !t.has_run) return;
        loadLog(testId, t.title || testId);
      });
    });

    grid.querySelectorAll(".board-test-iperf-ip").forEach((input) => {
      input.addEventListener("click", (e) => e.stopPropagation());
      input.addEventListener("input", (e) => {
        iperfServerIp = e.target.value;
      });
    });

    grid.querySelectorAll(".board-test-iperf-bw").forEach((input) => {
      input.addEventListener("click", (e) => e.stopPropagation());
      input.addEventListener("input", (e) => {
        iperfMinBw = e.target.value;
      });
    });

    grid.querySelectorAll(".board-test-iperf10g-ip").forEach((input) => {
      input.addEventListener("click", (e) => e.stopPropagation());
      input.addEventListener("input", (e) => {
        iperf10gServerIp = e.target.value;
      });
    });

    grid.querySelectorAll(".board-test-iperf10g-bw").forEach((input) => {
      input.addEventListener("click", (e) => e.stopPropagation());
      input.addEventListener("input", (e) => {
        iperf10gMinBw = e.target.value;
      });
    });

    grid.querySelectorAll(".board-test-rerun").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const testId = btn.getAttribute("data-rerun") || "";
        if (!testId) return;
        try {
          var payload = {};
          if (testId === "iperf-test") payload.iperf = getIperfPayload();
          if (testId === "iperf10g-test") payload.iperf10g = getIperf10gPayload();
          await fetchJson("/api/board-test/rerun/" + encodeURIComponent(testId), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          await refreshStatus();
        } catch (err) {
          outputHead.textContent = "Output";
          output.textContent = "Failed to rerun " + testId + ": " + err.message;
        }
      });
    });

    grid.querySelectorAll(".board-test-run-single").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const testId = btn.getAttribute("data-run-single") || "";
        if (!testId) return;
        try {
          var payload = {};
          if (testId === "iperf-test") payload.iperf = getIperfPayload();
          if (testId === "iperf10g-test") payload.iperf10g = getIperf10gPayload();
          await fetchJson("/api/board-test/rerun/" + encodeURIComponent(testId), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          await refreshStatus();
        } catch (err) {
          outputHead.textContent = "Output";
          output.textContent = "Failed to run " + testId + ": " + err.message;
        }
      });
    });

    if (selectedTestId) {
      const stillExists = tests.some((t) => t.id === selectedTestId);
      if (!stillExists) {
        selectedTestId = "";
        outputHead.textContent = "Output";
        output.textContent = "Select a completed test to view captured output.";
      }
    } else if (!snapshot.started_once && !snapshot.running) {
      if (snapshot.discovering) {
        output.textContent = "Checking board compatibility and discovering available tests...";
      } else if (snapshot.discovered && tests.length === 0) {
        output.textContent = "No tests discovered.";
      } else if (snapshot.discovered) {
        output.textContent = "Select a completed test to view captured output.";
      }
    }
  }

  async function refreshStatus() {
    const snap = await fetchJson("/api/board-test/status");
    render(snap);
    return snap;
  }


  async function runAllManual() {
    try {
      await fetchJson("/api/board-test/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true, iperf: getIperfPayload(), iperf10g: getIperf10gPayload() }),
      });
      await refreshStatus();
    } catch (err) {
      outputHead.textContent = "Output";
      output.textContent = "Failed to start run-all: " + err.message;
    }
  }

  let discoverAttempts = 0;
  const MAX_DISCOVER_RETRIES = 5;
  const DISCOVER_RETRY_S = 50;
  let discoverStartedAt = 0;

  async function discoverTests(force) {
    try {
      const payload = force ? { force: true } : {};
      await fetchJson("/api/board-test/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      discoverStartedAt = Date.now();
    } catch (_err) {
      // Discovery is best-effort; polling will pick up the result.
    }
  }

  function checkDiscoverHealth(snap) {
    if (snap.discovered || snap.running || snap.started_once) return;
    if (!snap.discovering && discoverAttempts === 0) {
      // Initial kick not yet sent.
      return;
    }
    var elapsed = (Date.now() - discoverStartedAt) / 1000;
    if (snap.discovering && elapsed < DISCOVER_RETRY_S) return;
    // Discovery stalled or failed without setting discovered.
    if (discoverAttempts < MAX_DISCOVER_RETRIES) {
      discoverAttempts++;
      discoverTests(true);
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      refreshStatus().then(checkDiscoverHealth).catch(() => {});
    }, 1500);
  }

  if (backBtn) {
    backBtn.addEventListener("click", () => {
      window.location.assign("/");
    });
  }

  if (runAllBtn) {
    runAllBtn.addEventListener("click", runAllManual);
  }

  // Always force a fresh PCIe + test discovery when the page is loaded so we
  // never display a stale negative result cached from earlier in boot (the
  // Versal endpoint can take a few seconds to enumerate after the x86 host
  // is up, and rave-menu starts before that completes).
  output.textContent = "Checking board compatibility and discovering available tests...";
  discoverAttempts++;
  discoverTests(true);
  startPolling();
})();
