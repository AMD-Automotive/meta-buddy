/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

const MORE_INFO_URL = "https://www.sapphiretech.com/en/commercial/edge-plus-vpr_7p100";

const VERSAL_REPROGRAM_CONFIRM_MSG =
  "Reprogramming the Versal OSPI/UFS will erase current contents. Are you sure?";

// Catalogue lookup supplied by base.html; falls back to the key, which is the
// English source string.
const translate = typeof window.t === "function" ? window.t : (key) => key;

fetch("/api/storage-status")
  .then((response) => response.json())
  .then((status) => {
    if (status.state !== "degraded") return;
    const warning = document.getElementById("storageWarning");
    if (!warning) return;
    warning.hidden = false;
    if (status.reason) {
      warning.title = "Storage status: " + status.reason;
    }
  })
  .catch(() => {});

/* Versal "Program custom payload" pre-flight confirmation.
 *
 * Returns a Promise that resolves to either an args array suitable for
 * runAction("board-test", ..., args) -- order is [wic, ospi, bmap] -- or null if
 * the user cancelled or both inputs were empty.
 *
 *   - both filled  : standard erase confirm
 *   - OSPI only    : mismatch warning ("...just update the OSPI?")
 *   - WIC only     : mismatch warning ("...just update the Rootfs?")
 *
 * Single-phase updates skip the standard erase confirm because the mismatch
 * warning already implies erasure of the chosen device and chaining two
 * confirms is friction.
 */
async function confirmVersalProgram(ospi, wic, bmap, partialCount) {
  ospi = (ospi || "").trim();
  wic = (wic || "").trim();
  bmap = (bmap || "").trim();
  if (bmap && !wic) {
    window.alert("Select a Rootfs WIC image before adding a bmap file.");
    return null;
  }
  if (!ospi && !wic) return null;

  // A partial update is explicitly scoped by the advanced panel, so the
  // OSPI/rootfs mismatch warning below does not apply to it.
  if (partialCount) {
    const partialMsg =
      "Update " + partialCount + " selected partition(s) on the target? " +
      "The partition table will not be modified.";
    if (!(await showConfirm(partialMsg, { okLabel: "Update" }))) return null;
    return [wic, ospi, bmap];
  }

  if (ospi && wic) {
    if (!(await showConfirm(VERSAL_REPROGRAM_CONFIRM_MSG))) return null;
    return [wic, ospi, bmap];
  }

  const label = ospi ? "OSPI" : "Rootfs";
  const msg =
    "If OSPI and rootfs are updated independently, there could be a " +
    "mismatch -- are you sure you want to just update the " + label + "?";
  if (!(await showConfirm(msg, { okLabel: "Yes", cancelLabel: "Cancel" }))) {
    return null;
  }
  return ospi ? ["", ospi, ""] : [wic, "", bmap];
}
window.confirmVersalProgram = confirmVersalProgram;

