#!/usr/bin/env python3
"""Browser demo for teleop / RL hand switching on the real LinkerHand G20.

Runs the production hand-only runtime (real ``can0`` transport, or a hardware-free
``fake`` transport for verification) behind a stdlib HTTP server.  The English-only
console uses bundled fonts, Server-Sent Events, full 16-joint hand state,
OpenXR hand monitoring, and an optional clearly labelled synthetic arm source.
It adds no external runtime dependencies or hardware command endpoints.

    python tools/switch_web_demo.py --transport hand      # real hand on can0
    python tools/switch_web_demo.py --transport fake       # no hardware
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _die_with_parent() -> None:
    """preexec_fn: ask the kernel to SIGTERM this child if the demo process dies."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass

try:
    from tools.control_console import (
        ConsoleTelemetryPump,
        RealSenseD435Source,
        SyntheticArmTelemetry,
        SyntheticD435Source,
        make_console_server,
    )
    from tools.control_console.arm_listener import ArmTelemetryListener
    from tools.control_console.openxr_source import UdpOpenXRSource
    from tools.switch_demo_backend import (
        FINGER_NAMES,
        FINGER_PLOT_JOINTS,
        build_runtime,
    )
except ModuleNotFoundError:
    from control_console import (
        ConsoleTelemetryPump,
        RealSenseD435Source,
        SyntheticArmTelemetry,
        SyntheticD435Source,
        make_console_server,
    )
    from control_console.arm_listener import ArmTelemetryListener
    from control_console.openxr_source import UdpOpenXRSource
    from switch_demo_backend import (
        FINGER_NAMES,
        FINGER_PLOT_JOINTS,
        build_runtime,
    )

from dex_runtime.handoff import HandoffState
from dex_runtime.telemetry import TelemetryHub
from dex_teleop_adapters import OPENXR_PARENT_IDS

_STATE_COLORS = {
    HandoffState.RL_ACTIVE.value: "#c2410c",
    HandoffState.RL_SHADOW.value: "#1d4ed8",
    HandoffState.TELEOP_ACTIVE.value: "#1d4ed8",
    HandoffState.SAFE_HOLD.value: "#b91c1c",
    HandoffState.ESTOP.value: "#991b1b",
}

_HERE = Path(__file__).resolve().parent
_VR_BRIDGE = _HERE / "openxr_hand_bridge.py"
_DEFAULT_TELEOP_ROOT = "/home/user/dex_teleop"
_DEFAULT_VR_PYTHON = "/home/user/miniconda3/envs/dexmachina/bin/python"
_HOST_CAMERA_PYTHON = Path("/home/user/miniconda3/bin/python")
_DEFAULT_CAMERA_PYTHON = os.environ.get(
    "DEX_CAMERA_PYTHON",
    str(_HOST_CAMERA_PYTHON if _HOST_CAMERA_PYTHON.is_file() else Path(sys.executable)),
)


def start_vr_bridge(
    mode: str,
    *,
    host: str,
    hand_port: int,
    arm_port: int,
    vr_python: str,
    teleop_root: str,
    log_path: Path,
) -> subprocess.Popen | None:
    """Launch the single OpenXR producer that fans out to hand and arm ports."""

    if mode == "off":
        return None
    log = open(log_path, "w")
    command = [
        vr_python,
        str(_VR_BRIDGE),
        "--host", host,
        "--hand-port", str(hand_port),
        "--arm-port", str(arm_port),
        "--teleop-root", teleop_root,
    ]
    if mode == "fake":
        command.append("--fake")
    proc = subprocess.Popen(
        command,
        cwd=str(_HERE.parent),
        stdout=log, stderr=subprocess.STDOUT, preexec_fn=_die_with_parent,
    )
    print(
        f"[switch-web-demo] OpenXR bridge: {mode} -> "
        f"hand udp {hand_port}, arm udp {arm_port}"
    )
    return proc


