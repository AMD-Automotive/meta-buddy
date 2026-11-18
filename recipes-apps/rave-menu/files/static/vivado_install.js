/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* Vivado Lab Tools installer page logic.
 *
 * Several Vivado releases can be installed side by side; the release picker
 * at the top of the page drives the download details, the candidate list and
 * the install request. A release can be pre-selected via ?version=2026.1.
 *
 * Backed by:
 *   GET  /api/vivado/status   -- catalog + install state + discovered tars
 *   POST /api/vivado/upload   -- multipart upload
 *   POST /api/vivado/install  -- launches install job, returns job_id
 *   GET  /api/stream/<id>     -- existing SSE channel
 */
(function () {
  "use strict";

  const els = {
    state:        document.getElementById("vivadoCurrentState"),
    versionSel:   document.getElementById("vivadoVersion"),
    versionBadge: document.getElementById("vivadoVersionBadge"),
    versionNote:  document.getElementById("vivadoVersionNote"),
    expFilename:  document.getElementById("expFilename"),
    expSha:       document.getElementById("expSha"),
    expUrl:       document.getElementById("expUrl"),
    qrImg:        document.getElementById("qrImg"),
    copyBtn:      document.getElementById("copyUrlBtn"),
    openBtn:      document.getElementById("openUrlBtn"),
    copyHint:     document.getElementById("copyHint"),
    sourceList:   document.getElementById("sourceList"),
    step1:        document.getElementById("vivadoStep1"),
    step1Toggle:  document.getElementById("step1Toggle"),
    step2:        document.getElementById("vivadoStep2"),
    step2Toggle:  document.getElementById("step2Toggle"),
    rescanBtn:    document.getElementById("rescanBtn"),
    uploadFile:   document.getElementById("uploadFile"),
    uploadDrop:   document.querySelector(".vi-drop"),
    uploadStatus: document.getElementById("uploadStatus"),
    selectedPath: document.getElementById("selectedPath"),
    selectedBadge:document.getElementById("selectedBadge"),
    installBtn:   document.getElementById("installBtn"),
    forceBtn:     document.getElementById("forceBtn"),
    shaWarning:   document.getElementById("shaWarning"),
    warnTitle:    document.getElementById("warnTitle"),
    warnExpectedRow: document.getElementById("warnExpectedRow"),
    warnExpected: document.getElementById("warnExpected"),
    warnComputed: document.getElementById("warnComputed"),
    warnHint:     document.getElementById("warnHint"),
    progressWrap: document.getElementById("progressWrap"),
    progressLabel:document.getElementById("progressLabel"),
    progressLog:  document.getElementById("progressLog"),
    eulaModal:    document.getElementById("eulaModal"),
    eulaSections: document.getElementById("eulaSections"),
    eulaAccept:   document.getElementById("eulaAccept"),
    eulaAgree:    document.getElementById("eulaAgree"),
    eulaDecline:  document.getElementById("eulaDecline"),
    eulaClose:    document.getElementById("eulaClose"),
    tipsModal:    document.getElementById("browserTipsModal"),
    tipsClose:    document.getElementById("browserTipsClose"),
    tipsCancel:   document.getElementById("browserTipsCancel"),
    tipsContinue: document.getElementById("browserTipsContinue"),
    tabs:         document.querySelectorAll(".vi-tab"),
    tabPanels:    {
      found:  document.getElementById("tabFound"),
      upload: document.getElementById("tabUpload"),
    },
  };

  document.body.appendChild(els.eulaModal);

  let selected = null;     // {path, sha_ok, version, hint, name, kind, sha256}
  let versions = [];       // catalog + installed, newest first
  let version = "";        // currently chosen release
  let activeJob = null;    // EventSource
  let prereqBlocked = false;  // chosen release needs the base one installed first

  // ?version=2026.1 deep-links here from the lab modal's version picker.
  const requestedVersion =
    new URLSearchParams(window.location.search).get("version") || "";

  function entry() {
    return versions.filter(function (v) { return v.version === version; })[0] || null;
  }

  function baseEntry() {
    return versions.filter(function (v) { return v.is_base; })[0] || null;
  }

  function baseInstalled() {
    const b = baseEntry();
    return !!(b && b.installed);
  }

  function baseLabel() {
    const b = baseEntry();
    return b ? b.version : "The base release";
  }

  // Install needs a tar for the chosen release and that release's
  // prerequisite satisfied.
  function updateInstallEnabled() {
    const wrongRelease = !!(selected && selected.version && selected.version !== version);
    els.installBtn.disabled = !selected || !selected.path || wrongRelease || prereqBlocked;
  }

  // ── tabs ──────────────────────────────────────────────────────────────
  els.tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      els.tabs.forEach(function (x) { x.classList.remove("is-active"); });
      t.classList.add("is-active");
      const name = t.getAttribute("data-tab");
      Object.keys(els.tabPanels).forEach(function (k) {
        els.tabPanels[k].classList.toggle("is-active", k === name);
      });
    });
  });

  // ── status / discovery ────────────────────────────────────────────────
  let lastSources = [];

  function refreshStatus() {
    fetch("/api/vivado/status").then(function (r) { return r.json(); })
      .then(function (j) {
        versions = j.versions || [];
        if (!version) {
          const wanted = [requestedVersion, j.default_version, j.base_version];
          version = wanted.filter(function (v) {
            return v && versions.some(function (e) { return e.version === v; });
          })[0] || (versions[0] && versions[0].version) || "";
        }
        renderVersionPicker();
        lastSources = j.sources || [];
        renderSelectedVersion();
      })
      .catch(function () {
        els.state.textContent = "Status request failed.";
        els.state.classList.add("is-missing");
      });
  }

  function renderVersionPicker() {
    if (!els.versionSel) return;
    els.versionSel.innerHTML = "";
    versions.forEach(function (v) {
      const opt = document.createElement("option");
      opt.value = v.version;
      let label = v.version;
      if (v.is_base) label += " — base";
      label += v.installed ? " (installed)" : " (not installed)";
      opt.textContent = label;
      els.versionSel.appendChild(opt);
    });
    if (version) els.versionSel.value = version;

    const installedCount = versions.filter(function (v) { return v.installed; }).length;
    if (!installedCount) {
      els.state.textContent = "No Vivado HW Server release is installed yet. Use the steps below.";
      els.state.className = "vi-current-state is-missing";
    } else if (!baseInstalled()) {
      const names = versions.filter(function (v) { return v.installed; })
        .map(function (v) { return v.version; }).join(", ");
      els.state.textContent = "Installed: " + names + ". " + baseLabel() +
        " is still required before the Versal lab and App Store can be used.";
      els.state.className = "vi-current-state is-missing";
    } else {
      const names = versions.filter(function (v) { return v.installed; })
        .map(function (v) { return v.version; }).join(", ");
      els.state.textContent = "Installed: " + names +
        ". Installing another release leaves these in place.";
      els.state.className = "vi-current-state is-installed";
    }
  }

  // Re-render everything that depends on the chosen release.
  function renderSelectedVersion() {
    const e = entry();
    selected = null;
    els.selectedPath.textContent = "(none)";
    els.selectedBadge.textContent = "";
    els.selectedBadge.className = "vi-badge";
    els.installBtn.disabled = true;
    els.forceBtn.style.display = "none";
    els.shaWarning.style.display = "none";

    renderExpected(e);
    renderSources(lastSources);
  }

  function renderExpected(e) {
    const known = e && e.in_catalog !== false && e.filename;
    els.expFilename.textContent = known ? e.filename : "(not in the shipped catalog)";
    els.expSha.textContent = known ? e.sha256 : "(unknown)";
    els.expUrl.textContent = known ? e.url : "(unknown)";
    els.expUrl.setAttribute("href", known ? e.url : "#");
    if (els.qrImg) {
      els.qrImg.style.display = known ? "" : "none";
      if (known) {
        els.qrImg.src = "/vivado-installer-url.png?version=" + encodeURIComponent(e.version);
      }
    }

    if (!e) {
      els.versionBadge.textContent = "";
      els.versionNote.textContent = "";
      return;
    }
    els.versionBadge.textContent = e.installed ? "Installed" : "Not installed";
    els.versionBadge.className = "vi-badge " + (e.installed ? "ok" : "warn");

    // A non-base release unlocks nothing on its own. Say so whenever the base
    // is absent, but only block the install when there is nothing there yet --
    // reinstalling an existing one is still a valid repair.
    const baseMissing = !e.is_base && !baseInstalled();
    prereqBlocked = baseMissing && !e.installed;

    let note = "";
    if (e.is_base) {
      note = "Base release — required for programming the Versal from this device.";
    } else if (baseMissing) {
      note = baseLabel() + " must be installed prior to any other version.  This is required for the lab tools and app store.  Extra versions can be installed on demand for JTAG server use only.";
    } else {
      note = "Optional release — installs alongside the base one for remote connections.";
    }
    if (e.installed && e.install_dir) note += " Installed at " + e.install_dir + ".";
    els.versionNote.textContent = note;
    els.versionNote.classList.toggle("is-blocked", baseMissing);
    updateInstallEnabled();

    // Once this release is installed both upstream steps are done -- mark
    // them complete and collapse so the page focuses on the result. The
    // user can still expand either step.
    if (els.step1) {
      els.step1.classList.toggle("is-complete", !!e.installed);
      if (e.installed && !step1UserToggled) setStep1Collapsed(true);
    }
    if (els.step2) {
      els.step2.classList.toggle("is-complete", !!e.installed);
      if (!step2UserToggled) setStep2Collapsed(!!e.installed);
    }
  }

  if (els.versionSel) {
    els.versionSel.addEventListener("change", function () {
      version = els.versionSel.value;
      step1UserToggled = false;
      step2UserToggled = false;
      renderVersionPicker();
      renderSelectedVersion();
    });
  }

  function fmtSize(n) {
    if (!n && n !== 0) return "";
    if (n >= 1024 * 1024 * 1024) return (n / 1073741824).toFixed(2) + " GiB";
    if (n >= 1024 * 1024) return (n / 1048576).toFixed(1) + " MiB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KiB";
    return n + " B";
  }

  function setStep1Collapsed(collapsed) {
    if (!els.step1) return;
    els.step1.classList.toggle("is-collapsed", !!collapsed);
    if (els.step1Toggle) {
      els.step1Toggle.textContent = collapsed ? "Show" : "Hide";
    }
  }

  function setStep2Collapsed(collapsed) {
    if (!els.step2) return;
    els.step2.classList.toggle("is-collapsed", !!collapsed);
    if (els.step2Toggle) {
      els.step2Toggle.textContent = collapsed ? "Show" : "Hide";
    }
  }

  // User-collapse override: once the user manually clicks Show/Hide we stop
  // auto-collapsing on subsequent status refreshes.
  let step1UserToggled = false;
  if (els.step1Toggle) {
    els.step1Toggle.addEventListener("click", function () {
      step1UserToggled = true;
      setStep1Collapsed(!els.step1.classList.contains("is-collapsed"));
    });
  }
  let step2UserToggled = false;
  if (els.step2Toggle) {
    els.step2Toggle.addEventListener("click", function () {
      step2UserToggled = true;
      setStep2Collapsed(!els.step2.classList.contains("is-collapsed"));
    });
  }

  function renderSources(items) {
    els.sourceList.innerHTML = "";
    // Only tars whose checksum matches *this* release count as a match; a
    // tar for another release is listed but cannot be installed here.
    const matching = items.filter(function (s) { return s.version === version; });
    const hasAny = items.length > 0;
    if (els.step1) {
      els.step1.classList.toggle("is-complete", matching.length > 0);
      if (!step1UserToggled) setStep1Collapsed(matching.length > 0);
    }
    if (!hasAny) {
      const li = document.createElement("li");
      li.className = "vi-source-empty";
      li.textContent = "No installer files found yet. Try rescanning after the download completes.";
      els.sourceList.appendChild(li);
      return;
    }
    items.forEach(function (s) {
      const li = document.createElement("li");
      const main = document.createElement("div");
      main.className = "vi-src-main";
      const name = document.createElement("div");
      name.className = "vi-src-name";
      name.textContent = s.name + "  —  " + s.path;
      const meta = document.createElement("div");
      meta.className = "vi-src-meta";
      meta.textContent = (s.kind || "?") + " • " + fmtSize(s.size);
      main.appendChild(name);
      main.appendChild(meta);
      const isMatch = s.version === version;
      let hint = s.hint;
      if (s.version && !isMatch) {
        hint = "This is the " + s.version + " installer. Switch the release above to install it.";
      }
      if (hint) {
        const hintEl = document.createElement("div");
        hintEl.className = "vi-src-hint";
        hintEl.textContent = hint;
        main.appendChild(hintEl);
      }
      const badge = document.createElement("span");
      if (isMatch) {
        badge.className = "vi-badge ok";
        badge.textContent = "SHA OK";
      } else if (s.version) {
        badge.className = "vi-badge warn";
        badge.textContent = s.version;
      } else if (!s.sha256) {
        badge.className = "vi-badge warn";
        badge.textContent = "Unhashable";
      } else {
        badge.className = "vi-badge bad";
        badge.textContent = "SHA mismatch";
      }
      li.appendChild(main);
      li.appendChild(badge);
      li.addEventListener("click", function () { selectSource(s, li); });
      els.sourceList.appendChild(li);
    });
    // Auto-select the first perfect match for the chosen release.
    if (matching.length) {
      const index = items.indexOf(matching[0]);
      selectSource(matching[0], els.sourceList.children[index]);
    }
  }

  function selectSource(s, li) {
    selected = s;
    if (li) {
      els.sourceList.querySelectorAll("li.is-selected").forEach(function (x) { x.classList.remove("is-selected"); });
      li.classList.add("is-selected");
    }
    const isMatch = s.version === version;
    els.selectedPath.textContent = s.path;
    els.selectedBadge.textContent = isMatch ? "SHA OK"
      : (s.version ? "This is " + s.version : "SHA mismatch");
    els.selectedBadge.className = "vi-badge " + (isMatch ? "ok" : "bad");
    // A tar for a different release can never be installed here -- xsetup
    // would write into that release's directory instead.
    updateInstallEnabled();
    els.forceBtn.style.display = "none";
    els.shaWarning.style.display = "none";
  }

  els.rescanBtn.addEventListener("click", refreshStatus);

  // ── URL helpers ───────────────────────────────────────────────────────
  els.copyBtn.addEventListener("click", function () {
    const url = els.expUrl.textContent;
    if (!url) return;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(function () {
        els.copyHint.style.display = "block";
        setTimeout(function () { els.copyHint.style.display = "none"; }, 1500);
      });
    } else {
      const ta = document.createElement("textarea");
      ta.value = url; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (_) {}
      document.body.removeChild(ta);
    }
  });
  function openInstallerUrlPopup() {
    const url = els.expUrl.getAttribute("href");
    if (!url || url === "#") return;
    // Open in a new tab. Users are instructed (via the browser-tips modal
    // shown immediately before this) to press F11 to exit fullscreen so the
    // tab strip and download bar are visible.
    return window.open(url, "_blank", "noopener");
  }

  // True when this page is being viewed on the device's own browser (cog
  // Chromium under Weston). Remote/desktop browsers connect via the device's
  // IP address and don't need the keyboard-shortcut prompt.
  function isOnTargetBrowser() {
    const h = window.location.hostname;
    return h === "127.0.0.1" || h === "localhost" || h === "::1";
  }

  function showBrowserTipsThen(action) {
    if (!els.tipsModal) { action(); return; }
    if (!isOnTargetBrowser()) { action(); return; }

    els.tipsModal.style.display = "flex";

    function close() {
      els.tipsModal.style.display = "none";
      els.tipsContinue.removeEventListener("click", onContinue);
      els.tipsCancel.removeEventListener("click", onCancel);
      els.tipsClose.removeEventListener("click", onCancel);
    }
    function onContinue() { close(); action(); }
    function onCancel() { close(); }
    els.tipsContinue.addEventListener("click", onContinue);
    els.tipsCancel.addEventListener("click", onCancel);
    els.tipsClose.addEventListener("click", onCancel);
  }

  els.openBtn.addEventListener("click", function () {
    showBrowserTipsThen(openInstallerUrlPopup);
  });
  // Make clicking the URL text behave the same as the button -- otherwise
  // the anchor's default action would open a new tab and strand the user.
  els.expUrl.addEventListener("click", function (ev) {
    ev.preventDefault();
    showBrowserTipsThen(openInstallerUrlPopup);
  });
  if (els.qrImg) {
    els.qrImg.style.cursor = "pointer";
    els.qrImg.addEventListener("click", function () {
      showBrowserTipsThen(openInstallerUrlPopup);
    });
  }

  // ── upload tab ────────────────────────────────────────────────────────
  els.uploadFile.addEventListener("change", function () {
    if (els.uploadFile.files && els.uploadFile.files[0]) {
      uploadFile(els.uploadFile.files[0]);
    }
  });
  ["dragenter", "dragover"].forEach(function (e) {
    els.uploadDrop.addEventListener(e, function (ev) {
      ev.preventDefault(); els.uploadDrop.classList.add("is-drag");
    });
  });
  ["dragleave", "drop"].forEach(function (e) {
    els.uploadDrop.addEventListener(e, function (ev) {
      ev.preventDefault(); els.uploadDrop.classList.remove("is-drag");
    });
  });
  els.uploadDrop.addEventListener("drop", function (ev) {
    if (ev.dataTransfer && ev.dataTransfer.files[0]) uploadFile(ev.dataTransfer.files[0]);
  });

  function uploadFile(file) {
    els.uploadStatus.textContent = "Uploading " + file.name + " (" + fmtSize(file.size) + ")...";
    const fd = new FormData();
    fd.append("file", file, file.name);
    fetch("/api/vivado/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          els.uploadStatus.textContent = "Upload failed: " + (j.error || "unknown");
          return;
        }
        els.uploadStatus.textContent = "Uploaded to " + j.path + " — " +
          (j.version ? "matches " + j.version : "checksum matches no known release");
        // Refresh list and auto-select the upload.
        refreshStatus();
      })
      .catch(function (err) {
        els.uploadStatus.textContent = "Upload failed: " + err;
      });
  }

  // ── install ───────────────────────────────────────────────────────────
  // Two-step flow: click "Install" -> fetch + show EULAs -> user accepts
  // both -> we POST /api/vivado/install with accepted_eulas:true.
  let pendingForce = false;

  els.installBtn.addEventListener("click", function () { requestEulasAndPrompt(false); });
  els.forceBtn.addEventListener("click",   function () { requestEulasAndPrompt(true); });

  function requestEulasAndPrompt(force) {
    if (!selected || !selected.path) return;
    if (prereqBlocked) return;   // Install/Force must not bypass the base prerequisite.
    pendingForce = !!force;
    els.installBtn.disabled = true;
    els.forceBtn.disabled = true;
    els.eulaSections.innerHTML =
      '<div class="vi-eula-body" style="text-align:center;">Loading license agreements...</div>';
    els.eulaAccept.checked = false;
    els.eulaAgree.disabled = true;
    els.eulaModal.style.display = "flex";

    fetch("/api/vivado/eulas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: selected.path }),
    })
      .then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); })
      .then(function (pair) {
        const status = pair[0]; const j = pair[1];
        if (status !== 200 || !j.ok) {
          els.eulaSections.innerHTML =
            '<div class="vi-eula-body">Failed to read license agreements from the installer: ' +
            escapeHtml((j && j.error) || "unknown") + '</div>';
          return;
        }
        renderEulas(j.eulas || []);
      })
      .catch(function (err) {
        els.eulaSections.innerHTML =
          '<div class="vi-eula-body">Failed to read license agreements: ' +
          escapeHtml(String(err)) + '</div>';
      });
  }

  function renderEulas(list) {
    els.eulaSections.innerHTML = "";
    if (!list.length) {
      els.eulaSections.innerHTML =
        '<div class="vi-eula-body">No EULA text was found in this installer tarball.</div>';
      return;
    }
    list.forEach(function (e) {
      const sec = document.createElement("div");
      sec.className = "vi-eula-section";
      const title = document.createElement("div");
      title.className = "vi-eula-section-title";
      title.textContent = e.title || "License Agreement";
      const body = document.createElement("div");
      body.className = "vi-eula-body";
      body.textContent = e.body || "";
      sec.appendChild(title);
      sec.appendChild(body);
      els.eulaSections.appendChild(sec);
    });
  }

  function closeEulaModal() {
    els.eulaModal.style.display = "none";
    els.installBtn.disabled = false;
    els.forceBtn.disabled = false;
  }

  els.eulaAccept.addEventListener("change", function () {
    els.eulaAgree.disabled = !els.eulaAccept.checked;
  });
  els.eulaDecline.addEventListener("click", closeEulaModal);
  els.eulaClose.addEventListener("click",   closeEulaModal);
  els.eulaAgree.addEventListener("click", function () {
    if (!els.eulaAccept.checked) return;
    els.eulaModal.style.display = "none";
    startInstall(pendingForce);
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[c];
    });
  }

  function startInstall(force) {
    if (!selected || !selected.path) return;
    els.installBtn.disabled = true;
    els.forceBtn.disabled = true;
    els.shaWarning.style.display = "none";
    fetch("/api/vivado/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: selected.path,
        version: version,
        force: !!force,
        accepted_eulas: true,
      }),
    })
      .then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); })
      .then(function (pair) {
        const status = pair[0]; const j = pair[1];
        if (status === 409 && j && j.error === "wrong_version") {
          els.warnTitle.textContent = "Wrong release.";
          els.warnExpectedRow.style.display = "none";
          els.warnComputed.textContent = j.computed_sha256 || "(unknown)";
          els.warnHint.textContent = "This file is the " + j.matched_version +
            " installer. Select " + j.matched_version +
            " above to install it, or choose a " + version + " file.";
          els.shaWarning.style.display = "block";
          els.forceBtn.style.display = "none";
          els.installBtn.disabled = true;
          return;
        }
        if (status === 409 && j && j.error === "sha256_mismatch") {
          els.warnTitle.textContent = "Checksum mismatch.";
          els.warnExpectedRow.style.display = "";
          els.warnExpected.textContent = j.expected_sha256 || "";
          els.warnComputed.textContent = j.computed_sha256 || "(unknown)";
          els.warnHint.textContent = "The downloaded file may have been " +
            "corrupted, or it is not the " + version + " release.";
          els.shaWarning.style.display = "block";
          els.forceBtn.style.display = "inline-flex";
          els.forceBtn.disabled = false;
          els.installBtn.disabled = false;
          return;
        }
        if (!j.ok || !j.job_id) {
          els.installBtn.disabled = false;
          alert("Install request failed: " + (j.error || "unknown"));
          return;
        }
        attachStream(j.job_id);
      })
      .catch(function (err) {
        els.installBtn.disabled = false;
        alert("Install request failed: " + err);
      });
  }

  function attachStream(jobId) {
    els.progressWrap.style.display = "block";
    els.progressLabel.textContent = "Installing " + version + "... (this takes several minutes)";
    els.progressLog.textContent = "";
    if (activeJob) { try { activeJob.close(); } catch (_) {} }
    activeJob = new EventSource("/api/stream/" + encodeURIComponent(jobId));
    activeJob.onmessage = function (ev) {
      els.progressLog.textContent += ev.data + "\n";
      els.progressLog.scrollTop = els.progressLog.scrollHeight;
    };
    activeJob.addEventListener("status", function (ev) {
      const text = (ev.data || "");
      if (text.indexOf("exited:0") === 0) {
        els.progressLabel.textContent = "Install of " + version + " complete.";
        refreshStatus();
      } else if (text.indexOf("exited:") === 0) {
        els.progressLabel.textContent = "Install failed (" + text + ")";
      } else {
        els.progressLabel.textContent = text;
      }
      try { activeJob.close(); } catch (_) {}
      activeJob = null;
      els.installBtn.disabled = false;
    });
    activeJob.onerror = function () {
      // Network blip / server end -- don't tear down the install thread.
      els.progressLog.textContent += "[connection lost; reconnecting...]\n";
    };
  }

  refreshStatus();
})();
