const bridge = window.AstrBotPluginPage;

const manualInput = document.getElementById("manualInput");
const addBtn = document.getElementById("addBtn");
const manualTags = document.getElementById("manualTags");
const aiTags = document.getElementById("aiTags");
const genBtn = document.getElementById("genBtn");
const genStatus = document.getElementById("genStatus");
const clearManual = document.getElementById("clearManual");
const clearAi = document.getElementById("clearAi");

await bridge.ready();

async function loadKeywords() {
  const data = await bridge.apiGet("keywords/list");
  renderManual(data.manual || []);
  renderAi(data.ai || []);
}

function renderManual(keywords) {
  manualTags.innerHTML = keywords
    .map(
      (kw) =>
        `<span class="tag manual">${escapeHtml(kw)}<button class="tag-del" data-kw="${escapeAttr(kw)}" data-type="manual">×</button></span>`
    )
    .join("");
  if (!keywords.length) manualTags.innerHTML = '<span class="empty">暂无</span>';
  bindTagEvents();
}

function renderAi(keywords) {
  aiTags.innerHTML = keywords
    .map(
      (kw) =>
        `<span class="tag ai">${escapeHtml(kw)}<button class="tag-del" data-kw="${escapeAttr(kw)}" data-type="ai">×</button></span>`
    )
    .join("");
  if (!keywords.length) aiTags.innerHTML = '<span class="empty">暂无，点击下方按钮生成</span>';
  bindTagEvents();
}

function bindTagEvents() {
  document.querySelectorAll(".tag-del").forEach((btn) => {
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
  loadKeywords();
}

addBtn.onclick = addKeyword;
manualInput.onkeydown = (e) => {
  if (e.key === "Enter") addKeyword();
};

genBtn.onclick = async () => {
  genBtn.disabled = true;
  genStatus.textContent = "⏳ 正在生成...";
  genStatus.className = "status";
  try {
    const result = await bridge.apiPost("keywords/generate", {});
    if (result.ok) {
      genStatus.textContent = `✅ 已生成 ${result.keywords.length} 个关键词`;
      genStatus.className = "status success";
    } else {
      genStatus.textContent = "❌ 生成失败";
      genStatus.className = "status error";
    }
  } catch {
    genStatus.textContent = "❌ 生成失败";
    genStatus.className = "status error";
  }
  genBtn.disabled = false;
  loadKeywords();
};

clearManual.onclick = async () => {
  await bridge.apiPost("keywords/clear", { target: "manual" });
  loadKeywords();
};

clearAi.onclick = async () => {
  await bridge.apiPost("keywords/clear", { target: "ai" });
  loadKeywords();
};

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
function escapeAttr(s) {
  return s.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

loadKeywords();
