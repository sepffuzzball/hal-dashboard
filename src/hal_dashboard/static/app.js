"use strict";

const TOKEN_KEY = "hal-dashboard-token";
const MIN_REFRESH_SECONDS = 5;
const MAX_REFRESH_SECONDS = 300;
const TERMINAL_STATES = new Set(["succeeded", "failed"]);
const ACTIVE_STATES = new Set(["active", "activating"]);
const STEP_LABELS = {
  snapshot: "Capture current state",
  "stop-others": "Stop conflicts",
  "start-model": "Start selected model",
  "companion-service": "Apply ComfyUI policy",
  "apply-state": "Apply requested state",
};

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  config: null,
  status: null,
  selectedModelId: null,
  refreshSeconds: 60,
  nextRefreshAt: 0,
  refreshTimer: null,
  countdownTimer: null,
  operationTimer: null,
  elapsedTimer: null,
  operation: null,
  operationUrl: null,
  refreshing: false,
};

const el = {};

document.addEventListener("DOMContentLoaded", initialize);

function initialize() {
  const ids = [
    "server-name", "global-health", "last-contact", "lock-button", "notice", "main",
    "refresh-interval", "refresh-button", "refresh-meta", "cpu-card", "memory-card",
    "storage-card", "gpu-cards", "server-specs", "operation-panel", "operation-status",
    "operation-elapsed", "operation-message", "operation-steps", "operation-final",
    "dismiss-operation", "conflict-warning", "services-body", "review-button", "model-list",
    "document-state", "token-dialog", "token-form", "token-input", "token-copy", "token-error",
    "unlock-button", "review-dialog", "review-form", "close-review", "review-model-name",
    "review-eta", "review-changes", "comfy-policy", "comfy-options", "comfy-policy-copy",
    "review-warning", "cancel-review", "confirm-switch", "live-region",
  ];
  ids.forEach((id) => { el[toCamel(id)] = document.getElementById(id); });

  el.tokenForm.addEventListener("submit", submitToken);
  el.tokenDialog.addEventListener("cancel", (event) => event.preventDefault());
  el.lockButton.addEventListener("click", lockConsole);
  el.refreshButton.addEventListener("click", () => refreshStatus(true));
  el.refreshInterval.addEventListener("change", changeRefreshInterval);
  el.reviewButton.addEventListener("click", openReview);
  el.closeReview.addEventListener("click", closeReview);
  el.cancelReview.addEventListener("click", closeReview);
  el.reviewForm.addEventListener("submit", confirmSwitch);
  el.comfyOptions.addEventListener("change", updateReviewChanges);
  el.dismissOperation.addEventListener("click", dismissOperation);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("online", () => refreshStatus(true));
  window.addEventListener("offline", () => setGlobalHealth("Offline", "failed"));

  checkPublicHealth();
  if (state.token) {
    unlockWithStoredToken();
  } else {
    showTokenDialog();
  }
}

function toCamel(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

async function checkPublicHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    const payload = await response.json();
    setGlobalHealth(payload.status === "ok" ? "Server online" : "Server unavailable", payload.status === "ok" ? "active" : "failed");
  } catch (_) {
    setGlobalHealth("Server unavailable", "failed");
  }
}

async function unlockWithStoredToken() {
  try {
    await loadConsole();
  } catch (error) {
    if (error.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      state.token = "";
      showTokenDialog("Your stored token was not accepted. Enter a current token to continue.");
    } else {
      showTokenDialog("The console could not be loaded. Check the connection and retry.");
    }
  }
}

async function submitToken(event) {
  event.preventDefault();
  const token = el.tokenInput.value.trim();
  if (!token) return;
  el.unlockButton.disabled = true;
  el.tokenError.hidden = true;
  state.token = token;
  try {
    await loadConsole();
    sessionStorage.setItem(TOKEN_KEY, token);
    el.tokenInput.value = "";
    el.tokenDialog.close();
  } catch (error) {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    el.tokenError.textContent = error.status === 401
      ? "Token not accepted. Check it and try again."
      : "Unable to contact the console. Check the connection and retry.";
    el.tokenError.hidden = false;
    el.tokenInput.select();
  } finally {
    el.unlockButton.disabled = false;
  }
}

