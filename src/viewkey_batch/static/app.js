const UI_STATE_KEY = "viewkey-ui-state";

function savedUiState() {
  try { return JSON.parse(localStorage.getItem(UI_STATE_KEY) || "{}"); }
  catch (_) { return {}; }
}

const saved = savedUiState();
const state = {
  videos: [],
  statuses: {},
  downloadedKeys: new Set(),
  downloads: [],
  downloadCounts: {},
  settings: null,
  downloadPollTimer: null,
  selected: new Set(),
  downloadSelected: new Set(),
  mode: saved.mode || "category",
  activeView: saved.activeView || "catalog",
  category: saved.category || "latest",
  page: Number(saved.page) || 1,
  pagination: {has_previous: false, has_next: false},
  browseRequest: 0,
  animationTimer: null,
  jobId: null,
  jobTimer: null,
  captchaSid: null,
  downloadSignature: "",
  downloadFilter: "pending",
  settingsSection: saved.settingsSection || "settingsAccount",
  lastErrorToast: "",
  lastErrorAt: 0,
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[c]);

function toast(message, error = false) {
  if (error) {
    const now = Date.now();
    if (state.lastErrorToast === message && now - state.lastErrorAt < 8000) return;
    state.lastErrorToast = message;
    state.lastErrorAt = now;
  }
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toastRegion").append(node);
  setTimeout(() => node.remove(), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    ...options,
  });
  const body = await response.text();
  let data = {};
  try { data = body ? JSON.parse(body) : {}; } catch (_) { data = {detail: body || "服务器未返回 JSON"}; }
  if (!response.ok) throw new Error(`服务器返回异常（HTTP ${response.status}）：${data.detail || "请求失败"}`);
  return data;
}

// identity 是后端的去重主键；旧数据没有 identity 字段时回退到 viewkey
const videoKey = (video) => video.identity || video.viewkey;

function visibleVideos() {
  const query = $("#searchInput").value.trim().toLowerCase();
  return state.videos.filter(video => {
    const sources = video.sources?.length ? video.sources : [video.source];
    const inSource = state.mode !== "category" || sources.includes(state.category);
    const matches = !query || `${video.title} ${video.author} ${video.viewkey}`.toLowerCase().includes(query);
    return inSource && matches;
  });
}

function persistUiState() {
  localStorage.setItem(UI_STATE_KEY, JSON.stringify({
    activeView: state.activeView,
    mode: state.mode,
    category: state.category,
    page: state.page,
    settingsSection: state.settingsSection,
    search: $("#searchInput")?.value || "",
  }));
}

function syncRoute(replace = false) {
  const params = new URLSearchParams({category: state.category, page: String(state.page)});
  const hash = state.activeView === "downloads" ? "#/downloads" : state.activeView === "settings" ? "#/settings" : `#/catalog?${params}`;
  history[replace ? "replaceState" : "pushState"](null, "", hash);
  persistUiState();
}

