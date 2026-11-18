/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

(function initFileBrowser() {
  const modal = document.getElementById("fileBrowserModal");
  const fbClose = document.getElementById("fbClose");
  const fbList = document.getElementById("fbList");
  const fbBreadcrumb = document.getElementById("fbBreadcrumb");
  const fbSources = document.getElementById("fbSources");
  if (!modal) return;

  const DEFAULT_SOURCES = [{ id: "usb", label: "USB Media", available: true }];

  let targetInput = null;
  let currentPath = "";
  let currentSource = "usb";
  let sources = DEFAULT_SOURCES;

  function sourceLabel(id) {
    const match = sources.filter((s) => s.id === id)[0];
    return match ? match.label : id;
  }

  function renderSources() {
    if (!fbSources) return;
    const usable = sources.filter((s) => s.available);
    fbSources.textContent = "";
    // Only worth showing the switcher when there is more than one place to look.
    fbSources.classList.toggle("hidden", usable.length < 2);
    for (const src of usable) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "fb-source" + (src.id === currentSource ? " active" : "");
      btn.textContent = src.label;
      btn.addEventListener("click", () => {
        if (src.id === currentSource) return;
        currentSource = src.id;
        renderSources();
        loadDir("");
      });
      fbSources.appendChild(btn);
    }
  }

  async function loadSources() {
    try {
      const resp = await fetch("/api/browse-sources");
      const data = await resp.json();
      if (data && data.sources && data.sources.length) sources = data.sources;
    } catch (e) {
      sources = DEFAULT_SOURCES;
    }
    renderSources();
  }

  async function openBrowser(inputId) {
    targetInput = document.getElementById(inputId);
    if (!targetInput) return;
    currentPath = "";
    modal.classList.remove("hidden");
    fbList.innerHTML = '<div class="fb-loading">Loading…</div>';
    await loadSources();
    // Open on the first source that actually has something to browse, so a
    // board with no USB stick lands straight on the installed Ubuntu.
    const usable = sources.filter((s) => s.available);
    currentSource = usable.length ? usable[0].id : "usb";
    renderSources();
    loadDir("");
  }

  function closeBrowser() {
    modal.classList.add("hidden");
    targetInput = null;
  }

  const fbRefresh = document.getElementById("fbRefresh");

  fbClose.addEventListener("click", closeBrowser);
  fbRefresh.addEventListener("click", () => loadDir(currentPath));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeBrowser();
  });

  function renderBreadcrumb(dirPath) {
    fbBreadcrumb.textContent = "";
    const parts = dirPath ? dirPath.split("/") : [];
    const crumbs = [{ label: sourceLabel(currentSource), path: "" }];
    let acc = "";
    for (const p of parts) {
      acc = acc ? acc + "/" + p : p;
      crumbs.push({ label: p, path: acc });
    }
    crumbs.forEach((c, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "fb-sep";
        sep.textContent = " / ";
        fbBreadcrumb.appendChild(sep);
      }
      const a = document.createElement("a");
      a.href = "#";
      a.className = "fb-crumb";
      a.textContent = c.label;
      a.addEventListener("click", (e) => { e.preventDefault(); loadDir(c.path); });
      fbBreadcrumb.appendChild(a);
    });
  }

  async function loadDir(relPath) {
    currentPath = relPath;
    renderBreadcrumb(relPath);
    fbList.innerHTML = '<div class="fb-loading">Loading…</div>';
    try {
      const resp = await fetch("/api/browse?source=" + encodeURIComponent(currentSource) +
                              "&path=" + encodeURIComponent(relPath));
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        fbList.innerHTML = '<div class="fb-empty"></div>';
        fbList.firstChild.textContent = err.error || "Unable to browse";
        return;
      }
      const data = await resp.json();
      fbList.textContent = "";
      if (currentPath) {
        const back = document.createElement("div");
        back.className = "fb-entry fb-back";
        back.innerHTML = '<span class="fb-icon"><svg viewBox="0 0 24 24" width="18" height="18"><path d="M19 12H5M12 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="fb-name">..</span>';
        back.addEventListener("click", () => {
          const parent = currentPath.includes("/") ? currentPath.substring(0, currentPath.lastIndexOf("/")) : "";
          loadDir(parent);
        });
        fbList.appendChild(back);
      }
      if (!data.entries.length && !currentPath) {
        fbList.innerHTML = '<div class="fb-empty">No files or devices found</div>';
        return;
      }
      const dirs = data.entries.filter(e => e.is_dir);
      const files = data.entries.filter(e => !e.is_dir);
      for (const entry of [...dirs, ...files]) {
        const row = document.createElement("div");
        row.className = "fb-entry" + (entry.is_dir ? " fb-dir" : " fb-file");
        const icon = document.createElement("span");
        icon.className = "fb-icon";
        icon.innerHTML = entry.is_dir
          ? '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" fill="rgba(255,170,60,0.85)" stroke="none"/></svg>'
          : '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" fill="none" stroke="currentColor" stroke-width="1.5"/><polyline points="14 2 14 8 20 8" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';
        const label = document.createElement("span");
        label.className = "fb-name";
        label.textContent = entry.name;
        row.appendChild(icon);
        row.appendChild(label);
        row.addEventListener("click", () => {
          if (entry.is_dir) {
            loadDir(currentPath ? currentPath + "/" + entry.name : entry.name);
          } else {
            const fullPath = data.abs_path + "/" + entry.name;
            targetInput.value = fullPath;
            targetInput.dispatchEvent(new Event("input"));
            closeBrowser();
          }
        });
        fbList.appendChild(row);
      }
    } catch (e) {
      fbList.innerHTML = '<div class="fb-empty">Connection error</div>';
    }
  }

  document.querySelectorAll(".browse-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-target");
      if (targetId) openBrowser(targetId);
    });
  });
})();
