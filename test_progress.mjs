// Offline DOM doubles only. These fixtures never reach the local control server.
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";

class Element {
  constructor() {
    this.textContent = "";
    this.attributes = {};
    this.children = [];
    this.listeners = {};
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
  }
  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
}

let elements;
let callbacks;
let nextFrame;
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElementNS() {
    return new Element();
  },
  createElement() {
    return new Element();
  },
};
globalThis.requestAnimationFrame = (callback) => {
  callbacks.set(++nextFrame, callback);
  return nextFrame;
};
globalThis.cancelAnimationFrame = (id) => callbacks.delete(id);
const chartsSource = await readFile(new URL("./control_panel/charts.js", import.meta.url), "utf8");
const chartsUrl = "data:text/javascript;base64," + Buffer.from(chartsSource).toString("base64");
const source = (
  await readFile(new URL("./control_panel/progress.js", import.meta.url), "utf8")
).replace('"/charts.js"', JSON.stringify(chartsUrl));
const { RunProgressView } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);

function setup() {
  elements = new Map();
  callbacks = new Map();
  nextFrame = 0;
  const view = new RunProgressView();
  view.setPlan({
    report_path: "offline-fixture",
    checks: { passed: true },
    comparisons: {},
    expected_timeline: [
      { elapsed_s: 0, speed_m_s: 0, cadence_spm: 0, distance_m: 0 },
      { elapsed_s: 50, speed_m_s: 4, cadence_spm: 175, distance_m: 200 },
      { elapsed_s: 100, speed_m_s: 0, cadence_spm: 0, distance_m: 400 },
    ],
    metadata: {
      duration_s: 100,
      distance_m: 400,
      cadence_target: 175,
      loop_length_m: 400,
      track_outline_xy: [
        [20, 0],
        [0, 10],
        [-20, 0],
        [0, -10],
        [20, 0],
      ],
    },
    preview: [
      { x: 20, y: 0, t: 0, speed: 0, distance: 0, cadence: 0 },
      { x: -20, y: 0, t: 50, speed: 4, distance: 200, cadence: 175 },
      { x: 20, y: 0, t: 100, speed: 0, distance: 400, cadence: 0 },
    ],
  });
  return view;
}

test("preview position and counters describe the same point", () => {
  const view = setup();
  view.time = 50;
  view.showPreview();
  assert.equal(elements.get("live-distance").textContent, "200");
  assert.equal(elements.get("live-time").textContent, "0:50");
  assert.equal(elements.get("live-pace").textContent, "4:10");
  assert.equal(elements.get("progress-percent").textContent, "50%");
  assert.equal(Number(view.dot.attributes.cx), view.convert({ x: -20, y: 0 })[0]);
  assert.equal(elements.get("progress-mode").textContent, "离线演示 ×10");
});

test("expected and current charts use the same instant rather than a future sample", () => {
  const view = setup();
  const reference = view.charts.expectedAt(25);
  assert.equal(reference.cadence_spm, 87.5);
  view.charts.live(
    view.plan.expected_timeline,
    [{ elapsed_s: 25, cadence_spm: 85, speed_m_s: 2, distance_m: 98 }],
    25,
  );
  assert.equal(view.charts.charts[0].current.textContent, "85");
  assert.equal(view.charts.charts[0].expected.textContent, "/ 88");
  assert.equal(elements.get("chart-source").textContent, "当前输入 / 预期");
  assert.notEqual(
    view.charts.charts[0].expectedPath.attributes.d,
    view.charts.charts[0].currentPath.attributes.d,
  );
});

test("preview charts label their data source and round pace consistently across minute boundaries", () => {
  const view = setup();
  const speed = 1000 / 299.6;
  view.plan.preview[1].speed = speed;
  view.plan.expected_timeline[1].speed_m_s = speed;
  elements.get("preview-time").value = "50";
  elements.get("preview-time").listeners.input();
  assert.equal(elements.get("chart-source").textContent, "离线预览 / 预期");
  assert.equal(elements.get("live-pace").textContent, "5:00");
  assert.equal(view.charts.charts[1].current.textContent, "5:00");
  assert.equal(view.charts.charts[1].expected.textContent, "/ 5:00");
});

test("starting a live workflow clears preview-only progress", () => {
  const view = setup();
  view.time = 50;
  view.showPreview();
  view.setJob({ id: "fixture-run", action: "run", state: "waiting", telemetry: null });
  assert.equal(elements.get("live-distance").textContent, "0");
  assert.equal(elements.get("live-time").textContent, "0:00");
  assert.equal(elements.get("progress-mode").textContent, "等待开始输入");
  assert.equal(elements.get("preview-player").hidden, true);
});

