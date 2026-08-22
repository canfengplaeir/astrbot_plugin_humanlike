const bridge = window.AstrBotPluginPage;
const form = document.getElementById("settingsForm");
const saveBtn = document.getElementById("saveBtn");
const saveStatus = document.getElementById("saveStatus");

await bridge.ready();

const RANGES = [
  "flow_engine.flow_reply_threshold", "flow_engine.flow_decay_rate",
  "flow_engine.flow_boost_mention", "flow_engine.flow_boost_keyword",
  "flow_engine.flow_boost_question", "flow_engine.flow_boost_activity",
  "flow_engine.flow_decay_on_reply", "flow_engine.random_silence_probability",
  "debounce.min_reply_cooldown", "debounce.max_replies_per_window",
  "accumulation.silence_threshold", "accumulation.immediate_flow_threshold",
];

function getVal(path) {
  const el = form.querySelector(`[name="${path}"]`);
  if (!el) return null;
  if (el.type === "checkbox") return el.checked;
  if (el.type === "range") return parseFloat(el.value);
  return el.value;
}

function setVal(path, val) {
  const el = form.querySelector(`[name="${path}"]`);
  if (!el) return;
  if (el.type === "checkbox") {
    el.checked = !!val;
  } else if (el.type === "range") {
    el.value = (val != null) ? val : (el.min || 0);
    updateOutput(el);
  } else {
    el.value = (val != null) ? val : "";
  }
}

function updateOutput(range) {
  const output = range.closest(".range-field")?.querySelector("output");
  if (output) output.textContent = range.value;
}

RANGES.forEach(name => {
  const el = form.querySelector(`[name="${name}"]`);
  if (el) {
    el.addEventListener("input", () => updateOutput(el));
  }
});

function flatten(cfg) {
  const flat = {};
  for (const [section, items] of Object.entries(cfg)) {
    if (typeof items === "object" && items !== null && !Array.isArray(items)) {
      for (const [k, v] of Object.entries(items)) {
        flat[`${section}.${k}`] = v;
      }
    }
  }
  return flat;
}

function buildBody() {
  const sections = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    const parts = el.name.split(".");
    if (parts.length === 2) {
      const [sec, key] = parts;
      sections[sec] = sections[sec] || {};
      sections[sec][key] = el.type === "checkbox" ? el.checked : (el.type === "range" || el.type === "number" ? parseFloat(el.value) || 0 : el.value);
    } else {
      sections[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
  }
  return sections;
}

async function load() {
  try {
    const data = await bridge.apiGet("settings");
    const flat = flatten(data);
    for (const [k, v] of Object.entries(flat)) {
      setVal(k, v);
    }
    for (const k of ["ai_judge_prompt", "ai_reply_prompt"]) {
      if (data[k] !== undefined) setVal(k, data[k]);
    }
  } catch (e) {
    saveStatus.textContent = "加载失败";
    saveStatus.className = "status-line error";
  }
}

form.onsubmit = async (e) => {
  e.preventDefault();
  saveBtn.disabled = true;
  saveBtn.innerHTML = '<span class="spinner"></span>';
  saveStatus.textContent = "";
  saveStatus.className = "status-line";
  try {
    const body = buildBody();
    const result = await bridge.apiPost("settings/save", body);
    if (result.ok) {
      saveStatus.textContent = "已保存";
      saveStatus.className = "status-line success";
    } else {
      saveStatus.textContent = "保存失败";
      saveStatus.className = "status-line error";
    }
  } catch (e) {
    saveStatus.textContent = e.message || "请求失败";
    saveStatus.className = "status-line error";
  }
  saveBtn.disabled = false;
  saveBtn.innerHTML = "保存设置";
};

load();
