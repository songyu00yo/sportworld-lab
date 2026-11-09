const element = (id) => document.getElementById(id);

function node(name, attributes = {}) {
  const value = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, item] of Object.entries(attributes)) value.setAttribute(key, String(item));
  return value;
}

const number = (value, digits = 1) => (Number.isFinite(value) ? value.toFixed(digits) : "—");
function pace(value) {
  if (!Number.isFinite(value)) return "—";
  const seconds = Math.round(value);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
const definitions = [
  { key: "cadence_spm", name: "模型步频", unit: "步/分", format: (v) => number(v, 0) },
  {
    key: "pace",
    name: "模拟配速",
    unit: "分/公里",
    value: (s) => (s.speed_m_s > 0 ? 1000 / s.speed_m_s : null),
    format: pace,
  },
  { key: "speed_m_s", name: "速度", unit: "m/s", format: (v) => number(v, 2) },
  { key: "distance_m", name: "累计距离", unit: "m", format: (v) => number(v, 0) },
  {
    key: "acceleration_rms_m_s2",
    name: "加速度幅值",
    unit: "RMS · m/s²",
    format: (v) => number(v, 2),
  },
  { key: "gyro_rms_rad_s", name: "角速度幅值", unit: "RMS · rad/s", format: (v) => number(v, 2) },
  { key: "interval_imu_hz", name: "动作更新频率", unit: "Hz", format: (v) => number(v, 1) },
  {
    key: "lateness_s",
    name: "输入延迟",
    unit: "ms",
    value: (s) => (Number.isFinite(s.lateness_s) ? s.lateness_s * 1000 : null),
    format: (v) => number(v, 1),
  },
  { key: "step_length_m", name: "模型单步距离", unit: "cm", value: (s) => Number.isFinite(s.step_length_m) ? s.step_length_m * 100 : null, format: (v) => number(v, 0) },
  { key: "step_count_model", name: "模型累计步数", unit: "步", format: (v) => number(v, 0) },
  { key: "altitude_m", name: "GPS 观测海拔", unit: "m", format: (v) => number(v, 1) },
];

export class InputCharts {
  constructor(onSeek) {
    this.onSeek = onSeek;
    this.expected = [];
    this.mode = "preview";
    this.lastPreviewDraw = -Infinity;
    this.charts = [];
    const container = element("input-charts");
    for (const definition of definitions) {
      const row = document.createElement("section");
      row.className = "input-chart";
      const header = document.createElement("div");
      header.className = "chart-header";
      const title = document.createElement("span");
      title.textContent = definition.name;
      const unit = document.createElement("small");
      unit.textContent = definition.unit;
      const current = document.createElement("strong");
      current.textContent = "—";
      const expected = document.createElement("em");
      expected.textContent = "/ —";
      header.append(title, unit, current, expected);
      const svg = node("svg", {
        viewBox: "0 0 230 52",
        role: "img",
        "aria-label": `${definition.name}的预期与当前输入曲线`,
      });
      svg.append(node("line", { x1: 3, y1: 46, x2: 227, y2: 46, stroke: "#222" }));
      svg.append(
        node("line", {
          x1: 3,
          y1: 25,
          x2: 227,
          y2: 25,
          stroke: "#151515",
          "stroke-dasharray": "2 3",
        }),
      );
      const expectedPath = node("path", { fill: "none", stroke: "#505050", "stroke-width": 1 });
      const currentPath = node("path", { fill: "none", stroke: "#dddddd", "stroke-width": 1.2 });
      const cursor = node("line", {
        x1: 3,
        y1: 3,
        x2: 3,
        y2: 47,
        stroke: "#888",
        "stroke-width": 0.6,
        "stroke-dasharray": "1 3",
      });
      const axis = document.createElement("div");
      axis.className = "chart-axis";
      const scaleNote = document.createElement("span");
      const rangeLabel = document.createElement("span");
      axis.append(scaleNote, rangeLabel);
      svg.append(expectedPath, currentPath, cursor);
      svg.addEventListener("pointerdown", (event) => {
        if (this.mode !== "preview" || !this.expected.length) return;
        const rectangle = svg.getBoundingClientRect();
        const fraction = Math.max(
          0,
          Math.min(1, (event.clientX - rectangle.left) / rectangle.width),
        );
        this.onSeek(fraction * this.expected.at(-1).elapsed_s);
      });
      row.append(header, axis, svg);
      container.append(row);
      this.charts.push({ definition, current, expected, expectedPath, currentPath, cursor, rangeLabel, scaleNote, svg });
    }
  }

  setPlan(timeline) {
    this.expected = timeline || [];
    this.mode = "preview";
    this.lastPreviewDraw = -Infinity;
    this.render([], 0, false);
  }

  preview(time) {
    this.mode = "preview";
    const now = performance.now();
    if (
      now - this.lastPreviewDraw < 100 &&
      time > 0 &&
      time < (this.expected.at(-1)?.elapsed_s || 0)
    )
      return;
    this.lastPreviewDraw = now;
    const current = time > 0 ? this.expected.filter((point) => point.elapsed_s <= time) : [];
    if (
      time > 0 &&
      time <= (this.expected.at(-1)?.elapsed_s || 0) &&
      current.at(-1)?.elapsed_s !== time
    ) {
      current.push(this.expectedAt(time));
    }
    this.render(current, time, true);
  }

  live(timeline, history, time) {
    this.mode = "live";
    if (timeline?.length) this.expected = timeline;
    this.render(history || [], time || 0, false);
  }

  expectedAt(time) {
    const upper = this.expected.findIndex((point) => point.elapsed_s >= time);
    if (upper <= 0) return upper === 0 ? this.expected[0] : this.expected.at(-1);
    const left = this.expected[upper - 1],
      right = this.expected[upper];
    const fraction = (time - left.elapsed_s) / (right.elapsed_s - left.elapsed_s);
    const result = {};
    for (const key of Object.keys(right)) {
      result[key] =
        Number.isFinite(left[key]) && Number.isFinite(right[key])
          ? left[key] + (right[key] - left[key]) * fraction
          : right[key];
    }
    return result;
  }

  render(history, time, preview) {
    element("chart-source").textContent =
      preview && time > 0 ? "离线预览 / 预期" : "当前输入 / 预期";
    const duration = Math.max(this.expected.at(-1)?.elapsed_s || 1, history.at(-1)?.elapsed_s || 0);
    // The planned moving speed sets one shared threshold, including for slow plans.
    // Startup/stops remain in the numeric readout; omitted curve sections are labelled.
    const positiveSpeeds = (series) => series.map((sample) => sample.speed_m_s)
      .filter((value) => Number.isFinite(value) && value > 0).sort((a, b) => a - b);
    let movingSpeeds = positiveSpeeds(this.expected);
    if (!movingSpeeds.length) movingSpeeds = positiveSpeeds(history);
    const movingThreshold = movingSpeeds.length ? movingSpeeds[Math.floor(movingSpeeds.length / 2)] * 0.75 : 0;
    for (const chart of this.charts) {
      const key = chart.definition.key;
      const get = chart.definition.value || ((sample) => sample[key]);
      const moving = ["pace", "cadence_spm", "speed_m_s", "step_length_m"].includes(key);
      const focused = moving || key === "altitude_m";
      const plotValue = (sample) => {
        const value = get(sample);
        if (moving && (!(sample.speed_m_s >= movingThreshold) || !(sample.speed_m_s > 0) || !(value > 0))) return null;
        return value;
      };
      const values = this.expected.concat(history).map(plotValue).filter(Number.isFinite);
      let minimum = 0, maximum = 1;
      if (values.length) {
        let low = Infinity, high = -Infinity;
        for (const value of values) { low = Math.min(low, value); high = Math.max(high, value); }
        if (focused) {
          const minimumSpan = { cadence_spm: 8, pace: 10, step_length_m: 10, altitude_m: 1 }[key] ?? 0.2;
          const span = Math.max(high - low, minimumSpan);
          const middle = (low + high) / 2;
          minimum = key === "altitude_m" ? middle - span * 0.6 : Math.max(0, middle - span * 0.6);
          maximum = middle + span * 0.6;
        } else {
          minimum = Math.min(0, low);
          maximum = Math.max(1, high) * 1.08;
        }
      }
      chart.range = { minimum, maximum, movingThreshold };
      chart.rangeLabel.textContent = values.length
        ? `${chart.definition.format(minimum)}–${chart.definition.format(maximum)}` : "无可绘制数据";
      chart.scaleNote.textContent = moving ? `≥${number(movingThreshold, 2)}m/s · 低速/停步断线`
        : key === "altitude_m" ? "观测范围 · 含定位测量误差" : "零基线 · 全范围";
      chart.svg.setAttribute("aria-label", `${chart.definition.name}，${chart.scaleNote.textContent}，纵轴${chart.rangeLabel.textContent} ${chart.definition.unit}，浅线预期、亮线当前输入`);
      const path = (series) => {
        let drawing = false;
        const parts = [];
        for (const sample of series) {
          const value = plotValue(sample);
          if (!Number.isFinite(value) || !Number.isFinite(sample.elapsed_s)) {
            drawing = false;
            continue;
          }
          const x = 3 + (sample.elapsed_s / duration) * 224;
          const y = 46 - ((value - minimum) / (maximum - minimum)) * 40;
          parts.push(`${drawing ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`);
          drawing = true;
        }
        return parts.join(" ");
      };
      chart.expectedPath.setAttribute("d", path(this.expected));
      chart.currentPath.setAttribute("d", path(history));
      const x = 3 + Math.min(1, time / duration) * 224;
      chart.cursor.setAttribute("x1", x);
      chart.cursor.setAttribute("x2", x);
      const reference = this.expectedAt(time);
      chart.current.textContent = history.length
        ? chart.definition.format(get(history.at(-1)))
        : "—";
      chart.expected.textContent = `/ ${reference ? chart.definition.format(get(reference)) : "—"}`;
    }
  }
}
