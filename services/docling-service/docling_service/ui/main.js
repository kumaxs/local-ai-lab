const UI_STATE = {
  token: "",
  authGeneration: 0,
  jobs: [],
  jobsQuery: null,
  jobsPageIndex: 0,
  jobsCursorHistory: [null],
  jobsNextCursor: null,
  jobsLoadingPage: false,
  jobsRequestSequence: 0,
  refreshTimer: null,
  config: null,
  configRequestSequence: 0,
  lastHealthFetch: null,
  lastStorageFetch: null,
  applyingConfig: false,
  refreshSequence: 0,
  uploadXhr: null,
};

const stateFilter = document.getElementById("stateFilter");
const serviceVersion = document.getElementById("serviceVersion");
const serviceQueued = document.getElementById("serviceQueued");
const serviceProfile = document.getElementById("serviceProfile");
const serviceHealth = document.getElementById("serviceHealth");
const pendingCount = document.getElementById("pendingCount");
const inputBytes = document.getElementById("inputBytes");
const outputBytes = document.getElementById("outputBytes");
const reservedBytes = document.getElementById("reservedBytes");
const totalBytes = document.getElementById("totalBytes");
const filesystemFree = document.getElementById("filesystemFree");
const maxDataBytes = document.getElementById("maxDataBytes");
const maxOutputBytes = document.getElementById("maxOutputBytes");
const minFreeBytes = document.getElementById("minFreeBytes");
const maxPendingJobs = document.getElementById("maxPendingJobs");
const cleanupInterval = document.getElementById("cleanupInterval");
const countQueued = document.getElementById("countQueued");
const countRunning = document.getElementById("countRunning");
const countTerminal = document.getElementById("countTerminal");
const countTotal = document.getElementById("countTotal");
const jobsContainer = document.getElementById("jobsContainer");
const jobsEmpty = document.getElementById("jobsEmpty");
const jobsPagination = document.getElementById("jobsPagination");
const jobsPageSummary = document.getElementById("jobsPageSummary");
const previousJobsPageButton = document.getElementById("previousJobsPage");
const nextJobsPageButton = document.getElementById("nextJobsPage");
const uploadDropZone = document.getElementById("uploadDropZone");
const fileInput = document.getElementById("fileInput");
const selectFileButton = document.getElementById("selectFileButton");
const uploadState = document.getElementById("uploadState");
const uploadProgress = document.getElementById("uploadProgress");
const refreshDashboard = document.getElementById("refreshDashboard");
const tokenInput = document.getElementById("tokenInput");
const tokenSaveButton = document.getElementById("tokenSaveButton");
const clearTokenButton = document.getElementById("clearTokenButton");
const toggleConfig = document.getElementById("toggleConfig");
const configBody = document.getElementById("configBody");
const configRows = document.getElementById("configRows");
const readonlyRows = document.getElementById("readonlyRows");
const configRevision = document.getElementById("configRevision");
const applyConfig = document.getElementById("applyConfig");

const CONFIG_LABELS = {
  input_ttl_seconds: "输入TTL",
  success_output_ttl_seconds: "成功产物TTL",
  failed_output_ttl_seconds: "失败产物TTL",
  job_ttl_seconds: "任务TTL",
  staging_ttl_seconds: "暂存TTL",
  temp_ttl_seconds: "临时TTL",
  cleanup_interval_seconds: "清理间隔",
  idempotency_ttl_seconds: "IdempotencyTTL",
  download_lease_seconds: "下载租约TTL",
};

const CONFIG_ORDER = [
  "input_ttl_seconds",
  "success_output_ttl_seconds",
  "failed_output_ttl_seconds",
  "job_ttl_seconds",
  "staging_ttl_seconds",
  "temp_ttl_seconds",
  "cleanup_interval_seconds",
  "idempotency_ttl_seconds",
  "download_lease_seconds",
];
const TERMINAL_STATES = new Set(["succeeded", "failed", "interrupted"]);
const AUTHENTICATED_BLOB_LIMIT_BYTES = 256 * 1024 * 1024;
const JOB_PAGE_SIZE = 100;
const DEADLINE_FIELDS = [
  ["input_expires_at", "输入截止"],
  ["output_expires_at", "产物截止"],
  ["tombstone_expires_at", "记录截止"],
];

function safeText(node, value) {
  if (node) {
    node.textContent = value === null || value === undefined ? "" : String(value);
  }
}

function authHeaders() {
  const headers = {};
  if (UI_STATE.token) {
    headers.Authorization = `Bearer ${UI_STATE.token}`;
  }
  return headers;
}

function setToken(value) {
  UI_STATE.token = String(value || "").trim();
  invalidateAuthContext();
}

function hasToken() {
  return Boolean(UI_STATE.token);
}

function authContextIsCurrent(generation) {
  return generation === UI_STATE.authGeneration;
}

function setStatusMessage(node, text, isError = false) {
  node.textContent = text;
  node.classList.toggle("error", isError);
  node.classList.toggle("ok", !isError);
}

