// Offline DOM doubles only. No control server or emulator receives these clicks.
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.disabled = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll() {
    return this.children.flatMap((child) => [
      ...(["button", "select"].includes(child.tag) ? [child] : []), ...child.querySelectorAll(),
    ]);
  }
}
globalThis.document = { createElement: (tag) => new Element(tag) };
const source = await readFile(new URL("./control_panel/workflow.js", import.meta.url), "utf8");
const { PromptControls, runStopNotice } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

test("stopped input keeps a pause notice across polling and page reload", () => {
  const state = { app_pause_required: true, job: { state: "failed" } };
  const message = runStopNotice(state);
  assert.match(message, /立即在 App 暂停/);
  assert.equal(runStopNotice({ ...state, job: null }), message);
  assert.equal(runStopNotice({ app_pause_required: false }), "");
  assert.equal(runStopNotice(null), "");
});

test("outdoor action notices follow live cues and disappear after recovery or stale input", () => {
  const status = { job: { id: "outdoor", action: "run", mode: "outdoor", state: "running",
    telemetry: { phase: "sending", received_at: Date.now() / 1000,
      operator_action: "warmup", warmup_remaining_s: 3.2 } } };
  assert.match(runStopNotice(status), /4 秒/);
  status.job.telemetry.operator_action = "start_app";
  assert.match(runStopNotice(status), /现在在右侧 App 点击 GO/);
  status.job.telemetry.operator_action = null;
  assert.equal(runStopNotice(status), "");
  status.job.telemetry.operator_action = "pause_app";
  assert.match(runStopNotice(status), /现在请在 App 暂停/);
  status.job.telemetry.received_at -= 10;
  assert.equal(runStopNotice(status), "");
  status.job.telemetry.received_at = Date.now() / 1000;
  status.job.telemetry.phase = "restored";
  assert.equal(runStopNotice(status), "");
  status.job.telemetry.phase = "sending";
  status.job.mode = "campus";
  assert.equal(runStopNotice(status), "");
});

function setup(control, submit = async () => {}) {
  const root = new Element("div");
  const errors = [];
  const view = new PromptControls(root, submit, (error) => errors.push(error.message));
  view.render({ id: "prompt", control }, "job");
  const selects = () => root.querySelectorAll().filter((e) => e.tag === "select");
  const buttons = () => root.querySelectorAll().filter((e) => e.tag === "button");
  return { root, view, errors, selects, buttons };
}

test("confirmation is one click, preserves its exact answer, and blocks double submission", async () => {
  const sent = [];
  const { buttons } = setup({ type: "choice", choices: [{ label: "已就绪，继续", value: "已确认" }] },
    async (...args) => { sent.push(args); });
  const button = buttons()[0];
  await Promise.all([button.listeners.click(), button.listeners.click()]);
  assert.equal(button.disabled, true);
  assert.deepEqual(sent, [["已确认", { job_id: "job", prompt_id: "prompt" }]]);
});

test("App distance stays unselected until the operator chooses both parts", async () => {
  const sent = [];
  const { view, selects } = setup({ type: "number", min: 0, max: 5, step: .01 }, async (value) => sent.push(value));
  assert.equal(view.confirm.disabled, true);
  assert.deepEqual(selects().map((e) => e.value), ["", ""]);
  selects()[0].value = "1";
  selects()[0].listeners.change();
  assert.equal(view.confirm.disabled, true);
  selects()[1].value = "18";
  selects()[1].listeners.change();
  assert.equal(view.confirm.disabled, false);
  await view.confirm.listeners.click();
  assert.deepEqual(sent, ["1.18"]);
});

test("numeric maximum is enforced and unknown radius skips without a guess", async () => {
  const numeric = setup({ type: "number", min: 0, max: 5, step: .01 });
  numeric.selects()[0].value = "5";
  numeric.selects()[1].value = "1";
  numeric.view.update();
  assert.equal(numeric.view.confirm.disabled, true);
  const sent = [];
  const optional = setup({ type: "number", min: 1, max: 100, step: 1, optional: true }, async (value) => sent.push(value));
  await optional.buttons().find((e) => e.textContent === "未知，跳过").listeners.click();
  assert.deepEqual(sent, [""]);
});

test("time is click-selected, includes explicit zero seconds, and rejects zero or over an hour", async () => {
  const sent = [];
  const { view, selects } = setup({ type: "time" }, async (value) => sent.push(value));
  assert.equal(view.confirm.disabled, true);
  for (const [minute, second, valid] of [["0", "0", false], ["60", "1", false], ["4", "0", true]]) {
    selects()[0].value = minute;
    selects()[1].value = second;
    view.update();
    assert.equal(view.confirm.disabled, !valid);
  }
  await view.confirm.listeners.click();
  assert.deepEqual(sent, ["4:00"]);
});

test("failed submission can retry and new prompts remain independent of the old response", async () => {
  const { view, buttons, errors } = setup({ type: "choice", choices: [{ label: "继续", value: "" }] },
    async () => { throw new Error("连接失败"); });
  await buttons()[0].listeners.click();
  assert.equal(buttons()[0].disabled, false);
  assert.deepEqual(errors, ["连接失败"]);
  let reject;
  view.submit = () => new Promise((_, fail) => { reject = fail; });
  const oldRequest = buttons()[0].listeners.click();
  view.render({ id: "next", control: { type: "choice", choices: [{ label: "下一步", value: "next" }] } }, "job");
  reject(new Error("旧请求失败"));
  await oldRequest;
  assert.equal(buttons()[0].disabled, false);
  assert.deepEqual(errors, ["连接失败"]);
});
