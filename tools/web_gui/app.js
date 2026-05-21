/* ═══════════════════════════════════════════
   Multi-Agent 协作工作台 · 前端逻辑
   ═══════════════════════════════════════════ */

const state = {
  tasks: [],
  task: null,
  providers: {},
  selectedSlug: "",
  live: true,
  eventSource: null,
  pollTimer: null,
  progressTimer: null,
  lastVersion: 0,
  theme: localStorage.getItem("ma-theme") || "light",
  autoPlayLocks: new Map(),
  runnerBusy: false,
  runLogs: new Map(),
  runLogLoading: new Set(),
  runLogFetchedAt: new Map(),
  pendingModalOpen: false,
};

const $ = (id) => document.getElementById(id);

/* ── 工具函数 ── */
function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function normStatus(s) {
  return String(s || "open").replace(/`/g, "").trim().toLowerCase();
}

function statusBadgeClass(status) {
  const s = normStatus(status);
  if (s === "answered" || s === "accepted" || s === "logged" || s === "finished") return "badge-success";
  if (s === "blocked" || s === "invalidated" || s === "failed" || s === "cancelled") return "badge-danger";
  if (s === "skipped") return "badge-success";
  return "badge-warning"; // open, running, queued
}

function statusLabel(status) {
  const map = {
    open: "进行中", answered: "已回复", accepted: "已接受",
    blocked: "已阻塞", invalidated: "已失效", queued: "排队中",
    running: "运行中", finished: "已完成", failed: "失败", logged: "已记录",
    cancelled: "已中断", skipped: "已跳过"
  };
  return map[normStatus(status)] || normStatus(status);
}

function providerLabel(status) {
  const map = { available: "✅ 就绪", not_ready: "❌ 未配置" };
  return map[status] || status;
}

function shorten(text, limit) {
  if (!text) return "";
  return text.length > limit ? text.slice(0, limit) + "\n..." : text;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function promptRequestId(prompt) {
  const match = String(prompt || "").match(/Request ID:\s*`?([^\s`]+)/);
  return match ? match[1] : "";
}

function isPendingRequest(request) {
  const s = normStatus(request?.status);
  return s === "open" || s === "blocked" || s === "running" || s === "queued";
}

function pendingRequests(task) {
  return [...(task?.requests || [])]
    .filter(isPendingRequest)
    .sort((a, b) => {
      const ap = Number(a.priority || 0);
      const bp = Number(b.priority || 0);
      if (bp !== ap) return bp - ap;
      return Number(a.ledger_index || 0) - Number(b.ledger_index || 0);
    });
}

function clearResolvedAutoPlayLocks(task) {
  const active = new Set((task.runs || [])
    .filter((r) => r.status === "queued" || r.status === "running")
    .map((r) => r.request_id)
    .filter(Boolean));
  const open = new Set((task.requests || [])
    .filter((r) => normStatus(r.status) === "open")
    .map((r) => r.request_id)
    .filter(Boolean));
  for (const requestId of Array.from(state.autoPlayLocks.keys())) {
    if (!active.has(requestId) && !open.has(requestId)) {
      state.autoPlayLocks.delete(requestId);
    }
  }
}

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `请求失败: ${res.status}`);
  return data;
}

/* ── 主题切换 ── */
function applyTheme() {
  document.documentElement.setAttribute("data-theme", state.theme);
  const btn = $("themeToggle");
  btn.textContent = state.theme === "dark" ? "☀️" : "◐";
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("ma-theme", state.theme);
  applyTheme();
}

/* ── 标签页 ── */
function initTabs() {
  document.querySelectorAll(".tab-bar .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      tab.closest(".card-tabs").querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.closest(".card-tabs").querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
      tab.classList.add("active");
      $(`tab-${target}`).classList.add("active");
    });
  });
}

/* ══════════ 数据加载 ══════════ */

async function loadTasks() {
  const [data, provData] = await Promise.all([
    fetchJson("/api/tasks"),
    fetchJson("/api/providers"),
  ]);
  state.providers = provData.providers || {};
  state.tasks = data.tasks || [];

  const select = $("taskSelect");
  select.innerHTML = "";
  for (const t of state.tasks) {
    const opt = document.createElement("option");
    opt.value = t.slug;
    opt.textContent = t.slug;
    select.appendChild(opt);
  }
  if (!state.selectedSlug && state.tasks.length) {
    state.selectedSlug = state.tasks[0].slug;
  }
  select.value = state.selectedSlug;
  if (state.selectedSlug) await loadTaskState(state.selectedSlug);
}

async function loadTaskState(slug) {
  state.selectedSlug = slug;
  state.task = await fetchJson(`/api/tasks/${encodeURIComponent(slug)}/state`);
  state.lastVersion = state.task.version || 0;
  render();
  connectLiveStream();
}

/* ── 实时流 ── */
function connectLiveStream() {
  closeLiveStream();
  if (!state.live || !state.selectedSlug) {
    renderLiveStatus("已暂停", false);
    return;
  }
  if (window.EventSource) {
    const url = `/api/tasks/${encodeURIComponent(state.selectedSlug)}/events`;
    state.eventSource = new EventSource(url);
    state.eventSource.addEventListener("open", () => renderLiveStatus("实时同步", true));
    state.eventSource.addEventListener("state", async (e) => {
      const d = JSON.parse(e.data);
      if (d.version && d.version !== state.lastVersion) await refreshFromLiveEvent(d.version);
    });
    state.eventSource.addEventListener("error", () => {
      renderLiveStatus("重连中...", false);
      startPolling();
    });
  } else {
    startPolling();
  }
}

function closeLiveStream() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  if (state.progressTimer) { clearInterval(state.progressTimer); state.progressTimer = null; }
}

function startProgressPolling() {
  if (state.progressTimer) return;
  state.progressTimer = setInterval(async () => {
    try {
      const data = await fetchJson("/api/runs");
      const runs = data.runs || [];
      // 只在有任何 running/queued 的 run 时才刷新
      const active = runs.filter(r => r.status === "running" || r.status === "queued");
      if (!active.length) {
        // 没有活动 run，停止轮询并做最终刷新
        clearInterval(state.progressTimer);
        state.progressTimer = null;
        if (state.selectedSlug) await loadTaskState(state.selectedSlug);
        return;
      }
      // 将进度数据合并到 state.task 中
      if (state.task && state.task.runs) {
        for (const ar of active) {
          const idx = state.task.runs.findIndex(r => r.run_id === ar.run_id);
          if (idx >= 0) {
            Object.assign(state.task.runs[idx], {
              status: ar.status,
              progress: ar.progress,
              progress_ts: ar.progress_ts,
            });
          }
        }
        renderRuns(state.task);
        renderGlobalStatus(state.task);
        renderActiveAgent(state.task);
      }
    } catch { /* ignore */ }
  }, 2000); // 每 2 秒轮询一次进度
}

function startPolling() {
  if (state.pollTimer || !state.live) return;
  state.pollTimer = setInterval(async () => {
    try {
      const next = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/state`);
      if ((next.version || 0) !== state.lastVersion) {
        state.task = next;
        state.lastVersion = next.version || 0;
        render();
      }
      renderLiveStatus("实时同步", true);
    } catch {
      renderLiveStatus("重试中", false);
    }
  }, 3000);
}