class DemoController:
    """Owns the runtime thread and the periodic bookkeeping the UI depends on."""

    def __init__(
        self,
        *,
        work: Path,
        policy_action: tuple[float, ...],
        runtime,
        gateway,
        switch,
        renderer,
        auto_handback_s: float,
        allow_switch: bool = True,
        switch_block_reason: str | None = None,
        require_arm_hold_controller: bool = False,
        initial_input_timeout_s: float = 5.0,
    ) -> None:
        self.work = work
        self.policy_action = policy_action
        self.runtime = runtime
        self.gateway = gateway
        self.switch = switch
        self.renderer = renderer
        self.auto_handback_s = auto_handback_s
        self.allow_switch = allow_switch
        self.switch_block_reason = switch_block_reason
        self.require_arm_hold_controller = require_arm_hold_controller
        self.initial_input_timeout_s = initial_input_timeout_s
        self._arm_available = not require_arm_hold_controller
        self._last_arm_probe = 0.0

        self._lock = threading.Lock()
        self._status = None
        self._connected = False
        self._confirmed = False
        self._active_started: float | None = None
        self._auto_handback_sent = False
        self._pending_stop = False
        self._want_activate = False
        self._last_activate_tap = 0.0
        self._message = "Connecting to the runtime."
        self._outcome: dict[str, object] = {}

        self._runtime_thread = threading.Thread(
            target=self._run_runtime, name="switch-demo-runtime", daemon=True
        )
        self._controller_thread = threading.Thread(
            target=self._control_loop, name="switch-demo-controller", daemon=True
        )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._runtime_thread.start()
        self._controller_thread.start()

    def shutdown(self, timeout_s: float = 6.0) -> None:
        self.runtime.request_stop()
        self._runtime_thread.join(timeout_s)
        self._controller_thread.join(1.0)

    def _run_runtime(self) -> None:
        try:
            self._outcome["result"] = self.runtime.run(
                initial_input_timeout_s=self.initial_input_timeout_s
            )
        except BaseException as exc:
            self._outcome["exception"] = exc

    def _run_alive(self) -> bool:
        return self._runtime_thread.is_alive()

    # -- periodic bookkeeping -------------------------------------------

    def _control_loop(self) -> None:
        while True:
            self._drain_status()
            self._maybe_probe_arm()
            self._maybe_confirm()
            self._pump_activate()
            self._update_timeout()
            self._follow_through_stop()
            if not self._run_alive():
                self._drain_status()
                break
            time.sleep(0.05)

    def _drain_status(self) -> None:
        while True:
            try:
                status = self.renderer.queue.get_nowait()
            except Exception:  # queue.Empty
                break
            with self._lock:
                self._status = status

    def _maybe_confirm(self) -> None:
        if self._confirmed:
            return
        if not self.runtime.wait_until_connected(0.0):
            return
        with self._lock:
            self._connected = True
        # Defer confirmation (which auto-arms the policy) until teleop has ramped
        # the hand into the policy's grasp posture, so arming never rejects an
        # out-of-range pose.  at_home is immediately True for the synthetic policy.
        if not getattr(self.runtime.retargeter, "at_home", True):
            with self._lock:
                self._message = "Teleoperation is moving the hand to the policy grasp posture."
            return
        self.runtime.confirm_operator("web-demo-operator")
        with self._lock:
            self._confirmed = True
            self._message = (
                "Connected and confirmed. Use F12 to request RL control."
                if self.allow_switch and self._arm_available
                else "Waiting for the verified Hitbot hold controller."
                if self.allow_switch and self.require_arm_hold_controller
                else self.switch_block_reason
                or "Monitoring only. RL switching is disabled."
            )

    def _maybe_probe_arm(self, *, force: bool = False) -> bool:
        if not self.require_arm_hold_controller:
            return True
        now = time.monotonic()
        if not force and now - self._last_arm_probe < 0.5:
            return self._arm_available
        with self._lock:
            status = self._status
        if status is not None and status.state not in (
            HandoffState.TELEOP_ACTIVE.value,
            HandoffState.RL_SHADOW.value,
        ):
            return self._arm_available
        self._last_arm_probe = now
        try:
            available = bool(self.runtime.arm_gateway.probe())
        except BaseException:
            available = False
        with self._lock:
            self._arm_available = available
            if self._confirmed:
                self._message = (
                    self.switch_block_reason
                    or "Monitoring only. RL switching is disabled."
                    if not self.allow_switch
                    else "Connected and confirmed. Use F12 to request RL control."
                    if available
                    else "Waiting for the verified Hitbot hold controller."
                )
        return available

    def _update_timeout(self) -> None:
        with self._lock:
            status = self._status
            is_rl = status is not None and status.state == HandoffState.RL_ACTIVE.value
            if not is_rl:
                self._active_started = None
                self._auto_handback_sent = False
                return
            if self._active_started is None:
                self._active_started = time.monotonic()
            remaining = self.auto_handback_s - (time.monotonic() - self._active_started)
            already_sent = self._auto_handback_sent
            if remaining <= 0.0 and not already_sent:
                self._auto_handback_sent = True
        if is_rl and remaining <= 0.0 and not already_sent:
            self.switch.tap()
            with self._lock:
                self._message = "The RL time limit elapsed; hand-back was requested."

    def _pump_activate(self) -> None:
        # Switching is manual: the operator's single tap latches, and we re-tap
        # while still in RL_SHADOW until the adapter history is physically ready
        # to activate.  The shadow->active path spends >1s in ARM_HOLD/HAND_BLEND,
        # so the latch clears long before RL_ACTIVE and can never cause a hand-back.
        with self._lock:
            status = self._status
            want = self._want_activate
        if status is None:
            return
        if status.state != HandoffState.RL_SHADOW.value:
            if want:
                with self._lock:
                    self._want_activate = False
            return
        if want and time.monotonic() - self._last_activate_tap > 0.25:
            self.switch.tap()
            self._last_activate_tap = time.monotonic()

    def _follow_through_stop(self) -> None:
        with self._lock:
            pending = self._pending_stop
            status = self._status
        if pending and status is not None and status.state in (
            HandoffState.TELEOP_ACTIVE.value,
            HandoffState.RL_SHADOW.value,
        ):
            self.runtime.request_stop()

    # -- UI actions ------------------------------------------------------

    def _switchable(self, status) -> bool:
        # Switching is manual only: the button is live whenever the hand is in a
        # toggle-able state.  No 30/30-history / readiness precondition is imposed
        # here; a shadow tap latches (see _pump_activate) and lands as soon as the
        # adapter is physically ready.
        if not self.allow_switch or status is None:
            return False
        if (
            self.require_arm_hold_controller
            and status.state == HandoffState.RL_SHADOW.value
            and not self._arm_available
        ):
            return False
        return status.state in (
            HandoffState.RL_SHADOW.value,
            HandoffState.RL_ACTIVE.value,
        )

    def _switch_gate(self, status, *, pending_stop: bool, stopped: bool) -> str:
        """Return an explicit UI gate; authorization and hold health are distinct."""
        if not self.allow_switch:
            return "disabled"
        if pending_stop or stopped or status is None:
            return "state-unavailable"
        if (
            self.require_arm_hold_controller
            and status.state == HandoffState.RL_SHADOW.value
            and not self._arm_available
        ):
            return "waiting-arm-hold"
        if status.state in (
            HandoffState.RL_SHADOW.value,
            HandoffState.RL_ACTIVE.value,
        ):
            return "ready"
        return "state-unavailable"

    def do_switch(self) -> dict:
        if not self.allow_switch:
            msg = self.switch_block_reason or "Monitoring only. RL switching is disabled."
            with self._lock:
                self._message = msg
            return {"ok": False, "message": msg}
        with self._lock:
            status = self._status
            pending = self._pending_stop
        if (
            status is not None
            and status.state == HandoffState.RL_SHADOW.value
            and self.require_arm_hold_controller
        ):
            if not self._maybe_probe_arm(force=True):
                msg = "RL switch unavailable: verified Hitbot hold controller is not connected."
                with self._lock:
                    self._message = msg
                return {"ok": False, "message": msg}
        if pending or not self._run_alive():
            return {"ok": False, "message": "The runtime is stopping; switching is unavailable."}
        if not self._switchable(status):
            msg = "The current state does not accept a switch request."
            with self._lock:
                self._message = msg
            return {"ok": False, "message": msg}
        if status.state == HandoffState.RL_SHADOW.value:
            with self._lock:
                self._want_activate = True
            self._last_activate_tap = time.monotonic()
        self.switch.tap()
        msg = f"F12 request sent from {status.state}."
        with self._lock:
            self._message = msg
        return {"ok": True, "message": msg}

    def do_confirm(self) -> dict:
        """Refresh the time-bounded operator readiness evidence explicitly."""
        with self._lock:
            connected = self._connected
            pending = self._pending_stop
        if pending or not self._run_alive():
            return {"ok": False, "message": "The runtime is stopping; confirmation is unavailable."}
        if not connected:
            return {"ok": False, "message": "The hardware runtime is not connected."}
        self.runtime.confirm_operator("web-console-operator")
        with self._lock:
            self._confirmed = True
            self._message = "Operator confirmation refreshed. Readiness will update on the next control tick."
        return {"ok": True, "message": self._message}

    def do_stop(self) -> dict:
        with self._lock:
            self._pending_stop = True
            status = self._status
        if status is not None and status.state == HandoffState.RL_ACTIVE.value:
            self.switch.tap()
            msg = "RL hand-back requested; the runtime will stop after teleoperation returns."
        else:
            self.runtime.request_stop()
            msg = "Safe runtime stop requested."
        with self._lock:
            self._message = msg
        return {"ok": True, "message": msg}

    # -- snapshot for the page ------------------------------------------

    def _fingers(self) -> list[dict]:
        state = self.gateway.latest_state
        if state is None:
            return []
        joints = self.gateway.mapper.calibration.joints
        fingers = []
        for name, index in zip(FINGER_NAMES, FINGER_PLOT_JOINTS, strict=False):
            joint = joints[index]
            value = state.semantic_position[index]
            fingers.append(
                {
                    "name": name,
                    "value": round(value, 4),
                    "lower": round(joint.lower, 4),
                    "upper": round(joint.upper, 4),
                }
            )
        return fingers

    def vr_control_snapshot(self) -> dict:
        frame = getattr(self.runtime, "latest_control_telemetry", None)
        if frame is None:
            return {
                "connected": False,
                "mode": "runtime",
                "control_correlated": False,
                "nodes": [],
                "source_sequence": None,
                "sample_monotonic_ns": 0,
                "received_monotonic_ns": 0,
                "rate_hz": 0.0,
                "dropped_since_last": 0,
            }
        sample = frame.manus_sample
        candidate = frame.teleop_candidate
        payload = sample.payload
        points = getattr(payload, "points_m", ())
        nodes = []
        if len(points) == len(OPENXR_PARENT_IDS):
            for index, (point, parent) in enumerate(zip(points, OPENXR_PARENT_IDS, strict=False)):
                if len(point) != 3:
                    nodes = []
                    break
                nodes.append(
                    {
                        "id": index,
                        "parent": parent,
                        "x": float(point[0]),
                        "y": float(point[1]),
                        "z": float(point[2]),
                        "valid": bool(sample.validity_mask[index]),
                    }
                )
        source_sequence = sample.sequence
        candidate_source_sequence = candidate.source_state_sequence
        source_health = getattr(frame.manus_source_status.health, "value", "degraded")
        requested_candidate = frame.requested_candidate
        drives_current_command = (
            requested_candidate is not None
            and requested_candidate.source_state_sequence == source_sequence
            and requested_candidate.identity.source_id == candidate.identity.source_id
        )
        source_snapshot = getattr(self.runtime.manus_source, "snapshot", None)
        source_diagnostics = {}
        if source_snapshot is not None:
            try:
                source_diagnostics = source_snapshot()
            except BaseException:
                source_diagnostics = {}
        diagnostics = dict(sample.diagnostics)
        source_stale_after_ns = int(
            source_diagnostics.get(
                "stale_after_ns",
                getattr(self.runtime.manus_source, "stale_after_ns", 500_000_000),
            )
        )
        return {
            "connected": source_health == "healthy",
            "mode": source_diagnostics.get(
                "mode", diagnostics.get("bridge_mode", "runtime")
            ),
            "control_correlated": source_sequence == candidate_source_sequence,
            "drives_current_command": drives_current_command,
            "device": getattr(payload, "device", source_diagnostics.get("device")),
            "runtime": getattr(payload, "runtime", source_diagnostics.get("runtime")),
            "session_running": source_diagnostics.get("session_running", True),
            "session_focused": source_diagnostics.get("session_focused", True),
            "side": getattr(payload, "hand_side", None),
            "layout": getattr(payload, "layout_id", None),
            "joint_count": len(points),
            "valid_joint_count": sum(bool(value) for value in sample.validity_mask),
            "wrist_position_m": (
                list(points[1]) if len(points) > 1 else None
            ),
            "wrist_orientation_xyzw": (
                list(payload.orientations_xyzw[1])
                if len(getattr(payload, "orientations_xyzw", ())) > 1
                else None
            ),
            "pinch_m": getattr(payload, "pinch_m", None),
            "source_sequence": source_sequence,
            "candidate_sequence": candidate.identity.sequence,
            "candidate_source_sequence": candidate_source_sequence,
            "runtime_tick": frame.tick,
            "sample_monotonic_ns": sample.received_time_ns,
            "received_monotonic_ns": sample.received_time_ns,
            "rate_hz": source_diagnostics.get(
                "rate_hz", 1_000_000_000 / frame.control_period_ns
            ),
            "dropped_since_last": source_diagnostics.get("dropped_since_last", 0),
            # The source itself is still validated against the strict timeout.
            # The aggregate envelope represents the last *completed* 10 Hz
            # control tick, so it needs enough time for the next frame and
            # display-pump publication before declaring that immutable frame
            # stale on its own.
            "source_stale_after_ns": source_stale_after_ns,
            "stale_after_ns": source_stale_after_ns + 2 * frame.control_period_ns,
            "rejected_frames": source_diagnostics.get("rejected_frames", 0),
            "valid_mask": list(sample.validity_mask),
            "nodes": nodes,
            "source_health": source_health,
            "source_reason": frame.manus_source_status.reason,
            "coordinate_frame_id": sample.coordinate_frame_id,
            "units": sample.units,
        }

    def linker_snapshot(self) -> dict:
        frame = getattr(self.runtime, "latest_control_telemetry", None)
        state = self.gateway.latest_state if frame is None else frame.hand_state
        ownership = self.gateway.ownership
        fault = self.gateway.fault_reason
        now_ns = time.monotonic_ns()
        if state is None:
            return {
                "connected": False,
                "health": "fault" if fault else "stale",
                "fault": fault,
                "sample_monotonic_ns": 0,
                "received_monotonic_ns": 0,
                "rate_hz": self.gateway.config.gateway_hz,
                "state_sequence": None,
                "owner": None if ownership is None else ownership.owner.value,
                "control_epoch": None if ownership is None else ownership.control_epoch,
                "acknowledgement": None,
                "maximum_error_rad": None,
                "joints": [],
            }
        requested_candidate = None if frame is None else frame.requested_candidate
        authorized_command = None if frame is None else frame.authorized_command
        gateway_acknowledgement = None if frame is None else frame.gateway_acknowledgement
        effective = (
            state.last_effective_target
            if frame is None
            else frame.effective_target
        )
        requested = (
            None if requested_candidate is None else requested_candidate.semantic_position
        )
        authorized = (
            None if authorized_command is None else authorized_command.semantic_position
        )
        target = None if effective is None else effective.semantic_position
        joints = []
        maximum_error = None
        squared_errors = []
        for index, joint in enumerate(self.gateway.mapper.calibration.joints):
            measured = state.semantic_position[index]
            effective_target = None if target is None else target[index]
            error = None if effective_target is None else measured - effective_target
            if error is not None:
                maximum_error = max(abs(error), maximum_error or 0.0)
                squared_errors.append(error * error)
            joints.append(
                {
                    "index": index,
                    "name": joint.name,
                    "measured": measured,
                    "requested_target": None if requested is None else requested[index],
                    "authorized_target": None if authorized is None else authorized[index],
                    "effective_target": effective_target,
                    "error": error,
                    "lower": joint.lower,
                    "upper": joint.upper,
                    "native_slot": joint.slot,
                }
            )
        if frame is None:
            sample_time_ns = state.acquisition_time_ns
            received_time_ns = state.acquisition_time_ns
            rate_hz = self.gateway.config.gateway_hz
            stale_after_ns = self.gateway.config.state_stale_ns
            freshness_age_ns = max(0, now_ns - state.acquisition_time_ns)
            state_age_at_tick_ns = 0
        else:
            sample_time_ns = frame.actual_time_ns
            received_time_ns = frame.actual_time_ns
            rate_hz = 1_000_000_000 / frame.control_period_ns
            stale_after_ns = max(250_000_000, frame.control_period_ns * 2)
            freshness_age_ns = max(0, now_ns - frame.actual_time_ns)
            state_age_at_tick_ns = max(
                0, frame.actual_time_ns - state.acquisition_time_ns
            )
        connected = fault is None and freshness_age_ns <= stale_after_ns
        rms_error = (
            None
            if not squared_errors
            else (sum(squared_errors) / len(squared_errors)) ** 0.5
        )
        command_age_ms = (
            None
            if authorized_command is None
            else max(0, now_ns - authorized_command.authorized_time_ns) / 1_000_000
        )
        acknowledgement_age_ms = (
            None
            if gateway_acknowledgement is None
            else max(
                0,
                now_ns
                - gateway_acknowledgement.gateway.acknowledged_time_ns,
            )
            / 1_000_000
        )
        native_mapping = None
        if frame is not None:
            native_mapping = {
                "mapping_id": (
                    None if authorized_command is None else authorized_command.mapping_id
                ),
                "native_arc": list(frame.mapping_preview.native_arc),
                "saturated_joints": list(frame.mapping_preview.saturated_joints),
            }
        command_identity_match = None
        acknowledgement_missing = (
            authorized_command is not None and gateway_acknowledgement is None
        )
        if authorized_command is not None:
            command_identity_match = (
                gateway_acknowledgement is not None
                and effective is not None
                and authorized_command.command_id
                == gateway_acknowledgement.gateway.command_id
                == gateway_acknowledgement.effective_target.command_id
                == effective.command_id
            )
        epoch_match = (
            frame is None or state.identity.control_epoch == frame.control_epoch
        )
        correlation_reasons = []
        if not epoch_match:
            correlation_reasons.append("state-control-epoch-mismatch")
        if acknowledgement_missing:
            correlation_reasons.append("gateway-acknowledgement-missing")
        elif command_identity_match is False:
            correlation_reasons.append("command-identity-mismatch")
        return {
            "connected": connected,
            "health": (
                "fault"
                if fault
                else "degraded"
                if connected and correlation_reasons
                else "healthy"
                if connected
                else "stale"
            ),
            "fault": fault,
            "source_reason": ",".join(correlation_reasons),
            "sample_monotonic_ns": sample_time_ns,
            "received_monotonic_ns": received_time_ns,
            "rate_hz": rate_hz,
            "stale_after_ns": stale_after_ns,
            "gateway_rate_hz": self.gateway.config.gateway_hz,
            "measured_state_time_ns": state.acquisition_time_ns,
            "state_age_at_tick_ms": state_age_at_tick_ns / 1_000_000,
            "state_sequence": state.identity.sequence,
            "owner": (
                frame.hand_owner
                if frame is not None
                else None if ownership is None else ownership.owner.value
            ),
            "control_epoch": (
                frame.control_epoch if frame is not None else state.identity.control_epoch
            ),
            "state_epoch": state.identity.control_epoch,
            "epoch_match": epoch_match,
            "command_identity_match": command_identity_match,
            "acknowledgement_missing": acknowledgement_missing,
            "acknowledgement": (
                gateway_acknowledgement.gateway.level.name
                if gateway_acknowledgement is not None
                else None
                if frame is not None
                else state.acknowledgement_capability.name
            ),
            "runtime_tick": None if frame is None else frame.tick,
            "control_sample_sequence": (
                None if frame is None else frame.manus_sample.sequence
            ),
            "candidate_source_sequence": (
                None if frame is None else frame.teleop_candidate.source_state_sequence
            ),
            "requested_source": (
                None
                if requested_candidate is None
                else requested_candidate.identity.source_id
            ),
            "authorized_command_id": (
                None if authorized_command is None else authorized_command.command_id
            ),
            "requested_candidate_sequence": (
                None
                if requested_candidate is None
                else requested_candidate.identity.sequence
            ),
            "authorized_command_sequence": (
                None
                if authorized_command is None
                else authorized_command.identity.sequence
            ),
            "acknowledged_command_id": (
                None
                if gateway_acknowledgement is None
                else gateway_acknowledgement.gateway.command_id
            ),
            "effective_command_id": None if effective is None else effective.command_id,
            "command_age_ms": command_age_ms,
            "acknowledgement_age_ms": acknowledgement_age_ms,
            "maximum_error_rad": maximum_error,
            "rms_error_rad": rms_error,
            "watchdog_healthy": (
                None if ownership is None else ownership.watchdog_healthy
            ),
            "state_quality": state.state_quality,
            "hardware_faults": list(state.hardware_faults),
            "native_mapping": native_mapping,
            "joints": joints,
            "units": "rad",
        }

    def snapshot(self) -> dict:
        with self._lock:
            status = self._status
            connected = self._connected
            confirmed = self._confirmed
            active_started = self._active_started
            pending_stop = self._pending_stop
            message = self._message
            arm_available = self._arm_available
        exc = self._outcome.get("exception")
        stopped = not self._run_alive()
        fault = None if exc is None else f"{type(exc).__name__}: {exc}"

        if status is None:
            state = "CONNECTING"
            mode = "—"
        else:
            state = status.state
            if state == HandoffState.RL_ACTIVE.value:
                mode = "RL / CHECKPOINT INFERENCE" if not self.policy_action else "RL / SYNTHETIC BIAS"
            elif state in (
                HandoffState.RL_SHADOW.value,
                HandoffState.TELEOP_ACTIVE.value,
            ):
                mode = "TELEOP / OPENXR LEFT HAND"
            else:
                mode = "CONTROL TRANSITION"

        rl_remaining = None
        if (
            status is not None
            and status.state == HandoffState.RL_ACTIVE.value
            and active_started is not None
        ):
            rl_remaining = round(
                max(0.0, self.auto_handback_s - (time.monotonic() - active_started)), 1
            )

        if fault is not None:
            state = "FAULT"
            message = f"Runtime fault: {fault}"
        elif stopped:
            message = "The runtime stopped safely and finalized its logs."

        history = None
        if status is not None and status.history_count is not None and status.history_required is not None:
            history = f"{status.history_count}/{status.history_required}"

        policy_bias = [
            f"{index}:{value:+.2f}"
            for index, value in enumerate(self.policy_action)
            if value
        ]

        # Preserve the evidence from one completed control tick.  The browser
        # never reconstructs readiness from unrelated, differently timed
        # source snapshots.
        control_frame = getattr(self.runtime, "latest_control_telemetry", None)
        readiness_snapshot = (
            None if control_frame is None else control_frame.readiness
        )
        readiness_providers = []
        readiness_blocking_reasons = []
        readiness_ready = None if status is None else status.readiness_ready
        if readiness_snapshot is not None:
            evaluated_time_ns = readiness_snapshot.evaluated_time_ns
            readiness_ready = readiness_snapshot.ready
            readiness_blocking_reasons = list(
                readiness_snapshot.blocking_reasons
            )
            for evidence in readiness_snapshot.evidence:
                readiness_providers.append(
                    {
                        "provider_id": evidence.provider_id,
                        "result": evidence.result.value,
                        "valid": evidence.valid_at(evaluated_time_ns),
                        "reason_codes": list(evidence.reason_codes),
                        "generated_time_ns": evidence.generated_time_ns,
                        "valid_until_ns": evidence.valid_until_ns,
                    }
                )

        switch_gate = self._switch_gate(
            status,
            pending_stop=pending_stop,
            stopped=stopped,
        )
        return {
            "state": state,
            "session_id": self.gateway.config.control_session_id,
            "color": _STATE_COLORS.get(state, "#a16207" if status is not None else "#52606d"),
            "mode": mode,
            "hand_owner": None if status is None else status.hand_owner,
            "control_epoch": None if status is None else status.control_epoch,
            "history": history,
            "readiness_ready": readiness_ready,
            "readiness_providers": readiness_providers,
            "readiness_blocking_reasons": readiness_blocking_reasons,
            "rejection_reason": None if status is None else status.rejection_reason,
            "blend_alpha": None if status is None else status.blend_alpha,
            "rl_timeout_remaining": rl_remaining,
            "switchable": self._switchable(status) and not pending_stop and not stopped,
            "switch_gate": switch_gate,
            "arm_hold_ready": (
                arm_available if self.require_arm_hold_controller else None
            ),
            "switch_block_reason": (
                "RL switch unavailable: verified Hitbot hold controller is not connected."
                if switch_gate == "waiting-arm-hold"
                else self.switch_block_reason
            ),
            "connected": connected,
            "confirmed": confirmed,
            "stopped": stopped,
            "fault": fault,
            "message": message,
            "policy_name": None if status is None else status.policy_name,
            "policy_bias": policy_bias,
            "logs_path": str(self.work),
            "fingers": self._fingers(),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("hand", "fake"), default="hand")
    parser.add_argument(
        "--policy",
        choices=("real", "synthetic"),
        default="real",
        help="real dex-forge checkpoint (default) or the synthetic MCP/thumb bias",
    )
    parser.add_argument("--deploy", default=None, help="override the deploy.pth for --policy real")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--vr-udp-port",
        type=int,
        default=8770,
        help="UDP port to receive OpenXR hand frames from openxr_hand_bridge.py",
    )
    parser.add_argument(
        "--vr-arm-udp-port",
        type=int,
        default=8771,
        help="UDP fanout port consumed by the single Hitbot owner",
    )
    parser.add_argument(
        "--vr",
        choices=("auto", "real", "fake", "off"),
        default="auto",
        help=(
            "OpenXR control input: real/fake feed the production retargeter; "
            "auto selects real when dex_teleop and the VR Python are present"
        ),
    )
    parser.add_argument("--vr-python", default=_DEFAULT_VR_PYTHON)
    parser.add_argument("--teleop-root", default=_DEFAULT_TELEOP_ROOT)
    parser.add_argument(
        "--arm-telemetry",
        choices=("live", "fake", "off"),
        default="live",
        help=(
            "live listens for read-only dex_teleop Hitbot cycles; fake is a "
            "labelled synthetic source; off shows stale"
        ),
    )
    parser.add_argument(
        "--arm-udp-port",
        type=int,
        default=8780,
        help="localhost UDP port for dex_teleop Hitbot cycle telemetry",
    )
    parser.add_argument(
        "--arm-hold-port",
        type=int,
        default=8781,
        help="localhost UDP port for the verified dex_teleop Hitbot hold controller",
    )
    parser.add_argument(
        "--enable-real-arm-hold-switch",
        action="store_true",
        help="explicitly release live RL switching after the real-arm HIL gate passes",
    )
    parser.add_argument(
        "--camera",
        choices=("d435", "fake", "off"),
        default="off",
        help="D435 RGB/depth source; fake is a labelled offline preview",
    )
    parser.add_argument(
        "--camera-serial",
        default=None,
        help="optional RealSense serial number when more than one camera is attached",
    )
    parser.add_argument(
        "--camera-python",
        default=_DEFAULT_CAMERA_PYTHON,
        help=(
            "Python executable that owns the isolated D435 SDK worker; defaults "
            "to DEX_CAMERA_PYTHON or the host base Python when available"
        ),
    )
    parser.add_argument("--teleop-amplitude-rad", type=float, default=0.08)
    parser.add_argument("--policy-action", type=float, default=0.12)
    parser.add_argument("--auto-handback-seconds", type=float, default=6.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate arguments/imports without opening CAN or a server",
    )
    args = parser.parse_args()
    if not 0.01 <= args.teleop_amplitude_rad <= 0.12:
        parser.error("--teleop-amplitude-rad must be within 0.01..0.12")
    if not 0.02 <= args.policy_action <= 0.20:
        parser.error("--policy-action must be within 0.02..0.20")
    if not 2.0 <= args.auto_handback_seconds <= 10.0:
        parser.error("--auto-handback-seconds must be within 2..10")
    if not 1 <= args.arm_hold_port <= 65535:
        parser.error("--arm-hold-port must be within 1..65535")
    if not 1 <= args.vr_udp_port <= 65535 or not 1 <= args.vr_arm_udp_port <= 65535:
        parser.error("OpenXR UDP ports must be within 1..65535")
    if args.vr != "off" and not os.access(args.vr_python, os.X_OK):
        parser.error(f"--vr-python is not executable: {args.vr_python}")
    if args.camera == "d435" and not os.access(args.camera_python, os.X_OK):
        parser.error(f"--camera-python is not executable: {args.camera_python}")
    return args


