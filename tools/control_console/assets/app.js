"use strict";

const ACTUAL = "#56d4ff";
const TARGET = "#f7b955";
const GRID = "#253443";
const SVG_NS = "http://www.w3.org/2000/svg";
const HISTORY_LIMIT = 200;

const latencyHistory = { openxr: [], linker: [], hitbot: [] };
const armTrail = { actual: [], target: [], lastSequence: null };
let latestSnapshot = null;
let lastSnapshotWallTime = 0;
let actionBusy = false;
let eventSource = null;
let selectedJointIndex = 0;

function byId(id) {
  return document.getElementById(id);
}

function setText(id, value, fallback = "--") {
  byId(id).textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function fixed(value, digits = 1, fallback = "--") {
  return finite(value) ? value.toFixed(digits) : fallback;
}

function upper(value, fallback = "--") {
  return value === null || value === undefined || value === "" ? fallback : String(value).toUpperCase();
}

function readinessLabel(providerId) {
  const labels = {
    "operator-confirmation-v1": "OPERATOR",
    "hand-state-freshness-v1": "HAND STATE",
    "gateway-health-v1": "GATEWAY",
    "policy-compatibility-v1": "POLICY",
  };
  return labels[providerId] || upper(providerId).replace(/-V\d+$/, "").replaceAll("-", " ");
}

function source(snapshot, name) {
  return snapshot && snapshot.sources && snapshot.sources[name]
    ? snapshot.sources[name]
    : {
        health: "stale",
        age_ms: null,
        rate_hz: 0,
        sequence: null,
        payload: {},
      };
}

function pushBounded(array, value) {
  if (!finite(value)) {
    return;
  }
  array.push(value);
  if (array.length > HISTORY_LIMIT) {
    array.splice(0, array.length - HISTORY_LIMIT);
  }
}

function setHealthChip(id, label, telemetry) {
  const element = byId(id);
  const health = telemetry.health || "stale";
  const mode = telemetry.payload && telemetry.payload.mode;
  element.className = `health-chip health-${health}`;
  if (health === "healthy") {
    const rate = finite(telemetry.rate_hz) ? ` ${telemetry.rate_hz.toFixed(0)} Hz` : "";
    element.textContent = ["synthetic", "fake"].includes(mode)
      ? `${label} SIM${rate}`
      : `${label}${rate}`;
  } else {
    element.textContent = `${label} ${health.toUpperCase()}`;
  }
  const reason = telemetry.payload && telemetry.payload.source_reason;
  element.title = reason
    ? `${label}: ${reason}`
    : finite(telemetry.age_ms)
    ? `${label} source age ${telemetry.age_ms.toFixed(1)} ms`
    : `${label} has no current sample`;
}

function renderRuntime(telemetry) {
  const payload = telemetry.payload || {};
  const state = upper(payload.state, "CONNECTING");
  const stateChip = byId("runtime-state");
  stateChip.textContent = state;
  stateChip.className = "state-chip";
  if (state === "FAULT" || state === "SAFE_HOLD" || state === "ESTOP") {
    stateChip.classList.add("state-fault");
  } else if (state === "RL_ACTIVE") {
    stateChip.classList.add("state-active");
  } else {
    stateChip.classList.add("state-neutral");
  }

  setText("session-id", payload.session_id);
  setText("runtime-owner", payload.hand_owner ? `OWNER ${upper(payload.hand_owner)}` : "OWNER --");
  setText("runtime-message", payload.rejection_reason ? `Rejected: ${payload.rejection_reason}` : payload.message);
  setText("policy-name", payload.policy_name ? `POLICY ${payload.policy_name}` : "POLICY --");
  setText("log-path", payload.logs_path ? `LOGS ${payload.logs_path}` : "LOGS --");

  const timelineState =
    state === "RL_ACTIVE"
      ? "rl"
      : state === "RL_SHADOW"
        ? "shadow"
        : state === "TELEOP_ACTIVE"
          ? "teleop"
          : "blend";
  document.querySelectorAll("#mode-timeline [data-mode]").forEach((item) => {
    item.classList.toggle("active", item.dataset.mode === timelineState);
  });

  const readiness = byId("readiness-value");
  const readinessPanel = document.querySelector(".readiness-panel");
  const readinessProviders = Array.isArray(payload.readiness_providers)
    ? payload.readiness_providers
    : [];
  const passedProviders = readinessProviders.filter((provider) =>
    provider.valid === true && ["pass", "operator-confirmed"].includes(provider.result)
  ).length;
  if (payload.readiness_ready === true) {
    readiness.textContent = readinessProviders.length
      ? `${passedProviders} / ${readinessProviders.length}`
      : "READY";
    readinessPanel.classList.remove("not-ready");
  } else if (payload.readiness_ready === false) {
    readiness.textContent = "NOT READY";
    readinessPanel.classList.add("not-ready");
  } else {
    readiness.textContent = "-- / --";
    readinessPanel.classList.add("not-ready");
  }
  setText("history-value", payload.history ? `HISTORY ${payload.history}` : "HISTORY --");

  const providerList = byId("readiness-providers");
  providerList.replaceChildren();
  readinessProviders.forEach((provider) => {
    const passed = provider.valid === true && ["pass", "operator-confirmed"].includes(provider.result);
    const item = document.createElement("li");
    item.className = passed ? "provider-pass" : "provider-fail";
    const mark = document.createElement("i");
    mark.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = readinessLabel(provider.provider_id);
    item.append(mark, label);
    const reasons = Array.isArray(provider.reason_codes) ? provider.reason_codes : [];
    item.title = passed
      ? `${label.textContent}: ${upper(provider.result)}`
      : `${label.textContent}: ${provider.valid === false ? "EXPIRED" : upper(provider.result)}${reasons.length ? ` — ${reasons.join(", ")}` : ""}`;
    providerList.append(item);
  });

  const switchButton = byId("switch-action");
  const switchGate = payload.switch_gate || (
    payload.switch_block_reason ? "disabled" : payload.switchable ? "ready" : "state-unavailable"
  );
  const switchBlocked = switchGate !== "ready";
  switchButton.disabled = actionBusy || !payload.switchable;
  const switchLabels = {
    disabled: "RL SWITCH DISABLED",
    "waiting-arm-hold": "WAITING FOR ARM HOLD",
    "state-unavailable": "SWITCH UNAVAILABLE",
  };
  switchButton.querySelector("span").textContent = switchBlocked
    ? switchLabels[switchGate] || "SWITCH UNAVAILABLE"
    : state === "RL_ACTIVE" ? "RETURN TO TELEOP" : "SWITCH TO RL";
  switchButton.title = payload.switch_block_reason || "";
  const gateStatus = byId("switch-gate-status");
  if (switchGate === "ready") {
    gateStatus.textContent = payload.arm_hold_ready === true ? "ARM HOLD READY" : "RL SWITCH READY";
    gateStatus.className = "switch-gate gate-ready";
  } else if (switchGate === "waiting-arm-hold") {
    gateStatus.textContent = "ARM HOLD NOT READY";
    gateStatus.className = "switch-gate gate-waiting";
  } else if (switchGate === "disabled") {
    gateStatus.textContent = "RL SWITCH NOT AUTHORIZED";
    gateStatus.className = "switch-gate gate-disabled";
  } else {
    gateStatus.textContent = "CURRENT STATE CANNOT SWITCH";
    gateStatus.className = "switch-gate";
  }
  const operatorConfirmed = readinessProviders.some((provider) =>
    provider.provider_id === "operator-confirmation-v1" &&
    provider.valid === true &&
    provider.result === "operator-confirmed"
  );
  const confirmButton = byId("confirm-action");
  confirmButton.disabled = actionBusy || payload.stopped === true || payload.connected !== true || operatorConfirmed;
  confirmButton.querySelector("span").textContent = operatorConfirmed
    ? "OPERATOR CONFIRMED"
    : "CONFIRM OPERATOR";
  byId("stop-action").disabled = actionBusy || payload.stopped === true;
}

function removeDynamicSkeleton() {
  document.querySelectorAll("#openxr-skeleton .skeleton-dynamic").forEach((node) => node.remove());
}

function renderOpenXR(telemetry) {
  const payload = telemetry.payload || {};
  const nodes = Array.isArray(payload.nodes) ? payload.nodes.filter((node) =>
    node.valid !== false && finite(node.x) && finite(node.y) && finite(node.z)
  ) : [];
  setText("openxr-device", upper(payload.device));
  setText("openxr-runtime", upper(payload.runtime));
  setText("openxr-session", payload.session_focused === true ? "FOCUSED" : payload.session_running === true ? "VISIBLE" : "OFFLINE");
  setText("openxr-side", upper(payload.side));
  setText("openxr-joints", `${payload.valid_joint_count ?? nodes.length} / ${payload.joint_count ?? 26}`);
  setText("openxr-rate", finite(telemetry.rate_hz) ? `${telemetry.rate_hz.toFixed(0)} Hz` : "-- Hz");
  setText("openxr-age", finite(telemetry.age_ms) ? `${telemetry.age_ms.toFixed(1)} ms` : "-- ms");
  setText("openxr-pinch", finite(payload.pinch_m) ? `${(payload.pinch_m * 1000).toFixed(0)} mm` : "-- mm");
  setText("openxr-sequence", payload.source_sequence ?? telemetry.sequence);
  setText("coupling-rate", finite(telemetry.rate_hz) ? `${telemetry.rate_hz.toFixed(0)} HZ` : "-- HZ");
  const wrist = Array.isArray(payload.wrist_position_m) ? payload.wrist_position_m : null;
  setText(
    "openxr-wrist",
    wrist && wrist.length >= 3
      ? `WRIST ${fixed(wrist[0], 3)} / ${fixed(wrist[1], 3)} / ${fixed(wrist[2], 3)} M`
      : "WRIST 6DOF + 26 JOINTS",
  );
  setText(
    "openxr-mode",
    payload.mode === "fake"
      ? payload.drives_current_command
        ? "SIMULATED CONTROL"
        : "SIMULATED INPUT"
      : payload.control_correlated && payload.drives_current_command
        ? "CONTROL INPUT"
        : payload.control_correlated
          ? "SHADOW INPUT"
          : payload.connected
          ? "VISUAL ONLY"
          : "NO SOURCE",
  );

  removeDynamicSkeleton();
  const empty = byId("openxr-empty");
  if (["stale", "fault"].includes(telemetry.health) || nodes.length === 0) {
    empty.style.display = "flex";
    return;
  }
  empty.style.display = "none";

  const projected = nodes.map((node) => ({
    id: node.id,
    parent: node.parent,
    root: node.parent === null || node.parent === undefined || Number(node.parent) < 0,
    x: node.x + node.z * 0.28,
    y: -node.y + node.z * 0.06,
  }));
  const xs = projected.map((point) => point.x);
  const ys = projected.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = Math.max(maxX - minX, 1e-6);
  const spanY = Math.max(maxY - minY, 1e-6);
  const left = 45;
  const top = 24;
  const width = 270;
  const height = 280;
  const scale = Math.min(width / spanX, height / spanY);
  const offsetX = left + (width - spanX * scale) / 2;
  const offsetY = top + (height - spanY * scale) / 2;
  const screen = new Map();
  projected.forEach((point) => {
    screen.set(point.id, {
      ...point,
      sx: offsetX + (point.x - minX) * scale,
      sy: offsetY + (point.y - minY) * scale,
    });
  });

  const svg = byId("openxr-skeleton");
  screen.forEach((point) => {
    const parent = screen.get(point.parent);
    if (!parent) {
      return;
    }
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", parent.sx);
    line.setAttribute("y1", parent.sy);
    line.setAttribute("x2", point.sx);
    line.setAttribute("y2", point.sy);
    line.setAttribute("class", "skeleton-bone skeleton-dynamic");
    svg.appendChild(line);
  });
  screen.forEach((point) => {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", point.sx);
    circle.setAttribute("cy", point.sy);
    circle.setAttribute("r", point.root ? 5.5 : 3.8);
    circle.setAttribute(
      "class",
      `skeleton-node skeleton-dynamic${point.root ? " skeleton-root" : ""}`,
    );
    svg.appendChild(circle);
  });
}

function jointSignature(joints) {
  return joints.map((joint) => joint.name || joint.index).join("|");
}

function buildJointRows(joints) {
  const container = byId("joint-tracks");
  const signature = jointSignature(joints);
  if (container.dataset.signature === signature) {
    return;
  }
  container.replaceChildren();
  container.dataset.signature = signature;
  joints.forEach((joint, index) => {
    const row = document.createElement("div");
    row.className = "joint-row";
    row.dataset.index = index;
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `Inspect ${joint.name || `joint ${index + 1}`}`);
    const select = () => {
      selectedJointIndex = index;
      if (latestSnapshot) {
        renderLinker(source(latestSnapshot, "linker"));
      }
    };
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });

    const name = document.createElement("span");
    name.className = "joint-name";
    name.textContent = String(index + 1).padStart(2, "0");
    name.title = joint.name || `Joint ${index + 1}`;

    const track = document.createElement("div");
    track.className = "joint-track";
    const actual = document.createElement("i");
    actual.className = "joint-actual";
    const requested = document.createElement("i");
    requested.className = "joint-requested";
    const authorized = document.createElement("i");
    authorized.className = "joint-authorized";
    const effective = document.createElement("i");
    effective.className = "joint-effective";
    track.append(effective, authorized, requested, actual);

    const value = document.createElement("span");
    value.className = "joint-value";
    row.append(name, track, value);
    container.appendChild(row);
  });
}