function bytesToHuman(bytes) {
  const numeric = Number(bytes || 0);
  if (!Number.isFinite(numeric)) {
    return "-";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = numeric;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(1)} ${units[unitIndex]}`;
}

function secondsToHuman(seconds) {
  const numeric = Number(seconds || 0);
  const minute = 60;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (numeric >= day) {
    return `${Math.floor(numeric / day)} 天 ${Math.floor((numeric % day) / hour)} 小时`;
  }
  if (numeric >= hour) {
    return `${Math.floor(numeric / hour)} 小时 ${Math.floor((numeric % hour) / minute)} 分钟`;
  }
  if (numeric >= minute) {
    return `${Math.floor(numeric / minute)} 分钟`;
  }
  return `${Math.floor(numeric)} 秒`;
}

function parseNumericText(value) {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return null;
  }
  if (!/^-?\d+$/.test(normalized)) {
    throw new Error("请输入整数");
  }
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error("请输入范围内的整数");
  }
  return parsed;
}

function withAuth(url, options = {}) {
  const headers = Object.assign({}, options.headers || {}, authHeaders());
  return fetch(url, Object.assign({}, options, { headers }));
}

async function requestJson(url, options = {}) {
  const response = await withAuth(url, options);
  const contentType = response.headers.get("content-type") || "";
  // FastAPI errors use application/problem+json in some deployments.
  const payload = contentType.includes("json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const error = new Error(
      typeof payload === "string"
        ? payload
        : payload?.detail || "请求失败"
    );
    error.status = response.status;
    throw error;
  }
  return payload;
}

function normalizeNextCursor(value) {
  if (typeof value !== "string") {
    return null;
  }
  const cursor = value.trim();
  return cursor ? cursor : null;
}

function jobsQueryValue() {
  return String(stateFilter.value || "");
}

function jobsUrl(cursor = null, state = jobsQueryValue()) {
  const params = new URLSearchParams();
  params.set("limit", String(JOB_PAGE_SIZE));
  if (state) {
    params.set("state", state);
  }
  if (cursor) {
    params.set("cursor", cursor);
  }
  return `/v1/jobs?${params.toString()}`;
}

function validJobs(items) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items.filter(
    (item) => item && typeof item === "object" && String(item.job_id || ""),
  );
}

function renderJobsPagination() {
  if (
    !jobsPagination ||
    !jobsPageSummary ||
    !previousJobsPageButton ||
    !nextJobsPageButton
  ) {
    return;
  }
  const loaded = UI_STATE.jobs.length;
  const hasPrevious = UI_STATE.jobsPageIndex > 0;
  const hasNext = Boolean(UI_STATE.jobsNextCursor);
  jobsPagination.hidden = loaded === 0 && !hasPrevious && !hasNext;
  previousJobsPageButton.disabled = UI_STATE.jobsLoadingPage || !hasPrevious;
  nextJobsPageButton.disabled = UI_STATE.jobsLoadingPage || !hasNext;
  if (!loaded && !hasPrevious && !hasNext) {
    jobsPageSummary.textContent = "当前筛选没有任务";
  } else {
    jobsPageSummary.textContent = (
      `第 ${UI_STATE.jobsPageIndex + 1} 页，当前页 ${loaded} 项` +
      "（状态计数仅统计当前页，刷新会重新读取本页）"
    );
  }
}

function resetJobsPagination() {
  UI_STATE.jobs = [];
  UI_STATE.jobsQuery = jobsQueryValue();
  UI_STATE.jobsPageIndex = 0;
  UI_STATE.jobsCursorHistory = [null];
  UI_STATE.jobsNextCursor = null;
  UI_STATE.jobsLoadingPage = false;
  renderJobs();
  applyCounts();
}

function invalidateAuthContext() {
  UI_STATE.authGeneration += 1;
  UI_STATE.refreshSequence += 1;
  UI_STATE.jobsRequestSequence += 1;
  UI_STATE.configRequestSequence += 1;
  UI_STATE.jobsLoadingPage = false;
  UI_STATE.applyingConfig = false;

  const activeUpload = UI_STATE.uploadXhr;
  UI_STATE.uploadXhr = null;
  if (activeUpload) {
    activeUpload.abort();
    setStatusMessage(uploadState, "凭证已更改，上传已取消", true);
  }

  UI_STATE.config = null;
  UI_STATE.lastHealthFetch = null;
  UI_STATE.lastStorageFetch = null;
  resetJobsPagination();
  renderSystemCards();
  renderStorageCards();
  renderConfigRows();
  setConfigEditingEnabled(false);
}

function setConfigEditingEnabled(enabled) {
  applyConfig.disabled = !enabled;
  toggleConfig.disabled = !enabled;
}

function buildAuthNotice() {
  if (!hasToken()) {
    serviceHealth.textContent = "未设置 Token：未启用鉴权时可直接使用";
  }
}

function alignServerTime(job) {
  if (typeof job.__receivedAt !== "number") {
    job.__receivedAt = Date.now();
  }
  if (!job.server_time) {
    return Date.now();
  }
  const serverAt = Date.parse(job.server_time);
  if (!Number.isFinite(serverAt)) {
    return Date.now();
  }
  const offset = job.__receivedAt - serverAt;
  return Date.now() - offset;
}

function remainingSeconds(job, deadline) {
  const deadlineAt = Date.parse(deadline || "");
  if (!Number.isFinite(deadlineAt)) {
    return null;
  }
  const serverNow = alignServerTime(job);
  return Math.max(0, Math.floor((deadlineAt - serverNow) / 1000));
}

function renderExpiration(job) {
  const container = document.createElement("div");
  container.className = "deadline-list";
  DEADLINE_FIELDS.forEach(([key, label]) => {
    const line = document.createElement("div");
    const labelNode = document.createElement("span");
    const valueNode = document.createElement("time");
    labelNode.className = "deadline-label";
    valueNode.className = "deadline-value";
    labelNode.textContent = `${label}：`;
    valueNode.dataset.jobId = String(job.job_id || "");
    valueNode.dataset.deadlineKey = key;
    const expiry = job[key];
    if (!expiry) {
      valueNode.textContent = "未设置";
    } else {
      valueNode.dateTime = String(expiry);
      const parsed = Date.parse(expiry);
      const remaining = remainingSeconds(job, expiry);
      const friendly = Number.isFinite(parsed)
        ? new Date(parsed).toLocaleString("zh-CN", { hour12: false })
        : String(expiry);
      valueNode.textContent = `${friendly}（${remaining === null ? "未设置" : remaining <= 0 ? "已过期" : `剩余 ${remaining} 秒`}）`;
    }
    line.appendChild(labelNode);
    line.appendChild(valueNode);
    container.appendChild(line);
  });
  return container;
}

function updateCountdowns() {
  if (!document || !document.querySelectorAll) {
    return;
  }
  const jobs = new Map(UI_STATE.jobs.map((job) => [String(job.job_id), job]));
  document.querySelectorAll(".deadline-value[data-deadline-key]").forEach((node) => {
    const job = jobs.get(node.dataset.jobId);
    const expiry = job && job[node.dataset.deadlineKey];
    if (!job || !expiry) {
      node.textContent = "未设置";
      return;
    }
    const parsed = Date.parse(expiry);
    const remaining = remainingSeconds(job, expiry);
    const friendly = Number.isFinite(parsed)
      ? new Date(parsed).toLocaleString("zh-CN", { hour12: false })
      : String(expiry);
    node.textContent = `${friendly}（${remaining === null ? "未设置" : remaining <= 0 ? "已过期" : `剩余 ${remaining} 秒`}）`;
  });
}

function queuePositionText(position) {
  if (position === undefined || position === null) {
    return "—";
  }
  return String(position);
}

function renderJobRow(job) {
  const row = document.createElement("tr");
  const stateCell = document.createElement("td");
  const idCell = document.createElement("td");
  const fileCell = document.createElement("td");
  const progressCell = document.createElement("td");
  const queueCell = document.createElement("td");
  const expireCell = document.createElement("td");
  const actionsCell = document.createElement("td");

  const detailCell = document.createElement("td");
  const outputsId = `outputs-${job.job_id}`;
  const detailsArea = document.createElement("div");

  row.appendChild(stateCell);
  row.appendChild(idCell);
  row.appendChild(fileCell);
  row.appendChild(progressCell);
  row.appendChild(queueCell);
  row.appendChild(expireCell);
  row.appendChild(actionsCell);
  row.appendChild(detailCell);

  safeText(stateCell, {
    queued: "排队中",
    running: "处理中",
    succeeded: "已完成",
    failed: "失败",
    interrupted: "已中断",
  }[job.state] || job.state);
  const artifactLabel = document.createElement("div");
  artifactLabel.className = "muted";
  safeText(artifactLabel, `产物：${{
    pending: "待生成",
    available: "可下载",
    expired: "已过期",
    deleted: "已删除",
  }[job.artifact_state] || job.artifact_state || "未知"}`);
  stateCell.appendChild(artifactLabel);
  safeText(idCell, job.job_id);
  safeText(fileCell, job.original_name);
  const percent = job.progress_percent;
  const terminal = TERMINAL_STATES.has(job.state);
  const stage = job.progress_stage || (
    job.state === "queued" ? "等待处理" : terminal ? job.state : "处理中"
  );
  const progressLabel = document.createElement("div");
  safeText(
    progressLabel,
    `${stage}（${percent === null || percent === undefined ? (terminal ? "已结束" : "进行中") : `${percent}%`}）`
  );
  const progress = document.createElement("progress");
  progress.max = 100;
  progress.setAttribute("aria-label", "任务处理进度");
  if (percent === null || percent === undefined) {
    progress.removeAttribute("value");
    progress.classList.add("progress-indeterminate");
  } else {
    progress.value = Math.max(0, Math.min(100, Number(percent)));
  }
  progressCell.appendChild(progressLabel);
  progressCell.appendChild(progress);

  const queueLabel = document.createElement("div");
  safeText(queueLabel, `队列位置：${queuePositionText(job.queue_position)}`);
  const messageLabel = document.createElement("div");
  messageLabel.className = "muted";
  safeText(messageLabel, job.progress_message || "");
  queueCell.appendChild(queueLabel);
  queueCell.appendChild(messageLabel);
  expireCell.appendChild(renderExpiration(job));

  const actionRow = document.createElement("div");
  actionRow.className = "actions";

  const detailButton = document.createElement("button");
  detailButton.type = "button";
  const artifactsAvailable = terminal && job.artifact_state === "available";
  safeText(detailButton, artifactsAvailable ? "查看输出" : terminal ? "产物不可用" : "产物待完成");
  detailButton.disabled = !artifactsAvailable;
  detailButton.title = artifactsAvailable
    ? "查看已发布产物"
    : terminal ? "产物已过期或删除" : "任务完成后才能查看产物";
  detailButton.addEventListener("click", () => {
    toggleJobDetails(job.job_id, outputsId, detailCell, detailButton);
  });

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  safeText(deleteButton, "删除任务");
  deleteButton.disabled = !terminal;
  deleteButton.title = terminal ? "删除任务及其输入、产物" : "任务完成后才能删除";
  deleteButton.addEventListener("click", async () => {
    if (!window.confirm(`确认删除 ${job.job_id} ？`)) {
      return;
    }
    await deleteJob(job.job_id);
  });

  const downloadArchiveButton = document.createElement("button");
  downloadArchiveButton.type = "button";
  safeText(downloadArchiveButton, "下载 ZIP");
  downloadArchiveButton.disabled = !artifactsAvailable;
  downloadArchiveButton.title = artifactsAvailable
    ? "下载任务归档"
    : terminal ? "产物已过期或删除" : "任务完成后才能下载";
  downloadArchiveButton.addEventListener("click", async () => {
    if (!artifactsAvailable) {
      return;
    }
    try {
      await downloadBlob(
        `/v1/jobs/${encodeURIComponent(job.job_id)}/archive`,
        `${job.job_id}.zip`,
        job.output_size_bytes
      );
    } catch (error) {
      setStatusMessage(uploadState, `下载失败：${String(error.message || error)}`, true);
    }
  });

  actionRow.appendChild(detailButton);
  actionRow.appendChild(downloadArchiveButton);
  actionRow.appendChild(deleteButton);
  actionsCell.appendChild(actionRow);

  detailButton.setAttribute("aria-expanded", "false");
  detailButton.setAttribute("aria-controls", outputsId);
  if (!detailCell.firstChild) {
    detailCell.appendChild(detailsArea);
  }
  safeText(detailsArea, "");
  detailsArea.id = outputsId;
  detailsArea.hidden = true;
  return row;
}

async function toggleJobDetails(jobId, outputsId, detailCell, button) {
  const expanded = button.getAttribute("aria-expanded") === "true";
  if (expanded) {
    const container = detailCell.querySelector(`#${outputsId}`);
    if (container) {
      container.hidden = true;
    }
    button.setAttribute("aria-expanded", "false");
    safeText(button, "查看输出");
    return;
  }
  const container = detailCell.querySelector(`#${outputsId}`) || document.createElement("div");
  container.id = outputsId;
  container.hidden = false;
  if (!container.parentNode) {
    detailCell.appendChild(container);
  }
  const loaded = await loadOutputs(jobId, container);
  if (!loaded) {
    button.setAttribute("aria-expanded", "false");
    safeText(button, "重试查看");
    return;
  }
  button.setAttribute("aria-expanded", "true");
  safeText(button, "隐藏输出");
}