function readRoute() {
  const match = location.hash.match(/^#\/(catalog|downloads|settings)(?:\?(.*))?$/);
  if (!match) return;
  state.activeView = match[1];
  const params = new URLSearchParams(match[2] || "");
  if (params.get("category")) state.category = params.get("category");
  if (/^\d+$/.test(params.get("page") || "")) state.page = Math.max(1, Number(params.get("page")));
}

function stateText(status) {
  const labels = {queued: "等待下载", downloading: "正在下载", completed: "下载完成", failed: "下载失败"};
  return labels[status?.state] || "";
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function render(animate = false) {
  const videos = visibleVideos();
  const resultCount = $("#resultCount");
  if (resultCount) resultCount.textContent = videos.length;
  $("#selectedCount").textContent = state.selected.size;
  $("#catalogBadge").textContent = state.videos.length;
  $("#downloadButton").disabled = state.selected.size === 0;
  $("#removeButton").disabled = state.selected.size === 0;
  $("#selectAll").checked = videos.length > 0 && videos.every(v => state.selected.has(videoKey(v)));
  $("#selectAll").indeterminate = videos.some(v => state.selected.has(videoKey(v))) && !$("#selectAll").checked;
  $("#emptyState").classList.toggle("hidden", videos.length > 0);
  $("#videoGrid").classList.toggle("hidden", videos.length === 0);

  const grid = $("#videoGrid");
  const existing = new Map([...grid.children].map(card => [card.dataset.key, card]));
  videos.forEach((video, index) => {
    const key = videoKey(video);
    const selected = state.selected.has(key);
    const status = state.statuses[key];
    let card = existing.get(key);
    const contentSignature = JSON.stringify([video.title, video.author, video.thumbnail_url, video.duration, video.views]);
    if (card && card.dataset.contentSignature !== contentSignature) {
      card.remove();
      card = null;
    }
    if (!card) {
      const template = document.createElement("template");
      template.innerHTML = `<article class="video-card" data-key="${escapeHtml(key)}">
        <div class="thumb">
          <img src="${escapeHtml(video.thumbnail_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">
          <div class="downloaded-badge-slot"></div>
          <label class="card-select check-control" title="选择视频">
            <input type="checkbox"><span></span>
          </label>
          ${video.duration ? `<span class="duration">${escapeHtml(video.duration)}</span>` : ""}
        </div>
        <div class="card-body">
          <h3 class="video-title" title="${escapeHtml(video.title)}">${escapeHtml(video.title || "未命名视频")}</h3>
          <div class="metadata">
            <span class="author">${escapeHtml(video.author || video.source || "未知作者")}</span>
            ${video.views ? `<span>${escapeHtml(video.views)} 次播放</span>` : ""}
            <span class="key">${escapeHtml((video.viewkey || video.identity || "").slice(0, 8))}</span>
          </div>
        </div>
      </article>`;
      card = template.content.firstElementChild;
      card.dataset.contentSignature = contentSignature;
      const image = card.querySelector(".thumb img");
      const showImage = () => image.classList.add("is-loaded");
      if (image.complete) showImage();
      else image.addEventListener("load", showImage, {once: true});
      card.querySelector("input").addEventListener("change", event => {
      event.target.checked ? state.selected.add(card.dataset.key) : state.selected.delete(card.dataset.key);
      render();
      });
    }
    existing.delete(key);
    card.style.setProperty("--card-index", Math.min(index, 12));
    card.classList.toggle("selected", selected);
    card.querySelector("input").checked = selected;
    card.querySelector(".downloaded-badge-slot").innerHTML = state.downloadedKeys.has(key)
      ? '<span class="downloaded-badge"><i data-lucide="check"></i>已下载</span>'
      : "";
    grid.append(card);
  });
  existing.forEach(card => card.remove());
  if (animate && videos.length) {
    grid.classList.remove("animate-in");
    void grid.offsetWidth;
    grid.classList.add("animate-in");
    clearTimeout(state.animationTimer);
    state.animationTimer = setTimeout(() => grid.classList.remove("animate-in"), 900);
  }
  if (window.lucide) lucide.createIcons();
}

// 侧栏任务分类的统一筛选规则，工具栏全选也复用
function downloadsMatchingFilter(filter) {
  return state.downloads.filter(item => {
    switch (filter) {
      case "pending": return item.state !== "completed";
      case "downloading": return item.state === "downloading";
      case "queued": return item.state === "queued";
      case "failed": return item.state === "failed";
      case "completed": return item.state === "completed";
      default: return true;
    }
  });
}

function updateDownloadSidebar(counts) {
  const pending = counts.queued + counts.downloading + counts.failed;
  $("#filterPendingBadge").textContent = pending;
  $("#filterCompletedBadge").textContent = counts.completed;
  $("#filterFailedBadge").textContent = counts.failed;
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(item => {
    item.classList.toggle("active", item.dataset.filter === state.downloadFilter);
  });
}

function renderDownloads(totalSize = 0) {
  const visibleDownloads = downloadsMatchingFilter(state.downloadFilter);
  const counts = {queued: 0, downloading: 0, completed: 0, failed: 0, ...state.downloadCounts};
  $("#queuedCount").textContent = counts.queued;
  $("#downloadingCount").textContent = counts.downloading;
  $("#completedCount").textContent = counts.completed;
  $("#failedCount").textContent = counts.failed;
  $("#retryAllButton").disabled = counts.failed === 0;
  $("#totalDiskUsage").textContent = formatBytes(totalSize);
  $("#totalDiskUsage").dataset.bytes = totalSize;
  const active = counts.queued + counts.downloading;
  $("#activeDownloadBadge").textContent = active;
  $("#activeDownloadBadge").classList.toggle("hidden", active === 0);
  updateDownloadSidebar(counts);
  $("#downloadEmpty").classList.toggle("hidden", visibleDownloads.length > 0);
  $("#downloadList").classList.toggle("hidden", visibleDownloads.length === 0);
  $("#downloadEmpty h2").textContent = state.downloads.length ? "当前筛选没有任务" : "还没有下载任务";
  $("#downloadEmpty p").textContent = state.downloads.length ? "切换到其他状态查看下载任务。" : "在视频目录中勾选内容，然后点击“下载所选”。";
  const downloadKeys = new Set(state.downloads.map(item => item.viewkey));
  state.downloadSelected.forEach(key => { if (!downloadKeys.has(key)) state.downloadSelected.delete(key); });
  const selectAllDownloads = $("#selectAllDownloads");
  selectAllDownloads.checked = visibleDownloads.length > 0 && visibleDownloads.every(item => state.downloadSelected.has(item.viewkey));
  selectAllDownloads.indeterminate = visibleDownloads.some(item => state.downloadSelected.has(item.viewkey)) && !selectAllDownloads.checked;
  $("#selectedDownloadCount").textContent = state.downloadSelected.size;
  $("#removeDownloadsButton").disabled = state.downloadSelected.size === 0;
  $("#downloadSelectedButton").disabled = state.downloadSelected.size === 0;

  const signature = state.downloads.map(item => item.viewkey).join("|");
  if (signature !== state.downloadSignature) {
    state.downloadSignature = signature;
    $("#downloadList").innerHTML = state.downloads.map(item => `
    <div class="download-row ${escapeHtml(item.state)}" data-key="${escapeHtml(item.viewkey)}" data-action-state="">
      <label class="check-control download-row-select" title="选择任务"><input class="download-checkbox" data-key="${escapeHtml(item.viewkey)}" type="checkbox"><span></span></label>
      <div class="download-thumb"><img src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.classList.add('is-hidden')"></div>
      <div class="download-info">
        <strong class="download-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title || item.viewkey)}</strong>
        <span class="download-meta"><span class="meta-file"></span><span class="meta-sep" hidden>·</span><span class="meta-size"></span><span class="meta-sep" hidden>·</span><span class="meta-extra"></span></span>
        <div class="row-progress"><span style="width:${item.percent || 0}%"></span></div>
      </div>
      <div class="download-side">
        <span class="download-state"></span>
        <div class="row-action"></div>
      </div>
    </div>
    `).join("");
  }

  const rows = new Map([...document.querySelectorAll(".download-row")].map(row => [row.dataset.key, row]));
  state.downloads.forEach(item => {
    const row = rows.get(item.viewkey);
    if (!row) return;
    row.classList.toggle("hidden", !visibleDownloads.some(entry => entry.viewkey === item.viewkey));
    row.classList.toggle("failed", item.state === "failed");
    row.querySelector(".download-checkbox").checked = state.downloadSelected.has(item.viewkey);
    const metaFile = row.querySelector(".meta-file");
    metaFile.textContent = item.file_name || "等待生成文件";
    const metaSize = row.querySelector(".meta-size");
    const metaExtra = row.querySelector(".meta-extra");
    const separators = row.querySelectorAll(".meta-sep");
    const extraText = item.state === "downloading" && item.speed ? item.speed : item.state === "failed" && item.error ? item.error : "";
    metaSize.textContent = item.file_size ? formatBytes(item.file_size) : "";
    metaExtra.textContent = extraText;
    metaExtra.classList.toggle("is-error", item.state === "failed" && Boolean(item.error));
    let parts = [metaFile.textContent, metaSize.textContent, extraText].filter(Boolean).length;
    let shown = 0;
    separators.forEach(sep => { shown += 1; sep.hidden = shown >= parts; });
    const stateNode = row.querySelector(".download-state");
    stateNode.className = `download-state ${item.state}`;
    stateNode.textContent = item.state === "downloading" ? `${stateText(item)} ${item.percent || 0}%` : stateText(item);
    row.querySelector(".row-progress span").style.width = `${item.percent || 0}%`;
    if (row.dataset.actionState !== item.state) {
      row.dataset.actionState = item.state;
      row.querySelector(".row-action").innerHTML = `${item.state === "failed"
        ? `<button class="icon-button retry-download" data-key="${escapeHtml(item.viewkey)}" title="重新下载"><i data-lucide="rotate-ccw"></i></button>`
        : `<span class="row-action-placeholder"></span>`}
        <button class="icon-button delete-download" data-key="${escapeHtml(item.viewkey)}" title="删除任务"><i data-lucide="trash-2"></i></button>`;
    }
  });
  if (state.activeView === "downloads") $("#viewSubtitle").textContent = `${state.downloads.length} 个下载任务`;

  document.querySelectorAll(".retry-download:not([data-bound])").forEach(button => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      try {
        watchJob(await api("/api/downloads", {method: "POST", body: JSON.stringify({viewkeys: [button.dataset.key], workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4})}));
      } catch (error) { toast(error.message, true); }
    });
  });
  document.querySelectorAll(".download-checkbox:not([data-bound])").forEach(checkbox => {
    checkbox.dataset.bound = "true";
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.downloadSelected.add(checkbox.dataset.key) : state.downloadSelected.delete(checkbox.dataset.key);
      renderDownloads(totalSize);
    });
  });
  document.querySelectorAll(".delete-download:not([data-bound])").forEach(button => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      if (!confirm("从下载管理删除此任务？本地视频文件不会删除。")) return;
      try {
        await api("/api/downloads/remove", {method: "POST", body: JSON.stringify({viewkeys: [button.dataset.key]})});
        state.downloadSelected.delete(button.dataset.key);
        await loadDownloads();
      } catch (error) { toast(error.message, true); }
    });
  });
  if (window.lucide) lucide.createIcons();
}