/* Themed confirm dialog — returns a Promise<boolean>. */
function showConfirm(message, { title = null, okLabel = null, cancelLabel = null } = {}) {
  title = title || translate("Please confirm");
  okLabel = okLabel || translate("Continue");
  cancelLabel = cancelLabel || translate("Cancel");
  return new Promise((resolve) => {
    const modal = document.getElementById("confirmModal");
    const msgEl = document.getElementById("confirmMessage");
    const titleEl = document.getElementById("confirmTitle");
    const okBtn = document.getElementById("confirmOk");
    const cancelBtn = document.getElementById("confirmCancel");
    if (!modal || !msgEl || !okBtn || !cancelBtn) {
      resolve(window.confirm(message));
      return;
    }
    if (titleEl) titleEl.textContent = title;
    msgEl.textContent = message;
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel;
    modal.classList.remove("hidden");
    requestAnimationFrame(() => cancelBtn.focus());

    function cleanup(result) {
      modal.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    function onBackdrop(e) { if (e.target === modal) cleanup(false); }
    function onKey(e) {
      if (e.key === "Escape") cleanup(false);
      else if (e.key === "Enter") cleanup(true);
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

const customUrlCard = document.getElementById("customUrlCard");
const customUrlToggle = document.getElementById("customUrlToggle");
const customUrlForm = document.getElementById("customUrlForm");
const ospiInput = document.getElementById("ospiInput");
const wicInput = document.getElementById("wicInput");
const customUrlGo = document.getElementById("customUrlGo");

function toggleCustomUrlEntry() {
  if (!customUrlCard || !customUrlToggle) return;
  const collapsed = customUrlCard.classList.toggle("is-collapsed");
  customUrlToggle.setAttribute("aria-expanded", String(!collapsed));
  if (!collapsed && ospiInput) {
    requestAnimationFrame(() => ospiInput.focus());
  }
}

function updateProgramButton() {
  if (customUrlGo) {
    customUrlGo.disabled = !(ospiInput.value.trim() || wicInput.value.trim());
  }
}

if (customUrlCard && customUrlToggle && customUrlForm && ospiInput && wicInput) {
  customUrlToggle.addEventListener("click", toggleCustomUrlEntry);
  ospiInput.addEventListener("input", updateProgramButton);
  wicInput.addEventListener("input", updateProgramButton);

  customUrlForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const args = await confirmVersalProgram(ospiInput.value, wicInput.value);
    if (!args) return;
    runAction("board-test", "Program custom Versal payload", true, args);
  });
}




document.querySelectorAll(".menu-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const action = btn.getAttribute("data-action");

    if (action === "more-info") {
      window.location.assign(MORE_INFO_URL);
      return;
    }

    if (action === "wifi") {
      networkUi.openModal();
      return;
    }

    if (action === "versal-lab") {
      const m = document.getElementById("versalLabModal");
      if (m) m.classList.remove("hidden");
      return;
    }

    if (action === "serial-1" || action === "serial-2") {
      const port = parseInt(btn.getAttribute("data-serial"), 10);
      if (window.openSerialConsole) window.openSerialConsole(port);
      return;
    }

    if (action === "x86-console") {
      if (window.openX86Console) window.openX86Console();
      return;
    }

    if (action === "audio-page") {
      window.location.assign("/audio");
      return;
    }

    if (action === "localisation-page") {
      window.location.assign("/localisation");
      return;
    }

    if (action === "run-board-test-page") {
      window.location.assign("/board-test");
      return;
    }

    if (action === "app-store") {
      window.location.assign("/app-store");
      return;
    }

    if (action === "system-update-page") {
      window.location.assign("/system-update");
      return;
    }

    if (action === "install-nvme-page") {
      window.location.assign("/install-nvme");
      return;
    }
    if (action === "power-off-page") {
      window.location.assign("/power-off");
      return;
    }

    const showTerminal = btn.getAttribute("data-terminal") === "1";
    const title = btn.querySelector(".txt")?.textContent || action;
    if (action === "board-test" && !(await showConfirm(translate(VERSAL_REPROGRAM_CONFIRM_MSG)))) {
      return;
    }
    if (action === "reconfigure-versal" &&
        !(await showConfirm(translate("Reboot the Versal device?"), { okLabel: translate("Reboot") }))) {
      return;
    }
    runAction(action, title, showTerminal);
  });
});

/* Auto-trigger action from query string (e.g. ?autorun=board-test) */
(function () {
  var params = new URLSearchParams(window.location.search);
  var auto = params.get("autorun");
  if (!auto) return;
  history.replaceState(null, "", window.location.pathname);
  var btn = document.querySelector('.menu-btn[data-action="' + CSS.escape(auto) + '"]');
  if (btn) btn.click();
})();

/* About modal */
(function () {
  var aboutBtn = document.getElementById("aboutBtn");
  var aboutModal = document.getElementById("aboutModal");
  var aboutClose = document.getElementById("aboutClose");
  if (!aboutBtn || !aboutModal) return;
  aboutBtn.addEventListener("click", function () { aboutModal.classList.remove("hidden"); });
  aboutClose.addEventListener("click", function () { aboutModal.classList.add("hidden"); });
  aboutModal.addEventListener("click", function (e) { if (e.target === aboutModal) aboutModal.classList.add("hidden"); });
})();

/* Default-boot chooser (Buddy / Ubuntu / installed app).
 * Replaces the old binary Buddy<->Ubuntu slider with an N-way single-select
 * list driven by which targets report installed (see Phase 5 §3a). */