async function refreshFromLiveEvent(version) {
  const next = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/state`);
  state.task = next;
  state.lastVersion = version || next.version || 0;
  render();
}

function renderLiveStatus(label, on) {
  $("liveStatus").textContent = label;
  const dot = $("liveDot");
  dot.classList.toggle("on", on);
  dot.classList.toggle("off", !state.live);
}

/* ══════════ 渲染 ══════════ */

function render() {
  const task = state.task;
  if (!task) return;

  // 标题 & 概要
  $("taskTitle").textContent = task.title || task.slug;
  $("briefText").textContent = shorten(task.brief || "暂无任务概要", 3000);
  $("summaryText").textContent = shorten(task.summary || "暂无摘要", 4000);

  // 统计
  const open = pendingRequests(task);
  $("agentCount").textContent = task.agents.length;
  $("requestCount").textContent = task.requests.length;
  $("openCount").textContent = open.length;
  $("resourceCount").textContent = task.resources.length;

  renderSelectors(task);
  renderGlobalStatus(task);
  renderFlow(task);
  renderRequests(task);
  renderResources(task);
  renderActivity(task);
  renderAgentProfiles(task);
  renderRuns(task);
  renderActiveAgent(task);
  if (state.pendingModalOpen) renderPendingModal(task);
  renderLiveStatus("实时同步", true);
  
  checkAutoPlay(task);
}

function checkAutoPlay(task) {
  const toggle = $("autoPlayToggle");
  if (!toggle || !toggle.checked) return;
  const status = $("groupCommandStatus");
  clearResolvedAutoPlayLocks(task);
  if (!$("runnerBaseUrl").value.trim() || !$("runnerModel").value.trim() || !$("runnerApiKey").value.trim()) {
    if (status) status.textContent = "自动接力暂停：请先填写 API 地址、模型和 API Key。";
    return;
  }
  
  // 并发限制检查
  const maxConcurrent = task.max_concurrent || 3;
  const activeRuns = (task.runs || []).filter(r => r.status === "running" || r.status === "queued");
  if (activeRuns.length >= maxConcurrent) {
    if (status) status.textContent = `自动接力等待：运行中任务已达上限 ${maxConcurrent}。`;
    return;
  }
  
  // 寻找所有可运行的请求
  if (!task.requests) return;
  const openReqs = allRunnableRequests(task);
  
  // 遍历寻找第一个既没有在运行，也没有被锁定的请求来执行
  let toDispatch = null;
  for (const req of openReqs) {
    const requestId = req.request_id || "";
    const existingRun = (task.runs || []).find(r => (
      r.request_id === requestId && (r.status === "queued" || r.status === "running")
    ));
    if (existingRun) {
      state.autoPlayLocks.set(requestId, Date.now());
      continue; // 此请求已在运行，检查下一个
    }
    const lockedAt = state.autoPlayLocks.get(requestId);
    if (lockedAt && Date.now() - lockedAt < 120000) {
      continue; // 此请求最近已提交，正在排队，检查下一个
    }
    toDispatch = req;
    break; // 找到了一个可以启动的请求
  }

  if (toDispatch) {
    const requestId = toDispatch.request_id || "";
    state.autoPlayLocks.set(requestId, Date.now());
    const prompt = promptForRequest(toDispatch);
    if (status) status.textContent = `自动接力准备启动：${requestId} → ${toDispatch.to}`;
 
    setTimeout(() => {
      if ($("autoPlayToggle").checked) {
        const payload = apiConfigPayload(prompt, toDispatch.to, "execute");
        startRunnerPayload(payload, { requestId, statusEl: status });
      }
    }, 1500);
  } else {
    if (status && activeRuns.length === 0) {
      status.textContent = "自动接力就绪：暂无待处理任务。";
    }
  }
}

function allRunnableRequests(task) {
  return pendingRequests(task).filter(r => (
    normStatus(r.status) === "open"
    && r.request_id !== "TODO"
    && String(r.need || "").trim().toUpperCase() !== "TODO"
    && r.to !== "user"
    && r.to !== "workflow"
  ));
}

function nextRunnableRequest(task) {
  return allRunnableRequests(task)[0] || null;
}

function promptForRequest(req) {
  return `请处理派发给你的任务：\nRequest ID: ${req.request_id}\n来自: ${req.from}\n需求: ${req.need}\n\n请把实际产物写入对应任务文件；不要手动编辑 workflow ledger，后端状态机会在运行完成后更新 request 状态并派发后续任务。`;
}

function apiConfigPayload(prompt, role = "leader", mode = "execute") {
  return {
    protocol: $("runnerProtocol").value,
    role,
    mode,
    model: $("runnerModel").value,
    base_url: $("runnerBaseUrl").value,
    api_key: $("runnerApiKey").value,
    prompt,
  };
}

function hasApiConfig() {
  return $("runnerBaseUrl").value.trim() && $("runnerModel").value.trim() && $("runnerApiKey").value.trim();
}

function renderGlobalStatus(task) {
  const container = $("globalAgentStatus");
  if (!container) return;
  container.innerHTML = "";

  if (!task) return;

  // 统计 running/queued 数量
  const allRuns = task.runs || [];
  const runningCount = allRuns.filter(r => r.status === "running").length;
  const queuedCount = allRuns.filter(r => r.status === "queued").length;

  // 如果有运行中的 agent，启动进度轮询
  if (runningCount > 0 || queuedCount > 0) {
    startProgressPolling();
  }

  const runningAgents = new Set(
    allRuns
      .filter((r) => r.status === "running" && r.role)
      .map((r) => r.role)
  );

  const queuedAgents = new Map(
    allRuns
      .filter((r) => r.status === "queued" && r.role)
      .map((r) => [r.role, r.progress || "排队中…"])
  );

  const agentsSet = new Set(task.agents.length ? task.agents : ["leader"]);
  runningAgents.forEach(a => agentsSet.add(a));
  queuedAgents.forEach((_, a) => agentsSet.add(a));

  const allAgents = ["leader", "literature_collector", "mathematician", "code_expert", "latex_writer"];
  allAgents.forEach(a => agentsSet.add(a));

  Array.from(agentsSet).forEach(agent => {
    const el = document.createElement("div");
    el.className = "status-item";

    const dot = document.createElement("div");
    dot.className = "status-dot";

    if (runningAgents.has(agent)) {
      dot.classList.add("running");
    } else if (queuedAgents.has(agent)) {
      dot.classList.add("queued");
    } else if (task.agents.includes(agent) || agent === "leader") {
      dot.classList.add("active");
    }

    el.appendChild(dot);

    const name = document.createElement("span");
    name.textContent = agent;
    el.appendChild(name);

    // 显示进度文字
    if (runningAgents.has(agent)) {
      const run = allRuns.find(r => r.role === agent && r.status === "running");
      if (run && run.progress) {
        const prog = document.createElement("span");
        prog.className = "agent-progress-text";
        prog.textContent = run.progress;
        el.appendChild(prog);
      }
    } else if (queuedAgents.has(agent)) {
      const prog = document.createElement("span");
      prog.className = "agent-progress-text queued-text";
      prog.textContent = queuedAgents.get(agent);
      el.appendChild(prog);
    }

    container.appendChild(el);
  });
}

function renderSelectors(task) {
  // Agent 筛选
  const af = $("agentFilter");
  const afVal = af.value;
  af.innerHTML = '<option value="">所有 Agent</option>';
  for (const a of task.agents) {
    const o = document.createElement("option");
    o.value = a; o.textContent = a; af.appendChild(o);
  }
  af.value = afVal;

  // 资源筛选
  const rf = $("resourceFilter");
  const rfVal = rf.value;
  const owners = [...new Set(task.resources.map((r) => r.owner).filter(Boolean))].sort();
  rf.innerHTML = '<option value="">所有者</option>';
  for (const ow of owners) {
    const o = document.createElement("option");
    o.value = ow; o.textContent = ow; rf.appendChild(o);
  }
  rf.value = rfVal;
}

function renderFlow(task) {
  const container = $("flowGraph");
  container.innerHTML = "";

  const runningAgents = new Set(
    (task.runs || [])
      .filter((r) => r.status === "running" && r.role)
      .map((r) => r.role)
  );

  const agentsSet = new Set(task.agents.length ? task.agents : ["leader"]);
  runningAgents.forEach(a => agentsSet.add(a));
  const agents = Array.from(agentsSet);

  if (agents.length < 2) {
    container.innerHTML = '<div class="flow-empty">Agent 数量不足，无法绘制拓扑</div>';
    return;
  }

  const W = Math.max(container.clientWidth, 600);
  const H = Math.max(container.clientHeight, 260);
  const cx = W / 2, cy = H / 2;
  const rx = Math.max(150, W / 2 - 100);
  const ry = Math.max(85, H / 2 - 60);
  const pos = {};

  agents.forEach((a, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / agents.length;
    pos[a] = { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  });

  // SVG 连线
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "flow-svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  for (const req of task.requests) {
    const from = req.from, to = req.to;
    if (!pos[from] || !pos[to] || from === to) continue;
    const s = normStatus(req.status);
    const color = s === "answered" || s === "accepted" ? "var(--success)"
      : s === "blocked" || s === "invalidated" ? "var(--danger)" : "var(--warning)";

    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", pos[from].x);
    line.setAttribute("y1", pos[from].y);
    line.setAttribute("x2", pos[to].x);
    line.setAttribute("y2", pos[to].y);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-opacity", "0.5");
    svg.appendChild(line);
  }
  container.appendChild(svg);

  const hasRunning = (task.runs || []).some(r => r.status === "running");
  const hasPending = task.requests.some(r => ["open", "queued", "blocked"].includes(normStatus(r.status)));
  
  let globalStatusText = "已完成";
  let globalStatusBg = "var(--success)";
  if (hasRunning) {
    globalStatusText = "进行中";
    globalStatusBg = "var(--warning)";
  } else if (hasPending) {
    globalStatusText = "待命中";
    globalStatusBg = "var(--danger)";
  }

  const statsNode = document.createElement("div");
  statsNode.className = "flow-stats-overlay";
  statsNode.innerHTML = `
    <div class="stat-item" style="font-weight:bold; font-size: 1rem;">
      总任务状态: <span class="badge" style="background:${globalStatusBg}; font-size: 0.9rem; margin-left: 6px;">${globalStatusText}</span>
    </div>
  `;
  container.appendChild(statsNode);

  // Agent 节点
  for (const a of agents) {
    const p = pos[a];
    const node = document.createElement("div");
    node.className = "agent-node";
    if (runningAgents.has(a)) {
      node.className += " agent-running";
    }
    node.style.left = `${Math.max(4, Math.min(W - 144, p.x - 70))}px`;
    node.style.top = `${Math.max(4, Math.min(H - 58, p.y - 27))}px`;
    const sent = task.requests.filter((r) => r.from === a).length;
    const recv = task.requests.filter((r) => r.to === a).length;
    
    let titleStr = `<strong>${esc(a)}</strong>`;
    if (runningAgents.has(a)) {
      titleStr = `<strong>🔴 ${esc(a)}</strong>`;
    }
    
    node.innerHTML = `${titleStr}<span>${sent} 发出 / ${recv} 收到</span>`;
    container.appendChild(node);
  }
}

function renderRequests(task) {
  const filter = $("agentFilter").value;
  const list = $("requestList");
  list.innerHTML = "";

  const reqs = task.requests.filter((r) => {
    return !filter || [r.from, r.to].includes(filter);
  });

  if (!reqs.length) {
    list.innerHTML = '<div class="request-card"><div class="card-body">暂无对话请求</div></div>';
    return;
  }

  for (const r of reqs) {
    const card = document.createElement("article");
    card.className = "request-card";
    card.innerHTML = `
      <div class="card-meta">
        <span class="badge ${statusBadgeClass(r.status)}">${statusLabel(r.status)}</span>
        <strong class="agent-name">${esc(r.from || "")}</strong>
        <span>→</span>
        <strong class="agent-name">${esc(r.to || "")}</strong>
        <span>${esc(r.type || "")}</span>
      </div>
      <div class="card-body">${esc(r.need || "无描述")}</div>
      ${r.context ? `<div class="request-context">${esc(r.context)}</div>` : ""}
      ${r.note ? `<div class="request-context"><strong>处理备注:</strong><br>${esc(r.note)}</div>` : ""}
      <div class="card-footer">
        <span>${esc(r.request_id || "")}</span>
        ${r.parent && r.parent !== "none" ? `<span>父: ${esc(r.parent)}</span>` : ""}
      </div>
    `;
    list.appendChild(card);
  }
}

function openPendingModal() {
  state.pendingModalOpen = true;
  $("pendingModal").classList.add("active");
  renderPendingModal(state.task);
}

function closePendingModal() {
  state.pendingModalOpen = false;
  $("pendingModal").classList.remove("active");
}

function renderPendingModal(task) {
  const list = $("pendingList");
  const summary = $("pendingModalSummary");
  if (!list || !summary) return;
  const items = pendingRequests(task);
  summary.textContent = items.length ? `共 ${items.length} 项，优先级高的会先被自动接力处理` : "暂无待处理项目";
  list.innerHTML = "";

  if (!items.length) {
    list.innerHTML = '<div class="pending-empty">当前没有 open / blocked / queued / running 项目。</div>';
    return;
  }

  for (const item of items) {
    const card = document.createElement("article");
    card.className = "pending-item";
    const priority = Number(item.priority || 0);
    card.innerHTML = `
      <div class="pending-main">
        <div class="pending-meta">
          <span class="badge ${statusBadgeClass(item.status)}">${statusLabel(item.status)}</span>
          <strong>${esc(item.from || "")}</strong>
          <span>→</span>
          <strong>${esc(item.to || "")}</strong>
          <span>${esc(item.type || "")}</span>
        </div>
        <div class="pending-need">${esc(item.need || "无描述")}</div>
        ${item.context ? `<div class="pending-context">${esc(item.context)}</div>` : ""}
        ${item.note ? `<div class="pending-context"><strong>处理备注:</strong><br>${esc(item.note)}</div>` : ""}
        <div class="pending-footer">
          <span>${esc(item.request_id || "")}</span>
          ${item.parent && item.parent !== "none" ? `<span>父: ${esc(item.parent)}</span>` : ""}
        </div>
      </div>
      <div class="pending-priority">
        <span class="priority-value">P ${priority}</span>
        <button class="btn-ghost-sm priority-btn" data-request-id="${esc(item.request_id || "")}" data-action="top">置顶</button>
        <button class="btn-ghost-sm priority-btn" data-request-id="${esc(item.request_id || "")}" data-action="up">上调</button>
        <button class="btn-ghost-sm priority-btn" data-request-id="${esc(item.request_id || "")}" data-action="down">下调</button>
        <button class="btn-ghost-sm priority-btn" data-request-id="${esc(item.request_id || "")}" data-action="reset">重置</button>
      </div>
    `;
    list.appendChild(card);
  }
}

async function updateRequestPriority(requestId, action) {
  if (!state.selectedSlug || !requestId) return;
  const buttons = [...document.querySelectorAll(".priority-btn")]
    .filter((btn) => btn.dataset.requestId === requestId);
  buttons.forEach((btn) => { btn.disabled = true; });
  try {
    await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/requests/priority`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, action }),
    });
    await loadTaskState(state.selectedSlug);
  } catch (err) {
    alert(`调整优先级失败: ${err.message}`);
  } finally {
    buttons.forEach((btn) => { btn.disabled = false; });
  }
}

