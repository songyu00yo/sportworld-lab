import { InputCharts } from "/charts.js";

const element = (id) => document.getElementById(id);

function svgNode(name, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function clockText(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

export class RunProgressView {
  constructor() {
    this.plan = null;
    this.time = 0;
    this.playing = false;
    this.live = false;
    this.previousFrame = null;
    this.animationFrame = null;
    this.telemetryKey = null;
    this.surface = "render";
    this.charts = new InputCharts((time) => {
      if (this.live) return;
      this.time = time;
      this.charts.lastPreviewDraw = -Infinity;
      this.showPreview();
    });
    for (const surface of ["render", "satellite"])
      element(`route-${surface}`).addEventListener("click", () => this.setSurface(surface));
    element("play-preview").addEventListener("click", () => {
      if (!this.plan || this.live) return;
      if (this.time >= this.plan.metadata.duration_s) this.time = 0;
      this.playing = !this.playing;
      if (this.animationFrame !== null) cancelAnimationFrame(this.animationFrame);
      this.previousFrame = null;
      element("play-preview").textContent = this.playing ? "暂停预览" : "播放预览";
      if (this.playing) this.animationFrame = requestAnimationFrame((time) => this.animate(time));
      else {
        this.charts.lastPreviewDraw = -Infinity;
        this.showPreview();
      }
    });
    element("preview-time").addEventListener("input", () => {
      if (this.live) return;
      this.time = Number(element("preview-time").value);
      this.charts.lastPreviewDraw = -Infinity;
      this.showPreview();
    });
  }

  setPlan(plan) {
    if (this.plan?.report_path === plan.report_path && this.plan?.map_available === plan.map_available) return;
    this.plan = plan;
    this.playing = false;
    if (this.animationFrame !== null) cancelAnimationFrame(this.animationFrame);
    this.live = false;
    this.time = 0;
    this.telemetryKey = null;
    this.charts.setPlan(plan.expected_timeline);
    element("play-preview").textContent = "播放预览";
    element("preview-time").max = plan.metadata.duration_s;
    element("preview-time").value = 0;
    const meta = plan.metadata;
    this.outline = (meta.track_outline_xy || plan.preview.map((point) => [point.x, point.y])).map(
      ([x, y]) => ({ x, y }),
    );
    this.checkpoints = meta.checkpoints || [];
    const bounds = this.outline.concat(
      this.checkpoints.map((point) => ({ x: point.position_xy[0], y: point.position_xy[1] })),
    );
    const xs = bounds.map((point) => point.x);
    const ys = bounds.map((point) => point.y);
    const minX = Math.min(...xs),
      maxX = Math.max(...xs);
    const minY = Math.min(...ys),
      maxY = Math.max(...ys);
    const scale = Math.min(300 / Math.max(maxX - minX, 1), 155 / Math.max(maxY - minY, 1));
    this.convert = (point) => [
      180 + (point.x - (maxX + minX) / 2) * scale,
      95 - (point.y - (maxY + minY) / 2) * scale,
    ];
    const route = element("route-chart");
    route.replaceChildren();
    const rendered = svgNode("g", { class: "route-render-layer" });
    const satellite = svgNode("g", { class: "route-satellite-layer" });
    const track = meta.track;
    const calibrated = Boolean(plan.map_available !== false && track?.calibration?.estimated_center_pixel && track?.center_wgs84);
    element("route-satellite").disabled = !calibrated;
    element("route-satellite").title = calibrated ? "" : "未提供已校准的本地卫星底图";
    const syntheticElevation = meta.elevation_profile?.source_kind === "synthetic";
    element("map-status").hidden = calibrated && !syntheticElevation;
    element("map-status").textContent = [
      calibrated ? "" : "未提供已校准的卫星底图，当前显示渲染跑道。",
      syntheticElevation ? "海拔采用合成示例数据。" : "",
    ].filter(Boolean).join(" ");
    if (calibrated) {
      const center = track.calibration.estimated_center_pixel;
      const metresPerPixel =
        (2 * Math.PI * 6371008.8 * Math.cos((track.center_wgs84[1] * Math.PI) / 180)) /
        (2 ** 18 * 256);
      const origin = this.convert({ x: 0, y: 0 });
      const pixelScale = metresPerPixel * scale;
      satellite.append(svgNode("image", {
        href: "/track-map.png", x: origin[0] - center[0] * pixelScale,
        y: origin[1] - center[1] * pixelScale,
        width: 768 * pixelScale, height: 768 * pixelScale,
        preserveAspectRatio: "none",
      }));
      satellite.append(svgNode("rect", { x: 0, y: 0, width: 360, height: 190, fill: "#000", opacity: 0.16 }));
    }
    route.append(satellite, rendered);
    const gridSize = scale * 10;
    const origin = this.convert({ x: 0, y: 0 });
    const definitions = svgNode("defs", {});
    const pattern = svgNode("pattern", {
      id: "track-grid",
      width: gridSize,
      height: gridSize,
      x: origin[0] % gridSize,
      y: origin[1] % gridSize,
      patternUnits: "userSpaceOnUse",
    });
    pattern.append(
      svgNode("path", {
        d: `M ${gridSize} 0 H 0 V ${gridSize}`,
        fill: "none",
        stroke: "#191919",
        "stroke-width": 0.6,
      }),
    );
    definitions.append(pattern);
    rendered.append(
      definitions,
      svgNode("rect", { x: 0, y: 0, width: 360, height: 190, fill: "url(#track-grid)" }),
    );
    const outlinePoints = this.outline.map((point) => this.convert(point).join(",")).join(" ");
    rendered.append(
      svgNode("polyline", {
        points: outlinePoints,
        fill: "none",
        stroke: "#3a3a3a",
        "stroke-width": 12,
        "stroke-linejoin": "round",
      }),
    );
    rendered.append(
      svgNode("polyline", {
        points: outlinePoints,
        fill: "none",
        stroke: "#101010",
        "stroke-width": 8,
        "stroke-linejoin": "round",
      }),
    );
    const pitch = (meta.pitch_xy || []).slice(0, 4).map(([x, y]) => ({ x, y }));
    if (pitch.length === 4) {
      rendered.append(
        svgNode("polygon", {
          points: pitch.map((point) => this.convert(point).join(",")).join(" "),
          fill: "#0b0b0b",
          stroke: "#2e2e2e",
          "stroke-width": 0.7,
        }),
      );
      const top = this.convert({
        x: (pitch[0].x + pitch[3].x) / 2,
        y: (pitch[0].y + pitch[3].y) / 2,
      });
      const bottom = this.convert({
        x: (pitch[1].x + pitch[2].x) / 2,
        y: (pitch[1].y + pitch[2].y) / 2,
      });
      rendered.append(
        svgNode("line", {
          x1: top[0],
          y1: top[1],
          x2: bottom[0],
          y2: bottom[1],
          stroke: "#323232",
          "stroke-width": 0.7,
        }),
      );
      rendered.append(
        svgNode("circle", {
          cx: (top[0] + bottom[0]) / 2,
          cy: (top[1] + bottom[1]) / 2,
          r: 9.15 * scale,
          fill: "none",
          stroke: "#323232",
          "stroke-width": 0.7,
        }),
      );
    }
    this.completed = svgNode("polyline", {
      points: "",
      fill: "none",
      stroke: "#eeeeee",
      "stroke-width": 2.6,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    });
    route.append(this.completed);
    for (const checkpoint of this.checkpoints) {
      const point = this.convert({ x: checkpoint.position_xy[0], y: checkpoint.position_xy[1] });
      const marker = svgNode("circle", {
        class: "checkpoint-reference",
        cx: point[0],
        cy: point[1],
        r: 2.5,
        fill: "#6c6c6c",
        stroke: "#080808",
        "stroke-width": 1,
      });
      const title = svgNode("title", {});
      const label = checkpoint.name.startsWith("mandatory_")
        ? checkpoint.name.slice("mandatory_".length) : "历史参照点";
      title.textContent = `历史地图参照：${label} · 估算距跑道 ${checkpoint.distance_to_track_m.toFixed(1)} 米`;
      marker.append(title);
      route.append(marker);
    }
    this.dotHalo = svgNode("circle", { cx: 0, cy: 0, r: 7, fill: "#ffffff20" });
    this.dot = svgNode("circle", {
      cx: 0,
      cy: 0,
      r: 3.6,
      fill: "#eeeeee",
      stroke: "white",
      "stroke-width": 1.5,
    });
    route.append(this.dotHalo, this.dot);
    const scaleLabel = svgNode("text", { x: 7, y: 183, fill: "#555", "font-size": 5.5 });
    scaleLabel.textContent = "GRID 10 m";
    rendered.append(scaleLabel);
    this.setSurface(calibrated ? this.surface : "render");
    this.drawSpeedPlan();
    this.drawCalibration();
    element("result-distance").textContent = `${(meta.distance_m / 1000).toFixed(2)} 公里`;
    element("result-cadence").textContent = `${meta.cadence_target} 步/分`;
    element("result-checks").textContent = plan.checks.passed ? "通过" : "待检查";
    element("track-summary").textContent = meta.track
      ? `内圈 ${meta.loop_length_m.toFixed(0)} 米，本次计划 ${meta.laps.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} 圈。`
      : "路线按输入文件生成。";
    const observedDate = meta.checkpoint_source?.match(/\d{4}-\d{2}-\d{2}/)?.[0] || "日期未核实";
    element("checkpoint-note").textContent = this.checkpoints.length
      ? `校准页灰点是历史地图参照（${observedDate}），不代表今日任务。当天要求以右侧 App 为准。`
      : "";
    const comparisons = Object.values(plan.comparisons);
    const closer = comparisons.filter((item) => item.closer_to_this_excerpt === "new").length;
    const farther = comparisons.filter((item) => item.closer_to_this_excerpt === "old").length;
    element("comparison").textContent =
      `独立样本对比中，${closer} 项指标更接近参考值，${farther} 项差距增大。`;
    element("empty-preview").hidden = true;
    element("generated").hidden = false;
    this.showPreview();
  }

  setSurface(surface) {
    if (surface === "satellite" && element("route-satellite").disabled) return;
    this.surface = surface;
    element("route-chart").setAttribute("data-surface", surface);
    element("route-chart").setAttribute("aria-label",
      `${surface === "satellite" ? "卫星影像（估算校准）" : "渲染跑道"}、当前位置和本圈进度`);
    for (const name of ["render", "satellite"])
      element(`route-${name}`).setAttribute("aria-pressed", String(name === surface));
    element("route-attribution").hidden = surface !== "satellite";
  }

  drawSpeedPlan() {
    const chart = element("speed-chart");
    const maxSpeed = Math.max(...this.plan.preview.map((point) => point.speed), 1);
    this.speedScale = maxSpeed;
    chart.replaceChildren(svgNode("line", { x1: 10, x2: 230, y1: 63, y2: 63, stroke: "#222222" }));
    chart.append(
      svgNode("polyline", {
        points: this.plan.preview
          .map(
            (point) =>
              `${10 + (point.t / this.plan.metadata.duration_s) * 220},${63 - (point.speed / maxSpeed) * 48}`,
          )
          .join(" "),
        fill: "none",
        stroke: "#555555",
        "stroke-width": 1.5,
      }),
    );
    this.speedObserved = svgNode("polyline", {
      points: "",
      fill: "none",
      stroke: "#eeeeee",
      "stroke-width": 2,
    });
    this.speedCursor = svgNode("line", {
      x1: 10,
      x2: 10,
      y1: 9,
      y2: 65,
      stroke: "#999999",
      "stroke-width": 0.8,
      "stroke-dasharray": "2 3",
    });
    chart.append(this.speedObserved, this.speedCursor);
  }

  drawCalibration() {
    const map = element("calibration-map");
    const meta = this.plan.metadata;
    map.replaceChildren();
    const calibrated = this.plan.map_available !== false && meta.track?.calibration?.estimated_center_pixel && meta.track?.center_wgs84;
    map.hidden = !calibrated;
    element("calibration-note").textContent = calibrated
      ? "位置按影像估算。影像日期未知。© Esri、Vantor、Earthstar Geographics。"
      : "未提供已校准的本地卫星底图。示例几何仅用于离线预览，实际位置需要单独核对。";
    if (!calibrated) return;
    map.append(svgNode("image", { href: "/track-map.png", x: 0, y: 0, width: 768, height: 768 }));
    const center = meta.track.calibration.estimated_center_pixel;
    const metresPerPixel =
      (2 * Math.PI * 6371008.8 * Math.cos((meta.track.center_wgs84[1] * Math.PI) / 180)) /
      (2 ** 18 * 256);
    map.append(
      svgNode("polyline", {
        points: meta.track_outline_xy
          .map(([x, y]) => `${center[0] + x / metresPerPixel},${center[1] - y / metresPerPixel}`)
          .join(" "),
        fill: "none",
        stroke: "#ffe69a",
        "stroke-width": 2,
        "stroke-linejoin": "round",
      }),
    );
  }

  previewAt(time) {
    const points = this.plan.preview;
    const upper = points.findIndex((point) => point.t >= time);
    if (upper <= 0) return upper === 0 ? points[0] : points.at(-1);
    const left = points[upper - 1],
      right = points[upper];
    const fraction = (time - left.t) / (right.t - left.t);
    const sample = {};
    for (const key of ["x", "y", "t", "speed", "distance", "cadence"]) {
      sample[key] = (left[key] || 0) + ((right[key] || 0) - (left[key] || 0)) * fraction;
    }
    return sample;
  }

  showPreview() {
    if (!this.plan || this.live) return;
    const sample = this.previewAt(this.time);
    const expected = this.charts.expectedAt(this.time);
    this.showProgress(
      {
        position_xy: [sample.x, sample.y],
        distance_m: expected?.distance_m ?? sample.distance ?? 0,
        elapsed_s: sample.t,
        speed_m_s: expected?.speed_m_s ?? sample.speed,
        cadence_spm: expected?.cadence_spm ?? sample.cadence,
        planned_total_distance_m: this.plan.metadata.distance_m,
      },
      this.time === 0 ? "计划预览" : "离线演示 ×10",
    );
    element("preview-time").value = this.time;
    element("speed-label").textContent = "速度计划";
    this.charts.preview(this.time);
  }

  showProgress(data, mode) {
    const distance = Math.max(0, data.distance_m || 0);
    const elapsed = Math.max(0, data.elapsed_s || 0);
    const target = data.planned_total_distance_m || this.plan.metadata.distance_m;
    const loop = this.plan.metadata.loop_length_m;
    const fraction = Math.min(1, distance / target);
    const completedLaps = Math.floor(distance / loop);
    const lapDistance = distance % loop;
    const endpoint = {
      x: data.position_xy?.[0] ?? this.outline[0].x,
      y: data.position_xy?.[1] ?? this.outline[0].y,
    };
    const point = this.convert(endpoint);
    for (const dot of [this.dotHalo, this.dot]) {
      dot.setAttribute("cx", point[0]);
      dot.setAttribute("cy", point[1]);
    }
    const through = Math.floor((lapDistance / loop) * (this.outline.length - 1));
    const trail = this.outline.slice(0, through + 1).concat(endpoint);
    this.completed.setAttribute(
      "points",
      trail.map((position) => this.convert(position).join(",")).join(" "),
    );
    element("progress-mode").textContent = mode;
    element("lap-counter").textContent = `${completedLaps} 圈 + ${Math.round(lapDistance)} 米`;
    let caption = `${Math.round(distance)} / ${Math.round(target)} 米`;
    if (Number.isFinite(data.app_window_model_distance_m))
      caption += ` · App 记录段模型估算 ${Math.round(data.app_window_model_distance_m)} 米`;
    element("progress-caption").textContent = caption;
    element("progress-percent").textContent = `${Math.round(fraction * 100)}%`;
    element("run-progress").value = fraction;
    element("live-distance").textContent = Math.round(distance).toLocaleString("zh-CN");
    element("live-time").textContent = clockText(elapsed);
    element("live-pace").textContent =
      data.speed_m_s > 0.3 ? clockText(Math.round(1000 / data.speed_m_s)) : "—";
    element("live-cadence").textContent = Number.isFinite(data.cadence_spm)
      ? Math.round(data.cadence_spm)
      : "—";
    element("live-imu-hz").textContent = Number.isFinite(data.imu_hz)
      ? data.imu_hz.toFixed(1)
      : "—";
    element("live-gps-hz").textContent = Number.isFinite(data.gps_hz)
      ? data.gps_hz.toFixed(1)
      : "—";
    element("live-missed").textContent = Number.isFinite(data.missed_frames)
      ? data.missed_frames
      : "—";
    const x = 10 + Math.min(1, elapsed / this.plan.metadata.duration_s) * 220;
    this.speedCursor.setAttribute("x1", x);
    this.speedCursor.setAttribute("x2", x);
  }

  setJob(job) {
    if (!this.plan) return;
    const active = job?.action === "run" && ["running", "waiting", "stopping"].includes(job.state);
    const telemetry = job?.action === "run" ? job.telemetry : null;
    this.live = Boolean(active || telemetry);
    element("preview-player").hidden = this.live;
    element("stop-live").hidden = !active;
    element("stop-live").disabled = job?.state === "stopping";
    if (this.live) {
      this.playing = false;
      if (this.animationFrame !== null) cancelAnimationFrame(this.animationFrame);
      element("play-preview").textContent = "播放预览";
    }
    if (!telemetry) {
      if (active) {
        const first = this.plan.preview[0];
        this.showProgress(
          {
            position_xy: [first.x, first.y],
            distance_m: 0,
            elapsed_s: 0,
            speed_m_s: 0,
            cadence_spm: 0,
            planned_total_distance_m: this.plan.metadata.distance_m,
          },
          "等待开始输入",
        );
        this.charts.live(job.expected_timeline, [], 0);
      }
      return;
    }
    const key = `${job.id}-${telemetry.received_at}`;
    const age = Date.now() / 1000 - telemetry.received_at;
    let mode =
      telemetry.phase === "restored"
        ? "输入停止 · 已恢复"
        : ["failed", "stopped"].includes(job.state)
          ? "输入已中断 · 查看恢复结果"
          : age > 4
            ? "等待数据更新"
            : "输入程序实时数据";
    if (job.state === "running" && telemetry.phase === "sending" && age <= 4) {
      if (telemetry.operator_action === "warmup")
        mode = `预热中 · 剩余 ${Math.max(0, Math.ceil(telemetry.warmup_remaining_s || 0))} 秒`;
      if (telemetry.operator_action === "start_app") mode = "预热就绪 · 现在点击 App 的 GO";
      if (telemetry.operator_action === "pause_app") mode = "现在结束 App 记录 · 输入平滑收尾中";
    }
    if (this.telemetryKey === key) {
      element("progress-mode").textContent = mode;
      return;
    }
    this.telemetryKey = key;
    this.showProgress(telemetry, mode);
    this.charts.live(job.expected_timeline, job.telemetry_history, telemetry.elapsed_s);
    const duration = Math.max(this.plan.metadata.duration_s, telemetry.elapsed_s || 0);
    const history = job.telemetry_history || [];
    this.speedObserved.setAttribute(
      "points",
      history
        .map(
          (point) =>
            `${10 + (point.elapsed_s / duration) * 220},${63 - Math.min(1, point.speed_m_s / this.speedScale) * 48}`,
        )
        .join(" "),
    );
    element("speed-label").textContent = "浅线为计划 · 深线为已发送速度";
  }

  setParametersMatch(matches) {
    if (!this.plan || this.live) return;
    element("play-preview").disabled = !matches;
    element("preview-time").disabled = !matches;
    if (!matches) {
      this.playing = false;
      if (this.animationFrame !== null) cancelAnimationFrame(this.animationFrame);
      element("play-preview").textContent = "需重新生成";
      element("progress-mode").textContent = "上一份计划";
    } else if (element("play-preview").textContent === "需重新生成") {
      element("play-preview").textContent = "播放预览";
      this.showPreview();
    }
  }

  animate(timestamp) {
    if (!this.playing || this.live || !this.plan) return;
    if (this.previousFrame !== null)
      this.time += Math.min(0.1, (timestamp - this.previousFrame) / 1000) * 10;
    this.previousFrame = timestamp;
    this.time = Math.min(this.time, this.plan.metadata.duration_s);
    this.showPreview();
    if (this.time >= this.plan.metadata.duration_s) {
      this.playing = false;
      element("play-preview").textContent = "重新播放";
      return;
    }
    this.animationFrame = requestAnimationFrame((time) => this.animate(time));
  }
}