async function loadOutputs(jobId, outputContainer) {
  const authGeneration = UI_STATE.authGeneration;
  const table = document.createElement("table");
  const header = document.createElement("tr");
  ["文件", "大小", "操作"].forEach((title) => {
    const th = document.createElement("th");
    safeText(th, title);
    header.appendChild(th);
  });
  table.appendChild(header);

  safeText(outputContainer, "加载中...");
  try {
    const outputPayload = await requestJson(`/v1/jobs/${encodeURIComponent(jobId)}/outputs`);
    if (!authContextIsCurrent(authGeneration)) {
      return false;
    }
    outputContainer.replaceChildren();
    if (!Array.isArray(outputPayload.files) || !outputPayload.files.length) {
      safeText(outputContainer, "无输出");
      return true;
    }
    outputPayload.files.forEach((entry) => {
      const row = document.createElement("tr");
      const pathCell = document.createElement("td");
      const sizeCell = document.createElement("td");
      const actionCell = document.createElement("td");
      safeText(pathCell, entry.path);
      safeText(sizeCell, bytesToHuman(entry.size_bytes));
      const action = document.createElement("button");
      action.type = "button";
      safeText(action, "下载");
      action.addEventListener("click", () => {
        const encodedPath = String(entry.path || "")
          .split("/")
          .map((part) => encodeURIComponent(part))
          .join("/");
        downloadBlob(
          `/v1/jobs/${encodeURIComponent(jobId)}/files/${encodedPath}`,
          entry.path,
          entry.size_bytes
        ).catch((error) => {
          setStatusMessage(uploadState, `下载失败：${String(error.message || error)}`, true);
        });
      });
      actionCell.appendChild(action);
      row.appendChild(pathCell);
      row.appendChild(sizeCell);
      row.appendChild(actionCell);
      table.appendChild(row);
    });
    outputContainer.appendChild(table);
    return true;
  } catch (error) {
    if (!authContextIsCurrent(authGeneration)) {
      return false;
    }
    if (error.status === 401) {
      invalidateAuthContext();
    }
    setStatusMessage(outputContainer, `获取输出失败：${String(error.message || error)}`, true);
    return false;
  }
}

