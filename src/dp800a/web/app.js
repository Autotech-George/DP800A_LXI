/* DP800A web GUI */
(() => {
  const CHANNELS = [1, 2, 3];
  const CH_COLORS = ["#4cc2ff", "#ffb454", "#6dd16d"];
  const SAMPLE_HZ = 2;          // matches server-side WS push rate
  const DIVISIONS = 10;         // oscilloscope-style major divisions across X
  const state = {
    config: null,
    connected: false,
    snapshot: null,
    ws: null,
    wsBackoff: 1000,
    samples: [],                // ring buffer: [{ t: epoch_ms, v:[...], i:[...] }]
    windowSec: 60,              // visible timebase
    chartV: null,
    chartI: null,
  };

  // ---- helpers ---------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function toast(msg, kind = "ok") {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "show " + kind;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.className = ""), 3500);
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!r.ok) {
      let detail = `${r.status}`;
      try { const j = await r.json(); detail = j.detail || JSON.stringify(j); } catch {}
      throw new Error(detail);
    }
    if (r.status === 204) return null;
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }

  function fmt(n, d = 3) { return Number.isFinite(n) ? n.toFixed(d) : "—"; }

  // ---- channel cards ---------------------------------------------------
  function renderChannelCards() {
    const root = $("#channels");
    root.innerHTML = "";
    for (const ch of CHANNELS) {
      const cap = state.config?.limits?.[String(ch)] || { max_voltage: 30, max_current: 3 };
      const card = document.createElement("div");
      card.className = "channel-card";
      card.dataset.channel = ch;
      card.innerHTML = `
        <header>
          <h2>Channel ${ch}</h2>
          <span class="output-state output-off" data-out>OUTPUT OFF</span>
        </header>
        <div class="row">
          <label>Set Voltage (V)
            <input type="number" step="0.001" min="0" max="${cap.max_voltage}" data-set-v value="0"/>
          </label>
          <label>Set Current (A)
            <input type="number" step="0.001" min="0" max="${cap.max_current}" data-set-i value="0"/>
          </label>
          <button data-apply class="primary">Apply</button>
          <button data-toggle>Turn ON</button>
        </div>
        <div class="meas">
          <div class="cell"><div class="label">Voltage</div><div class="value" data-mv>—<span class="unit">V</span></div></div>
          <div class="cell"><div class="label">Current</div><div class="value" data-mi>—<span class="unit">A</span></div></div>
          <div class="cell"><div class="label">Power</div><div class="value" data-mp>—<span class="unit">W</span></div></div>
        </div>
        <div class="protect-row" style="margin-top:10px">
          <span class="lbl">OVP</span>
          <input type="number" step="0.01" min="0" max="${cap.max_voltage}" data-ovp value="0"/>
          <label class="inline"><input type="checkbox" data-ovp-en/> enabled</label>
          <button data-ovp-apply>Apply OVP</button>
        </div>
        <div class="protect-row">
          <span class="lbl">OCP</span>
          <input type="number" step="0.01" min="0" max="${cap.max_current}" data-ocp value="0"/>
          <label class="inline"><input type="checkbox" data-ocp-en/> enabled</label>
          <button data-ocp-apply>Apply OCP</button>
        </div>
      `;
      root.appendChild(card);

      // wire handlers
      card.querySelector("[data-apply]").addEventListener("click", () => doApply(ch, card));
      card.querySelector("[data-toggle]").addEventListener("click", () => doToggleOutput(ch, card));
      card.querySelector("[data-ovp-apply]").addEventListener("click", () => doOvp(ch, card));
      card.querySelector("[data-ocp-apply]").addEventListener("click", () => doOcp(ch, card));
    }
  }

  function updateChannelFromSnapshot(snap) {
    if (!snap || !snap.channels) return;
    for (const c of snap.channels) {
      const card = document.querySelector(`.channel-card[data-channel="${c.channel}"]`);
      if (!card) continue;
      card.querySelector("[data-mv]").innerHTML = `${fmt(c.measurement.voltage)}<span class="unit">V</span>`;
      card.querySelector("[data-mi]").innerHTML = `${fmt(c.measurement.current)}<span class="unit">A</span>`;
      card.querySelector("[data-mp]").innerHTML = `${fmt(c.measurement.power)}<span class="unit">W</span>`;
      const outEl = card.querySelector("[data-out]");
      outEl.textContent = c.output_on ? "OUTPUT ON" : "OUTPUT OFF";
      outEl.className = "output-state " + (c.output_on ? "output-on" : "output-off");
      card.querySelector("[data-toggle]").textContent = c.output_on ? "Turn OFF" : "Turn ON";
      // reflect protection state if user not editing
      const ovp = card.querySelector("[data-ovp]");
      const ocp = card.querySelector("[data-ocp]");
      if (document.activeElement !== ovp) ovp.value = c.ovp_value.toFixed(2);
      if (document.activeElement !== ocp) ocp.value = c.ocp_value.toFixed(2);
      card.querySelector("[data-ovp-en]").checked = c.ovp_enabled;
      card.querySelector("[data-ocp-en]").checked = c.ocp_enabled;
    }
    pushChartPoint(snap);
  }

  // ---- actions ---------------------------------------------------------
  async function doApply(ch, card) {
    const voltage = parseFloat(card.querySelector("[data-set-v]").value);
    const current = parseFloat(card.querySelector("[data-set-i]").value);
    try {
      await api(`/api/channel/${ch}/apply`, { method: "POST", body: JSON.stringify({ voltage, current }) });
      toast(`CH${ch} set to ${voltage} V / ${current} A`);
    } catch (e) { toast(e.message, "err"); }
  }

  async function doToggleOutput(ch, card) {
    const isOn = card.querySelector("[data-out]").textContent.includes("ON");
    const target = !isOn;
    if (target) {
      const v = card.querySelector("[data-set-v]").value;
      const i = card.querySelector("[data-set-i]").value;
      const ok = await confirmDialog(
        `Enable CH${ch} output?`,
        `Output will be enabled with setpoint ${v} V / ${i} A. Are you sure?`
      );
      if (!ok) return;
    }
    try {
      await api(`/api/channel/${ch}/output`, {
        method: "POST", body: JSON.stringify({ on: target, confirm: true }),
      });
      toast(`CH${ch} output ${target ? "ON" : "OFF"}`);
    } catch (e) { toast(e.message, "err"); }
  }

  async function doOvp(ch, card) {
    const value = parseFloat(card.querySelector("[data-ovp]").value);
    const enabled = card.querySelector("[data-ovp-en]").checked;
    try {
      await api(`/api/channel/${ch}/ovp`, { method: "POST", body: JSON.stringify({ value, enabled }) });
      toast(`CH${ch} OVP ${enabled ? "enabled" : "disabled"} @ ${value} V`);
    } catch (e) { toast(e.message, "err"); }
  }

  async function doOcp(ch, card) {
    const value = parseFloat(card.querySelector("[data-ocp]").value);
    const enabled = card.querySelector("[data-ocp-en]").checked;
    try {
      await api(`/api/channel/${ch}/ocp`, { method: "POST", body: JSON.stringify({ value, enabled }) });
      toast(`CH${ch} OCP ${enabled ? "enabled" : "disabled"} @ ${value} A`);
    } catch (e) { toast(e.message, "err"); }
  }

  // ---- system bar ------------------------------------------------------
  function wireSystemBar() {
    $("#trackingToggle").addEventListener("change", async (e) => {
      try {
        await api("/api/tracking", { method: "POST", body: JSON.stringify({ on: e.target.checked }) });
        toast(`Tracking ${e.target.checked ? "ON" : "OFF"}`);
      } catch (err) { toast(err.message, "err"); e.target.checked = !e.target.checked; }
    });
    $("#saveBtn").addEventListener("click", async () => {
      const slot = parseInt($("#memorySlot").value, 10);
      try { await api("/api/memory/save", { method: "POST", body: JSON.stringify({ slot }) }); toast(`Saved to slot ${slot}`); }
      catch (e) { toast(e.message, "err"); }
    });
    $("#recallBtn").addEventListener("click", async () => {
      const slot = parseInt($("#memorySlot").value, 10);
      try { await api("/api/memory/recall", { method: "POST", body: JSON.stringify({ slot }) }); toast(`Recalled slot ${slot}`); }
      catch (e) { toast(e.message, "err"); }
    });
    $("#rawSendBtn").addEventListener("click", async () => {
      const scpi = $("#rawInput").value.trim();
      if (!scpi) return;
      const expect = $("#rawExpect").checked;
      try {
        const r = await api("/api/raw", { method: "POST", body: JSON.stringify({ scpi, expect_response: expect }) });
        $("#rawOutput").textContent = (expect ? `< ${r.response ?? ""}\n` : "(write ok)\n") + $("#rawOutput").textContent;
      } catch (e) { toast(e.message, "err"); }
    });
  }

  // ---- connection ------------------------------------------------------
  function wireConnection() {
    $("#discoverBtn").addEventListener("click", async () => {
      $("#discoverBtn").disabled = true;
      toast("Scanning network...");
      try {
        const list = await api("/api/discover");
        const sel = $("#discoveredList");
        sel.innerHTML = '<option value="">— discovered devices —</option>';
        for (const d of list) {
          const opt = document.createElement("option");
          opt.value = d.resource;
          opt.textContent = `${d.name} @ ${d.host}`;
          sel.appendChild(opt);
        }
        toast(`Found ${list.length} device(s)`);
      } catch (e) { toast(e.message, "err"); }
      $("#discoverBtn").disabled = false;
    });
    $("#discoveredList").addEventListener("change", (e) => {
      if (e.target.value) $("#resourceInput").value = e.target.value;
    });
    $("#connectBtn").addEventListener("click", async () => {
      const resource = $("#resourceInput").value.trim();
      if (!resource) { toast("Enter a VISA resource string", "err"); return; }
      try {
        const r = await api("/api/connect", { method: "POST", body: JSON.stringify({ resource }) });
        $("#idnLabel").textContent = r.idn;
        $("#statusLed").className = "led on";
        state.connected = true;
        openTelemetryWS();
        toast(`Connected: ${r.idn}`);
      } catch (e) { toast(e.message, "err"); }
    });
    $("#disconnectBtn").addEventListener("click", async () => {
      try {
        await api("/api/disconnect", { method: "POST" });
        state.connected = false;
        $("#idnLabel").textContent = "not connected";
        $("#statusLed").className = "led off";
        if (state.ws) state.ws.close();
        toast("Disconnected");
      } catch (e) { toast(e.message, "err"); }
    });
  }

  // ---- WebSocket -------------------------------------------------------
  function openTelemetryWS() {
    if (state.ws) try { state.ws.close(); } catch {}
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws/telemetry`);
    state.ws = ws;
    ws.onmessage = (e) => {
      try {
        const snap = JSON.parse(e.data);
        state.snapshot = snap;
        if (snap.connected) {
          state.connected = true;
          $("#statusLed").className = "led on";
          if (snap.idn) $("#idnLabel").textContent = snap.idn;
          $("#trackingToggle").checked = !!snap.tracking_on;
        } else {
          state.connected = false;
          $("#statusLed").className = "led off";
        }
        updateChannelFromSnapshot(snap);
      } catch (err) { console.error(err); }
    };
    ws.onclose = () => {
      if (!state.connected) return;
      setTimeout(openTelemetryWS, state.wsBackoff);
      state.wsBackoff = Math.min(state.wsBackoff * 1.5, 10000);
    };
    ws.onopen = () => { state.wsBackoff = 1000; };
  }

  // ---- charts ----------------------------------------------------------
  function fmtTime24(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function niceStep(windowSec, divisions) {
    // Pick a "nice" step so divisions land on whole-second boundaries.
    const raw = windowSec / divisions;
    const candidates = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600];
    for (const c of candidates) if (c >= raw) return c;
    return raw;
  }

  function initCharts() {
    const baseOpts = (yLabel) => ({
      type: "line",
      data: {
        datasets: CHANNELS.map((ch, idx) => ({
          label: `CH${ch}`,
          data: [],            // [{x: secondsAgo (negative), y: value}]
          borderWidth: 2, pointRadius: 0, tension: 0.15,
          borderColor: CH_COLORS[idx],
          spanGaps: false,
          parsing: false,
        })),
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        normalized: true,
        scales: {
          x: {
            type: "linear",
            min: -state.windowSec,
            max: 0,
            offset: false,
            bounds: "ticks",
            ticks: {
              color: "#8a96a8",
              autoSkip: false,
              maxRotation: 0,
              minRotation: 0,
              stepSize: niceStep(state.windowSec, DIVISIONS),
              // Label = absolute wall-clock time at that division (24h)
              callback: (value) => fmtTime24(new Date(Date.now() + value * 1000)),
            },
            grid: { color: "#2a3340" },
          },
          y: {
            title: { display: true, text: yLabel, color: "#8a96a8" },
            ticks: { color: "#8a96a8" },
            grid: { color: "#2a3340" },
          },
        },
        plugins: {
          legend: { labels: { color: "#e8edf2" } },
          tooltip: {
            enabled: true,
            mode: "index",
            intersect: false,
            position: "nearest",
            callbacks: {
              title: (items) => items.length
                ? fmtTime24(new Date(Date.now() + items[0].parsed.x * 1000))
                : "",
              label: (item) => {
                const unit = yLabel === "Volts" ? "V" : "A";
                const y = item.parsed.y;
                return `${item.dataset.label}: ${Number.isFinite(y) ? y.toFixed(3) : "—"} ${unit}`;
              },
            },
          },
        },
        interaction: {
          mode: "index",
          intersect: false,
          axis: "x",
        },
        hover: {
          mode: "index",
          intersect: false,
          axis: "x",
        },
      },
    });
    state.chartV = new Chart($("#chartV"), baseOpts("Volts"));
    state.chartI = new Chart($("#chartI"), baseOpts("Amps"));
  }

  function applyTimebase(windowSec) {
    state.windowSec = windowSec;
    const step = niceStep(windowSec, DIVISIONS);
    for (const chart of [state.chartV, state.chartI]) {
      if (!chart) continue;
      chart.options.scales.x.min = -windowSec;
      chart.options.scales.x.max = 0;
      chart.options.scales.x.ticks.stepSize = step;
    }
    redrawCharts();
  }

  function pruneSamples() {
    // Keep a small margin beyond the visible window so points just leaving
    // the screen still draw to the edge.
    const cutoff = Date.now() - (state.windowSec + 5) * 1000;
    while (state.samples.length && state.samples[0].t < cutoff) {
      state.samples.shift();
    }
    // Hard cap: longest timebase is 30 min @ 2 Hz = 3600 samples.
    const HARD_CAP = 4096;
    if (state.samples.length > HARD_CAP) {
      state.samples.splice(0, state.samples.length - HARD_CAP);
    }
  }

  function redrawCharts() {
    if (!state.chartV || !state.chartI) return;
    const now = Date.now();
    for (let idx = 0; idx < CHANNELS.length; idx++) {
      const vData = state.chartV.data.datasets[idx].data;
      const iData = state.chartI.data.datasets[idx].data;
      vData.length = 0;
      iData.length = 0;
      for (const s of state.samples) {
        const x = (s.t - now) / 1000; // seconds-ago, <= 0
        const v = s.v[idx];
        const i = s.i[idx];
        if (v != null) vData.push({ x, y: v });
        if (i != null) iData.push({ x, y: i });
      }
    }
    state.chartV.update("none");
    state.chartI.update("none");
  }

  function pushChartPoint(snap) {
    const sample = { t: Date.now(), v: [null, null, null], i: [null, null, null] };
    for (let idx = 0; idx < CHANNELS.length; idx++) {
      const ch = CHANNELS[idx];
      const c = snap.channels?.find((x) => x.channel === ch);
      if (c) {
        sample.v[idx] = c.measurement.voltage;
        sample.i[idx] = c.measurement.current;
      }
    }
    state.samples.push(sample);
    pruneSamples();
    redrawCharts();
  }

  function wireChartControls() {
    const sel = $("#timebaseSel");
    if (sel) {
      sel.addEventListener("change", () => {
        const v = parseInt(sel.value, 10);
        if (Number.isFinite(v) && v > 0) applyTimebase(v);
      });
    }
    const clearBtn = $("#clearChartsBtn");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        state.samples.length = 0;
        redrawCharts();
        toast("Chart history cleared");
      });
    }
  }

  // ---- settings --------------------------------------------------------
  async function loadConfig() {
    state.config = await api("/api/config");
    if (state.config.last_resource && !$("#resourceInput").value) {
      $("#resourceInput").value = state.config.last_resource;
    }
    $("#rawCard").hidden = !state.config.raw_scpi_enabled;
  }

  function wireSettings() {
    $("#settingsBtn").addEventListener("click", async () => {
      await loadConfig();
      const body = $("#limitsBody");
      body.innerHTML = "";
      for (const ch of CHANNELS) {
        const lim = state.config.limits[String(ch)] || { max_voltage: 30, max_current: 3 };
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>CH${ch}</td>
          <td><input type="number" step="0.1" min="0" data-cap-v="${ch}" value="${lim.max_voltage}"/></td>
          <td><input type="number" step="0.1" min="0" data-cap-i="${ch}" value="${lim.max_current}"/></td>
        `;
        body.appendChild(tr);
      }
      $("#rawEnabled").checked = !!state.config.raw_scpi_enabled;
      $("#settingsDialog").showModal();
    });

    $("#settingsForm").addEventListener("submit", async (e) => {
      const action = e.submitter?.value;
      if (action !== "default") return; // cancel
      const limits = {};
      for (const ch of CHANNELS) {
        limits[String(ch)] = {
          max_voltage: parseFloat(document.querySelector(`[data-cap-v="${ch}"]`).value),
          max_current: parseFloat(document.querySelector(`[data-cap-i="${ch}"]`).value),
        };
      }
      const newCfg = { ...state.config, limits, raw_scpi_enabled: $("#rawEnabled").checked };
      try {
        state.config = await api("/api/config", { method: "PUT", body: JSON.stringify(newCfg) });
        renderChannelCards();
        $("#rawCard").hidden = !state.config.raw_scpi_enabled;
        toast("Settings saved");
      } catch (err) { toast(err.message, "err"); }
    });
  }

  function confirmDialog(title, body) {
    return new Promise((resolve) => {
      const dlg = $("#confirmDialog");
      $("#confirmTitle").textContent = title;
      $("#confirmBody").textContent = body;
      dlg.showModal();
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        resolve(dlg.returnValue === "ok");
      });
    });
  }

  // ---- bootstrap -------------------------------------------------------
  async function init() {
    wireConnection();
    wireSystemBar();
    wireSettings();
    initCharts();
    wireChartControls();
    // Continuous redraw so the trace scrolls left even when no new sample
    // has arrived yet (keeps the time axis flowing in real time).
    setInterval(redrawCharts, 1000 / SAMPLE_HZ);
    try {
      await loadConfig();
      renderChannelCards();
      const status = await api("/api/status");
      if (status.connected) {
        state.connected = true;
        $("#statusLed").className = "led on";
        if (status.idn) $("#idnLabel").textContent = status.idn;
        if (status.resource) $("#resourceInput").value = status.resource;
        openTelemetryWS();
      }
    } catch (e) { console.error(e); }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
