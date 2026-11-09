import { RunProgressView } from "/progress.js";
import { PromptControls, runStopNotice } from "/workflow.js";
("use strict");
const element = (id) => document.getElementById(id);

function friendlyEvent(line) {
  const stamp = line.match(/^\d{2}:\d{2}:\d{2}\s+/)?.[0] || "";
  const body = line.slice(stamp.length);
  if (body.startsWith("Geometry valid:")) return stamp + "路线与时长检查通过。";
  if (body.startsWith("离线输入已导出到")) return stamp + "离线输入已生成。";
  if (body.startsWith("检查报告保存在")) return stamp + "离线校验报告已保存。";
  return line;
}
let status = null;
let parametersRestored = false;
let parametersEdited = false;
let frameId = null;
let imageUrl = null;
let currentPrompt = null;
const progressView = new RunProgressView();
let requestingFrame = false;
let lastFrameRequestedAt = -Infinity;
let shutdownRequested = false;
let shutdownAcknowledged = false;
let shutdownFinished = false;
let toastTimer;
let notificationAudio = null;
let lastStopNoticeKey = null;
document.addEventListener("pointerdown", () => {
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio || notificationAudio) return;
  try {
    notificationAudio = new Audio();
    notificationAudio.resume().catch(() => {});
  } catch { notificationAudio = null; }
}, { once: true });

function soundStopNotice() {
  if (!notificationAudio || notificationAudio.state !== "running") return;
  for (const delay of [0, 0.3, 0.6]) {
    const oscillator = notificationAudio.createOscillator();
    const gain = notificationAudio.createGain();
    const start = notificationAudio.currentTime + delay;
    oscillator.frequency.value = 740;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(0.12, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.001, start + 0.2);
    oscillator.connect(gain);
    gain.connect(notificationAudio.destination);
    oscillator.start(start);
    oscillator.stop(start + 0.22);
    oscillator.addEventListener("ended", () => { oscillator.disconnect(); gain.disconnect(); }, { once: true });
  }
}
const promptControls = new PromptControls(element("answer-controls"), async (answer, identity) => {
  if (status?.job?.id !== identity.job_id || status?.job?.prompt?.id !== identity.prompt_id)
    throw new Error("步骤已更新，请按当前提示操作");
  await api("/api/answer", { ...identity, answer });
  await refreshStatus();
}, (error) => toast(error.message));

function toast(message) {
  element("toast").textContent = message;
  element("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element("toast").hidden = true;
  }, 5500);
}

async function api(path, data) {
  const options =
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败，请重试");
  return result;
}

function paceString(seconds) {
  return `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}`;
}

function settings() {
  return {
    mode: element("run-mode").value,
    pace: Number(element("pace").value),
    cadence: Number(element("cadence").value),
    distance: Number(element("distance").value),
    auto_cadence: element("auto-cadence").checked,
  };
}

function matchesGenerated() {
  if (!status?.generated) return false;
  const current = settings();
  const previous = status.generated.fingerprint.settings;
  return ["pace", "cadence", "distance"].every((key) => current[key] === previous[key]);
}

