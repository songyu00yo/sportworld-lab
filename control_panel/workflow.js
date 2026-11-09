// Workflow controls deliberately have no text fields or inferred App results.
export function runStopNotice(status) {
  if (status?.app_pause_required)
    return "输入已停止。请立即在 App 暂停，再核对并结束本次跑步。需要续跑时，请使用这条记录的继续入口。";
  const job = status?.job;
  const telemetry = job?.telemetry;
  if (job?.action !== "run" || job.mode !== "outdoor" || job.state !== "running"
      || telemetry?.phase !== "sending" || !Number.isFinite(telemetry.received_at)
      || Date.now() / 1000 - telemetry.received_at > 4) return "";
  if (telemetry.operator_action === "warmup")
    return `连续预热中，约 ${Math.max(0, Math.ceil(telemetry.warmup_remaining_s || 0))} 秒后提示点击 GO。`;
  if (telemetry.operator_action === "start_app")
    return "预热已完成。现在在右侧 App 点击 GO，网页会持续同步。";
  if (telemetry.operator_action === "pause_app")
    return "现在请在 App 暂停，并立即长按结束、确认保存。网页会继续完成收尾并恢复。";
  return "";
}

export class PromptControls {
  constructor(root, submit, onError) {
    this.root = root;
    this.submit = submit;
    this.onError = onError;
  }

  render(prompt, jobId) {
    this.prompt = prompt;
    this.identity = { job_id: jobId, prompt_id: prompt.id };
    this.busy = false;
    this.confirm = null;
    this.root.replaceChildren();
    const control = prompt.control || {};
    if (control.type === "choice") {
      for (const option of control.choices)
        this.button(option.label, () => this.send(option.value));
      return;
    }
    if (control.type === "time") {
      const minutes = this.picker("分钟", 0, 60);
      const seconds = this.picker("秒", 0, 59);
      this.value = () => minutes.value === "" || seconds.value === "" ? null
        : `${minutes.value}:${seconds.value.padStart(2, "0")}`;
      this.valid = () => this.value() !== null
        && Number(minutes.value) * 60 + Number(seconds.value) > 0
        && Number(minutes.value) * 60 + Number(seconds.value) <= 3600;
    } else if (control.type === "number") {
      const fractional = control.step < 1;
      const whole = this.picker(fractional ? "整数部分" : control.unit || "数值",
        Math.floor(control.min), Math.floor(control.max));
      const fraction = fractional ? this.picker("小数部分", 0, 99, (value) => `.${String(value).padStart(2, "0")}`) : null;
      this.value = () => whole.value === "" || (fraction && fraction.value === "") ? null
        : String(Number(whole.value) + (fraction ? Number(fraction.value) / 100 : 0));
      this.valid = () => this.value() !== null
        && Number(this.value()) >= control.min && Number(this.value()) <= control.max;
      if (!fractional && control.max >= 1000) {
        const presets = document.createElement("div");
        presets.className = "prompt-presets";
        for (const value of [25, 50, 75, 100, 150]) {
          if (value < control.min || value > control.max) continue;
          this.button(`${value} 米`, () => {
            whole.value = String(value);
            this.update();
          }, presets);
        }
        this.root.append(presets);
      }
      if (control.optional) this.button("未知，跳过", () => this.send(""));
    } else {
      this.note("此步骤尚未提供点选控件，请刷新页面或停止当前流程。");
      return;
    }
    this.confirm = this.button("按 App 显示核对并继续", () => {
      if (this.valid()) return this.send(this.value());
    });
    this.note("只需点选，无需打字。请以 App 实际显示为准。");
    this.update();
  }

  picker(label, low, high, format = String) {
    const wrapper = document.createElement("label");
    wrapper.className = "prompt-picker";
    const name = document.createElement("span");
    name.textContent = label;
    const select = document.createElement("select");
    select.setAttribute("aria-label", label);
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "点此选择";
    select.append(empty);
    for (let value = low; value <= high; value++) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = format(value);
      select.append(option);
    }
    select.value = "";
    select.addEventListener("change", () => this.update());
    wrapper.append(name, select);
    this.root.append(wrapper);
    return select;
  }

  button(label, action, parent = this.root) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button dark";
    button.textContent = label;
    button.addEventListener("click", action);
    parent.append(button);
    return button;
  }

  note(text) {
    const note = document.createElement("p");
    note.className = "prompt-note";
    note.textContent = text;
    this.root.append(note);
  }

  update() {
    if (this.confirm) this.confirm.disabled = this.busy || !this.valid();
  }

  async send(value) {
    if (this.busy) return;
    const identity = this.identity;
    this.busy = true;
    for (const control of this.root.querySelectorAll("button, select")) control.disabled = true;
    try {
      await this.submit(value, identity);
      // Polling will replace these controls when the next prompt arrives.
    } catch (error) {
      if (identity !== this.identity) return;
      this.busy = false;
      for (const control of this.root.querySelectorAll("button, select")) control.disabled = false;
      this.update();
      this.onError(error);
    }
  }
}
