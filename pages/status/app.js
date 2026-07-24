const bridge = window.AstrBotPluginPage;

const tableBody = document.getElementById("groupTable");
const emptyMsg = document.getElementById("emptyMsg");
const meta = document.getElementById("meta");
const refreshBtn = document.getElementById("refreshBtn");
const autoLabel = document.getElementById("autoLabel");

await bridge.ready();

let timer = 10;
let interval;

async function load() {
  try {
    const data = await bridge.apiGet("status/groups");
    meta.textContent =
      `共 ${data.total_groups} 个群 | 接管模式: ${data.override ? "开启" : "关闭"} | 累积模式: ${data.accum_enabled ? "开启" : "关闭"}`;

    if (!data.groups || !data.groups.length) {
      tableBody.innerHTML = "";
      emptyMsg.style.display = "block";
      return;
    }
    emptyMsg.style.display = "none";
    tableBody.innerHTML = data.groups
      .map((g) => {
        const bar = "█".repeat(Math.floor(g.flow / 5)) + "░".repeat(20 - Math.floor(g.flow / 5));
        const ago = g.last_reply_ago < 0 ? "未发言" : g.last_reply_ago < 60 ? `${g.last_reply_ago}s` : `${Math.floor(g.last_reply_ago / 60)}min`;
        return `<tr>
          <td title="${g.group_id}">${g.group_id}</td>
          <td><span class="flow-bar">${bar}</span> ${g.flow.toFixed(0)}</td>
          <td>${ago}</td>
          <td>${g.recent_replies}</td>
          <td>${g.pending > 0 ? `+${g.pending}` : "0"}</td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    emptyMsg.style.display = "block";
    emptyMsg.textContent = "加载失败: " + (e.message || "网络错误");
  }
}

function startAutoRefresh() {
  timer = 10;
  autoLabel.textContent = `${timer}s 后自动刷新`;
  clearInterval(interval);
  interval = setInterval(() => {
    timer--;
    autoLabel.textContent = `${timer}s 后自动刷新`;
    if (timer <= 0) {
      load();
      startAutoRefresh();
    }
  }, 1000);
}

refreshBtn.onclick = () => { load(); startAutoRefresh(); };

load();
startAutoRefresh();