async function downloadBlob(url, filename, knownSizeBytes = null) {
  const authGeneration = UI_STATE.authGeneration;
  // Without bearer auth, a normal anchor lets the browser stream directly to
  // disk. Bearer-protected downloads require fetch; cap that Blob fallback so
  // a multi-gigabyte artifact cannot exhaust the tab's memory.
  if (!hasToken()) {
    const directLink = document.createElement("a");
    directLink.href = url;
    directLink.download = filename;
    directLink.rel = "noopener";
    document.body.appendChild(directLink);
    directLink.click();
    directLink.remove();
    return;
  }
  const knownSize = Number(knownSizeBytes);
  if (Number.isFinite(knownSize) && knownSize > AUTHENTICATED_BLOB_LIMIT_BYTES) {
    throw new Error("该产物超过页面安全下载上限（256 MiB），请使用带 Bearer Token 的 API 客户端流式下载");
  }
  const response = await withAuth(url, {
    method: "GET",
  });
  if (!authContextIsCurrent(authGeneration)) {
    if (response.body && typeof response.body.cancel === "function") {
      await response.body.cancel();
    }
    return;
  }
  if (!response.ok) {
    if (response.status === 401) {
      invalidateAuthContext();
    }
    throw new Error(`下载失败：${response.status}`);
  }
  const responseSize = Number(response.headers.get("content-length"));
  if (Number.isFinite(responseSize) && responseSize > AUTHENTICATED_BLOB_LIMIT_BYTES) {
    if (response.body && typeof response.body.cancel === "function") {
      await response.body.cancel();
    }
    throw new Error("响应超过页面安全下载上限（256 MiB），请使用 API 客户端流式下载");
  }
  const blob = await response.blob();
  if (!authContextIsCurrent(authGeneration)) {
    return;
  }
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

async function deleteJob(jobId) {
  const authGeneration = UI_STATE.authGeneration;
  try {
    await requestJson(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    if (!authContextIsCurrent(authGeneration)) {
      return;
    }
    await refreshDashboardData({ cancelNavigation: true });
  } catch (error) {
    if (!authContextIsCurrent(authGeneration)) {
      return;
    }
    if (error.status === 401) {
      invalidateAuthContext();
    }
    setStatusMessage(uploadState, `删除失败：${String(error.message || error)}`, true);
  }
}

function renderJobs() {
  if (!jobsContainer) {
    return;
  }
  jobsContainer.replaceChildren();
  if (!UI_STATE.jobs.length) {
    jobsEmpty.hidden = false;
    renderJobsPagination();
    return;
  }
  jobsEmpty.hidden = true;
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["状态", "任务 ID", "文件名", "进度", "队列/说明", "过期", "操作", "输出"].forEach(
    (title) => {
      const th = document.createElement("th");
      safeText(th, title);
      head.appendChild(th);
    }
  );
  table.appendChild(head);

  UI_STATE.jobs.forEach((job) => {
    const row = renderJobRow(job);
    if (row) {
      table.appendChild(row);
    }
  });
  jobsContainer.appendChild(table);
  updateCountdowns();
  renderJobsPagination();
}

function applyCounts() {
  let queued = 0;
  let running = 0;
  let terminal = 0;
  UI_STATE.jobs.forEach((job) => {
    if (job.state === "queued") {
      queued += 1;
    } else if (job.state === "running") {
      running += 1;
    } else {
      terminal += 1;
    }
  });
  countQueued.textContent = String(queued);
  countRunning.textContent = String(running);
  countTerminal.textContent = String(terminal);
  countTotal.textContent = String(UI_STATE.jobs.length);
}

function renderSystemCards() {
  const response = UI_STATE.lastHealthFetch;
  if (!response) {
    serviceVersion.textContent = "-";
    serviceQueued.textContent = "-";
    serviceProfile.textContent = "-";
    serviceHealth.textContent = "等待连接";
    return;
  }
  serviceVersion.textContent = response.release_version || "-";
  serviceQueued.textContent = String(response.max_concurrent_jobs ?? "-");
  serviceProfile.textContent = response.profile || "-";
  serviceHealth.textContent = response.ok === false ? "服务异常" : "API 已连接";
}

function renderStorageCards() {
  const payload = UI_STATE.lastStorageFetch;
  if (!payload) {
    [
      pendingCount,
      inputBytes,
      outputBytes,
      reservedBytes,
      totalBytes,
      filesystemFree,
      maxDataBytes,
      maxOutputBytes,
      minFreeBytes,
      maxPendingJobs,
      cleanupInterval,
    ].forEach((node) => safeText(node, "-"));
    return;
  }
  pendingCount.textContent = String(payload.usage?.pending_jobs || 0);
  inputBytes.textContent = bytesToHuman(payload.usage?.input_bytes || 0);
  outputBytes.textContent = bytesToHuman(payload.usage?.output_bytes || 0);
  reservedBytes.textContent = bytesToHuman(payload.usage?.reserved_output_bytes || 0);
  totalBytes.textContent = bytesToHuman(payload.usage?.total_managed_bytes || 0);
  filesystemFree.textContent = bytesToHuman(payload.usage?.filesystem_free_bytes || 0);
  maxDataBytes.textContent = bytesToHuman(payload.limits?.max_data_bytes || 0);
  maxOutputBytes.textContent = bytesToHuman(payload.limits?.max_output_bytes || 0);
  minFreeBytes.textContent = bytesToHuman(payload.limits?.min_free_bytes || 0);
  maxPendingJobs.textContent = String(payload.limits?.max_pending_jobs ?? "-");
  cleanupInterval.textContent = String(payload.cleanup_interval_seconds || "-");
}

function renderConfigRows() {
  if (!UI_STATE.config) {
    configRows.replaceChildren();
    readonlyRows.replaceChildren();
    setStatusMessage(configRevision, "配置暂不可用", true);
    return;
  }
  const { revision, editable, readonly } = UI_STATE.config;
  safeText(configRevision, `revision=${revision}`);

  configRows.replaceChildren();
  const configEntries = editable && typeof editable === "object" ? editable : {};
  CONFIG_ORDER.forEach((key) => {
    const metadata = configEntries[key];
    if (!metadata) {
      return;
    }
    const value = metadata.value;
    const envValue = metadata.environment_value;
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const envCell = document.createElement("td");
    const valueCell = document.createElement("td");
    const displayCell = document.createElement("td");
    const resetCell = document.createElement("td");

    safeText(nameCell, metadata.label || CONFIG_LABELS[key] || key);
    safeText(envCell, String(envValue));
    const input = document.createElement("input");
    input.type = "number";
    input.min = String(metadata.minimum || 1);
    input.max = String(metadata.maximum || "");
    input.step = "1";
    input.value = String(value);
    input.setAttribute("aria-label", `${metadata.label || CONFIG_LABELS[key] || key}配置值`);
    input.dataset.key = key;
    input.addEventListener("input", () => {
      delete input.dataset.reset;
    });
    valueCell.appendChild(input);

    safeText(displayCell, `${secondsToHuman(value)}（${metadata.unit || "seconds"}）`);
    const resetButton = document.createElement("button");
    resetButton.type = "button";
    safeText(resetButton, "重置");
    resetButton.setAttribute("type", "button");
    resetButton.addEventListener("click", () => {
      input.value = String(envValue);
      input.dataset.reset = "true";
    });
    resetCell.appendChild(resetButton);
    row.appendChild(nameCell);
    row.appendChild(envCell);
    row.appendChild(valueCell);
    row.appendChild(displayCell);
    row.appendChild(resetCell);
    configRows.appendChild(row);
  });

  readonlyRows.replaceChildren();
  if (readonly && typeof readonly === "object") {
    Object.keys(readonly).forEach((key) => {
      const row = document.createElement("tr");
      const keyCell = document.createElement("td");
      const valueCell = document.createElement("td");
      safeText(keyCell, key);
      const entry = readonly[key];
      const value = entry && typeof entry === "object" ? entry.value : entry;
      const lowered = key.toLowerCase();
      const sensitive = ["token", "secret", "password", "credential", "authorization", "api_key"]
        .some((marker) => lowered.includes(marker));
      safeText(
        valueCell,
        sensitive
          ? typeof value === "boolean" ? (value ? "已配置" : "未配置") : "不可见"
          : value === null || value === undefined ? "—" : String(value),
      );
      if (entry && typeof entry === "object" && entry.reason) {
        const reason = document.createElement("div");
        reason.className = "config-row__desc";
        safeText(reason, entry.reason);
        valueCell.appendChild(reason);
      }
      row.appendChild(keyCell);
      row.appendChild(valueCell);
      readonlyRows.appendChild(row);
    });
  }
}

function buildConfigChanges() {
  const changes = {};
  if (!UI_STATE.config) {
    return changes;
  }
  const editable = UI_STATE.config.editable || {};
  Array.from(configRows.querySelectorAll("input")).forEach((input) => {
    const key = input.dataset.key;
    const value = parseNumericText(input.value);
    const currentValue = Number(editable[key]?.value);
    if (input.dataset.reset === "true") {
      changes[key] = null;
      return;
    }
    if (value === null || value !== currentValue) {
      changes[key] = null;
      if (value !== null) {
        changes[key] = value;
      }
    }
  });
  return changes;
}

async function loadConfig() {
  const authGeneration = UI_STATE.authGeneration;
  const requestSequence = ++UI_STATE.configRequestSequence;
  try {
    const config = await requestJson("/v1/system/config");
    if (
      !authContextIsCurrent(authGeneration) ||
      requestSequence !== UI_STATE.configRequestSequence
    ) {
      return false;
    }
    UI_STATE.config = config;
    renderConfigRows();
    setConfigEditingEnabled(true);
    return true;
  } catch (error) {
    if (
      !authContextIsCurrent(authGeneration) ||
      requestSequence !== UI_STATE.configRequestSequence
    ) {
      return false;
    }
    UI_STATE.config = null;
    setConfigEditingEnabled(false);
    if (error.status === 401) {
      invalidateAuthContext();
    }
    const detail = error.status === 401
      ? "API 需要 Token，请填写后保存（Token 仅存于内存）"
      : `配置加载失败：${String(error.message || error)}`;
    setStatusMessage(serviceHealth, detail, true);
    return false;
  }
}

async function applyConfigChange() {
  if (UI_STATE.applyingConfig) {
    return;
  }
  if (!UI_STATE.config) {
    return;
  }
  const authGeneration = UI_STATE.authGeneration;
  const requestSequence = ++UI_STATE.configRequestSequence;
  try {
    const changes = buildConfigChanges();
    if (!Object.keys(changes).length) {
      setStatusMessage(serviceHealth, "无变更");
      return;
    }
    UI_STATE.applyingConfig = true;
    const response = await requestJson("/v1/system/config", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        revision: UI_STATE.config.revision,
        changes,
      }),
    });
    if (
      !authContextIsCurrent(authGeneration) ||
      requestSequence !== UI_STATE.configRequestSequence
    ) {
      return;
    }
    UI_STATE.config = response;
    renderConfigRows();
    setStatusMessage(serviceHealth, "配置已更新");
  } catch (error) {
    if (
      !authContextIsCurrent(authGeneration) ||
      requestSequence !== UI_STATE.configRequestSequence
    ) {
      return;
    }
    if (error.status === 409) {
      await loadConfig();
      if (!authContextIsCurrent(authGeneration)) {
        return;
      }
      setStatusMessage(serviceHealth, "配置版本冲突，已刷新最新值，请确认后重试", true);
    } else if (error.status === 401) {
      invalidateAuthContext();
      setStatusMessage(serviceHealth, "API 需要 Token，请填写后保存（Token 仅存于内存）", true);
    } else {
      setStatusMessage(serviceHealth, `更新失败：${String(error.message || error)}`, true);
    }
  } finally {
    if (authContextIsCurrent(authGeneration)) {
      UI_STATE.applyingConfig = false;
    }
  }
}