async function loadCatalog() {
  const requestId = ++state.browseRequest;
  const refreshButton = $("#refreshButton");
  const grid = $("#videoGrid");
  refreshButton.disabled = true;
  refreshButton.classList.add("is-loading");
  grid.classList.add("is-refreshing");
  try {
    const data = await api(`/api/browse?category=${encodeURIComponent(state.category)}&page=${state.page}`);
    if (requestId !== state.browseRequest) return;
    state.videos = data.videos;
    state.statuses = data.download_status;
    state.downloadedKeys = new Set(data.downloaded_keys || []);
    state.pagination = data.pagination;
    $("#pageNumber").value = state.page;
    $("#previousPage").disabled = !data.pagination.has_previous;
    $("#nextPage").disabled = !data.pagination.has_next;
    const keys = new Set(state.videos.map(videoKey));
    state.selected.forEach(key => { if (!keys.has(key)) state.selected.delete(key); });
    render(true);
  } catch (error) {
    if (requestId !== state.browseRequest) return;
    toast(error.message, true);
  } finally {
    if (requestId === state.browseRequest) {
      refreshButton.disabled = false;
      refreshButton.classList.remove("is-loading");
      grid.classList.remove("is-refreshing");
    }
  }
}

async function loadDownloads() {
  try {
    const data = await api("/api/downloads");
    state.downloads = data.downloads;
    state.downloadCounts = data.counts;
    state.downloadedKeys = new Set(data.downloads.filter(item => item.state === "completed").map(item => item.viewkey));
    data.downloads.forEach(item => { state.statuses[item.viewkey] = item; });
    renderDownloads(data.total_size);
    if (state.activeView === "catalog") render();
  } catch (error) {
    toast(error.message, true);
  }
}