def _resolved_vr_mode(args: argparse.Namespace) -> str:
    if args.vr != "auto":
        return args.vr
    return (
        "real"
        if Path(args.teleop_root, "main_new.py").is_file()
        and os.access(args.vr_python, os.X_OK)
        else "off"
    )


def main() -> int:
    args = parse_args()
    vr_mode = _resolved_vr_mode(args)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "transport": args.transport,
                    "policy": args.policy,
                    "deploy": args.deploy,
                    "vr": args.vr,
                    "resolved_vr": vr_mode,
                    "vr_udp_port": args.vr_udp_port,
                    "vr_arm_udp_port": args.vr_arm_udp_port,
                    "vr_python": args.vr_python,
                    "teleop_root": args.teleop_root,
                    "arm_telemetry": args.arm_telemetry,
                    "arm_udp_port": args.arm_udp_port,
                    "arm_hold_port": args.arm_hold_port,
                    "enable_real_arm_hold_switch": args.enable_real_arm_hold_switch,
                    "camera": args.camera,
                    "camera_serial": args.camera_serial,
                    "camera_python": args.camera_python,
                    "host": args.host,
                    "port": args.port,
                    "teleop_amplitude_rad": args.teleop_amplitude_rad,
                    "policy_action": args.policy_action,
                    "policy_delta_rad_per_tick": args.policy_action * 0.05,
                    "auto_handback_seconds": args.auto_handback_seconds,
                },
                sort_keys=True,
            )
        )
        return 0

    lock = None
    if args.transport == "hand":
        lock_path = Path("/tmp/dex-switch-web-demo.lock")
        lock = lock_path.open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another switch web demo is already running") from exc

    vr = None
    if vr_mode != "off":
        vr = UdpOpenXRSource(
            "127.0.0.1",
            args.vr_udp_port,
            source_id="openxr-left",
            hand_side="left",
            stale_after_ns=100_000_000,
            warmup_samples=30 if vr_mode == "real" else 1,
        )
    try:
        work, action, runtime, gateway, switch, renderer = build_runtime(
            args.transport,
            args.teleop_amplitude_rad,
            args.policy_action,
            policy_kind=args.policy,
            deploy_pth=args.deploy,
            teleop_source=vr,
            arm_hold_kind="hitbot" if args.arm_telemetry == "live" else "fake",
            arm_hold_port=args.arm_hold_port,
        )
    except BaseException:
        if vr is not None:
            vr.stop()
        raise
    controller = DemoController(
        work=work,
        policy_action=action,
        runtime=runtime,
        gateway=gateway,
        switch=switch,
        renderer=renderer,
        auto_handback_s=args.auto_handback_seconds,
        allow_switch=(
            args.arm_telemetry != "live" or args.enable_real_arm_hold_switch
        ),
        switch_block_reason=(
            "RL switch disabled until explicit real-arm HIL authorization is enabled."
            if args.arm_telemetry == "live" and not args.enable_real_arm_hold_switch
            else None
        ),
        require_arm_hold_controller=args.arm_telemetry == "live",
        # A cold WiVRn/OpenXR session may need time for the headset to become
        # focused. Fake/off modes retain the short feedback loop used by tests.
        initial_input_timeout_s=120.0 if vr_mode == "real" else 5.0,
    )
    hub = TelemetryHub()
    camera = None
    try:
        if args.arm_telemetry == "fake":
            arm = SyntheticArmTelemetry()
        elif args.arm_telemetry == "live":
            arm = ArmTelemetryListener("127.0.0.1", args.arm_udp_port)
            arm.start()
        else:
            arm = None
        if args.camera == "fake":
            camera = SyntheticD435Source()
            camera.start()
        elif args.camera == "d435":
            camera = RealSenseD435Source(
                serial=args.camera_serial,
                camera_python=args.camera_python,
            )
            camera.start()
    except BaseException:
        if vr is not None:
            vr.stop()
        raise
    vr_proc = None
    if vr is not None:
        try:
            vr_proc = start_vr_bridge(
                vr_mode,
                host="127.0.0.1",
                hand_port=args.vr_udp_port,
                arm_port=args.vr_arm_udp_port,
                vr_python=args.vr_python,
                teleop_root=args.teleop_root,
                log_path=work / "openxr_bridge.log",
            )
        except BaseException:
            if isinstance(arm, ArmTelemetryListener):
                arm.stop()
            if camera is not None:
                camera.stop()
            vr.stop()
            raise
    controller.start()

    telemetry_pump = None
    server = None
    url = f"http://{args.host}:{args.port}/"
    try:
        server = make_console_server(
            args.host,
            args.port,
            controller=controller,
            hub=hub,
            camera=camera,
        )
        telemetry_pump = ConsoleTelemetryPump(
            hub,
            controller=controller,
            vr=vr,
            arm=arm,
            camera=camera,
            display_hz=20.0,
        )
        telemetry_pump.start()
        print(f"[switch-web-demo] transport={args.transport}  ->  {url}")
        print(f"[switch-web-demo] OpenXR control input={vr_mode}")
        print(f"[switch-web-demo] arm telemetry={args.arm_telemetry}")
        print(f"[switch-web-demo] camera={args.camera}")
        print(f"[switch-web-demo] logs: {work}")
        print("[switch-web-demo] Ctrl-C to stop")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[switch-web-demo] stopping...")
    finally:
        if server is not None:
            server.console_stop.set()
        if telemetry_pump is not None:
            telemetry_pump.stop()
        controller.shutdown()
        if vr_proc is not None:
            vr_proc.terminate()
            try:
                vr_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                vr_proc.kill()
        if vr is not None:
            vr.stop()
        if isinstance(arm, ArmTelemetryListener):
            arm.stop()
        if camera is not None:
            try:
                camera.stop()
            except TimeoutError as exc:
                print(f"[switch-web-demo] camera shutdown warning: {exc}")
        if server is not None:
            server.server_close()
    exc = controller._outcome.get("exception")
    return 1 if exc is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
