const bridge = window.AstrBotPluginPage;

const tableBody = document.getElementById("groupTable");
const emptyMsg = document.getElementById("emptyMsg");
const metaBar = document.getElementById("metaBar");
const refreshBtn = document.getElementById("refreshBtn");
const autoLabel = document.getElementById("autoLabel");

await bridge.ready();

let timer = 10;
let interval;

function flowClass(val) {
  if (val >= 70) return "high";
  if (val >= 40) return "mid";
  return "low";
}

function agoStr(s) {
  if (s < 0) return "未发言";
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h";
}

async function load() {
  try {
    const data = await bridge.apiGet("status/groups");
    metaBar.innerHTML = `<span><span class="meta-dot"></span> 接管模式${data.override ? "开启" : "关闭"}</span> <span><span class="meta-dot${data.accum_enabled ? "" : " off"}"></span> 累积模式${data.accum_enabled ? "开启" : "关闭"}</span>`;

    if (!data.groups || !data.groups.length) {
      tableBody.innerHTML = "";
      emptyMsg.style.display = "block";
      emptyMsg.textContent = "尚无群聊数据 — 有群发送消息后这里会出现状态";
      return;
    }
    emptyMsg.style.display = "none";
    tableBody.innerHTML = data.groups
      .map((g) => {
        const cls = flowClass(g.flow);
        return `<tr>
          <td>${esc(g.group_id)}</td>
          <td><div class="flow-row"><div class="flow-bar-wrap"><div class="flow-bar-fill ${cls}" style="width:${Math.min(100, g.flow)}%"></div></div><span class="flow-val">${g.flow.toFixed(0)}</span></div></td>
          <td>${agoStr(g.last_reply_ago)}</td>
          <td>${g.recent_replies}</td>
          <td style="color:${g.pending > 0 ? 'var(--terracotta)' : ''}">${g.pending > 0 ? "\u25cf " + g.pending : "\u2014"}</td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    emptyMsg.style.display = "block";
    emptyMsg.textContent = "加载失败";
  }
}

function startTimer() {
  timer = 10;
  autoLabel.textContent = timer + "s";
  clearInterval(interval);
  interval = setInterval(() => {
    timer--;
    autoLabel.textContent = timer + "s";
    if (timer <= 0) { load(); startTimer(); }
  }, 1000);
}

refreshBtn.onclick = () => { load(); startTimer(); };

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

load();
startTimer();