function renderResources(task) {
  const filter = $("resourceFilter").value;
  const list = $("resourceList");
  list.innerHTML = "";

  const res = task.resources.filter((r) => !filter || r.owner === filter);
  if (!res.length) {
    list.innerHTML = '<div class="resource-card"><div class="card-body">暂无共享资源</div></div>';
    return;
  }

  for (const r of res) {
    const card = document.createElement("article");
    card.className = "resource-card";
    card.innerHTML = `
      <div class="card-meta">
        <span class="badge ${statusBadgeClass(r.status || "available")}">${statusLabel(r.status || "available")}</span>
        <strong>${esc(r.resource_id || "")}</strong>
        <span>${esc(r.owner || "")}</span>
        <span>${esc(r.type || "")}</span>
      </div>
      <div class="card-body">${esc(r.path_link || r.description || "")}</div>
      <div class="card-footer">
        ${r.reusable_by ? `<span>可复用: ${esc(r.reusable_by)}</span>` : ""}
        ${r.notes ? `<span>${esc(r.notes)}</span>` : ""}
      </div>
    `;
    list.appendChild(card);
  }
}

function renderActivity(task) {
  const list = $("activityList");
  list.innerHTML = "";
  const activity = task.activity || [];

  if (!activity.length) {
    list.innerHTML = '<div class="activity-card"><div class="card-body">暂无活动记录</div></div>';
    return;
  }

  for (const item of [...activity].reverse()) {
    const title = item.title || "";
    const isWeb = title.includes("Intervention") || title.includes("干预");
    const isOverride = title.includes("Override") || title.includes("纠偏");
    const card = document.createElement("article");
    card.className = "activity-card";

    let badgeClass = "badge-muted";
    let badgeText = "日志";
    if (isOverride) { badgeClass = "badge-danger"; badgeText = "🚨 管理员纠偏"; }
    else if (isWeb) { badgeClass = "badge-warning"; badgeText = "用户命令"; }
    else if (title.includes("Agent Run")) { badgeClass = "badge-success"; badgeText = "🤖 调度记录"; }

    card.innerHTML = `
      <div class="card-meta">
        <span class="badge ${badgeClass}">${badgeText}</span>
        <strong class="agent-name">${esc(title)}</strong>
        <span>${esc(item.from || "")} → ${esc(item.to || "")}</span>
      </div>
      <div class="card-body">${esc(item.summary || item.body || "")}</div>
    `;
    list.appendChild(card);
  }
}

