// Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
// SPDX-License-Identifier: MIT
// Contributors: KC
// Maintainer: KC

/* Support Logs panel on the System Update page: status, the recording toggle
   and the download link. */

(function () {
  var toggle = document.getElementById("supportLogEnabled");
  var summary = document.getElementById("supportLogSummary");
  var download = document.getElementById("supportLogDownload");
  if (!toggle || !summary) return;

  function humanSize(bytes) {
    if (!bytes) return "0 B";
    var units = ["B", "KiB", "MiB", "GiB"];
    var i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i += 1; }
    return (i === 0 ? bytes : bytes.toFixed(1)) + " " + units[i];
  }

  function render(data) {
    toggle.checked = !!data.enabled;
    if (download) download.style.display = data.entries ? "" : "none";

    var parts = [];
    if (data.entries) {
      parts.push(window.t("{n} entries").replace("{n}", data.entries));
      parts.push(humanSize(data.bytes));
      if (data.newest) parts.push(window.t("last entry") + " " + data.newest);
    } else {
      parts.push(window.t("No entries yet."));
    }
    if (!data.enabled) parts.push(window.t("Recording is off."));

    summary.textContent = parts.join(" · ");

    // Both of these mean the log is not doing its job, so say so here rather
    // than leaving the user to discover it when support asks for the archive.
    if (data.broken) {
      appendWarning(window.t("Logging stopped: ") + data.broken);
    } else if (data.degraded) {
      appendWarning(window.t(
        "Persistent storage is unavailable, so these logs will not survive a system update."));
    }
  }

  function appendWarning(text) {
    var note = document.createElement("div");
    note.style.cssText = "margin-top:6px; color:var(--accent2);";
    note.textContent = text;
    summary.appendChild(note);
  }

  function fail(key) {
    summary.textContent = window.t(key);
  }

  function load() {
    fetch("/api/support-logs")
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { fail("Could not read the support log status."); });
  }

  toggle.addEventListener("change", function () {
    toggle.disabled = true;
    fetch("/api/support-logs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: toggle.checked }),
    })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { fail("Could not change the setting."); })
      .then(function () { toggle.disabled = false; });
  });

  load();
})();