function fraction(value, lower, upper) {
  if (!finite(value) || !finite(lower) || !finite(upper) || upper <= lower) {
    return null;
  }
  return Math.max(0, Math.min(1, (value - lower) / (upper - lower)));
}

function renderLinker(telemetry) {
  const payload = telemetry.payload || {};
  let joints = Array.isArray(payload.joints) ? payload.joints : [];
  if (joints.length === 0) {
    joints = Array.from({ length: 16 }, (_, index) => ({
      index,
      name: `joint_${String(index + 1).padStart(2, "0")}`,
      measured: null,
      target: null,
      lower: 0,
      upper: 1,
    }));
  }
  buildJointRows(joints);
  document.querySelectorAll("#joint-tracks .joint-row").forEach((row, index) => {
    const joint = joints[index] || {};
    const measured = joint.measured;
    const requested = joint.requested_target;
    const authorized = joint.authorized_target;
    const effective = joint.effective_target ?? joint.target;
    const actualFraction = fraction(measured, joint.lower, joint.upper);
    const layers = [
      [".joint-actual", actualFraction],
      [".joint-requested", fraction(requested, joint.lower, joint.upper)],
      [".joint-authorized", fraction(authorized, joint.lower, joint.upper)],
      [".joint-effective", fraction(effective, joint.lower, joint.upper)],
    ];
    layers.forEach(([selector, layerFraction]) => {
      const marker = row.querySelector(selector);
      marker.style.display = layerFraction === null ? "none" : "block";
      if (layerFraction !== null) {
        marker.style.left = `${layerFraction * 100}%`;
      }
    });
    const degrees = finite(measured) ? measured * 180 / Math.PI : null;
    row.querySelector(".joint-value").textContent = finite(degrees) ? `${degrees.toFixed(1)}°` : "--";
    row.classList.toggle("selected", index === selectedJointIndex);
    row.title = [
      joint.name || `Joint ${index + 1}`,
      `actual ${fixed(degrees, 1)} deg`,
      `requested ${fixed(finite(requested) ? requested * 180 / Math.PI : null, 1)} deg`,
      `authorized ${fixed(finite(authorized) ? authorized * 180 / Math.PI : null, 1)} deg`,
      `effective ${fixed(finite(effective) ? effective * 180 / Math.PI : null, 1)} deg`,
    ].join(" | ");
  });

  setText("linker-owner", upper(payload.owner));
  const acknowledgement = payload.acknowledgement_missing === true
    ? "MISSING"
    : payload.epoch_match === false
      ? "EPOCH MISMATCH"
      : payload.command_identity_match === false
        ? "ID MISMATCH"
    : payload.acknowledgement || payload.ack || (
      payload.runtime_tick !== null && payload.runtime_tick !== undefined ? "MISSING" : null
    );
  setText("linker-ack", upper(acknowledgement));
  const errorRad = finite(payload.maximum_error_rad)
    ? payload.maximum_error_rad
    : joints.reduce((maximum, joint) => {
        const target = joint.effective_target ?? joint.target;
        return finite(joint.measured) && finite(target)
          ? Math.max(maximum, Math.abs(joint.measured - target))
          : maximum;
      }, 0);
  setText("linker-error", finite(errorRad) ? `${(errorRad * 180 / Math.PI).toFixed(1)}°` : "--");
  const rmsRad = finite(payload.rms_error_rad) ? payload.rms_error_rad : null;
  setText("linker-rms", finite(rmsRad) ? `${(rmsRad * 180 / Math.PI).toFixed(1)}°` : "--");
  setText("linker-sequence", payload.state_sequence ?? telemetry.sequence);

  const selected = joints[selectedJointIndex];
  if (selected) {
    const layerDegrees = (value) => finite(value)
      ? `${(value * 180 / Math.PI).toFixed(1)}°`
      : "--";
    setText(
      "joint-detail",
      `${String(selectedJointIndex + 1).padStart(2, "0")} ${upper(selected.name)}  `
      + `A ${layerDegrees(selected.measured)}  `
      + `R ${layerDegrees(selected.requested_target)}  `
      + `U ${layerDegrees(selected.authorized_target)}  `
      + `E ${layerDegrees(selected.effective_target ?? selected.target)}`,
    );
  } else {
    setText("joint-detail", "SELECT A JOINT FOR LAYER VALUES");
  }
}