function updateParameters() {
  const pace = Number(element("pace").value);
  if (document.activeElement !== element("pace-text"))
    element("pace-text").value = paceString(pace);
  if (element("auto-cadence").checked)
    element("cadence").value = Math.max(140, Math.min(190, Math.round(225 - 0.2 * pace)));
  const values = settings();
  const duration = (values.distance * values.pace) / 1000;
  element("duration").textContent =
    `${Math.floor(duration / 60)} 分 ${Math.round((duration % 60) * 10) / 10} 秒`;
  let error = "";
  if (!Number.isFinite(values.distance) || values.distance < 1000 || values.distance > 1200)
    error = "计划距离应为 1000～1200 米。";
  else if (!Number.isFinite(values.cadence) || values.cadence < 120 || values.cadence > 210)
    error = "参考步频应为 120～210 步/分。";
  else if (duration > 300) error = "当前计划超过 5 分钟，请减少距离或提高配速。";
  element("parameter-error").hidden = !error;
  element("parameter-error").textContent = error;
  const busy = status?.job && ["running", "waiting", "stopping"].includes(status.job.state);
  for (const id of [
    "pace",
    "pace-text",
    "pace-faster",
    "pace-slower",
    "cadence",
    "distance",
    "auto-cadence",
    "prepare",
    "run-mode",
  ])
    element(id).disabled = Boolean(busy);
  element("generate").disabled = Boolean(error || busy || status?.reference_ready === false);
  element("generate").firstChild.textContent =
    busy && status.job.action === "generate" ? "正在生成… " : "生成路线与动作 ";
  const outdoor = values.mode === "outdoor";
  const activeRun = busy && status.job.action === "run";
  const modeBlock = outdoor ? status?.outdoor_blocked : status?.run_blocked;
  const usedPlan = status?.generated?.used_for_run
    ? "本次计划已用于准备，请重新生成下一次节奏。"
    : null;
  const blocked = activeRun ? null : modeBlock || usedPlan;
  element("run").textContent = outdoor ? "准备户外测试" : "准备校园跑";
  element("run").disabled = Boolean(error || busy || !matchesGenerated() || blocked);
  element("run").title = blocked || "";
  element("notice").hidden = !blocked && !outdoor;
  element("notice").textContent = blocked || (outdoor ? "户外测试 · 先在 App 选择户外跑，结果单独登记。" : "");
  element("check-checkpoints").textContent = outdoor ? "户外模式" : "待核实";
  element("run-reason").textContent =
    blocked ||
    (matchesGenerated()
      ? outdoor
        ? "点准备后自动初始化定位；GPS 变绿后在 App 开始，倒计时结束再同步。"
        : "点准备后自动初始化定位；核对地图，GPS 变绿后在 App 开始，再同步。"
      : status?.generated
        ? "参数已变化，请重新生成后继续。"
        : "生成并检查后可继续。");
  progressView.setParametersMatch(matchesGenerated());
  if (status?.generated && !matchesGenerated())
    element("generation-status").textContent = "参数已调整";
}

function drawPreview(generated) {
  progressView.setPlan({ ...generated, map_available: status.map_available });
}