// 采集任务进行中时从本地库刷新，避免高频轮询打目标站触发人机验证
async function refreshCatalogFromStore() {
  try {
    const data = await api("/api/catalog");
    state.videos = data.videos;
    state.statuses = {...state.statuses, ...data.download_status};
    const keys = new Set(state.videos.map(videoKey));
    state.selected.forEach(key => { if (!keys.has(key)) state.selected.delete(key); });
    render();
  } catch (_) { /* 轮询失败不打断任务 */ }
}

function fillSettings(data) {
  state.settings = data;
  $("#settingDownloadDir").value = data.download_dir || "";
  $("#settingFolderMode").value = data.folder_mode || "flat";
  $("#settingPreferHd").checked = Boolean(data.prefer_hd);
  $("#settingWorkers").value = data.workers;
  $("#settingFragments").value = data.fragments;
  $("#settingUiRefresh").value = data.ui_refresh_seconds;
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    fillSettings(data);
    return data;
  } catch (error) { toast(error.message, true); return null; }
}

function restartDownloadPolling() {
  clearInterval(state.downloadPollTimer);
  const seconds = Math.max(1, Number(state.settings?.ui_refresh_seconds || 3));
  state.downloadPollTimer = setInterval(() => loadDownloads(), seconds * 1000);
}

