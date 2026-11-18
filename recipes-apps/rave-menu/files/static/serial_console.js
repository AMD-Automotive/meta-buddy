/*
 * Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved.
 * SPDX-License-Identifier: MIT
 * Contributors: KC
 * Maintainer: KC
 */

/* =====================================================================
 * Serial Console — client-side controller
 * ---------------------------------------------------------------------
 * Full-screen interactive console for /dev/ttyUSB1 and /dev/ttyUSB2,
 * launched from the Versal AI Edge Series Gen 2 Lab modal.
 *
 * Read this header before changing anything below; some behaviours look
 * arbitrary but are load-bearing.
 * =====================================================================
 *
 * 1. PURPOSE
 * ----------
 * Provide a real terminal experience in the browser for the two USB-
 * serial consoles exposed by the Versal carrier. The user must be able
 * to type into the device, see ANSI-coloured output, scroll back, and
 * leave-and-return without losing context.
 *
 * 2. UI SURFACE  (templates/_serial_console.html)
 * ----------------------------------------------
 *   #serialConsoleModal      — full-screen modal container
 *   #serialConsoleTitle      — "Serial Console — /dev/ttyUSB<N>"
 *   #serialConsoleStatus     — connection status text
 *   #serialKeepAliveChk      — header toggle, see §6
 *   #serialConsoleTerm       — host element for the xterm.js DOM
 *   #serialClearBtn          — clear terminal and captured log
 *   #serialSaveLogBtn        — "Save console log",        see §7
 *   #serialRebootBtn         — "Reboot Versal",           see §8
 *   #serialExitBtn           — "Exit",                    see §6
 *
 * 3. PUBLIC API
 * -------------
 *   window.openSerialConsole(portNumber)
 *       Show the modal and attach to /dev/ttyUSB<portNumber>.
 *       portNumber is an integer matching a key of routes_serial.PORTS
 *       on the backend (currently 1 or 2).
 *   window.openX86Console()
 *       Show the same terminal UI attached to a local x86 shell.
 *
 * 4. BACKEND CONTRACT  (routes_serial.py)
 * ---------------------------------------
 *   POST /api/serial/<n>/open       — idempotent open. Returns
 *                                     {ok:false, busy:true, holders:[
 *                                       {pid, name}, ...]} with HTTP 409
 *                                     when another process owns /dev/ttyUSB<N>.
 *   POST /api/serial/<n>/close      — force-close (kicks ALL viewers; we
 *                                     do NOT call this from Exit, so
 *                                     other browsers stay attached)
 *   POST /api/serial/<n>/write      — JSON {"data": "<chars>"}
 *   POST /api/serial/<n>/keepalive  — bumps backend last_activity timer;
 *                                     returns the same 409/busy shape if
 *                                     it had to (re)open and someone else
 *                                     grabbed the port in the meantime
 *   GET  /api/serial/<n>/stream     — SSE; data events carry JSON-encoded
 *                                     UTF-8 strings; "busy" events carry
 *                                     {error, device, holders} JSON when
 *                                     the device is held by another
 *                                     process; ": ping" comments are sent
 *                                     every 1 s when idle
 *
 *   The backend fans every byte out to every subscriber, so multiple
 *   browsers see a UNIFIED LIVE VIEW of the same port. Do not introduce
 *   per-browser exclusivity here without a matching backend change.
 *
 * 5. RUNTIME STATE  (var STATE)
 * -----------------------------
 * One persistent record per port number, keyed by integer port:
 *   STATE[port] = {
 *     portNumber: int,           // for callbacks that close over state
 *     term:       Terminal,      // xterm.js instance (kept across exits
 *                                //   when Keep-alive is on)
 *     fit:        FitAddon,
 *     es:         EventSource,   // SSE stream; null when detached
 *     log:        string,        // captured plain-text log, capped 4 MB
 *     kaTimer:    intervalId,    // 1 Hz /keepalive POST while hidden
 *   }
 *
 * The state outlives the modal being hidden; this is what makes the
 * "leave and come back" feature work. It is reset only on hard reload
 * or when Keep-alive is OFF and the user presses Exit.
 *
 * 6. KEEP-ALIVE TOGGLE  (#serialKeepAliveChk)
 * -------------------------------------------
 *   OFF (default):
 *     Exit closes SSE, disposes the Terminal, and drops STATE[port].
 *     The backend watchdog will release the OS device after its idle
 *     grace window (~30 s) so ssh/minicom can use the port.
 *
 *   ON:
 *     Exit hides the modal but keeps SSE attached AND starts a 1 Hz
 *     /keepalive POST loop. The Terminal DOM is detached from the
 *     visible container but the Terminal INSTANCE is preserved, so on
 *     re-open we just re-attach and the user sees everything that
 *     arrived while away. The captured log keeps accumulating.
 *
 *   The 1 Hz POST cadence pairs with the backend's
 *   KEEPALIVE_INTERVAL_SECONDS=1 and KEEPALIVE_MAX_MISSES=30, giving a
 *   ~30 s tolerance for transient network drops before the port is
 *   released. Do not raise the cadence above 1 Hz without a backend
 *   review (would burn cycles and log spam).
 *
 *   The toggle state is persisted in localStorage["rave.serial.keepAlive"].
 *
 * 7. SAVE CONSOLE LOG  (#serialSaveLogBtn)
 * ----------------------------------------
 * Downloads the captured per-port log as
 *   serial-ttyUSB<N>-YYYYMMDD-HHMMSS.log
 * The log is the raw UTF-8 stream we received from SSE, with ANSI
 * escape sequences (CSI, OSC, single-char) stripped to make the file
 * readable in plain editors. The 4 MB cap is a rolling window.
 *
 * 8. REBOOT BUTTON  (#serialRebootBtn)
 * ------------------------------------
 * NO CONFIRMATION. The full-screen console sits over the rest of the
 * UI, which means the standard showConfirm() modal renders BEHIND this
 * window and is unreachable. Pressing Reboot inside the lab console is
 * itself the act of confirmation; do not re-introduce a confirm() here.
 *
 * 9. INVARIANTS  (do not break)
 * -----------------------------
 *  (a) Terminal.onData() is registered EXACTLY ONCE per Terminal
 *      instance (in ensureTerminal). Re-attaching the DOM via
 *      attachTerminalTo() must never re-register it, or every
 *      keystroke would be sent N times.
 *  (b) convertEol stays false. The device emits "\r\n" natively;
 *      enabling convertEol would double newlines.
 *  (c) Exit MUST NOT call /api/serial/<n>/close — that would forcibly
 *      disconnect every other browser viewing the same port. Just drop
 *      our own SSE subscription and let the backend watchdog release
 *      the device when the last subscriber leaves.
 *  (d) Writes are not echoed locally. Every viewer sees only what the
 *      device echoes back, so all browsers stay in sync.
 *
 * 10. DEPENDENCIES
 * ----------------
 *   xterm.js 5.x   (vendored at /static/vendor/xterm/xterm.js)
 *   xterm-addon-fit (vendored at /static/vendor/xterm/xterm-addon-fit.js)
 *   Browser EventSource, fetch, Blob, URL, localStorage.
 * =====================================================================
 */