async function loadConsole() {
  const config = await api("/api/config");
  state.config = config;
  state.refreshSeconds = validRefreshDefault(config.refresh);
  renderConfig();
  el.main.hidden = false;
  el.lockButton.hidden = false;
  el.documentState.textContent = "Authenticated session";
  await refreshStatus(true);
  scheduleRefresh();
}

function validRefreshDefault(refresh) {
  const choices = Array.isArray(refresh && refresh.choices_seconds)
    ? refresh.choices_seconds.filter((value) => Number.isInteger(value) && value >= MIN_REFRESH_SECONDS && value <= MAX_REFRESH_SECONDS)
    : [];
  return choices.includes(refresh && refresh.default_seconds) ? refresh.default_seconds : 60;
}

function renderConfig() {
  const config = state.config;
  el.serverName.textContent = config.server.name || "HAL";
  document.title = `${config.server.name || "HAL"} Operations Console`;
  replaceChildren(el.refreshInterval);
  const choices = Array.isArray(config.refresh.choices_seconds) ? config.refresh.choices_seconds : [60];
  choices.forEach((seconds) => {
    const option = create("option", null, formatInterval(seconds));
    option.value = String(seconds);
    option.selected = seconds === state.refreshSeconds;
    el.refreshInterval.append(option);
  });
  renderServerSpecs(config.server);
  renderModels();
}

function renderServerSpecs(server) {
  replaceChildren(el.serverSpecs);
  const specs = [
    ["OS", server.os],
    ["Memory", Number.isFinite(server.ram_gb) ? `${server.ram_gb} GB configured` : "Not specified"],
    ["CPU", server.cpu_summary || "Not specified"],
    ["GPU", server.gpu_summary || "Not specified"],
  ];
  specs.forEach(([term, description]) => {
    const wrapper = create("div");
    wrapper.append(create("dt", null, term), create("dd", null, description));
    el.serverSpecs.append(wrapper);
  });
}

async function refreshStatus(manual = false) {
  if (!state.token || state.refreshing || (document.hidden && !manual)) return;
  state.refreshing = true;
  el.refreshButton.disabled = true;
  if (manual) el.refreshMeta.textContent = "Refreshing status...";
  try {
    const status = await api("/api/status");
    state.status = status;
    renderStatus();
    const now = new Date();
    el.lastContact.textContent = `Last contact ${formatTime(now)}`;
    setGlobalHealth("Connected", "active");
    state.nextRefreshAt = Date.now() + state.refreshSeconds * 1000;
    if (status.active_operation && !state.operationUrl) {
      beginOperationPolling(status.active_operation.status_url);
    }
    showNotice("", false);
  } catch (error) {
    if (error.status === 401) {
      handleUnauthorized();
      return;
    }
    setGlobalHealth("Contact failed", "failed");
    showNotice(messageForError(error, "Status could not be refreshed."), true);
  } finally {
    state.refreshing = false;
    el.refreshButton.disabled = false;
    updateMutationAvailability();
    updateCountdown();
  }
}

function renderStatus() {
  const status = state.status;
  renderMetric(el.cpuCard, "CPU load", "CPU", status.cpu, null);
  renderMetric(el.memoryCard, "Memory", "RAM", status.memory, "bytes");
  renderMetric(el.storageCard, "Root storage", "ROOT", status.storage_root, "bytes");
  renderGpus(status.gpus);
  renderServices();
  renderModels();
  renderConflictWarnings(status.conflict_warning);
}

