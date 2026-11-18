/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  let payload = null;
  try {
    payload = await resp.json();
  } catch (e) {
    payload = null;
  }

  if (!resp.ok) {
    const detail = payload && (payload.error || payload.message) ? payload.error || payload.message : `HTTP ${resp.status}`;
    throw new Error(detail);
  }

  return payload;
}

function initNetworkModal() {
  const netModal = document.getElementById("netModal");
  const netClose = document.getElementById("netClose");
  const wifiScan = document.getElementById("wifiScan");
  const wifiPowerToggle = document.getElementById("wifiPowerToggle");
  const netStatus = document.getElementById("netStatus");
  const netInfo = document.getElementById("netInfo");

  const wifiForm = document.getElementById("wifiConfigForm");
  const wifiServiceSelect = document.getElementById("wifiServiceSelect");
  const wifiSecurity = document.getElementById("wifiSecurity");
  const wifiIdentity = document.getElementById("wifiIdentity");
  const wifiPassphrase = document.getElementById("wifiPassphrase");

  const ethForm = document.getElementById("ethConfigForm");
  const ethServiceSelect = document.getElementById("ethServiceSelect");
  const ethAddress = document.getElementById("ethAddress");
  const ethNetmask = document.getElementById("ethNetmask");
  const ethGateway = document.getElementById("ethGateway");
  const ethDns1 = document.getElementById("ethDns1");
    const ethDns2 = document.getElementById("ethDns2");
    const ethPowerToggle = document.getElementById("ethPowerToggle");

  let currentSnapshot = null;
  let wifiSecurityByService = {};
  let wifiPowerPending = null;
  let ethPowerPending = null;
  let modalPollId = null;
  let snapshotLoading = false;

  function setStatus(msg) {
    netStatus.textContent = msg;
  }

  function currentMode() {
    const checked = document.querySelector('input[name="ethMode"]:checked');
    return checked ? checked.value : "dhcp";
  }

  function updateModeFields() {
    const isStatic = currentMode() === "static";
    [ethAddress, ethNetmask, ethGateway, ethDns1, ethDns2].forEach((field) => {
      if (field) field.disabled = !isStatic;
    });

    document.querySelectorAll(".static-only").forEach((node) => {
      node.hidden = !isStatic;
    });
  }

  function updateWifiAuthFields() {
    const sec = wifiSecurity.value || "auto";
    const isOpen = sec === "open";
    const isEnterprise = sec === "8021x";
    wifiIdentity.disabled = !isEnterprise;
    wifiPassphrase.disabled = isOpen;
    if (!isEnterprise) wifiIdentity.value = "";
    if (isOpen) wifiPassphrase.value = "";
  }

  function securityLabel(sec) {
    if (sec === "open") return "Open";
    if (sec === "psk") return "WPA/WPA2-PSK";
    if (sec === "wep") return "WEP";
    if (sec === "8021x") return "802.1X";
    return sec || "Unknown";
  }

  async function loadSnapshot(scan, options = {}) {
    const silent = !!options.silent;
    if (!silent) setStatus(scan ? "scanning..." : "loading...");
    const qs = scan ? "?scan=1" : "";
    const payload = await fetchJson(`/api/network/services${qs}`);
    currentSnapshot = payload;
    renderSnapshot(payload);
    if (!silent) setStatus("ready");
  }

  async function refreshSnapshot(scan = false, options = {}) {
    if (netModal.classList.contains("hidden") || snapshotLoading) return;
    snapshotLoading = true;
    try {
      await loadSnapshot(!!scan, options);
    } finally {
      snapshotLoading = false;
    }
  }

  function isRelevantInterface(ifname) {
      const n = String(ifname || "").toLowerCase();
      if (!n || n === "lo") return false;
      const hiddenPrefixes = ["sit", "tunl", "ip6tnl", "gre", "gretap", "erspan", "ifb", "dummy", "veth", "docker", "br-", "virbr", "wg", "tailscale", "zt", "tap", "tun"];
      return !hiddenPrefixes.some((p) => n.startsWith(p));
    }

    function renderInterfaceSummary(snapshot) {
      const interfaces = (snapshot.interfaces || []).filter((itf) => isRelevantInterface(itf.ifname));
      netInfo.textContent = "";

      const table = document.createElement("table");
      table.className = "iface-table";

      const thead = document.createElement("thead");
      const hrow = document.createElement("tr");
      ["Interface", "Status", "IP Address", "Link Speed", "SSID"].forEach((label) => {
        const th = document.createElement("th");
        th.textContent = label;
        hrow.appendChild(th);
      });
      thead.appendChild(hrow);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      if (!interfaces.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.className = "iface-empty";
        cell.textContent = "No relevant interfaces detected.";
        row.appendChild(cell);
        tbody.appendChild(row);
      } else {
        const wifiTech = (snapshot.technologies && snapshot.technologies.wifi) || null;
        const wifiPowered = !!(wifiTech && wifiTech.powered);

        interfaces.sort((a, b) => String(a.ifname).localeCompare(String(b.ifname)));
        interfaces.forEach((itf) => {
          const row = document.createElement("tr");
          const isWifiIf = String(itf.ifname || "").startsWith("wl");
          const isEthIf = String(itf.ifname || "").startsWith("en");
          const ipv4Addrs = (itf.addresses || []).filter((a) => a.family === "inet");
          const preferredIpv4 = (ipv4Addrs.find((a) => !!a.secondary) || ipv4Addrs[ipv4Addrs.length - 1] || {}).address || "";
          const hasIpv4 = !!preferredIpv4;
          const flags = Array.isArray(itf.flags) ? itf.flags : [];
          const rawUp = String(itf.operstate || "").toUpperCase() === "UP";
          const hasLowerUp = flags.some((f) => String(f || "").toUpperCase() === "LOWER_UP");
          const statusUp = isEthIf ? (hasLowerUp || (rawUp && flags.length === 0)) : rawUp;
          const ipv4 = isEthIf
            ? (statusUp && hasIpv4 ? preferredIpv4 : "")
            : (hasIpv4 ? preferredIpv4 : "-");

          const ifCell = document.createElement("td");
          const nameWrap = document.createElement("div");
          nameWrap.className = "iface-name-wrap";
          const nameText = document.createElement("span");
          nameText.textContent = itf.ifname || "-";
          nameWrap.appendChild(nameText);

          if (isWifiIf || isEthIf) {
            const power = document.createElement("label");
            power.className = "power-switch";
            power.title = isWifiIf ? "Toggle WiFi" : "Toggle Wired";
            const input = document.createElement("input");
            input.type = "checkbox";
            const slider = document.createElement("span");
            slider.className = "power-slider";
            power.appendChild(input);
            power.appendChild(slider);

            if (isWifiIf) {
              const pendingActive = !!(wifiPowerPending && Date.now() <= wifiPowerPending.untilMs);
              const desiredState = pendingActive ? !!wifiPowerPending.targetOn : wifiPowered;
              input.checked = desiredState;
              input.disabled = !wifiTech || pendingActive;
              input.addEventListener("change", () => {
                const preferredServiceId = wifiServiceSelect ? wifiServiceSelect.value : "";
                handleWifiPowerToggle(!!input.checked, preferredServiceId);
              });
            } else {
              const pendingActive = !!(ethPowerPending && ethPowerPending.ifname === itf.ifname && Date.now() <= ethPowerPending.untilMs);
              const pendingOn = !!(pendingActive && ethPowerPending.targetOn);
              input.checked = pendingActive ? pendingOn : statusUp;
              input.disabled = pendingActive;
              input.addEventListener("change", () => {
                handleEthernetPowerToggle(itf.ifname, !!input.checked);
              });
            }

            nameWrap.appendChild(power);
          }

          ifCell.appendChild(nameWrap);

          const stCell = document.createElement("td");
          const dot = document.createElement("span");
          dot.className = "iface-dot " + (statusUp ? "up" : "down");
          const txt = document.createElement("span");
          txt.textContent = statusUp ? "UP" : "DOWN";
          stCell.appendChild(dot);
          stCell.appendChild(txt);

          const ipCell = document.createElement("td");
          ipCell.textContent = ipv4;

          const speedCell = document.createElement("td");
          const speedWrap = document.createElement("div");
          speedWrap.className = "iface-speed-wrap";
          const speedText = document.createElement("span");
          speedText.textContent = statusUp ? (itf.link_speed || "-") : "";
          speedWrap.appendChild(speedText);

          const quality = Number(itf.wifi_signal);
          if (statusUp && isWifiIf && Number.isFinite(quality)) {
            const meter = document.createElement("span");
            meter.className = "signal-meter";
            const activeSegments = Math.max(0, Math.min(8, Math.round((quality / 100) * 8)));
            for (let i = 0; i < 8; i += 1) {
              const seg = document.createElement("span");
              let tier = "dark";
              if (i < 3) tier = "yellow";
              else if (i < 6) tier = "light";
              seg.className = "signal-seg " + tier + (i < activeSegments ? " on" : " off");
              meter.appendChild(seg);
            }
            speedWrap.appendChild(meter);
          }
          speedCell.appendChild(speedWrap);

          const ssidCell = document.createElement("td");
          ssidCell.textContent = itf.ssid || "-";

          row.appendChild(ifCell);
          row.appendChild(stCell);
          row.appendChild(ipCell);
          row.appendChild(speedCell);
          row.appendChild(ssidCell);
          tbody.appendChild(row);
        });
      }

      table.appendChild(tbody);
      netInfo.appendChild(table);

      const extras = [];
      const hint = snapshot.wifi_hint ? String(snapshot.wifi_hint) : "";
      if (hint) extras.push(hint);
      const errors = (snapshot.errors || []).filter(Boolean);
      if (errors.length) extras.push("ConnMan: " + errors.join(" ; "));

      const viewer = snapshot.viewer || null;
      const portal = snapshot.captive_portal || null;
      if (portal && portal.required) {
        if (viewer && viewer.is_local === false) {
          extras.push("Captive portal detected on WiFi. Complete login on the machine-local browser.");
        } else {
          extras.push("Captive portal detected on WiFi. Open a web page on this device to complete sign-in.");
        }
      }
      if (extras.length) {
        const extra = document.createElement("div");
        extra.className = "iface-extra";
        extra.textContent = extras.join(" | ");
        netInfo.appendChild(extra);
      }
    }

  function populateWifiForm(services) {
    const wifiServices = services.filter((s) => s.type === "wifi");
    const previous = wifiServiceSelect.value;
    wifiServiceSelect.textContent = "";
    wifiSecurityByService = {};

    if (!wifiServices.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No WiFi service";
      wifiServiceSelect.appendChild(opt);
      wifiServiceSelect.disabled = true;
      return;
    }

    const activeWifiInterface = ((currentSnapshot && currentSnapshot.interfaces) || []).find((itf) =>
      String(itf.ifname || "").startsWith("wl") &&
      String(itf.operstate || "").toUpperCase() === "UP" &&
      String(itf.ssid || "").trim()
    );
    const operationalSsid = activeWifiInterface ? String(activeWifiInterface.ssid || "").trim() : "";

    let operationalServiceId = "";
    if (operationalSsid) {
      const matchBySsid = wifiServices.find((svc) => String(svc.name || "").trim() === operationalSsid);
      if (matchBySsid) operationalServiceId = matchBySsid.id;
    }
    if (!operationalServiceId) {
      const connectedServices = wifiServices.filter((svc) => svc.connected);
      if (connectedServices.length === 1) operationalServiceId = connectedServices[0].id;
    }

    wifiServiceSelect.disabled = false;
    wifiServices.forEach((svc) => {
      const opt = document.createElement("option");
      opt.value = svc.id;
      const isOperational = operationalServiceId && svc.id === operationalServiceId;
      opt.textContent = String(svc.name || "");
      wifiServiceSelect.appendChild(opt);
      wifiSecurityByService[svc.id] = svc.security || "auto";
    });

    if (previous && wifiSecurityByService[previous]) {
      // Preserve user choice during auto-refresh while editing WiFi settings.
      wifiServiceSelect.value = previous;
    } else if (operationalServiceId && wifiSecurityByService[operationalServiceId]) {
      wifiServiceSelect.value = operationalServiceId;
    }

    const selected = wifiServiceSelect.value;
    if (selected && wifiSecurityByService[selected]) {
      wifiSecurity.value = wifiSecurityByService[selected];
    }
    updateWifiAuthFields();
  }

  function focusWifiService(serviceId, security) {
    if (!serviceId) return;
    if ([...wifiServiceSelect.options].some((o) => o.value === serviceId)) {
      wifiServiceSelect.value = serviceId;
    }
    if (security && ["open", "psk", "wep", "8021x"].includes(security)) {
      wifiSecurity.value = security;
    } else if (wifiSecurityByService[serviceId]) {
      wifiSecurity.value = wifiSecurityByService[serviceId];
    }
    updateWifiAuthFields();
    wifiPassphrase.focus();
  }

  function ethernetLinkUp(snapshot) {
      const interfaces = (snapshot && snapshot.interfaces) || [];
      return interfaces.some((itf) => {
        const isEth = String(itf.ifname || "").startsWith("en");
        const isUp = String(itf.operstate || "").toUpperCase() === "UP";
        const hasIpv4 = ((itf.addresses || []).some((a) => a.family === "inet"));
        return isEth && isUp && hasIpv4;
      });
    }

    function isEthPendingOn(serviceId) {
      if (!ethPowerPending) return false;
      if (Date.now() > ethPowerPending.untilMs) {
        ethPowerPending = null;
        return false;
      }
      return !!(ethPowerPending.targetOn && ethPowerPending.serviceId === serviceId);
    }

    async function waitForEthernetSettle(ifname, wantOn, timeoutMs = 18000) {
      const deadline = Date.now() + timeoutMs;
      let lastUp = false;

      while (Date.now() < deadline) {
        try {
          const payload = await fetchJson("/api/network/services");
          currentSnapshot = payload;

          const itf = (payload.interfaces || []).find((i) => i.ifname === ifname);
          const flags = (itf && Array.isArray(itf.flags)) ? itf.flags : [];
          const hasLowerUp = flags.some((f) => String(f || "").toUpperCase() === "LOWER_UP");
          const rawUp = String((itf && itf.operstate) || "").toUpperCase() === "UP";
          const upNow = hasLowerUp || (rawUp && flags.length === 0);
          lastUp = upNow;

          renderSnapshot(payload);

          if ((wantOn && upNow) || (!wantOn && !upNow)) return upNow;
        } catch (_err) {
          // Keep polling; transient reads are expected while the link settles.
        }

        await new Promise((resolve) => setTimeout(resolve, 800));
      }

      return lastUp;
    }

  function populateEthernetForm(services, snapshot) {
      const ethernetServices = services.filter((s) => s.type === "ethernet");
      const previous = ethServiceSelect.value;
      ethServiceSelect.textContent = "";

      if (!ethernetServices.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No wired service";
        ethServiceSelect.appendChild(opt);
        ethServiceSelect.disabled = true;
        if (ethPowerToggle) {
          ethPowerToggle.checked = false;
          ethPowerToggle.disabled = true;
          ethPowerToggle.dataset.serviceId = "";
        }
        return;
      }

      const linkUp = ethernetLinkUp(snapshot);
      ethServiceSelect.disabled = false;
      ethernetServices.forEach((svc) => {
        const opt = document.createElement("option");
        opt.value = svc.id;
        const liveConnected = !!(svc.connected && linkUp);
        opt.textContent = svc.name;
        ethServiceSelect.appendChild(opt);
      });

      if (previous && ethernetServices.some((svc) => svc.id === previous)) {
        ethServiceSelect.value = previous;
      }

      updateEthernetDisconnectState(ethernetServices, linkUp);
    }

    function updateEthernetDisconnectState(ethernetServices, linkUp) {
      if (!ethPowerToggle) return;
      const selectedId = ethServiceSelect.value;
      const selected = (ethernetServices || []).find((svc) => svc.id === selectedId);
      const liveConnected = !!(selected && selected.connected && linkUp);
      const pendingOn = isEthPendingOn(selectedId);
      const isBusy = !!(ethPowerPending && ethPowerPending.serviceId === selectedId && Date.now() <= ethPowerPending.untilMs);

      ethPowerToggle.checked = pendingOn || liveConnected;
      ethPowerToggle.dataset.serviceId = selectedId || "";
      ethPowerToggle.disabled = !selectedId || isBusy;
    }

  function renderSnapshot(snapshot) {
    const services = snapshot.services || [];
    const wifiTech = (snapshot.technologies && snapshot.technologies.wifi) || null;
    const wifiOn = !!(wifiTech && wifiTech.powered);
    renderInterfaceSummary(snapshot);

    populateWifiForm(services);
    populateEthernetForm(services, snapshot);
  }

  async function waitForWifiPowerSettle(wantOn, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let lastPowered = false;

    while (Date.now() < deadline) {
      try {
        const payload = await fetchJson("/api/network/services");
        currentSnapshot = payload;
        renderSnapshot(payload);

        const wifiTech = (payload.technologies && payload.technologies.wifi) || null;
        const powered = !!(wifiTech && wifiTech.powered);
        lastPowered = powered;
        if (powered === wantOn) return powered;
      } catch (_err) {
        // Keep polling while technology state settles.
      }

      await new Promise((resolve) => setTimeout(resolve, 700));
    }

    return lastPowered;
  }

  async function waitForWifiSettle(serviceId, timeoutMs = 25000) {
    const deadline = Date.now() + timeoutMs;
    let lastConnected = false;

    while (Date.now() < deadline) {
      try {
        const payload = await fetchJson("/api/network/services");
        currentSnapshot = payload;
        renderSnapshot(payload);

        const wifiSvc = (payload.services || []).find((svc) => svc.id === serviceId);
        const connected = !!(wifiSvc && wifiSvc.connected);
        lastConnected = connected;

        const wlIf = (payload.interfaces || []).find((itf) => String(itf.ifname || "").startsWith("wl"));
        const ipv4 = ((wlIf && wlIf.addresses) || [])
          .filter((a) => a.family === "inet")
          .map((a) => a.address)
          .join(", ");

        if (connected && ipv4) {
          return { connected: true, ip: ipv4 };
        }
      } catch (_err) {
        // Keep polling; transient reads can fail while interfaces settle.
      }

      await new Promise((resolve) => setTimeout(resolve, 1000));
    }

    return { connected: lastConnected, ip: "" };
  }

  async function recoverWifiAfterEnable(preferredServiceId, timeoutMs = 22000) {
    const deadline = Date.now() + timeoutMs;

    try {
      await fetchJson("/api/network/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ technology: "wifi" }),
      });
    } catch (_err) {
      // Continue; scans can fail transiently while WiFi powers up.
    }

    while (Date.now() < deadline) {
      try {
        const payload = await fetchJson("/api/network/services");
        currentSnapshot = payload;
        renderSnapshot(payload);

        const wifiServices = (payload.services || []).filter((s) => s.type === "wifi");
        const alreadyConnected = wifiServices.find((s) => s.connected);
        if (alreadyConnected) {
          return await waitForWifiSettle(alreadyConnected.id, 12000);
        }

        const matchPreferred = preferredServiceId ? wifiServices.find((s) => s.id === preferredServiceId) : null;
        const autoCandidate = wifiServices.find((s) => s.favorite || s.auto_connect);
        const chosen = matchPreferred || autoCandidate || wifiServices[0] || null;

        if (chosen) {
          try {
            await fetchJson("/api/network/connect", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ service_id: chosen.id, security: "auto", identity: "", passphrase: "" }),
            });
          } catch (_err) {
            // Keep polling until known service appears or user provides creds.
          }

          const settled = await waitForWifiSettle(chosen.id, 15000);
          if (settled.connected || settled.ip) return settled;
        }
      } catch (_err) {
        // Keep polling until timeout.
      }

      await new Promise((resolve) => setTimeout(resolve, 1000));
    }

    return { connected: false, ip: "" };
  }

  async function toggleWifi() {
    try {
      const wifiTech = (currentSnapshot && currentSnapshot.technologies && currentSnapshot.technologies.wifi) || null;
      const enabled = !(wifiTech && wifiTech.powered);
      setStatus(enabled ? "enabling wifi..." : "disabling wifi...");
      await fetchJson("/api/network/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ technology: "wifi", enabled }),
      });
      await loadSnapshot(false);
    } catch (err) {
      setStatus(`error: ${err.message}`);
    }
  }

  async function submitWifiForm(e) {
    e.preventDefault();

    const serviceId = wifiServiceSelect.value;
    if (!serviceId) {
      setStatus("no WiFi service selected");
      return;
    }

    const security = wifiSecurity.value || "auto";
    const body = {
      service_id: serviceId,
      security,
      identity: wifiIdentity.value.trim(),
      passphrase: wifiPassphrase.value.trim(),
    };

    if (security === "open") {
      body.identity = "";
      body.passphrase = "";
    }

    if (security !== "open" && !body.passphrase) {
      setStatus("enter a passphrase/password for secured WiFi");
      return;
    }

    if (security !== "8021x") {
      body.identity = "";
    }

    try {
      setStatus(`connecting WiFi (${security})...`);
      await fetchJson("/api/network/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      wifiPassphrase.value = "";
      setStatus("waiting for WiFi IP...");
      const settled = await waitForWifiSettle(serviceId, 25000);
      if (settled.ip) {
        setStatus(`WiFi connected (${settled.ip})`);
      } else if (settled.connected) {
        setStatus("WiFi connected");
      } else {
        setStatus("WiFi connect requested; still waiting for interface to settle");
      }
    } catch (err) {
      setStatus(`error: ${err.message}`);
    }
  }

  async function submitEthernetForm(e) {
    e.preventDefault();

    const serviceId = ethServiceSelect.value;
    if (!serviceId) {
      setStatus("no wired service selected");
      return;
    }

    const mode = currentMode();
    const dnsServers = [ethDns1.value.trim(), ethDns2.value.trim()].filter(Boolean).join(",");
      const body = {
        service_id: serviceId,
        method: mode,
        dns_servers: dnsServers,
      };

    if (mode === "static") {
      body.address = ethAddress.value.trim();
      body.netmask = ethNetmask.value.trim();
      body.gateway = ethGateway.value.trim();
    }

    try {
      setStatus(`applying ${mode} ethernet config...`);
      await fetchJson("/api/network/configure_ipv4", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await loadSnapshot(false);
      setStatus("ethernet settings applied");
    } catch (err) {
      setStatus(`error: ${err.message}`);
    }
  }

  function openModal() {
    netModal.classList.remove("hidden");
    updateModeFields();
    updateWifiAuthFields();
    refreshSnapshot(false).catch((err) => {
      setStatus(`error: ${err.message}`);
    });

    if (modalPollId) clearInterval(modalPollId);
    modalPollId = setInterval(() => {
      refreshSnapshot(false, { silent: true }).catch((err) => {
        setStatus(`error: ${err.message}`);
      });
    }, 3000);
  }

  function closeModal() {
    netModal.classList.add("hidden");
    if (modalPollId) {
      clearInterval(modalPollId);
      modalPollId = null;
    }
  }

  netClose.onclick = closeModal;
  netModal.onclick = (e) => {
    if (e.target === netModal) closeModal();
  };

  wifiScan.onclick = () => {
    fetchJson("/api/network/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ technology: "wifi" }),
    })
      .then(() => refreshSnapshot(true))
      .catch((err) => {
        setStatus(`error: ${err.message}`);
      });
  };

  async function handleWifiPowerToggle(targetOn, preferredServiceId = "") {
    wifiPowerPending = { targetOn, untilMs: Date.now() + 22000 };

    try {
      setStatus(targetOn ? "enabling wifi..." : "disabling wifi...");
      await fetchJson("/api/network/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ technology: "wifi", enabled: targetOn }),
      });

      const powered = await waitForWifiPowerSettle(targetOn, 18000);
      if (!targetOn) {
        setStatus(powered ? "wifi still shutting down" : "wifi disabled");
        return;
      }

      if (!powered) {
        setStatus("wifi still coming up");
        return;
      }

      setStatus("wifi enabled, recovering connection...");
      const settled = await recoverWifiAfterEnable(preferredServiceId, 22000);
      if (settled.ip) setStatus(`WiFi connected (${settled.ip})`);
      else if (settled.connected) setStatus("WiFi connected");
      else setStatus("wifi enabled, no WiFi service connected yet");
    } catch (err) {
      setStatus("error: " + err.message);
    } finally {
      wifiPowerPending = null;
      await loadSnapshot(false);
    }
  }

    async function handleEthernetPowerToggle(ifname, targetOn) {
      const ifn = ifname || "";
      if (!ifn) {
        setStatus("no interface for this row");
        await loadSnapshot(false);
        return;
      }
      ethPowerPending = { ifname: ifn, targetOn: !!targetOn, untilMs: Date.now() + 20000 };

      try {
        setStatus(targetOn ? `enabling ${ifn}...` : `disabling ${ifn}...`);
        await fetchJson("/api/network/interface_power", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ifname: ifn, enabled: !!targetOn }),
        });

        if (targetOn) {
          const up = await waitForEthernetSettle(ifn, true, 20000);
          setStatus(up ? `${ifn} enabled` : `${ifn} enabled; waiting for link`);
        } else {
          await waitForEthernetSettle(ifn, false, 8000);
          setStatus(`${ifn} disabled`);
        }
      } catch (err) {
        setStatus("error: " + err.message);
      } finally {
        ethPowerPending = null;
        await loadSnapshot(false);
      }
    }

    ethServiceSelect.addEventListener("change", () => {
      const services = (currentSnapshot && currentSnapshot.services) || [];
      const linkUp = ethernetLinkUp(currentSnapshot || {});
      updateEthernetDisconnectState(services.filter((s) => s.type === "ethernet"), linkUp);
    });

    wifiServiceSelect.addEventListener("change", () => {
    const svcId = wifiServiceSelect.value;
    if (svcId && wifiSecurityByService[svcId]) {
      wifiSecurity.value = wifiSecurityByService[svcId];
    }
    updateWifiAuthFields();
  });

  wifiSecurity.addEventListener("change", updateWifiAuthFields);

  document.querySelectorAll('input[name="ethMode"]').forEach((el) => {
    el.addEventListener("change", updateModeFields);
  });

  wifiForm.addEventListener("submit", submitWifiForm);
  ethForm.addEventListener("submit", submitEthernetForm);

  return { openModal };
}

const networkUi = initNetworkModal();
