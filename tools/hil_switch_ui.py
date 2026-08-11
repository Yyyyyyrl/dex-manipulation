#!/usr/bin/env python3
"""Small real-hand HIL UI for virtual teleop / synthetic-policy switching.

This commissioning tool intentionally uses the production runtime, handoff,
safety, mapping, gateway, and pinned G20 CAN driver.  It replaces only the
unavailable Manus and PCsensor devices with bounded virtual sources.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import can
import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from dex_contracts import (  # noqa: E402
    PROTOCOL_VERSION,
    AcknowledgementLevel,
    MessageIdentity,
    ResourceId,
    SourceHealth,
    TeleopHandCandidate,
    TimestampedSample,
)
from dex_hardware_linker import (  # noqa: E402
    GatewayConfig,
    LinkerGateway,
    LinkerMapper,
    LinkerSdkTransport,
)
from dex_runtime.application import HandOnlyRuntime  # noqa: E402
from dex_runtime.handoff import HandoffState  # noqa: E402
from dex_runtime.operator_switch import OperatorSwitchEvent, SwitchEdge  # noqa: E402
from dex_runtime.preflight import preflight_deployment  # noqa: E402
from dex_runtime.status import RuntimeStatus  # noqa: E402
from dex_teleop_adapters import ManusSourceStatus  # noqa: E402
from policy_package_factory import rewrite_manifest, write_test_package  # noqa: E402
from test_deployment_preflight import _write_config  # noqa: E402

CAN_ID = 0x28
CONTROL_PERIOD_S = 0.1
TELEOP_FREQUENCY_HZ = 0.18
TELEOP_JOINTS = (2, 5, 8, 11)
POLICY_JOINTS = (1, 4, 7, 10, 15)
FINGER_PLOT_JOINTS = (15, 2, 5, 8, 11)
FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def _query_frame(bus: can.BusABC, command: int) -> list[int]:
    bus.send(
        can.Message(
            arbitration_id=CAN_ID,
            data=[command],
            is_extended_id=False,
        )
    )
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        message = bus.recv(timeout=max(0.0, deadline - time.monotonic()))
        if (
            message is not None
            and message.arbitration_id == CAN_ID
            and len(message.data) == 7
            and message.data[0] == command
        ):
            return list(message.data[1:])
    raise TimeoutError(f"G20 did not answer read-only frame 0x{command:02X}")


def read_current_native(channel: str = "can0") -> tuple[int, ...]:
    """Read G20 position slots without importing or opening the SDK."""

    bus = can.Bus(interface="socketcan", channel=channel)
    try:
        fingers = [_query_frame(bus, command) for command in range(0x41, 0x46)]
    finally:
        bus.shutdown()
    native = [0] * 20
    for index, finger in enumerate(fingers):
        native[index] = finger[2]
        native[index + 5] = finger[0]
        native[index + 15] = finger[5]
    native[10] = fingers[0][1]
    return tuple(native)


def choose_policy_action(
    semantic: tuple[float, ...], mapper: LinkerMapper, magnitude: float
) -> tuple[float, ...]:
    action = [0.0] * len(semantic)
    for index in POLICY_JOINTS:
        joint = mapper.calibration.joints[index]
        room_up = joint.upper - semantic[index]
        room_down = semantic[index] - joint.lower
        action[index] = magnitude if room_up >= room_down else -magnitude
    return tuple(action)


def write_biased_policy(
    directory: Path, action: tuple[float, ...]
) -> Path:
    package = write_test_package(directory)
    actor_path = package / "actor.safetensors"
    actor_state = load_file(str(actor_path))
    actor_state = {
        name: torch.zeros_like(value) for name, value in actor_state.items()
    }
    actor_state["mu.bias"] = torch.tensor(
        action,
        dtype=actor_state["mu.bias"].dtype,
    )
    save_file(actor_state, str(actor_path))
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["display_name"] = "HIL bounded visible switch policy"
    manifest["weights"]["actor"]["sha256"] = hashlib.sha256(
        actor_path.read_bytes()
    ).hexdigest()
    rewrite_manifest(package, manifest)
    return package


class ExistingSettingsLinkerTransport(LinkerSdkTransport):
    """Use the pinned SDK while preserving the hand's current speed/torque."""

    acknowledgement_level = AcknowledgementLevel.SENT_TO_BUS

    def open(self) -> None:
        self._check_thread()
        scripts = self._verify_driver()
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from LinkerHand.linker_hand_api import LinkerHandApi

        self._api = LinkerHandApi(
            hand_type=self.side,
            hand_joint=self.hand_joint,
            can=self.can_channel,
        )

    def close(self) -> None:
        self._check_thread()
        if self._api is not None:
            self._api.hand.close_can_interface()
            self._api = None