function renderMetric(container, title, code, data, detailType) {
  replaceChildren(container);
  const heading = create("div", "metric-heading");
  heading.append(create("h2", null, title), create("span", "metric-kicker", code));
  container.append(heading);
  if (!data || !Number.isFinite(data.percent)) {
    container.append(create("p", "metric-value", "--"));
    container.append(create("p", "unavailable", safeReason(data && data.reason, "Metric unavailable")));
    return;
  }
  const value = create("p", "metric-value");
  value.append(document.createTextNode(formatPercent(data.percent)), create("span", "metric-unit", "%"));
  const progress = create("progress");
  progress.max = 100;
  progress.value = clamp(data.percent, 0, 100);
  progress.setAttribute("aria-label", `${title}: ${formatPercent(data.percent)} percent`);
  container.append(value, progress);
  if (detailType === "bytes") {
    const detail = Number.isFinite(data.used_bytes) && Number.isFinite(data.total_bytes)
      ? `${formatBytes(data.used_bytes)} used of ${formatBytes(data.total_bytes)}`
      : "Capacity detail unavailable";
    container.append(create("p", "metric-detail", detail));
  } else {
    container.append(create("p", "metric-detail", "Current aggregate utilization"));
  }
  if (data.reason) container.append(create("p", "unavailable", safeReason(data.reason, "Some data unavailable")));
}

function renderGpus(gpuSection) {
  replaceChildren(el.gpuCards);
  const items = gpuSection && Array.isArray(gpuSection.items) ? gpuSection.items : [];
  if (!items.length) {
    const card = create("article", "metric-card gpu-card");
    card.append(create("h2", null, "GPU telemetry"), create("p", "unavailable", safeReason(gpuSection && gpuSection.reason, "No GPU data available")));
    el.gpuCards.append(card);
    return;
  }
  items.forEach((gpu) => {
    const card = create("article", "metric-card gpu-card");
    const heading = create("div", "metric-heading");
    heading.append(create("h2", null, `GPU ${safeText(gpu.index, "--")}`), create("span", "metric-kicker", `DEVICE ${safeText(gpu.index, "--")}`));
    card.append(heading, create("p", "gpu-name", safeText(gpu.name, "Name unavailable")));
    const stats = create("div", "gpu-stats");
    stats.append(
      gpuStat("Utilization", percentOrDash(gpu.utilization_percent)),
      gpuStat("Temperature", Number.isFinite(gpu.temperature_celsius) ? `${gpu.temperature_celsius} C` : "--"),
      gpuStat("VRAM used", Number.isFinite(gpu.memory_used_bytes) ? formatBytes(gpu.memory_used_bytes) : "--"),
      gpuStat("VRAM total", Number.isFinite(gpu.memory_total_bytes) ? formatBytes(gpu.memory_total_bytes) : "--")
    );
    card.append(stats);
    if (Number.isFinite(gpu.memory_percent)) {
      const progress = create("progress");
      progress.max = 100;
      progress.value = clamp(gpu.memory_percent, 0, 100);
      progress.setAttribute("aria-label", `GPU ${gpu.index} VRAM: ${formatPercent(gpu.memory_percent)} percent`);
      card.append(progress);
    }
    if (gpuSection.reason) card.append(create("p", "unavailable", safeReason(gpuSection.reason, "Some GPU data unavailable")));
    el.gpuCards.append(card);
  });
}

function gpuStat(label, value) {
  const wrapper = create("div", "gpu-stat");
  wrapper.append(create("strong", null, value), create("span", null, label));
  return wrapper;
}

function renderServices(itemsOverride = null) {
  if (!state.config) return;
  const statusItems = itemsOverride || (state.status && state.status.services && state.status.services.items) || [];
  const byId = new Map(statusItems.map((item) => [item.id, item]));
  replaceChildren(el.servicesBody);
  const configured = [
    ...state.config.models.map((item) => ({ ...item, kind: "model" })),
    ...state.config.services.map((item) => ({ ...item, kind: "auxiliary" })),
  ];
  configured.forEach((spec) => {
    const observed = byId.get(spec.id) || { id: spec.id, display_name: spec.display_name, kind: spec.kind, state: "unknown", healthy: null, reason: "Status unavailable" };
    const row = create("tr");
    const nameCell = cell("Service");
    nameCell.append(document.createTextNode(spec.display_name), create("span", "service-sub", spec.id));
    const roleCell = cell("Role", spec.kind === "model" ? "Inference model" : "Auxiliary");
    const stateCell = cell("System state");
    stateCell.append(statusBadge(observed.state));
    const healthCell = cell("HTTP health");
    const health = observed.healthy === true ? ["Healthy", "healthy"] : observed.healthy === false ? ["Unhealthy", "unhealthy"] : ["Not verified", "unchecked"];
    healthCell.append(statusBadge(health[0], health[1]));
    if (observed.reason) healthCell.append(create("span", "health-copy", safeReason(observed.reason, "Health detail unavailable")));
    const controlCell = cell("Control");
    if (spec.kind === "auxiliary") controlCell.append(serviceControls(spec, observed));
    else controlCell.append(create("span", "control-reason", "Use model switch review"));
    row.append(nameCell, roleCell, stateCell, healthCell, controlCell);
    el.servicesBody.append(row);
  });
}