// ---- 账号登录 ----

function updateAccountStatus(data) {
  const node = $("#accountStatus");
  if (!node) return;
  if (data.logged_in === true) node.textContent = `已登录（${data.cookie_count || 0} 条 Cookie）`;
  else if (data.has_cookies) node.textContent = `已保存 ${data.cookie_count} 条 Cookie，未验证是否有效，可点击「检测登录状态」`;
  else node.textContent = "未登录。VIP 高清视频需要登录后才能下载";
}

async function loadAccount() {
  try {
    updateAccountStatus(await api("/api/account"));
  } catch (_) { /* 状态获取失败不打断页面 */ }
}

async function refreshCaptcha() {
  try {
    const data = await api("/api/account/captcha");
    state.captchaSid = data.sid;
    $("#captchaImage").src = data.image;
  } catch (error) { toast(error.message, true); }
}

function bindAccountControls() {
  $("#captchaImage").addEventListener("click", refreshCaptcha);
  $("#loginButton").addEventListener("click", async () => {
    const payload = {
      sid: state.captchaSid,
      username: $("#accountUsername").value.trim(),
      password: $("#accountPassword").value,
      captcha: $("#accountCaptcha").value.trim(),
    };
    if (!payload.sid) { toast("请先等待验证码加载", true); return; }
    if (!payload.username || !payload.password || !payload.captcha) { toast("请填写用户名、密码和验证码", true); return; }
    try {
      const result = await api("/api/account/login", {method: "POST", body: JSON.stringify(payload)});
      toast(result.message, !result.ok);
      if (result.ok) {
        $("#accountPassword").value = "";
        $("#accountCaptcha").value = "";
      }
      loadAccount();
      refreshCaptcha();
    } catch (error) {
      toast(error.message, true);
      refreshCaptcha();
    }
  });
  $("#verifyLoginButton").addEventListener("click", async () => {
    try {
      const data = await api("/api/account/verify", {method: "POST"});
      updateAccountStatus(data);
      toast(data.message, data.logged_in !== true);
    } catch (error) { toast(error.message, true); }
  });
  $("#logoutButton").addEventListener("click", async () => {
    if (!confirm("退出登录并清除已保存的 Cookie？")) return;
    try {
      await api("/api/account/cookies", {method: "DELETE"});
      state.captchaSid = null;
      loadAccount();
      toast("已退出登录");
    } catch (error) { toast(error.message, true); }
  });
  $("#saveCookiesButton").addEventListener("click", async () => {
    const content = $("#settingCookies").value.trim();
    if (!content) { toast("请先粘贴 Cookie 内容", true); return; }
    try {
      const result = await api("/api/account/cookies", {method: "POST", body: JSON.stringify({content})});
      $("#settingCookies").value = "";
      toast(`已保存 ${result.cookie_count} 条 Cookie`);
      loadAccount();
    } catch (error) { toast(error.message, true); }
  });
}