function receivedJobs(items) {
  const receivedAt = Date.now();
  const jobs = validJobs(items);
  jobs.forEach((item) => {
    // Pair each server timestamp with this response receipt time so countdowns
    // track server time instead of accumulating browser polling delay.
    item.__receivedAt = receivedAt;
  });
  return jobs;
}

function updateJobsCursorHistory(pageIndex, pageCursor, nextCursor) {
  const history = UI_STATE.jobsCursorHistory.slice(0, pageIndex + 1);
  history[pageIndex] = pageCursor || null;
  if (nextCursor) {
    history[pageIndex + 1] = nextCursor;
  }
  UI_STATE.jobsCursorHistory = history;
}

function jobsCursorWouldLoop(nextCursor, pageIndex, pageCursor) {
  if (!nextCursor) {
    return false;
  }
  const visited = UI_STATE.jobsCursorHistory
    .slice(0, pageIndex + 1)
    .filter(Boolean);
  if (pageCursor) {
    visited.push(pageCursor);
  }
  return new Set(visited).has(nextCursor);
}

function invalidJobsCursorProgressionError() {
  const error = new Error("服务返回了未推进或回环的分页游标");
  error.code = "invalid_jobs_cursor_progression";
  return error;
}

function quarantineJobsNextCursor() {
  UI_STATE.jobsCursorHistory = UI_STATE.jobsCursorHistory.slice(
    0,
    UI_STATE.jobsPageIndex + 1,
  );
  UI_STATE.jobsNextCursor = null;
  renderJobsPagination();
}

