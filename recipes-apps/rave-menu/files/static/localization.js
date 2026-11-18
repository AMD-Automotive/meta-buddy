// Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
// SPDX-License-Identifier: MIT
// Contributors: KC
// Maintainer: KC

(function () {
  "use strict";

  var state = { current: null, shortlist: [], all: null, locales: [] };

  // Supplied by base.html from the server-side catalogue; falls back to the
  // key itself, which is the English source string.
  function t(key) {
    return (window.t ? window.t(key) : key);
  }

  function fill(key, values) {
    return Object.keys(values).reduce(function (out, name) {
      return out.replace("{" + name + "}", values[name]);
    }, t(key));
  }

  function $(id) { return document.getElementById(id); }

  function showError(message) {
    var box = $("locError");
    if (!message) { box.hidden = true; box.textContent = ""; return; }
    box.hidden = false;
    box.textContent = message;
  }

  function api(path, body) {
    var opts = { method: body ? "POST" : "GET", headers: {} };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok || data.ok === false) {
          throw new Error(data.error || (t("request failed") + " (" + resp.status + ")"));
        }
        return data;
      });
    });
  }

  function option(value, label) {
    var el = document.createElement("option");
    el.value = value;
    el.textContent = label;
    return el;
  }

  function fillVariants(layoutCode) {
    var select = $("variantSelect");
    select.innerHTML = "";
    select.appendChild(option("", t("Default")));

    var source = (state.all || state.shortlist).filter(function (l) {
      return l.code === layoutCode;
    })[0];
    if (!source) { return; }
    source.variants.forEach(function (v) {
      select.appendChild(option(v.code, v.name));
    });
    if (state.current && state.current.keyboard.layout === layoutCode) {
      select.value = state.current.keyboard.variant || "";
    }
  }

  function renderShortlist() {
    var select = $("layoutSelect");
    select.innerHTML = "";
    state.shortlist.forEach(function (l) {
      select.appendChild(option(l.code, l.name));
    });

    var active = state.current.keyboard.layout;
    var known = state.shortlist.some(function (l) { return l.code === active; });
    // The active layout may have been chosen from the full list, in which case
    // it is not in the shortlist and must still be shown as selected.
    if (!known) {
      select.appendChild(option(active, active));
    }
    select.value = active;
    fillVariants(active);
  }

  function renderLocales() {
    var select = $("languageSelect");
    select.innerHTML = "";
    state.locales.forEach(function (l) {
      var label = l.installed ? l.name : l.name + " " + t("(not installed)");
      var el = option(l.code, label);
      el.disabled = !l.installed;
      select.appendChild(el);
    });
    select.value = state.current.language;
  }

  function renderSearchResults(term) {
    var box = $("layoutResults");
    box.innerHTML = "";
    if (!state.all) { return; }
    var needle = term.trim().toLowerCase();
    var matches = state.all.filter(function (l) {
      return !needle || l.name.toLowerCase().indexOf(needle) !== -1 ||
             l.code.toLowerCase().indexOf(needle) !== -1;
    }).slice(0, 80);

    matches.forEach(function (l) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "loc-result";
      row.textContent = l.name + "  (" + l.code + ")";
      row.addEventListener("click", function () {
        var select = $("layoutSelect");
        if (!Array.prototype.some.call(select.options, function (o) {
          return o.value === l.code;
        })) {
          select.appendChild(option(l.code, l.name));
        }
        select.value = l.code;
        fillVariants(l.code);
        $("allWrap").hidden = true;
      });
      box.appendChild(row);
    });
    if (!matches.length) {
      box.textContent = t("No matching layouts");
    }
  }

  function applyState(data) {
    state.current = data.current;
    state.shortlist = data.shortlist;
    state.locales = data.locales;
    renderShortlist();
    renderLocales();
    $("activeLayout").textContent =
      data.current.keyboard.layout +
      (data.current.keyboard.variant ? " / " + data.current.keyboard.variant : "");
  }

  var countdownTimer = null;

  function startConfirm(seconds) {
    var bar = $("confirmBar");
    bar.hidden = false;
    var remaining = seconds;
    var label = $("confirmCountdown");

    function tick() {
      label.textContent = remaining > 0
        ? fill("Reverting automatically in {n}s.", { n: remaining })
        : t("Reverting…");
      if (remaining <= 0) { clearInterval(countdownTimer); return; }
      remaining -= 1;
    }
    tick();
    clearInterval(countdownTimer);
    countdownTimer = setInterval(tick, 1000);
    $("layoutTest").focus();
  }

  function stopConfirm() {
    clearInterval(countdownTimer);
    $("confirmBar").hidden = true;
  }

  function load() {
    return api("/api/localization").then(applyState).catch(function (err) {
      showError(err.message);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    load().then(function () {
      // Arriving with ?verify=1 means the compositor has just restarted for us,
      // so the revert timer is already running on the device.
      if (window.RAVE_LOC_VERIFY) { startConfirm(60); }
    });

    $("layoutSelect").addEventListener("change", function () {
      fillVariants(this.value);
    });

    $("showAll").addEventListener("click", function () {
      var wrap = $("allWrap");
      wrap.hidden = !wrap.hidden;
      if (wrap.hidden) { return; }
      $("layoutSearch").focus();
      if (state.all) { renderSearchResults(""); return; }
      // Fetched on demand: the full catalogue is ~100 layouts with several
      // hundred variants and has no business in every page load.
      api("/api/localization/layouts").then(function (data) {
        state.all = data.layouts;
        renderSearchResults("");
      }).catch(function (err) { showError(err.message); });
    });

    $("layoutSearch").addEventListener("input", function () {
      renderSearchResults(this.value);
    });

    $("applyKeyboard").addEventListener("click", function () {
      showError("");
      var layout = $("layoutSelect").value;
      if (!window.confirm(
          fill("Apply the {layout} keyboard layout?", { layout: layout }) + "\n\n" +
          fill("The display on the appliance will go blank for a few seconds. " +
               "If the new layout is unusable it reverts automatically after {n} seconds.",
               { n: 60 }))) {
        return;
      }
      this.disabled = true;
      api("/api/localization/keyboard", {
        layout: layout,
        variant: $("variantSelect").value
      }).then(function (data) {
        applyState(data);
        if (data.changed && data.restarting) {
          startConfirm(data.revert_seconds || 60);
        }
      }).catch(function (err) { showError(err.message); })
        .finally(function () { $("applyKeyboard").disabled = false; });
    });

    $("applyLanguage").addEventListener("click", function () {
      showError("");
      this.disabled = true;
      api("/api/localization/language", {
        language: $("languageSelect").value
      }).then(applyState)
        .catch(function (err) { showError(err.message); })
        .finally(function () { $("applyLanguage").disabled = false; });
    });

    $("confirmKeep").addEventListener("click", function () {
      api("/api/localization/confirm", {}).then(function (data) {
        stopConfirm();
        applyState(data);
      }).catch(function (err) { showError(err.message); });
    });

    $("confirmRevert").addEventListener("click", function () {
      api("/api/localization/revert", {}).then(function (data) {
        stopConfirm();
        applyState(data);
      }).catch(function (err) { showError(err.message); });
    });
  });
})();