function prepareCanvas(canvas) {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.floor(canvas.clientWidth));
  const height = Math.max(1, Math.floor(canvas.clientHeight));
  const backingWidth = Math.floor(width * ratio);
  const backingHeight = Math.floor(height * ratio);
  if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
    canvas.width = backingWidth;
    canvas.height = backingHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function drawGrid(context, width, height, padding) {
  context.strokeStyle = GRID;
  context.lineWidth = 1;
  context.globalAlpha = 0.8;
  for (let index = 0; index <= 4; index += 1) {
    const x = padding + (width - padding * 2) * index / 4;
    const y = padding + (height - padding * 2) * index / 4;
    context.beginPath();
    context.moveTo(x, padding);
    context.lineTo(x, height - padding);
    context.stroke();
    context.beginPath();
    context.moveTo(padding, y);
    context.lineTo(width - padding, y);
    context.stroke();
  }
  context.globalAlpha = 1;
}

function drawTrail(context, points, color, x, y) {
  if (points.length < 2) {
    return;
  }
  context.strokeStyle = color;
  context.lineWidth = 2;
  context.lineJoin = "round";
  context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) {
      context.moveTo(x(point), y(point));
    } else {
      context.lineTo(x(point), y(point));
    }
  });
  context.stroke();
}

function drawEndpoint(context, point, color, x, y) {
  if (!point) {
    return;
  }
  const px = x(point);
  const py = y(point);
  context.strokeStyle = color;
  context.lineWidth = 1.5;
  context.beginPath();
  context.moveTo(px - 6, py);
  context.lineTo(px + 6, py);
  context.moveTo(px, py - 6);
  context.lineTo(px, py + 6);
  context.stroke();
  context.beginPath();
  context.arc(px, py, 3.5, 0, Math.PI * 2);
  context.stroke();
}