async function requestJobsPage(url) {
  try {
    return await requestJson(url);
  } catch (error) {
    error.jobsRequest = true;
    throw error;
  }
}

async function refreshDashboardData({ resetJobs = false, cancelNavigation = false } = {}) {
  if (UI_STATE.jobsLoadingPage) {
    if (!cancelNavigation) {
      return false;
    }
    // A deliberate manual/programmatic refresh owns the list lane. Invalidate
    // the navigation response and reload the page that is currently visible.
    UI_STATE.refreshSequence += 1;
    UI_STATE.jobsRequestSequence += 1;
    UI_STATE.jobsLoadingPage = false;
    renderJobsPagination();
  }

  const authGeneration = UI_STATE.authGeneration;
  const refreshSequence = ++UI_STATE.refreshSequence;
  const query = jobsQueryValue();
  const queryChanged = UI_STATE.jobsQuery !== query;
  const shouldResetJobs = resetJobs || queryChanged;
  const pageIndex = shouldResetJobs ? 0 : UI_STATE.jobsPageIndex;
  const pageCursor = shouldResetJobs
    ? null
    : UI_STATE.jobsCursorHistory[pageIndex] || null;
  const jobsRequestSequence = ++UI_STATE.jobsRequestSequence;
  try {
    const [capabilities, storage, jobsResponse] = await Promise.all([
      requestJson("/v1/capabilities"),
      requestJson("/v1/system/storage"),
      requestJobsPage(jobsUrl(pageCursor, query)),
    ]);
    if (
      !authContextIsCurrent(authGeneration) ||
      refreshSequence !== UI_STATE.refreshSequence ||
      jobsRequestSequence !== UI_STATE.jobsRequestSequence
    ) {
      return false;
    }
    UI_STATE.lastHealthFetch = capabilities;
    UI_STATE.lastStorageFetch = storage;
    const nextCursor = normalizeNextCursor(jobsResponse.next_cursor);
    if (jobsCursorWouldLoop(nextCursor, pageIndex, pageCursor)) {
      throw invalidJobsCursorProgressionError();
    }
    UI_STATE.jobs = receivedJobs(jobsResponse.items);
    UI_STATE.jobsQuery = query;
    UI_STATE.jobsPageIndex = pageIndex;
    UI_STATE.jobsNextCursor = nextCursor;
    updateJobsCursorHistory(pageIndex, pageCursor, nextCursor);
    renderJobs();
    applyCounts();
    renderSystemCards();
    renderStorageCards();
    if (!UI_STATE.config) {
      await loadConfig();
    }
    if (
      !authContextIsCurrent(authGeneration) ||
      refreshSequence !== UI_STATE.refreshSequence ||
      jobsRequestSequence !== UI_STATE.jobsRequestSequence
    ) {
      return false;
    }
    setStatusMessage(serviceHealth, "已刷新", false);
    return true;
  } catch (error) {
    if (
      !authContextIsCurrent(authGeneration) ||
      refreshSequence !== UI_STATE.refreshSequence ||
      jobsRequestSequence !== UI_STATE.jobsRequestSequence
    ) {
      return false;
    }
    if (
      error.jobsRequest &&
      (error.status === 400 || error.status === 422) &&
      pageIndex > 0
    ) {
      resetJobsPagination();
      const recovered = await refreshDashboardData({
        resetJobs: true,
        cancelNavigation: true,
      });
      if (authContextIsCurrent(authGeneration) && recovered) {
        setStatusMessage(serviceHealth, "分页游标已失效，已返回第一页", true);
      }
      return recovered;
    } else if (error.code === "invalid_jobs_cursor_progression") {
      quarantineJobsNextCursor();
      setStatusMessage(serviceHealth, error.message, true);
    } else if (error.status === 401) {
      invalidateAuthContext();
      setStatusMessage(serviceHealth, "API 需要 Token，请填写后保存（Token 仅存于内存）", true);
    } else {
      setStatusMessage(serviceHealth, `刷新失败：${String(error.message || error)}`, true);
    }
    return false;
  }
}

