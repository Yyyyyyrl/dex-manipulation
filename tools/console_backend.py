#!/usr/bin/env python3
"""Shared backend for the live control console (hand or fake transport).

`run_console.py` is the entry point; this module is what it builds the runtime
with, on the live path as well as in fake mode.

It carries the hardware-capable pieces the console needs: a bounded
virtual OpenXR wave source, a visible four-finger retargeter, a synthetic biased
RL policy, the real Linker SDK transport (plus a raw can0 initial read), and a
programmatic operator switch.  It imports no GUI toolkit and imports ``can``
lazily, so the ``fake`` transport path runs anywhere with only the core deps.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

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
    FakeLinkerTransport,
    GatewayConfig,
    LinkerGateway,
    LinkerMapper,
    LinkerSdkTransport,
)
from dex_runtime.application import HandOnlyRuntime  # noqa: E402
from dex_runtime.operator_switch import OperatorSwitchEvent, SwitchEdge  # noqa: E402
from dex_runtime.preflight import preflight_deployment  # noqa: E402
from dex_runtime.status import RuntimeStatus  # noqa: E402
from dex_teleop_adapters import (  # noqa: E402
    OpenXRSourceStatus,
    build_openxr_dexpilot_retargeter,
)
from synthetic_policy import (  # noqa: E402
    CALIBRATION_DIGEST,
    CALIBRATION_ID,
    CALIBRATION_LOWER,
    CALIBRATION_UPPER,
    rewrite_manifest,
    write_synthetic_package,
)

CAN_ID = 0x28
CONTROL_PERIOD_NS = 100_000_000
TELEOP_FREQUENCY_HZ = 0.18
TELEOP_JOINTS = (2, 5, 8, 11)
POLICY_JOINTS = (1, 4, 7, 10, 15)
FINGER_PLOT_JOINTS = (15, 2, 5, 8, 11)
FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def read_current_native(channel: str = "can0") -> tuple[int, ...]:
    """Read G20 position slots over raw CAN without importing or opening the SDK."""

    import can  # lazy: only the hand transport needs python-can

    def _query_frame(bus, command: int) -> list[int]:
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


def write_biased_policy(directory: Path, action: tuple[float, ...]) -> Path:
    package = write_synthetic_package(directory)
    actor_path = package / "actor.safetensors"
    actor_state = load_file(str(actor_path))
    actor_state = {name: torch.zeros_like(value) for name, value in actor_state.items()}
    actor_state["mu.bias"] = torch.tensor(action, dtype=actor_state["mu.bias"].dtype)
    save_file(actor_state, str(actor_path))
    manifest = json.loads((package / "manifest.json").read_text())
    manifest["display_name"] = "Demo bounded visible switch policy"
    manifest["weights"]["actor"]["sha256"] = hashlib.sha256(actor_path.read_bytes()).hexdigest()
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


class VirtualOpenXRSource:
    """Bounded synthetic source used only when no external OpenXR stream exists."""

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
            target=publish, name="virtual-openxr-visible-wave", daemon=True
        )
        self._thread.start()

    def status(self, _now_ns: int) -> OpenXRSourceStatus:
        return OpenXRSourceStatus(
            "virtual-openxr-visible-wave",
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
    """Turn each virtual sample into a bounded four-finger PIP wave candidate."""

    RAMP_STEP_RAD = 0.03  # per-tick base move toward the policy grasp posture
    WAVE_RAMP_IN_S = 2.0  # grow wave amplitude from 0 to avoid a tick-1 delta jump

    def __init__(
        self,
        gateway: LinkerGateway,
        preflight,
        amplitude_rad: float,
        home_posture: tuple[float, ...] | None = None,
    ) -> None:
        self.gateway = gateway
        self.mapper = preflight.mapper
        self.hand_model = self.mapper.calibration.hand_model
        self.hand_side = self.mapper.calibration.hand_side
        self.semantic_schema_id = self.mapper.calibration.semantic_schema_id
        self.amplitude_rad = amplitude_rad
        # When a real task policy is loaded, teleop first ramps the hand into the
        # policy's grasp posture (its action-bound midpoint) and waves there, so
        # the policy can be armed/activated in-range.  None -> wave in place.
        self.home_posture = home_posture
        self.sequence = 0
        self.base: tuple[float, ...] | None = None
        self.started_ns: int | None = None

    @property
    def at_home(self) -> bool:
        if self.home_posture is None:
            return True
        if self.base is None:
            return False
        return all(abs(b - h) <= 1e-3 for b, h in zip(self.base, self.home_posture, strict=False))

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
        if self.home_posture is not None:
            # Ramp the wave base toward the policy grasp posture, bounded per tick
            # so the hand tracks without tripping delta/following-error limits.
            self.base = tuple(
                current + max(-self.RAMP_STEP_RAD, min(self.RAMP_STEP_RAD, home - current))
                for current, home in zip(self.base, self.home_posture, strict=False)
            )
        elapsed_s = (sample.received_time_ns - self.started_ns) / 1_000_000_000
        phase = 2.0 * math.pi * TELEOP_FREQUENCY_HZ * elapsed_s
        # Grow the wave amplitude from zero so the first ticks don't jump the
        # command by the full amplitude (which would trip the per-tick delta limit).
        amplitude = self.amplitude_rad * min(1.0, elapsed_s / self.WAVE_RAMP_IN_S)
        target = list(self.base)
        for offset, index in enumerate(TELEOP_JOINTS):
            joint = self.mapper.calibration.joints[index]
            value = self.base[index] + amplitude * math.sin(phase + offset * math.pi / 2.0)
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


class BoundedTeleopRetargeter:
    """Ramp validated OpenXR DexPilot targets from the measured hand posture.

    The standard runtime composition remains unchanged.  The console already
    uses a bounded posture ramp for synthetic teleoperation; this wrapper gives
    real/fake OpenXR input the same first-tick behavior while preserving sample
    and candidate identity for telemetry correlation.
    """

    RAMP_STEP_RAD = 0.03

    def __init__(self, retargeter, gateway: LinkerGateway, preflight) -> None:
        self.retargeter = retargeter
        self.gateway = gateway
        self.lower = tuple(preflight.binding.safety.position_lower_rad)
        self.upper = tuple(preflight.binding.safety.position_upper_rad)
        self._last_target: tuple[float, ...] | None = None

    @property
    def at_home(self) -> bool:
        return True

    def reset(self) -> None:
        self.retargeter.reset()
        self._last_target = None

    def status(self):
        return self.retargeter.status()

    def prepare(self, sample: TimestampedSample, **kwargs) -> None:
        """Warm solver caches before the control scheduler and issue no command."""

        self.retargeter.retarget(sample, **kwargs)
        self.reset()

    def retarget(self, sample: TimestampedSample, **kwargs) -> TeleopHandCandidate:
        raw = self.retargeter.retarget(sample, **kwargs)
        if self._last_target is None:
            state = self.gateway.latest_state
            if state is None:
                raise RuntimeError("current hand state unavailable for OpenXR ramp")
            self._last_target = tuple(state.semantic_position)
        bounded = tuple(
            min(max(value, lower), upper)
            for value, lower, upper in zip(
                raw.semantic_position, self.lower, self.upper, strict=False
            )
        )
        ramped = tuple(
            current + max(-self.RAMP_STEP_RAD, min(self.RAMP_STEP_RAD, desired - current))
            for current, desired in zip(self._last_target, bounded, strict=False)
        )
        self._last_target = ramped
        clipped = sum(
            value != bounded_value
            for value, bounded_value in zip(raw.semantic_position, bounded, strict=False)
        )
        return replace(
            raw,
            semantic_position=ramped,
            diagnostics=raw.diagnostics
            + (
                ("demo_ramp_step_rad", self.RAMP_STEP_RAD),
                ("demo_clipped_joint_count", clipped),
            ),
        )


class WebSwitch:
    """Programmatic F12 switch source; a UI button taps it over HTTP."""

    status = "healthy-web-switch"

    def __init__(self) -> None:
        self.callback = None
        self.sequence = 0

    def start(self, callback) -> None:
        self.callback = callback

    def tap(self) -> None:
        if self.callback is None:
            raise RuntimeError("web switch source is not started")
        now_ns = time.monotonic_ns()
        for edge in (SwitchEdge.PRESS, SwitchEdge.RELEASE):
            self.callback(
                OperatorSwitchEvent(
                    source_id="web-switch",
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
    """Single-slot latest-wins RuntimeStatus sink read by the web layer."""

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


def _base_config(work: Path, package: Path) -> dict:
    """The shared deployment config every console transport mode starts from."""

    package_id = json.loads((package / "manifest.json").read_text())["package_id"]
    return {
        "format_version": 1,
        "binding_id": "linker-g20-left-switch-demo-v1",
        "protocol_version": "1.0",
        "control_session_id": f"console-{time.monotonic_ns()}",
        "hand": {
            "model": "LinkerHand G20",
            "side": "left",
            "serial_number": "LHT20-010-415-L-B-1-D",
            "hand_joint": "G20",
        },
        "calibration": {
            "artifact_path": str(
                ROOT
                / "src/dex_hardware_linker/assets/calibrations/linker_g20_left_lht20_010_415_v1.json"
            ),
            "schema_path": str(
                ROOT
                / "src/dex_hardware_linker/assets/calibrations/linker_g20_left_semantic_schema_v1.json"
            ),
            "calibration_id": CALIBRATION_ID,
            "artifact_digest": CALIBRATION_DIGEST,
        },
        "teleop": {
            "repository_root": str(ROOT),
            "profile_path": str(ROOT / "configs/teleop/linker_g20_left_openxr_dexpilot_v1.json"),
            "retargeting_model_directory": str(ROOT / "src/dex_hardware_linker/assets/model"),
            "manus": {
                "source_id": "openxr-left",
                "topic": "openxr_left_hand",
                "stale_after_ns": 100_000_000,
                "candidate_ttl_ns": 100_000_000,
            },
        },
        "policies": {
            "stores": [str(package.parent)],
            "selected_package_id": package_id,
            "allow_unsigned_local": True,
        },
        "safety": {
            "position_lower_rad": list(CALIBRATION_LOWER),
            "position_upper_rad": list(CALIBRATION_UPPER),
            "maximum_delta_per_tick_rad": 0.1,
            "maximum_target_rate_rad_s": 1.0,
            "maximum_following_error_rad": 0.5,
            "maximum_state_age_ns": 100_000_000,
            # The real Linker SDK performs a state read before draining the
            # command queue. Its CAN round trip can legitimately exceed 50 ms,
            # so keep a command valid for exactly one 10 Hz control period.
            # It still expires before a later control tick can supersede it.
            "command_deadline_ns": CONTROL_PERIOD_NS,
        },
        "readiness": {
            "required_provider_ids": [
                "operator-confirmation-v1",
                "hand-state-freshness-v1",
                "gateway-health-v1",
                "policy-compatibility-v1",
            ],
            "evidence_validity_ns": 200_000_000,
            "operator_confirmation_validity_ns": 600_000_000_000,
        },
        "handoff": {
            "ownership_lease_ns": 60_000_000_000,
            "gateway_ack_timeout_s": 0.5,
            "teleop_command_period_ns": CONTROL_PERIOD_NS,
            "policy_blend_ticks": 10,
            # The widest G20 range is 1.57 rad. At the configured 0.1 rad/tick
            # limit, 20 ticks leaves margin for a full-range simulated return.
            "handback_blend_ticks": 20,
        },
        "switch": {
            "kind": "evdev-f12",
            "device_path": "/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd",
            "source_id": "footswitch",
            "key": "F12",
            "debounce_ns": 50_000_000,
            "require_pcsensor_identity": True,
        },
        "logging": {
            "events_path": str(work / "events.jsonl"),
            "trace_path": str(work / "trace.jsonl"),
            "trace_minimum_period_ns": 50_000_000,
        },
        "status": {"period_ns": 100_000_000, "use_ansi": False},
        "arm": {"mode": "fake-hold"},
    }


def _write_config(
    work: Path,
    package: Path,
    transport_kind: str,
    *,
    arm_hold_kind: str = "fake",
    arm_hold_port: int = 8781,
) -> Path:
    config = _base_config(work, package)
    if transport_kind == "hand":
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
    else:
        config["gateway"] = {
            "transport": "fake",
            "gateway_id": "linker-g20-left",
            "gateway_hz": 50.0,
            "state_stale_ns": 100_000_000,
            "command_watchdog_ns": 1_000_000_000,
            "maximum_round_trip_error_rad": 0.01,
            "linker_sdk": None,
        }
    if arm_hold_kind == "hitbot":
        config["arm"] = {
            "mode": "hitbot-hold-v1",
            "control_host": "127.0.0.1",
            "control_port": arm_hold_port,
            "request_timeout_s": 0.35,
            "command_ttl_ns": 500_000_000,
            "hold_lease_ns": 1_000_000_000,
        }
    elif arm_hold_kind != "fake":
        raise ValueError("arm_hold_kind must be 'fake' or 'hitbot'")
    path = work / "deployment.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def build_runtime(
    transport_kind: str,
    amplitude_rad: float,
    policy_action_magnitude: float,
    *,
    policy_kind: str = "synthetic",
    deploy_pth=None,
    teleop_source=None,
    arm_hold_kind: str = "fake",
    arm_hold_port: int = 8781,
):
    """Build a hand-only runtime for ``transport_kind`` in {"hand", "fake"}.

    ``policy_kind`` selects the RL policy driving the RL_ACTIVE state:
    ``"synthetic"`` writes a bounded MCP/thumb bias actor (deterministic, visible);
    ``"real"`` repackages a dex-forge ``deploy.pth`` checkpoint into a G20-bound
    runtime package whose real trained actor/adapter run genuine inference.
    """

    if transport_kind not in ("hand", "fake"):
        raise ValueError("transport_kind must be 'hand' or 'fake'")
    if policy_kind not in ("synthetic", "real"):
        raise ValueError("policy_kind must be 'synthetic' or 'real'")
    work = Path(tempfile.mkdtemp(prefix="dex-console-", dir="/tmp"))
    mapper = LinkerMapper.load()

    if transport_kind == "hand":
        initial_native = read_current_native()
    else:
        midpoint = tuple(
            (lower + upper) * 0.5
            for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER, strict=False)
        )
        initial_native = mapper.prepare(midpoint).native_range
    current_semantic = mapper.inverse(initial_native)

    if policy_kind == "real":
        from repackage_stage2_policy import repackage_g20_policy

        kwargs = {} if deploy_pth is None else {"deploy_pth": Path(deploy_pth)}
        package, _package_id = repackage_g20_policy(work / "store" / "real-policy", **kwargs)
        policy_action = ()
        # The real task policy runs in a tighter posture range than teleop.  Rather
        # than pre-posing the hand, teleop ramps it into the policy's grasp posture
        # (its action-bound midpoint); arming is deferred until it arrives.  This
        # holds for both the real hand and fake mode, so fake rehearses the hand.
        action_transform = json.loads((package / "manifest.json").read_text())["action_transform"]
        policy_home = tuple(
            (lower + upper) * 0.5
            for lower, upper in zip(
                action_transform["position_lower_rad"],
                action_transform["position_upper_rad"],
                strict=False,
            )
        )
    else:
        policy_home = None
        policy_action = choose_policy_action(current_semantic, mapper, policy_action_magnitude)
        package = write_biased_policy(work / "store" / "visible-policy", policy_action)

    config_path = _write_config(
        work,
        package,
        transport_kind,
        arm_hold_kind=arm_hold_kind,
        arm_hold_port=arm_hold_port,
    )
    preflight = preflight_deployment(str(config_path))
    binding = preflight.binding

    if transport_kind == "hand":
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
    else:
        transport = FakeLinkerTransport(initial_native)

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
    switch = WebSwitch()
    renderer = QueueStatusRenderer()
    if teleop_source is None:
        runtime_teleop_source = VirtualOpenXRSource()
        runtime_retargeter = VisibleWaveRetargeter(
            gateway,
            preflight,
            amplitude_rad,
            home_posture=policy_home,
        )
    else:
        runtime_teleop_source = teleop_source
        dexpilot_retargeter = build_openxr_dexpilot_retargeter(
            preflight.teleop_profile,
            model_directory=binding.teleop.retargeting_model_directory,
            # OpenXR hand tracking can briefly occlude between camera frames.
            # Keep the last validated target for at most 500 ms; longer loss
            # still expires and follows the existing fail-closed path.
            candidate_ttl_ns=max(
                binding.teleop.manus.candidate_ttl_ns,
                500_000_000,
            ),
            source_id="openxr-udp-retargeter",
        )
        runtime_retargeter = BoundedTeleopRetargeter(
            dexpilot_retargeter,
            gateway,
            preflight,
        )

    runtime = HandOnlyRuntime(
        preflight,
        gateway,
        runtime_teleop_source,
        runtime_retargeter,
        switch,
        status_renderer=renderer,
    )
    return work, policy_action, runtime, gateway, switch, renderer