(function () {
  var openBtn = document.getElementById("bootSettingsBtn");
  var modal = document.getElementById("bootSettingsModal");
  if (!openBtn || !modal) return;

  var listEl = document.getElementById("bootChooserList");
  var statusEl = document.getElementById("bootChooserStatus");
  var closeBtn = document.getElementById("bootSettingsClose");
  var timeoutInput = document.getElementById("bootTimeoutInput");

  /* Map a boot-default payload to the currently-selected target id. */
  function currentTarget(d) {
    var bd = d.boot_default;
    if (bd === "menu") return "buddy";
    if (bd === "ubuntu" || bd === "app") return bd;
    /* Legacy fallback: only the Ubuntu/Buddy flag is set. */
    return d.default === "ubuntu" ? "ubuntu" : "buddy";
  }

  /* Build the list of selectable targets from what is installed. */
  function targetsFor(d) {
    var t = [{ id: "buddy", label: "Buddy (menu)" }];
    if (d.installed) t.push({ id: "ubuntu", label: "Ubuntu" });
    if (d.app_installed) {
      t.push({ id: "app", label: (d.app_name || "Application").trim() || "Application" });
    }
    return t;
  }

  function renderList(d) {
    var sel = currentTarget(d);
    var targets = targetsFor(d);
    listEl.textContent = "";
    targets.forEach(function (tgt) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "boot-choice" + (tgt.id === sel ? " selected" : "");
      row.dataset.target = tgt.id;
      var bullet = document.createElement("span");
      bullet.className = "boot-choice-bullet";
      var label = document.createElement("span");
      label.className = "boot-choice-label";
      label.textContent = tgt.label;
      row.appendChild(bullet);
      row.appendChild(label);
      row.addEventListener("click", function () { apply(tgt.id, d); });
      listEl.appendChild(row);
    });
  }

  async function apply(target, d) {
    if (target === currentTarget(d)) return;
    Array.prototype.forEach.call(listEl.children, function (c) { c.disabled = true; });
    statusEl.textContent = "Applying...";
    try {
      var resp = await fetch("/api/boot-default", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: target })
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok || !data.ok) throw new Error(data.error || ("HTTP " + resp.status));
      var fresh = await loadState();
      statusEl.textContent = "Saved \u2014 applies on next boot.";
      renderList(fresh);
    } catch (err) {
      statusEl.textContent = "Could not change default boot: " + err.message;
      Array.prototype.forEach.call(listEl.children, function (c) { c.disabled = false; });
    }
  }

  async function loadState() {
    var resp = await fetch("/api/boot-default");
    return resp.json();
  }

  var savedTimeout = null;

  async function applyTimeout() {
    var val = parseInt(timeoutInput.value, 10);
    if (isNaN(val) || val < 1 || val > 3600) {
      statusEl.textContent = "Enter a countdown between 1 and 3600 seconds.";
      if (savedTimeout !== null) timeoutInput.value = String(savedTimeout);
      return;
    }
    if (val === savedTimeout) return;
    timeoutInput.disabled = true;
    statusEl.textContent = "Saving countdown...";
    try {
      var resp = await fetch("/api/boot-timeout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timeout: val })
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok || !data.ok) throw new Error(data.error || ("HTTP " + resp.status));
      savedTimeout = data.timeout;
      timeoutInput.value = String(data.timeout);
      statusEl.textContent = "Saved \u2014 menu countdown is now " + data.timeout + "s.";
    } catch (err) {
      statusEl.textContent = "Could not change countdown: " + err.message;
      if (savedTimeout !== null) timeoutInput.value = String(savedTimeout);
    } finally {
      timeoutInput.disabled = false;
    }
  }

  async function open() {
    statusEl.textContent = "Loading...";
    listEl.textContent = "";
    modal.classList.remove("hidden");
    try {
      var d = await loadState();
      renderList(d);
      if (timeoutInput && typeof d.timeout !== "undefined") {
        savedTimeout = d.timeout;
        timeoutInput.value = String(d.timeout);
      }
      statusEl.textContent = "Applies on next boot.";
    } catch (err) {
      statusEl.textContent = "Could not read boot settings: " + err.message;
    }
  }

  function close() { modal.classList.add("hidden"); }

  openBtn.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  if (timeoutInput) {
    timeoutInput.addEventListener("change", applyTimeout);
    timeoutInput.addEventListener("blur", applyTimeout);
    timeoutInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); timeoutInput.blur(); }
    });
  }
  modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
})();

/* Versal AI Edge Series Gen 2 Lab logic lives in static/lab.js.
 * Full-screen serial console lives in static/serial_console.js
 * (exposes window.openSerialConsole(portNumber)). */