async function navigateJobsPage(direction) {
  if (UI_STATE.jobsLoadingPage || ![-1, 1].includes(direction)) {
    return;
  }
  const targetIndex = UI_STATE.jobsPageIndex + direction;
  if (targetIndex < 0 || (direction > 0 && !UI_STATE.jobsNextCursor)) {
    return;
  }
  const pageCursor = direction > 0
    ? UI_STATE.jobsNextCursor
    : UI_STATE.jobsCursorHistory[targetIndex] || null;
  const query = jobsQueryValue();
  if (query !== UI_STATE.jobsQuery) {
    resetJobsPagination();
    await refreshDashboardData({ resetJobs: true, cancelNavigation: true });
    return;
  }

  const authGeneration = UI_STATE.authGeneration;
  const navigationRefreshSequence = ++UI_STATE.refreshSequence;
  const requestSequence = ++UI_STATE.jobsRequestSequence;
  UI_STATE.jobsLoadingPage = true;
  renderJobsPagination();
  try {
    const jobsResponse = await requestJobsPage(jobsUrl(pageCursor, query));
    if (
      !authContextIsCurrent(authGeneration) ||
      navigationRefreshSequence !== UI_STATE.refreshSequence ||
      requestSequence !== UI_STATE.jobsRequestSequence
    ) {
      return false;
    }
    const nextCursor = normalizeNextCursor(jobsResponse.next_cursor);
    if (jobsCursorWouldLoop(nextCursor, targetIndex, pageCursor)) {
      throw invalidJobsCursorProgressionError();
    }
    UI_STATE.jobs = receivedJobs(jobsResponse.items);
    UI_STATE.jobsQuery = query;
    UI_STATE.jobsPageIndex = targetIndex;
    UI_STATE.jobsNextCursor = nextCursor;
    updateJobsCursorHistory(targetIndex, pageCursor, nextCursor);
    renderJobs();
    applyCounts();
    setStatusMessage(serviceHealth, `已加载第 ${targetIndex + 1} 页`, false);
    return true;
  } catch (error) {
    if (
      !authContextIsCurrent(authGeneration) ||
      navigationRefreshSequence !== UI_STATE.refreshSequence ||
      requestSequence !== UI_STATE.jobsRequestSequence
    ) {
      return false;
    }
    if (error.code === "invalid_jobs_cursor_progression") {
      quarantineJobsNextCursor();
      setStatusMessage(serviceHealth, error.message, true);
    } else if (error.jobsRequest && (error.status === 400 || error.status === 422)) {
      UI_STATE.jobsLoadingPage = false;
      resetJobsPagination();
      const recovered = await refreshDashboardData({
        resetJobs: true,
        cancelNavigation: true,
      });
      if (authContextIsCurrent(authGeneration) && recovered) {
        setStatusMessage(serviceHealth, "分页游标已失效，已返回第一页", true);
      }
      return recovered;
    } else if (error.status === 401) {
      invalidateAuthContext();
      setStatusMessage(serviceHealth, "API 需要 Token，请填写后保存（Token 仅存于内存）", true);
    } else {
      setStatusMessage(
        serviceHealth,
        `翻页失败：${String(error.message || error)}`,
        true,
      );
    }
    return false;
  } finally {
    if (
      authContextIsCurrent(authGeneration) &&
      navigationRefreshSequence === UI_STATE.refreshSequence &&
      requestSequence === UI_STATE.jobsRequestSequence
    ) {
      UI_STATE.jobsLoadingPage = false;
      renderJobsPagination();
    }
  }
}