function pickActiveRun(task) {
  const runs = task.runs || [];
  return runs.find((r) => normStatus(r.status) === "running")
    || runs.find((r) => normStatus(r.status) === "queued")
    || runs.find((r) => ["failed", "finished", "logged"].includes(normStatus(r.status)))
    || runs[0]
    || null;
}

async function requestRunLog(run) {
  if (!state.selectedSlug || !run?.run_id) return;
  const runId = run.run_id;
  if (state.runLogLoading.has(runId)) return;
  const status = normStatus(run.status);
  const cached = state.runLogs.get(runId);
  const fetchedAt = state.runLogFetchedAt.get(runId) || 0;
  if (Date.now() - fetchedAt < 2500) return;
  if (cached?.ok && status !== "running" && status !== "queued") return;

  state.runLogLoading.add(runId);
  try {
    const data = await fetchJson(
      `/api/tasks/${encodeURIComponent(state.selectedSlug)}/runs/${encodeURIComponent(runId)}/log`
    );
    state.runLogs.set(runId, { ...data, ok: true });
  } catch (err) {
    state.runLogs.set(runId, { run_id: runId, ok: false, log: `日志暂不可用：${err.message}` });
  } finally {
    state.runLogFetchedAt.set(runId, Date.now());
    state.runLogLoading.delete(runId);
  }

  const current = pickActiveRun(state.task || {});
  if (current?.run_id === runId) renderActiveAgent(state.task);
}