class VirtualManusSource:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_time_ns: int | None = None
        self._sequence = 0

    def start(self, callback) -> None:
        def publish() -> None:
            while not self._stop.is_set():
                now_ns = time.monotonic_ns()
                callback(
                    TimestampedSample(
                        payload="virtual-visible-wave",
                        generated_time_ns=now_ns,
                        received_time_ns=now_ns,
                        sequence=self._sequence,
                        source_health=SourceHealth.HEALTHY,
                        validity_mask=(True,),
                        coordinate_frame_id="virtual-wave-frame",
                        units="meter",
                    )
                )
                self._last_time_ns = now_ns
                self._sequence += 1
                self._stop.wait(0.04)

        self._thread = threading.Thread(
            target=publish,
            name="virtual-manus-visible-wave",
            daemon=True,
        )
        self._thread.start()

    def status(self, _now_ns: int) -> ManusSourceStatus:
        return ManusSourceStatus(
            "virtual-manus-visible-wave",
            SourceHealth.HEALTHY,
            self._sequence,
            self._last_time_ns,
            "",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)


class VisibleWaveRetargeter:
    def __init__(
        self,
        gateway: LinkerGateway,
        preflight,
        amplitude_rad: float,
    ) -> None:
        self.gateway = gateway
        self.mapper = preflight.mapper
        self.hand_model = self.mapper.calibration.hand_model
        self.hand_side = self.mapper.calibration.hand_side
        self.semantic_schema_id = self.mapper.calibration.semantic_schema_id
        self.amplitude_rad = amplitude_rad
        self.sequence = 0
        self.base: tuple[float, ...] | None = None
        self.started_ns: int | None = None

    def reset(self) -> None:
        self.sequence = 0
        self.base = None
        self.started_ns = None

    def retarget(
        self,
        sample: TimestampedSample,
        *,
        control_session_id: str,
        control_epoch: int,
        task_id: str | None,
        task_version: str | None,
    ) -> TeleopHandCandidate:
        if self.base is None:
            state = self.gateway.latest_state
            if state is None:
                raise RuntimeError("current hand state unavailable for virtual teleop")
            self.base = tuple(state.semantic_position)
            self.started_ns = sample.received_time_ns
        elapsed_s = (sample.received_time_ns - self.started_ns) / 1_000_000_000
        phase = 2.0 * math.pi * TELEOP_FREQUENCY_HZ * elapsed_s
        target = list(self.base)
        for offset, index in enumerate(TELEOP_JOINTS):
            joint = self.mapper.calibration.joints[index]
            value = self.base[index] + self.amplitude_rad * math.sin(
                phase + offset * math.pi / 2.0
            )
            target[index] = min(max(value, joint.lower), joint.upper)
        candidate = TeleopHandCandidate(
            identity=MessageIdentity(
                protocol_version=PROTOCOL_VERSION,
                control_session_id=control_session_id,
                source_id="virtual-visible-wave-retargeter",
                resource_id=ResourceId.HAND,
                hand_model=self.hand_model,
                hand_side=self.hand_side,
                semantic_schema_id=self.semantic_schema_id,
                task_id=task_id,
                task_version=task_version,
                policy_package_id=None,
                calibration_id=None,
                control_epoch=control_epoch,
                sequence=self.sequence,
            ),
            semantic_position=tuple(target),
            generated_time_ns=sample.received_time_ns,
            valid_until_ns=sample.received_time_ns + 300_000_000,
            source_state_sequence=sample.sequence,
            diagnostics=(("pattern", "four-finger-pip-wave"),),
        )
        self.sequence += 1
        return candidate


