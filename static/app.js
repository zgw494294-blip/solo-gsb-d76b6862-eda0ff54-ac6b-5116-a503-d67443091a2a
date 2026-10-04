"use strict";

// ---------- 常量 ----------
const TENS_40_180 = range(40, 180, 10);
const DURATIONS = [5, 10, 15];
const PALETTE = ["#1d6fa8", "#1b9e77", "#d95f02", "#7570b3"];

const DEFAULTS = [
  { name: "材料A", holdLow: 60, holdHigh: 100, duration: 10, tolerance: 150 },
  { name: "材料B", holdLow: 80, holdHigh: 120, duration: 5, tolerance: 160 },
  { name: "材料C", holdLow: 50, holdHigh: 90, duration: 15, tolerance: 140 },
];

const EMPTY_RESULT = {
  name: "",
  holdLow: 60,
  holdHigh: 100,
  duration: 10,
  tolerance: 150,
};

// ---------- 工具 ----------
function range(a, b, step) {
  const out = [];
  for (let v = a; v <= b; v += step) out.push(v);
  return out;
}

function el(tag, opts = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(opts)) {
    if (k === "class") node.className = v;
    else if (k === "attrs") Object.entries(v).forEach(([ak, av]) => node.setAttribute(ak, av));
    else node[k] = v;
  }
  children.filter(Boolean).forEach((c) =>
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c)
  );
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  return node;
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = {};
  }
  return { ok: resp.ok, status: resp.status, data };
}

function detailOf(data) {
  if (!data) return "请求失败。";
  if (typeof data.detail === "string") return data.detail;
  if (data.detail && typeof data.detail === "object" && data.detail.detail) return data.detail.detail;
  return "请求失败。";
}

function field(label, control) {
  return el("label", { class: "field" },
    el("span", { class: "field-label", textContent: label }), control);
}

function tensSelect(value, values, key, idx) {
  const sel = el("select", { attrs: { "data-key": key, "data-idx": String(idx) } });
  values.forEach((v) => sel.appendChild(el("option", { value: String(v), textContent: String(v) })));
  sel.value = String(value);
  return sel;
}

function rebuildToleranceOptions(sel, holdHigh, current) {
  sel.innerHTML = "";
  for (let v = holdHigh; v <= 200; v += 10) {
    sel.appendChild(el("option", { value: String(v), textContent: String(v) }));
  }
  sel.value = String(current >= holdHigh ? current : holdHigh);
}

// 单个「材料字段」行（无标题色条，供配方表单与即时表单共用）
function materialFields(values, idx) {
  const box = el("div", { class: "fields", attrs: { "data-midx": String(idx) } });
  const nameInput = el("input", {
    type: "text", value: values.name || "", maxlength: 20,
    placeholder: `材料${idx + 1}（可留空）`,
    attrs: { "data-key": "name", "data-idx": String(idx) },
  });
  const lowSel = tensSelect(values.holdLow, TENS_40_180, "holdLow", idx);
  const highSel = tensSelect(values.holdHigh, TENS_40_180, "holdHigh", idx);
  const durSel = el("select", { attrs: { "data-key": "duration", "data-idx": String(idx) } });
  DURATIONS.forEach((v) =>
    durSel.appendChild(el("option", { value: String(v), textContent: `${v} 分钟` }))
  );
  durSel.value = String(values.duration);
  const tolSel = el("select", { attrs: { "data-key": "tolerance", "data-idx": String(idx) } });
  rebuildToleranceOptions(tolSel, values.holdHigh, values.tolerance);

  box.appendChild(field("名称（可留空）", nameInput));
  box.appendChild(field("保温下限 °C", lowSel));
  box.appendChild(field("保温上限 °C", highSel));
  box.appendChild(field("连续保温时长", durSel));
  box.appendChild(field("耐受上限 °C", tolSel));
  return box;
}

function readMaterialFields(container, idx) {
  const q = (key) => container.querySelector(`[data-idx="${idx}"][data-key="${key}"]`);
  return {
    name: q("name").value.trim(),
    holdLow: Number(q("holdLow").value),
    holdHigh: Number(q("holdHigh").value),
    duration: Number(q("duration").value),
    tolerance: Number(q("tolerance").value),
  };
}

function bindToleranceRebuild(container) {
  container.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t && t.dataset.key === "holdHigh") {
      const idx = Number(t.dataset.idx);
      const tolSel = container.querySelector(`[data-idx="${idx}"][data-key="tolerance"]`);
      rebuildToleranceOptions(tolSel, Number(t.value), Number(tolSel.value));
    }
  });
}

// ---------- 标签页 ----------
const tabs = document.getElementById("tabs");
const panels = {
  schedule: document.getElementById("panel-schedule"),
  recipes: document.getElementById("panel-recipes"),
  drafts: document.getElementById("panel-drafts"),
};
tabs.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  Object.entries(panels).forEach(([key, p]) => {
    p.hidden = key !== btn.dataset.tab;
    p.classList.toggle("active", key === btn.dataset.tab);
  });
  if (btn.dataset.tab === "recipes") loadRecipes();
  if (btn.dataset.tab === "drafts") {
    loadRecipesIntoRefs();
    loadDrafts();
  }
});

// ============================================================
// 结果视图：在指定 slot 内构建与原页面相同的结果结构并渲染
// ============================================================
function createResultView(slot) {
  slot.innerHTML = "";
  const root = el("div");
  root.innerHTML = `
    <div data-role="ok" hidden>
      <section class="card">
        <h2 data-role="title">编排结果 <span class="badge badge-ok">有解</span></h2>
        <div class="summary" data-role="summary"></div>
        <div class="chart-wrap">
          <svg data-role="chart" viewBox="0 0 900 380" role="img" aria-label="炉温设定曲线"></svg>
          <ul class="legend" data-role="legend"></ul>
        </div>
        <div data-role="windows" class="windows"></div>
        <p class="shared-note" data-role="shared" hidden></p>
      </div>
      <section class="card">
        <h2>档位明细</h2>
        <div class="table-wrap">
          <table data-role="steps">
            <thead>
              <tr>
                <th>档位</th>
                <th data-role="th-phase" hidden>阶段</th>
                <th>设定温度 (°C)</th>
                <th>时长 (分钟)</th>
                <th>与上档温差 (°C)</th>
                <th>该档用于</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </section>
    </div>
    <div data-role="fail" hidden>
      <section class="card card-fail">
        <h2><span class="badge badge-fail">无解</span></h2>
        <p data-role="reason"></p>
      </section>
    </div>`;
  slot.appendChild(root);
  const $ = (role) => root.querySelector(`[data-role="${role}"]`);
  return {
    clear() {
      $("ok").hidden = true;
      $("fail").hidden = true;
    },
    render(data, title) {
      this.clear();
      if (!data.solvable) {
        $("fail").hidden = false;
        $("reason").textContent = data.reason || "在给定约束下无解。";
        return;
      }
      $("ok").hidden = false;
      if (title) $("title").firstChild.textContent = title + " ";
      renderSummary(data, $("summary"));
      renderChart(data, $("chart"));
      renderLegend(data, $("legend"));
      renderWindows(data, $("windows"), $("shared"));
      renderSteps(data, $("steps"), $("th-phase"));
    },
  };
}

function kv(k, v) {
  return el("div", { class: "kv" }, document.createTextNode(`${k}：`), el("b", { textContent: v }));
}

function renderSummary(data, box) {
  box.innerHTML = "";
  box.appendChild(kv("总档数", `${data.stepCount} 档（≤ 20）`));
  box.appendChild(kv("总时长", `${data.totalMinutes} 分钟`));
  box.appendChild(kv("耐受上限（取所有材料最小值）", `${data.toleranceCap}°C`));
  if (data.hasExecuted) {
    box.appendChild(kv("已执行档位", `${data.executedCount} 档（${data.executedCount * 5} 分钟）`));
    if (data.alreadyComplete) {
      box.appendChild(kv("后续编排", "0 档（已全部达标且回到 20°C，无需续排）"));
    } else {
      box.appendChild(kv("后续档位", `${data.continuationCount} 档（${data.continuationMinutes} 分钟）`));
    }
  }
  const seq = el("div", { class: "kv" });
  seq.appendChild(el("span", { textContent: "完整温度序列：" }));
  seq.appendChild(el("code", { textContent: data.temperatures.join(" → ") + " °C" }));
  box.appendChild(seq);
  if (data.hasExecuted && !data.alreadyComplete) {
    const seqE = el("div", { class: "kv" });
    seqE.appendChild(el("span", { textContent: "已执行：" }));
    seqE.appendChild(el("code", { class: "seq-exec", textContent: data.executedTemperatures.join(" → ") + " °C" }));
    box.appendChild(seqE);
    const seqC = el("div", { class: "kv" });
    seqC.appendChild(el("span", { textContent: "后续编排：" }));
    seqC.appendChild(el("code", { class: "seq-cont", textContent: data.continuationTemperatures.join(" → ") + " °C" }));
    box.appendChild(seqC);
  }
  box.appendChild(el("div", { class: "kv", textContent: "已满足：总档数最少；同档数下后续温度序列字典序最小。" }));
}

