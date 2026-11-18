/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* App Store page logic (Phase 5).
 *
 * Fetches the catalog from /api/apps, renders one card per app, and drives
 * installs through the SAME multi-tab terminal modal used for Versal
 * programming (static/terminal.js::runAction -> /api/run/install-app ->
 * /api/stream/<job_id>). Also handles "install from URL" and the installed-app
 * / Versal-recovery surface. Manual installs accept URLs, local/USB paths,
 * and multi-gigabyte browser uploads streamed through /api/upload-payload.
 *
 * Security notes: the client never trusts itself. URLs are re-validated
 * server-side (routes_app_store.py); the sha256 gate is enforced by the
 * installer. install buttons forward only catalog-resolved {url, sha256} (or a
 * user-entered URL) as argv -- never free text into a shell.
 */
(function () {
  "use strict";

  var catalogEl = document.getElementById("appCatalog");
  var statusEl = document.getElementById("catalogStatus");
  var installedSection = document.getElementById("installedSection");
  var installedName = document.getElementById("installedName");
  var installedVersion = document.getElementById("installedVersion");
  var installedJumboBadge = document.getElementById("installedJumboBadge");
  var recoverBtn = document.getElementById("recoverVersalBtn");

  var jobRunning = false;
  var installedId = "";
  var storeCount = 1;

  /* ---- small helpers ---- */

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function fmtSize(bytes) {
    if (!bytes || bytes < 0) return "";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + units[i];
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

  /* Ask the device what a bundle is before installing it. The catalog's jumbo
   * fields are only a copy, and a manual URL install has no catalog, so the
   * descriptor at the head of the bundle is the authority. */
  async function inspectBundle(source) {
    try {
      var resp = await fetch("/api/apps/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: source })
      });
      var data = await resp.json().catch(function () { return {}; });
      return resp.ok ? data : null;
    } catch (e) {
      return null;
    }
  }

  /* Size dialog for a jumbo app -> Promise<number|null> (GiB, null = cancel).
   *
   * The bundle names a floor it cannot run below and the size it would like;
   * the device knows how much tail there is. The user picks anywhere between
   * the floor and the whole tail, and whatever is left stays free for an
   * Ubuntu install later. */
  function showJumboSize(info, title) {
    var minGib = info.minimum_storage || 0;
    var desiredGib = info.desired_storage || minGib;
    var availGib = Math.floor((info.available_mib || 0) / 1024);
    var maxGib = Math.max(availGib, minGib);
    var startGib = Math.min(desiredGib, maxGib);
    var ubuntuMinGib = info.ubuntu_min_gib || 0;
    var ubuntuInstalled = !!info.ubuntu_installed;
    /* The descriptor names the app, so a manual install has a real name too. */
    var appName = info.name || title;
    var BAR_MET = "linear-gradient(180deg, #4fb96a, #2b7a43)";
    var BAR_SHORT = "linear-gradient(180deg, #e9723d, #a4451e)";

    return new Promise(function (resolve) {
      var modal = document.getElementById("jumboModal");
      var input = document.getElementById("jumboSize");
      var note = document.getElementById("jumboNote");
      var warn = document.getElementById("jumboWarn");
      var bar = document.getElementById("jumboBarApp");
      var leftover = document.getElementById("jumboBarFree");
      var titleEl = document.getElementById("jumboTitle");
      var okBtn = document.getElementById("jumboOk");
      var cancelBtn = document.getElementById("jumboCancel");
      if (!modal || !input || !okBtn || !cancelBtn) { resolve(startGib); return; }

      if (titleEl) titleEl.textContent = "Jumbo Bundle Storage Allocation";
      input.min = String(minGib);
      input.max = String(maxGib);
      input.value = String(startGib);
      var ubuntuLine = "";
      if (ubuntuMinGib) {
        ubuntuLine = ubuntuInstalled
          ? " Ubuntu is already installed and keeps its own space."
          : " Ubuntu needs " + ubuntuMinGib +
            " GiB of what is left over, so leave at least that much to keep it installable.";
      }
      note.textContent =
        appName + " needs at least " + minGib + " GiB and asks for " +
        desiredGib + " GiB. This device has " + availGib +
        " GiB available after rave-data." + ubuntuLine;

      function sync() {
        var v = parseInt(input.value, 10);
        var valid = !isNaN(v) && v >= minGib && v <= maxGib;
        okBtn.disabled = !valid;
        var rest = valid ? maxGib - v : 0;
        if (!valid) {
          warn.textContent = "Choose between " + minGib + " and " + maxGib + " GiB.";
          warn.style.color = "#ffb4b4";
        } else if (ubuntuMinGib && !ubuntuInstalled && rest < ubuntuMinGib) {
          /* Allowed, but it is a one-way door for Ubuntu: say so plainly. */
          warn.textContent =
            "\u26a0  Ubuntu could NOT be installed afterwards. It needs " +
            ubuntuMinGib + " GiB and only " + rest +
            " GiB would remain. Reduce this to " + (maxGib - ubuntuMinGib) +
            " GiB or less to keep Ubuntu available.";
          warn.style.color = "#ffcf8b";
        } else {
          warn.textContent = "";
        }
        warn.style.display = warn.textContent ? "block" : "none";
        if (valid && bar && leftover) {
          /* Green once the app has the size it asked for, orange while it is
           * running on less than that. */
          bar.style.background = v >= desiredGib ? BAR_MET : BAR_SHORT;
          bar.style.flex = String(v);
          bar.textContent = v + " GiB app";
          leftover.style.flex = String(rest);
          leftover.textContent = rest > 0 ? rest + " GiB free" : "";
        }
      }
      sync();
      modal.classList.remove("hidden");
      requestAnimationFrame(function () { input.focus(); });

      function cleanup(result) {
        modal.classList.add("hidden");
        input.removeEventListener("input", sync);
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
        resolve(result);
      }
      function onOk() { if (!okBtn.disabled) cleanup(parseInt(input.value, 10)); }
      function onCancel() { cleanup(null); }
      function onKey(e) {
        if (e.key === "Escape") cleanup(null);
        else if (e.key === "Enter") onOk();
      }
      input.addEventListener("input", sync);
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
    });
  }

  /* Resolve the extra install arguments a jumbo bundle needs, asking the user
   * for a size. Returns null if they cancelled, [] for an ordinary app. */
  async function jumboArgs(source, title) {
    var info = await inspectBundle(source);
    if (!info || info.type !== "jumbo") return [];
    if (!info.minimum_storage) return [];
    var availMib = info.available_mib || 0;
    if (availMib < info.minimum_storage * 1024) {
      window.alert(
        "\u201c" + (info.name || title) + "\u201d needs at least " + info.minimum_storage +
        " GiB of application storage, but this device has only " +
        Math.floor(availMib / 1024) + " GiB available.");
      return null;
    }
    var size = await showJumboSize(info, title);
    if (size === null) return null;
    return ["--jumbo-size-gib", String(size)];
  }

  /* Disable/enable every install affordance while a job runs (single-install
   * model -- matches the single JOBS slot and destructive slot writes). */
  function setJobRunning(running) {
    jobRunning = running;
    var btns = document.querySelectorAll(".app-flash");
    Array.prototype.forEach.call(btns, function (b) {
      if (b.dataset.alwaysOn === "1") return;
      b.disabled = running;
    });
  }

  /* Kick off an install-app job in the shared terminal modal. */
  function startInstall(title, args) {
    if (jobRunning) return;
    setJobRunning(true);
    /* runAction (terminal.js) opens the modal, POSTs /api/run/install-app and
     * streams the =-=-=-= stage tabs. It manages its own close button. */
    runAction("install-app", title, true, args);
    /* Re-enable when the modal is dismissed; refresh installed state too. */
    watchTermModal();
  }

  /* When the terminal modal closes, re-enable buttons + refresh installed app. */
  function watchTermModal() {
    var modal = document.getElementById("termModal");
    if (!modal) { setJobRunning(false); return; }
    var obs = new MutationObserver(function () {
      if (modal.classList.contains("hidden")) {
        obs.disconnect();
        setJobRunning(false);
        loadInstalled();
      }
    });
    obs.observe(modal, { attributes: true, attributeFilter: ["class"] });
  }

  /* ---- catalog rendering ---- */

  function makeCard(app) {
    var card = el("article", "app-card");
    /* Tint the card so you can tell which store delivered the app. The colour
     * is only meaningful when more than one store is configured. */
    if (storeCount > 1 && app.store_color) {
      card.classList.add("app-card-sourced");
      card.style.setProperty("--store-color", app.store_color);
    }

    var thumb = el("div", "app-thumb app-thumb-catalog");
    if (app.thumbnail) {
      var img = el("img", "app-thumb-img");
      img.src = app.thumbnail;
      img.alt = "";
      img.onerror = function () { this.style.display = "none"; };
      thumb.appendChild(img);
    }
    card.appendChild(thumb);

    var copy = el("div", "app-copy");
    copy.appendChild(el("h3", null, app.title || app.id));

    var badges = el("div", "app-badges");
    if (app.id && app.id === installedId) {
      badges.appendChild(el("span", "app-badge badge-installed", "Installed"));
    }
    if (app.has_versal) badges.appendChild(el("span", "app-badge badge-versal", "Versal"));
    if (app.jumbo) {
      badges.appendChild(el("span", "app-badge badge-jumbo",
        app.minimum_storage ? "Jumbo · " + app.minimum_storage + "+ GiB" : "Jumbo"));
    }
    if (app.experimental) badges.appendChild(el("span", "app-badge badge-exp", "Experimental"));
    if (badges.childNodes.length) copy.appendChild(badges);

    if (app.short_description) copy.appendChild(el("p", null, app.short_description));

    var meta = [];
    if (app.vendor) meta.push(app.vendor);
    if (app.version) meta.push("v" + app.version);
    var sz = fmtSize(app.size_bytes);
    if (sz) meta.push(sz);
    if (meta.length) copy.appendChild(el("p", "app-meta", meta.join("  \u00b7  ")));

    card.appendChild(copy);

    var isInstalled = app.id && app.id === installedId;
    var btn = el("button", "app-flash", isInstalled ? "Reinstall" : "Install");
    btn.type = "button";
    btn.addEventListener("click", function () { onInstallCatalog(app, isInstalled); });
    card.appendChild(btn);

    return card;
  }

  async function onInstallCatalog(app, isInstalled) {
    if (jobRunning) return;
    if (!app.url) { window.alert("This app has no download URL."); return; }

    var lines = [
      (isInstalled ? "Reinstall" : "Install") + " \u201c" + (app.title || app.id) + "\u201d?",
      "",
      "This writes the application slot" +
        (app.has_versal ? " and reprograms the Versal device." : "."),
      "Any currently installed application will be overwritten."
    ];
    var ok = await showConfirm(lines.join("\n"), {
      title: isInstalled ? "Reinstall application" : "Install application",
      okLabel: isInstalled ? "Reinstall" : "Install"
    });
    if (!ok) return;

    var args = [app.url];
    if (app.sha256) args.push("--sha256", app.sha256);
    var extra = await jumboArgs(app.url, app.title || app.id);
    if (extra === null) return;
    startInstall((isInstalled ? "Reinstall " : "Install ") + (app.title || app.id),
                 args.concat(extra));
  }

  function renderCatalog(apps) {
    catalogEl.textContent = "";
    if (!apps || !apps.length) {
      statusEl.textContent = "No applications are available right now.";
      statusEl.classList.remove("hidden");
      return;
    }
    statusEl.classList.add("hidden");
    apps.forEach(function (app) { catalogEl.appendChild(makeCard(app)); });
  }

  async function loadCatalog() {
    statusEl.textContent = "Loading apps\u2026";
    statusEl.classList.remove("hidden");
    try {
      var resp = await fetch("/api/apps");
      var data = await resp.json().catch(function () { return {}; });
      applyInstalled(data.installed);
      renderStores(data.stores);
      if (!resp.ok || data.ok === false) {
        statusEl.textContent = data.status === "offline"
          ? "App catalog is offline. You can still install from a URL below."
          : (data.error || "Could not load the app catalog.");
        statusEl.classList.remove("hidden");
        catalogEl.textContent = "";
        return;
      }
      renderCatalog(data.apps || []);
      if (data.status === "partial") {
        var down = (data.stores || []).filter(function (s) { return !s.ok; });
        if (down.length) {
          statusEl.textContent = "Some stores are offline: " +
            down.map(function (s) { return s.label; }).join(", ");
          statusEl.classList.remove("hidden");
        }
      }
    } catch (err) {
      statusEl.textContent = "Could not reach the app catalog: " + err.message;
      statusEl.classList.remove("hidden");
    }
  }

  /* ---- installed app + recovery ---- */

  function applyInstalled(installed) {
    if (installed && installed.installed) {
      installedId = installed.id || "";
      installedName.textContent = installed.name || installed.id || "Application";
      installedVersion.textContent = installed.version ? ("v" + installed.version) : "";
      installedSection.classList.remove("hidden");
      if (installedJumboBadge) {
        installedJumboBadge.textContent = installed.jumbo_gib
          ? ("Jumbo \u00b7 " + installed.jumbo_gib + " GiB") : "Jumbo";
        installedJumboBadge.classList.toggle("hidden", !installed.jumbo);
      }
      if (installed.has_versal) {
        recoverBtn.classList.remove("hidden");
      } else {
        recoverBtn.classList.add("hidden");
      }
    } else {
      installedId = "";
      installedSection.classList.add("hidden");
      recoverBtn.classList.add("hidden");
      if (installedJumboBadge) installedJumboBadge.classList.add("hidden");
    }
  }

  async function loadInstalled() {
    try {
      var resp = await fetch("/api/apps/installed");
      var data = await resp.json().catch(function () { return {}; });
      applyInstalled(data);
    } catch (err) { /* leave previous state */ }
  }

  async function onRecover() {
    if (jobRunning) return;
    var ok = await showConfirm(
      "Reprogram the Versal device from the on-disk images?\n\n" +
      "This re-flashes the Versal side only and leaves the x86 application " +
      "untouched.",
      { title: "Reprogram Versal", okLabel: "Reprogram" });
    if (!ok) return;
    /* The installer's --recovery path reprograms Versal from the persisted
     * images (Phase 6 backend refines this; the button is wired here). */
    startInstall("Reprogram Versal", ["--recovery"]);
  }

  /* ---- manual install source picker ---- */

  function isHttpUrl(s) {
    return /^https?:\/\/\S+$/i.test((s || "").trim());
  }

  function isBundleSource(s) {
    s = (s || "").trim();
    return isHttpUrl(s) || s.charAt(0) === "/";
  }

  function uploadFile(row, input, status, file) {
    if (!file || typeof file.size !== "number" || file.size === 0) {
      status.className = "app-upload-status is-error";
      status.textContent = "Select a non-empty regular file.";
      return;
    }
    row.classList.add("is-uploading");
    status.className = "app-upload-status";
    status.innerHTML = "Uploading <strong></strong> — <span class=\"pct\">0%</span> of " +
      fmtSize(file.size) + "<span class=\"upload-bar\"><span></span></span>";
    status.querySelector("strong").textContent = file.name;
    var pct = status.querySelector(".pct");
    var bar = status.querySelector(".upload-bar > span");
    var loaded = 0;
    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload-payload");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = function (event) {
      loaded = event.loaded;
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
        status.className = "app-upload-status is-success";
        status.textContent = "Uploaded " + data.name + " (" + fmtSize(data.size) + ")";
      } else {
        status.className = "app-upload-status is-error";
        status.textContent = "Upload failed: " + (data.error || ("HTTP " + xhr.status));
      }
    };
    xhr.onerror = function () {
      row.classList.remove("is-uploading");
      status.className = "app-upload-status is-error";
      status.textContent = "Upload failed after " + fmtSize(loaded) + ": connection lost";
    };
    xhr.onabort = function () {
      row.classList.remove("is-uploading");
      status.className = "app-upload-status is-error";
      status.textContent = "Upload aborted after " + fmtSize(loaded);
    };
    xhr.send(file);
  }

  function wireManualInstall() {
    var disclosure = document.getElementById("urlDisclosure");
    var form = document.getElementById("installUrlForm");
    var input = document.getElementById("urlInput");
    var btn = document.getElementById("installUrlBtn");
    var row = form && form.querySelector(".app-source-row");
    var status = document.getElementById("appUploadStatus");
    if (!disclosure || !form || !input || !btn || !row || !status) return;

    disclosure.addEventListener("click", function () {
      var open = form.classList.toggle("hidden") === false;
      disclosure.setAttribute("aria-expanded", open ? "true" : "false");
    });

    function refresh() {
      btn.disabled = jobRunning || !isBundleSource(input.value);
    }
    input.addEventListener("input", refresh);

    var picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".rave2app,application/octet-stream";
    picker.hidden = true;
    row.appendChild(picker);
    row.querySelector(".upload-btn").addEventListener("click", function () { picker.click(); });
    picker.addEventListener("change", function () {
      if (picker.files && picker.files.length) uploadFile(row, input, status, picker.files[0]);
      picker.value = "";
    });
    ["dragenter", "dragover"].forEach(function (name) {
      row.addEventListener(name, function (event) {
        event.preventDefault();
        row.classList.add("is-drop-hover");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      row.addEventListener(name, function (event) {
        event.preventDefault();
        row.classList.remove("is-drop-hover");
      });
    });
    row.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) uploadFile(row, input, status, files[0]);
    });
    row.querySelector(".history-btn").addEventListener("click", function () {
      openUploadHistory(input, status);
    });

    btn.addEventListener("click", async function () {
      if (jobRunning) return;
      var source = input.value.trim();
      if (!isBundleSource(source)) {
        window.alert("Enter an http(s) URL or an absolute local path.");
        return;
      }

      var msg = "Install a bundle from:\n" + source + "\n\n" +
        (isHttpUrl(source) ? "No checksum was provided — only install bundles you trust.\n\n" : "") +
        "This " +
        "writes/erases the application slot and may reprogram Versal.";
      var ok = await showConfirm(msg, {
        title: "Manual install", okLabel: "Install"
      });
      if (!ok) return;

      var extra = await jumboArgs(source, "This bundle");
      if (extra === null) return;
      startInstall("Install application bundle", [source].concat(extra));
    });
  }

  var historyModal = document.getElementById("uploadHistoryModal");
  var historyList = document.getElementById("uploadHistoryList");
  var historyStatus = document.getElementById("uploadHistoryStatus");
  var historyFree = document.getElementById("uploadHistoryFree");
  var historyInput = null;
  var historyInputStatus = null;

  function closeUploadHistory() { historyModal.classList.add("hidden"); }

  function deleteUpload(name) {
    fetch("/api/uploads/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function (response) { return response.json(); }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "delete failed");
      refreshUploadHistory();
    }).catch(function (error) {
      historyStatus.className = "upload-history-status is-error";
      historyStatus.textContent = "Delete failed: " + error.message;
    });
  }

  function renderUploadHistory(items) {
    historyList.textContent = "";
    if (!items.length) {
      var empty = el("li", "uh-empty", "No uploaded files yet.");
      historyList.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      var entry = el("li");
      entry.title = item.path;
      entry.appendChild(el("span", "uh-name", item.name));
      entry.appendChild(el("span", "uh-meta", fmtSize(item.size)));
      var remove = el("button", "uh-del", "×");
      remove.type = "button";
      remove.title = "Delete " + item.name;
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteUpload(item.name);
      });
      entry.appendChild(remove);
      entry.addEventListener("click", function () {
        historyInput.value = item.path;
        historyInput.dispatchEvent(new Event("input", { bubbles: true }));
        historyInputStatus.className = "app-upload-status is-success";
        historyInputStatus.textContent = "Selected " + item.name + " (" + fmtSize(item.size) + ")";
        closeUploadHistory();
      });
      historyList.appendChild(entry);
    });
  }

  function refreshUploadHistory() {
    historyStatus.className = "upload-history-status";
    historyStatus.textContent = "Loading…";
    fetch("/api/uploads").then(function (response) { return response.json(); }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "list failed");
      historyStatus.textContent = data.items.length ? "Select a file to install." : "";
      historyFree.textContent = typeof data.free === "number" ? "Free: " + fmtSize(data.free) : "";
      renderUploadHistory(data.items || []);
    }).catch(function (error) {
      historyStatus.className = "upload-history-status is-error";
      historyStatus.textContent = "Could not list uploads: " + error.message;
    });
  }

  function openUploadHistory(input, status) {
    historyInput = input;
    historyInputStatus = status;
    historyModal.classList.remove("hidden");
    refreshUploadHistory();
  }

  function wireUploadHistory() {
    if (!historyModal) return;
    document.getElementById("uploadHistoryClose").addEventListener("click", closeUploadHistory);
    historyModal.addEventListener("click", function (event) {
      if (event.target === historyModal) closeUploadHistory();
    });
    document.getElementById("uploadHistoryClear").addEventListener("click", async function () {
      var ok = await showConfirm("Delete every uploaded file?", {
        title: "Clear upload history", okLabel: "Delete All"
      });
      if (!ok) return;
      fetch("/api/uploads/clear", { method: "POST" })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || "clear failed");
          refreshUploadHistory();
        }).catch(function (error) {
          historyStatus.className = "upload-history-status is-error";
          historyStatus.textContent = "Clear failed: " + error.message;
        });
    });
  }

  /* ---- additional app stores ---- */

  var storesListEl = document.getElementById("storesList");

  /* Render the configured stores (built-in + user-added). Accepts the status
   * objects from /api/apps (with ok/error/count) or the bare list from
   * /api/apps/stores. */
  function renderStores(stores) {
    if (!storesListEl) return;
    if (!Array.isArray(stores) || !stores.length) return;
    storeCount = stores.length;
    storesListEl.textContent = "";

    stores.forEach(function (s) {
      var row = el("li", "store-row");
      /* Colour-code the row to match the accent used on this store's app
       * cards, so the two surfaces read as the same source. */
      if (s.color) row.style.setProperty("--store-color", s.color);

      var main = el("div", "store-main");
      main.appendChild(el("span", "store-name", s.label || s.root));
      main.appendChild(el("span", "store-url", s.root));
      row.appendChild(main);

      var status;
      if (s.default) {
        status = el("span", "store-status builtin", "Built-in");
      } else if (s.ok === true) {
        status = el("span", "store-status ok",
          typeof s.count === "number" ? (s.count + " apps") : "Online");
      } else if (s.ok === false) {
        status = el("span", "store-status err", "Offline");
        if (s.error) status.title = s.error;
      } else {
        status = el("span", "store-status", "Store");
      }
      row.appendChild(status);

      var rm = el("button", "store-remove", "\u00d7");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove store");
      rm.dataset.alwaysOn = "1";
      if (s.removable === false || s.default) {
        rm.disabled = true;
        rm.title = "The built-in store cannot be removed";
      } else {
        rm.addEventListener("click", function () { onRemoveStore(s.root, s.label); });
      }
      row.appendChild(rm);

      storesListEl.appendChild(row);
    });
  }

  async function onRemoveStore(root, label) {
    if (jobRunning) return;
    var ok = await showConfirm(
      "Remove the app store\n\n" + (label || root) + "\n\n" +
      "Its apps will no longer be listed. Anything already installed is " +
      "unaffected.",
      { title: "Remove app store", okLabel: "Remove" });
    if (!ok) return;
    try {
      var resp = await fetch("/api/apps/stores", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: root })
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) { window.alert(data.error || "Could not remove the store."); return; }
    } catch (err) {
      window.alert("Could not remove the store: " + err.message);
      return;
    }
    loadCatalog();
  }

  function wireStores() {
    var disclosure = document.getElementById("storesDisclosure");
    var panel = document.getElementById("storesPanel");
    var input = document.getElementById("storeInput");
    var btn = document.getElementById("addStoreBtn");
    if (!disclosure || !panel || !input || !btn) return;

    disclosure.addEventListener("click", function () {
      var open = panel.classList.toggle("hidden") === false;
      disclosure.setAttribute("aria-expanded", open ? "true" : "false");
    });

    btn.dataset.alwaysOn = "1";
    function refresh() { btn.disabled = !isHttpUrl(input.value); }
    input.addEventListener("input", refresh);

    btn.addEventListener("click", async function () {
      var url = input.value.trim();
      if (!isHttpUrl(url)) { window.alert("Enter a valid http(s):// URL."); return; }
      btn.disabled = true;
      try {
        var resp = await fetch("/api/apps/stores", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: url })
        });
        var data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) { window.alert(data.error || "Could not add the store."); return; }
        input.value = "";
      } catch (err) {
        window.alert("Could not add the store: " + err.message);
        return;
      } finally {
        refresh();
      }
      loadCatalog();
    });
  }

  /* ---- static buttons ---- */

  function wireStaticButtons() {
    var back = document.getElementById("appStoreBack");
    if (back) back.addEventListener("click", function () { window.location.assign("/"); });

    var watch = document.getElementById("watchVideoBtn");
    if (watch) {
      watch.dataset.alwaysOn = "1";
      watch.addEventListener("click", function () {
        window.location.assign("https://www.youtube.com/watch?v=vFGiEFz4XYg&autoplay=1&fs=1");
      });
    }

    var ubuntu = document.getElementById("installUbuntuBtn");
    if (ubuntu) {
      ubuntu.addEventListener("click", function () { window.location.assign("/ubuntu-install"); });
    }

    var uninstallUbuntu = document.getElementById("uninstallUbuntuBtn");
    if (uninstallUbuntu) {
      uninstallUbuntu.addEventListener("click", function () { window.location.assign("/ubuntu-uninstall"); });
    }

    if (recoverBtn) recoverBtn.addEventListener("click", onRecover);
  }

  /* ---- init ---- */
  wireStaticButtons();
  wireManualInstall();
  wireUploadHistory();
  wireStores();
  loadCatalog();
})();