function cell(label, text) {
  const td = create("td", null, text);
  td.dataset.label = label;
  return td;
}

function serviceControls(spec, observed) {
  const wrapper = create("div");
  const controls = create("div", "service-controls");
  const on = create("button", "quiet-button", "On");
  const off = create("button", "quiet-button", "Off");
  on.type = off.type = "button";
  const conflict = activeConflictForService(spec);
  const busy = mutationBusy();
  const indeterminate = observed.state === "unknown" || observed.state === "deactivating";
  on.disabled = busy || indeterminate || ACTIVE_STATES.has(observed.state) || Boolean(conflict);
  off.disabled = busy || indeterminate || observed.state === "inactive" || observed.state === "failed";
  on.addEventListener("click", () => setService(spec.id, true));
  off.addEventListener("click", () => setService(spec.id, false));
  controls.append(on, off);
  wrapper.append(controls);
  let reason = "Choose an explicit desired state.";
  if (busy) reason = "Unavailable while another operation is running.";
  else if (conflict) reason = `On is blocked while ${conflict.display_name} is active.`;
  else if (ACTIVE_STATES.has(observed.state)) reason = "Already on; only Off is available.";
  else if (observed.state === "inactive" || observed.state === "failed") reason = "Already not running; only On is available.";
  else if (indeterminate) reason = "Control unavailable until a stable state can be observed.";
  wrapper.append(create("span", "control-reason", reason));
  return wrapper;
}

function activeConflictForService(service) {
  if (!state.status) return null;
  const activeIds = new Set(state.status.active_model_ids || []);
  return state.config.models.find((model) => service.conflicts_with.includes(model.id) && activeIds.has(model.id)) || null;
}

function renderModels() {
  if (!state.config) return;
  const activeIds = new Set((state.status && state.status.active_model_ids) || []);
  replaceChildren(el.modelList);
  state.config.models.forEach((model) => {
    const card = create("div", "model-card");
    const active = activeIds.has(model.id);
    const selected = state.selectedModelId === model.id;
    card.classList.toggle("active", active);
    card.classList.toggle("selected", selected);
    card.setAttribute("role", "radio");
    card.setAttribute("aria-checked", String(selected));
    card.setAttribute("aria-label", `${model.display_name}${active ? ", currently active" : ""}`);
    card.tabIndex = selected || (!state.selectedModelId && model === state.config.models[0]) ? 0 : -1;
    if (mutationBusy()) card.setAttribute("aria-disabled", "true");
    const top = create("div", "model-card-top");
    top.append(create("span", "metric-kicker", model.id));
    if (active) top.append(statusBadge("Active", "active"));
    card.append(top, create("h3", null, model.display_name), create("p", "model-synopsis", model.synopsis));
    const meta = create("dl", "model-meta");
    meta.append(metaItem("GPU allocation", formatGpuAllocation(model.gpus)), metaItem("Startup ETA", formatDuration(model.startup_eta_seconds)));
    card.append(meta);
    const strengths = create("ul", "strength-list");
    const entries = Array.isArray(model.strengths) && model.strengths.length ? model.strengths : ["General workloads"];
    entries.forEach((strength) => strengths.append(create("li", null, strength)));
    card.append(strengths);
    card.addEventListener("click", () => selectModel(model.id));
    card.addEventListener("keydown", (event) => handleModelKey(event, model.id));
    el.modelList.append(card);
  });
  updateMutationAvailability();
}

