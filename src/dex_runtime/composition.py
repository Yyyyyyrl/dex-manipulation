"""Construct the adopted hand-only runtime from a validated preflight result."""

from __future__ import annotations

from dex_hardware_linker import (
    FakeLinkerTransport,
    GatewayConfig,
    LinkerGateway,
    LinkerSdkTransport,
)
from dex_teleop_adapters import ManusHandSource, build_dexpilot_retargeter

from .application import HandOnlyRuntime
from .operator_switch import EvdevF12SwitchSource
from .preflight import PreflightResult


def build_hand_only_runtime(preflight: PreflightResult) -> HandOnlyRuntime:
    """Bind only declared, preflighted components; this function opens no transport."""

    binding = preflight.binding
    if binding.gateway.transport == "fake":
        midpoint = tuple(
            (lower + upper) * 0.5
            for lower, upper in zip(
                binding.safety.position_lower_rad,
                binding.safety.position_upper_rad, strict=False,
            )
        )
        transport = FakeLinkerTransport(
            preflight.mapper.prepare(midpoint).native_range
        )
    else:
        sdk = binding.gateway.linker_sdk
        if sdk is None:
            raise ValueError("linker-sdk transport binding is missing")
        transport = LinkerSdkTransport(
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
    source = ManusHandSource(
        source_id=binding.teleop.manus.source_id,
        hand_side=binding.hand.side,
        topic=binding.teleop.manus.topic,
        stale_after_ns=binding.teleop.manus.stale_after_ns,
    )
    retargeter = build_dexpilot_retargeter(
        preflight.teleop_profile,
        model_directory=binding.teleop.retargeting_model_directory,
        candidate_ttl_ns=binding.teleop.manus.candidate_ttl_ns,
    )
    switch = EvdevF12SwitchSource(
        device_path=binding.switch.device_path,
        source_id=binding.switch.source_id,
        debounce_ns=binding.switch.debounce_ns,
        require_pcsensor_identity=binding.switch.require_pcsensor_identity,
    )
    return HandOnlyRuntime(preflight, gateway, source, retargeter, switch)
