"use strict";

const BUILD_ID = "20260924-3";
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
  tokenRequired: true,
  config: null,
  status: null,
  focusedModelId: null,
  submittingModelId: null,
  submittingMutation: false,
  refreshSeconds: 60,
  nextRefreshAt: 0,
  refreshTimer: null,
  countdownTimer: null,
  operationTimer: null,
  elapsedTimer: null,
  operation: null,
  operationUrl: null,
  operationFocusReturn: null,
  operationFocusSet: false,
  comfyControlMode: "auto",
  refreshing: false,
};

const el = {};

document.addEventListener("DOMContentLoaded", initialize);

function initialize() {
  const ids = [
    "server-name", "global-health", "last-contact", "lock-button", "notice", "main",
    "refresh-interval", "refresh-button", "refresh-meta", "cpu-card", "memory-card",
    "storage-card", "gpu-cards", "server-specs", "operation-backdrop", "operation-panel", "operation-status",
    "operation-elapsed", "operation-message", "operation-steps", "operation-final",
    "dismiss-operation", "conflict-warning", "services-body", "model-list",
    "document-state", "token-dialog", "token-form", "token-input", "token-copy", "token-error",
    "unlock-button", "live-region",
  ];
  ids.forEach((id) => { el[toCamel(id)] = document.getElementById(id); });

  el.tokenForm.addEventListener("submit", submitToken);
  el.tokenDialog.addEventListener("cancel", (event) => event.preventDefault());
  el.lockButton.addEventListener("click", lockConsole);
  el.refreshButton.addEventListener("click", () => refreshStatus(true));
  // The operation-dismiss control is optional: an older cached index.html may
  // not include it, so only bind and use it when it is present. This keeps a
  // mixed cached-asset version from throwing during initialization.
  if (el.dismissOperation) el.dismissOperation.addEventListener("click", dismissOperation);
  el.refreshInterval.addEventListener("change", changeRefreshInterval);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  document.addEventListener("keydown", handleOperationDialogKey);
  window.addEventListener("online", () => refreshStatus(true));
  window.addEventListener("offline", () => setGlobalHealth("Offline", "failed"));

  checkPublicHealth();
  beginSession();
}

// Decide the authentication flow before touching any protected endpoint. The
// auth-mode probe is fetched without a token so the console can bypass the
// token dialog entirely when the backend runs in reverse-proxy mode.
async function beginSession() {
  let tokenRequired;
  try {
    tokenRequired = await fetchAuthMode();
  } catch (_) {
    // Fail closed: if the mode is unknown, keep token auth and let the user
    // retry by entering a token. This never weakens security.
    state.tokenRequired = true;
    showTokenDialog("The console could not determine its authentication mode. Check the connection or enter a token to retry.");
    return;
  }
  state.tokenRequired = tokenRequired;
  if (!tokenRequired) {
    await startDelegatedSession();
    return;
  }
  if (state.token) {
    unlockWithStoredToken();
  } else {
    showTokenDialog();
  }
}

// Fetch /api/auth-mode with no credentials. Resolves to the token_required
// boolean; rejects on network errors or an unexpected payload.
async function fetchAuthMode() {
  const response = await fetch("/api/auth-mode", { cache: "no-store" });
  if (!response.ok) throw new Error("auth-mode unavailable");
  const payload = await response.json();
  if (typeof payload.token_required !== "boolean") throw new Error("unexpected auth-mode payload");
  return payload.token_required;
}

// Reverse-proxy mode: load the console immediately, hide the Lock control, and
// label the session as delegated. No token is ever sent.
async function startDelegatedSession() {
  try {
    await loadConsole();
  } catch (_) {
    el.main.hidden = false;
    el.lockButton.hidden = true;
    el.lockButton.disabled = true;
    el.documentState.textContent = "Authentication delegated to reverse proxy";
    setGlobalHealth("Contact failed", "failed");
    showNotice("Authentication is delegated to the reverse proxy, but the console could not load. Use Refresh now to retry.", true);
  }
}