function metaItem(term, value) {
  const wrapper = create("div");
  wrapper.append(create("dt", null, term), create("dd", null, value));
  return wrapper;
}

function selectModel(modelId) {
  if (mutationBusy()) return;
  state.selectedModelId = modelId;
  renderModels();
  const selected = el.modelList.querySelector('[aria-checked="true"]');
  if (selected) selected.focus({ preventScroll: true });
}

function handleModelKey(event, modelId) {
  const models = state.config.models;
  const index = models.findIndex((model) => model.id === modelId);
  let next = null;
  if (["ArrowRight", "ArrowDown"].includes(event.key)) next = models[(index + 1) % models.length];
  if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = models[(index - 1 + models.length) % models.length];
  if (event.key === " " || event.key === "Enter") next = models[index];
  if (!next) return;
  event.preventDefault();
  selectModel(next.id);
}

function renderConflictWarnings(conflict) {
  const warnings = conflict && Array.isArray(conflict.warnings) ? conflict.warnings : [];
  if (!warnings.length) {
    el.conflictWarning.hidden = true;
    return;
  }
  el.conflictWarning.textContent = `Configuration conflict observed: ${warnings.join(" ")}`;
  el.conflictWarning.hidden = false;
}

function openReview() {
  const model = selectedModel();
  if (!model || mutationBusy()) return;
  el.reviewModelName.textContent = model.display_name;
  el.reviewEta.textContent = `Typical model startup: ${formatDuration(model.startup_eta_seconds)}`;
  const radios = Array.from(el.comfyOptions.querySelectorAll('input[name="comfy"]'));
  radios.forEach((radio) => {
    radio.checked = radio.value === "auto";
    radio.disabled = !model.allow_comfyui_override && radio.value !== "auto";
  });
  el.comfyPolicyCopy.textContent = model.allow_comfyui_override
    ? `Auto follows this model's configured policy: ComfyUI ${model.comfyui_default ? "On" : "Off"}.`
    : `Fixed policy: ComfyUI ${model.comfyui_default ? "On" : "Off"}. Override is not permitted for this model.`;
  updateReviewChanges();
  el.reviewDialog.showModal();
}

function updateReviewChanges() {
  const model = selectedModel();
  if (!model) return;
  const desiredComfy = desiredComfyState(model);
  const services = (state.status && state.status.services && state.status.services.items) || [];
  const byId = new Map(services.map((item) => [item.id, item]));
  const changes = new Set();
  state.config.models.forEach((candidate) => {
    const observed = byId.get(candidate.id);
    if (candidate.id !== model.id && observed && ACTIVE_STATES.has(observed.state)) changes.add(`Stop ${candidate.display_name}.`);
  });
  state.config.services.forEach((service) => {
    const observed = byId.get(service.id);
    if (model.conflicts_with.includes(service.id) && observed && ACTIVE_STATES.has(observed.state)) changes.add(`Stop ${service.display_name}.`);
  });
  const selectedObserved = byId.get(model.id);
  if (!selectedObserved || selectedObserved.state !== "active") changes.add(`Start and verify ${model.display_name}.`);
  const comfy = state.config.services.find((service) => service.id === "comfyui");
  const comfyObserved = comfy && byId.get(comfy.id);
  if (comfy) {
    if (desiredComfy && (!comfyObserved || !ACTIVE_STATES.has(comfyObserved.state))) changes.add("Start and verify ComfyUI.");
    if (!desiredComfy && comfyObserved && ACTIVE_STATES.has(comfyObserved.state)) changes.add("Stop ComfyUI.");
  }
  replaceChildren(el.reviewChanges);
  const changeItems = Array.from(changes);
  (changeItems.length ? changeItems : ["No service state changes are expected; the server will verify the requested topology."]).forEach((change) => el.reviewChanges.append(create("li", null, change)));
  const noChange = changeItems.length === 0;
  el.reviewWarning.textContent = noChange
    ? "The requested topology already appears active."
    : "Services remain unchanged until you confirm. Stopping services and health checks can add time beyond the typical startup figure.";
  el.confirmSwitch.disabled = noChange || mutationBusy();
}

