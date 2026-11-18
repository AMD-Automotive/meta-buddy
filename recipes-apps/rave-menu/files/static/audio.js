/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* Audio page – new mixer UI */
(function () {
  "use strict";

  const sinkList = document.getElementById("sinkList");
  const inputSection = document.getElementById("inputSection");
  const backendBadge = document.getElementById("audioBackend");
  const adau1860DevSection = document.getElementById("adau1860DevSection");

  let state = null;
  let loopbackActive = false;
  let pollTimer = null;
  let selectedDebugKey = null;
  // Persisted playback-pair index per input key (survives auto-refresh re-render)
  const selectedPairIdx = {};

  // --- Input registry: add new inputs here ---
  const INPUT_REGISTRY = [
    {
      key: "adau1860_dmic",
      label: "ADAU1860 DMIC Array",
      detailFn: (inp) => `${inp.channels}ch`,
      meter: {
        startUrl: "/api/audio/input/adau1860_dmic/start_metering",
        stopUrl: "/api/audio/input/adau1860_dmic/stop_metering",
        levelsUrl: "/api/audio/input/adau1860_dmic/levels",
        channelLabel: (i) => `Ch ${i + 1}`,
      },
      loopback: {
        startUrl: "/api/audio/input/adau1860_dmic/test",
        statusUrl: "/api/audio/input/adau1860_dmic/test/status",
      },
      controls: [],
    },
    {
      key: "nau8822_mic",
      label: "NAU8822 Analog Mic",
      detailFn: (inp) => `PGA: ${inp.pga_gain}/${inp.pga_gain_max}`,
      meter: {
        startUrl: "/api/audio/input/nau8822_mic/start_metering",
        stopUrl: "/api/audio/input/nau8822_mic/stop_metering",
        levelsUrl: "/api/audio/input/nau8822_mic/levels",
        channelLabel: (i) => i === 0 ? "L" : "R",
      },
      loopback: {
        startUrl: "/api/audio/input/nau8822_mic/test",
        statusUrl: "/api/audio/input/nau8822_mic/test/status",
      },
      controls: [
        { type: "slider", id: "nau8822PgaSlider", label: "PGA Gain",
          valueFn: (inp) => inp.pga_gain, maxFn: (inp) => inp.pga_gain_max,
          postUrl: "/api/audio/mic_control", postKey: "pga_gain" },
        { type: "checkbox", id: "nau8822Boost", label: "+20dB Boost",
          valueFn: (inp) => inp.boost_20db,
          postUrl: "/api/audio/mic_control", postKey: "boost_20db" },
      ],
    },
  ];

  // Per-input state
  const inputState = {};
  INPUT_REGISTRY.forEach((cfg) => {
    inputState[cfg.key] = { metering: false, meterTimer: null };
  });

  // --- API helper ---
  async function api(method, url, body) {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    let payload = null;
    try { payload = await resp.json(); } catch (_) {}
    if (!resp.ok || (payload && payload.ok === false)) {
      const err = (payload && (payload.error || payload.message)) || `HTTP ${resp.status}`;
      throw new Error(err);
    }
    return payload;
  }

  // --- Render sink cards ---
  function renderSinks(sinks) {
    sinkList.innerHTML = "";
    if (!sinks || !sinks.length) {
      sinkList.innerHTML = '<div class="audio-empty">No audio outputs detected.</div>';
      return;
    }
    sinks.forEach((sink) => {
      const card = document.createElement("div");
      card.className = "sink-card" + (sink.is_default ? " is-default" : "") +
                       (!sink.available ? " is-unavailable" : "");
      card.dataset.id = sink.id;

      const volPct = sink.available ? sink.volume_percent : 0;
      card.innerHTML = `
        <div class="sink-card-head">
          <button type="button" class="sink-default-btn ${sink.is_default ? "active" : ""}"
                  title="Set as default" ${!sink.available ? "disabled" : ""}>
            <span class="sink-default-dot"></span>
          </button>
          <div class="sink-card-name">${escHtml(sink.name)}</div>
          <button type="button" class="sink-mute-btn ${sink.muted ? "is-muted" : ""}"
                  title="${sink.muted ? "Unmute" : "Mute"}" ${!sink.available ? "disabled" : ""}>
            ${sink.muted ? muteIcon() : volumeIcon()}
          </button>
        </div>
        <div class="sink-card-slider">
          <input type="range" min="0" max="150" step="1" value="${volPct}"
                 class="vol-slider" ${!sink.available ? "disabled" : ""}>
          <span class="vol-label">${sink.available ? volPct + "%" : "—"}</span>
        </div>
      `;

      if (sink.available) {
        // Default button
        card.querySelector(".sink-default-btn").addEventListener("click", async () => {
          try {
            await api("POST", "/api/audio/set_default", { id: sink.id });
            refresh();
          } catch (e) { console.error(e); }
        });

        // Volume slider
        const slider = card.querySelector(".vol-slider");
        const label = card.querySelector(".vol-label");
        let volDebounce = null;
        slider.addEventListener("input", () => { label.textContent = slider.value + "%"; });
        slider.addEventListener("change", () => {
          clearTimeout(volDebounce);
          volDebounce = setTimeout(async () => {
            try {
              await api("POST", "/api/audio/set_volume", { id: sink.id, percent: parseInt(slider.value, 10) });
              // Play confirmation chime on that sink
              api("POST", "/api/audio/play_chime", { id: sink.id }).catch(() => {});
            } catch (e) { console.error(e); }
          }, 150);
        });

        // Mute button
        card.querySelector(".sink-mute-btn").addEventListener("click", async () => {
          try {
            await api("POST", "/api/audio/set_mute", { id: sink.id, mute: !sink.muted });
            refresh();
          } catch (e) { console.error(e); }
        });
      }

      sinkList.appendChild(card);
    });
  }

  // --- Render inputs: per-input controls + single unified debug section ---
  function renderInputs(inputs) {
    if (!inputs) {
      inputSection.innerHTML = '<div class="audio-empty">No inputs available.</div>';
      return;
    }

    // Skip full re-render while metering or loopback is active
    const anyMetering = INPUT_REGISTRY.some((cfg) => inputState[cfg.key].metering);
    if ((anyMetering || loopbackActive) && inputSection.children.length > 0) {
      return;
    }

    // Collect available inputs
    const available = INPUT_REGISTRY.filter((cfg) => {
      const inp = inputs[cfg.key];
      return inp && inp.available;
    });
    if (!available.length) {
      inputSection.innerHTML = '<div class="audio-empty">No inputs available.</div>';
      return;
    }

    let html = '';

    // Per-input controls (sliders, checkboxes)
    available.forEach((cfg) => {
      const inp = inputs[cfg.key];
      if (!cfg.controls.length) return;
      html += `<div class="input-card" id="input-card-${cfg.key}">`;
      html += `<div class="input-card-head">`;
      html += `  <span class="input-card-name">${cfg.label}</span>`;
      html += `  <span class="input-card-detail">${cfg.detailFn(inp)}</span>`;
      html += `</div>`;
      cfg.controls.forEach((ctrl) => {
        if (ctrl.type === "slider") {
          html += `<div class="input-card-slider"><label>${ctrl.label}</label>`;
          html += `<input type="range" min="0" max="${ctrl.maxFn(inp)}" step="1" value="${ctrl.valueFn(inp)}" class="vol-slider" id="${ctrl.id}">`;
          html += `<span class="vol-label">${ctrl.valueFn(inp)}</span></div>`;
        } else if (ctrl.type === "checkbox") {
          html += `<div class="input-card-toggle"><label class="toggle-row">`;
          html += `<input type="checkbox" id="${ctrl.id}" ${ctrl.valueFn(inp) ? "checked" : ""}>`;
          html += `<span>${ctrl.label}</span></label></div>`;
        }
      });
      html += `</div>`;
    });

    // --- Unified debug section ---
    const debugInputs = available.filter((cfg) => cfg.meter || cfg.loopback);
    if (debugInputs.length) {
      // Persist user's dropdown selection across refreshes
      if (!selectedDebugKey || !debugInputs.find((c) => c.key === selectedDebugKey)) {
        selectedDebugKey = debugInputs[0].key;
      }
      const selKey = selectedDebugKey;
      html += `<div class="input-debug-section" id="debug-section">`;
      html += `<div class="input-debug-header">`;
      html += `  <span class="input-debug-title">Debug</span>`;
      html += `  <select id="debug-input-select" class="debug-select">`;
      debugInputs.forEach((cfg) => {
        html += `    <option value="${cfg.key}" ${cfg.key === selKey ? "selected" : ""}>${cfg.label}</option>`;
      });
      html += `  </select>`;
      html += `</div>`;

      // Metering: meters always visible (one set per input, toggled by selector)
      html += `<div class="debug-meter-section">`;
      html += `  <div class="debug-btn-row">`;
      html += `    <button type="button" id="debug-meter-btn" class="net-btn net-btn-primary btn-sm">Start Metering</button>`;
      html += `    <button type="button" id="debug-loopback-btn" class="net-btn net-btn-secondary btn-sm">Record &amp; Playback</button>`;
      html += `  </div>`;

      // Fixed-pair playback selector (multi-channel inputs only)
      debugInputs.forEach((cfg) => {
        const inp = inputs[cfg.key];
        const pairs = inp.record_pairs;
        if (!pairs || pairs.length < 2) return;
        const labels = inp.channel_labels || [];
        const isSelected = cfg.key === selKey;
        html += `<div class="debug-pair-group ${isSelected ? "" : "hidden"}" id="pair-group-${cfg.key}">`;
        html += `  <label class="debug-pair-label">Playback pair</label>`;
        html += `  <select class="debug-select debug-pair-select" id="pair-select-${cfg.key}">`;
        const selIdx = selectedPairIdx[cfg.key] != null ? selectedPairIdx[cfg.key] : 0;
        pairs.forEach((p, idx) => {
          const la = labels[p[0]] != null ? labels[p[0]] : `Ch ${p[0] + 1}`;
          const lb = labels[p[1]] != null ? labels[p[1]] : `Ch ${p[1] + 1}`;
          html += `<option value="${idx}" ${idx === selIdx ? "selected" : ""}>${la} &amp; ${lb}</option>`;
        });
        html += `  </select>`;
        html += `</div>`;
      });

      debugInputs.forEach((cfg) => {
        const inp = inputs[cfg.key];
        const chCount = inp.channels || 2;
        const isSelected = cfg.key === selKey;
        html += `<div class="debug-meter-group ${isSelected ? "" : "hidden"}" id="meter-group-${cfg.key}">`;
        html += `  <div id="meters-${cfg.key}" class="dmic-meters">`;
        // Render empty/idle meter bars
        for (let i = 0; i < chCount; i++) {
          html += `<div class="dmic-channel">`;
          html += `  <span class="dmic-ch-label">${cfg.meter.channelLabel(i)}</span>`;
          html += `  <div class="dmic-bar-bg"><div class="dmic-bar" style="width:0%"></div><div class="dmic-peak" style="left:0%"></div></div>`;
          html += `  <span class="dmic-db">-\u221e dB</span>`;
          html += `</div>`;
        }
        html += `  </div>`;
        html += `</div>`;
      });
      html += `  <div id="debug-loopback-status" class="loopback-status-area"></div>`;
      html += `</div>`;

      html += `</div>`; // .input-debug-section
    }

    inputSection.innerHTML = html;
    bindInputControls(inputs, available, debugInputs);
  }

  function bindInputControls(inputs, available, debugInputs) {
    // Controls (sliders, checkboxes)
    available.forEach((cfg) => {
      cfg.controls.forEach((ctrl) => {
        const el = document.getElementById(ctrl.id);
        if (!el) return;
        if (ctrl.type === "slider") {
          const label = el.parentElement.querySelector(".vol-label");
          el.addEventListener("input", () => { label.textContent = el.value; });
          el.addEventListener("change", async () => {
            try {
              await api("POST", ctrl.postUrl, { control: ctrl.postKey, value: parseInt(el.value, 10) });
            } catch (e) { console.error(e); }
          });
        } else if (ctrl.type === "checkbox") {
          el.addEventListener("change", async () => {
            try {
              await api("POST", ctrl.postUrl, { control: ctrl.postKey, value: el.checked });
            } catch (e) { console.error(e); }
          });
        }
      });
    });

    if (!debugInputs || !debugInputs.length) return;

    // Persist playback-pair selection per input across auto-refresh re-renders
    debugInputs.forEach((cfg) => {
      const ps = document.getElementById(`pair-select-${cfg.key}`);
      if (ps) {
        ps.addEventListener("change", () => {
          selectedPairIdx[cfg.key] = parseInt(ps.value, 10);
        });
      }
    });

    // Debug input selector — switch visible meters
    const sel = document.getElementById("debug-input-select");
    if (sel) {
      sel.addEventListener("change", () => {
        selectedDebugKey = sel.value;
        debugInputs.forEach((cfg) => {
          const g = document.getElementById(`meter-group-${cfg.key}`);
          if (g) g.classList.toggle("hidden", cfg.key !== sel.value);
          const pg = document.getElementById(`pair-group-${cfg.key}`);
          if (pg) pg.classList.toggle("hidden", cfg.key !== sel.value);
        });
        updateDebugMeterBtn();
      });
    }

    // Meter button
    const meterBtn = document.getElementById("debug-meter-btn");
    if (meterBtn) {
      meterBtn.addEventListener("click", () => {
        const key = sel ? sel.value : debugInputs[0].key;
        toggleMeter(key);
      });
    }

    // Loopback button
    const lbBtn = document.getElementById("debug-loopback-btn");
    if (lbBtn) {
      lbBtn.addEventListener("click", () => {
        const key = sel ? sel.value : debugInputs[0].key;
        runLoopbackTest(key);
      });
    }

    updateDebugMeterBtn();
  }

  function updateDebugMeterBtn() {
    const sel = document.getElementById("debug-input-select");
    const btn = document.getElementById("debug-meter-btn");
    if (!sel || !btn) return;
    const key = sel.value;
    const st = inputState[key];
    if (st && st.metering) {
      btn.textContent = "Stop Metering";
      btn.classList.replace("net-btn-primary", "net-btn-warning");
    } else {
      btn.textContent = "Start Metering";
      btn.classList.replace("net-btn-warning", "net-btn-primary");
    }
  }

  // --- Per-input metering toggle ---
  async function toggleMeter(key) {
    const cfg = INPUT_REGISTRY.find((c) => c.key === key);
    if (!cfg || !cfg.meter) return;
    const st = inputState[key];
    try {
      if (!st.metering) {
        await api("POST", cfg.meter.startUrl);
        st.metering = true;
        startMeterTimer(key);
      } else {
        await api("POST", cfg.meter.stopUrl);
        st.metering = false;
        stopMeterTimer(key);
      }
      updateDebugMeterBtn();
    } catch (e) { console.error(e); }
  }

  function startMeterTimer(key) {
    const st = inputState[key];
    if (st.meterTimer) return;
    st.meterTimer = setInterval(() => pollLevels(key), 100);
  }

  function stopMeterTimer(key) {
    const st = inputState[key];
    if (st.meterTimer) { clearInterval(st.meterTimer); st.meterTimer = null; }
  }

  async function pollLevels(key) {
    const cfg = INPUT_REGISTRY.find((c) => c.key === key);
    if (!cfg || !inputState[key].metering) return;
    try {
      const data = await api("GET", cfg.meter.levelsUrl);
      const container = document.getElementById(`meters-${key}`);
      if (!container) return;
      container.classList.remove("hidden");
      const channels = data.details || [];

      const existing = container.querySelectorAll(".dmic-channel");
      if (existing.length === channels.length) {
        channels.forEach((ch, i) => {
          const rmsDb = ch.db != null ? ch.db : -96;
          const peakDb = ch.peak_db != null ? ch.peak_db : -96;
          const rmsWidth = Math.max(0, Math.min(100, (rmsDb + 60) / 60 * 100));
          const peakLeft = Math.max(0, Math.min(100, (peakDb + 60) / 60 * 100));
          existing[i].querySelector(".dmic-bar").style.width = rmsWidth + "%";
          existing[i].querySelector(".dmic-peak").style.left = peakLeft + "%";
          existing[i].querySelector(".dmic-db").textContent =
            rmsDb > -60 ? rmsDb.toFixed(0) + " dB" : "-\u221e dB";
        });
      } else {
        container.innerHTML = channels.map((ch, i) => {
          const rmsDb = ch.db != null ? ch.db : -96;
          const peakDb = ch.peak_db != null ? ch.peak_db : -96;
          const rmsWidth = Math.max(0, Math.min(100, (rmsDb + 60) / 60 * 100));
          const peakLeft = Math.max(0, Math.min(100, (peakDb + 60) / 60 * 100));
          return `
          <div class="dmic-channel">
            <span class="dmic-ch-label">${cfg.meter.channelLabel(i)}</span>
            <div class="dmic-bar-bg">
              <div class="dmic-bar" style="width:${rmsWidth}%"></div>
              <div class="dmic-peak" style="left:${peakLeft}%"></div>
            </div>
            <span class="dmic-db">${rmsDb > -60 ? rmsDb.toFixed(0) : "-\u221e"} dB</span>
          </div>`;
        }).join("");
      }
    } catch (_) {}
  }

  // --- Loopback test with progress bar ---
  function runLoopbackTest(key) {
    const cfg = INPUT_REGISTRY.find((c) => c.key === key);
    if (!cfg || !cfg.loopback) return;

    // Resolve the selected fixed channel pair, if any.
    let pair = null;
    const inp = state && state.inputs ? state.inputs[key] : null;
    const pairSel = document.getElementById(`pair-select-${key}`);
    if (pairSel && inp && inp.record_pairs) {
      pair = inp.record_pairs[parseInt(pairSel.value, 10)] || null;
    }

    const container = document.getElementById("debug-loopback-status");
    if (!container) return;
    const btn = document.getElementById("debug-loopback-btn");
    if (btn) btn.disabled = true;
    loopbackActive = true;

    container.innerHTML = `
      <div class="loopback-progress">
        <span class="loopback-phase">Preparing…</span>
        <div class="loopback-bar-bg"><div class="loopback-bar" style="width:0%"></div></div>
      </div>`;

    const phaseEl = container.querySelector(".loopback-phase");
    const barEl = container.querySelector(".loopback-bar");

    api("POST", cfg.loopback.startUrl, pair ? { pair } : undefined).then(() => {
      let phase = "";

      const tick = setInterval(async () => {
        try {
          const s = await api("GET", cfg.loopback.statusUrl);

          if (s.status === "starting") {
            if (phaseEl) phaseEl.textContent = "Preparing…";
          } else if (s.status === "recording") {
            if (phase !== "recording") {
              phase = "recording";
              if (phaseEl) phaseEl.textContent = "Recording… speak now";
              if (barEl) { barEl.style.width = "0%"; barEl.classList.remove("playback"); }
            }
            const pct = Math.min(100, (s.progress / 50) * 100);
            if (barEl) barEl.style.width = pct + "%";
          } else if (s.status === "playing") {
            if (phase !== "playing") {
              phase = "playing";
              if (phaseEl) phaseEl.textContent = "Playing back…";
              if (barEl) { barEl.style.width = "0%"; barEl.classList.add("playback"); }
            }
            const pct = Math.min(100, ((s.progress - 50) / 50) * 100);
            if (barEl) barEl.style.width = pct + "%";
          } else if (s.status === "done" || s.status === "idle") {
            clearInterval(tick);
            if (phaseEl) phaseEl.textContent = "Done ✓";
            if (barEl) barEl.style.width = "100%";
            if (btn) btn.disabled = false;
            setTimeout(() => { container.innerHTML = ""; loopbackActive = false; }, 3000);
          } else if (s.status === "error") {
            clearInterval(tick);
            container.innerHTML = `<span class="loopback-error">Error: ${s.error || "unknown"}</span>`;
            if (btn) btn.disabled = false;
            setTimeout(() => { container.innerHTML = ""; loopbackActive = false; }, 5000);
          }
        } catch (_) {
          clearInterval(tick);
          container.innerHTML = "";
          if (btn) btn.disabled = false;
          loopbackActive = false;
        }
      }, 200);
    }).catch((e) => {
      container.innerHTML = `<span class="loopback-error">Error: ${e.message}</span>`;
      if (btn) btn.disabled = false;
      loopbackActive = false;
    });
  }

  // --- Main refresh ---
  async function refresh() {
    try {
      state = await api("GET", "/api/audio/state");
      backendBadge.textContent = state.backend || "?";
      backendBadge.className = "audio-backend-badge badge-" + (state.backend || "unknown");
      // Sync metering state from server
      if (state.inputs) {
        INPUT_REGISTRY.forEach((cfg) => {
          const inp = state.inputs[cfg.key];
          if (!inp) return;
          const st = inputState[cfg.key];
          const wasActive = st.metering;
          st.metering = inp.metering_active || false;
          if (st.metering && !wasActive) startMeterTimer(cfg.key);
          else if (!st.metering && wasActive) stopMeterTimer(cfg.key);
        });
      }
      renderSinks(state.sinks);
      renderInputs(state.inputs);
      // Show/hide ADAU1860 developer panel based on codec presence
      if (adau1860DevSection) {
        try {
          const adauStatus = await api("GET", "/api/audio/adau1860/status");
          adau1860DevSection.style.display = adauStatus.loaded ? "" : "none";
        } catch (_) {
          adau1860DevSection.style.display = "none";
        }
      }
    } catch (e) {
      console.error("refresh error:", e);
    }
  }

  // --- Icons ---
  function volumeIcon() {
    return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.08"/>
    </svg>`;
  }
  function muteIcon() {
    return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M11 5L6 9H2v6h4l5 4V5z"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>
    </svg>`;
  }
  function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // --- Poll loop ---
  refresh();
  pollTimer = setInterval(refresh, 2000);
})();