async function downloadSelectedTasks() {
  const keys = [...state.downloadSelected];
  if (!keys.length) return;
  try {
    const settings = state.settings || await loadSettings() || {};
    const job = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({viewkeys: keys, workers: settings.workers || 2, fragments: settings.fragments || 4}),
    });
    state.downloadSelected.clear();
    watchJob(job);
  } catch (error) { toast(error.message, true); }
}

function collectSettings() {
  return {
    download_dir: $("#settingDownloadDir").value.trim(),
    folder_mode: $("#settingFolderMode").value,
    prefer_hd: $("#settingPreferHd").checked,
    workers: Number($("#settingWorkers").value),
    fragments: Number($("#settingFragments").value),
    ui_refresh_seconds: Number($("#settingUiRefresh").value),
  };
}

function setView(view, {sync = true, replace = false} = {}) {
    state.activeView = view;
  document.querySelectorAll(".side-nav-item").forEach(tab => tab.classList.toggle("active", tab.dataset.view === view));
  $("#catalogView").classList.toggle("hidden", view !== "catalog");
  $("#downloadsView").classList.toggle("hidden", view !== "downloads");
  $("#settingsView").classList.toggle("hidden", view !== "settings");
  $("#searchBox").classList.toggle("hidden", view !== "catalog");
  // 左侧分类区跟随视图切换：目录→视频分类，下载→任务分类，设置→设置分类
  $("#categorySection").classList.toggle("hidden", view !== "catalog");
  $("#downloadFilterSection").classList.toggle("hidden", view !== "downloads");
  $("#settingsNavSection").classList.toggle("hidden", view !== "settings");
  $("#viewTitle").textContent = view === "catalog" ? "视频目录" : view === "downloads" ? "下载管理" : "设置中心";
  $("#viewSubtitle").innerHTML = view === "catalog"
    ? `<span id="resultCount">${visibleVideos().length}</span> 个采集结果`
    : view === "downloads" ? `${state.downloads.length} 个下载任务` : "下载目录、性能和界面设置";
  if (sync) syncRoute(replace);
  if (window.lucide) lucide.createIcons();
}