function desiredComfyState(model) {
  const checked = el.comfyOptions.querySelector('input[name="comfy"]:checked');
  if (!checked || checked.value === "auto") return model.comfyui_default;
  return checked.value === "on";
}

function selectedComfyOverride() {
  const checked = el.comfyOptions.querySelector('input[name="comfy"]:checked');
  if (!checked || checked.value === "auto") return null;
  return checked.value === "on";
}

async function confirmSwitch(event) {
  event.preventDefault();
  const model = selectedModel();
  if (!model || mutationBusy()) return;
  const body = { model_id: model.id, comfyui: model.allow_comfyui_override ? selectedComfyOverride() : null };
  closeReview();
  await submitMutation("/api/switch", "POST", body);
}

async function setService(serviceId, active) {
  if (mutationBusy()) return;
  await submitMutation(`/api/services/${encodeURIComponent(serviceId)}`, "PUT", { active });
}

async function submitMutation(url, method, body) {
  setMutationsDisabled(true);
  try {
    const response = await api(url, { method, body: JSON.stringify(body) });
    beginOperationPolling(response.status_url);
  } catch (error) {
    if (error.status === 401) {
      handleUnauthorized();
      return;
    }
    if (error.status === 409 && error.payload && error.payload.status_url) {
      showNotice("Another operation is active. Following its progress instead.", false);
      beginOperationPolling(error.payload.status_url);
      return;
    }
    const conflicts = error.payload && Array.isArray(error.payload.conflicts_with)
      ? ` Conflicting models: ${error.payload.conflicts_with.join(", ")}.`
      : "";
    showNotice(messageForError(error, "The requested change was not accepted.") + conflicts, true);
    setMutationsDisabled(false);
  }
}

function beginOperationPolling(statusUrl) {
  if (!isLocalOperationUrl(statusUrl)) {
    showNotice("The operation response could not be followed safely.", true);
    setMutationsDisabled(false);
    return;
  }
  clearTimeout(state.operationTimer);
  state.operationUrl = statusUrl;
  state.operation = { status: "queued", steps: [], created_at: new Date().toISOString() };
  renderOperation();
  pollOperation();
}

async function pollOperation() {
  if (!state.operationUrl) return;
  try {
    state.operation = await api(state.operationUrl);
    renderOperation();
    if (TERMINAL_STATES.has(state.operation.status)) {
      state.operationUrl = null;
      clearTimeout(state.operationTimer);
      await refreshStatus(true);
      setMutationsDisabled(false);
      return;
    }
  } catch (error) {
    if (error.status === 401) {
      handleUnauthorized();
      return;
    }
    if (error.status === 404) {
      state.operationUrl = null;
      state.operation = { ...state.operation, status: "failed", message: "Operation status is no longer available.", finished_at: new Date().toISOString() };
      renderOperation();
      await refreshStatus(true);
      return;
    }
    el.operationMessage.textContent = "Operation status temporarily unavailable. Retrying...";
  }
  state.operationTimer = window.setTimeout(pollOperation, 1000);
}