test("rapid scrubbing forces charts to the final selected instant", () => {
  const view = setup();
  elements.get("preview-time").value = "10";
  elements.get("preview-time").listeners.input();
  elements.get("preview-time").value = "25";
  elements.get("preview-time").listeners.input();
  assert.equal(elements.get("live-distance").textContent, "100");
  assert.equal(view.charts.charts[3].current.textContent, "100");
  assert.equal(view.charts.charts[0].current.textContent, "88");
});

test("live counters use received telemetry and expose stale data", () => {
  const view = setup();
  view.setJob({
    id: "fixture-run",
    action: "run",
    state: "running",
    telemetry: {
      phase: "sending",
      position_xy: [-2, 7],
      distance_m: 60,
      elapsed_s: 15,
      speed_m_s: 4,
      cadence_spm: 173,
      planned_total_distance_m: 400,
      received_at: Date.now() / 1000 - 10,
      imu_hz: 19.9,
      gps_hz: 1,
      missed_frames: 0,
    },
    telemetry_history: [],
  });
  assert.equal(elements.get("live-distance").textContent, "60");
  assert.equal(elements.get("live-time").textContent, "0:15");
  assert.equal(elements.get("live-cadence").textContent, 173);
  assert.equal(elements.get("progress-mode").textContent, "等待数据更新");
});

test("outdoor warmup cues preserve total input and label the separate model window", () => {
  const view = setup();
  const job = { id: "outdoor-window", action: "run", mode: "outdoor", state: "running",
    telemetry_history: [], telemetry: { phase: "sending", received_at: Date.now() / 1000,
      elapsed_s: 4, distance_m: 7, operator_action: "warmup", warmup_remaining_s: 3.2 } };
  view.setJob(job);
  assert.equal(elements.get("progress-mode").textContent, "预热中 · 剩余 4 秒");
  job.telemetry.operator_action = "start_app";
  view.setJob(job);
  assert.equal(elements.get("progress-mode").textContent, "预热就绪 · 现在点击 App 的 GO");
  job.telemetry = { ...job.telemetry, received_at: job.telemetry.received_at + .1,
    elapsed_s: 50, distance_m: 200, operator_action: null, app_window_model_distance_m: 165 };
  view.setJob(job);
  assert.equal(elements.get("live-distance").textContent, "200");
  assert.match(elements.get("progress-caption").textContent, /模型估算 165 米/);
  assert.equal(elements.get("progress-mode").textContent, "输入程序实时数据");
  job.telemetry.operator_action = "pause_app";
  view.setJob(job);
  assert.equal(elements.get("progress-mode").textContent, "现在结束 App 记录 · 输入平滑收尾中");
  job.telemetry.phase = "restored";
  view.setJob(job);
  assert.equal(elements.get("progress-mode").textContent, "输入停止 · 已恢复");
});

test("parameter changes stop animation and require regeneration", () => {
  const view = setup();
  elements.get("play-preview").listeners.click();
  assert.equal(callbacks.size, 1);
  view.setParametersMatch(false);
  assert.equal(view.playing, false);
  assert.equal(callbacks.size, 0);
  assert.equal(elements.get("play-preview").disabled, true);
});

test("rapid pause and resume never leave duplicate animation loops", () => {
  setup();
  const click = elements.get("play-preview").listeners.click;
  click();
  click();
  assert.equal(callbacks.size, 0);
  click();
  assert.equal(callbacks.size, 1);
});