function hasActiveSession() {
  return state.tokenRequired ? Boolean(state.token) : true;
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
  if (state.tokenRequired) {
    el.lockButton.hidden = false;
    el.lockButton.disabled = false;
    el.documentState.textContent = "Authenticated session";
  } else {
    el.lockButton.hidden = true;
    el.lockButton.disabled = true;
    el.documentState.textContent = "Authentication delegated to reverse proxy";
  }
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
  if (!hasActiveSession() || state.refreshing || (document.hidden && !manual)) return;
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
      beginOperationPolling(status.active_operation.status_url, status.active_operation.target_model_id);
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
    else controlCell.append(create("span", "control-reason", "Activate a model card to switch"));
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
  const isComfy = spec.id === "comfyui";
  const auto = isComfy ? create("button", "quiet-button", "Auto") : null;
  const on = create("button", "quiet-button", "On");
  const off = create("button", "quiet-button", "Off");
  if (auto) auto.type = "button";
  on.type = off.type = "button";
  const conflict = activeConflictForService(spec);
  const busy = mutationBusy();
  const indeterminate = observed.state === "unknown" || observed.state === "deactivating";
  on.disabled = busy || indeterminate || (!isComfy && ACTIVE_STATES.has(observed.state)) || Boolean(conflict);
  off.disabled = busy || indeterminate || (!isComfy && (observed.state === "inactive" || observed.state === "failed"));
  if (isComfy) {
    auto.disabled = busy || indeterminate;
    [auto, on, off].forEach((button) => button.setAttribute("aria-pressed", "false"));
    const selected = state.comfyControlMode === "on" ? on : state.comfyControlMode === "off" ? off : auto;
    selected.setAttribute("aria-pressed", "true");
    auto.addEventListener("click", () => selectComfyAuto(observed));
    on.addEventListener("click", () => setService(spec.id, true, "on"));
    off.addEventListener("click", () => setService(spec.id, false, "off"));
    controls.append(auto, on, off);
  } else {
    on.addEventListener("click", () => setService(spec.id, true));
    off.addEventListener("click", () => setService(spec.id, false));
    controls.append(on, off);
  }
  wrapper.append(controls);
  let reason = "Choose an explicit desired state.";
  if (isComfy && state.comfyControlMode === "auto") {
    const model = activeHealthyModel();
    reason = model
      ? `Auto follows the active inference system. ${model.display_name} policy: ${model.comfyui_default ? "On" : "Off"}.`
      : "Auto follows the active inference system. It will apply on the next model switch.";
    if (busy) reason += " A change is currently in progress.";
  } else if (busy) reason = "Unavailable while another operation is running.";
  else if (conflict) reason = `On is blocked while ${conflict.display_name} is active.`;
  else if (!isComfy && ACTIVE_STATES.has(observed.state)) reason = "Already on; only Off is available.";
  else if (!isComfy && (observed.state === "inactive" || observed.state === "failed")) reason = "Already not running; only On is available.";
  else if (isComfy) reason = `${titleCase(state.comfyControlMode)} is selected. Auto follows the active inference system.`;
  else if (indeterminate) reason = "Control unavailable until a stable state can be observed.";
  wrapper.append(create("span", "control-reason", reason));
  return wrapper;
}

function activeHealthyModel() {
  if (!state.config || !state.status) return null;
  const services = (state.status.services && state.status.services.items) || [];
  const observedById = new Map(services.map((item) => [item.id, item]));
  return state.config.models.find((model) => {
    const observed = observedById.get(model.id);
    return observed && observed.state === "active" && observed.healthy === true;
  }) || null;
}

function observedActiveState(observed) {
  if (observed && ACTIVE_STATES.has(observed.state)) return true;
  if (observed && (observed.state === "inactive" || observed.state === "failed")) return false;
  return null;
}

