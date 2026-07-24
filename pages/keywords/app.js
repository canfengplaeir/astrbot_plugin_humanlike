const bridge = window.AstrBotPluginPage;

const manualInput = document.getElementById("manualInput");
const addBtn = document.getElementById("addBtn");
const manualTags = document.getElementById("manualTags");
const aiTags = document.getElementById("aiTags");
const genBtn = document.getElementById("genBtn");
const genStatus = document.getElementById("genStatus");
const clearManual = document.getElementById("clearManual");
const clearAi = document.getElementById("clearAi");
const manualCount = document.getElementById("manualCount");
const aiCount = document.getElementById("aiCount");
const personaSelect = document.getElementById("personaSelect");

await bridge.ready();

async function loadKeywords() {
  const data = await bridge.apiGet("keywords/list");
  renderManual(data.manual || []);
  renderAi(data.ai || []);
}

function renderManual(keywords) {
  manualCount.textContent = keywords.length ? `共 ${keywords.length} 个` : "";
  clearManual.style.display = keywords.length ? "" : "none";
  if (!keywords.length) { manualTags.innerHTML = '<span class="empty-msg">暂无手动关键词</span>'; return; }
  manualTags.innerHTML = keywords.map(kw =>
    `<span class="tag manual">${esc(kw)}<button class="tag-del" data-kw="${escAttr(kw)}" data-type="manual">×</button></span>`
  ).join("");
  bindDel();
}

function renderAi(keywords) {
  aiCount.textContent = keywords.length ? `共 ${keywords.length} 个` : "";
  clearAi.style.display = keywords.length ? "" : "none";
  if (!keywords.length) { aiTags.innerHTML = '<span class="empty-msg">暂无，选择人格后点击生成</span>'; return; }
  aiTags.innerHTML = keywords.map(kw =>
    `<span class="tag ai">${esc(kw)}<button class="tag-del" data-kw="${escAttr(kw)}" data-type="ai">×</button></span>`
  ).join("");
  bindDel();
}

function bindDel() {
  document.querySelectorAll(".tag-del").forEach(btn => {
    btn.onclick = async () => {
      await bridge.apiPost("keywords/remove", { keyword: btn.dataset.kw });
      loadKeywords();
    };
  });
}

async function addKeyword() {
  const kw = manualInput.value.trim();
  if (!kw) return;
  await bridge.apiPost("keywords/add", { keyword: kw });
  manualInput.value = "";
  manualInput.focus();
  loadKeywords();
}

addBtn.onclick = addKeyword;
manualInput.onkeydown = e => { if (e.key === "Enter") addKeyword(); };

genBtn.onclick = async () => {
  genBtn.disabled = true;
  genBtn.innerHTML = '<span class="spinner"></span>';
  genStatus.textContent = "";
  genStatus.className = "status-line";
  try {
    const result = await bridge.apiPost("keywords/generate", { persona_id: personaSelect.value });
    if (result.ok && result.keywords) {
      genStatus.textContent = `已生成 ${result.keywords.length} 个关键词`;
      genStatus.className = "status-line success";
    } else {
      genStatus.textContent = "生成失败";
      genStatus.className = "status-line error";
    }
  } catch (e) {
    genStatus.textContent = e.message || "请求失败";
    genStatus.className = "status-line error";
  }
  genBtn.disabled = false;
  genBtn.innerHTML = "生成";
  loadKeywords();
};

clearManual.onclick = async () => { await bridge.apiPost("keywords/clear", { target: "manual" }); loadKeywords(); };
clearAi.onclick = async () => { await bridge.apiPost("keywords/clear", { target: "ai" }); loadKeywords(); };

async function loadPersonas() {
  try {
    const personas = await bridge.apiGet("keywords/personas");
    personaSelect.innerHTML = personas.map(p => `<option value="${escAttr(p.id)}">${esc(p.id)}</option>`).join("");
    if (!personas.length) personaSelect.innerHTML = '<option value="">（默认人格）</option>';
  } catch { personaSelect.innerHTML = '<option value="">（默认）</option>'; }
}

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
function escAttr(s) { return s.replace(/"/g, "&quot;"); }

loadPersonas();
loadKeywords();