function renderActiveAgent(task) {
  const badge = $("activeAgentBadge");
  const meta = $("activeAgentMeta");
  const output = $("activeAgentOutput");
  if (!badge || !meta || !output) return;

  const run = pickActiveRun(task);
  if (!run) {
    badge.textContent = "空闲";
    badge.className = "status-badge badge-muted";
    meta.textContent = "暂无活跃 Agent";
    output.textContent = "没有运行中的 agent。";
    return;
  }

  const status = normStatus(run.status);
  badge.textContent = statusLabel(status);
  badge.className = `status-badge ${statusBadgeClass(status)}`;

  const metaParts = [
    run.role ? `Agent: ${run.role}` : "",
    run.mode ? `模式: ${run.mode}` : "",
    run.request_id ? `Request: ${run.request_id}` : "",
    run.started_at ? `开始: ${formatTime(run.started_at)}` : "",
    run.finished_at ? `结束: ${formatTime(run.finished_at)}` : "",
  ].filter(Boolean);
  meta.innerHTML = `
    <div>${esc(metaParts.join(" · ") || run.run_id)}</div>
    <div class="active-agent-note">显示可见执行日志、模型输出和本地命令结果；不包含隐藏推理链。</div>
  `;

  const cached = state.runLogs.get(run.run_id);
  const pieces = [];
  if (run.progress) pieces.push(`[progress] ${run.progress}`);
  if (run.error) pieces.push(`[error] ${run.error}`);
  if (cached?.truncated) pieces.push("[log] 已截取最近日志内容。");
  if (cached?.log) pieces.push(cached.log);
  if (!pieces.length) pieces.push(state.runLogLoading.has(run.run_id) ? "正在读取日志..." : "日志尚未写入。");
  output.textContent = pieces.join("\n\n");

  requestRunLog(run);
}