function selectComfyAuto(observed) {
  if (mutationBusy()) return;
  state.comfyControlMode = "auto";
  renderServices();
  const model = activeHealthyModel();
  if (!model) {
    el.liveRegion.textContent = "ComfyUI Auto selected. It will apply on the next model switch.";
    return;
  }
  const desired = Boolean(model.comfyui_default);
  const active = observedActiveState(observed);
  el.liveRegion.textContent = `ComfyUI Auto selected. ${model.display_name} policy is ${desired ? "On" : "Off"}.`;
  if (active !== null && active !== desired) setService("comfyui", desired);
}

function activeConflictForService(service) {
  if (!state.status) return null;
  const services = (state.status.services && state.status.services.items) || [];
  const activeIds = new Set(
    services
      .filter((item) => item.kind === "model" && ACTIVE_STATES.has(item.state))
      .map((item) => item.id)
  );
  return state.config.models.find((model) => service.conflicts_with.includes(model.id) && activeIds.has(model.id)) || null;
}

function renderModels() {
  if (!state.config) return;
  const previouslyFocusedId = document.activeElement && document.activeElement.dataset.modelId;
  const services = (state.status && state.status.services && state.status.services.items) || [];
  const observedById = new Map(services.map((item) => [item.id, item]));
  const loadingModelId = currentLoadingModelId();
  const busy = mutationBusy();
  replaceChildren(el.modelList);
  state.config.models.forEach((model) => {
    const card = create("div", "model-card");
    card.dataset.modelId = model.id;
    const observed = observedById.get(model.id);
    const loading = loadingModelId === model.id;
    const active = !loading && observed && observed.state === "active" && observed.healthy === true;
    const starting = !loading && observed && (observed.state === "activating" || (observed.state === "active" && observed.healthy !== true));
    card.classList.toggle("active", active);
    card.classList.toggle("loading", loading);
    card.setAttribute("role", "button");
    const stateLabel = loading ? ", loading" : active ? ", active and healthy" : starting ? ", starting; health pending" : "";
    card.setAttribute("aria-label", `Switch to ${model.display_name}${stateLabel}`);
    const focusId = state.focusedModelId || (state.config.models[0] && state.config.models[0].id);
    card.tabIndex = model.id === focusId ? 0 : -1;
    if (busy) card.setAttribute("aria-disabled", "true");
    const top = create("div", "model-card-top");
    top.append(create("span", "metric-kicker", model.id));
    if (loading) top.append(statusBadge("Loading", "loading"));
    else if (active) top.append(statusBadge("Active", "active"));
    else if (starting) top.append(statusBadge(observed.state === "active" ? "Health pending" : "Starting", "starting"));
    card.append(top, create("h3", null, model.display_name), create("p", "model-synopsis", model.synopsis));
    const meta = create("dl", "model-meta");
    meta.append(metaItem("GPU allocation", formatGpuAllocation(model.gpus)), metaItem("Load window", formatLoadWindow(model)));
    card.append(meta);
    const strengths = create("ul", "strength-list");
    const entries = Array.isArray(model.strengths) && model.strengths.length ? model.strengths : ["General workloads"];
    entries.forEach((strength) => strengths.append(create("li", null, strength)));
    card.append(strengths);
    card.addEventListener("click", () => activateModel(model));
    card.addEventListener("keydown", (event) => handleModelKey(event, model.id));
    el.modelList.append(card);
  });
  if (previouslyFocusedId) {
    const focusedCard = Array.from(el.modelList.children).find((card) => card.dataset.modelId === previouslyFocusedId);
    if (focusedCard) focusedCard.focus({ preventScroll: true });
  }
}

function metaItem(term, value) {
  const wrapper = create("div");
  wrapper.append(create("dt", null, term), create("dd", null, value));
  return wrapper;
}

function handleModelKey(event, modelId) {
  if (event.repeat) return;
  const models = state.config.models;
  const index = models.findIndex((model) => model.id === modelId);
  let next = null;
  if (["ArrowRight", "ArrowDown"].includes(event.key)) next = models[(index + 1) % models.length];
  if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = models[(index - 1 + models.length) % models.length];
  if (next) {
    event.preventDefault();
    moveModelFocus(next.id);
    return;
  }
  if (event.key !== " " && event.key !== "Enter") return;
  event.preventDefault();
  activateModel(models[index]);
}

