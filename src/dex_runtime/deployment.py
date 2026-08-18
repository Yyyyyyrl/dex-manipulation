"""Strict, immutable hand-only deployment binding loaded before hardware access."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class DeploymentBindingError(ValueError):
    pass


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DeploymentBindingError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        raise DeploymentBindingError(f"invalid {label} fields; missing={missing}, extra={extra}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentBindingError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DeploymentBindingError(f"{label} must be a positive integer")
    return value


def _udp_port(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise DeploymentBindingError(f"{label} must be an integer within 1..65535")
    return value


def _positive_float(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise DeploymentBindingError(f"{label} must be a positive finite number")
    return float(value)


def _resolve(base: Path, value: object, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


@dataclass(frozen=True)
class HandBinding:
    model: str
    side: str
    serial_number: str
    hand_joint: str


@dataclass(frozen=True)
class CalibrationBinding:
    artifact_path: Path
    schema_path: Path
    calibration_id: str
    artifact_digest: str


@dataclass(frozen=True)
class ManusBinding:
    source_id: str
    topic: str
    stale_after_ns: int
    candidate_ttl_ns: int


@dataclass(frozen=True)
class OpenXRBinding:
    """Loopback UDP bridge carrying Quest/WiVRn ``XR_EXT_hand_tracking`` frames.

    The OpenXR runtime is owned by a separate bridge process; the runtime only
    ever consumes its loopback fanout, so this binding names a socket, not a
    device.
    """

    source_id: str
    host: str
    port: int
    stale_after_ns: int
    candidate_ttl_ns: int


@dataclass(frozen=True)
class TeleopBinding:
    """Exactly one of `manus` / `openxr` is set; the parser enforces it."""

    repository_root: Path
    profile_path: Path
    retargeting_model_directory: Path
    manus: ManusBinding | None
    openxr: OpenXRBinding | None


@dataclass(frozen=True)
class LinkerSdkBinding:
    sdk_root: Path
    can_channel: str
    speed: tuple[int, ...]
    torque: tuple[int, ...]


@dataclass(frozen=True)
class GatewayRuntimeBinding:
    transport: str
    gateway_id: str
    gateway_hz: float
    state_stale_ns: int
    command_watchdog_ns: int
    maximum_round_trip_error_rad: float
    linker_sdk: LinkerSdkBinding | None


@dataclass(frozen=True)
class PolicyStoreBinding:
    stores: tuple[Path, ...]
    selected_package_id: str
    allow_unsigned_local: bool


@dataclass(frozen=True)
class SafetyRuntimeBinding:
    position_lower_rad: tuple[float, ...]
    position_upper_rad: tuple[float, ...]
    maximum_delta_per_tick_rad: float
    maximum_target_rate_rad_s: float
    maximum_following_error_rad: float
    maximum_state_age_ns: int
    command_deadline_ns: int


@dataclass(frozen=True)
class ReadinessRuntimeBinding:
    required_provider_ids: tuple[str, ...]
    evidence_validity_ns: int
    operator_confirmation_validity_ns: int


@dataclass(frozen=True)
class HandoffRuntimeBinding:
    ownership_lease_ns: int
    gateway_ack_timeout_s: float
    teleop_command_period_ns: int
    policy_blend_ticks: int
    handback_blend_ticks: int


@dataclass(frozen=True)
class SwitchBinding:
    kind: str
    device_path: str
    source_id: str
    key: str
    debounce_ns: int
    require_pcsensor_identity: bool


@dataclass(frozen=True)
class LoggingBinding:
    events_path: Path
    trace_path: Path
    trace_minimum_period_ns: int


@dataclass(frozen=True)
class StatusBinding:
    period_ns: int
    use_ansi: bool


@dataclass(frozen=True)
class ArmRuntimeBinding:
    mode: str
    control_host: str | None
    control_port: int | None
    request_timeout_s: float | None
    command_ttl_ns: int | None
    hold_lease_ns: int | None


@dataclass(frozen=True)
class DeploymentBinding:
    source_path: Path
    format_version: int
    binding_id: str
    protocol_version: str
    control_session_id: str
    hand: HandBinding
    calibration: CalibrationBinding
    teleop: TeleopBinding
    gateway: GatewayRuntimeBinding
    policies: PolicyStoreBinding
    safety: SafetyRuntimeBinding
    readiness: ReadinessRuntimeBinding
    handoff: HandoffRuntimeBinding
    switch: SwitchBinding
    logging: LoggingBinding
    status: StatusBinding
    arm: ArmRuntimeBinding

    @property
    def arm_mode(self) -> str:
        return self.arm.mode

    @classmethod
    def load(cls, path: str | Path) -> DeploymentBinding:
        source = Path(path).resolve()
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise DeploymentBindingError("deployment binding root must be an object")
        _exact(
            raw,
            {
                "format_version",
                "binding_id",
                "protocol_version",
                "control_session_id",
                "hand",
                "calibration",
                "teleop",
                "gateway",
                "policies",
                "safety",
                "readiness",
                "handoff",
                "switch",
                "logging",
                "status",
                "arm",
            },
            "deployment binding",
        )
        if raw["format_version"] != 1 or raw["protocol_version"] != "1.0":
            raise DeploymentBindingError("unsupported deployment binding or protocol version")
        base = source.parent

        hand_raw = _object(raw["hand"], "hand")
        _exact(hand_raw, {"model", "side", "serial_number", "hand_joint"}, "hand")
        hand = HandBinding(
            _text(hand_raw["model"], "hand.model"),
            _text(hand_raw["side"], "hand.side"),
            _text(hand_raw["serial_number"], "hand.serial_number"),
            _text(hand_raw["hand_joint"], "hand.hand_joint"),
        )
        if hand.model != "LinkerHand G20" or hand.side != "left" or hand.hand_joint != "G20":
            raise DeploymentBindingError("initial verified binding is LinkerHand G20 left only")

        calibration_raw = _object(raw["calibration"], "calibration")
        _exact(
            calibration_raw,
            {"artifact_path", "schema_path", "calibration_id", "artifact_digest"},
            "calibration",
        )
        calibration = CalibrationBinding(
            _resolve(base, calibration_raw["artifact_path"], "calibration.artifact_path"),
            _resolve(base, calibration_raw["schema_path"], "calibration.schema_path"),
            _text(calibration_raw["calibration_id"], "calibration.calibration_id"),
            _text(calibration_raw["artifact_digest"], "calibration.artifact_digest"),
        )

        teleop_raw = _object(raw["teleop"], "teleop")
        declared = {"manus", "openxr"} & set(teleop_raw)
        if len(declared) != 1:
            raise DeploymentBindingError(
                "teleop must declare exactly one operator device, either manus or "
                f"openxr; got {sorted(declared)}"
            )
        _exact(
            teleop_raw,
            {"repository_root", "profile_path", "retargeting_model_directory"} | declared,
            "teleop",
        )
        manus: ManusBinding | None = None
        openxr: OpenXRBinding | None = None
        if "manus" in declared:
            manus_raw = _object(teleop_raw["manus"], "teleop.manus")
            _exact(
                manus_raw,
                {"source_id", "topic", "stale_after_ns", "candidate_ttl_ns"},
                "teleop.manus",
            )
            manus = ManusBinding(
                _text(manus_raw["source_id"], "teleop.manus.source_id"),
                _text(manus_raw["topic"], "teleop.manus.topic"),
                _positive_int(manus_raw["stale_after_ns"], "teleop.manus.stale_after_ns"),
                _positive_int(manus_raw["candidate_ttl_ns"], "teleop.manus.candidate_ttl_ns"),
            )
        else:
            openxr_raw = _object(teleop_raw["openxr"], "teleop.openxr")
            _exact(
                openxr_raw,
                {"source_id", "host", "port", "stale_after_ns", "candidate_ttl_ns"},
                "teleop.openxr",
            )
            openxr = OpenXRBinding(
                _text(openxr_raw["source_id"], "teleop.openxr.source_id"),
                _text(openxr_raw["host"], "teleop.openxr.host"),
                _udp_port(openxr_raw["port"], "teleop.openxr.port"),
                _positive_int(openxr_raw["stale_after_ns"], "teleop.openxr.stale_after_ns"),
                _positive_int(openxr_raw["candidate_ttl_ns"], "teleop.openxr.candidate_ttl_ns"),
            )
        teleop = TeleopBinding(
            _resolve(base, teleop_raw["repository_root"], "teleop.repository_root"),
            _resolve(base, teleop_raw["profile_path"], "teleop.profile_path"),
            _resolve(
                base,
                teleop_raw["retargeting_model_directory"],
                "teleop.retargeting_model_directory",
            ),
            manus,
            openxr,
        )

        gateway_raw = _object(raw["gateway"], "gateway")
        _exact(
            gateway_raw,
            {
                "transport",
                "gateway_id",
                "gateway_hz",
                "state_stale_ns",
                "command_watchdog_ns",
                "maximum_round_trip_error_rad",
                "linker_sdk",
            },
            "gateway",
        )
        transport = _text(gateway_raw["transport"], "gateway.transport")
        if transport not in ("fake", "linker-sdk"):
            raise DeploymentBindingError("gateway.transport must be fake or linker-sdk")
        sdk_raw_value = gateway_raw["linker_sdk"]
        sdk: LinkerSdkBinding | None = None
        if transport == "linker-sdk":
            sdk_raw = _object(sdk_raw_value, "gateway.linker_sdk")
            _exact(sdk_raw, {"sdk_root", "can_channel", "speed", "torque"}, "gateway.linker_sdk")
            speed_raw = sdk_raw["speed"]
            torque_raw = sdk_raw["torque"]
            if not isinstance(speed_raw, list) or not isinstance(torque_raw, list):
                raise DeploymentBindingError("Linker speed and torque must be lists")
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in (*speed_raw, *torque_raw)
            ):
                raise DeploymentBindingError("Linker speed and torque values must be integers")
            speed = tuple(speed_raw)
            torque = tuple(torque_raw)
            if (
                len(speed) != 5
                or len(torque) != 5
                or any(value < 0 or value > 255 for value in (*speed, *torque))
            ):
                raise DeploymentBindingError(
                    "Linker speed and torque need five values within 0..255"
                )
            sdk = LinkerSdkBinding(
                _resolve(base, sdk_raw["sdk_root"], "gateway.linker_sdk.sdk_root"),
                _text(sdk_raw["can_channel"], "gateway.linker_sdk.can_channel"),
                speed,
                torque,
            )
        elif sdk_raw_value is not None:
            raise DeploymentBindingError("fake gateway must set linker_sdk to null")
        gateway = GatewayRuntimeBinding(
            transport,
            _text(gateway_raw["gateway_id"], "gateway.gateway_id"),
            _positive_float(gateway_raw["gateway_hz"], "gateway.gateway_hz"),
            _positive_int(gateway_raw["state_stale_ns"], "gateway.state_stale_ns"),
            _positive_int(gateway_raw["command_watchdog_ns"], "gateway.command_watchdog_ns"),
            _positive_float(
                gateway_raw["maximum_round_trip_error_rad"],
                "gateway.maximum_round_trip_error_rad",
            ),
            sdk,
        )

        policies_raw = _object(raw["policies"], "policies")
        _exact(
            policies_raw,
            {"stores", "selected_package_id", "allow_unsigned_local"},
            "policies",
        )
        stores_raw = policies_raw["stores"]
        if not isinstance(stores_raw, list) or not stores_raw:
            raise DeploymentBindingError("at least one policy store is required")
        if not isinstance(policies_raw["allow_unsigned_local"], bool):
            raise DeploymentBindingError("allow_unsigned_local must be explicit boolean")
        policies = PolicyStoreBinding(
            tuple(_resolve(base, item, "policy store") for item in stores_raw),
            _text(policies_raw["selected_package_id"], "policies.selected_package_id"),
            policies_raw["allow_unsigned_local"],
        )

        safety_raw = _object(raw["safety"], "safety")
        _exact(
            safety_raw,
            {
                "position_lower_rad",
                "position_upper_rad",
                "maximum_delta_per_tick_rad",
                "maximum_target_rate_rad_s",
                "maximum_following_error_rad",
                "maximum_state_age_ns",
                "command_deadline_ns",
            },
            "safety",
        )
        lower_raw = safety_raw["position_lower_rad"]
        upper_raw = safety_raw["position_upper_rad"]
        if not isinstance(lower_raw, list) or not isinstance(upper_raw, list):
            raise DeploymentBindingError("safety position limits must be lists")
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in (*lower_raw, *upper_raw)
        ):
            raise DeploymentBindingError("safety position limits must be numeric")
        lower = tuple(float(value) for value in lower_raw)
        upper = tuple(float(value) for value in upper_raw)
        if (
            len(lower) != 16
            or len(upper) != 16
            or any(not math.isfinite(value) for value in (*lower, *upper))
            or any(high <= low for low, high in zip(lower, upper, strict=False))
        ):
            raise DeploymentBindingError("safety needs 16 finite ordered hand position limits")
        safety = SafetyRuntimeBinding(
            lower,
            upper,
            _positive_float(
                safety_raw["maximum_delta_per_tick_rad"],
                "safety.maximum_delta_per_tick_rad",
            ),
            _positive_float(
                safety_raw["maximum_target_rate_rad_s"],
                "safety.maximum_target_rate_rad_s",
            ),
            _positive_float(
                safety_raw["maximum_following_error_rad"],
                "safety.maximum_following_error_rad",
            ),
            _positive_int(safety_raw["maximum_state_age_ns"], "safety.maximum_state_age_ns"),
            _positive_int(safety_raw["command_deadline_ns"], "safety.command_deadline_ns"),
        )

        readiness_raw = _object(raw["readiness"], "readiness")
        _exact(
            readiness_raw,
            {
                "required_provider_ids",
                "evidence_validity_ns",
                "operator_confirmation_validity_ns",
            },
            "readiness",
        )
        provider_ids_raw = readiness_raw["required_provider_ids"]
        if not isinstance(provider_ids_raw, list) or any(
            not isinstance(value, str) or not value for value in provider_ids_raw
        ):
            raise DeploymentBindingError("readiness provider IDs must be non-empty strings")
        provider_ids = tuple(provider_ids_raw)
        if len(set(provider_ids)) != len(provider_ids):
            raise DeploymentBindingError("readiness provider IDs must be unique")
        mandatory = {
            "operator-confirmation-v1",
            "hand-state-freshness-v1",
            "gateway-health-v1",
            "policy-compatibility-v1",
        }
        if not mandatory.issubset(provider_ids):
            raise DeploymentBindingError("initial readiness provider set is incomplete")
        readiness = ReadinessRuntimeBinding(
            provider_ids,
            _positive_int(readiness_raw["evidence_validity_ns"], "readiness.evidence_validity_ns"),
            _positive_int(
                readiness_raw["operator_confirmation_validity_ns"],
                "readiness.operator_confirmation_validity_ns",
            ),
        )

        handoff_raw = _object(raw["handoff"], "handoff")
        _exact(
            handoff_raw,
            {
                "ownership_lease_ns",
                "gateway_ack_timeout_s",
                "teleop_command_period_ns",
                "policy_blend_ticks",
                "handback_blend_ticks",
            },
            "handoff",
        )
        handoff = HandoffRuntimeBinding(
            _positive_int(handoff_raw["ownership_lease_ns"], "handoff.ownership_lease_ns"),
            _positive_float(handoff_raw["gateway_ack_timeout_s"], "handoff.gateway_ack_timeout_s"),
            _positive_int(
                handoff_raw["teleop_command_period_ns"],
                "handoff.teleop_command_period_ns",
            ),
            _positive_int(handoff_raw["policy_blend_ticks"], "handoff.policy_blend_ticks"),
            _positive_int(handoff_raw["handback_blend_ticks"], "handoff.handback_blend_ticks"),
        )

        switch_raw = _object(raw["switch"], "switch")
        _exact(
            switch_raw,
            {
                "kind",
                "device_path",
                "source_id",
                "key",
                "debounce_ns",
                "require_pcsensor_identity",
            },
            "switch",
        )
        if switch_raw["kind"] != "evdev-f12" or switch_raw["key"] != "F12":
            raise DeploymentBindingError("the confirmed initial switch binding is evdev F12")
        if not isinstance(switch_raw["require_pcsensor_identity"], bool):
            raise DeploymentBindingError("switch identity enforcement must be explicit")
        if not switch_raw["require_pcsensor_identity"]:
            raise DeploymentBindingError("the confirmed PCsensor switch identity must be enforced")
        switch = SwitchBinding(
            "evdev-f12",
            _text(switch_raw["device_path"], "switch.device_path"),
            _text(switch_raw["source_id"], "switch.source_id"),
            "F12",
            _positive_int(switch_raw["debounce_ns"], "switch.debounce_ns"),
            switch_raw["require_pcsensor_identity"],
        )

        logging_raw = _object(raw["logging"], "logging")
        _exact(
            logging_raw,
            {"events_path", "trace_path", "trace_minimum_period_ns"},
            "logging",
        )
        logging = LoggingBinding(
            _resolve(base, logging_raw["events_path"], "logging.events_path"),
            _resolve(base, logging_raw["trace_path"], "logging.trace_path"),
            _positive_int(
                logging_raw["trace_minimum_period_ns"],
                "logging.trace_minimum_period_ns",
            ),
        )

        status_raw = _object(raw["status"], "status")
        _exact(status_raw, {"period_ns", "use_ansi"}, "status")
        if not isinstance(status_raw["use_ansi"], bool):
            raise DeploymentBindingError("status.use_ansi must be explicit boolean")
        status = StatusBinding(
            _positive_int(status_raw["period_ns"], "status.period_ns"),
            status_raw["use_ansi"],
        )
        arm_raw = _object(raw["arm"], "arm")
        arm_mode = _text(arm_raw.get("mode"), "arm.mode")
        if arm_mode == "fake-hold":
            _exact(arm_raw, {"mode"}, "arm")
            arm = ArmRuntimeBinding("fake-hold", None, None, None, None, None)
        elif arm_mode == "hitbot-hold-v1":
            _exact(
                arm_raw,
                {
                    "mode",
                    "control_host",
                    "control_port",
                    "request_timeout_s",
                    "command_ttl_ns",
                    "hold_lease_ns",
                },
                "arm",
            )
            host = _text(arm_raw["control_host"], "arm.control_host")
            if host not in ("127.0.0.1", "localhost"):
                raise DeploymentBindingError("Hitbot hold control must use loopback")
            port = _positive_int(arm_raw["control_port"], "arm.control_port")
            if port > 65535:
                raise DeploymentBindingError("arm.control_port must be within 1..65535")
            arm = ArmRuntimeBinding(
                arm_mode,
                host,
                port,
                _positive_float(arm_raw["request_timeout_s"], "arm.request_timeout_s"),
                _positive_int(arm_raw["command_ttl_ns"], "arm.command_ttl_ns"),
                _positive_int(arm_raw["hold_lease_ns"], "arm.hold_lease_ns"),
            )
        else:
            raise DeploymentBindingError(f"unsupported arm mode: {arm_mode}")

        return cls(
            source,
            1,
            _text(raw["binding_id"], "binding_id"),
            "1.0",
            _text(raw["control_session_id"], "control_session_id"),
            hand,
            calibration,
            teleop,
            gateway,
            policies,
            safety,
            readiness,
            handoff,
            switch,
            logging,
            status,
            arm,
        )