function render() {
  if (!parametersRestored) {
    parametersRestored = true;
    const saved = status.generated?.fingerprint?.settings;
    if (saved && !parametersEdited) {
      for (const key of ["pace", "cadence", "distance"]) element(key).value = saved[key];
      element("auto-cadence").checked = saved.cadence === Math.max(140, Math.min(190, Math.round(225 - .2 * saved.pace)));
      updateParameters();
    }
  }
  element("connection").textContent = "本机已连接";
  element("connection").classList.add("connected");
  element("prepare").hidden = status.reference_ready;
  if (status.generated) {
    drawPreview(status.generated);
    element("generation-status").textContent = matchesGenerated() ? "离线检查通过" : "参数已调整";
  }
  const job = status.job;
  const activeRun = job?.action === "run" && ["running", "waiting", "stopping"].includes(job.state);
  if (activeRun)
    element("run-mode").value = job.mode || "campus";
  progressView.setJob(job);
  const blockingRecords = status.pending.some((record) => !record.optional);
  element("workflow").hidden = !job || (job.action === "record_result" && !["running", "waiting", "stopping"].includes(job.state));
  element("session-checks").hidden = blockingRecords && !activeRun;
  element("check-model").textContent = status.generated?.checks?.passed
    ? matchesGenerated()
      ? "通过"
      : "需重新生成"
    : "待生成";
  element("check-model").className =
    status.generated?.checks?.passed && matchesGenerated() ? "ok" : "";
  element("check-input").textContent =
    job?.action === "run"
      ? {
          running: job.telemetry?.phase === "sending" ? "输入中" : "准备中",
          waiting: "等待确认",
          stopping: "恢复中",
          finished: "已结束",
          failed: "需核验",
          stopped: "已停止",
        }[job.state] || "待核验"
      : "未开始";
  element("check-account").textContent =
    {
      wechat_expired: "需要重新登录",
      sportworld_foreground: "App 已打开",
      other_app: "App 未在前台",
      unknown: "待检查",
    }[status.device_status] || "待检查";
  element("check-account").className = status.device_status === "wechat_expired" ? "warn" : "";
  element("open-viewer").hidden = status.device_status !== "wechat_expired";
  element("open-app").hidden = status.device_status === "wechat_expired";
  element("footer-mode").textContent =
    job?.state === "waiting"
      ? "等待你的操作确认"
      : job?.action === "run" && job.state === "running" && job.telemetry?.phase === "sending"
        ? "输入进行中 · 页面显示输入程序数据"
        : "就绪 · 输入数据与 App 成绩分别核验";
  if (!job) {
    element("stop").hidden = true;
    element("job-title").textContent = "运行记录";
    element("latest-event").textContent = "输入尚未开始。可以调整参数并生成离线计划。";
  }
  if (job) {
    const labels = {
      generate: "生成与检查",
      run: job.mode === "outdoor" ? "户外测试" : "跑步操作",
      record_result: "登记 App 结果",
      prepare_reference: "准备公开参考",
    };
    element("job-title").textContent = labels[job.action] || "操作进度";
    const busy = ["running", "waiting", "stopping"].includes(job.state);
    element("stop").hidden = !busy;
    element("stop").disabled = job.state === "stopping";
    element("stop").textContent = job.state === "stopping" ? "正在停止…" : "停止并恢复";
    element("events").textContent = job.events.map(friendlyEvent).join("\n");
    element("latest-event").textContent =
      job.state === "stopping"
        ? "正在停止输入并等待恢复结果。"
        : friendlyEvent(job.events.at(-1) || "正在准备…");
    element("prompt-box").hidden = !job.prompt;
    if (job.prompt && currentPrompt !== job.prompt.id) {
      currentPrompt = job.prompt.id;
      element("prompt-text").textContent = job.prompt.text;
      promptControls.render(job.prompt, job.id);
      element("prompt-box").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
  const recentResults = status.recent_results || [];
  element("pending").hidden = (!status.pending.length && !recentResults.length) || activeRun;
  element("pending").classList.toggle("optional-results", !blockingRecords);
  element("pending-title").textContent = blockingRecords ? "运行记录需核对" : "运行记录";
  element("pending-help").textContent = blockingRecords
    ? "中断恢复和校园跑计分状态仍需核实。"
    : "户外测试的结果补记可选，不影响下次测试。这里仅保存本地核对信息。";
  const list = element("pending-list");
  const ids = JSON.stringify([status.pending, recentResults]);
  if (list.dataset.ids !== ids) {
    list.dataset.ids = ids;
    list.replaceChildren();
    for (const pending of status.pending) {
      const button = document.createElement("button");
      button.textContent = `${pending.date} · ${pending.optional ? "补记结果（可选）" : "核对本次记录"}`;
      button.addEventListener("click", () => action("record_result", { run_id: pending.id }));
      list.append(button);
      if (pending.preparation_cancellable) {
        const cancel = document.createElement("button");
        cancel.textContent = `${pending.date} · App 已回到首页，取消本次准备`;
        cancel.addEventListener("click", async () => {
          try {
            await api("/api/cancel-preparation", { run_id: pending.id });
            await refreshStatus();
          } catch (error) {
            toast(error.message);
          }
        });
        list.append(cancel);
      }
      if (pending.resumable) {
        const resume = document.createElement("button");
        resume.textContent = `${pending.date} · 继续补足本次户外跑`;
        resume.addEventListener("click", () => action("resume", { run_id: pending.id }));
        list.append(resume);
      }
    }
    for (const record of recentResults) {
      const row = document.createElement("p");
      row.className = "helper";
      const details = [];
      if (record.distance_km != null) details.push(`${record.distance_km} km`);
      if (record.duration_s != null) details.push(paceString(record.duration_s));
      if (record.pace_s_km != null) details.push(`${paceString(record.pace_s_km)} /km`);
      row.textContent = `${record.date} · ${record.label}${details.length ? " · " + details.join(" / ") : ""}`;
      list.append(row);
    }
  }
  updateParameters();
}

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let workspaceTransition = null;
function transitionWorkspace(update) {
  if (workspaceTransition) workspaceTransition.skipTransition();
  if (!reducedMotion.matches && document.startViewTransition) {
    const transition = document.startViewTransition(update);
    workspaceTransition = transition;
    transition.finished.catch(() => {}).finally(() => {
      if (workspaceTransition === transition) workspaceTransition = null;
    });
  } else update();
}

const layouts = new Set(["standard", "track", "device"]);
function setLayout(value, animate = true) {
  const layout = layouts.has(value) ? value : "standard";
  const update = () => {
    document.querySelector(".app").dataset.layout = layout;
    element("layout-mode").value = layout;
  };
  if (animate) transitionWorkspace(update);
  else update();
  try { localStorage.setItem("running-console-layout", layout); } catch { /* Storage may be blocked. */ }
}
let savedLayout = "standard";
try { savedLayout = localStorage.getItem("running-console-layout"); } catch { /* Use default. */ }
setLayout(savedLayout, false);
element("layout-mode").addEventListener("change", (event) => setLayout(event.target.value));
element("layout-reset").addEventListener("click", () => setLayout("standard"));

// Delegation covers controls created later, including pending result actions.
function pressFeedback(target, clientX, clientY) {
  if (reducedMotion.matches || target.disabled || !target.animate) return;
  target.animate([
    { boxShadow: "inset 0 0 0 1px #fff, 0 0 20px #ffffff38", filter: "brightness(1.5)" },
    { boxShadow: "inset 0 0 0 1px #ffffff00, 0 0 0 #ffffff00", filter: "brightness(1)" },
  ], { duration: 650, easing: "cubic-bezier(.16,1,.3,1)" });
  if (target.tagName !== "BUTTON" && target.tagName !== "SUMMARY") return;
  const rect = target.getBoundingClientRect();
  const ripple = document.createElement("span");
  ripple.className = "press-wave";
  ripple.setAttribute("aria-hidden", "true");
  ripple.style.left = `${clientX == null ? rect.width / 2 : clientX - rect.left}px`;
  ripple.style.top = `${clientY == null ? rect.height / 2 : clientY - rect.top}px`;
  const diameter = Math.hypot(rect.width, rect.height) * 2;
  ripple.style.width = ripple.style.height = `${diameter}px`;
  target.append(ripple);
  ripple.animate([
    { transform: "translate(-50%, -50%) scale(0)", opacity: 0.5 },
    { transform: "translate(-50%, -50%) scale(1)", opacity: 0 },
  ], { duration: 700, easing: "cubic-bezier(.16,1,.3,1)" }).finished
    .catch(() => {}).finally(() => ripple.remove());
}
document.addEventListener("pointerdown", (event) => {
  const target = event.target.closest("button, select, input, summary, .input-chart, .device-screen, .checkbox");
  if (target && !target.querySelector("input:disabled"))
    pressFeedback(target, event.clientX, event.clientY);
});
document.addEventListener("keydown", (event) => {
  if ((event.key === "Enter" || event.key === " ") && !event.repeat) {
    const target = event.target.closest("button, summary");
    if (target) pressFeedback(target);
  }
});

function selectView(view) {
  document.querySelector(".app").dataset.view = view;
  element("route-panel-title").textContent = view === "calibration" ? "跑道校准" : "运行视图";
  for (const name of ["run", "calibration", "logs"]) {
    element(`view-${name}`).classList.toggle("selected", name === view);
    element(`view-${name}`).setAttribute("aria-pressed", String(name === view));
  }
  document.querySelector(".more-data").open = view === "calibration";
  if (view === "logs") element("events").closest("details").open = true;
}

for (const name of ["run", "calibration", "logs"])
  element(`view-${name}`).addEventListener("click", () => transitionWorkspace(() => selectView(name)));

document.addEventListener("keydown", (event) => {
  if (
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)
  )
    return;
  const views = { 1: "run", 2: "calibration", 3: "logs" };
  if (views[event.key]) {
    event.preventDefault();
    transitionWorkspace(() => selectView(views[event.key]));
  }
  if (event.key.toLowerCase() === "g" && !element("generate").disabled) {
    event.preventDefault();
    element("generate").click();
  }
  if (
    event.code === "Space" &&
    !element("play-preview").disabled &&
    !element("preview-player").hidden
  ) {
    event.preventDefault();
    element("play-preview").click();
  }
});

async function refreshStatus() {
  if (shutdownFinished) return;
  try {
    status = await api("/api/status");
    render();
    shutdownRequested = Boolean(status.closing);
    shutdownAcknowledged ||= shutdownRequested;
    document.querySelector(".app").classList.toggle("is-closing", shutdownRequested);
    element("shutdown").disabled = shutdownRequested;
    element("shutdown").textContent = shutdownRequested ? "正在退出…" : "⏻ 彻底退出";
    if (shutdownRequested) {
      element("connection").textContent = "正在彻底退出";
      element("notice").hidden = false;
      element("notice").textContent = "正在停止输入、恢复并关闭后台。无需继续操作，登录和已保存记录会保留。";
    } else if (status.shutdown_error) {
      element("notice").hidden = false;
      element("notice").textContent = status.shutdown_error;
      shutdownAcknowledged = false;
    } else {
      const message = runStopNotice(status);
      const notice = element("notice");
      notice.hidden = !message;
      const action = status.job?.telemetry?.operator_action;
      const urgent = Boolean(status.app_pause_required || action === "pause_app");
      notice.classList.toggle("run-stop-notice", Boolean(message) && urgent);
      notice.setAttribute("role", message && (urgent || action === "start_app") ? "alert" : "status");
      if (notice.textContent !== message) notice.textContent = message;
      if (message) {
        const pendingKey = status.pending.filter((record) => record.app_pause_required).map((record) => record.id).join(",");
        const actionKey = ["start_app", "pause_app"].includes(action) ? `${status.job.id}:${action}` : null;
        const key = pendingKey || actionKey;
        if (key && key !== lastStopNoticeKey) soundStopNotice();
        lastStopNoticeKey = key;
      } else lastStopNoticeKey = null;
    }
  } catch (error) {
    if (shutdownAcknowledged) {
      shutdownFinished = true;
      element("connection").textContent = "已退出";
      element("shutdown").textContent = "已退出";
      element("notice").hidden = false;
      element("notice").textContent = "控制台已关闭，可以关闭此页。下次双击“打开校园跑控制台.command”即可重新打开。";
    } else element("connection").textContent = "连接中断";
    element("connection").classList.remove("connected");
  }
}

async function action(name, extra = {}) {
  try {
    await api("/api/action", { action: name, ...settings(), ...extra });
    await refreshStatus();
  } catch (error) {
    toast(error.message);
  }
}

async function refreshFrame(force = true) {
  if (requestingFrame || document.hidden || shutdownRequested || shutdownFinished) return;
  const job = status?.job;
  const sending = job?.action === "run" && job.state === "running" && job.telemetry?.phase === "sending";
  const now = performance.now();
  if (!force && sending && now - lastFrameRequestedAt < 3000) return;
  lastFrameRequestedAt = now;
  requestingFrame = true;
  try {
    const response = await fetch("/api/frame");
    if (!response.ok) throw new Error((await response.json()).error);
    const nextId = response.headers.get("X-Frame-Id");
    const blob = await response.blob();
    const nextUrl = URL.createObjectURL(blob);
    const image = element("device-image");
    image.onload = () => {
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      imageUrl = nextUrl;
      frameId = nextId;
      image.hidden = false;
      element("screen-message").hidden = true;
      element("back").disabled = false;
    };
    image.src = nextUrl;
  } catch (error) {
    element("screen-message").hidden = false;
    element("screen-message").querySelector("p").textContent = error.message;
    element("device-image").hidden = true;
    element("back").disabled = true;
    frameId = null;
  } finally {
    requestingFrame = false;
  }
}

for (const id of ["pace", "pace-text", "cadence", "distance", "auto-cadence"])
  element(id).addEventListener("input", () => { parametersEdited = true; });
for (const id of ["pace-faster", "pace-slower"])
  element(id).addEventListener("click", () => { parametersEdited = true; });
element("pace").addEventListener("input", updateParameters);
element("pace-text").addEventListener("change", () => {
  const match = element("pace-text").value.match(/^(\d{1,2})[:：](\d{2})$/);
  const value = match ? Number(match[1]) * 60 + Number(match[2]) : NaN;
  if (!match || Number(match[2]) >= 60 || value < 200 || value > 300)
    toast("配速范围为 3:20～5:00，请按分:秒填写。");
  else element("pace").value = value;
  updateParameters();
});
element("pace-text").addEventListener("input", () => {
  const match = element("pace-text").value.match(/^(\d{1,2})[:：](\d{2})$/);
  if (!match || Number(match[2]) >= 60) return;
  const value = Number(match[1]) * 60 + Number(match[2]);
  if (value >= 200 && value <= 300) {
    element("pace").value = value;
    updateParameters();
  }
});
element("pace-faster").addEventListener("click", () => {
  element("pace").value = Math.max(200, Number(element("pace").value) - 5);
  updateParameters();
});
element("pace-slower").addEventListener("click", () => {
  element("pace").value = Math.min(300, Number(element("pace").value) + 5);
  updateParameters();
});
element("cadence").addEventListener("input", () => {
  element("auto-cadence").checked = false;
  updateParameters();
});
element("distance").addEventListener("input", updateParameters);
element("auto-cadence").addEventListener("change", updateParameters);
element("run-mode").addEventListener("change", updateParameters);
element("generate").addEventListener("click", () => action("generate"));
element("run").addEventListener("click", () => action("run"));
element("prepare").addEventListener("click", () => action("prepare_reference"));
element("shutdown").addEventListener("click", async () => {
  element("shutdown").disabled = true;
  shutdownRequested = true;
  try {
    await api("/api/shutdown", {});
    shutdownAcknowledged = true;
    await refreshStatus();
  } catch (error) {
    shutdownRequested = false;
    element("shutdown").disabled = false;
    toast(error.message);
  }
});
element("refresh").addEventListener("click", () => refreshFrame());
element("open-app").addEventListener("click", async () => {
  element("open-app").disabled = true;
  try {
    await api("/api/open-app", {});
    await refreshFrame();
  } catch (error) {
    toast(error.message);
  } finally {
    element("open-app").disabled = false;
  }
});
element("open-viewer").addEventListener("click", async () => {
  try {
    const result = await api("/api/open-viewer", {});
    toast(result.message);
  } catch (error) {
    toast(error.message);
  }
});
element("stop").addEventListener("click", async () => {
  try {
    await api("/api/stop", {});
    await refreshStatus();
  } catch (error) {
    toast(error.message);
  }
});
element("back").addEventListener("click", async () => {
  try {
    await api("/api/input", { kind: "back", frame_id: frameId });
    await refreshFrame();
  } catch (error) {
    toast(error.message);
  }
});
let pointer = null;
const image = element("device-image");
function imageCoordinates(event) {
  const rectangle = image.getBoundingClientRect();
  return {
    x: Math.max(
      0,
      Math.min(
        image.naturalWidth - 1,
        ((event.clientX - rectangle.left) / rectangle.width) * image.naturalWidth,
      ),
    ),
    y: Math.max(
      0,
      Math.min(
        image.naturalHeight - 1,
        ((event.clientY - rectangle.top) / rectangle.height) * image.naturalHeight,
      ),
    ),
  };
}
image.addEventListener("pointerdown", (event) => {
  if (!frameId) return;
  event.preventDefault();
  image.setPointerCapture(event.pointerId);
  pointer = { ...imageCoordinates(event), at: performance.now(), frame: frameId };
});
image.addEventListener("pointerup", async (event) => {
  if (!pointer) return;
  const start = pointer;
  pointer = null;
  const end = imageCoordinates(event),
    duration = Math.min(3000, Math.max(100, Math.round(performance.now() - start.at)));
  const swipe = Math.hypot(end.x - start.x, end.y - start.y) > 12 || duration > 650;
  try {
    await api("/api/input", {
      kind: swipe ? "swipe" : "tap",
      frame_id: start.frame,
      x: start.x,
      y: start.y,
      end_x: end.x,
      end_y: end.y,
      duration_ms: duration,
    });
    await refreshFrame();
  } catch (error) {
    toast(error.message);
  }
});
image.addEventListener("pointercancel", () => {
  pointer = null;
});
window.history.replaceState(null, "", window.location.pathname);
updateParameters();
refreshStatus();
refreshFrame();
setInterval(refreshStatus, 1000);
setInterval(() => {
  if (!pointer) refreshFrame(false);
}, 2000);

element("stop-live").addEventListener("click", async () => {
  try {
    await api("/api/stop", {});
    await refreshStatus();
  } catch (error) {
    toast(error.message);
  }
});