function renderWindows(data, box, note) {
  box.innerHTML = "";
  const boundary = data.executedCount || 0;
  data.windows.forEach((w, i) => {
    const color = PALETTE[i % PALETTE.length];
    const card = el("div", {
      class: "window-card",
      attrs: { style: `border-left-color:${color}` },
    });
    card.appendChild(el("h4", { textContent: w.material }));
    card.appendChild(
      el("p", { textContent: `首次达标连续区间：第 ${w.startStep}～${w.endStep} 档（共 ${w.minutes} 分钟）` })
    );
    card.appendChild(
      el("p", {}, document.createTextNode("区间温度："),
        el("span", { class: "temps", textContent: w.temperatures.join(" → ") + " °C" }))
    );
    if (boundary > 0) {
      let phaseText;
      if (w.endStep <= boundary) phaseText = "该区间全部位于已执行档位中。";
      else if (w.startStep > boundary) phaseText = "该区间全部位于后续档位中。";
      else phaseText = `该区间横跨已执行（第 1～${boundary} 档）与后续档位。`;
      card.appendChild(el("p", { class: "window-phase", textContent: phaseText }));
    }
    box.appendChild(card);
  });

  if (data.sharedSteps.length > 0) {
    note.hidden = false;
    const namesAt = (s) => data.steps.find((st) => st.index === s).inWindow.join("、");
    const parts = data.sharedSteps.map((s) => `第 ${s} 档（${namesAt(s)}）`);
    note.textContent = `共用档位：${parts.join("；")}。`;
  } else {
    note.hidden = true;
  }
}

function renderSteps(data, table, thPhase) {
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  thPhase.hidden = !data.hasExecuted;

  data.windows.forEach((w, i) => {
    w.__color = PALETTE[i % PALETTE.length];
  });
  const colorOf = new Map(data.windows.map((w) => [w.material, w.__color]));

  data.steps.forEach((st) => {
    const tr = el("tr", { class: st.executed ? "row-exec" : "row-cont" });
    tr.appendChild(el("td", { textContent: String(st.index) }));
    if (data.hasExecuted) {
      tr.appendChild(
        el("td", {},
          el("span", {
            class: st.executed ? "phase-tag phase-exec" : "phase-tag phase-cont",
            textContent: st.executed ? "已执行" : "后续",
          }))
      );
    }
    tr.appendChild(el("td", { textContent: String(st.temperature) }));
    tr.appendChild(el("td", { textContent: String(st.minutes) }));
    const deltaText = st.deltaFromPrev === 0 ? "0" : (st.deltaFromPrev > 0 ? `+${st.deltaFromPrev}` : `${st.deltaFromPrev}`);
    tr.appendChild(el("td", { textContent: deltaText }));

    const useCell = el("td", { class: "use-cell" });
    if (st.inWindow.length === 0) {
      useCell.appendChild(el("span", { class: "dash", textContent: "—（升降温 / 末档）" }));
    } else {
      st.inWindow.forEach((name) => {
        useCell.appendChild(
          el("span", {
            class: "tag" + (st.shared ? " shared" : ""),
            textContent: name,
            attrs: { style: `background:${colorOf.get(name)}` },
          })
        );
      });
      if (st.shared) useCell.appendChild(document.createTextNode(" 共用"));
    }
    tr.appendChild(useCell);
    tbody.appendChild(tr);
  });
}

function renderLegend(data, ul) {
  ul.innerHTML = "";
  data.materials.forEach((m, i) => {
    ul.appendChild(
      el("li", {},
        el("span", { class: "key", attrs: { style: `background:${PALETTE[i % PALETTE.length]}` } }),
        document.createTextNode(`${m.name} 保温区间 ${m.holdLow}–${m.holdHigh}°C`))
    );
  });
  ul.appendChild(el("li", {}, el("span", { class: "key line" }),
    document.createTextNode(`耐受上限 ${data.toleranceCap}°C（所有档位不得超过）`)));
  if (data.hasExecuted) {
    ul.appendChild(el("li", {}, el("span", { class: "key series-exec" }),
      document.createTextNode("已执行档位曲线")));
    if (!data.alreadyComplete) {
      ul.appendChild(el("li", {}, el("span", { class: "key series" }),
        document.createTextNode("后续编排曲线（接续已执行末档，末档回到 20°C）")));
    } else {
      ul.appendChild(el("li", {}, document.createTextNode("已全部达标且回到 20°C，后续为空。")));
    }
  } else {
    ul.appendChild(el("li", {}, el("span", { class: "key series" }),
      document.createTextNode("设定温度曲线（第 0 点为初态 20°C，末档回到 20°C）")));
  }
}