function moveModelFocus(modelId) {
  state.focusedModelId = modelId;
  Array.from(el.modelList.children).forEach((card, index) => {
    const focused = state.config.models[index].id === modelId;
    card.tabIndex = focused ? 0 : -1;
    if (focused) card.focus({ preventScroll: true });
  });
}

async function activateModel(model) {
  if (!model || mutationBusy()) return;
  state.comfyControlMode = "auto";
  state.focusedModelId = model.id;
  state.submittingModelId = model.id;
  state.submittingMutation = true;
  el.liveRegion.textContent = `Loading ${model.display_name}. Model switch submitted.`;
  renderModels();
  renderServices();
  await submitMutation("/api/switch", "POST", { model_id: model.id, comfyui: null }, model.id);
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

async function setService(serviceId, active, comfyMode = null) {
  if (mutationBusy()) return;
  if (serviceId === "comfyui" && comfyMode) state.comfyControlMode = comfyMode;
  state.submittingMutation = true;
  renderServices();
  renderModels();
  await submitMutation(`/api/services/${encodeURIComponent(serviceId)}`, "PUT", { active });
}

async function submitMutation(url, method, body, targetModelId = null) {
  setMutationsDisabled(true);
  try {
    const response = await api(url, { method, body: JSON.stringify(body) });
    state.submittingModelId = null;
    state.submittingMutation = false;
    beginOperationPolling(response.status_url, response.target_model_id || targetModelId);
  } catch (error) {
    state.submittingModelId = null;
    state.submittingMutation = false;
    if (error.status === 401) {
      handleUnauthorized();
      return;
    }
    if (error.status === 409 && error.payload && error.payload.status_url) {
      showNotice("Another operation is active. Following its progress instead.", false);
      beginOperationPolling(error.payload.status_url, error.payload.target_model_id);
      return;
    }
    const conflicts = error.payload && Array.isArray(error.payload.conflicts_with)
      ? ` Conflicting models: ${error.payload.conflicts_with.join(", ")}.`
      : "";
    showNotice(messageForError(error, "The requested change was not accepted.") + conflicts, true);
    setMutationsDisabled(false);
  }
}

function beginOperationPolling(statusUrl, targetModelId = null) {
  if (!isLocalOperationUrl(statusUrl)) {
    showNotice("The operation response could not be followed safely.", true);
    setMutationsDisabled(false);
    return;
  }
  clearTimeout(state.operationTimer);
  state.operationUrl = statusUrl;
  state.operation = { status: "queued", steps: [], created_at: new Date().toISOString(), target_model_id: targetModelId };
  renderOperation();
  renderModels();
  pollOperation();
}

async function pollOperation() {
  if (!state.operationUrl) return;
  try {
    const previousTarget = state.operation && state.operation.target_model_id;
    const operation = await api(state.operationUrl);
    state.operation = { ...operation, target_model_id: operation.target_model_id || previousTarget || null };
    renderOperation();
    renderModels();
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
  showOperationOverlay();
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
  if (el.dismissOperation) el.dismissOperation.hidden = !TERMINAL_STATES.has(operation.status);
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
  hideOperationOverlay(true);
  clearInterval(state.elapsedTimer);
}

function showOperationOverlay() {
  if (el.operationPanel.hidden) {
    const active = document.activeElement;
    state.operationFocusReturn = active && active !== document.body ? active : null;
    el.operationPanel.hidden = false;
    // The backdrop is optional: an older cached index.html may not include it.
    if (el.operationBackdrop) el.operationBackdrop.hidden = false;
    document.body.classList.add("operation-overlay-active");
    // Scroll the freshly shown panel to the top before moving focus into it.
    el.operationPanel.scrollTop = 0;
  }
  if (!state.operationFocusSet) {
    state.operationFocusSet = true;
    window.requestAnimationFrame(() => el.operationPanel.focus({ preventScroll: true }));
  }
}

function hideOperationOverlay(restoreFocus) {
  el.operationPanel.hidden = true;
  if (el.operationBackdrop) el.operationBackdrop.hidden = true;
  document.body.classList.remove("operation-overlay-active");
  state.operationFocusSet = false;
  if (restoreFocus) {
    let target = state.operationFocusReturn;
    if (!target || !target.isConnected) {
      target = Array.from(el.modelList.children).find((card) => card.dataset.modelId === state.focusedModelId) || el.refreshButton;
    }
    if (target && !target.hidden) target.focus({ preventScroll: true });
  }
  state.operationFocusReturn = null;
}

function handleOperationDialogKey(event) {
  if (el.operationPanel.hidden || event.key !== "Tab") return;
  const focusable = Array.from(el.operationPanel.querySelectorAll("button:not([disabled]):not([hidden])"));
  if (!focusable.length) {
    event.preventDefault();
    el.operationPanel.focus({ preventScroll: true });
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === el.operationPanel)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function mutationBusy() {
  return Boolean(state.submittingMutation || state.submittingModelId || state.operationUrl || (state.status && state.status.active_operation));
}

function currentLoadingModelId() {
  if (state.submittingModelId) return state.submittingModelId;
  if (state.operation && !TERMINAL_STATES.has(state.operation.status) && state.operation.target_model_id) {
    return state.operation.target_model_id;
  }
  const active = state.status && state.status.active_operation;
  return active && active.target_model_id ? active.target_model_id : null;
}

function setMutationsDisabled(disabled) {
  Array.from(el.servicesBody.querySelectorAll("button")).forEach((button) => { button.disabled = disabled || button.disabled; });
  if (!disabled) {
    renderServices();
    renderModels();
  }
}

function updateMutationAvailability() {
  if (!mutationBusy()) renderServices();
  renderModels();
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
  if (!hasActiveSession() || document.hidden) {
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
  } else if (hasActiveSession()) {
    refreshStatus(true);
    scheduleRefresh();
  }
}

function lockConsole() {
  state.token = "";
  state.config = null;
  state.status = null;
  state.focusedModelId = null;
  state.submittingModelId = null;
  state.submittingMutation = false;
  sessionStorage.removeItem(TOKEN_KEY);
  clearInterval(state.refreshTimer);
  clearInterval(state.countdownTimer);
  clearTimeout(state.operationTimer);
  clearInterval(state.elapsedTimer);
  state.operationUrl = null;
  state.operation = null;
  el.main.hidden = true;
  el.lockButton.hidden = true;
  hideOperationOverlay(false);
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
  const headers = {};
  // Only send the token when token auth is in play. In reverse-proxy mode the
  // header is omitted entirely (never sent empty).
  if (state.tokenRequired) {
    headers["X-Hal-Token"] = state.token;
  }
  if (options.body) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers,
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
  const allowed = new Set(["active", "inactive", "activating", "deactivating", "failed", "unknown", "healthy", "unhealthy", "unchecked", "running", "queued", "succeeded", "done", "pending", "skipped", "loading", "starting"]);
  if (normalized === "done") return "succeeded";
  if (normalized === "pending") return "unknown";
  return allowed.has(normalized) ? normalized : "unknown";
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
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
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
function formatLoadWindow(model) {
  const minimum = model.startup_eta_min_seconds;
  const maximum = model.startup_eta_seconds;
  if (!Number.isFinite(minimum)) return formatDuration(maximum);
  if (!Number.isFinite(maximum) || maximum === minimum) return formatDuration(minimum);
  if (minimum >= 60 && maximum >= 60 && minimum % 60 === 0 && maximum % 60 === 0) {
    return `${minimum / 60}-${maximum / 60} min`;
  }
  return `${formatDuration(minimum)}-${formatDuration(maximum)}`;
}
function formatGpuAllocation(gpus) { return Array.isArray(gpus) && gpus.length ? gpus.map((gpu) => `GPU ${gpu}`).join(" + ") : "None"; }
function formatTime(date) { return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date); }
function titleCase(value) { return humanize(value).replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function humanize(value) { return String(value || "unknown").replace(/[-_]/g, " "); }