function renderRuns(task) {
  const list = $("runList");
  if (!list) return;
  list.innerHTML = "";
  const runs = task.runs || [];

  if (!runs.length) {
    list.innerHTML = '<div class="run-card"><div class="card-meta"><span>暂无运行记录</span></div></div>';
    return;
  }

  for (const run of runs.slice(0, 8)) {
    const card = document.createElement("article");
    card.className = "run-card";
    const progressHtml = run.progress
      ? `<div class="run-progress"><span class="progress-text">${esc(run.progress)}</span></div>`
      : "";
    card.innerHTML = `
      <div class="card-meta">
        <span class="badge ${statusBadgeClass(run.status)}">${statusLabel(run.status)}</span>
        <strong>${esc(run.role || "")}</strong>
        <span>${esc(run.mode || "")}</span>
        ${run.request_id ? `<span class="run-req-id">${esc(run.request_id)}</span>` : ""}
      </div>
      ${progressHtml}
      <div class="card-footer">
        <span>${esc(run.run_id || "")}</span>
      </div>
    `;
    list.appendChild(card);
  }
}

function renderAgentProfiles(task) {
  const list = $("agentProfileList");
  if (!list) return;
  const profiles = task.agent_profiles || [];
  if (!profiles.length) {
    list.innerHTML = '<div class="profile-empty">暂无 Agent 角色文件</div>';
    return;
  }
  list.innerHTML = "";
  for (const profile of profiles) {
    const item = document.createElement("div");
    item.className = "profile-item";
    const exists = profile.exists ? "已创建" : "缺少文件";
    item.innerHTML = `
      <div>
        <strong>${esc(profile.role || "")}</strong>
        <span>${esc(profile.path || "")}</span>
      </div>
      <em class="${profile.exists ? "profile-ok" : "profile-missing"}">${exists}</em>
    `;
    list.appendChild(item);
  }
}