function renderChart(data, svg) {
  svg.innerHTML = "";
  const W = 900, H = 380;
  const M = { t: 26, r: 22, b: 48, l: 54 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const seq = data.temperatures;
  const n = seq.length;
  const full = [20].concat(seq);

  const maxT = Math.max(...seq);
  const yMax = Math.max(60, Math.min(200, Math.ceil((maxT + 20) / 20) * 20));
  const x = (k) => M.l + (pw * k) / n;
  const y = (t) => M.t + ph * (1 - (t - 20) / (yMax - 20));

  data.materials.forEach((m, i) => {
    const color = PALETTE[i % PALETTE.length];
    svg.appendChild(svgEl("rect", {
      x: M.l, y: y(m.holdHigh), width: pw,
      height: y(m.holdLow) - y(m.holdHigh),
      fill: color, "fill-opacity": 0.10,
    }));
  });

  for (let t = 20; t <= yMax; t += 20) {
    svg.appendChild(svgEl("line", {
      x1: M.l, x2: M.l + pw, y1: y(t), y2: y(t),
      stroke: t === 20 ? "#9fb3c8" : "#e6ecf2", "stroke-width": 1,
    }));
    const txt = svgEl("text", {
      x: M.l - 8, y: y(t) + 3.5, "text-anchor": "end",
      "font-size": 10, fill: "#7b8794",
    });
    txt.textContent = t;
    svg.appendChild(txt);
  }

  if (data.toleranceCap <= yMax) {
    svg.appendChild(svgEl("line", {
      x1: M.l, x2: M.l + pw,
      y1: y(data.toleranceCap), y2: y(data.toleranceCap),
      stroke: "#6b7280", "stroke-width": 1.4, "stroke-dasharray": "6 4",
    }));
  }

  svg.appendChild(svgEl("line", {
    x1: M.l, x2: M.l + pw, y1: y(20), y2: y(20),
    stroke: "#9fb3c8", "stroke-width": 1,
  }));
  const initLab = svgEl("text", { x: x(0), y: H - M.b + 18, "text-anchor": "middle", "font-size": 10, fill: "#7b8794" });
  initLab.textContent = "初态";
  svg.appendChild(initLab);
  for (let k = 1; k <= n; k++) {
    const txt = svgEl("text", { x: x(k), y: H - M.b + 18, "text-anchor": "middle", "font-size": 10, fill: "#52606d" });
    txt.textContent = k;
    svg.appendChild(txt);
  }

  const boundary = data.executedCount || 0;
  const pts = full.map((t, k) => `${x(k)},${y(t)}`);
  if (data.hasExecuted && boundary < n) {
    svg.appendChild(svgEl("polyline", {
      points: pts.slice(0, boundary + 1).join(" "),
      fill: "none", stroke: "#475569", "stroke-width": 2.4,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    svg.appendChild(svgEl("polyline", {
      points: pts.slice(boundary).join(" "),
      fill: "none", stroke: "#c2410c", "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    svg.appendChild(svgEl("line", {
      x1: x(boundary), x2: x(boundary), y1: M.t, y2: M.t + ph,
      stroke: "#94a3b8", "stroke-width": 1, "stroke-dasharray": "4 4",
    }));
    const divLab = svgEl("text", {
      x: x(boundary), y: M.t - 8, "text-anchor": "middle",
      "font-size": 10, fill: "#64748b",
    });
    divLab.textContent = "已执行 ｜ 后续";
    svg.appendChild(divLab);
  } else {
    svg.appendChild(svgEl("polyline", {
      points: pts.join(" "),
      fill: "none", stroke: data.hasExecuted ? "#475569" : "#c2410c",
      "stroke-width": data.hasExecuted ? 2.4 : 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }

  data.windows.forEach((w, i) => {
    const color = PALETTE[i % PALETTE.length];
    const ks = [];
    for (let k = w.startStep; k <= w.endStep; k++) ks.push(k);
    svg.appendChild(svgEl("polyline", {
      points: ks.map((k) => `${x(k)},${y(full[k])}`).join(" "),
      fill: "none", stroke: color, "stroke-width": 5,
      "stroke-linecap": "round", "stroke-linejoin": "round", "stroke-opacity": 0.85,
    }));
    ks.forEach((k) => svg.appendChild(svgEl("circle", {
      cx: x(k), cy: y(full[k]), r: 5, fill: color, stroke: "#fff", "stroke-width": 1.2,
    })));
  });

  for (let k = 1; k <= n; k++) {
    const isExec = data.hasExecuted && k <= boundary;
    svg.appendChild(svgEl("circle", {
      cx: x(k), cy: y(full[k]), r: 3,
      fill: isExec ? "#475569" : "#b91c1c",
    }));
    const lab = svgEl("text", {
      x: x(k), y: y(full[k]) - 7, "text-anchor": "middle",
      "font-size": 9.5, fill: "#334e68",
    });
    lab.textContent = full[k];
    svg.appendChild(lab);
  }
  [0, n].forEach((k) =>
    svg.appendChild(svgEl("circle", {
      cx: x(k), cy: y(full[k]), r: 3.5,
      fill: "#fff", stroke: "#627d98", "stroke-width": 1.6,
    }))
  );
}

// ============================================================
// 标签页 1：即时编排（原表单逻辑，结果改用 createResultView）
// ============================================================
const materialsBox = document.getElementById("materials");
const countSelect = document.getElementById("material-count");
const form = document.getElementById("schedule-form");
const formError = document.getElementById("form-error");
const submitBtn = document.getElementById("submit-btn");
const executedInput = document.getElementById("executed-input");
const scheduleView = createResultView(document.getElementById("schedule-result-slot"));
scheduleView.clear();

function buildRows() {
  materialsBox.innerHTML = "";
  DEFAULTS.forEach((d, i) => {
    const color = PALETTE[i];
    const fields = materialFields(d, i);
    const row = el(
      "div",
      { class: "material-row", attrs: { "data-row": String(i) } },
      el(
        "div",
        { class: "row-title" },
        el("span", { class: "swatch", attrs: { style: `background:${color}` } }),
        document.createTextNode(`材料 ${i + 1}`)
      ),
      fields
    );
    materialsBox.appendChild(row);
  });
  applyCount();
}

function applyCount() {
  const n = Number(countSelect.value);
  document.querySelectorAll("#materials [data-row]").forEach((row) => {
    row.hidden = Number(row.dataset.row) >= n;
  });
}

function readForm() {
  const n = Number(countSelect.value);
  const out = [];
  for (let i = 0; i < n; i++) out.push(readMaterialFields(materialsBox, i));
  return out;
}

materialsBox.addEventListener("change", (ev) => {
  scheduleView.clear();
  formError.hidden = true;
  const t = ev.target;
  if (t && t.dataset.key === "holdHigh") {
    const idx = Number(t.dataset.idx);
    const tolSel = materialsBox.querySelector(`[data-idx="${idx}"][data-key="tolerance"]`);
    rebuildToleranceOptions(tolSel, Number(t.value), Number(tolSel.value));
  }
});
materialsBox.addEventListener("input", () => {
  scheduleView.clear();
  formError.hidden = true;
});
countSelect.addEventListener("change", () => {
  applyCount();
  scheduleView.clear();
  formError.hidden = true;
});

function clientValidate(items) {
  for (let i = 0; i < items.length; i++) {
    const m = items[i];
    const tag = m.name || `第 ${i + 1} 种材料`;
    if (m.holdLow > m.holdHigh) return `「${tag}」的保温下限不得高于保温上限。`;
    if (m.tolerance < m.holdHigh)
      return `「${tag}」的耐受上限不得低于保温上限（${m.holdHigh}°C）。`;
  }
  return null;
}

function parseExecuted() {
  const raw = executedInput.value.trim();
  if (!raw) return { values: null, error: null };
  const parts = raw.split(/[,，\s]+/).filter(Boolean);
  if (parts.length < 1 || parts.length > 20)
    return { values: null, error: "已执行档位数量必须为 1～20 个。" };
  const values = [];
  for (let i = 0; i < parts.length; i++) {
    const tok = parts[i];
    if (!/^[+-]?\d+$/.test(tok))
      return { values: null, error: `已执行记录第 ${i + 1} 档「${tok}」不是整数温度。` };
    const t = Number(tok);
    if (t < 20 || t > 200)
      return { values: null, error: `已执行记录第 ${i + 1} 档温度 ${t}°C 超出允许范围 20～200°C。` };
    if (t % 10 !== 0)
      return { values: null, error: `已执行记录第 ${i + 1} 档温度 ${t}°C 不是整十数。` };
    values.push(t);
  }
  let prev = 20;
  for (let i = 0; i < values.length; i++) {
    if (Math.abs(values[i] - prev) > 20)
      return {
        values: null,
        error: `已执行记录第 ${i + 1} 档温度 ${values[i]}°C 与上一档 ${prev}°C 温差 ${Math.abs(values[i] - prev)}°C，超过 20°C 上限。`,
      };
    prev = values[i];
  }
  return { values, error: null };
}

executedInput.addEventListener("input", () => {
  scheduleView.clear();
  formError.hidden = true;
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  formError.hidden = true;
  const items = readForm();
  const err = clientValidate(items);
  if (err) {
    scheduleView.clear();
    showFormError(err);
    return;
  }
  const { values: executed, error: execErr } = parseExecuted();
  if (execErr) {
    scheduleView.clear();
    showFormError(execErr);
    return;
  }

  const body = { materials: items };
  if (executed) body.executedTemperatures = executed;

  submitBtn.disabled = true;
  submitBtn.textContent = "求解中…";
  try {
    const { ok, data } = await api("/api/schedule", { method: "POST", body: JSON.stringify(body) });
    if (!ok) {
      scheduleView.clear();
      showFormError(detailOf(data));
      return;
    }
    scheduleView.render(data, "编排结果");
  } catch (e) {
    scheduleView.clear();
    showFormError("无法连接后端服务，请确认服务已启动。");
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "一键启动编排";
  }
});

function showFormError(msg) {
  formError.textContent = msg;
  formError.hidden = false;
}

// ============================================================
// 标签页 2：材料配方（创建 / 读取 / 整份修改）
// ============================================================
const recipeForm = document.getElementById("recipe-form");
const recipeFieldsBox = document.getElementById("recipe-fields");
const recipeError = document.getElementById("recipe-error");
const recipeMsg = document.getElementById("recipe-message");
const recipeEditId = document.getElementById("recipe-edit-id");
const recipeSaveBtn = document.getElementById("recipe-save-btn");
const recipeCancelBtn = document.getElementById("recipe-cancel-btn");
const recipeFormTitle = document.getElementById("recipe-form-title");

recipeFieldsBox.appendChild(materialFields(EMPTY_RESULT, 0));
bindToleranceRebuild(recipeFieldsBox);

function resetRecipeForm() {
  recipeFieldsBox.innerHTML = "";
  recipeFieldsBox.appendChild(materialFields(EMPTY_RESULT, 0));
  recipeEditId.value = "";
  recipeSaveBtn.textContent = "创建配方";
  recipeCancelBtn.hidden = true;
  recipeFormTitle.textContent = "新建材料配方";
  recipeError.hidden = true;
  recipeMsg.hidden = true;
}

recipeCancelBtn.addEventListener("click", resetRecipeForm);

recipeForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  recipeError.hidden = true;
  recipeMsg.hidden = true;
  const payload = readMaterialFields(recipeFieldsBox, 0);
  const editId = recipeEditId.value;
  recipeSaveBtn.disabled = true;
  try {
    const path = editId ? `/api/recipes/${editId}` : "/api/recipes";
    const method = editId ? "PUT" : "POST";
    const { ok, status, data } = await api(path, { method, body: JSON.stringify(payload) });
    if (!ok) {
      recipeError.textContent = detailOf(data);
      recipeError.hidden = false;
      return;
    }
    if (editId) {
      const n = data.recomputedDrafts.length;
      const unsolvable = data.recomputedDrafts.filter((d) => !d.solvable).length;
      recipeMsg.textContent =
        `配方 ${data.recipe.id} 已整份修改（版本 ${data.recipe.version}）；` +
        `引用它的 ${n} 个炉次草稿已按新参数重算、版本各 +1` +
        (unsolvable ? `，其中 ${unsolvable} 个当前无解（旧曲线已清除并记录原因）。` : "。");
    } else {
      recipeMsg.textContent = `配方 ${data.id} 已创建（版本 1）。`;
    }
    recipeMsg.hidden = false;
    resetRecipeFormKeepMsg();
    await loadRecipes();
  } catch (e) {
    recipeError.textContent = "无法连接后端服务，请确认服务已启动。";
    recipeError.hidden = false;
  } finally {
    recipeSaveBtn.disabled = false;
  }
});

function resetRecipeFormKeepMsg() {
  recipeFieldsBox.innerHTML = "";
  recipeFieldsBox.appendChild(materialFields(EMPTY_RESULT, 0));
  recipeEditId.value = "";
  recipeSaveBtn.textContent = "创建配方";
  recipeCancelBtn.hidden = true;
  recipeFormTitle.textContent = "新建材料配方";
  recipeError.hidden = true;
}

async function loadRecipes() {
  const tbody = document.querySelector("#recipes-table tbody");
  tbody.innerHTML = "";
  const { ok, data } = await api("/api/recipes");
  const recipes = ok ? data : [];
  document.getElementById("recipe-count").textContent = `（共 ${recipes.length} 个）`;
  document.getElementById("recipe-empty").hidden = recipes.length !== 0;
  recipes.forEach((r) => {
    const disabled = r.status === "disabled";
    const tr = el("tr", { class: "rrecipe-" + r.id + (disabled ? " row-disabled" : "") });
    tr.appendChild(el("td", { textContent: String(r.id) }));
    tr.appendChild(el("td", { textContent: r.name || "—" }));
    tr.appendChild(el("td", { textContent: String(r.holdLow) }));
    tr.appendChild(el("td", { textContent: String(r.holdHigh) }));
    tr.appendChild(el("td", { textContent: String(r.duration) }));
    tr.appendChild(el("td", { textContent: String(r.tolerance) }));
    tr.appendChild(el("td", {}, el("span", { class: "ver-badge", textContent: `v${r.version}` })));
    tr.appendChild(el("td", {}, el("span", {
      class: "status-badge " + (disabled ? "status-disabled" : "status-active"),
      textContent: disabled ? "已停用" : "可用",
    })));
    tr.appendChild(el("td", { class: "ts", textContent: fmtTime(r.updatedAt) }));
    const editBtn = el("button", {
      class: "btn-secondary btn-small", type: "button", textContent: "整份修改",
    });
    editBtn.addEventListener("click", () => fillRecipeForm(r));
    const toggleBtn = el("button", {
      class: "btn-secondary btn-small", type: "button",
      textContent: disabled ? "恢复" : "停用",
    });
    toggleBtn.addEventListener("click", () => toggleRecipeStatus(r));
    tr.appendChild(el("td", {}, el("div", { class: "op-btns" }, editBtn, toggleBtn)));
    tbody.appendChild(tr);
  });
  // 同步炉次草稿页的引用下拉
  populateDraftRefs(recipes);
}

// 停用 / 恢复配方：只切换状态，不动参数与版本，也不重算任何炉次
async function toggleRecipeStatus(r) {
  const disabled = r.status === "disabled";
  recipeError.hidden = true;
  recipeMsg.hidden = true;
  try {
    const { ok, data } = await api(`/api/recipes/${r.id}/${disabled ? "enable" : "disable"}`, { method: "POST" });
    if (!ok) {
      recipeError.textContent = detailOf(data);
      recipeError.hidden = false;
      return;
    }
    recipeMsg.textContent = disabled
      ? `配方 ${r.id} 已恢复为可用，可再次被新炉次引用。`
      : `配方 ${r.id} 已停用：新建炉次不能再引用它；已有引用草稿不受影响，仍可重算与归档。`;
    recipeMsg.hidden = false;
    await loadRecipes();
  } catch (e) {
    recipeError.textContent = "无法连接后端服务，请确认服务已启动。";
    recipeError.hidden = false;
  }
}

function fillRecipeForm(r) {
  recipeFieldsBox.innerHTML = "";
  recipeFieldsBox.appendChild(materialFields(r, 0));
  recipeEditId.value = String(r.id);
  recipeSaveBtn.textContent = `保存修改（版本 ${r.version} → ${r.version + 1}）`;
  recipeCancelBtn.hidden = false;
  recipeFormTitle.textContent = `整份修改配方 #${r.id}`;
  recipeError.hidden = true;
  recipeMsg.hidden = true;
  if (recipeForm.scrollIntoView) recipeForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

function fmtTime(s) {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString("zh-CN", { hour12: false });
  } catch (e) {
    return s;
  }
}

// ============================================================
// 标签页 3：炉次草稿
// ============================================================
const draftForm = document.getElementById("draft-form");
const draftError = document.getElementById("draft-error");
const draftCountSel = document.getElementById("draft-count");
const draftRefsBox = document.getElementById("draft-refs");
const draftDetailCard = document.getElementById("draft-detail-card");
const draftMeta = document.getElementById("draft-meta");
const draftResultView = createResultView(document.getElementById("draft-result-slot"));
draftResultView.clear();

let cachedRecipes = [];

// ----- 炉次详情的请求编排状态（防止切换炉次 / 切走再返回时响应串用）-----
// 只有“当前选择的最新有效请求”允许改写详情/曲线/提示/按钮。
let viewedDraft = null; // 最近一次成功读回的炉次（归档按钮据此判定资格）
let detailSeq = 0; // 查看/刷新的代际：每次 openDraft +1；归档回包也按点击时
                   // 代际归位，切走再回到同一炉次（ID 相同、代际已新）同样作废
let getInFlight = false; // 当前代际的 GET 是否仍在途（此时禁用归档）
let detailTargetId = null; // 当前代际 GET 所请求的炉次 ID
let archivingIds = new Set(); // 已提交、尚未返回的归档请求所绑定的炉次 ID
const draftDetailError = document.getElementById("draft-detail-error");
const draftDetailOk = document.getElementById("draft-detail-ok");
const draftArchiveBtn = document.getElementById("draft-archive-btn");
const draftSheetBtn = document.getElementById("draft-sheet-btn");
const draftSheetError = document.getElementById("draft-sheet-error");
const draftSheetOk = document.getElementById("draft-sheet-ok");
// 已提交、尚未返回的工艺单下载请求所绑定的炉次 ID（同炉次去重，不同炉次独立）
let downloadingIds = new Set();

// 详情仍在加载（当前选择的最新 GET 未成功读回）期间，归档不可用
function detailLoading() {
  return getInFlight;
}

function syncArchiveButton() {
  // 工艺单按钮先无条件同步：加载中 / 无有效详情 / 活动草稿等分支随后
  // 提前 return，也必须保证下载按钮被隐藏。
  syncSheetButton();
  if (detailLoading()) {
    draftArchiveBtn.disabled = true;
    draftArchiveBtn.textContent = "详情加载中…";
    return;
  }
  if (!viewedDraft) {
    draftArchiveBtn.disabled = true;
    draftArchiveBtn.textContent = "核准归档";
    return;
  }
  if (archivingIds.has(viewedDraft.id)) {
    draftArchiveBtn.disabled = true;
    draftArchiveBtn.textContent = "归档提交中…";
    return;
  }
  draftArchiveBtn.disabled = viewedDraft.status === "archived";
  draftArchiveBtn.textContent = viewedDraft.status === "archived"
    ? "已归档（方案已冻结）"
    : `核准归档（携带版本 v${viewedDraft.version}）`;
}

// 工艺单下载按钮只在「当前已归档详情成功加载后」出现：加载中、读取失败
// （含网络异常）或活动草稿一律隐藏；同一炉次的下载在自身响应回来前禁用
// 并去重，切到别的炉次不影响其在途请求。
function syncSheetButton() {
  if (viewedDraft && viewedDraft.status === "archived" && !getInFlight) {
    draftSheetBtn.hidden = false;
    if (downloadingIds.has(viewedDraft.id)) {
      draftSheetBtn.disabled = true;
      draftSheetBtn.textContent = "工艺单下载中…";
    } else {
      draftSheetBtn.disabled = false;
      draftSheetBtn.textContent = `下载 HTML 工艺单（#${viewedDraft.id} · v${viewedDraft.version}）`;
    }
  } else {
    draftSheetBtn.hidden = true;
    draftSheetBtn.disabled = false;
    draftSheetBtn.textContent = "下载 HTML 工艺单";
  }
}

function clearSheetMessages() {
  draftSheetError.hidden = true;
  draftSheetError.textContent = "";
  draftSheetOk.hidden = true;
  draftSheetOk.textContent = "";
}

async function loadRecipesIntoRefs() {
  const { ok, data } = await api("/api/recipes");
  cachedRecipes = ok ? data : [];
  populateDraftRefs(cachedRecipes);
}

function populateDraftRefs(recipes) {
  cachedRecipes = recipes;
  buildDraftRefRows();
}

function buildDraftRefRows() {
  const n = Number(draftCountSel.value);
  const prev = Array.from(draftRefsBox.querySelectorAll("select")).map((s) => s.value);
  // 新建炉次只允许引用「可用」配方；已停用配方不出现在下拉中
  const available = cachedRecipes.filter((r) => r.status !== "disabled");
  draftRefsBox.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const sel = el("select", { attrs: { "data-ref": String(i) } });
    sel.appendChild(el("option", { value: "", textContent: "选择配方…" }));
    available.forEach((r) => {
      sel.appendChild(el("option", {
        value: String(r.id),
        textContent: `#${r.id} ${r.name || "（未命名）"} [${r.holdLow}-${r.holdHigh}°C, ${r.duration}min, 耐受${r.tolerance}°C, v${r.version}]`,
      }));
    });
    // 之前选中的配方若已被停用（或删除），选择一并清除
    if (prev[i] && available.some((r) => String(r.id) === prev[i])) sel.value = prev[i];
    const row = el("div", { class: "ref-row" },
      el("span", { class: "ref-order", textContent: `第 ${i + 1} 个配方` }), sel);
    draftRefsBox.appendChild(row);
  }
}
draftCountSel.addEventListener("change", buildDraftRefRows);

draftForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  draftError.hidden = true;
  const ids = Array.from(draftRefsBox.querySelectorAll("select")).map((s) => s.value);
  if (ids.some((v) => v === "")) {
    draftError.textContent = "请为每个位置选择一个现有配方。";
    draftError.hidden = false;
    return;
  }
  const payload = {
    name: document.getElementById("draft-name").value.trim(),
    recipeIds: ids.map(Number),
  };
  // 前端先按已知的配方状态拦截；若页面缓存滞后（他处刚停用），
  // 服务端仍会返回 422 并在下方展示，不会保存炉次
  const byId = new Map(cachedRecipes.map((r) => [r.id, r]));
  const disabledIds = payload.recipeIds.filter((id) => {
    const r = byId.get(id);
    return r && r.status === "disabled";
  });
  if (disabledIds.length) {
    draftError.textContent = `配方 ID ${disabledIds.join(", ")} 已停用，不能用于新建炉次。`;
    draftError.hidden = false;
    return;
  }
  const btn = document.getElementById("draft-create-btn");
  btn.disabled = true;
  try {
    const { ok, status, data } = await api("/api/drafts", { method: "POST", body: JSON.stringify(payload) });
    if (!ok) {
      draftError.textContent = detailOf(data);
      draftError.hidden = false;
      return;
    }
    await loadDrafts();
    openDraft(data.id);
  } catch (e) {
    draftError.textContent = "无法连接后端服务，请确认服务已启动。";
    draftError.hidden = false;
  } finally {
    btn.disabled = false;
  }
});

async function loadDrafts() {
  const tbody = document.querySelector("#drafts-table tbody");
  tbody.innerHTML = "";
  const { ok, data } = await api("/api/drafts");
  const drafts = ok ? data : [];
  document.getElementById("draft-empty").hidden = drafts.length !== 0;
  const recipesById = new Map(cachedRecipes.map((r) => [r.id, r]));
  drafts.forEach((d) => {
    const tr = el("tr", { class: viewedDraft && viewedDraft.id === d.id ? "row-selected" : "" });
    tr.appendChild(el("td", { textContent: String(d.id) }));
    tr.appendChild(el("td", { textContent: d.name || "—" }));
    tr.appendChild(el("td", {
      textContent: d.recipeIds.map((id) => {
        const r = recipesById.get(id);
        const label = `#${id}${r && r.name ? " " + r.name : ""}`;
        return r && r.status === "disabled" ? `${label}（已停用）` : label;
      }).join(" → "),
    }));
    tr.appendChild(el("td", {}, el("span", { class: "ver-badge", textContent: `v${d.version}` })));
    tr.appendChild(el("td", {}, el("span", {
      class: "status-badge " + (d.status === "archived" ? "status-archived" : "status-active"),
      textContent: d.status === "archived" ? "已归档" : "活动",
    })));
    tr.appendChild(el("td", {
      textContent: d.solvable ? `有解（${d.result.stepCount} 档）` : "无解",
      class: d.solvable ? "" : "text-fail",
    }));
    tr.appendChild(el("td", { class: "ts", textContent: fmtTime(d.updatedAt) }));
    const viewBtn = el("button", { class: "btn-secondary btn-small", type: "button", textContent: "查看" });
    viewBtn.addEventListener("click", () => openDraft(d.id));
    tr.appendChild(el("td", {}, viewBtn));
    tbody.appendChild(tr);
  });
}

// 查看或刷新某炉次：一发起，旧详情立即失去归档资格；在最新 GET 成功
// 读回前，归档按钮保持禁用。请求自带代际序号：切走再回来（哪怕仍是同一
// 炉次）后，旧 GET 与旧归档回包的成功/失败/网络异常一律按代际作废，不得
// 改写当前详情，也不得再代为触发新的详情读取。
async function openDraft(id, banner) {
  const seq = ++detailSeq;
  viewedDraft = null; // 旧详情即刻失去归档资格，哪怕请求还在途
  getInFlight = true;
  detailTargetId = id;
  // 注意：不清空 archivingIds。切到别的炉次时，甲已提交的归档照常生效；
  // 同一炉次在归档返回前刷新（或切走再回到该炉次），也仍受“不得重复提交”
  // 约束（按钮禁用）；该旧归档回包到达时按代际作废，不再触发 GET。

  const card = draftDetailCard;
  card.hidden = false;
  // 切换炉次 / 刷新详情：立即清除旧试算与旧复用结论（含旧在途响应的回填资格）
  hideTrial();
  hideReuse();
  const titleEl = document.getElementById("draft-detail-title");
  titleEl.textContent = `炉次 #${id} 详情加载中…`;
  draftMeta.innerHTML = "";
  draftResultView.clear();
  // 一切换 / 刷新即作废旧下载提示；旧下载的迟到回包按代际丢弃，不写提示
  clearSheetMessages();
  if (banner && banner.kind === "error") {
    draftDetailError.textContent = banner.text;
    draftDetailError.hidden = false;
  } else if (banner && banner.kind === "ok") {
    draftDetailOk.textContent = banner.text;
    draftDetailOk.hidden = false;
  } else {
    draftDetailError.hidden = true;
    draftDetailOk.hidden = true;
  }
  syncArchiveButton();

  try {
    const { ok, data } = await api(`/api/drafts/${id}`);
    if (seq !== detailSeq) return; // 已切到更新的查看/刷新：本次响应作废
    getInFlight = false;
    if (!ok) {
      viewedDraft = null; // 读取失败：仍然没有可归档的有效详情
      titleEl.textContent = `炉次 #${id}`;
      draftDetailError.textContent = detailOf(data);
      draftDetailError.hidden = false;
      draftDetailOk.hidden = true;
      syncArchiveButton(); // 同时隐藏工艺单下载按钮（已归档详情并未成功加载）
      return;
    }
    renderDraftDetail(data, banner);
    if (card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    if (seq !== detailSeq) return; // 网络异常同样不得干扰更新的选择
    getInFlight = false;
    viewedDraft = null;
    titleEl.textContent = `炉次 #${id}`;
    draftDetailError.textContent =
      "读取炉次详情失败（网络异常），当前内容不可用于归档，请重试。";
    draftDetailError.hidden = false;
    draftDetailOk.hidden = true;
    syncArchiveButton();
  }
}

function renderDraftDetail(d, banner) {
  viewedDraft = d;
  document.getElementById("draft-detail-title").textContent =
    `炉次 #${d.id} ${d.name || "（未命名）"}`;
  if (banner && banner.kind === "error") {
    draftDetailError.textContent = banner.text;
    draftDetailError.hidden = false;
    draftDetailOk.hidden = true;
  } else if (banner && banner.kind === "ok") {
    draftDetailOk.textContent = banner.text;
    draftDetailOk.hidden = false;
    draftDetailError.hidden = true;
  } else {
    draftDetailError.hidden = true;
    draftDetailOk.hidden = true;
  }

  syncArchiveButton();

  draftMeta.innerHTML = "";
  draftMeta.appendChild(kv("状态", d.status === "archived" ? "已归档（冻结）" : "活动"));
  draftMeta.appendChild(kv("草稿版本", `v${d.version}`));
  draftMeta.appendChild(kv("按顺序引用配方", d.recipeIds
    .map((id) => {
      const rv = d.recipeVersions[String(id)];
      return `#${id}${rv ? `（引用时 v${rv}）` : ""}`;
    })
    .join(" → ")));
  draftMeta.appendChild(kv("求解", d.solvable ? `有解，${d.result.stepCount} 档 / ${d.result.totalMinutes} 分钟` : "无解"));
  draftMeta.appendChild(kv("创建 / 更新", `${fmtTime(d.createdAt)} ｜ ${fmtTime(d.updatedAt)}`));
  if (d.archivedAt) draftMeta.appendChild(kv("归档时间", fmtTime(d.archivedAt)));

  const matBox = el("div", { class: "kv" });
  matBox.appendChild(el("span", {
    textContent: d.status === "archived"
      ? "冻结当时的材料参数："
      : "当前求解所用材料参数（与下方结果同版）：",
  }));
  d.materials.forEach((m, i) => {
    matBox.appendChild(el("code", { class: "mat-chip", textContent:
      `${i + 1}. ${m.name}：区间 ${m.holdLow}–${m.holdHigh}°C，${m.duration} 分钟，耐受 ${m.tolerance}°C` }));
  });
  draftMeta.appendChild(matBox);

  if (d.solvable) {
    draftResultView.render(d.result,
      d.status === "archived" ? `归档方案（冻结 v${d.version}）` : `求解结果（v${d.version}）`);
  } else {
    draftResultView.render({ solvable: false, reason: d.reason });
  }

  // 只有已归档（必有冻结曲线）才开放电费试算与原曲线复用检查；
  // 二者均只读归档与当前配方，不影响方案
  if (d.status === "archived") {
    setupTrial(d);
    setupReuse(d);
  } else {
    hideTrial();
    hideReuse();
  }

  loadDrafts();
}

document.getElementById("draft-refresh-btn").addEventListener("click", () => {
  // 等待（详情加载 / 归档提交）期间仍允许切换与刷新；刷新绑定当前选择。
  // 已在加载中再点刷新会以更新的代际重发，旧 GET 自动作废；归档仍在途
  // 时刷新也不解除“同一炉次不得重复归档”，两者响应按代际各自归位。
  const currentId = getInFlight ? detailTargetId : (viewedDraft && viewedDraft.id);
  if (currentId == null) return;
  openDraft(currentId);
});

draftArchiveBtn.addEventListener("click", async () => {
  // 归档资格只认最近一次成功读回的详情；加载中按钮本就禁用
  if (!viewedDraft || viewedDraft.status === "archived") return;
  // 绑定“点击这一刻”的炉次 ID 与版本：之后切到别的炉次也不改写其目标
  const targetId = viewedDraft.id;
  const targetVersion = viewedDraft.version;
  // 同一炉次归档未结束，不得重复提交（不同炉次各自独立）
  if (archivingIds.has(targetId)) return;
  // 同时绑定点击这一刻的详情代际：此后无论是切走再回到甲、还是直接刷新
  // 同一炉次，只要又发起过查看/刷新（detailSeq +1），本次旧回包即作废——
  // 不得清空新详情、插入旧提示，也不得再代为触发一次详情 GET。
  // 不变式：未发生新的查看/刷新时，当前选择必为 targetId，无需再比对 ID。
  const clickSeq = detailSeq;

  draftDetailError.hidden = true;
  draftDetailOk.hidden = true;
  archivingIds.add(targetId);
  syncArchiveButton();
  let r;
  try {
    r = await api(`/api/drafts/${targetId}/archive`, {
      method: "POST",
      body: JSON.stringify({ version: targetVersion }),
    });
  } catch (e) {
    archivingIds.delete(targetId);
    // 网络异常同样先看代际：切走再返回（或已刷新）后到达的异常不得干扰
    // 当前详情；仅后台释放在途状态并恢复按钮资格。
    if (clickSeq !== detailSeq) {
      syncArchiveButton();
      return;
    }
    // 网络失败：不动详情、不触发读取，仅提示可重试（同一炉次并未归档成功）。
    // 若同代际内详情 GET 仍在加载，提示交给 GET 完成后呈现，这里只恢复按钮。
    if (!getInFlight) {
      draftDetailError.textContent =
        "归档请求失败（网络异常），未改变任何状态，可重试归档。";
      draftDetailError.hidden = false;
    }
    syncArchiveButton();
    return;
  }
  archivingIds.delete(targetId);

  // 点击之后又发生过查看或刷新（含“切到乙再回到甲”——ID 相同但代际已新）：
  // 旧归档回包（成功 / 409 / 422）一律作废，不改写当前详情、曲线、提示，
  // 也不额外触发详情读取；仅在成功时后台静默同步列表，并按当前有效详情
  // 恢复按钮。
  if (clickSeq !== detailSeq) {
    if (r.ok) loadDrafts();
    syncArchiveButton();
    return;
  }

  if (r.status === 409) {
    // 版本过期/已归档：读回同一炉次，并保留可见原因（409 附当前版本号）
    const cur = r.data.detail && r.data.detail.currentVersion;
    const reason =
      detailOf(r.data) + (cur ? ` 已为你重新载入当前版本 v${cur}。` : "");
    await openDraft(targetId, { kind: "error", text: reason });
    return;
  }
  if (!r.ok) {
    // 422：当前无解等，状态未改变；读回同一炉次，原因保留可见
    await openDraft(targetId, {
      kind: "error",
      text: "归档失败（未改变任何状态）：" + detailOf(r.data),
    });
    return;
  }
  // 成功：读回并显示冻结方案与成功提示，按钮保持“已归档”禁用
  await openDraft(targetId, {
    kind: "ok",
    text: `归档成功：已冻结炉次 #${r.data.id} 的版本 v${r.data.version} 材料参数与完整求解结果；此后配方修改不再影响本炉次。`,
  });
});

// ============================================================
// 已归档炉次工艺单下载（只读归档快照，不写库；
// 绑定点击时炉次，切换 / 刷新后旧响应与异常一律不下载、不写提示）
// ============================================================

// 从 Content-Disposition 解析文件名：优先 RFC 5987 的 filename*=UTF-8''，
// 其次 ASCII filename；都没有时用兜底名。
function parseFilename(header, fallback) {
  if (header) {
    const star = /filename\*\s*=\s*([^']+)'([^']*)'([^;]+)/i.exec(header);
    if (star) {
      try { return decodeURIComponent(star[3].trim()); } catch (e) { /* 继续 */ }
    }
    const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
    if (plain && plain[1]) return plain[1].trim();
  }
  return fallback;
}

async function triggerBrowserDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    // 浏览器已接管下载后即可释放
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

draftSheetBtn.addEventListener("click", async () => {
  // 仅当前已归档详情成功加载后可点（按钮在其他状态下隐藏）
  if (!viewedDraft || viewedDraft.status !== "archived" || getInFlight) return;
  // 绑定点击那一刻的炉次 ID 与详情代际
  const targetId = viewedDraft.id;
  const clickSeq = detailSeq;
  // 同一炉次下载请求未结束前不得重复提交（不同炉次互不影响）
  if (downloadingIds.has(targetId)) return;

  clearSheetMessages();
  downloadingIds.add(targetId);
  syncSheetButton();

  let resp;
  try {
    // 工艺单是 HTML 附件，不能带 api() 的 JSON 请求头；直接用 fetch 取 blob
    resp = await fetch(`/api/drafts/${targetId}/sheet`);
  } catch (e) {
    downloadingIds.delete(targetId);
    // 切走 / 刷新后到达的网络异常：不下载、不写提示，仅恢复按钮资格
    if (clickSeq !== detailSeq) { syncSheetButton(); return; }
    draftSheetError.textContent = "工艺单下载失败（网络异常），未改变任何数据，可重试。";
    draftSheetError.hidden = false;
    syncSheetButton();
    return;
  }

  if (!resp.ok) {
    // 404/422/500 等 HTTP 错误：读回中文原因展示，不触发任何下载
    let reason = "工艺单下载失败，请重试。";
    try {
      const data = await resp.json();
      reason = detailOf(data);
    } catch (e) { /* 响应体不是 JSON：用兜底原因 */ }
    downloadingIds.delete(targetId);
    if (clickSeq !== detailSeq) { syncSheetButton(); return; }
    draftSheetError.textContent = `工艺单下载失败：${reason}`;
    draftSheetError.hidden = false;
    syncSheetButton();
    return;
  }

  let blob;
  try {
    blob = await resp.blob();
  } catch (e) {
    downloadingIds.delete(targetId);
    if (clickSeq !== detailSeq) { syncSheetButton(); return; }
    draftSheetError.textContent = "工艺单内容读取失败（网络异常），可重试。";
    draftSheetError.hidden = false;
    syncSheetButton();
    return;
  }
  downloadingIds.delete(targetId);

  // 切换或刷新后才完整到达的旧响应：既不触发下载，也不写任何提示；
  // 但要按当前有效详情恢复按钮（旧在途去重状态至此释放）。
  if (clickSeq !== detailSeq) { syncSheetButton(); return; }

  // 服务端返回的是 UTF-8 HTML 附件；文件名含炉次 ID 与冻结版本
  const filename = parseFilename(
    resp.headers.get("Content-Disposition"),
    `draft-${targetId}-sheet.html`
  );
  try {
    await triggerBrowserDownload(
      new Blob([blob], { type: "text/html;charset=utf-8" }),
      filename
    );
  } catch (e) {
    draftSheetError.textContent = "工艺单已生成，但浏览器保存失败，可重试。";
    draftSheetError.hidden = false;
    syncSheetButton();
    return;
  }
  draftSheetOk.textContent =
    `工艺单已下载：${filename}。文件内容取自归档冻结快照，可离线打开，后续配方修改、停用及重启均不改变。`;
  draftSheetOk.hidden = false;
  syncSheetButton();
});

// ============================================================
// 已归档炉次电费试算（只读冻结曲线；切换炉次 / 刷新 / 编辑输入立即作废旧试算）
// ============================================================
const elecCard = document.getElementById("draft-electricity-card");
const elecForm = document.getElementById("electricity-form");
const elecStart = document.getElementById("elec-start");
const elecPricesBox = document.getElementById("elec-prices");
const elecPowersBox = document.getElementById("elec-powers");
const elecError = document.getElementById("elec-error");
const elecResult = document.getElementById("elec-result");
const elecSubmitBtn = document.getElementById("elec-submit-btn");

// trialSeq：试算代际。切换炉次、刷新详情、编辑任一输入、正式发起试算都会 +1；
// 只有“当前最新一代”自身的成功响应 / HTTP 错误 / 网络异常允许回填。旧代际的
// 回包（哪怕是切走后又回到原炉次才到达）一律丢弃：不回填结果、不写提示、
// 不动按钮，也不受旧请求是否已经结束影响。
let trialSeq = 0;
// 当前代际是否正有一笔试算在途：为 true 时按钮禁用并阻止重复提交；
// 编辑输入 / 切换炉次 / 刷新生效新一代后即为 false，旧在途请求不再影响它。
let trialLoading = false;
let elecDraft = null; // 当前试算所绑定的已归档炉次

// 24 小时电价输入（00～23 时）只需建一次；数值作为试算输入保留
for (let h = 0; h < 24; h++) {
  const cell = el("label", { class: "elec-price-cell" },
    el("span", { textContent: `${String(h).padStart(2, "0")} 时` }),
    el("input", {
      type: "number", min: "0", step: "any", defaultValue: "0.5",
      attrs: { "data-elec": "price", "data-hour": String(h), inputmode: "decimal" },
    }));
  cell.querySelector("input").value = "0.5";
  elecPricesBox.appendChild(cell);
}

function clearTrialOutput() {
  // 只清结果区（用于编辑输入时立即移除旧试算数字）；错误提示交给提交逻辑管理
  elecResult.innerHTML = "";
}

function resetTrialButton() {
  // 按钮只表达“当前这一代”的状态：只要当前没有在途试算，立即可再次提交，
  // 无需等待任何旧请求结束（旧请求的回包按代际作废，不会再动按钮）。
  elecSubmitBtn.disabled = false;
  elecSubmitBtn.textContent = "试算电费";
}

function invalidateTrial() {
  // 旧试算立即作废：代际 +1、清空结果与错误提示，并恢复提交能力
  // （输入框本身保留供继续编辑；卡片隐藏时按钮同样复位，不影响其他界面）
  trialSeq += 1;
  trialLoading = false;
  clearTrialOutput();
  elecError.hidden = true;
  resetTrialButton();
}

function rebuildPowerInputs(temperatures) {
  elecPowersBox.innerHTML = "";
  temperatures.forEach((t, i) => {
    const cell = el("label", { class: "elec-power-cell" },
      el("span", { textContent: `第 ${i + 1} 档 · ${t}°C` }),
      el("input", {
        type: "number", min: "0", step: "any",
        attrs: { "data-elec": "power", "data-step": String(i + 1), inputmode: "decimal",
                 placeholder: "0" },
      }));
    elecPowersBox.appendChild(cell);
  });
}

function setupTrial(d) {
  // 仅已归档炉次可试算；曲线来自归档冻结结果，与配方当前状态无关
  invalidateTrial();
  elecDraft = d;
  rebuildPowerInputs(d.result.temperatures);
  elecCard.hidden = false;
}

function hideTrial() {
  invalidateTrial();
  elecDraft = null;
  elecCard.hidden = true;
}

// 编辑任一试算输入（开炉分钟 / 电价 / 功率）：立即作废旧试算——清除旧结果
// 与错误提示、恢复按钮，无需等待此前已发请求结束；其迟到响应（含异常）
// 因代际已新而作废。
elecForm.addEventListener("input", () => {
  invalidateTrial();
});

function fmtNum(x) {
  // 页面展示：去掉浮点噪声，最多 4 位小数（费用列固定 2 位）
  return String(Math.round(Number(x) * 10000) / 10000);
}
function fmtMoney(x) {
  return Number(x).toFixed(2);
}

function readElecPrices() {
  const out = new Array(24);
  const inputs = elecPricesBox.querySelectorAll('input[data-elec="price"]');
  for (const inp of inputs) {
    const h = Number(inp.dataset.hour);
    const raw = inp.value.trim();
    if (raw === "") { out[h] = 0; continue; } // 留空按 0 元/kWh
    const v = Number(raw);
    if (!Number.isFinite(v) || v < 0) {
      return { error: `${String(h).padStart(2, "0")} 时电价必须是非负有限数值。` };
    }
    out[h] = v;
  }
  return { values: out };
}

function readElecPowers(n) {
  const out = [];
  const inputs = elecPowersBox.querySelectorAll('input[data-elec="power"]');
  for (const inp of inputs) {
    const raw = inp.value.trim();
    if (raw === "") { out.push(0); continue; } // 留空按 0 kW
    const v = Number(raw);
    if (!Number.isFinite(v) || v < 0) {
      return { error: `第 ${inp.dataset.step} 档功率必须是非负有限数值。` };
    }
    out.push(v);
  }
  if (out.length !== n) {
    return { error: `功率档位数量（${out.length}）与冻结曲线档数（${n}）不一致。` };
  }
  return { values: out };
}

function renderElectricity(data) {
  elecResult.innerHTML = "";
  const totals = el("div", { id: "elec-totals" });
  totals.appendChild(el("div", { class: "kv" },
    document.createTextNode("总电量："),
    el("b", { textContent: `${fmtNum(data.totalKwh)} kWh` })));
  totals.appendChild(el("div", { class: "kv" },
    document.createTextNode("总费用（未舍入逐档汇总后四舍五入）："),
    el("b", { textContent: `${fmtMoney(data.totalCost)} 元` })));
  totals.appendChild(el("div", { class: "kv" },
    document.createTextNode("总时长："),
    el("b", { textContent: `${data.totalMinutes} 分钟（${data.stepCount} 档）` })));
  elecResult.appendChild(totals);

  const card = el("section", { class: "card" },
    el("h2", { textContent: "逐档电费明细" }));
  const tableWrap = el("div", { class: "table-wrap" });
  const table = el("table");
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", { textContent: "档位" }),
    el("th", { textContent: "设定温度 (°C)" }),
    el("th", { textContent: "时长 (分钟)" }),
    el("th", { textContent: "电量 (kWh)" }),
    el("th", { textContent: "费用 (元)" }))));
  const tbody = el("tbody");
  let roundedSum = 0;
  data.steps.forEach((s) => {
    roundedSum += s.cost;
    const tr = el("tr");
    tr.appendChild(el("td", { textContent: String(s.index) }));
    tr.appendChild(el("td", { textContent: String(s.temperature) }));
    tr.appendChild(el("td", { textContent: String(s.minutes) }));
    tr.appendChild(el("td", { textContent: fmtNum(s.kwh) }));
    tr.appendChild(el("td", { textContent: fmtMoney(s.cost) }));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  card.appendChild(tableWrap);
  elecResult.appendChild(card);

  // 逐档费用先各自舍入，其和与总费用可能相差 1 分
  if (Math.abs(roundedSum - data.totalCost) > 1e-9) {
    elecResult.appendChild(el("p", { class: "elec-round-note", textContent:
      `注：逐档费用已各自四舍五入到分（合计 ${fmtMoney(roundedSum)} 元），` +
      `总费用按未舍入逐档金额汇总后再四舍五入，因此为 ${fmtMoney(data.totalCost)} 元。` }));
  }
}

elecForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  // 只有“当前已归档详情成功加载后”才可提交：加载中 / 读取失败 / 未归档时
  // elecDraft 均为 null（卡片本身也隐藏）
  if (!elecDraft || elecDraft.status !== "archived") return;
  // 防止重复提交：当前这一代自身响应未回来前，再次提交一律忽略（按钮也已禁用）
  if (trialLoading) return;
  clearTrialOutput();
  elecError.hidden = true;

  // 先做客户端校验：此时不占用代际 / 在途状态，旧请求的回包处理不受影响
  const startRaw = elecStart.value.trim();
  if (!/^\d+$/.test(startRaw)) {
    elecError.textContent = "开炉分钟必须是 0～1439 之间的整数。";
    elecError.hidden = false;
    return;
  }
  const startMinute = Number(startRaw);
  if (startMinute < 0 || startMinute > 1439) {
    elecError.textContent = "开炉分钟必须在 0～1439 之间。";
    elecError.hidden = false;
    return;
  }
  const n = elecDraft.result.temperatures.length;
  const { values: prices, error: pErr } = readElecPrices();
  if (pErr) { elecError.textContent = pErr; elecError.hidden = false; return; }
  const { values: powers, error: wErr } = readElecPowers(n);
  if (wErr) { elecError.textContent = wErr; elecError.hidden = false; return; }

  // 正式发起请求才登记新一代：旧结果即刻作废，旧回包到达时按代际丢弃；
  // 按钮只在“这一代自身”的响应回来前禁用。编辑输入 / 切换炉次后，新的提交
  // 无需等待旧请求结束。
  const seq = ++trialSeq;
  trialLoading = true;
  elecSubmitBtn.disabled = true;
  elecSubmitBtn.textContent = "试算中…";
  let r;
  try {
    r = await api(`/api/drafts/${elecDraft.id}/electricity`, {
      method: "POST",
      body: JSON.stringify({ startMinute, prices, powers }),
    });
  } catch (e) {
    // 网络异常只认当前代际：旧请求（已被编辑 / 切换 / 新提交作废，哪怕切走
    // 后又回到原炉次）的异常不得回填提示、不得改变当前按钮，也不受它是否
    // 已经结束影响。
    if (seq !== trialSeq) return;
    trialLoading = false;
    elecError.textContent = "试算请求失败（网络异常），未改变任何数据，可重试。";
    elecError.hidden = false;
    resetTrialButton();
    return;
  }
  // 迟到响应（切换炉次 / 刷新 / 编辑输入 / 更新的试算之后到达，含切走后又
  // 回到同一炉次的情形）一律作废：不回填结果或提示，也绝不恢复按钮——
  // 按钮只由当前最新一代自身的完成来恢复。
  if (seq !== trialSeq) return;
  trialLoading = false;
  resetTrialButton();
  if (!r.ok) {
    // HTTP 错误（404/422/500 等）：展示后端中文原因，可修改后再次试算
    elecError.textContent = detailOf(r.data);
    elecError.hidden = false;
    return;
  }
  renderElectricity(r.data);
});

// ============================================================
// 已归档炉次原曲线复用检查（只读当前配方，不重新求解；
// 切换炉次 / 刷新详情立即作废旧结论，旧响应与异常一律不回填）
// ============================================================
const reuseCard = document.getElementById("draft-reuse-card");
const reuseError = document.getElementById("reuse-error");
const reuseResult = document.getElementById("reuse-result");
const reuseSubmitBtn = document.getElementById("reuse-submit-btn");

// reuseSeq：检查代际。切换炉次、刷新详情、正式发起新检查都会 +1；
// 只有“当前最新一代”自身的成功响应 / HTTP 错误 / 网络异常允许回填。
// 旧代际的回包（哪怕是切走后又回到原炉次才到达）一律丢弃：不回填结论、
// 不写提示、不动按钮。
let reuseSeq = 0;
// 当前代际是否正有一笔检查在途：为 true 时按钮禁用并阻止重复提交
let reuseLoading = false;
let reuseDraft = null; // 当前检查所绑定的已归档炉次

function resetReuseButton() {
  // 按钮只表达“当前这一代”的状态：当前没有在途检查即可再次提交
  reuseSubmitBtn.disabled = false;
  reuseSubmitBtn.textContent = "检查原曲线能否复用";
}

function invalidateReuse() {
  // 旧结论立即作废：代际 +1、清空结论与错误提示，并恢复提交能力
  reuseSeq += 1;
  reuseLoading = false;
  reuseResult.innerHTML = "";
  reuseError.hidden = true;
  resetReuseButton();
}

function setupReuse(d) {
  // 仅已归档炉次可检查；曲线来自归档冻结结果，配方参数按当前读取
  invalidateReuse();
  reuseDraft = d;
  reuseCard.hidden = false;
}

function hideReuse() {
  invalidateReuse();
  reuseDraft = null;
  reuseCard.hidden = true;
}

function renderReuse(data) {
  reuseResult.innerHTML = "";

  const verdict = el("p", {
    class: "reuse-verdict " + (data.reusable ? "reuse-verdict-ok" : "reuse-verdict-no"),
  });
  verdict.appendChild(el("span", {
    class: "badge " + (data.reusable ? "badge-ok" : "badge-fail"),
    textContent: data.reusable ? "可复用" : "不可复用",
  }));
  verdict.appendChild(document.createTextNode(
    data.reusable
      ? "全部材料保温达标、无任何超限且配方均可用：原曲线可用于当前配方。"
      : "存在保温未达标、超过耐受上限或配方已停用的项目：原曲线不可直接复用。"
  ));
  reuseResult.appendChild(verdict);

  const card = el("section", { class: "card" },
    el("h2", { textContent: `逐项结论（按原引用顺序，曲线 ${data.stepCount} 档）` }));
  const tableWrap = el("div", { class: "table-wrap" });
  const table = el("table");
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", { textContent: "配方" }),
    el("th", { textContent: "当前版本" }),
    el("th", { textContent: "状态" }),
    el("th", { textContent: "保温达标" }),
    el("th", { textContent: "首次达标区间" }),
    el("th", { textContent: "首次超限档号" }))));
  const tbody = el("tbody");
  data.items.forEach((it) => {
    const tr = el("tr");
    tr.appendChild(el("td", { textContent: `#${it.recipeId} ${it.name}` }));
    tr.appendChild(el("td", {},
      el("span", { class: "ver-badge", textContent: `v${it.version}` })));
    tr.appendChild(el("td", {}, el("span", {
      class: "status-badge " + (it.status === "disabled" ? "status-disabled" : "status-active"),
      textContent: it.status === "disabled" ? "已停用" : "可用",
    })));
    tr.appendChild(el("td", {
      class: it.holdOk ? "" : "text-fail",
      textContent: it.holdOk ? "达标" : "未达标",
    }));
    tr.appendChild(el("td", {
      textContent: it.window
        ? `第 ${it.window.startStep}～${it.window.endStep} 档`
        : "—",
    }));
    tr.appendChild(el("td", {
      class: it.toleranceBreachStep != null ? "text-fail" : "",
      textContent: it.toleranceBreachStep != null
        ? `第 ${it.toleranceBreachStep} 档`
        : "无超限",
    }));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  card.appendChild(tableWrap);
  reuseResult.appendChild(card);
}

reuseSubmitBtn.addEventListener("click", async () => {
  // 只有“当前已归档详情成功加载后”才可提交：加载中 / 读取失败 / 未归档时
  // reuseDraft 均为 null（卡片本身也隐藏）
  if (!reuseDraft || reuseDraft.status !== "archived") return;
  // 防止重复提交：当前这一代自身响应未回来前，再次提交一律忽略（按钮也已禁用）
  if (reuseLoading) return;
  reuseResult.innerHTML = "";
  reuseError.hidden = true;

  // 正式发起请求才登记新一代：旧结论即刻作废，旧回包到达时按代际丢弃；
  // 按钮只在“这一代自身”的响应回来前禁用。
  const seq = ++reuseSeq;
  reuseLoading = true;
  reuseSubmitBtn.disabled = true;
  reuseSubmitBtn.textContent = "检查中…";
  let r;
  try {
    r = await api(`/api/drafts/${reuseDraft.id}/reuse-check`, { method: "POST" });
  } catch (e) {
    // 网络异常只认当前代际：旧请求（已被切换 / 刷新 / 新提交作废）的异常
    // 不得回填提示、不得改变当前按钮
    if (seq !== reuseSeq) return;
    reuseLoading = false;
    reuseError.textContent = "检查请求失败（网络异常），未改变任何数据，可重试。";
    reuseError.hidden = false;
    resetReuseButton();
    return;
  }
  // 迟到响应（切换炉次 / 刷新详情 / 更新的检查之后到达，含切走后又回到
  // 同一炉次的情形）一律作废：不回填结论或提示，也绝不动当前按钮
  if (seq !== reuseSeq) return;
  reuseLoading = false;
  resetReuseButton();
  if (!r.ok) {
    // HTTP 错误（404/422/500 等）：展示后端中文原因
    reuseError.textContent = detailOf(r.data);
    reuseError.hidden = false;
    return;
  }
  renderReuse(r.data);
});

// ---------- 启动 ----------
buildRows();