function drawArmPlot(canvasId, firstIndex, secondIndex) {
  const canvas = byId(canvasId);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 22;
  drawGrid(context, width, height, padding);
  const all = armTrail.actual.concat(armTrail.target);
  if (all.length === 0) {
    return;
  }
  const firstValues = all.map((point) => point[firstIndex]);
  const secondValues = all.map((point) => point[secondIndex]);
  let minFirst = Math.min(...firstValues);
  let maxFirst = Math.max(...firstValues);
  let minSecond = Math.min(...secondValues);
  let maxSecond = Math.max(...secondValues);
  const firstPadding = Math.max((maxFirst - minFirst) * 0.18, 25);
  const secondPadding = Math.max((maxSecond - minSecond) * 0.18, 25);
  minFirst -= firstPadding;
  maxFirst += firstPadding;
  minSecond -= secondPadding;
  maxSecond += secondPadding;
  const x = (point) => padding + (point[firstIndex] - minFirst) / (maxFirst - minFirst) * (width - padding * 2);
  const y = (point) => height - padding - (point[secondIndex] - minSecond) / (maxSecond - minSecond) * (height - padding * 2);
  drawTrail(context, armTrail.actual, ACTUAL, x, y);
  context.setLineDash([5, 4]);
  drawTrail(context, armTrail.target, TARGET, x, y);
  context.setLineDash([]);
  drawEndpoint(context, armTrail.actual.at(-1), ACTUAL, x, y);
  drawEndpoint(context, armTrail.target.at(-1), TARGET, x, y);
}

