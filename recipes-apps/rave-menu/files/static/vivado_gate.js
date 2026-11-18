/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* vivado_gate.js — gate Versal lab / board-test / app-store on Vivado install.
 *
 * Loaded after app.js on the main menu page. Fetches /api/vivado/status once
 * on load, then intercepts clicks on actions that require the lab tools.
 * If not installed, the user is offered to jump to /vivado-install instead.
 *
 * `installed` from that endpoint means "some usable release is present", not
 * specifically the base one. Gating on the base release alone would lock a
 * board that only has a newer release out of the lab card — which is where
 * the toolchain selector lives, so the gate would hide its own remedy.
 *
 * Kept entirely in this file so the gating logic doesn't bleed into app.js.
 */
(function () {
  "use strict";

  // Actions that cannot run without a usable <prefix>/<version>/.../hw_server.
  var GATED = {
    "board-test": true,
    "app-store": true,
    "versal-lab": true,
    "run-board-test-page": true,
    "reconfigure-versal": true,
  };

  var installed = null;  // tri-state: null=unknown, true/false once known
  var offerVersion = "";

  function setBadge(state) {
    // Optional visual marker on gated buttons. Light hint only -- the click
    // intercept is the real enforcement.
    document.querySelectorAll(".menu-btn[data-action]").forEach(function (btn) {
      var a = btn.getAttribute("data-action");
      if (!GATED[a]) return;
      btn.classList.toggle("vi-needs-install", state === false);
    });
  }

  function fetchStatus() {
    return fetch("/api/vivado/status").then(function (r) { return r.json(); })
      .then(function (j) {
        installed = !!j.installed;
        // Nothing installed at all, so steer the user at the default release.
        offerVersion = j.default_version || j.base_version || "";
        setBadge(installed);
        return installed;
      })
      .catch(function () { installed = null; });
  }

  function offerInstall() {
    var msg = "Vivado HW Server " + (offerVersion ? offerVersion + " " : "") +
      "is required for this feature. Install it now?";
    var url = "/vivado-install" +
      (offerVersion ? "?version=" + encodeURIComponent(offerVersion) : "");
    if (typeof window.showConfirm === "function") {
      window.showConfirm(msg, { okLabel: "Install", cancelLabel: "Not now", title: "Lab tools required" })
        .then(function (ok) { if (ok) window.location.assign(url); });
    } else if (window.confirm(msg)) {
      window.location.assign(url);
    }
  }

  // Capture-phase listener so we run *before* the menu button's own handler
  // in app.js / lab.js. If we stop propagation when blocked, the original
  // handler never fires.
  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest && ev.target.closest(".menu-btn[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    if (!GATED[action]) return;
    if (installed === true) return;          // tools present — let it through
    if (installed === null) return;          // unknown — fail open
    ev.stopPropagation();
    ev.preventDefault();
    offerInstall();
  }, true);

  // Versal programming forms: the main-menu one plus both tabs inside the
  // lab modal. Belt-and-braces behind the click gate above.
  ["customUrlForm", "labCustomUrlForm", "labJtagForm"].forEach(function (id) {
    var form = document.getElementById(id);
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      if (installed === false) {
        ev.preventDefault(); ev.stopPropagation(); offerInstall();
      }
    }, true);
  });

  fetchStatus();
})();