function applyControlState() {
  document.querySelectorAll(".side-nav-item").forEach(tab => tab.classList.toggle("active", tab.dataset.view === state.activeView));
  document.querySelectorAll("#categorySection .category-item").forEach(item => item.classList.toggle("active", item.dataset.category === state.category));
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(item => item.classList.toggle("active", item.dataset.filter === state.downloadFilter));
  document.querySelectorAll("#settingsNavSection .category-item").forEach(item => item.classList.toggle("active", item.dataset.settingsSection === state.settingsSection));
  document.querySelectorAll(".mode-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.mode === state.mode));
  document.querySelectorAll(".source-pane").forEach(pane => pane.classList.toggle("hidden", pane.dataset.pane !== state.mode));
}

// 设置中心按左侧分类真正分区：同一时间只显示一个设置分区
function applySettingsSection() {
  ["settingsAccount", "settingsStorage", "settingsRefresh"].forEach(id => {
    document.getElementById(id)?.classList.toggle("hidden", id !== state.settingsSection);
  });
}

function setJob(job) {
  const panel = $("#jobPanel");
  panel.classList.remove("empty");
  $("#jobTitle").textContent = job.kind === "crawl" ? "采集任务" : "下载任务";
  $("#jobMessage").textContent = job.error || job.message;
  const percent = job.total ? Math.round(job.current * 100 / job.total) : (job.status === "completed" ? 100 : 35);
  $("#jobProgress").style.width = `${percent}%`;
  const crawlButton = $("#crawlButton");
  if (crawlButton) crawlButton.disabled = job.status === "queued" || job.status === "running";
}

function watchJob(job) {
  state.jobId = job.id;
  setJob(job);
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const current = await api(`/api/jobs/${state.jobId}`);
      setJob(current);
      if (current.kind === "crawl") {
        // 任务未结束时只读本地库，结束后由下方完成分支做一次真实翻页刷新
        if (!["completed", "failed"].includes(current.status)) await refreshCatalogFromStore();
      }
      else await loadDownloads();
      if (["completed", "failed"].includes(current.status)) {
        clearInterval(state.jobTimer);
        const crawlButton = $("#crawlButton");
        if (crawlButton) crawlButton.disabled = false;
        if (current.kind === "crawl") await loadCatalog();
        await loadDownloads();
        toast(current.error || current.message, current.status === "failed");
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      toast(error.message, true);
    }
  }, 1200);
}

