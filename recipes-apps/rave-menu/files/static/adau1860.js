/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* ADAU1860 Codec Control page logic. */

(function () {
  "use strict";

  // --- DOM Elements ---
  const statusEl = document.getElementById("codecStatus");
  const refreshBtn = document.getElementById("codecRefresh");

  // Status cards
  const pllStatus = document.getElementById("pllStatus");
  const vendorId = document.getElementById("vendorId");
  const chipId = document.getElementById("chipId");
  const status2El = document.getElementById("status2");
  const cardPll = document.getElementById("cardPll");

  // Volume controls
  const dacVolSlider = document.getElementById("dacVolSlider");
  const dacVolDb = document.getElementById("dacVolDb");
  const dacVolRaw = document.getElementById("dacVolRaw");
  const dacMuteBtn = document.getElementById("dacMuteBtn");
  const dacBoostBtn = document.getElementById("dacBoostBtn");

  // Output mode
  const modeHeadphone = document.getElementById("modeHeadphone");
  const modeLineout = document.getElementById("modeLineout");

  // ASRC
  const asrcBypass = document.getElementById("asrcBypass");
  const asrcEnable = document.getElementById("asrcEnable");
  const asrcStatus = document.getElementById("asrcStatus");

  // DMIC
  const dmicEnabled = document.getElementById("dmicEnabled");
  const dmicSlots = [
    document.getElementById("dmicSlot0"),
    document.getElementById("dmicSlot1"),
    document.getElementById("dmicSlot2"),
    document.getElementById("dmicSlot3"),
  ];

  // Power states
  const pwrChip = document.getElementById("pwrChip");
  const pwrDac = document.getElementById("pwrDac");
  const pwrHp = document.getElementById("pwrHp");
  const pwrDsp = document.getElementById("pwrDsp");
  const pwrAsrc = document.getElementById("pwrAsrc");
  const clkCtrl1 = document.getElementById("clkCtrl1");

  // Register access
  const regAddr = document.getElementById("regAddr");
  const regValue = document.getElementById("regValue");
  const regReadBtn = document.getElementById("regReadBtn");
  const regWriteBtn = document.getElementById("regWriteBtn");
  const regResult = document.getElementById("regResult");

  // Reinit
  const reinitBtn = document.getElementById("reinitBtn");

  // --- State ---
  let currentState = null;
  let isMuted = false;

  // --- Helpers ---
  function setStatus(msg) {
    statusEl.textContent = msg;
  }

  async function api(method, url, body) {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    let payload = null;
    try { payload = await resp.json(); } catch (_) { payload = null; }
    if (!resp.ok || (payload && payload.ok === false)) {
      const err = (payload && (payload.error || payload.message)) || `HTTP ${resp.status}`;
      throw new Error(err);
    }
    return payload;
  }

  function hex(val) {
    if (val === null || val === undefined) return "--";
    return "0x" + val.toString(16).toUpperCase().padStart(2, "0");
  }

  // --- Render ---
  function renderStatus(snap) {
    const st = snap.status || {};
    const dmic = snap.dmic || {};

    // Check availability
    if (!st.available) {
      setStatus(st.error || "Codec not available");
      cardPll.classList.add("status-error");
      pllStatus.textContent = "N/A";
      vendorId.textContent = st.error || "Not found";
      return;
    }

    // PLL
    if (st.pll_locked) {
      pllStatus.textContent = "Locked";
      cardPll.classList.remove("status-error", "status-warn");
      cardPll.classList.add("status-ok");
    } else {
      pllStatus.textContent = "Unlocked";
      cardPll.classList.remove("status-ok");
      cardPll.classList.add("status-warn");
    }

    // IDs
    vendorId.textContent = hex(st.vendor_id);
    chipId.textContent = hex(st.chip_id);
    status2El.textContent = hex(st.status2);

    // Volume
    if (st.dac_vol_db !== null) {
      const db = st.dac_vol_db;
      // Map dB to slider: 0% = -71.25dB, 100% = 0dB
      const pct = Math.round(((db + 71.25) / 71.25) * 100);
      dacVolSlider.value = Math.max(0, Math.min(100, pct));
      dacVolDb.textContent = db.toFixed(1) + " dB";
      dacVolRaw.textContent = "(" + hex(st.dac_vol_raw) + ")";
    }

    // Mute state
    isMuted = st.dac_muted || false;
    dacMuteBtn.textContent = isMuted ? "Unmute" : "Mute";
    dacMuteBtn.classList.toggle("is-muted", isMuted);

    // Output mode
    const isLineout = st.output_mode === "lineout";
    modeHeadphone.classList.toggle("mode-active", !isLineout);
    modeLineout.classList.toggle("mode-active", isLineout);

    // ASRC
    const asrcOn = st.asrc_pwr && (st.asrc_pwr & 0x01);
    asrcBypass.classList.toggle("mode-active", !asrcOn);
    asrcEnable.classList.toggle("mode-active", asrcOn);
    asrcStatus.textContent = asrcOn ? "Enabled" : "Bypassed (Direct)";

    // DMIC
    dmicEnabled.textContent = dmic.enabled ? "Enabled" : "Disabled";
    dmicEnabled.classList.toggle("dmic-on", dmic.enabled);
    if (dmic.routes) {
      dmic.routes.forEach((r, i) => {
        if (dmicSlots[i]) {
          dmicSlots[i].textContent = r.source;
        }
      });
    }

    // Power states
    pwrChip.textContent = hex(st.chip_pwr);
    pwrDac.textContent = hex(st.dac_pwr);
    pwrHp.textContent = hex(st.hp_pwr);
    pwrDsp.textContent = hex(st.dsp_pwr);
    pwrAsrc.textContent = hex(st.asrc_pwr);
    clkCtrl1.textContent = hex(st.clk_ctrl1);

    currentState = snap;
  }

  async function refresh() {
    try {
      setStatus("Refreshing…");
      const snap = await api("GET", "/api/adau1860/snapshot");
      renderStatus(snap);
      setStatus("Ready");
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  }

  // --- Event Handlers ---

  // Refresh button
  refreshBtn.addEventListener("click", refresh);

  // Volume slider
  let volTimer = null;
  dacVolSlider.addEventListener("input", () => {
    // Live preview
    const pct = parseInt(dacVolSlider.value, 10);
    const db = -71.25 + (pct / 100) * 71.25;
    dacVolDb.textContent = db.toFixed(1) + " dB";
  });
  dacVolSlider.addEventListener("change", () => {
    clearTimeout(volTimer);
    volTimer = setTimeout(async () => {
      const pct = parseInt(dacVolSlider.value, 10);
      try {
        await api("POST", "/api/adau1860/volume", { percent: pct });
        setStatus("Volume updated");
        refresh();
      } catch (e) {
        setStatus("Error: " + e.message);
      }
    }, 100);
  });

  // Mute button
  dacMuteBtn.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/mute", { mute: !isMuted });
      setStatus(isMuted ? "Unmuted" : "Muted");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });

  // Boost button (+24 dB)
  dacBoostBtn.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/volume", { db: 24 });
      setStatus("Boost: +24 dB");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });

  // Output mode buttons
  modeHeadphone.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/output_mode", { mode: "headphone" });
      setStatus("Headphone mode");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });
  modeLineout.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/output_mode", { mode: "lineout" });
      setStatus("Line-out mode");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });

  // ASRC buttons
  asrcBypass.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/asrc", { bypass: true });
      setStatus("ASRC bypassed");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });
  asrcEnable.addEventListener("click", async () => {
    try {
      await api("POST", "/api/adau1860/asrc", { bypass: false });
      setStatus("ASRC enabled");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });

  // Register read
  regReadBtn.addEventListener("click", async () => {
    const addr = regAddr.value.trim();
    if (!addr) {
      regResult.textContent = "Enter an address";
      return;
    }
    try {
      const resp = await api("GET", "/api/adau1860/register/" + encodeURIComponent(addr));
      regResult.textContent = resp.message || `Value: ${hex(resp.value)}`;
      regResult.classList.remove("reg-error");
    } catch (e) {
      regResult.textContent = "Error: " + e.message;
      regResult.classList.add("reg-error");
    }
  });

  // Register write
  regWriteBtn.addEventListener("click", async () => {
    const addr = regAddr.value.trim();
    const val = regValue.value.trim();
    if (!addr || !val) {
      regResult.textContent = "Enter both address and value";
      return;
    }
    try {
      const addrInt = parseInt(addr, 0);
      const valInt = parseInt(val, 0);
      const resp = await api("POST", "/api/adau1860/register", { addr: addrInt, value: valInt });
      regResult.textContent = resp.message || "Write OK";
      regResult.classList.remove("reg-error");
      refresh();
    } catch (e) {
      regResult.textContent = "Error: " + e.message;
      regResult.classList.add("reg-error");
    }
  });

  // Reinit button
  reinitBtn.addEventListener("click", async () => {
    if (!confirm("Reinitialize the codec? This will reapply all configuration.")) {
      return;
    }
    try {
      setStatus("Reinitializing…");
      const resp = await api("POST", "/api/adau1860/reinit");
      setStatus(resp.message || "Reinitialized");
      refresh();
    } catch (e) {
      setStatus("Error: " + e.message);
    }
  });

  // --- DMIC Level Metering ---
  const dmicMeterEnabled = document.getElementById("dmicMeterEnabled");
  const meterError = document.getElementById("meterError");
  const meterBars = [
    document.getElementById("meterBar0"),
    document.getElementById("meterBar1"),
  ];
  const meterPeaks = [
    document.getElementById("meterPeak0"),
    document.getElementById("meterPeak1"),
  ];
  const meterDbs = [
    document.getElementById("meterDb0"),
    document.getElementById("meterDb1"),
  ];
  const meterPeakDbs = [
    document.getElementById("meterPeakDb0"),
    document.getElementById("meterPeakDb1"),
  ];

  let meterInterval = null;
  let peakHold = [0, 0];  // Peak hold values (dB)
  let peakDecay = 0.5;  // dB per update to decay peak

  function dbToPercent(db) {
    // Map -96 dB to 0%, 0 dB to 100%
    const pct = ((db + 96) / 96) * 100;
    return Math.max(0, Math.min(100, pct));
  }

  function getLevelClass(db) {
    if (db > -3) return "level-clip";
    if (db > -12) return "level-high";
    if (db > -30) return "level-mid";
    return "level-low";
  }

  function updateMeters(data) {
    if (!data.ok) {
      meterError.textContent = data.error || "Capture failed";
      return;
    }
    meterError.textContent = "";

    const channels = data.channels || [];
    channels.forEach((ch, i) => {
      if (i >= 2) return;

      const db = ch.db;
      const peakDb = ch.peak_db;
      const pct = dbToPercent(db);
      const peakPct = dbToPercent(peakDb);

      // Update peak hold (decay slowly)
      if (peakDb > peakHold[i]) {
        peakHold[i] = peakDb;
      } else {
        peakHold[i] = Math.max(peakDb, peakHold[i] - peakDecay);
      }
      const holdPct = dbToPercent(peakHold[i]);

      // Update bar
      if (meterBars[i]) {
        meterBars[i].style.width = pct + "%";
        meterBars[i].className = "meter-bar " + getLevelClass(db);
      }

      // Update peak indicator
      if (meterPeaks[i]) {
        meterPeaks[i].style.left = holdPct + "%";
      }

      // Update dB readout
      if (meterDbs[i]) {
        meterDbs[i].textContent = db.toFixed(1) + " dB";
      }

      // Update peak dB
      if (meterPeakDbs[i]) {
        meterPeakDbs[i].textContent = "pk " + peakHold[i].toFixed(1);
      }
    });
  }

  async function fetchMeterLevels() {
    try {
      const data = await api("GET", "/api/adau1860/dmic_levels");
      updateMeters(data);
    } catch (e) {
      meterError.textContent = "Error: " + e.message;
    }
  }

  async function startMetering() {
    if (meterInterval) return;
    peakHold = [-96, -96];  // Reset peaks
    meterError.textContent = "Starting...";

    try {
      // Start the streaming capture on the server
      await api("POST", "/api/adau1860/dmic_meter/start");
      // Poll for levels at ~30 Hz (server calculates in background)
      meterInterval = setInterval(fetchMeterLevels, 33);
      fetchMeterLevels();
      meterError.textContent = "";
    } catch (e) {
      meterError.textContent = "Failed to start: " + e.message;
      if (dmicMeterEnabled) dmicMeterEnabled.checked = false;
    }
  }

  async function stopMetering() {
    if (meterInterval) {
      clearInterval(meterInterval);
      meterInterval = null;
    }
    // Stop the streaming capture on the server
    try {
      await api("POST", "/api/adau1860/dmic_meter/stop");
    } catch (_) {}

    // Reset meters to idle state
    for (let i = 0; i < 2; i++) {
      if (meterBars[i]) meterBars[i].style.width = "0%";
      if (meterPeaks[i]) meterPeaks[i].style.left = "0%";
      if (meterDbs[i]) meterDbs[i].textContent = "-- dB";
      if (meterPeakDbs[i]) meterPeakDbs[i].textContent = "pk --";
    }
    meterError.textContent = "";
  }

  // Meter toggle checkbox
  if (dmicMeterEnabled) {
    dmicMeterEnabled.addEventListener("change", () => {
      if (dmicMeterEnabled.checked) {
        startMetering();
      } else {
        stopMetering();
      }
    });
  }

  // Stop metering when page is hidden
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && meterInterval) {
      stopMetering();
      if (dmicMeterEnabled) dmicMeterEnabled.checked = false;
    }
  });

  // --- Loopback Test ---
  const loopbackBtn = document.getElementById("loopbackBtn");
  const loopbackStatus = document.getElementById("loopbackStatus");
  const loopbackProgress = document.getElementById("loopbackProgress");
  const loopbackState = document.getElementById("loopbackState");
  let loopbackPollInterval = null;

  async function startLoopbackTest() {
    if (loopbackPollInterval) return;

    try {
      loopbackBtn.disabled = true;
      loopbackStatus.classList.add("active");
      loopbackState.textContent = "Starting...";
      loopbackState.classList.remove("error");

      const resp = await api("POST", "/api/adau1860/loopback/start");
      if (!resp.ok) {
        throw new Error(resp.error || "Failed to start");
      }

      // Poll for status
      loopbackPollInterval = setInterval(pollLoopbackStatus, 100);

    } catch (e) {
      loopbackState.textContent = "Error: " + e.message;
      loopbackState.classList.add("error");
      loopbackBtn.disabled = false;
    }
  }

  async function pollLoopbackStatus() {
    try {
      const resp = await api("GET", "/api/adau1860/loopback/status");

      loopbackProgress.style.width = resp.progress + "%";

      if (resp.status === "recording") {
        loopbackBtn.classList.add("recording");
        loopbackBtn.classList.remove("playing");
        loopbackBtn.querySelector(".loopback-text").textContent = "Recording...";
        loopbackState.textContent = "Recording from microphone...";
      } else if (resp.status === "playing") {
        loopbackBtn.classList.remove("recording");
        loopbackBtn.classList.add("playing");
        loopbackBtn.querySelector(".loopback-text").textContent = "Playing...";
        loopbackState.textContent = "Playing back through speaker...";
      } else if (resp.status === "done") {
        stopLoopbackPoll();
        loopbackState.textContent = "Done! Recording played back successfully.";
        setTimeout(resetLoopbackUI, 2000);
      } else if (resp.status === "error") {
        stopLoopbackPoll();
        loopbackState.textContent = "Error: " + (resp.error || "Unknown error");
        loopbackState.classList.add("error");
        setTimeout(resetLoopbackUI, 3000);
      } else if (resp.status === "idle") {
        // Finished or never started
        stopLoopbackPoll();
        resetLoopbackUI();
      }

    } catch (e) {
      stopLoopbackPoll();
      loopbackState.textContent = "Error: " + e.message;
      loopbackState.classList.add("error");
      setTimeout(resetLoopbackUI, 3000);
    }
  }

  function stopLoopbackPoll() {
    if (loopbackPollInterval) {
      clearInterval(loopbackPollInterval);
      loopbackPollInterval = null;
    }
  }

  function resetLoopbackUI() {
    stopLoopbackPoll();
    loopbackBtn.disabled = false;
    loopbackBtn.classList.remove("recording", "playing");
    loopbackBtn.querySelector(".loopback-text").textContent = "Record & Play (5s)";
    loopbackProgress.style.width = "0%";
    loopbackStatus.classList.remove("active");
    loopbackState.textContent = "";
    loopbackState.classList.remove("error");
  }

  if (loopbackBtn) {
    loopbackBtn.addEventListener("click", startLoopbackTest);
  }

  // --- Init ---
  refresh();

  // Auto-refresh every 5 seconds
  setInterval(() => {
    if (document.visibilityState === "visible") {
      refresh();
    }
  }, 5000);
})();
