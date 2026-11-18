/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* BIOS Update section of the System Update page (Phase 1 -- see
 * docs/bios-update-plan.md). Reuses the same upload/history/USB-browse
 * widget as the App Store's manual install field, and the same
 * confirm-dialog and SSE progress-log elements already on this page.
 *
 * Flow: pick a source (URL/Dropbox/local path/upload/USB) -> confirm the
 * local-access warning -> POST /api/run/bios-update -> stream progress via
 * /api/stream/<job_id>. The backend script stages everything and arms a
 * one-shot UEFI boot entry but does NOT reboot -- the operator must press
 * the page's existing "Reboot Now" button once they've read the log.
 */
(function () {
  "use strict";

  var statusEl = document.getElementById("biosStatus");
  var form = document.getElementById("biosForm");
  var input = document.getElementById("biosInput");
  var updateBtn = document.getElementById("biosUpdateBtn");
  var uploadStatus = document.getElementById("biosUploadStatus");
  var row = form && form.querySelector(".app-source-row");
  if (!statusEl || !form || !input || !updateBtn || !row) return;

  var progressWrap = document.getElementById("progressWrap");
  var progressBar = document.getElementById("progressBar");
  var progressLabel = document.getElementById("progressLabel");
  var progressLog = document.getElementById("progressLog");
  var rebootBtn = document.getElementById("rebootBtn");

  var jobRunning = false;

  function fmtSize(bytes) {
    if (!bytes || bytes < 0) return "";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + units[i];
  }

  function isBundleSource(s) {
    s = (s || "").trim();
    return /^https?:\/\/\S+$/i.test(s) || s.charAt(0) === "/";
  }

  /* Themed confirm dialog -> Promise<boolean>. Falls back to window.confirm. */
  function showConfirm(message, opts) {
    opts = opts || {};
    var title = opts.title || "Please confirm";
    var okLabel = opts.okLabel || "Continue";
    var cancelLabel = opts.cancelLabel || "Cancel";
    return new Promise(function (resolve) {
      var modal = document.getElementById("confirmModal");
      var msgEl = document.getElementById("confirmMessage");
      var titleEl = document.getElementById("confirmTitle");
      var okBtn = document.getElementById("confirmOk");
      var cancelBtn = document.getElementById("confirmCancel");
      if (!modal || !msgEl || !okBtn || !cancelBtn) {
        resolve(window.confirm(message));
        return;
      }
      if (titleEl) titleEl.textContent = title;
      msgEl.textContent = message;
      okBtn.textContent = okLabel;
      cancelBtn.textContent = cancelLabel;
      modal.classList.remove("hidden");
      requestAnimationFrame(function () { cancelBtn.focus(); });

      function cleanup(result) {
        modal.classList.add("hidden");
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onKey(e) {
        if (e.key === "Escape") cleanup(false);
        else if (e.key === "Enter") cleanup(true);
      }
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
    });
  }
  window.showConfirm = window.showConfirm || showConfirm;

  // ── Status: detected BIOS vendor / product line / version, pending result ──

  function renderStatus(data) {
    var bits = [];
    bits.push("Vendor: " + (data.vendor === "unknown" ? "unknown" : data.vendor));
    bits.push("Product line: " + (data.product_line === "unknown" ? "unknown" : data.product_line));
    if (data.version) bits.push("Version: " + data.version);
    statusEl.textContent = bits.join(" \u00b7 ");

    // A pending record just means a prior attempt staged something and armed
    // BootNext; by the time this page can load, that boot has already come
    // and gone (successfully or not) and the BIOS version above already
    // reflects reality. Nothing for the user to act on -- clear it silently.
    if (data.pending) {
      fetch("/api/bios-pending/clear", { method: "POST" }).catch(function () {});
    }
  }

  function loadBiosStatus() {
    fetch("/api/bios-status").then(function (r) { return r.json(); }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "failed");
      renderStatus(data);
    }).catch(function (err) {
      statusEl.textContent = "Could not read BIOS status: " + err.message;
    });
  }
  loadBiosStatus();

  // ── Source field: upload / history / USB browse (file_browser.js handles
  //    the .browse-btn[data-target] wiring generically) ──

  function uploadFile(file) {
    if (!file || typeof file.size !== "number" || file.size === 0) {
      uploadStatus.className = "app-upload-status is-error";
      uploadStatus.textContent = "Select a non-empty regular file.";
      return;
    }
    row.classList.add("is-uploading");
    uploadStatus.className = "app-upload-status";
    uploadStatus.innerHTML = "Uploading <strong></strong> \u2014 <span class=\"pct\">0%</span> of " +
      fmtSize(file.size) + "<span class=\"upload-bar\"><span></span></span>";
    uploadStatus.querySelector("strong").textContent = file.name;
    var pct = uploadStatus.querySelector(".pct");
    var bar = uploadStatus.querySelector(".upload-bar > span");
    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload-payload");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      var progress = Math.round(event.loaded / event.total * 100);
      pct.textContent = progress + "%";
      bar.style.width = progress + "%";
    };
    xhr.onload = function () {
      row.classList.remove("is-uploading");
      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300 && data.ok && data.path) {
        input.value = data.path;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        uploadStatus.className = "app-upload-status is-success";
        uploadStatus.textContent = "Uploaded " + data.name + " (" + fmtSize(data.size) + ")";
      } else {
        uploadStatus.className = "app-upload-status is-error";
        uploadStatus.textContent = "Upload failed: " + (data.error || ("HTTP " + xhr.status));
      }
    };
    xhr.onerror = function () {
      row.classList.remove("is-uploading");
      uploadStatus.className = "app-upload-status is-error";
      uploadStatus.textContent = "Upload failed: network error";
    };
    xhr.send(file);
  }

  var picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".zip,application/zip,application/octet-stream";
  picker.hidden = true;
  row.appendChild(picker);
  row.querySelector(".upload-btn").addEventListener("click", function () { picker.click(); });
  picker.addEventListener("change", function () {
    if (picker.files && picker.files.length) uploadFile(picker.files[0]);
    picker.value = "";
  });
  ["dragenter", "dragover"].forEach(function (name) {
    row.addEventListener(name, function (e) { e.preventDefault(); row.classList.add("is-drop-hover"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    row.addEventListener(name, function (e) { e.preventDefault(); row.classList.remove("is-drop-hover"); });
  });
  row.addEventListener("drop", function (e) {
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) uploadFile(files[0]);
  });

  // History button opens the shared uploadHistoryModal (see app_store.js for
  // the original; duplicated here since each page wires its own instance).
  var historyModal = document.getElementById("uploadHistoryModal");
  var historyList = document.getElementById("uploadHistoryList");
  var historyStatusEl = document.getElementById("uploadHistoryStatus");
  var historyFree = document.getElementById("uploadHistoryFree");

  function closeUploadHistory() { if (historyModal) historyModal.classList.add("hidden"); }

  function renderUploadHistory(items) {
    historyList.textContent = "";
    if (!items.length) {
      var empty = document.createElement("li");
      empty.className = "uh-empty";
      empty.textContent = "No uploaded files yet.";
      historyList.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      var entry = document.createElement("li");
      entry.title = item.path;
      var name = document.createElement("span");
      name.className = "uh-name";
      name.textContent = item.name;
      var meta = document.createElement("span");
      meta.className = "uh-meta";
      meta.textContent = fmtSize(item.size);
      entry.appendChild(name);
      entry.appendChild(meta);
      entry.addEventListener("click", function () {
        input.value = item.path;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        uploadStatus.className = "app-upload-status is-success";
        uploadStatus.textContent = "Selected " + item.name + " (" + fmtSize(item.size) + ")";
        closeUploadHistory();
      });
      historyList.appendChild(entry);
    });
  }

  function refreshUploadHistory() {
    historyStatusEl.className = "upload-history-status";
    historyStatusEl.textContent = "Loading\u2026";
    fetch("/api/uploads").then(function (r) { return r.json(); }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "list failed");
      historyStatusEl.textContent = data.items.length ? "Select a file to use." : "";
      historyFree.textContent = typeof data.free === "number" ? "Free: " + fmtSize(data.free) : "";
      renderUploadHistory(data.items || []);
    }).catch(function (err) {
      historyStatusEl.className = "upload-history-status is-error";
      historyStatusEl.textContent = "Could not list uploads: " + err.message;
    });
  }

  if (historyModal) {
    document.getElementById("uploadHistoryClose").addEventListener("click", closeUploadHistory);
    historyModal.addEventListener("click", function (e) { if (e.target === historyModal) closeUploadHistory(); });
    row.querySelector(".history-btn").addEventListener("click", function () {
      historyModal.classList.remove("hidden");
      refreshUploadHistory();
    });
  }

  // ── Submit ──

  input.addEventListener("input", function () {
    updateBtn.disabled = jobRunning || !input.value.trim();
  });

  function setRunning(running) {
    jobRunning = running;
    updateBtn.disabled = running || !input.value.trim();
  }

  updateBtn.addEventListener("click", async function () {
    if (jobRunning) return;
    var source = input.value.trim();
    if (!source) return;

    var ok = await showConfirm(
      "This will stage a BIOS update from:\n" + source + "\n\n" +
      "Local physical access to the board is required to complete BIOS " +
      "programming. This is NOT recommended to be done remotely -- if the " +
      "update misbehaves, someone must be at the machine to recover it.\n\n" +
      "Nothing is flashed until you reboot afterward.",
      { title: "BIOS update", okLabel: "Stage update" }
    );
    if (!ok) return;

    setRunning(true);
    progressWrap.classList.add("active");
    progressBar.style.width = "0%";
    progressLog.textContent = "";
    progressLabel.textContent = "Staging BIOS update...";

    try {
      var resp = await fetch("/api/run/bios-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ args: [source] })
      });
      var data = await resp.json();
      if (resp.status === 409) throw new Error(data.error || "Another operation is already running");
      if (!data.job_id) throw new Error(data.error || "Failed to start");

      var es = new EventSource("/api/stream/" + data.job_id);
      var lines = 0;
      es.onmessage = function (ev) {
        lines++;
        progressLog.textContent += ev.data + "\n";
        progressLog.scrollTop = progressLog.scrollHeight;
        progressBar.style.width = Math.min(90, lines * 3) + "%";
      };
      es.addEventListener("status", function (ev) {
        es.close();
        var parts = ev.data.split(":");
        var code = parseInt(parts[1], 10);
        if (code === 0) {
          progressBar.style.width = "100%";
          progressLabel.textContent = "BIOS update staged. Reboot now to apply it.";
          rebootBtn.style.display = "block";
        } else {
          progressLabel.textContent = "BIOS update staging failed (exit code " + code + "). See log above.";
        }
        setRunning(false);
        loadBiosStatus();
      });
      es.onerror = function () {
        es.close();
        progressLabel.textContent = "Stream disconnected.";
        setRunning(false);
      };
    } catch (err) {
      progressLabel.textContent = "Error: " + err.message;
      setRunning(false);
    }
  });
})();