function setupUpload() {
  selectFileButton.addEventListener("click", () => {
    fileInput.click();
  });
  uploadDropZone.addEventListener("click", (event) => {
    if (event.target !== selectFileButton && event.target !== fileInput) {
      fileInput.click();
    }
  });
  uploadDropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });
  uploadDropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "copy";
    }
  });
  uploadDropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) {
      uploadFile(file);
    }
  });
  fileInput.addEventListener("change", () => {
    if (!fileInput.files || !fileInput.files[0]) {
      return;
    }
    uploadFile(fileInput.files[0]);
  });
}

function uploadFile(file) {
  if (UI_STATE.uploadXhr) {
    setStatusMessage(uploadState, "已有上传正在进行", true);
    return;
  }
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    setStatusMessage(uploadState, "仅支持 PDF", true);
    return;
  }
  const form = new FormData();
  form.append("file", file, file.name);
  const xhr = new XMLHttpRequest();
  const authGeneration = UI_STATE.authGeneration;
  UI_STATE.uploadXhr = xhr;
  uploadProgress.value = 0;
  setStatusMessage(uploadState, "上传中...");
  xhr.open("POST", "/v1/jobs", true);
  xhr.upload.addEventListener("progress", (event) => {
    if (
      UI_STATE.uploadXhr !== xhr ||
      !authContextIsCurrent(authGeneration)
    ) {
      return;
    }
    if (!event.lengthComputable) {
      uploadProgress.classList.add("progress-indeterminate");
      return;
    }
    const percent = Math.round((event.loaded / event.total) * 100);
    uploadProgress.value = percent;
    uploadProgress.classList.remove("progress-indeterminate");
    setStatusMessage(uploadState, `上传进度：${percent}%`);
  });
  xhr.addEventListener("load", async () => {
    if (
      UI_STATE.uploadXhr !== xhr ||
      !authContextIsCurrent(authGeneration)
    ) {
      return;
    }
    UI_STATE.uploadXhr = null;
    fileInput.value = "";
    if (xhr.status >= 200 && xhr.status < 300) {
      setStatusMessage(uploadState, "上传完成，开始处理");
      uploadProgress.value = 100;
      await refreshDashboardData({ cancelNavigation: true });
      return;
    }
    if (xhr.status === 401) {
      invalidateAuthContext();
    }
    setStatusMessage(uploadState, `上传失败：${xhr.status}`, true);
  });
  xhr.addEventListener("error", () => {
    if (
      UI_STATE.uploadXhr !== xhr ||
      !authContextIsCurrent(authGeneration)
    ) {
      return;
    }
    UI_STATE.uploadXhr = null;
    setStatusMessage(uploadState, "上传失败，请稍后再试", true);
  });
  xhr.addEventListener("abort", () => {
    if (
      UI_STATE.uploadXhr !== xhr ||
      !authContextIsCurrent(authGeneration)
    ) {
      return;
    }
    UI_STATE.uploadXhr = null;
    setStatusMessage(uploadState, "上传已取消", true);
  });
  if (hasToken()) {
    xhr.setRequestHeader("Authorization", `Bearer ${UI_STATE.token}`);
  }
  xhr.send(form);
}

function initTimer() {
  if (UI_STATE.refreshTimer) {
    clearInterval(UI_STATE.refreshTimer);
  }
  if (UI_STATE.countdownTimer) {
    clearInterval(UI_STATE.countdownTimer);
  }
  UI_STATE.refreshTimer = setInterval(() => {
    refreshDashboardData().catch(() => undefined);
  }, 5000);
  UI_STATE.countdownTimer = setInterval(updateCountdowns, 1000);
}

function setupEvents() {
  tokenSaveButton.addEventListener("click", async () => {
    setToken(tokenInput.value);
    tokenInput.value = "";
    buildAuthNotice();
    setStatusMessage(
      serviceHealth,
      hasToken() ? "Token 已缓存到内存" : "未设置 Token，尝试无鉴权连接"
    );
    await refreshDashboardData({ cancelNavigation: true });
  });
  clearTokenButton.addEventListener("click", () => {
    setToken("");
    tokenInput.value = "";
    setStatusMessage(serviceHealth, "Token 已清空（页面离开后即失效）");
    refreshDashboardData({ cancelNavigation: true }).catch(() => undefined);
  });
  refreshDashboard.addEventListener("click", () => {
    refreshDashboardData({ cancelNavigation: true }).catch(() => undefined);
  });
  previousJobsPageButton.addEventListener("click", () => {
    navigateJobsPage(-1).catch(() => undefined);
  });
  nextJobsPageButton.addEventListener("click", () => {
    navigateJobsPage(1).catch(() => undefined);
  });
  toggleConfig.addEventListener("click", () => {
    const isHidden = configBody.hidden;
    configBody.hidden = !isHidden;
    toggleConfig.textContent = isHidden ? "收起配置" : "展开配置";
    toggleConfig.setAttribute("aria-expanded", String(isHidden));
    if (isHidden && !UI_STATE.config) {
      loadConfig().catch(() => undefined);
    }
  });
  applyConfig.addEventListener("click", () => {
    applyConfigChange().catch(() => undefined);
  });
  stateFilter.addEventListener("change", () => {
    resetJobsPagination();
    refreshDashboardData({ resetJobs: true, cancelNavigation: true }).catch(() => undefined);
  });
}

async function boot() {
  setupUpload();
  buildAuthNotice();
  setupEvents();
  initTimer();
  await refreshDashboardData();
}

if (
  globalThis.__DOCLING_UI_TEST__ &&
  typeof globalThis.__DOCLING_UI_TEST__ === "object"
) {
  Object.assign(globalThis.__DOCLING_UI_TEST__, {
    UI_STATE,
    setToken,
    loadConfig,
    loadOutputs,
    refreshDashboardData,
    navigateJobsPage,
  });
} else {
  boot().catch((error) => {
    setStatusMessage(serviceHealth, `启动失败：${String(error.message || error)}`, true);
  });
}