(function () {
  // ── DOM handles ─────────────────────────────────────────────────
  var consoleModal = document.getElementById("serialConsoleModal");
  if (!consoleModal) return; // template not on this page

  var titleEl     = document.getElementById("serialConsoleTitle");
  var statusEl    = document.getElementById("serialConsoleStatus");
  var termEl      = document.getElementById("serialConsoleTerm");
  var rebootBtn   = document.getElementById("serialRebootBtn");
  var exitBtn     = document.getElementById("serialExitBtn");
  var clearBtn    = document.getElementById("serialClearBtn");
  var saveLogBtn  = document.getElementById("serialSaveLogBtn");
  var keepAliveEl = document.getElementById("serialKeepAliveChk");
  var fontSizeEl  = document.getElementById("serialFontSize");
  var fontSizeValEl = document.getElementById("serialFontSizeVal");
  var timestampOptionEl = document.getElementById("serialTimestampOption");
  var timestampEl = document.getElementById("serialTimestampChk");

  // ── Constants ───────────────────────────────────────────────────
  var KEEPALIVE_KEY        = "rave.serial.keepAlive"; // localStorage
  var FONTSIZE_KEY         = "rave.serial.fontSize";  // localStorage
  var TIMESTAMP_KEY        = "rave.serial.timestamp"; // localStorage
  var KEEPALIVE_PERIOD_MS  = 1000;                    // see §6
  var LOG_CAP_BYTES        = 4 * 1024 * 1024;         // see §7
  var TERMINAL_SCROLLBACK  = 10000;                   // xterm rows
  var DEFAULT_FONT_SIZE    = 14;
  var MIN_FONT_SIZE        = 8;
  var MAX_FONT_SIZE        = 64;
  var BAUD_DISPLAY         = 115200;                  // backend hard-codes
  var X86_CONSOLE_KEY      = "x86";

  // ── Per-port persistent state (see §5) ──────────────────────────
  var STATE = {};
  var currentPort = null; // which port the modal is currently showing

  // ── Small helpers ───────────────────────────────────────────────

  /** Update the status indicator in the modal header. */
  function setStatus(t) { if (statusEl) statusEl.textContent = t; }

  /** Returns true if the Keep-alive header toggle is checked. */
  function getKeepAlive() { return !!(keepAliveEl && keepAliveEl.checked); }

  /**
   * Read the persisted terminal font size, clamped to [MIN, MAX].
   * Falls back to DEFAULT_FONT_SIZE when nothing is stored.
   */
  function getCurrentFontSize() {
    var stored = null;
    try { stored = window.localStorage.getItem(FONTSIZE_KEY); } catch (e) {}
    var n = stored ? parseInt(stored, 10) : DEFAULT_FONT_SIZE;
    if (!isFinite(n)) n = DEFAULT_FONT_SIZE;
    if (n < MIN_FONT_SIZE) n = MIN_FONT_SIZE;
    if (n > MAX_FONT_SIZE) n = MAX_FONT_SIZE;
    return n;
  }

  /**
   * Apply the given font size to every live xterm Terminal, refit
   * the visible one, persist the value, and update the slider readout.
   */
  function applyFontSize(px) {
    if (px < MIN_FONT_SIZE) px = MIN_FONT_SIZE;
    if (px > MAX_FONT_SIZE) px = MAX_FONT_SIZE;
    Object.keys(STATE).forEach(function (k) {
      var st = STATE[k];
      if (st && st.term && st.term.options) {
        try { st.term.options.fontSize = px; } catch (e) {}
      }
    });
    // Refit the active terminal so rows/cols match the new metrics.
    if (currentPort != null) {
      var cur = STATE[currentPort];
      if (cur && cur.fit) {
        try { cur.fit.fit(); } catch (e) {}
      }
    }
    try { window.localStorage.setItem(FONTSIZE_KEY, String(px)); } catch (e) {}
    if (fontSizeValEl) fontSizeValEl.textContent = String(px);
  }

  /**
   * Reflect the Keep-alive state in the Exit button label: when the
   * session will persist across an Exit press, the button reads "Back"
   * (you're stepping away, not closing). Otherwise it reads "Exit".
   */
  function updateExitButtonLabel() {
    if (!exitBtn) return;
    exitBtn.textContent = getKeepAlive() ? "Back" : "Exit";
  }

  /**
   * Lazily allocate (or retrieve) the persistent state record for a
   * given port number. See §5.
   */
  function getState(port) {
    if (!STATE[port]) {
      STATE[port] = {
        portNumber: port,
        term: null, fit: null,
        es: null,
        log: "",
        kaTimer: null,
        timestampLineStart: true,
      };
    }
    return STATE[port];
  }

  // ── Terminal (xterm.js) management ──────────────────────────────

  /**
   * Build the xterm Terminal for `state` if we don't already have one.
   * The Terminal is reused across modal show/hide cycles when Keep-
   * alive is on (§6). Registers Terminal.onData exactly once — see
   * invariant §9(a).
   *
   * @returns {Terminal|null} the Terminal, or null if xterm.js failed
   *   to load (in which case status text reflects the error).
   */
  function ensureTerminal(state) {
    if (state.term) return state.term;
    if (typeof Terminal === "undefined") {
      setStatus("error: xterm.js failed to load");
      return null;
    }
    var t = new Terminal({
      cursorBlink: true,
      convertEol: false,                     // §9(b)
      scrollback: TERMINAL_SCROLLBACK,
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: getCurrentFontSize(),
      theme: { background: "#000000", foreground: "#e6e6e6", cursor: "#e6e6e6" },
    });
    if (typeof FitAddon !== "undefined" && FitAddon.FitAddon) {
      state.fit = new FitAddon.FitAddon();
      t.loadAddon(state.fit);
    }
    state.term = t;
    // Forward keystrokes to the session. Closes over `state.portNumber`
    // so even if `currentPort` changes the keystrokes still go to the
    // right port. Registered ONCE — see §9(a).
    t.onData(function (data) { writeToPort(state.portNumber, data); });
    t.onResize(function (size) {
      if (state.portNumber !== X86_CONSOLE_KEY) return;
      fetch("/api/console/resize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: size.rows, cols: size.cols }),
      }).catch(function () {});
    });
    return t;
  }

  /**
   * Attach the (possibly previously-rendered) Terminal DOM to the given
   * container. On first call uses term.open(); on subsequent calls
   * (after Keep-alive Exit/Re-open) it just moves the existing element
   * back into the visible container so scrollback and cursor state are
   * preserved.
   */
  function attachTerminalTo(state, container) {
    if (!state.term) return;
    if (!state.term.element) {
      state.term.open(container);
    } else if (state.term.element.parentNode !== container) {
      container.appendChild(state.term.element);
    }
  }

  // ── Captured log (for Save console log, §7) ─────────────────────

  /**
   * Append text to the per-port captured log, trimming the head when
   * the cap is exceeded so memory stays bounded.
   */
  function appendLog(state, text) {
    state.log += text;
    if (state.log.length > LOG_CAP_BYTES) {
      state.log = state.log.slice(state.log.length - LOG_CAP_BYTES);
    }
  }

  function formatTimestamp(date) {
    function pad(value, width) { return String(value).padStart(width, "0"); }
    return pad(date.getHours(), 2) + ":" +
      pad(date.getMinutes(), 2) + ":" +
      pad(date.getSeconds(), 2) + "." +
      pad(date.getMilliseconds(), 3);
  }

  /** Prefix each logical serial line, even when lines span SSE chunks. */
  function timestampSerialText(state, text) {
    var enabled = !!(timestampEl && timestampEl.checked);
    var output = "";
    for (var i = 0; i < text.length; i += 1) {
      var character = text.charAt(i);
      if (state.timestampLineStart && character !== "\r" && character !== "\n") {
        if (enabled) {
          output += "\x1b[38;5;45m[" + formatTimestamp(new Date()) + "]\x1b[0m ";
        }
        state.timestampLineStart = false;
      }
      output += character;
      if (character === "\n") state.timestampLineStart = true;
    }
    return output;
  }

  /** Clear the current port's visible scrollback and captured log. */
  function clearConsole() {
    if (currentPort == null) return;
    var state = getState(currentPort);
    if (state.term) state.term.clear();
    state.log = "";
  }

  // ── Backend I/O ────────────────────────────────────────────────

  /**
   * POST keystrokes (or any string) to /api/serial/<port>/write. Errors
   * are swallowed: the only sensible recovery on a flaky link is to
   * keep typing; the user will notice missing echo.
   */
  function writeToPort(port, data) {
    if (port == null) return Promise.resolve();
    var url = port === X86_CONSOLE_KEY ? "/api/console/write" : "/api/serial/" + port + "/write";
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: data }),
    }).catch(function () {});
  }

  /**
   * Open an SSE stream from /api/serial/<port>/stream and route incoming
   * bytes to both the live Terminal and the captured log. EventSource
   * auto-reconnects on transient failures, so we only need to surface
   * status and never tear down on error.
   */
  function startStream(state, port) {
    if (state.es) return; // already streaming
    var streamUrl = port === X86_CONSOLE_KEY ? "/api/console/stream" : "/api/serial/" + port + "/stream?replay=0";
    var es = new EventSource(streamUrl);
    state.es = es;
    es.onopen = function () {
      if (currentPort === port) {
        setStatus(port === X86_CONSOLE_KEY ? "connected as amd-edf" : "connected @" + BAUD_DISPLAY);
      }
    };
    es.onmessage = function (e) {
      var text;
      try { text = JSON.parse(e.data); } catch (_) { text = e.data; }
      if (port !== X86_CONSOLE_KEY) text = timestampSerialText(state, text);
      if (state.term) state.term.write(text);
      appendLog(state, text);
    };
    es.addEventListener("status", function (e) {
      if (e.data === "closed" && currentPort === port) {
        setStatus("port released");
      }
    });
    es.addEventListener("busy", function (e) {
      // Server told us the OS device is held by another process.
      var info = {};
      try { info = JSON.parse(e.data); } catch (_) {}
      // Stop the auto-reconnect EventSource cycle: the server already
      // sent a 'closed' status event right after this one.
      stopStream(state);
      showBusy(state, info);
    });
    es.onerror = function () {
      if (currentPort === port) setStatus("connection error — reconnecting…");
    };
  }

  /** Drop our SSE subscription. See §9(c) — we deliberately do NOT
   *  call /close so that other browsers viewing the same port stay
   *  connected. */
  function stopStream(state) {
    if (state.es) { try { state.es.close(); } catch (_) {} state.es = null; }
  }

  /**
   * Start the 1 Hz /keepalive POST loop used while Keep-alive is on
   * AND the modal is hidden. While the modal is visible the SSE
   * stream itself bumps the backend's last_activity timer, so the
   * timer is unnecessary and we stop it. See §6.
   */
  function startKeepAlivePings(state, port) {
    if (state.kaTimer) return;
    state.kaTimer = setInterval(function () {
      var url = port === X86_CONSOLE_KEY ? "/api/console/keepalive" : "/api/serial/" + port + "/keepalive";
      fetch(url, { method: "POST" })
        .catch(function () {});
    }, KEEPALIVE_PERIOD_MS);
  }

  function stopKeepAlivePings(state) {
    if (state.kaTimer) { clearInterval(state.kaTimer); state.kaTimer = null; }
  }

  // ── Modal show/hide ────────────────────────────────────────────

  /**
   * Show the modal and (re)attach to /dev/ttyUSB<port>. Three cases:
   *
   *   - First time for this port: ensureTerminal creates the xterm,
   *     /open is POSTed, then the SSE stream starts.
   *   - Returning while Keep-alive is on AND we still have an SSE
   *     attached: just reattach the DOM and stop the explicit
   *     keepalive timer (the SSE stream is now the heartbeat).
   *   - Returning after Keep-alive was off (or after a hard error):
   *     same as first time; STATE was already cleared on Exit.
   *
   * Public entry point — exposed as window.openSerialConsole.
   */
  function openConsole(port) {
    currentPort = port;
    var state = getState(port);
    state.portNumber = port;

    var isX86 = port === X86_CONSOLE_KEY;
    if (titleEl) titleEl.textContent = isX86 ? "x86 Console" : "Serial Console — /dev/ttyUSB" + port;
    if (rebootBtn) rebootBtn.hidden = isX86;
    if (timestampOptionEl) timestampOptionEl.hidden = isX86;
    setStatus(state.es ? "reattaching…" : "opening…");
    consoleModal.classList.remove("hidden");

    ensureTerminal(state);
    attachTerminalTo(state, termEl);
    requestAnimationFrame(function () {
      try { state.fit && state.fit.fit(); } catch (_) {}
      if (state.term) state.term.focus();
    });

    if (state.es) {
      // Keep-alive path: SSE was preserved while hidden.
      setStatus(isX86 ? "connected as amd-edf" : "connected @" + BAUD_DISPLAY);
      stopKeepAlivePings(state);
      return;
    }

    var openUrl = isX86 ? "/api/console/open" : "/api/serial/" + port + "/open";
    fetch(openUrl, { method: "POST" })
      .then(function (r) { return r.json().then(function (j) { return { j: j, status: r.status }; }); })
      .then(function (res) {
        var j = res.j || {};
        if (j.ok) { startStream(state, port); return; }
        if (j.busy) {
          showBusy(state, j);
          return;
        }
        setStatus("error: " + (j.error || "open failed"));
      })
      .catch(function (e) { setStatus("error: " + e); });
  }

  /**
   * Render a "port in use" message in the terminal and status line.
   * Called when /api/serial/<n>/open or the SSE 'busy' event tells us
   * another process holds the device.
   *
   * `info` is { device, error, holders: [{pid, name}, ...] }.
   */
  function showBusy(state, info) {
    var holders = (info && info.holders) || [];
    var dev = (info && info.device) || ("/dev/ttyUSB" + currentPort);
    var summary;
    if (holders.length === 0) {
      summary = dev + " is in use by another process";
    } else {
      summary = dev + " is in use by " +
        holders.map(function (h) { return h.name + " (pid " + h.pid + ")"; }).join(", ");
    }
    setStatus(summary);
    if (state && state.term) {
      // Make it visible in the terminal pane too, in red, so it can't
      // be missed if the status line is off-screen.
      var lines = [
        "\r\n\x1b[31m\x1b[1m" + dev + " is currently in use.\x1b[0m\r\n",
      ];
      if (holders.length) {
        lines.push("Holding process(es):\r\n");
        holders.forEach(function (h) {
          lines.push("  - " + h.name + " (pid " + h.pid + ")\r\n");
        });
      } else {
        lines.push("Could not identify the holding process.\r\n");
      }
      lines.push("\r\nStop that application and press Back, then re-open the console.\r\n");
      var msg = lines.join("");
      state.term.write(msg);
      appendLog(state, msg);
    }
  }

  /**
   * Hide the modal. Behaviour depends on the Keep-alive toggle (§6).
   * Note: never calls /api/serial/<n>/close — see invariant §9(c).
   */
  function exitConsole() {
    var port = currentPort;
    if (port == null) { consoleModal.classList.add("hidden"); return; }
    var state = getState(port);

    if (getKeepAlive()) {
      // Keep SSE attached, start explicit /keepalive pings, detach the
      // Terminal DOM (but preserve the Terminal INSTANCE).
      startKeepAlivePings(state, port);
      if (state.term && state.term.element && state.term.element.parentNode) {
        state.term.element.parentNode.removeChild(state.term.element);
      }
    } else {
      // Tear down everything for this port.
      stopStream(state);
      stopKeepAlivePings(state);
      if (port === X86_CONSOLE_KEY) {
        fetch("/api/console/close", { method: "POST" }).catch(function () {});
      }
      if (state.term) { try { state.term.dispose(); } catch (_) {} }
      delete STATE[port];
    }
    currentPort = null;
    consoleModal.classList.add("hidden");
  }

  // ── Save console log (§7) ──────────────────────────────────────

  /**
   * Build a downloadable .log from the captured per-port text, with
   * ANSI escapes stripped, and trigger a browser download. The output
   * is plain UTF-8 so it opens cleanly in any text editor.
   */
  function downloadLog() {
    if (currentPort == null) return;
    var state = getState(currentPort);
    var data = state.log || "";
    // Three families of escapes appear in real device output:
    //   ESC ] ... (BEL | ESC \)   — OSC (window title, hyperlinks, …)
    //   ESC [ params final-byte    — CSI (colours, cursor moves)
    //   ESC <single-byte>           — short two-byte escapes
    var stripped = data
      .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")
      .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g, "")
      .replace(/\x1b[@-Z\\-_]/g, "");

    var ts = new Date();
    function pad(x) { return (x < 10 ? "0" : "") + x; }
    var name = "serial-ttyUSB" + currentPort + "-" +
      ts.getFullYear() + pad(ts.getMonth() + 1) + pad(ts.getDate()) + "-" +
      pad(ts.getHours()) + pad(ts.getMinutes()) + pad(ts.getSeconds()) + ".log";

    var blob = new Blob([stripped], { type: "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      a.parentNode && a.parentNode.removeChild(a);
    }, 0);
  }

  // ── Wire-up ────────────────────────────────────────────────────
  window.openSerialConsole = openConsole;
  window.openX86Console = function () { openConsole(X86_CONSOLE_KEY); };

  if (exitBtn)    exitBtn.addEventListener("click", exitConsole);
  if (clearBtn)   clearBtn.addEventListener("click", clearConsole);
  if (saveLogBtn) saveLogBtn.addEventListener("click", downloadLog);

  if (rebootBtn) rebootBtn.addEventListener("click", function () {
    // No showConfirm() here — see §8. Pressing this button IS the
    // confirmation; the standard confirm modal would render behind
    // this full-screen console and be unreachable.
    fetch("/api/run/reconfigure-versal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args: [] }),
    }).catch(function () {});
  });

  // Persist the Keep-alive toggle across page reloads.
  if (keepAliveEl) {
    try { keepAliveEl.checked = localStorage.getItem(KEEPALIVE_KEY) === "1"; } catch (_) {}
    keepAliveEl.addEventListener("change", function () {
      try {
        localStorage.setItem(KEEPALIVE_KEY, keepAliveEl.checked ? "1" : "0");
      } catch (_) {}
      updateExitButtonLabel();
    });
  }
  updateExitButtonLabel();

  if (timestampEl) {
    try { timestampEl.checked = localStorage.getItem(TIMESTAMP_KEY) === "1"; } catch (_) {}
    timestampEl.addEventListener("change", function () {
      try { localStorage.setItem(TIMESTAMP_KEY, timestampEl.checked ? "1" : "0"); } catch (_) {}
    });
  }

  // Wire the terminal font-size slider. Initial value is loaded from
  // localStorage; changes apply live to all open terminals.
  if (fontSizeEl) {
    var initialPx = getCurrentFontSize();
    fontSizeEl.value = String(initialPx);
    if (fontSizeValEl) fontSizeValEl.textContent = String(initialPx);
    fontSizeEl.addEventListener("input", function () {
      applyFontSize(parseInt(fontSizeEl.value, 10));
    });
  }

  // Re-fit the visible terminal when the window resizes (e.g. Weston
  // reconfigure). Hidden ports don't need refitting until they become
  // visible again, at which point openConsole() re-fits.
  window.addEventListener("resize", function () {
    if (currentPort == null) return;
    var s = STATE[currentPort];
    try { s && s.fit && s.fit.fit(); } catch (_) {}
  });
})();