class WindowF12Switch:
    status = "healthy-window-f12"

    def __init__(self) -> None:
        self.callback = None
        self.sequence = 0

    def start(self, callback) -> None:
        self.callback = callback

    def tap(self) -> None:
        if self.callback is None:
            raise RuntimeError("window F12 source is not started")
        now_ns = time.monotonic_ns()
        for edge in (SwitchEdge.PRESS, SwitchEdge.RELEASE):
            self.callback(
                OperatorSwitchEvent(
                    source_id="window-f12",
                    key="F12",
                    edge=edge,
                    generated_time_ns=now_ns,
                    received_time_ns=now_ns,
                    sequence=self.sequence,
                )
            )
            self.sequence += 1

    def stop(self) -> None:
        pass


class QueueStatusRenderer:
    def __init__(self) -> None:
        self.queue: queue.Queue[RuntimeStatus] = queue.Queue(maxsize=1)

    def render(self, status: RuntimeStatus) -> None:
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(status)
        except queue.Full:
            pass

    def close(self) -> None:
        pass


def build_runtime(
    amplitude_rad: float,
    policy_action_magnitude: float,
):
    work = Path(tempfile.mkdtemp(prefix="dex-hil-ui-", dir="/tmp"))
    mapper = LinkerMapper.load()
    current_semantic = mapper.inverse(read_current_native())
    policy_action = choose_policy_action(
        current_semantic,
        mapper,
        policy_action_magnitude,
    )
    package = write_biased_policy(work / "store" / "visible-policy", policy_action)
    config_path = _write_config(work, package)
    config = json.loads(config_path.read_text())
    config["binding_id"] = "linker-g20-left-hil-visible-switch-v1"
    config["control_session_id"] = f"hil-ui-{time.monotonic_ns()}"
    config["gateway"] = {
        "transport": "linker-sdk",
        "gateway_id": "linker-g20-left",
        "gateway_hz": 50.0,
        "state_stale_ns": 100_000_000,
        "command_watchdog_ns": 1_000_000_000,
        "maximum_round_trip_error_rad": 0.01,
        "linker_sdk": {
            "sdk_root": str(ROOT / ".vendor/linkerhand-ros-sdk"),
            "can_channel": "can0",
            "speed": [0, 0, 0, 0, 0],
            "torque": [0, 0, 0, 0, 0],
        },
    }
    config["readiness"]["operator_confirmation_validity_ns"] = 600_000_000_000
    config["status"] = {"period_ns": 100_000_000, "use_ansi": False}
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    preflight = preflight_deployment(str(config_path))
    binding = preflight.binding
    sdk = binding.gateway.linker_sdk
    assert sdk is not None
    transport = ExistingSettingsLinkerTransport(
        sdk.sdk_root,
        side=binding.hand.side,
        hand_joint=binding.hand.hand_joint,
        can_channel=sdk.can_channel,
        speed=sdk.speed,
        torque=sdk.torque,
    )
    gateway = LinkerGateway(
        GatewayConfig(
            binding.gateway.gateway_id,
            binding.control_session_id,
            binding.gateway.gateway_hz,
            binding.gateway.state_stale_ns,
            binding.gateway.command_watchdog_ns,
            binding.gateway.maximum_round_trip_error_rad,
        ),
        preflight.mapper,
        transport,
    )
    switch = WindowF12Switch()
    renderer = QueueStatusRenderer()
    runtime = HandOnlyRuntime(
        preflight,
        gateway,
        VirtualManusSource(),
        VisibleWaveRetargeter(gateway, preflight, amplitude_rad),
        switch,
        status_renderer=renderer,
    )
    return work, policy_action, runtime, gateway, switch, renderer