test("satellite uses the same world-to-stage transform and preserves live progress", () => {
  const view = setup();
  const plan = {
    ...view.plan,
    report_path: "offline-satellite-fixture",
    metadata: {
      ...view.plan.metadata,
      laps: 1,
      track: { center_wgs84: [120, 30], calibration: { estimated_center_pixel: [410, 385] } },
    },
  };
  view.setPlan(plan);
  view.time = 50;
  view.showPreview();
  const position = { ...view.dot.attributes };
  const satellite = elements.get("route-chart").children[0];
  const image = satellite.children[0];
  const metresPerPixel = 2 * Math.PI * 6371008.8 * Math.cos(Math.PI / 6) / (2 ** 18 * 256);
  const origin = view.convert({ x: 0, y: 0 });
  const scale = view.convert({ x: 1, y: 0 })[0] - origin[0];
  assert.ok(Math.abs(Number(image.attributes.width) - 768 * metresPerPixel * scale) < 1e-9);
  assert.ok(Math.abs(Number(image.attributes.x) - (origin[0] - 410 * metresPerPixel * scale)) < 1e-9);
  assert.ok(Math.abs(Number(image.attributes.y) - (origin[1] - 385 * metresPerPixel * scale)) < 1e-9);
  view.live = true;
  view.setSurface("satellite");
  assert.equal(elements.get("route-chart").attributes["data-surface"], "satellite");
  assert.equal(elements.get("route-satellite").attributes["aria-pressed"], "true");
  assert.equal(elements.get("route-attribution").hidden, false);
  assert.deepEqual(view.dot.attributes, position);
  assert.equal(view.time, 50);
  assert.equal(view.live, true);
  view.setSurface("render");
  assert.equal(elements.get("route-attribution").hidden, true);
  assert.deepEqual(view.dot.attributes, position);
});

test("uncalibrated plans cannot select satellite imagery", () => {
  const view = setup();
  assert.equal(elements.get("route-satellite").disabled, true);
  view.setSurface("satellite");
  assert.equal(view.surface, "render");
  assert.equal(elements.get("route-chart").attributes["data-surface"], "render");
});

test("missing local imagery falls back without a calibration crash", () => {
  const view = setup();
  view.setPlan({
    ...view.plan,
    map_available: false,
    metadata: {
      ...view.plan.metadata,
      laps: 1,
      track: { center_wgs84: [120, 30] },
      elevation_profile: { source_kind: "synthetic" },
    },
  });
  assert.equal(elements.get("route-satellite").disabled, true);
  assert.equal(elements.get("calibration-map").hidden, true);
  assert.match(elements.get("map-status").textContent, /合成示例/);
  assert.match(elements.get("calibration-note").textContent, /未提供/);
  assert.equal(elements.get("route-chart").attributes["data-surface"], "render");
});

test("missing image disables even a geometrically calibrated plan", () => {
  const view = setup();
  view.setPlan({
    ...view.plan,
    map_available: false,
    metadata: {
      ...view.plan.metadata,
      laps: 1,
      track: { center_wgs84: [120, 30], calibration: { estimated_center_pixel: [410, 385] } },
    },
  });
  assert.equal(elements.get("route-satellite").disabled, true);
  assert.equal(elements.get("calibration-map").hidden, true);
});

test("movement axes expose real effort variation without startup pace compression", () => {
  const charts = setup().charts;
  const timeline = [0, 0.1, 3, 3.3, 0, 3.1, 3.2, 0].map((speed, index) => ({
    elapsed_s: index, speed_m_s: speed, cadence_spm: speed > 1 ? 170 + index : 0,
    distance_m: index * 3,
  }));
  charts.setPlan(timeline);
  charts.live(timeline, [timeline[1]], 1);
  const [cadence, paceChart, speed, distance] = charts.charts;
  assert.ok(cadence.range.minimum > 150);
  assert.ok(speed.range.minimum > 2);
  assert.ok(paceChart.range.maximum < 400);
  assert.equal(paceChart.current.textContent, "166:40");
  assert.equal(paceChart.expected.textContent, "/ 166:40");
  assert.equal(paceChart.currentPath.attributes.d, "");
  assert.match(paceChart.scaleNote.textContent, /低速\/停步断线/);
  assert.equal((paceChart.expectedPath.attributes.d.match(/M/g) || []).length, 2);
  assert.equal(distance.range.minimum, 0);
  assert.match(distance.scaleNote.textContent, /零基线/);
  assert.match(paceChart.svg.attributes["aria-label"], /纵轴/);
});

test("slow plans use their own moving speed and keep valid slow pace", () => {
  const charts = setup().charts;
  const timeline = [0, 0.2, 0.22, 0.18, 0].map((speed, index) => ({
    elapsed_s: index, speed_m_s: speed, cadence_spm: speed ? 60 + index : 0, distance_m: index * 0.2,
  }));
  charts.setPlan(timeline);
  charts.live(timeline, [timeline[2]], 2);
  const paceChart = charts.charts[1];
  assert.ok(paceChart.range.movingThreshold < 0.18);
  assert.notEqual(paceChart.current.textContent, "—");
  assert.match(paceChart.currentPath.attributes.d, /^M/);
  assert.ok(paceChart.range.maximum > 1000 / 0.18);
});

