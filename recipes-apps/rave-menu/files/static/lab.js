/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* Versal(TM) AI Edge Series Gen 2 Lab modal.
 *
 * Wires the lab modal: open/close, HW Server toggle, and the lab-local
 * "Program custom Versal payload" collapsible form.
 *
 * Depends on globals from app.js:  showConfirm(), VERSAL_REPROGRAM_CONFIRM_MSG, runAction()
 */
(function () {
  var modal = document.getElementById("versalLabModal");
  if (!modal) return;

  var closeBtn = document.getElementById("versalLabClose");
  if (closeBtn) closeBtn.addEventListener("click", function () { modal.classList.add("hidden"); });
  modal.addEventListener("click", function (e) { if (e.target === modal) modal.classList.add("hidden"); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) modal.classList.add("hidden");
  });

  // ── Global programming-toolchain selector + HW Server toggle ──────────
  // One selection drives both: the server serves the selected release and
  // programming uses it too, so client and server always match.
  var toggle = document.getElementById("hwServerToggle");
  var statusEl = document.getElementById("hwServerStatus");
  var optsEl = document.getElementById("toolsVersionOpts");
  var noteEl = document.getElementById("toolsVersionNote");

  var lastTools = null;

  // Releases are listed oldest-first, so the segmented control reads the way
  // people say them: 2025.2 then 2026.1.
  function versionParts(v) {
    return (String(v).match(/\d+/g) || []).map(Number);
  }

  function compareVersions(a, b) {
    var x = versionParts(a), y = versionParts(b);
    for (var i = 0; i < Math.max(x.length, y.length); i++) {
      var d = (x[i] || 0) - (y[i] || 0);
      if (d) return d;
    }
    return 0;
  }

  function renderVersions(s) {
    if (!optsEl) return;
    var list = ((s && s.versions) || []).slice().sort(function (a, b) {
      return compareVersions(a.version, b.version);
    });
    if (!list.length) {
      optsEl.innerHTML = '<span class="tools-seg-loading">none available</span>';
      return;
    }
    var selected = (s && s.selected) || (s && s.effective) || "";
    optsEl.innerHTML = "";
    list.forEach(function (v) {
      var id = "toolsVer_" + v.version.replace(/[^\w.-]/g, "_");
      var active = v.version === selected;
      var label = document.createElement("label");
      label.className = "tools-seg-opt" +
        (active ? " is-active" : "") +
        (v.installed ? "" : " is-uninstalled");
      label.setAttribute("for", id);
      label.title = v.installed
        ? "Use " + v.version + " for programming and the hardware server"
        : v.version + " is not installed — choosing it starts the installer";

      var input = document.createElement("input");
      input.type = "radio";
      input.name = "toolsVersion";
      input.id = id;
      input.value = v.version;
      input.checked = active;
      // Deliberately not disabled when uninstalled: the grey means "needs
      // installing", and the click is what starts the install flow.
      input.dataset.installed = v.installed ? "1" : "0";

      var dot = document.createElement("span");
      dot.className = "tools-seg-dot";

      var text = document.createElement("span");
      text.className = "tools-seg-text";
      text.textContent = v.version;

      label.appendChild(input);
      label.appendChild(dot);
      label.appendChild(text);
      optsEl.appendChild(label);
    });
  }

  // Normally silent: the pill already states the choice, and changing the
  // choice stops any server, so the effective release matches it. This only
  // speaks for the cases selection alone cannot cover -- a server started
  // outside the menu, an env override, or a chosen release gone missing.
  function renderToolsNote(s) {
    if (!noteEl) return;
    var lines = (s && s.effective) ? (s.warnings || []).slice()
                                   : ["No Vivado HW Server installed."];
    noteEl.textContent = lines.join("\n");
    noteEl.classList.toggle("hidden", lines.length === 0);
  }

  function fetchTools() {
    return fetch("/api/tools/version")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        lastTools = s;
        renderVersions(s);
        renderToolsNote(s);
        return s;
      })
      .catch(function () { /* leave previous render in place */ });
  }

  function renderHwStatus(s) {
    if (!statusEl || !toggle) return;
    if (!s || s.error === "request failed") {
      statusEl.textContent = "Status: error — request failed";
      return;
    }
    if (s.running) {
      toggle.checked = true;
      // Single multi-line status block. No separate endpoint box —
      // each reachable IPv4 address is appended directly under the
      // running line so the lab card stays compact.
      var cs = s.cs_running
        ? "CS Server: PID " + s.cs_pid + " Port " + s.cs_port
        : "CS Server: stopped" + (s.cs_error ? " -- " + s.cs_error : "");
      var head = "Status: Running " + (s.version ? s.version + " " : "") +
        "(HW Server: PID " + s.pid + " Port " + s.port + "  " + cs + ")";
      var lines = [head];
      var ips = s.ips || [];
      if (ips.length) {
        ips.forEach(function (ip) {
          lines.push(ip.iface + "  \u2192  " + ip.addr + ":" + s.port +
            (s.cs_running ? "  /  " + ip.addr + ":" + s.cs_port : ""));
        });
      } else {
        lines.push("(no non-loopback IPv4 address detected)");
      }
      statusEl.textContent = lines.join("\n");
    } else {
      toggle.checked = false;
      var msg = "Status: stopped";
      if (s.error === "busy") {
        msg = "Status: unchanged — " + (s.message || "operation in progress");
      } else if (s.error === "not_installed") {
        msg += " — " + (s.message || "not installed");
      } else if (s.error) {
        msg += " — " + s.error;
      } else if (!s.binary) {
        msg += " — hw_server binary not found";
      }
      statusEl.textContent = msg;
    }
  }

  function fetchHwStatus() {
    fetch("/api/hw-server/status")
      .then(function (r) { return r.json(); })
      .then(function (s) { renderHwStatus(s); return fetchTools(); })
      .catch(function () { renderHwStatus({ error: "request failed" }); });
  }

  function setHwBusy(busy) {
    if (toggle) toggle.disabled = busy;
    if (optsEl) {
      optsEl.querySelectorAll("input").forEach(function (i) { i.disabled = busy; });
    }
    if (statusEl && busy) statusEl.textContent = "Status: working…";
  }

  function offerInstall(version) {
    var msg = "Vivado HW Server " + version + " is not installed on this device. " +
      "Install it now? Existing versions are kept.";
    var go = function (ok) {
      if (ok) window.location.assign("/vivado-install?version=" + encodeURIComponent(version));
      else fetchHwStatus();
    };
    if (typeof window.showConfirm === "function") {
      window.showConfirm(msg, {
        okLabel: "Install",
        cancelLabel: "Not now",
        title: "Version not installed",
      }).then(go);
    } else {
      go(window.confirm(msg));
    }
  }

  function commitVersion(version) {
    setHwBusy(true);
    fetch("/api/tools/version", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: version }),
    })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        setHwBusy(false);
        if (s && s.error === "not_installed") { offerInstall(version); return; }
        if (s && s.error === "busy") {
          renderHwStatus({ error: "busy", message: s.message });
        }
        fetchHwStatus();
      })
      .catch(function () { setHwBusy(false); fetchHwStatus(); });
  }

  if (optsEl) {
    optsEl.addEventListener("change", function (ev) {
      var input = ev.target;
      if (!input || input.name !== "toolsVersion") return;
      var version = input.value;

      if (input.dataset.installed !== "1") { offerInstall(version); return; }

      // Changing the selection stops a running server: it only ever serves
      // the previously selected release.
      var running = lastTools && lastTools.running;
      if (running && running.version && running.version !== version) {
        var msg = "Switching to " + version + " will stop the running " +
          running.version + " hardware server. Any remote Vivado connection " +
          "will be disconnected. Continue?";
        var go = function (ok) { if (ok) commitVersion(version); else fetchHwStatus(); };
        if (typeof window.showConfirm === "function") {
          window.showConfirm(msg, {
            okLabel: "Switch and stop server",
            cancelLabel: "Cancel",
            title: "Hardware server running",
          }).then(go);
        } else {
          go(window.confirm(msg));
        }
        return;
      }
      commitVersion(version);
    });
  }

  if (toggle) {
    toggle.addEventListener("change", function () {
      var wantOn = toggle.checked;
      var version = (lastTools && (lastTools.selected || lastTools.effective)) || "";
      if (wantOn && version && lastTools) {
        var entry = (lastTools.versions || []).filter(function (v) {
          return v.version === version;
        })[0];
        if (entry && !entry.installed) {
          toggle.checked = false;
          offerInstall(version);
          return;
        }
      }
      setHwBusy(true);
      fetch(wantOn ? "/api/hw-server/start" : "/api/hw-server/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(wantOn && version ? { version: version } : {}),
      })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          setHwBusy(false);
          renderHwStatus(s);
          fetchTools();
          if (s && s.error === "not_installed" && s.version) offerInstall(s.version);
        })
        .catch(function () { setHwBusy(false); fetchHwStatus(); });
    });
  }

  // Refresh HW status when opening the modal.
  var labBtn = document.querySelector('.menu-btn[data-action="versal-lab"]');
  if (labBtn) labBtn.addEventListener("click", function () { setTimeout(fetchHwStatus, 50); });

  // ── Lab "Program custom Versal payload" tabs ───────────────────────
  // Switches between the Flash and JTAG programming forms inside the card.
  var labTabBtns = modal.querySelectorAll(".lab-tab");
  var labTabPanes = modal.querySelectorAll(".lab-tab-pane");
  labTabBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var name = btn.getAttribute("data-tab");
      labTabBtns.forEach(function (b) {
        var on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      labTabPanes.forEach(function (p) {
        var on = p.getAttribute("data-tab-pane") === name;
        p.classList.toggle("hidden", !on);
      });
    });
  });

  // ── Lab "Program custom Versal payload" (always expanded) ───────────
  var labForm = document.getElementById("labCustomUrlForm");
  var labOspi = document.getElementById("labOspiInput");
  var labWic = document.getElementById("labWicInput");
  var labBmap = document.getElementById("labBmapInput");
  var labGo = document.getElementById("labCustomUrlGo");

  // ── Advanced: target partition layout and partial updates ───────────
  // A plan is tied to the image it was scanned against, so any change to the
  // WIC or bmap inputs discards it.
  var labScanBtn = document.getElementById("labScanLayout");
  var labAdvanced = document.getElementById("labAdvanced");
  var labSelectAll = document.getElementById("labSelectAll");
  var labLayoutStatus = document.getElementById("labLayoutStatus");
  var labLayoutResult = document.getElementById("labLayoutResult");
  var labLayoutBar = document.getElementById("labLayoutBar");
  var labLayoutRows = document.getElementById("labLayoutRows");
  var labLayoutMode = document.getElementById("labLayoutMode");
  var labScanProgress = document.getElementById("labScanProgress");
  var labScanFill = document.getElementById("labScanProgressFill");
  var layoutPlan = null;
  var scanTimer = null;

  /* A scan reports no progress of its own, so approximate it: the bar eases
     toward 95% over the ~30s a scan takes, then snaps to 100%. */
  function setScanProgress(pct) {
    if (labScanFill) labScanFill.style.width = Math.max(0, Math.min(100, pct)).toFixed(1) + "%";
  }

  function startScanProgress() {
    var started = Date.now();
    if (scanTimer) clearInterval(scanTimer);
    if (labScanProgress) labScanProgress.classList.remove("hidden");
    setScanProgress(2);
    scanTimer = setInterval(function () {
      var elapsed = (Date.now() - started) / 1000;
      setScanProgress(95 * (1 - Math.exp(-elapsed / 12)));
    }, 250);
  }

  function finishScanProgress(ok) {
    if (scanTimer) { clearInterval(scanTimer); scanTimer = null; }
    if (!labScanProgress) return;
    if (!ok) {
      labScanProgress.classList.add("hidden");
      setScanProgress(0);
      return;
    }
    setScanProgress(100);
    setTimeout(function () {
      labScanProgress.classList.add("hidden");
      setScanProgress(0);
    }, 600);
  }

  function fmtPartSize(n) {
    if (typeof n !== "number" || n <= 0) return "—";
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 100 || i <= 1 ? 0 : 1) + " " + units[i];
  }

  function layoutBoxes() {
    if (!labLayoutRows) return [];
    return Array.prototype.slice.call(labLayoutRows.querySelectorAll(".lab-part-check"));
  }

  function resetLayout(message, isError) {
    layoutPlan = null;
    updateZeroNote(0);
    if (labLayoutResult) labLayoutResult.classList.add("hidden");
    if (labSelectAll) labSelectAll.classList.add("hidden");
    if (!labLayoutStatus) return;
    labLayoutStatus.classList.remove("is-error", "is-success");
    if (isError) labLayoutStatus.classList.add("is-error");
    labLayoutStatus.textContent = message || "";
  }

  function rowFor(index) {
    var match = null;
    (layoutPlan ? layoutPlan.rows : []).forEach(function (row) {
      if (row.index === index) match = row;
    });
    return match;
  }

  /* Full write = every partition checked. Clearing any box drops to a partial
     update, which cannot touch partitions the target has grown. */
  /* The erase covers exactly what is being programmed, so say which that is. */
  function updateZeroNote(partialCount) {
    var note = document.getElementById("labZeroNote");
    if (!note) return;
    note.textContent = partialCount
      ? "Erases only the " + partialCount + " selected partition(s). " +
        "Every other partition is left untouched."
      : "Erases the whole image span, including the data partition, then writes " +
        "the image over it. Takes a few seconds on UFS.";
  }

  function refreshLayoutMode() {
    if (!layoutPlan) return;
    var boxes = layoutBoxes();
    var full = boxes.every(function (box) { return box.checked; });
    var partialAllowed = layoutPlan.partial_supported;

    boxes.forEach(function (box) {
      var row = rowFor(parseInt(box.value, 10));
      if (!partialAllowed) {
        box.checked = true;
        box.disabled = true;
      } else if (!full && row && !row.selectable) {
        box.checked = false;
        box.disabled = true;
      } else {
        box.disabled = false;
      }
    });

    var picked = boxes.filter(function (box) { return box.checked; });
    if (labSelectAll) labSelectAll.classList.toggle("hidden", full);
    if (labGo) labGo.disabled = partialAllowed && !full && picked.length === 0;

    updateZeroNote(full ? 0 : picked.length);

    if (!labLayoutMode) return;
    if (full) {
      labLayoutMode.textContent =
        "Full image write — the partition table and every partition will be written.";
      labLayoutMode.className = "lab-partmode";
    } else if (picked.length === 0) {
      labLayoutMode.textContent = "Select at least one partition, or select all for a full image write.";
      labLayoutMode.className = "lab-partmode is-error";
    } else {
      labLayoutMode.textContent =
        "Partial update — writing " + picked.length + " partition(s). " +
        "The partition table will not be modified.";
      labLayoutMode.className = "lab-partmode is-partial";
    }
  }

  function renderLayout(data) {
    layoutPlan = data.plan;
    var rows = data.plan.rows;
    var deviceSize = data.target.device_size || 1;

    var bar = "";
    var cursor = 0;
    rows.forEach(function (row, i) {
      if (typeof row.target_size !== "number") return;
      if (row.start > cursor) {
        bar += '<span class="lab-part lab-part-gap" style="flex:' + (row.start - cursor) + '"></span>';
      }
      bar += '<span class="lab-part lab-part-' + ((i % 4) + 1) + '" style="flex:' + row.target_size + '">' +
             '<span class="lab-part-name"></span></span>';
      cursor = row.start + row.target_size;
    });
    if (cursor < deviceSize) {
      bar += '<span class="lab-part lab-part-free" style="flex:' + (deviceSize - cursor) + '"></span>';
    }
    labLayoutBar.innerHTML = bar;
    var names = labLayoutBar.querySelectorAll(".lab-part-name");
    var named = rows.filter(function (row) { return typeof row.target_size === "number"; });
    for (var n = 0; n < names.length && n < named.length; n++) {
      names[n].textContent = named[n].name || ("p" + named[n].index);
    }

    labLayoutRows.innerHTML = "";
    rows.forEach(function (row) {
      var item = document.createElement("label");
      item.className = "lab-partrow";

      var box = document.createElement("input");
      box.type = "checkbox";
      box.className = "lab-part-check";
      box.value = String(row.index);
      box.checked = true;
      box.addEventListener("change", refreshLayoutMode);
      item.appendChild(box);

      var main = document.createElement("span");
      main.className = "lab-partrow-main";
      var title = document.createElement("strong");
      title.textContent = "p" + row.index + "  " + (row.name || "(unnamed)");
      main.appendChild(title);

      var meta = document.createElement("span");
      meta.className = "lab-partrow-meta";
      var sizeText = fmtPartSize(row.image_size);
      if (row.image_size !== row.target_size) {
        sizeText = "image " + fmtPartSize(row.image_size) +
                   " → target " + fmtPartSize(row.target_size);
      }
      meta.textContent = row.type_name + " · " + sizeText + " · at " + fmtPartSize(row.start);
      main.appendChild(meta);

      if (row.reason || row.warning) {
        var note = document.createElement("span");
        note.className = "lab-partrow-note" + (row.reason ? " is-blocked" : "");
        note.textContent = row.reason || row.warning;
        main.appendChild(note);
      }
      item.appendChild(main);
      labLayoutRows.appendChild(item);
    });

    labLayoutResult.classList.remove("hidden");
    labLayoutStatus.classList.remove("is-error");
    labLayoutStatus.classList.add("is-success");
    labLayoutStatus.textContent = data.plan.blocker
      ? "Target read. " + data.plan.blocker
      : "Target read. Clear any partition to switch to a partial update.";
    refreshLayoutMode();
  }

  if (labScanBtn) {
    labScanBtn.addEventListener("click", function () {
      var wic = labWic.value.trim();
      if (!wic) {
        resetLayout("Enter a Rootfs WIC image first.", true);
        return;
      }
      labScanBtn.disabled = true;
      resetLayout("Reading the partition table from the target…");
      startScanProgress();
      fetch("/api/versal/layout/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wic: wic, bmap: labBmap.value.trim() })
      })
        .then(function (response) {
          return response.json().then(function (body) { return { ok: response.ok, body: body }; });
        })
        .then(function (result) {
          if (!result.ok || !result.body.ok) {
            finishScanProgress(false);
            resetLayout(result.body.error || "Could not read the partition table.", true);
            return;
          }
          finishScanProgress(true);
          renderLayout(result.body);
        })
        .catch(function (err) {
          finishScanProgress(false);
          resetLayout("Layout scan failed: " + (err && err.message ? err.message : err), true);
        })
        .then(function () { labScanBtn.disabled = false; });
    });
  }

  if (labSelectAll) {
    labSelectAll.addEventListener("click", function () {
      layoutBoxes().forEach(function (box) { box.disabled = false; box.checked = true; });
      refreshLayoutMode();
    });
  }

  /* Partial updates need a bmap, so the panel only appears once one is given. */
  function updateAdvancedVisibility() {
    if (!labAdvanced) return;
    var hasBmap = !!labBmap.value.trim();
    labAdvanced.classList.toggle("hidden", !hasBmap);
    if (!hasBmap) labAdvanced.open = false;
    // No layout scanned yet means the whole image span is in scope.
    if (!layoutPlan) updateZeroNote(0);
  }

  function updateLabGo() {
    if (labGo) labGo.disabled = !(labOspi.value.trim() || labWic.value.trim());
  }
  if (labForm && labOspi && labWic && labBmap) {
    labOspi.addEventListener("input", updateLabGo);
    labWic.addEventListener("input", function () { resetLayout(""); updateLabGo(); });
    labBmap.addEventListener("input", function () {
      resetLayout("");
      updateAdvancedVisibility();
    });
    updateAdvancedVisibility();
    labForm.addEventListener("submit", async function (e) {
      e.preventDefault();

      var ranges = "";
      if (layoutPlan && layoutPlan.partial_supported) {
        var boxes = layoutBoxes();
        var full = boxes.every(function (box) { return box.checked; });
        if (!full) {
          var picked = boxes
            .filter(function (box) { return box.checked; })
            .map(function (box) { return rowFor(parseInt(box.value, 10)); })
            .filter(Boolean);
          if (!picked.length) return;
          ranges = picked.map(function (row) {
            return row.start + ":" + row.image_size;
          }).join(",");
        }
      }

      var args = await window.confirmVersalProgram(
        labOspi.value, labWic.value, labBmap.value,
        ranges ? ranges.split(",").length : 0);
      if (!args) return;
      var zeroFirst = document.getElementById("labZeroFirst");
      // Both slots must be filled: a hole in the array serialises as null and
      // reaches the script as the literal string "None".
      args[3] = ranges;
      args[4] = (zeroFirst && zeroFirst.checked) ? "1" : "";
      modal.classList.add("hidden");
      runAction("board-test", "Program custom Versal payload", true, args);
    });
  }

  // ── JTAG Program tab ────────────────────────────────────────────────
  // Single file input (boot.bin or .pdi). The "Configure" button triggers
  // the JTAG programming flow: reconfigure the Versal into JTAG boot mode,
  // then drive XSDB to program the supplied image.
  var jtagForm = document.getElementById("labJtagForm");
  var jtagInput = document.getElementById("labJtagInput");
  var jtagGo = document.getElementById("labJtagGo");
  function updateJtagGo() {
    if (jtagGo) jtagGo.disabled = !jtagInput.value.trim();
  }
  if (jtagForm && jtagInput && jtagGo) {
    jtagInput.addEventListener("input", updateJtagGo);
    jtagForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var path = jtagInput.value.trim();
      if (!path) return;
      var ok = await showConfirm(
        "Reconfigure the Versal to JTAG boot mode and program the selected file?",
        { okLabel: "Configure" }
      );
      if (!ok) return;
      modal.classList.add("hidden");
      runAction("versal-jtag", "JTAG program Versal", true, [path]);
    });
  }

  // ── File upload via the dedicated upload button on each .browse-row ─
  // Clicking the row's upload button opens a hidden <input type=file>;
  // the picked file is streamed to /api/upload-payload and the matching
  // text input is set to the saved server-side absolute path.
  function fmtBytes(n) {
    if (!isFinite(n) || n <= 0) return "0 B";
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + units[i];
  }

  function uploadFileToInput(row, input, status, file) {
    if (!file) {
      status.classList.add("is-error");
      status.textContent = "No file selected";
      return;
    }
    if (typeof file.size !== "number" || file.size === 0) {
      status.classList.add("is-error");
      status.textContent =
        "Cannot read \"" + (file.name || "selected item") + "\" " +
        "(0 bytes — looks like a folder or a link, not a regular file)";
      return;
    }

    row.classList.add("is-uploading");
    status.classList.remove("is-error", "is-success");
    status.innerHTML =
      'Uploading <strong></strong> — ' +
      '<span class="pct">0%</span> of ' + fmtBytes(file.size) +
      '<span class="upload-bar"><span></span></span>';
    status.querySelector("strong").textContent = file.name;

    var bar = status.querySelector(".upload-bar > span");
    var pct = status.querySelector(".pct");
    var lastLoaded = 0;

    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload-payload");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");

    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      lastLoaded = e.loaded;
      var p = Math.round((e.loaded / e.total) * 100);
      if (bar) bar.style.width = p + "%";
      if (pct) pct.textContent = p + "%";
    };

    xhr.onload = function () {
      row.classList.remove("is-uploading");
      var ok = xhr.status >= 200 && xhr.status < 300;
      var resp = null;
      try { resp = JSON.parse(xhr.responseText); } catch (e) {}
      if (ok && resp && resp.ok && resp.path) {
        input.value = resp.path;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        status.classList.add("is-success");
        status.textContent = "Uploaded " + resp.name + " (" + fmtBytes(resp.size) + ")";
      } else {
        status.classList.add("is-error");
        var err = (resp && resp.error) || ("HTTP " + xhr.status + " " + xhr.statusText);
        status.textContent = "Upload failed: " + err;
      }
    };

    xhr.onerror = function () {
      row.classList.remove("is-uploading");
      status.classList.add("is-error");
      var detail =
        "after " + fmtBytes(lastLoaded) + " of " + fmtBytes(file.size) +
        (xhr.status ? " — HTTP " + xhr.status : " — no response (connection reset)");
      status.textContent = "Upload failed: network error " + detail;
      try {
        console.error("[upload] xhr error", {
          name: file.name, size: file.size, type: file.type,
          loaded: lastLoaded, status: xhr.status, statusText: xhr.statusText
        });
      } catch (_) {}
    };

    xhr.onabort = function () {
      row.classList.remove("is-uploading");
      status.classList.add("is-error");
      status.textContent = "Upload aborted after " + fmtBytes(lastLoaded);
    };

    try {
      xhr.send(file);
    } catch (e) {
      row.classList.remove("is-uploading");
      status.classList.add("is-error");
      status.textContent = "Upload failed: could not start request (" + (e && e.message || e) + ")";
    }
  }

  modal.querySelectorAll(".browse-row[data-drop-target]").forEach(function (row) {
    var inputId = row.getAttribute("data-drop-target");
    var input = document.getElementById(inputId);
    var status = modal.querySelector('.upload-status[data-upload-status="' + inputId + '"]');
    if (!input || !status) return;

    // Hidden <input type=file> triggered by the row's upload button.
    var picker = document.createElement("input");
    picker.type = "file";
    picker.style.position = "absolute";
    picker.style.left = "-9999px";
    picker.style.width = "1px";
    picker.style.height = "1px";
    picker.style.opacity = "0";
    row.appendChild(picker);
    picker.addEventListener("change", function () {
      if (picker.files && picker.files.length) {
        uploadFileToInput(row, input, status, picker.files[0]);
      }
      picker.value = "";  // allow re-picking the same file
    });

    var uploadBtn = row.querySelector('.upload-btn[data-upload-target="' + inputId + '"]');
    if (uploadBtn) {
      uploadBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        picker.click();
      });
    }

    var historyBtn = row.querySelector('.history-btn[data-history-target="' + inputId + '"]');
    if (historyBtn) {
      historyBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        openHistory(input, status);
      });
    }
  });

  // ── Upload history modal ─────────────────────────────────────────────
  var histModal = document.getElementById("uploadHistoryModal");
  var histList = document.getElementById("uploadHistoryList");
  var histStatus = document.getElementById("uploadHistoryStatus");
  var histClose = document.getElementById("uploadHistoryClose");
  var histClear = document.getElementById("uploadHistoryClear");
  var histFree = document.getElementById("uploadHistoryFree");
  var histTargetInput = null;
  var histTargetStatus = null;

  function closeHistory() { if (histModal) histModal.classList.add("hidden"); }
  if (histClose) histClose.addEventListener("click", closeHistory);
  if (histModal) {
    histModal.addEventListener("click", function (e) {
      if (e.target === histModal) closeHistory();
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && histModal && !histModal.classList.contains("hidden")) closeHistory();
  });

  function renderHistory(items) {
    if (!histList) return;
    histList.innerHTML = "";
    if (!items || !items.length) {
      var li = document.createElement("li");
      li.className = "uh-empty";
      li.textContent = "No uploaded files yet.";
      histList.appendChild(li);
      return;
    }
    items.forEach(function (it) {
      var li = document.createElement("li");
      li.title = it.path;
      var name = document.createElement("span");
      name.className = "uh-name";
      name.textContent = it.name;
      var meta = document.createElement("span");
      meta.className = "uh-meta";
      var when = new Date(it.mtime * 1000);
      meta.textContent = fmtBytes(it.size) + " · " + when.toLocaleString();
      var del = document.createElement("button");
      del.type = "button";
      del.className = "uh-del";
      del.title = "Delete this file";
      del.setAttribute("aria-label", "Delete " + it.name);
      del.innerHTML =
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/>' +
        '<path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13"/>' +
        '<path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>';
      del.addEventListener("click", function (e) {
        e.stopPropagation();
        deleteOne(it);
      });
      li.appendChild(name);
      li.appendChild(meta);
      li.appendChild(del);
      li.addEventListener("click", function () {
        if (histTargetInput) {
          histTargetInput.value = it.path;
          histTargetInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
        if (histTargetStatus) {
          histTargetStatus.classList.remove("is-error");
          histTargetStatus.classList.add("is-success");
          histTargetStatus.textContent = "Selected " + it.name + " (" + fmtBytes(it.size) + ")";
        }
        closeHistory();
      });
      histList.appendChild(li);
    });
  }

  function deleteOne(it) {
    showConfirm(
      "Delete \"" + it.name + "\" (" + fmtBytes(it.size) + ") from the upload directory?",
      { okLabel: "Delete" }
    ).then(function (ok) {
      if (!ok) return;
      histStatus.classList.remove("is-error", "is-success");
      histStatus.textContent = "Deleting " + it.name + "…";
      fetch("/api/uploads/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: it.name })
      })
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp && resp.ok) {
            histStatus.classList.add("is-success");
            histStatus.textContent = "Deleted " + resp.name + " (" + fmtBytes(resp.freed || 0) + ")";
            refreshHistory();
          } else {
            histStatus.classList.add("is-error");
            histStatus.textContent = "Delete failed: " + ((resp && resp.error) || "unknown error");
          }
        })
        .catch(function (err) {
          histStatus.classList.add("is-error");
          histStatus.textContent = "Request failed: " + err;
        });
    });
  }

  function refreshHistory() {
    if (!histStatus) return;
    histStatus.classList.remove("is-error", "is-success");
    histStatus.textContent = "Loading…";
    fetch("/api/uploads")
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (resp && resp.ok) {
          histStatus.textContent = (resp.items || []).length
            ? "Click a file to select it."
            : "";
          renderHistory(resp.items);
          if (histFree) {
            if (typeof resp.free === "number") {
              var txt = "Free: " + fmtBytes(resp.free);
              if (typeof resp.total === "number" && resp.total > 0) {
                txt += " of " + fmtBytes(resp.total);
              }
              histFree.textContent = txt;
            } else {
              histFree.textContent = "";
            }
          }
        } else {
          histStatus.classList.add("is-error");
          histStatus.textContent = "Failed to list uploads: " + ((resp && resp.error) || "unknown error");
        }
      })
      .catch(function (err) {
        histStatus.classList.add("is-error");
        histStatus.textContent = "Request failed: " + err;
      });
  }

  function openHistory(input, status) {
    histTargetInput = input;
    histTargetStatus = status;
    if (histModal) histModal.classList.remove("hidden");
    refreshHistory();
  }

  if (histClear) {
    histClear.addEventListener("click", async function () {
      var ok = await showConfirm(
        "Delete every uploaded file from /var/lib/rave-menu/uploads? This frees disk space and cannot be undone.",
        { okLabel: "Clear all" }
      );
      if (!ok) return;
      histStatus.classList.remove("is-error", "is-success");
      histStatus.textContent = "Clearing…";
      fetch("/api/uploads/clear", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp && resp.ok) {
            histStatus.classList.add("is-success");
            histStatus.textContent =
              "Removed " + resp.removed + " file(s), freed " + fmtBytes(resp.freed || 0);
            renderHistory([]);
            refreshHistory();
          } else {
            histStatus.classList.add("is-error");
            histStatus.textContent = "Clear failed: " + ((resp && resp.error) || "unknown error");
          }
        })
        .catch(function (err) {
          histStatus.classList.add("is-error");
          histStatus.textContent = "Request failed: " + err;
        });
    });
  }
  // ── Storage: UFS LUN layout ───────────────────────────────────────────
  var lunScanBtn = document.getElementById("labLunScan");
  var lunResult = document.getElementById("labLunResult");
  var lunBar = document.getElementById("labLunBar");
  var lunRows = document.getElementById("labLunRows");
  var lunSummary = document.getElementById("labLunSummary");
  var lunEditor = document.getElementById("labLunEditor");
  var lunPlanBar = document.getElementById("labLunPlanBar");
  var lunFree = document.getElementById("labLunFree");
  var lunResetBtn = document.getElementById("labLunReset");
  var lunApplyBtn = document.getElementById("labLunApply");
  var lunRestoreBtn = document.getElementById("labLunRestore");
  var lunProgressTimer = null;
  var lunReport = null;

  var lunViewBtns = modal.querySelectorAll(".lab-subtab[data-lun-view]");
  var lunViewPanes = modal.querySelectorAll("[data-lun-view-pane]");

  function lunShowView(name) {
    lunViewBtns.forEach(function (b) {
      var on = b.getAttribute("data-lun-view") === name;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    lunViewPanes.forEach(function (p) {
      p.classList.toggle("hidden", p.getAttribute("data-lun-view-pane") !== name);
    });
  }

  lunViewBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      lunShowView(btn.getAttribute("data-lun-view"));
    });
  });

  /* Two places report progress: the scan controls at the top of the panel and
     the provisioning buttons at the bottom. An operation writes to whichever
     is beside the button that started it, so the user never has to scroll to
     find out what is happening. */
  var LUN_SCAN_UI = {
    status: document.getElementById("labLunStatus"),
    progress: document.getElementById("labLunScanProgress"),
    fill: document.getElementById("labLunScanProgressFill")
  };
  var LUN_OP_UI = {
    status: document.getElementById("labLunOpStatus"),
    progress: document.getElementById("labLunOpProgress"),
    fill: document.getElementById("labLunOpProgressFill")
  };
  var lunUi = LUN_SCAN_UI;

  /* Overwrite is offered whenever the plan is locally valid. The target
     validates it again and refuses a bad one without writing, so a separate
     check step would only cost a second board reset. */
  var lunPlanValid = false;
  var lunPlanSummary = "";

  function lunBusy(on) {
    [lunScanBtn, lunResetBtn, lunRestoreBtn].forEach(function (b) {
      if (b) b.disabled = on;
    });
    if (lunApplyBtn) lunApplyBtn.disabled = on || !lunPlanValid;
  }

  function lunUseUi(ui) {
    // Clear the other pair so one stale message cannot sit above a live one.
    var other = ui === LUN_SCAN_UI ? LUN_OP_UI : LUN_SCAN_UI;
    if (other.status) other.status.textContent = "";
    lunUi = ui;
  }

  function lunSay(message, isError) {
    if (!lunUi.status) return;
    lunUi.status.classList.toggle("is-error", !!isError);
    lunUi.status.classList.toggle("is-success", !isError && !!message);
    lunUi.status.textContent = message || "";
  }

  function lunProgressStart() {
    var ui = lunUi;
    if (!ui.progress || !ui.fill) return;
    var pct = 0;
    ui.progress.classList.remove("hidden");
    ui.progress.setAttribute("aria-hidden", "false");
    ui.fill.style.width = "0%";
    clearInterval(lunProgressTimer);
    // The target gives no progress, so creep towards 90% and let completion
    // finish the bar rather than implying precision we do not have.
    lunProgressTimer = setInterval(function () {
      pct = Math.min(90, pct + 2);
      ui.fill.style.width = pct + "%";
    }, 700);
  }

  function lunProgressStop(ok) {
    var ui = lunUi;
    if (!ui.progress || !ui.fill) return;
    clearInterval(lunProgressTimer);
    ui.fill.style.width = ok ? "100%" : "0%";
    setTimeout(function () {
      ui.progress.classList.add("hidden");
      ui.progress.setAttribute("aria-hidden", "true");
    }, 600);
  }

  function humanBytes(value) {
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return (i === 0 ? value : value.toFixed(2)) + " " + units[i];
  }

  function isReserved(entry) {
    return !!(entry.boot_lun || entry.boot_lun_id);
  }

  /* Boot LUNs are tiny, so they sort last: among full-size rows they read as
     an editing mistake. Their LUN number is still shown, because the index is
     fixed by the device even though the display order is not. */
  function orderedForDisplay(entries) {
    var editable = entries.filter(function (e) { return !isReserved(e); });
    var reserved = entries.filter(isReserved);
    return editable.concat(reserved);
  }

  function bootLabel(entry) {
    return translate("BOOT") + " " + (entry.boot_lun_id === 2 ? "B" : "A");
  }

  function renderLunRows(container, entries) {
    container.innerHTML = "";
    // Disabled LUNs are just absence; listing them is noise.
    var live = orderedForDisplay(entries).filter(function (e) { return e.enabled; });
    live.forEach(function (entry) {
      var row = document.createElement("div");
      row.className = "lab-partrow" + (isReserved(entry) ? " lab-lunrow-reserved" : "");
      var text = "LUN " + entry.lun + "  "
        + humanBytes(entry.capacity_bytes || 0)
        + "  (" + (entry.num_alloc_units || 0) + " AU)"
        + (entry.block_device ? "  /dev/" + entry.block_device : "");
      if (isReserved(entry)) {
        var badge = document.createElement("span");
        badge.className = "lab-lunbadge";
        badge.textContent = bootLabel(entry);
        row.appendChild(badge);
        text += "  " + translate("boot LUN");
      }
      row.appendChild(document.createTextNode(text));
      container.appendChild(row);
    });
    if (lunBar && container === lunRows) {
      drawBar(lunBar, entries);
    }
  }

  function drawBar(target, entries) {
    var shown = orderedForDisplay(entries).filter(function (e) { return e.enabled; });
    target.innerHTML = shown.map(function (e, i) {
      // A 4 MiB boot LUN is ~0.002% of the device, so give it a fixed sliver
      // rather than a segment too thin to see.
      if (isReserved(e)) {
        return '<span class="lab-part lab-part-reserved" title="LUN ' + e.lun + ' — '
             + translate("boot LUN") + '"></span>';
      }
      return '<span class="lab-part lab-part-' + ((i % 4) + 1) + '" style="flex:'
           + (e.capacity_bytes || 1) + '">'
           + '<span class="lab-part-name">LUN ' + e.lun + '</span></span>';
    }).join("");
  }

  function renderLunReport(report) {
    lunReport = report;
    renderLunRows(lunRows, report.luns);
    lunSummary.textContent =
      "UFS " + report.spec_version + "  " + (report.product || "")
      + " — " + translate("allocation unit") + " " + humanBytes(report.geometry.au_bytes)
      + ", " + translate("allocatable") + " " + humanBytes(report.geometry.budget_bytes)
      + (report.config_descr_lock
          ? "  — " + translate("PROVISIONING LOCKED: this device's layout can never be changed")
          : "");
    lunResult.classList.remove("hidden");
    // A fresh scan answers "what is on the board", so start on that view.
    lunShowView("current");
    if (lunRestoreBtn) lunRestoreBtn.disabled = !!report.config_descr_lock;
  }

  function lunRequest(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, data: data };
      });
    });
  }

  // ── Layout editor ─────────────────────────────────────────────────────
  // Everything is held in allocation units, which is the only granularity the
  // hardware has. Working in bytes and converting at the end would let the UI
  // express sizes the device cannot actually provide.
  var lunPlan = [];

  function auTotal() {
    return lunReport ? lunReport.geometry.budget_alloc_units : 0;
  }

  /* The plan assigns the boot LUNs itself, so nothing is held back from the
     budget and every LUN index is available to it. */
  function auBudget() { return auTotal(); }

  function maxBootLuns() {
    return lunReport && lunReport.max_boot_luns ? lunReport.max_boot_luns : 2;
  }

  function bootCount() {
    return lunPlan.filter(function (r) { return r.used && r.boot; }).length;
  }

  /* A LUN keeps the boot slot the device reported, so a board with LUN 1 as B
     and LUN 2 as A stays that way. A newly ticked boot LUN takes the lowest
     slot no other boot LUN holds. */
  function bootSlotId(lun) {
    var claimed = {};
    lunPlan.forEach(function (row) {
      if (row.used && row.boot && row.boot_lun_id) claimed[row.boot_lun_id] = true;
    });
    var next = 1;
    for (var i = 0; i < lunPlan.length; i += 1) {
      var row = lunPlan[i];
      if (!row.used || !row.boot) continue;
      if (row.boot_lun_id) {
        if (row.lun === lun) return row.boot_lun_id;
        continue;
      }
      while (claimed[next]) next += 1;
      claimed[next] = true;
      if (row.lun === lun) return next;
    }
    return 0;
  }

  function auBytes() {
    return lunReport ? lunReport.geometry.au_bytes : 0;
  }

  /* Enhanced memory types cost more raw capacity than they present, by the
     device's capacity adjustment factor, so 3 allocation units of enhanced
     memory show up as 4 MiB rather than 12 MiB. */
  function usableBytes(au, memoryType) {
    var table = (lunReport && lunReport.geometry.cap_adj_fac) || {};
    var factor = table[String(memoryType || 0)] || 256;
    return Math.floor(au * auBytes() * 256 / factor);
  }

  function auUsed(excludeLun) {
    return lunPlan.reduce(function (sum, row) {
      if (!row.used || row.lun === excludeLun || row.rest) return sum;
      return sum + row.au;
    }, 0);
  }

  function auFree(excludeLun) {
    return Math.max(0, auBudget() - auUsed(excludeLun));
  }

  function resetPlan() {
    if (!lunReport) return;
    // One row per LUN the device could have, each carrying that LUN's own
    // descriptor fields. Anything not edited here is sent back unchanged, so
    // applying with no edits leaves the device exactly as it was.
    var byLun = {};
    lunReport.luns.forEach(function (e) { byLun[e.lun] = e; });
    lunPlan = [];
    for (var lun = 0; lun < lunReport.geometry.max_number_lu; lun += 1) {
      var e = byLun[lun] || {};
      lunPlan.push({
        lun: lun,
        used: !!e.enabled,
        au: e.num_alloc_units || 0,
        rest: false,
        boot: !!e.boot_lun_id,
        boot_lun_id: e.boot_lun_id || 0,
        memory_type: e.memory_type || 0,
        data_reliability: e.data_reliability || 0,
        write_protect: e.write_protect || 0,
        logical_block_size_exp: e.logical_block_size_exp || 0,
        provisioning_type: e.provisioning_type || 0
      });
    }
    // LUN 0 is the rootfs target, so there is always at least one LUN.
    if (!lunPlan.some(function (r) { return r.used; })) {
      lunPlan[0].used = true;
      lunPlan[0].au = auBudget();
    }
    renderEditor();
  }

  function usedRows() {
    return lunPlan.filter(function (r) { return r.used; });
  }

  /* The request body for /api/versal/luns/apply: one object per LUN in use,
     carrying everything needed to leave untouched LUNs untouched. */
  function planLuns() {
    return usedRows().map(function (row) {
      return {
        lun: row.lun,
        size: row.rest ? "rest" : row.au * auBytes(),
        boot_lun_id: row.boot ? bootSlotId(row.lun) : 0,
        memory_type: row.memory_type,
        data_reliability: row.data_reliability,
        write_protect: row.write_protect,
        logical_block_size_exp: row.logical_block_size_exp,
        provisioning_type: row.provisioning_type
      };
    });
  }

  function renderEditor() {
    if (!lunEditor || !lunReport) return;
    lunEditor.innerHTML = "";

    // One row per LUN index, each keeping its own number. LUNs may be used in
    // any combination, so a device with a gap keeps its gap.
    lunPlan.forEach(function (row) {
      var lunNo = row.lun;
      var used = row.used;
      var el = document.createElement("div");
      el.className = "lab-lunrow" + (used ? "" : " lab-lunrow-unused");

      var useWrap = document.createElement("label");
      useWrap.className = "lab-lunuse";
      var use = document.createElement("input");
      use.type = "checkbox";
      use.checked = used;
      // LUN 0 holds the rootfs and is what Flash Program writes.
      use.disabled = lunNo === 0;
      use.addEventListener("change", function () {
        if (use.checked) {
          var free = auFree(null);
          if (free < 1) {
            var largest = null;
            usedRows().forEach(function (r) {
              if (!r.rest && (!largest || r.au > largest.au)) largest = r;
            });
            if (!largest || largest.au < 2) { renderEditor(); return; }
            free = Math.floor(largest.au / 2);
            largest.au -= free;
          }
          row.used = true;
          row.au = free;
        } else {
          row.used = false;
          row.au = 0;
          row.rest = false;
          row.boot = false;
        }
        renderEditor();
      });
      useWrap.appendChild(use);
      useWrap.appendChild(document.createTextNode(translate("used")));

      var label = document.createElement("span");
      label.className = "lab-lunlabel";
      label.textContent = "LUN " + lunNo;

      el.appendChild(label);
      el.appendChild(useWrap);

      var slider = document.createElement("input");
      slider.type = "range";
      slider.className = "lab-lunslider";
      slider.min = 1;
      slider.max = Math.max(1, used ? auFree(lunNo) : 1);
      slider.value = used ? Math.min(row.au, slider.max) : 1;
      slider.disabled = !used || row.rest;

      var size = document.createElement("input");
      size.type = "number";
      size.className = "lab-lunsize";
      size.min = 0;
      size.step = 0.01;
      size.disabled = !used || row.rest;

      function showBytes(au) {
        size.value = (au * auBytes() / (1024 * 1024 * 1024)).toFixed(2);
      }
      showBytes(!used ? 0 : (row.rest ? auFree(lunNo) : row.au));

      if (used) {
        slider.addEventListener("input", function () {
          row.au = parseInt(slider.value, 10) || 1;
          showBytes(row.au);
          refreshTotals();
        });
        slider.addEventListener("change", renderEditor);
        size.addEventListener("change", function () {
          var wanted = Math.round((parseFloat(size.value) || 0) * 1024 * 1024 * 1024 / auBytes());
          row.au = Math.max(1, Math.min(wanted, auFree(lunNo)));
          renderEditor();
        });
      }

      el.appendChild(slider);
      el.appendChild(size);

      var unit = document.createElement("span");
      unit.className = "lab-lununit";
      unit.textContent = "GiB";
      el.appendChild(unit);

      var restWrap = document.createElement("label");
      restWrap.className = "lab-lunrest";
      var rest = document.createElement("input");
      rest.type = "checkbox";
      rest.checked = used && row.rest;
      rest.disabled = !used;
      rest.addEventListener("change", function () {
        // Only one LUN can absorb the remainder, so selecting one clears the rest.
        lunPlan.forEach(function (other) { other.rest = false; });
        row.rest = rest.checked;
        if (row.rest) row.au = auFree(lunNo);
        renderEditor();
      });
      restWrap.appendChild(rest);
      restWrap.appendChild(document.createTextNode(translate("remainder")));
      el.appendChild(restWrap);

      // Only two boot LUNs exist in UFS, so once both are taken the remaining
      // boxes go dead rather than letting the user build a plan the target
      // would reject.
      var bootWrap = document.createElement("label");
      bootWrap.className = "lab-lunboot";
      var boot = document.createElement("input");
      boot.type = "checkbox";
      boot.checked = used && !!row.boot;
      boot.disabled = !used || (!row.boot && bootCount() >= maxBootLuns());
      boot.title = translate("Mark this LUN as a UFS boot LUN. Optional: the "
                            + "Versal boots from OSPI and does not use them.");
      boot.addEventListener("change", function () {
        row.boot = boot.checked;
        renderEditor();
      });
      bootWrap.appendChild(boot);
      bootWrap.appendChild(document.createTextNode(
        used && row.boot ? bootLabel({ boot_lun_id: bootSlotId(lunNo) })
                         : translate("boot")));
      el.appendChild(bootWrap);

      lunEditor.appendChild(el);
    });

    refreshTotals();
  }

  function refreshTotals() {
    if (!lunReport) return;
    var live = usedRows();
    var restRow = live.some(function (r) { return r.rest; });
    var used = auUsed(null);
    var free = Math.max(0, auBudget() - used);
    // A remainder LUN with nothing left to take is not a valid plan, and the
    // target would reject it; say so here rather than after a board reset.
    var starvedRest = restRow && free === 0;
    var over = used > auBudget();

    if (lunFree) {
      lunFree.textContent = starvedRest
        ? translate("nothing left for the remainder LUN")
        : over
          ? translate("over capacity by ") + humanBytes((used - auBudget()) * auBytes())
          : restRow
            ? translate("remainder assigned") + " — " + humanBytes(free * auBytes())
            : humanBytes(free * auBytes()) + " " + translate("unallocated");
      lunFree.classList.toggle("is-over", over || starvedRest);
    }
    var preview = live.map(function (row) {
      var au = row.rest ? free : row.au;
      // The bar shows the memory type too, since enhanced LUNs present less
      // than their allocation units suggest.
      return { lun: row.lun, enabled: true,
               boot_lun_id: row.boot ? bootSlotId(row.lun) : 0,
               capacity_bytes: usableBytes(au, row.memory_type),
               num_alloc_units: au };
    });
    if (lunPlanBar) drawBar(lunPlanBar, preview);

    lunPlanSummary = preview
      .map(function (e) {
        return "LUN " + e.lun + ": " + humanBytes(e.capacity_bytes)
             + (e.boot_lun_id ? "  (" + bootLabel(e) + ")" : "");
      })
      .join("\n");

    lunPlanValid = live.length > 0 && !over && !starvedRest
                   && bootCount() <= maxBootLuns()
                   && !lunReport.config_descr_lock;
    if (lunApplyBtn) lunApplyBtn.disabled = !lunPlanValid;
  }

  if (lunResetBtn) lunResetBtn.addEventListener("click", resetPlan);

  if (lunScanBtn) {
    lunScanBtn.addEventListener("click", function () {
      lunUseUi(LUN_SCAN_UI);
      lunBusy(true);
      lunSay(translate("Reading the LUN layout from the target…"));
      lunProgressStart();
      lunRequest("/api/versal/luns/scan")
        .then(function (result) {
          if (!result.ok || !result.data.ok) {
            lunProgressStop(false);
            lunSay(result.data.error || translate("Could not read the LUN layout."), true);
            return;
          }
          lunProgressStop(true);
          renderLunReport(result.data.report);
          resetPlan();
          lunSay(translate("Target read. Nothing has been changed."));
        })
        .catch(function (err) {
          lunProgressStop(false);
          lunSay(translate("LUN scan failed: ") + err, true);
        })
        .then(function () { lunBusy(false); });
    });
  }

  function lunCommit(body, message) {
    return showConfirm(message, { okLabel: translate("Erase and re-provision") })
      .then(function (ok) {
        if (!ok) return;
        lunUseUi(LUN_OP_UI);
        lunBusy(true);
        lunSay(translate("Re-provisioning the UFS device…"));
        lunProgressStart();
        return lunRequest("/api/versal/luns/apply", body)
          .then(function (result) {
            lunProgressStop(result.ok);
            if (!result.ok || !result.data.ok) {
              lunSay(result.data.error || translate("Re-provisioning failed."), true);
              return;
            }
            renderLunReport(result.data.preview);
            resetPlan();
            lunSay(translate("UFS re-provisioned. The Versal will not boot until "
                             + "an image is programmed from the Flash Program tab."));
          })
          .catch(function (err) {
            lunProgressStop(false);
            lunSay(translate("Re-provisioning failed: ") + err, true);
          })
          .then(function () { lunBusy(false); });
      });
  }

  if (lunApplyBtn) {
    lunApplyBtn.addEventListener("click", function () {
      if (!lunPlanValid) return;
      lunCommit(
        { luns: planLuns(), confirm: true },
        translate("This re-provisions the Versal's UFS device. All data on it, "
                  + "including the root filesystem, becomes unrecoverable. The "
                  + "board will not boot afterwards until you program an image.")
        + "\n\n" + translate("New layout:") + "\n" + lunPlanSummary
      );
    });
  }

  if (lunRestoreBtn) {
    lunRestoreBtn.addEventListener("click", function () {
      lunCommit(
        { restore_baseline: true, confirm: true },
        translate("This restores the factory LUN layout for this device. All "
                  + "data on the UFS device becomes unrecoverable. The board "
                  + "will not boot afterwards until you program an image.")
      );
    });
  }
})();