document.addEventListener("DOMContentLoaded", () => {
  readRoute();
  $("#searchInput").value = saved.search || "";
  applyControlState();
  applySettingsSection();
  setView(state.activeView, {sync: false});
  syncRoute(true);
  if (window.lucide) lucide.createIcons();
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("viewkey-theme", next);
    if (window.lucide) lucide.createIcons();
  });

  document.querySelectorAll(".side-nav-item").forEach(tab => tab.addEventListener("click", () => {
    setView(tab.dataset.view);
    if (tab.dataset.view === "catalog" && state.videos.length === 0) loadCatalog();
    if (tab.dataset.view === "downloads") loadDownloads();
    if (tab.dataset.view === "settings") { loadSettings(); loadAccount(); if (!state.captchaSid) refreshCaptcha(); }
  }));
  bindAccountControls();
  loadAccount();
  document.querySelectorAll("#categorySection .category-item").forEach(item => item.addEventListener("click", () => {
    state.mode = "category";
    state.category = item.dataset.category;
    state.page = 1;
    state.selected.clear();
    applyControlState();
    setView("catalog");
    loadCatalog();
  }));

  $("#searchInput").addEventListener("input", () => { render(); setView("catalog", {replace: true}); });
  $("#refreshButton").addEventListener("click", async () => { await loadCatalog(); await loadDownloads(); });
  $("#previousPage").addEventListener("click", async () => {
    if (state.page <= 1) return;
    state.page -= 1;
    state.selected.clear();
    syncRoute();
    await loadCatalog();
  });
  $("#nextPage").addEventListener("click", async () => {
    if (!state.pagination.has_next) return;
    state.page += 1;
    state.selected.clear();
    syncRoute();
    await loadCatalog();
  });
  $("#pageJumpForm").addEventListener("submit", async event => {
    event.preventDefault();
    state.page = Math.min(10000, Math.max(1, Number($("#pageNumber").value) || 1));
    state.selected.clear();
    syncRoute();
    await loadCatalog();
  });
  $("#settingsForm").addEventListener("submit", async event => {
    event.preventDefault();
    const payload = collectSettings();
    try {
      const data = await api("/api/settings", {method: "PUT", body: JSON.stringify(payload)});
      fillSettings(data);
      restartDownloadPolling();
      $("#settingsSaveMessage").textContent = "已保存";
      setTimeout(() => { $("#settingsSaveMessage").textContent = ""; }, 3000);
    } catch (error) { toast(error.message, true); }
  });
  $("#selectAllDownloads").addEventListener("change", event => {
    downloadsMatchingFilter(state.downloadFilter).forEach(item => event.target.checked
      ? state.downloadSelected.add(item.viewkey)
      : state.downloadSelected.delete(item.viewkey));
    renderDownloads(Number($("#totalDiskUsage").dataset.bytes || 0));
  });
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(button => button.addEventListener("click", () => {
    state.downloadFilter = button.dataset.filter;
    $("#selectAllDownloads").checked = false;
    applyControlState();
    renderDownloads(Number($("#totalDiskUsage").dataset.bytes || 0));
    window.scrollTo({top: 0});
  }));
  document.querySelectorAll("#settingsNavSection .category-item").forEach(button => button.addEventListener("click", () => {
    state.settingsSection = button.dataset.settingsSection;
    persistUiState();
    applyControlState();
    applySettingsSection();
  }));
  $("#removeDownloadsButton").addEventListener("click", async () => {
    if (!state.downloadSelected.size) return;
    if (!confirm(`从下载管理移除 ${state.downloadSelected.size} 个任务？本地视频文件不会删除。`)) return;
    try {
      await api("/api/downloads/remove", {method: "POST", body: JSON.stringify({viewkeys: [...state.downloadSelected]})});
      state.downloadSelected.clear();
      await loadDownloads();
      toast("已删除所选下载任务");
    } catch (error) { toast(error.message, true); }
  });
  $("#downloadSelectedButton").addEventListener("click", downloadSelectedTasks);
  $("#downloadMissingButton").addEventListener("click", async () => {
    const completed = new Set(state.downloads.filter(item => item.state === "completed").map(item => item.viewkey));
    try {
      const catalog = await api("/api/catalog");
      const missing = catalog.videos.filter(item => !completed.has(videoKey(item))).map(videoKey);
      if (!missing.length) return toast("全部视频都已下载完成");
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: missing, workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      watchJob(job);
    } catch (error) { toast(error.message, true); }
  });
  $("#retryAllButton").addEventListener("click", async () => {
    const failed = state.downloads.filter(item => item.state === "failed").map(item => item.viewkey);
    if (!failed.length) return;
    try {
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: failed, workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      watchJob(job);
    } catch (error) { toast(error.message, true); }
  });

  $("#selectAll").addEventListener("change", event => {
    visibleVideos().forEach(video => event.target.checked ? state.selected.add(videoKey(video)) : state.selected.delete(videoKey(video)));
    render();
  });

  $("#downloadButton").addEventListener("click", async () => {
    try {
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: [...state.selected], workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      watchJob(job);
      state.selected.clear();
      render();
      toast(`已加入 ${job.total || 0} 个下载任务，可继续选择视频`);
    } catch (error) { toast(error.message, true); }
  });

  $("#removeButton").addEventListener("click", async () => {
    if (!confirm(`从目录移出 ${state.selected.size} 个视频？已下载文件不会删除。`)) return;
    try {
      await api("/api/catalog/remove", {method: "POST", body: JSON.stringify({viewkeys: [...state.selected]})});
      state.selected.clear();
      await loadCatalog();
    } catch (error) { toast(error.message, true); }
  });

  // Keep the interface interactive while a slow listing page is loading.
  void (async () => {
    try {
      const config = await api("/api/config");
      $("#siteHost").textContent = new URL(config.base_url).host;
    } catch (_) {}
    await loadSettings();
    restartDownloadPolling();
    if (state.activeView === "downloads") await loadDownloads();
    else if (state.activeView === "catalog") { await loadCatalog(); await loadDownloads(); }
  })();
});

window.addEventListener("popstate", () => {
  readRoute();
  applyControlState();
  applySettingsSection();
  state.selected.clear();
  render();
  setView(state.activeView, {sync: false});
  persistUiState();
  if (state.activeView === "catalog") loadCatalog();
  if (state.activeView === "downloads") loadDownloads();
  if (state.activeView === "settings") loadSettings();
});