function renderOperation() {
  const operation = state.operation;
  if (!operation) return;
  el.operationPanel.hidden = false;
  el.operationStatus.textContent = titleCase(operation.status || "unknown");
  el.operationStatus.className = `status-label status-${statusClass(operation.status)}`;
  const current = operation.current_step ? STEP_LABELS[operation.current_step] || humanize(operation.current_step) : null;
  if (operation.status === "failed") {
    el.operationMessage.textContent = `${safeReason(operation.message, "The operation failed.")} Review the observed final states below before retrying.`;
  } else if (operation.status === "succeeded") {
    el.operationMessage.textContent = safeReason(operation.message, "Operation completed and final states were observed.");
  } else {
    el.operationMessage.textContent = current ? `In progress: ${current}.` : "Operation queued. Waiting for the first step.";
  }
  replaceChildren(el.operationSteps);
  (operation.steps || []).forEach((step) => {
    const item = create("li");
    item.append(create("strong", null, STEP_LABELS[step.name] || humanize(step.name)), statusBadge(step.status));
    el.operationSteps.append(item);
  });
  replaceChildren(el.operationFinal);
  if (Array.isArray(operation.services)) {
    const summary = operation.services.map((service) => `${service.display_name}: ${service.state}`).join("; ");
    el.operationFinal.textContent = `Final observed states: ${summary}.`;
    if (TERMINAL_STATES.has(operation.status)) renderServices(operation.services);
  }
  el.dismissOperation.hidden = !TERMINAL_STATES.has(operation.status);
  el.liveRegion.textContent = `${titleCase(operation.status)}. ${current || el.operationMessage.textContent}`;
  setMutationsDisabled(!TERMINAL_STATES.has(operation.status));
  startElapsedClock();
}

function startElapsedClock() {
  clearInterval(state.elapsedTimer);
  updateElapsed();
  if (state.operation && !TERMINAL_STATES.has(state.operation.status)) {
    state.elapsedTimer = window.setInterval(updateElapsed, 1000);
  }
}