class HilWindow:
    def __init__(
        self,
        root: tk.Tk,
        *,
        work: Path,
        policy_action: tuple[float, ...],
        runtime: HandOnlyRuntime,
        gateway: LinkerGateway,
        switch: WindowF12Switch,
        renderer: QueueStatusRenderer,
        auto_handback_s: float,
    ) -> None:
        self.root = root
        self.work = work
        self.policy_action = policy_action
        self.runtime = runtime
        self.gateway = gateway
        self.switch = switch
        self.renderer = renderer
        self.auto_handback_s = auto_handback_s
        self.status: RuntimeStatus | None = None
        self.outcome: dict[str, object] = {}
        self.confirmed = False
        self.f12_down = False
        self.active_started: float | None = None
        self.auto_handback_sent = False
        self.pending_stop = False
        self.close_when_stopped = False

        root.title("Dex HIL — Teleop / RL Switch")
        root.geometry("700x620")
        root.minsize(650, 560)
        root.configure(bg="#10151d")
        root.protocol("WM_DELETE_WINDOW", self.request_close)
        root.bind("<KeyPress-F12>", self.on_f12_press)
        root.bind("<KeyRelease-F12>", self.on_f12_release)
        root.bind("<Escape>", lambda _event: self.request_stop())

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#10151d")
        style.configure("TLabel", background="#10151d", foreground="#dfe7f2")
        style.configure("Title.TLabel", font=("Sans", 18, "bold"))
        style.configure("Value.TLabel", font=("Sans", 12, "bold"))
        style.configure("TButton", font=("Sans", 12, "bold"), padding=10)

        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="LinkerHand G20 实机控制切换", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text="F12：Teleop ↔ RL   |   Esc：安全停止",
        ).pack(anchor="w", pady=(2, 12))

        self.state_label = tk.Label(
            outer,
            text="CONNECTING",
            font=("Sans", 24, "bold"),
            fg="white",
            bg="#52606d",
            padx=18,
            pady=12,
        )
        self.state_label.pack(fill="x")

        grid = ttk.Frame(outer)
        grid.pack(fill="x", pady=14)
        self.values: dict[str, ttk.Label] = {}
        fields = (
            ("Mode", "mode"),
            ("Hand owner", "owner"),
            ("Epoch", "epoch"),
            ("Policy history", "history"),
            ("Readiness", "ready"),
            ("RL timeout", "timeout"),
        )
        for row, (label, key) in enumerate(fields):
            ttk.Label(grid, text=label + ":").grid(
                row=row, column=0, sticky="w", padx=(0, 18), pady=3
            )
            value = ttk.Label(grid, text="—", style="Value.TLabel")
            value.grid(row=row, column=1, sticky="w", pady=3)
            self.values[key] = value

        self.canvas = tk.Canvas(
            outer,
            height=190,
            bg="#171e28",
            highlightthickness=1,
            highlightbackground="#334155",
        )
        self.canvas.pack(fill="x", pady=(0, 12))

        self.message = ttk.Label(
            outer,
            text="正在连接 can0…",
            wraplength=640,
        )
        self.message.pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.f12_button = ttk.Button(
            buttons,
            text="F12 切换",
            command=self.tap_f12,
            state="disabled",
        )
        self.f12_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.stop_button = ttk.Button(
            buttons,
            text="安全停止",
            command=self.request_stop,
        )
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(8, 0))

        ttk.Label(
            outer,
            text=f"Logs: {work}",
            foreground="#8fa3b8",
        ).pack(anchor="w", pady=(14, 0))
        policy_parts = [
            f"{index}:{value:+.2f}"
            for index, value in enumerate(policy_action)
            if value
        ]
        ttk.Label(
            outer,
            text="RL action bias: " + ", ".join(policy_parts),
            foreground="#8fa3b8",
        ).pack(anchor="w")

        self.thread = threading.Thread(
            target=self.run_runtime,
            name="hil-ui-runtime",
            daemon=True,
        )
        self.thread.start()
        self.root.after(50, self.poll)

    def run_runtime(self) -> None:
        try:
            self.outcome["result"] = self.runtime.run(initial_input_timeout_s=5.0)
        except BaseException as exc:
            self.outcome["exception"] = exc

    def switchable(self) -> bool:
        if self.status is None:
            return False
        if self.status.state == HandoffState.RL_ACTIVE.value:
            return True
        return (
            self.status.state == HandoffState.RL_SHADOW.value
            and self.status.history_count is not None
            and self.status.history_required is not None
            and self.status.history_count >= self.status.history_required
            and self.status.readiness_ready
        )

    def tap_f12(self) -> None:
        if not self.switchable():
            self.message.configure(text="当前尚不可切换；等待 RL_SHADOW history/readiness 就绪。")
            return
        self.switch.tap()
        self.message.configure(text=f"F12 已发送：{self.status.state}")

    def on_f12_press(self, _event) -> str:
        if not self.f12_down:
            self.f12_down = True
            self.tap_f12()
        return "break"

    def on_f12_release(self, _event) -> str:
        self.f12_down = False
        return "break"

    def request_stop(self) -> None:
        self.pending_stop = True
        self.stop_button.configure(state="disabled", text="正在安全停止…")
        if self.status is not None and self.status.state == HandoffState.RL_ACTIVE.value:
            self.switch.tap()
            self.message.configure(text="已请求 RL hand-back；回到 Teleop 后停止。")
        elif self.status is None or self.status.state in (
            HandoffState.TELEOP_ACTIVE.value,
            HandoffState.RL_SHADOW.value,
        ):
            self.runtime.request_stop()

    def request_close(self) -> None:
        if not self.thread.is_alive():
            self.root.destroy()
            return
        self.close_when_stopped = True
        self.request_stop()

    def drain_status(self) -> None:
        while True:
            try:
                self.status = self.renderer.queue.get_nowait()
            except queue.Empty:
                return

    def update_status(self) -> None:
        status = self.status
        if status is None:
            return
        colors = {
            HandoffState.RL_ACTIVE.value: "#c2410c",
            HandoffState.RL_SHADOW.value: "#1d4ed8",
            HandoffState.TELEOP_ACTIVE.value: "#1d4ed8",
            HandoffState.SAFE_HOLD.value: "#b91c1c",
            HandoffState.ESTOP.value: "#991b1b",
        }
        self.state_label.configure(
            text=status.state,
            bg=colors.get(status.state, "#a16207"),
        )
        if status.state == HandoffState.RL_ACTIVE.value:
            mode = "RL：MCP/拇指缓慢偏置"
        elif status.state in (
            HandoffState.RL_SHADOW.value,
            HandoffState.TELEOP_ACTIVE.value,
        ):
            mode = "Teleop：四指 PIP 波动"
        else:
            mode = "平滑切换中"
        self.values["mode"].configure(text=mode)
        self.values["owner"].configure(text=status.hand_owner)
        self.values["epoch"].configure(text=str(status.control_epoch))
        history = (
            "—"
            if status.history_count is None or status.history_required is None
            else f"{status.history_count}/{status.history_required}"
        )
        self.values["history"].configure(text=history)
        self.values["ready"].configure(
            text="READY" if status.readiness_ready else "NOT READY"
        )
        self.f12_button.configure(
            state="normal" if self.switchable() and not self.pending_stop else "disabled"
        )
        if status.rejection_reason:
            self.message.configure(text="Rejected: " + status.rejection_reason)

    def update_timeout(self) -> None:
        status = self.status
        if status is None or status.state != HandoffState.RL_ACTIVE.value:
            self.active_started = None
            self.auto_handback_sent = False
            self.values["timeout"].configure(text="—")
            return
        if self.active_started is None:
            self.active_started = time.monotonic()
        remaining = max(0.0, self.auto_handback_s - (time.monotonic() - self.active_started))
        self.values["timeout"].configure(text=f"{remaining:.1f} s")
        if remaining <= 0.0 and not self.auto_handback_sent:
            self.auto_handback_sent = True
            self.switch.tap()
            self.message.configure(text="RL 达到自动时限，已请求 hand-back。")

    def draw_hand(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 500)
        height = max(canvas.winfo_height(), 180)
        state = self.gateway.latest_state
        if state is None:
            canvas.create_text(
                width / 2,
                height / 2,
                text="等待机械手状态…",
                fill="#cbd5e1",
                font=("Sans", 13),
            )
            return
        margin = 34
        slot = (width - 2 * margin) / len(FINGER_PLOT_JOINTS)
        for finger, (name, joint_index) in enumerate(
            zip(FINGER_NAMES, FINGER_PLOT_JOINTS, strict=False)
        ):
            joint = self.gateway.mapper.calibration.joints[joint_index]
            value = state.semantic_position[joint_index]
            fraction = (value - joint.lower) / (joint.upper - joint.lower)
            x0 = margin + finger * slot + slot * 0.25
            x1 = margin + finger * slot + slot * 0.75
            y0 = 24
            y1 = height - 42
            fill_y = y1 - fraction * (y1 - y0)
            canvas.create_rectangle(x0, y0, x1, y1, outline="#64748b", width=2)
            color = (
                "#f97316"
                if self.status is not None
                and self.status.state == HandoffState.RL_ACTIVE.value
                else "#3b82f6"
            )
            canvas.create_rectangle(x0 + 2, fill_y, x1 - 2, y1 - 2, fill=color, width=0)
            canvas.create_text(
                (x0 + x1) / 2,
                y1 + 16,
                text=name,
                fill="#cbd5e1",
                font=("Sans", 10),
            )
            canvas.create_text(
                (x0 + x1) / 2,
                12,
                text=f"{value:.2f}",
                fill="#e2e8f0",
                font=("Sans", 9),
            )

    def poll(self) -> None:
        self.drain_status()
        if not self.confirmed and self.runtime.wait_until_connected(0.0):
            self.runtime.confirm_operator("hil-ui-operator")
            self.confirmed = True
            self.message.configure(
                text="已连接并确认。等待 history 30/30 后按 F12 进入 RL。"
            )
        self.update_status()
        self.update_timeout()
        self.draw_hand()

        if self.pending_stop and self.status is not None and self.status.state in (
            HandoffState.TELEOP_ACTIVE.value,
            HandoffState.RL_SHADOW.value,
        ):
            self.runtime.request_stop()

        if not self.thread.is_alive():
            if "exception" in self.outcome:
                exc = self.outcome["exception"]
                self.state_label.configure(text="FAULT", bg="#991b1b")
                self.message.configure(text=f"Runtime fault: {type(exc).__name__}: {exc}")
            else:
                self.message.configure(text="Runtime 已安全停止；日志已写入。")
            self.f12_button.configure(state="disabled")
            self.stop_button.configure(state="disabled", text="已停止")
            if self.close_when_stopped:
                self.root.destroy()
            return
        self.root.after(50, self.poll)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teleop-amplitude-rad", type=float, default=0.08)
    parser.add_argument("--policy-action", type=float, default=0.12)
    parser.add_argument("--auto-handback-seconds", type=float, default=6.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate arguments/imports without opening CAN or a window",
    )
    args = parser.parse_args()
    if not 0.01 <= args.teleop_amplitude_rad <= 0.12:
        parser.error("--teleop-amplitude-rad must be within 0.01..0.12")
    if not 0.02 <= args.policy_action <= 0.20:
        parser.error("--policy-action must be within 0.02..0.20")
    if not 2.0 <= args.auto_handback_seconds <= 10.0:
        parser.error("--auto-handback-seconds must be within 2..10")
    return args


def main() -> int:
    args = parse_args()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "teleop_amplitude_rad": args.teleop_amplitude_rad,
                    "policy_action": args.policy_action,
                    "policy_delta_rad_per_tick": args.policy_action * 0.05,
                    "auto_handback_seconds": args.auto_handback_seconds,
                },
                sort_keys=True,
            )
        )
        return 0

    lock_path = Path("/tmp/dex-hil-switch-ui.lock")
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("another dex HIL switch UI is already running") from exc

    work, action, runtime, gateway, switch, renderer = build_runtime(
        args.teleop_amplitude_rad,
        args.policy_action,
    )
    root = tk.Tk()
    window = HilWindow(
        root,
        work=work,
        policy_action=action,
        runtime=runtime,
        gateway=gateway,
        switch=switch,
        renderer=renderer,
        auto_handback_s=args.auto_handback_seconds,
    )
    root.mainloop()
    if "exception" in window.outcome:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