function renderOrientationGlyph(tcp) {
  const svg = byId("arm-orientation");
  if (!tcp || tcp.length < 6 || !tcp.slice(3, 6).every(finite)) {
    svg.style.opacity = "0.25";
    return;
  }
  svg.style.opacity = "1";
  const [roll, pitch, yaw] = tcp.slice(3, 6).map((value) => value * Math.PI / 180);
  const sr = Math.sin(roll);
  const cr = Math.cos(roll);
  const sp = Math.sin(pitch);
  const cp = Math.cos(pitch);
  const sy = Math.sin(yaw);
  const cy = Math.cos(yaw);
  const rotation = [
    [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
    [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
    [-sp, cp * sr, cp * cr],
  ];
  const axes = [
    [rotation[0][0], rotation[1][0], rotation[2][0]],
    [rotation[0][1], rotation[1][1], rotation[2][1]],
    [rotation[0][2], rotation[1][2], rotation[2][2]],
  ];
  ["x", "y", "z"].forEach((name, index) => {
    const axis = axes[index];
    const x = 48 + axis[0] * 25 - axis[1] * 12;
    const y = 27 - axis[2] * 21 + axis[0] * 5 + axis[1] * 5;
    const line = byId(`orientation-${name}`);
    line.setAttribute("x2", x.toFixed(2));
    line.setAttribute("y2", y.toFixed(2));
    const label = byId(`orientation-${name}-label`);
    label.setAttribute("x", (x + 3).toFixed(2));
    label.setAttribute("y", (y + 3).toFixed(2));
  });
}

function renderHitbot(telemetry) {
  const payload = telemetry.payload || {};
  const actual = Array.isArray(payload.tcp_actual) ? payload.tcp_actual : null;
  const target = Array.isArray(payload.tcp_target) ? payload.tcp_target : null;
  const sequence = payload.source_sequence ?? telemetry.sequence;
  if (
    armTrail.actual.length === 0 &&
    Array.isArray(payload.trail_actual) &&
    Array.isArray(payload.trail_target)
  ) {
    armTrail.actual = payload.trail_actual
      .filter((point) => Array.isArray(point) && point.length >= 3 && point.slice(0, 3).every(finite))
      .slice(-HISTORY_LIMIT);
    armTrail.target = payload.trail_target
      .filter((point) => Array.isArray(point) && point.length >= 3 && point.slice(0, 3).every(finite))
      .slice(-HISTORY_LIMIT);
  }
  if (
    sequence !== armTrail.lastSequence &&
    actual && target &&
    actual.length >= 3 && target.length >= 3 &&
    actual.slice(0, 3).every(finite) && target.slice(0, 3).every(finite)
  ) {
    armTrail.actual.push(actual.slice(0, 6));
    armTrail.target.push(target.slice(0, 6));
    if (armTrail.actual.length > HISTORY_LIMIT) {
      armTrail.actual.shift();
      armTrail.target.shift();
    }
    armTrail.lastSequence = sequence;
  }
  drawArmPlot("arm-xy", 0, 1);
  drawArmPlot("arm-xz", 0, 2);

  const hitbotMode = payload.hold_state === "FAULT_HOLD"
    ? "HOLD FAULT"
    : payload.control_mode === "hold" && payload.hold_verified === true
      ? "HOLD VERIFIED"
    : payload.control_mode === "hold"
      ? "HOLDING"
      : payload.controller_alive === true && payload.motion_sample_fresh === false
        ? "CONTROLLER IDLE"
      : payload.mode === "synthetic"
    ? "SIMULATED"
    : payload.cycle_success === false
      ? "CYCLE DEGRADED"
      : payload.connected
        ? "LIVE TRACKING"
        : "NO SOURCE";
  setText("hitbot-mode", hitbotMode);
  byId("hitbot-mode").title = payload.failure_reason || payload.source_reason || "";
  setText(
    "hitbot-tcp",
    actual && actual.length >= 3
      ? `${fixed(actual[0], 0)} / ${fixed(actual[1], 0)} / ${fixed(actual[2], 0)}`
      : "--",
  );
  setText(
    "hitbot-rpy",
    actual && actual.length >= 6
      ? `${fixed(actual[3], 0)} / ${fixed(actual[4], 0)} / ${fixed(actual[5], 0)}`
      : "--",
  );
  renderOrientationGlyph(actual);
  setText("hitbot-ik", payload.ik_ok === true ? "OK" : payload.ik_ok === false ? "FAILED" : "--");
  setText("hitbot-hold", upper(payload.hold_state || "TELEOP"));
  const holdPositionError = payload.hold_position_error_mm;
  const holdOrientationError = payload.hold_orientation_error_deg;
  setText(
    "hitbot-hold-error",
    finite(holdPositionError) && finite(holdOrientationError)
      ? `${holdPositionError.toFixed(2)} mm / ${holdOrientationError.toFixed(2)} deg`
      : "--",
  );
  setText("hitbot-servo", finite(payload.servo_interval_ms) ? `${payload.servo_interval_ms.toFixed(1)} ms` : "-- ms");
  const cycleLatency = finite(payload.cycle_latency_ms)
    ? payload.cycle_latency_ms
    : payload.total_latency_ms;
  setText("hitbot-latency", finite(cycleLatency) ? `${cycleLatency.toFixed(1)} ms` : "-- ms");
  setText("hitbot-sequence", sequence);
  const holdState = upper(payload.hold_state || "TELEOP");
  setText(
    "mapping-anchor",
    payload.hold_verified === true
      ? "ANCHOR LOCKED"
      : holdState === "TELEOP"
        ? "ANCHOR LIVE"
        : holdState === "FAULT_HOLD"
          ? "ANCHOR FAULT"
          : "ANCHOR PENDING",
  );
  setText(
    "mapping-reanchor",
    holdState === "REANCHOR_ACKED" ? "RE-ANCHOR ACKED" : "RE-ANCHOR ARMED",
  );
}

function renderCamera(telemetry) {
  const payload = telemetry.payload || {};
  const healthy = telemetry.health === "healthy" && payload.connected === true;
  const panel = document.querySelector(".camera-panel");
  panel.classList.toggle("camera-ready", healthy);
  setText(
    "camera-mode",
    payload.mode === "synthetic" ? "SYNTHETIC" : healthy ? "RGB + DEPTH" : "NO SOURCE",
  );
  setText(
    "camera-rate",
    finite(telemetry.rate_hz) && telemetry.rate_hz > 0 ? `${telemetry.rate_hz.toFixed(0)} FPS` : "-- FPS",
  );
  setText("camera-model", upper(payload.device_model, "INTEL REALSENSE D435"));
  setText(
    "camera-resolution",
    finite(payload.color_width) && finite(payload.color_height)
      ? `${payload.color_width} × ${payload.color_height}`
      : "-- × --",
  );
  setText(
    "depth-resolution",
    finite(payload.depth_width) && finite(payload.depth_height)
      ? `${payload.depth_width} × ${payload.depth_height}`
      : "-- × --",
  );
  setText("camera-sequence", payload.source_sequence ?? telemetry.sequence);
  setText(
    "camera-reason",
    healthy
      ? payload.mode === "synthetic" ? "OFFLINE VISION REHEARSAL" : `SERIAL ${payload.serial || "--"}`
      : upper(payload.source_reason || payload.fault || "WAITING FOR D435"),
  );
  byId("camera-empty").title = payload.fault || payload.source_reason || "";
}

function drawSparkline(canvasId, values, color = ACTUAL) {
  const canvas = byId(canvasId);
  const { context, width, height } = prepareCanvas(canvas);
  context.strokeStyle = GRID;
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(0, height - 1);
  context.lineTo(width, height - 1);
  context.stroke();
  if (values.length < 2) {
    return;
  }
  const maximum = Math.max(10, ...values) * 1.15;
  context.strokeStyle = color;
  context.lineWidth = 1.5;
  context.beginPath();
  values.forEach((value, index) => {
    const x = index / Math.max(1, values.length - 1) * width;
    const y = height - Math.min(1, value / maximum) * (height - 4) - 2;
    if (index === 0) {
      context.moveTo(x, y);
    } else {
      context.lineTo(x, y);
    }
  });
  context.stroke();
}

function restoreLatencyHistory(telemetry, key, fallbackValue) {
  const sourceHistory = telemetry.payload && telemetry.payload.latency_history_ms;
  if (Array.isArray(sourceHistory)) {
    latencyHistory[key] = sourceHistory.filter(finite).slice(-HISTORY_LIMIT);
  } else {
    pushBounded(latencyHistory[key], fallbackValue);
  }
}

function renderLatency(openxr, linker, hitbot) {
  const openxrValue = openxr.payload && finite(openxr.payload.display_latency_ms)
    ? openxr.payload.display_latency_ms
    : openxr.age_ms;
  const linkerValue = linker.payload && finite(linker.payload.display_latency_ms)
    ? linker.payload.display_latency_ms
    : linker.age_ms;
  const hitbotValue = hitbot.payload && finite(hitbot.payload.display_latency_ms)
    ? hitbot.payload.display_latency_ms
    : hitbot.age_ms;
  restoreLatencyHistory(openxr, "openxr", openxrValue);
  restoreLatencyHistory(linker, "linker", linkerValue);
  restoreLatencyHistory(hitbot, "hitbot", hitbotValue);
  drawSparkline("latency-openxr", latencyHistory.openxr);
  drawSparkline("latency-linker", latencyHistory.linker);
  drawSparkline("latency-hitbot", latencyHistory.hitbot);
  setText("latency-openxr-value", finite(openxrValue) ? openxrValue.toFixed(0) : "--");
  setText("latency-linker-value", finite(linkerValue) ? linkerValue.toFixed(0) : "--");
  setText("latency-hitbot-value", finite(hitbotValue) ? hitbotValue.toFixed(0) : "--");
}

function render(snapshot) {
  latestSnapshot = snapshot;
  lastSnapshotWallTime = performance.now();
  const runtime = source(snapshot, "runtime");
  const openxr = source(snapshot, "openxr");
  const linker = source(snapshot, "linker");
  const hitbot = source(snapshot, "hitbot");
  const d435 = source(snapshot, "d435");
  setHealthChip("openxr-health", "OPENXR", openxr);
  setHealthChip("linker-health", "LINKER", linker);
  setHealthChip("hitbot-health", "HITBOT", hitbot);
  setHealthChip("d435-health", "D435", d435);
  renderRuntime(runtime);
  renderOpenXR(openxr);
  renderLinker(linker);
  renderHitbot(hitbot);
  renderCamera(d435);
  renderLatency(openxr, linker, hitbot);
  setText("snapshot-revision", `REVISION ${snapshot.revision ?? 0}`);
}

async function postAction(path) {
  if (actionBusy) {
    return;
  }
  actionBusy = true;
  if (latestSnapshot) {
    renderRuntime(source(latestSnapshot, "runtime"));
  }
  try {
    const response = await fetch(path, { method: "POST", cache: "no-store" });
    const result = await response.json();
    setText("runtime-message", result.message || (result.ok ? "Request accepted." : "Request rejected."));
  } catch (error) {
    setText("runtime-message", `Action request failed: ${error.message}`);
  } finally {
    actionBusy = false;
    if (latestSnapshot) {
      renderRuntime(source(latestSnapshot, "runtime"));
    }
  }
}

function connectStream() {
  if (eventSource) {
    eventSource.close();
  }
  const status = byId("stream-status");
  status.textContent = "STREAM CONNECTING";
  status.className = "stream-status";
  eventSource = new EventSource("/api/live");
  eventSource.addEventListener("open", () => {
    status.textContent = "STREAM LIVE";
    status.className = "stream-status connected";
  });
  eventSource.addEventListener("snapshot", (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (error) {
      status.textContent = "STREAM DATA FAULT";
      status.className = "stream-status disconnected";
    }
  });
  eventSource.addEventListener("error", () => {
    status.textContent = "STREAM RECONNECTING";
    status.className = "stream-status disconnected";
  });
}

async function loadInitialSnapshot() {
  const response = await fetch("/api/snapshot", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Snapshot request failed with HTTP ${response.status}`);
  }
  render(await response.json());
  const status = byId("stream-status");
  status.textContent = "SNAPSHOT READY";
  status.className = "stream-status connected";
}

async function verifyFonts() {
  if (!document.fonts) {
    throw new Error("The browser does not expose the Font Loading API.");
  }
  await Promise.all([
    document.fonts.load("400 16px 'Dex UI'", "DEX CONTROL LIVE"),
    document.fonts.load("700 16px 'Dex UI'", "OPENXR LINKER HITBOT"),
    document.fonts.load("400 16px 'Dex Mono'", "0123456789"),
  ]);
  const loaded =
    document.fonts.check("400 16px 'Dex UI'", "DEX CONTROL LIVE") &&
    document.fonts.check("700 16px 'Dex UI'", "OPENXR LINKER HITBOT") &&
    document.fonts.check("400 16px 'Dex Mono'", "0123456789");
  if (!loaded) {
    throw new Error("A required bundled WOFF2 face did not load.");
  }
}

async function boot() {
  try {
    await verifyFonts();
    document.body.classList.remove("font-loading");
    document.body.classList.add("font-ready");
    await loadInitialSnapshot();
    connectStream();
  } catch (error) {
    document.body.classList.remove("font-loading");
    document.body.classList.add("font-fault");
    setText("font-gate-title", "INTERFACE ASSET FAULT");
    setText("font-gate-detail", error.message);
  }
}

byId("confirm-action").addEventListener("click", () => postAction("/api/confirm"));
byId("switch-action").addEventListener("click", () => postAction("/api/switch"));
byId("stop-action").addEventListener("click", () => postAction("/api/stop"));
document.addEventListener("keydown", (event) => {
  if ((event.key === "F12" || event.key === " ") && !event.repeat) {
    event.preventDefault();
    if (!byId("switch-action").disabled) {
      postAction("/api/switch");
    }
  } else if (event.key === "Escape" && !event.repeat) {
    event.preventDefault();
    if (!byId("stop-action").disabled) {
      postAction("/api/stop");
    }
  }
});

window.addEventListener("resize", () => {
  drawArmPlot("arm-xy", 0, 1);
  drawArmPlot("arm-xz", 0, 2);
  drawSparkline("latency-openxr", latencyHistory.openxr);
  drawSparkline("latency-linker", latencyHistory.linker);
  drawSparkline("latency-hitbot", latencyHistory.hitbot);
});

setInterval(() => {
  setText("local-clock", new Date().toLocaleTimeString("en-US", { hour12: false }));
  if (lastSnapshotWallTime && performance.now() - lastSnapshotWallTime > 1000) {
    const status = byId("stream-status");
    status.textContent = "STREAM STALE";
    status.className = "stream-status disconnected";
  }
}, 250);

boot();