function updateElapsed() {
  const operation = state.operation;
  if (!operation) return;
  const start = Date.parse(operation.started_at || operation.created_at);
  const end = operation.finished_at ? Date.parse(operation.finished_at) : Date.now();
  if (!Number.isFinite(start)) {
    el.operationElapsed.textContent = "--:--";
    return;
  }
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  el.operationElapsed.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function dismissOperation() {
  if (state.operation && !TERMINAL_STATES.has(state.operation.status)) return;
  state.operation = null;
  el.operationPanel.hidden = true;
  clearInterval(state.elapsedTimer);
}

function closeReview() {
  if (el.reviewDialog.open) el.reviewDialog.close();
}

function mutationBusy() {
  return Boolean(state.operationUrl || (state.status && state.status.active_operation));
}

function setMutationsDisabled(disabled) {
  el.reviewButton.disabled = disabled || !state.selectedModelId;
  Array.from(el.servicesBody.querySelectorAll("button")).forEach((button) => { button.disabled = disabled || button.disabled; });
  if (!disabled) {
    renderServices();
    renderModels();
  }
}

function updateMutationAvailability() {
  el.reviewButton.disabled = mutationBusy() || !state.selectedModelId;
}

function changeRefreshInterval() {
  const seconds = Number(el.refreshInterval.value);
  if (!Number.isInteger(seconds) || seconds < MIN_REFRESH_SECONDS || seconds > MAX_REFRESH_SECONDS) return;
  state.refreshSeconds = seconds;
  state.nextRefreshAt = Date.now() + seconds * 1000;
  scheduleRefresh();
}

function scheduleRefresh() {
  clearInterval(state.refreshTimer);
  clearInterval(state.countdownTimer);
  if (!state.token || document.hidden) {
    el.refreshMeta.textContent = document.hidden ? "Automatic refresh paused while hidden" : "Automatic refresh unavailable";
    return;
  }
  state.nextRefreshAt = Date.now() + state.refreshSeconds * 1000;
  state.refreshTimer = window.setInterval(() => refreshStatus(false), state.refreshSeconds * 1000);
  state.countdownTimer = window.setInterval(updateCountdown, 1000);
  updateCountdown();
}

function updateCountdown() {
  if (document.hidden) {
    el.refreshMeta.textContent = "Automatic refresh paused while hidden";
    return;
  }
  const remaining = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
  el.refreshMeta.textContent = `Next refresh in ${remaining} second${remaining === 1 ? "" : "s"}`;
}

function handleVisibilityChange() {
  if (document.hidden) {
    clearInterval(state.refreshTimer);
    clearInterval(state.countdownTimer);
    updateCountdown();
  } else if (state.token) {
    refreshStatus(true);
    scheduleRefresh();
  }
}

function lockConsole() {
  state.token = "";
  state.config = null;
  state.status = null;
  state.selectedModelId = null;
  sessionStorage.removeItem(TOKEN_KEY);
  clearInterval(state.refreshTimer);
  clearInterval(state.countdownTimer);
  clearTimeout(state.operationTimer);
  clearInterval(state.elapsedTimer);
  state.operationUrl = null;
  state.operation = null;
  el.main.hidden = true;
  el.lockButton.hidden = true;
  el.operationPanel.hidden = true;
  el.documentState.textContent = "Console locked";
  showNotice("", false);
  showTokenDialog("Console locked. Enter the token to begin a new tab session.");
}

function handleUnauthorized() {
  lockConsole();
  el.tokenCopy.textContent = "Authentication expired or was rejected. Replace the token to reconnect.";
  el.tokenError.textContent = "The server returned an authentication error.";
  el.tokenError.hidden = false;
}

function showTokenDialog(message) {
  if (message) el.tokenCopy.textContent = message;
  if (!el.tokenDialog.open) el.tokenDialog.showModal();
  window.setTimeout(() => el.tokenInput.focus(), 0);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      "X-Hal-Token": state.token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error("API request failed");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function messageForError(error, fallback) {
  if (!navigator.onLine) return "This browser is offline. No request was sent.";
  if (error.status === 400) return safeReason(error.payload && error.payload.error, "The request was invalid.");
  if (error.status === 403) return "This browser origin is not permitted to make changes.";
  if (error.status === 404) return "The requested resource is no longer available.";
  if (error.status === 409) return safeReason(error.payload && error.payload.error, "The request conflicts with the current state.");
  return fallback;
}

function showNotice(message, error) {
  el.notice.textContent = message;
  el.notice.hidden = !message;
  el.notice.classList.toggle("error", Boolean(error));
}

function setGlobalHealth(text, status) {
  el.globalHealth.textContent = text;
  el.globalHealth.className = `status-label status-${statusClass(status)}`;
}

function statusBadge(text, status = text) {
  return create("span", `status-label status-${statusClass(status)}`, titleCase(text));
}

function statusClass(value) {
  const normalized = String(value || "unknown").toLowerCase();
  const allowed = new Set(["active", "inactive", "activating", "deactivating", "failed", "unknown", "healthy", "unhealthy", "unchecked", "running", "queued", "succeeded", "done", "pending", "skipped"]);
  if (normalized === "done") return "succeeded";
  if (normalized === "pending") return "unknown";
  return allowed.has(normalized) ? normalized : "unknown";
}

function selectedModel() {
  return state.config && state.config.models.find((model) => model.id === state.selectedModelId);
}

function isLocalOperationUrl(value) {
  return typeof value === "string" && /^\/api\/operations\/[a-zA-Z0-9_-]+$/.test(value);
}

function safeReason(value, fallback) {
  return typeof value === "string" && value.length <= 300 ? value : fallback;
}

function safeText(value, fallback) {
  if (typeof value === "string" && value) return value;
  if (Number.isFinite(value)) return String(value);
  return fallback;
}

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function replaceChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }
function formatPercent(value) { return Number(value).toFixed(Number(value) % 1 === 0 ? 0 : 1); }
function percentOrDash(value) { return Number.isFinite(value) ? `${formatPercent(value)}%` : "--"; }
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "--";
  const units = ["B", "KB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1000 && index < units.length - 1) { value /= 1000; index += 1; }
  return `${value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}
function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "Not specified";
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes} min ${remainder} sec` : `${minutes} min`;
}
function formatInterval(seconds) { return seconds < 60 ? `${seconds} seconds` : seconds === 60 ? "60 seconds" : `${seconds / 60} minutes`; }
function formatGpuAllocation(gpus) { return Array.isArray(gpus) && gpus.length ? gpus.map((gpu) => `GPU ${gpu}`).join(" + ") : "None"; }
function formatTime(date) { return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date); }
function titleCase(value) { return humanize(value).replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function humanize(value) { return String(value || "unknown").replace(/[-_]/g, " "); }