/* ── 验证 API ── */
async function verifyApi() {
  const btn = $("verifyApiBtn");
  const badge = $("apiStatusBadge");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "验证中...";
  badge.textContent = "验证中";
  badge.className = "status-badge badge-warning";

  const payload = {
    protocol: $("runnerProtocol").value,
    base_url: $("runnerBaseUrl").value,
    model: $("runnerModel").value,
    api_key: $("runnerApiKey").value,
  };

  try {
    const res = await fetchJson("/api/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    badge.textContent = "✅ " + res.message;
    badge.className = "status-badge badge-success";
  } catch (err) {
    badge.textContent = "❌ 连接失败";
    badge.className = "status-badge badge-danger";
    alert(`API 验证失败: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "验证连接";
  }
}

async function submitGroupCommand() {
  const input = $("groupCommandInput");
  const status = $("groupCommandStatus");
  const message = input.value.trim();
  if (!message) {
    status.textContent = "请输入命令";
    input.focus();
    return;
  }
  if (!state.selectedSlug) {
    status.textContent = "请先选择任务";
    return;
  }
  if (!hasApiConfig()) {
    status.textContent = "请先填写 API 地址、模型和 API Key";
    return;
  }

  const btn = $("groupCommandButton");
  btn.disabled = true;
  status.textContent = "正在交给 Leader...";

  try {
    const intervention = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/interventions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "instruction",
        target: "leader",
        parent: "none",
        artifact: "logs/workflow.db",
        message: `用户对整个 agent 工作组下发命令：\n${message}`,
      }),
    });
    if (intervention.request_id) {
      state.autoPlayLocks.set(intervention.request_id, Date.now());
    }

    const leaderPrompt = [
      "用户对整个 agent 工作组下发了新的总控命令。",
      intervention.request_id ? `Request ID: ${intervention.request_id}` : "",
      "",
      "请你以 Leader 角色处理：",
      "1. 重新解释用户目标和验收标准。",
      "2. 检查当前 open/blocked 请求，决定保留、关闭、重排或新增。",
      "3. 输出面向 specialist agent 的拆解方案，包含 Need 和 Artifact / Resource；后端状态机会负责创建结构化 request。",
      "4. 不要手动编辑 workflow ledger，也不要把所有工作留给 Leader 自己完成。",
      "",
      "用户命令：",
      message,
    ].filter(Boolean).join("\n");

    const result = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(apiConfigPayload(leaderPrompt, "leader", "execute")),
    });

    input.value = "";
    status.textContent = `已交给 Leader: ${result.run_id}`;
    await loadTaskState(state.selectedSlug);
  } catch (err) {
    status.textContent = `失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function createAgent() {
  const status = $("agentCreateStatus");
  const role = $("agentNameInput").value.trim();
  const title = $("agentTitleInput").value.trim();
  const mission = $("agentMissionInput").value.trim();
  if (!role) {
    status.textContent = "请输入 Agent ID";
    $("agentNameInput").focus();
    return;
  }
  if (!state.selectedSlug) {
    status.textContent = "请先选择任务";
    return;
  }

  const btn = $("agentCreateButton");
  btn.disabled = true;
  status.textContent = "正在创建 / 添加...";
  try {
    const result = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/agents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, title, mission }),
    });
    $("agentNameInput").value = "";
    $("agentTitleInput").value = "";
    $("agentMissionInput").value = "";
    status.textContent = `已添加 ${result.role}`;
    await loadTaskState(state.selectedSlug);
  } catch (err) {
    status.textContent = `失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function continueNextTask() {
  const status = $("groupCommandStatus");
  if (!state.task || !state.selectedSlug) {
    if (status) status.textContent = "请先选择任务";
    return;
  }
  if (!hasApiConfig()) {
    if (status) status.textContent = "请先填写 API 地址、模型和 API Key";
    return;
  }
  const isRunning = (state.task.runs || []).some((r) => r.status === "running" || r.status === "queued");
  if (isRunning) {
    if (status) status.textContent = "已有任务正在运行或排队，先中断或等待完成。";
    return;
  }
  const req = nextRunnableRequest(state.task);
  if (!req) {
    if (status) status.textContent = "没有可继续的 open request。";
    return;
  }
  const requestId = req.request_id || "";
  state.autoPlayLocks.set(requestId, Date.now());
  if (status) status.textContent = `手动继续：${requestId} → ${req.to}`;
  await startRunnerPayload(apiConfigPayload(promptForRequest(req), req.to, "execute"), { requestId, statusEl: status });
}

async function interruptRuns() {
  const status = $("groupCommandStatus");
  if (!state.selectedSlug) {
    if (status) status.textContent = "请先选择任务";
    return;
  }
  const btn = $("interruptRunsButton");
  btn.disabled = true;
  if (status) status.textContent = "正在中断运行/排队任务...";
  try {
    const result = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/runs/interrupt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (status) status.textContent = result.cancelled?.length
      ? `已中断：${result.cancelled.join(", ")}`
      : "当前没有运行或排队任务。";
    await loadTaskState(state.selectedSlug);
  } catch (err) {
    if (status) status.textContent = `中断失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function restartServer() {
  const status = $("groupCommandStatus");
  const btn = $("restartServerButton");
  btn.disabled = true;
  if (status) status.textContent = "服务器正在重启...";
  try {
    await fetchJson("/api/server/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } catch {
    // The connection may close while the process execs itself.
  }

  for (let i = 0; i < 40; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    try {
      await fetchJson("/api/tasks");
      if (status) status.textContent = "服务器已重启。";
      btn.disabled = false;
      if (state.selectedSlug) await loadTaskState(state.selectedSlug);
      return;
    } catch {
      if (status) status.textContent = `服务器正在重启... ${i + 1}`;
    }
  }
  if (status) status.textContent = "服务器重启后暂未响应，请手动刷新页面。";
  btn.disabled = false;
}

/* ── 启动 Runner ── */
async function startRunnerPayload(payload, options = {}) {
  const status = options.statusEl || $("groupCommandStatus");
  const requestId = options.requestId || promptRequestId(payload.prompt);
  if (state.runnerBusy) {
    if (status) status.textContent = "Runner 正在启动，请稍候。";
    return;
  }
  if (requestId) {
    const existingRun = (state.task?.runs || []).find(r => (
      r.request_id === requestId && (r.status === "queued" || r.status === "running")
    ));
    if (existingRun) {
      if (status) status.textContent = `已跳过：${requestId} 正在 ${existingRun.run_id} 中运行。`;
      state.autoPlayLocks.set(requestId, Date.now());
      return;
    }
  }

  state.runnerBusy = true;
  if (status) status.textContent = "自动接力正在启动...";
  try {
    const result = await fetchJson(`/api/tasks/${encodeURIComponent(state.selectedSlug)}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (status) status.textContent = `自动接力已启动: ${result.run_id}`;
    if (result.request_id) state.autoPlayLocks.set(result.request_id, Date.now());
    await loadTaskState(state.selectedSlug);
  } catch (err) {
    if (status) status.textContent = `自动接力失败：${err.message}`;
    if (requestId) state.autoPlayLocks.delete(requestId);
  } finally {
    state.runnerBusy = false;
  }
}

/* ── 简易 slug 生成 ── */
function autoSlug(text) {
  const ascii = text.normalize("NFKD").replace(/[^\w\s-]/g, "").trim();
  return ascii.toLowerCase().replace(/[\s_]+/g, "-").replace(/-+/g, "-").slice(0, 64) || "task";
}

/* ══════════ 新建任务弹窗 ══════════ */
function openCreateModal() {
  const modal = $("createTaskModal");
  modal.classList.add("active");
  $("newTaskTitle").value = "";
  $("newTaskSlug").value = "";
  $("newTaskAgents").value = "leader,literature_collector,mathematician,code_expert,latex_writer";
  $("newTaskForce").checked = false;
  $("createTaskStatus").textContent = "";
  $("submitCreateBtn").disabled = false;
  $("submitCreateBtn").textContent = "创建任务";
  setTimeout(() => $("newTaskTitle").focus(), 100);
}

function closeCreateModal() {
  $("createTaskModal").classList.remove("active");
}

async function createNewTask() {
  const title = $("newTaskTitle").value.trim();
  if (!title) {
    $("createTaskStatus").textContent = "❌ 请输入任务标题";
    $("newTaskTitle").focus();
    return;
  }

  const slug = $("newTaskSlug").value.trim() || autoSlug(title);
  const agents = $("newTaskAgents").value.trim();
  const force = $("newTaskForce").checked;

  const btn = $("submitCreateBtn");
  const status = $("createTaskStatus");
  btn.disabled = true;
  btn.textContent = "创建中...";
  status.textContent = "正在创建任务工作空间...";

  try {
    const result = await fetchJson("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, slug, agents, force }),
    });

    status.textContent = `✅ 已创建「${result.title}」(${result.files_count} 个文件)`;
    btn.textContent = "✅ 创建成功";

    // 刷新任务列表并自动切换到新任务
    state.selectedSlug = result.slug;
    await loadTasks();

    setTimeout(closeCreateModal, 800);
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    btn.disabled = false;
    btn.textContent = "创建任务";
  }
}

function updateTimestamp() {
  const target = $("timestampText");
  if (!target) return;
  target.textContent = new Date().toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/* ══════════ 事件绑定 ══════════ */

$("taskSelect").addEventListener("change", (e) => loadTaskState(e.target.value));
$("refreshButton").addEventListener("click", () => loadTaskState(state.selectedSlug));
$("agentFilter").addEventListener("change", () => renderRequests(state.task));
$("resourceFilter").addEventListener("change", () => renderResources(state.task));
$("groupCommandButton").addEventListener("click", submitGroupCommand);
$("groupCommandInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    submitGroupCommand();
  }
});
$("verifyApiBtn").addEventListener("click", verifyApi);
$("agentCreateButton").addEventListener("click", createAgent);
$("themeToggle").addEventListener("click", toggleTheme);
$("continueTaskButton").addEventListener("click", continueNextTask);
$("interruptRunsButton").addEventListener("click", interruptRuns);
$("restartServerButton").addEventListener("click", restartServer);
$("pendingStatCard").addEventListener("click", openPendingModal);
$("pendingStatCard").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    openPendingModal();
  }
});
$("closePendingModalBtn").addEventListener("click", closePendingModal);
$("refreshPendingBtn").addEventListener("click", () => loadTaskState(state.selectedSlug));
$("pendingModal").addEventListener("click", (e) => {
  if (e.target === $("pendingModal")) closePendingModal();
});
$("pendingList").addEventListener("click", (e) => {
  const btn = e.target.closest(".priority-btn");
  if (!btn) return;
  updateRequestPriority(btn.dataset.requestId, btn.dataset.action);
});

// 新建任务弹窗
$("createTaskBtn").addEventListener("click", openCreateModal);
$("closeModalBtn").addEventListener("click", closeCreateModal);
$("cancelModalBtn").addEventListener("click", closeCreateModal);
$("submitCreateBtn").addEventListener("click", createNewTask);
$("createTaskModal").addEventListener("click", (e) => {
  if (e.target === $("createTaskModal")) closeCreateModal();
});

// 标题输入时自动生成 slug（仅在 slug 为空时）
$("newTaskTitle").addEventListener("input", () => {
  if (!$("newTaskSlug").value) {
    $("newTaskSlug").placeholder = autoSlug($("newTaskTitle").value || "task");
  }
});

// Enter 键提交
$("newTaskTitle").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); createNewTask(); }
});

// Esc 键关闭弹窗
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("pendingModal").classList.contains("active")) {
    closePendingModal();
  }
  if (e.key === "Escape" && $("createTaskModal").classList.contains("active")) {
    closeCreateModal();
  }
});


$("liveToggle").addEventListener("click", () => {
  state.live = !state.live;
  $("liveToggle").textContent = state.live ? "暂停" : "继续";
  connectLiveStream();
});

$("runnerApiKey").addEventListener("input", () => {
  if ($("rememberApiKey").checked) {
    localStorage.setItem("ma-api-key", $("runnerApiKey").value);
  }
});

$("runnerBaseUrl").addEventListener("input", () => {
  localStorage.setItem("ma-base-url", $("runnerBaseUrl").value);
});

$("runnerModel").addEventListener("input", () => {
  localStorage.setItem("ma-model", $("runnerModel").value);
});

$("runnerProtocol").addEventListener("change", () => {
  localStorage.setItem("ma-protocol", $("runnerProtocol").value);
});

$("rememberApiKey").addEventListener("change", (e) => {
  if (e.target.checked) {
    localStorage.setItem("ma-api-key", $("runnerApiKey").value);
  } else {
    localStorage.removeItem("ma-api-key");
  }
});

const savedApiKey = localStorage.getItem("ma-api-key");
if (savedApiKey) {
  $("runnerApiKey").value = savedApiKey;
  $("rememberApiKey").checked = true;
}
const savedBaseUrl = localStorage.getItem("ma-base-url");
if (savedBaseUrl) $("runnerBaseUrl").value = savedBaseUrl;
const savedModel = localStorage.getItem("ma-model");
if (savedModel) $("runnerModel").value = savedModel;
const savedProtocol = localStorage.getItem("ma-protocol");
if (savedProtocol) $("runnerProtocol").value = savedProtocol;

window.addEventListener("resize", () => state.task && renderFlow(state.task));

/* ══════════ 初始化 ══════════ */
initTabs();
applyTheme();
updateTimestamp();
setInterval(updateTimestamp, 1000);
loadTasks().catch((err) => {
  $("taskTitle").textContent = "加载失败";
  $("briefText").textContent = err.message;
});