test("acceleration and braking samples cannot compress the running pace range", () => {
  const charts = setup().charts;
  const timeline = [0, 2, 4.4, 4.2, 3.6, 3.7, 4, 5, 2, 0].map((speed, index) => ({
    elapsed_s: index, speed_m_s: speed, cadence_spm: speed ? 175 * (speed / 4) ** .32 : 0,
    distance_m: index * 4,
  }));
  charts.setPlan(timeline);
  assert.ok(charts.charts[1].range.maximum < 300);
  assert.ok(charts.charts[0].range.minimum > 155);
  charts.live(timeline, [timeline[1]], 1);
  assert.equal(charts.charts[1].current.textContent, "8:20");
  assert.equal(charts.charts[1].currentPath.attributes.d, "");
});

test("empty and stationary chart series stay finite and never invent motion", () => {
  const charts = setup().charts;
  for (const timeline of [[], [{ elapsed_s: 0, speed_m_s: 0, cadence_spm: 0, distance_m: 0 }]]) {
    charts.setPlan(timeline);
    charts.live(timeline, timeline, 0);
    for (const chart of charts.charts) {
      assert.doesNotMatch(chart.expectedPath.attributes.d, /NaN|Infinity/);
      assert.doesNotMatch(chart.currentPath.attributes.d, /NaN|Infinity/);
      assert.ok(chart.range.maximum > chart.range.minimum);
    }
    for (const chart of charts.charts.slice(0, 3)) {
      assert.equal(chart.currentPath.attributes.d, "");
      assert.equal(chart.rangeLabel.textContent, "无可绘制数据");
    }
  }
});

test("held live outliers expand the visible range and keep exact readouts", () => {
  const charts = setup().charts;
  const timeline = [3, 3.2, 3.1].map((speed, index) => ({
    elapsed_s: index, speed_m_s: speed, cadence_spm: 172 + index, distance_m: index * 3,
  }));
  const history = [{ elapsed_s: 1, speed_m_s: 20, cadence_spm: 300, distance_m: 7 }];
  charts.live(timeline, history, 1);
  assert.ok(charts.charts[0].range.maximum > 300);
  assert.ok(charts.charts[2].range.maximum > 20);
  assert.equal(charts.charts[0].current.textContent, "300");
  assert.equal(charts.charts[2].current.textContent, "20.00");
  const range = { ...charts.charts[2].range };
  charts.live(timeline, history, 1);
  assert.deepEqual(charts.charts[2].range, range);
  assert.equal(charts.charts[3].range.minimum, 0);
  const points = charts.charts[3].expectedPath.attributes.d.match(/[ML]([\d.]+),([\d.]+)/g)
    .map((point) => point.slice(1).split(",").map(Number));
  assert.ok(points.every((point, index) => index === 0 || point[1] <= points[index - 1][1]));
});

test("altitude uses its observed range and preserves below-sea-level values", () => {
  const charts = setup().charts;
  for (const heights of [[79, 81, 78], [-5, -2, -4]]) {
    const timeline = heights.map((altitude_m, index) => ({ elapsed_s: index, speed_m_s: 0, altitude_m }));
    charts.live(timeline, [timeline[1]], 1);
    const altitude = charts.charts.find((chart) => chart.definition.key === "altitude_m");
    assert.ok(altitude.range.minimum < Math.min(...heights));
    assert.ok(altitude.range.maximum > Math.max(...heights));
    assert.ok(altitude.range.maximum - altitude.range.minimum < 10);
    assert.equal(altitude.current.textContent, heights[1].toFixed(1));
    assert.match(altitude.scaleNote.textContent, /测量误差/);
  }
});

test("step length displays centimetres and model step totals remain distinct", () => {
  const charts = setup().charts;
  const timeline = [1.55, 1.65, 1.6].map((step_length_m, index) => ({
    elapsed_s: index, speed_m_s: 5, step_length_m, step_count_model: index * 3.1,
  }));
  charts.live(timeline, [timeline[1]], 1);
  const stepLength = charts.charts.find((chart) => chart.definition.key === "step_length_m");
  const steps = charts.charts.find((chart) => chart.definition.key === "step_count_model");
  assert.equal(stepLength.current.textContent, "165");
  assert.ok(stepLength.range.minimum > 140);
  assert.equal(steps.current.textContent, "3");
  assert.match(steps.definition.name, /模型/);
});
