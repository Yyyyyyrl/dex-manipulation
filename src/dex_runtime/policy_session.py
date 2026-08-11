"""Environment-free policy inference with explicit shadow/activation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Mapping, Sequence

import torch
import torch.nn as nn

from dex_contracts import (
    MessageIdentity,
    PolicyHandCandidate,
    PROTOCOL_VERSION,
    ResourceId,
)

from .codecs import ProprioCodec
from .policy_package import ValidatedPolicyPackage


_ACTIVATIONS: dict[str, type[nn.Module]] = {
    "elu": nn.ELU,
    "relu": nn.ReLU,
    "tanh": nn.Tanh,
    "selu": nn.SELU,
    "gelu": nn.GELU,
}


class RunningMeanStd(nn.Module):
    def __init__(self, dim: int, epsilon: float = 1e-5) -> None:
        super().__init__()
        self.epsilon = epsilon
        self.register_buffer("running_mean", torch.zeros(dim, dtype=torch.float32))
        self.register_buffer("running_var", torch.ones(dim, dtype=torch.float32))
        self.register_buffer("count", torch.ones((), dtype=torch.float32))

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        normalized = (value - self.running_mean) / torch.sqrt(
            self.running_var + self.epsilon
        )
        return torch.clamp(normalized, -5.0, 5.0)


class RuntimeActor(nn.Module):
    def __init__(self, architecture: Mapping[str, object]) -> None:
        super().__init__()
        units = [int(value) for value in architecture["mlp_units"]]
        activation_name = str(architecture["activation"]).lower()
        if activation_name not in _ACTIVATIONS:
            raise ValueError(f"unsupported actor activation {activation_name!r}")
        activation = _ACTIVATIONS[activation_name]
        self.proprio_dim = int(architecture["proprio_dim"])
        self.latent_dim = int(architecture["latent_dim"])
        self.action_dim = int(architecture["action_dim"])
        self.clip_obs = float(architecture["clip_obs"])
        self.normalize_input = bool(architecture["normalize_input"])
        layers: list[nn.Module] = []
        width = self.proprio_dim + self.latent_dim
        for output_width in units:
            layers.extend((nn.Linear(width, output_width), activation()))
            width = output_width
        self.actor_mlp = nn.Sequential(*layers)
        self.mu = nn.Linear(width, self.action_dim)
        self.running_mean_std = (
            RunningMeanStd(self.proprio_dim) if self.normalize_input else None
        )

    def forward(self, proprio: torch.Tensor, latent: torch.Tensor) -> torch.Tensor:
        if proprio.shape[-1] != self.proprio_dim or latent.shape[-1] != self.latent_dim:
            raise ValueError("actor input width does not match package architecture")
        value = torch.clamp(proprio, -self.clip_obs, self.clip_obs)
        if self.running_mean_std is not None:
            value = self.running_mean_std(value)
        return self.mu(self.actor_mlp(torch.cat((value, latent), dim=-1)))


class RuntimeAdapter(nn.Module):
    def __init__(self, frame_dim: int, history_length: int, output_dim: int) -> None:
        super().__init__()
        self.frame_enc = nn.Sequential(
            nn.Linear(frame_dim, 32),
            nn.ELU(),
            nn.Linear(32, 32),
            nn.ELU(),
        )
        self.temporal = nn.Sequential(
            nn.Conv1d(32, 32, kernel_size=9, stride=2),
            nn.ELU(),
            nn.Conv1d(32, 32, kernel_size=5, stride=1),
            nn.ELU(),
            nn.Conv1d(32, 32, kernel_size=5, stride=1),
            nn.ELU(),
        )
        with torch.no_grad():
            flattened = self.temporal(torch.zeros(1, 32, history_length)).flatten(1).shape[1]
        self.head = nn.Linear(flattened, output_dim)

    def forward(self, history: torch.Tensor) -> torch.Tensor:
        batch, ticks, width = history.shape
        frames = self.frame_enc(history.reshape(batch * ticks, width)).reshape(
            batch, ticks, 32
        )
        return self.head(self.temporal(frames.permute(0, 2, 1)).flatten(1))


class PolicySessionState(str, Enum):
    LOADED = "loaded"
    SHADOW = "shadow"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    CLOSED = "closed"


@dataclass(frozen=True)
class PolicySessionStatus:
    state: PolicySessionState
    package_id: str
    codec_id: str
    history_count: int
    history_required: int
    latest_tick: int | None
    control_epoch: int | None
    preview_sequence: int
    last_action_max_abs: float | None

    @property
    def history_ready(self) -> bool:
        return self.history_count >= self.history_required


@dataclass(frozen=True)
class PolicyInferenceTrace:
    tick: int
    scheduled_time_ns: int
    state_sequence: int
    codec_input: tuple[float, ...]
    latent: tuple[float, ...]
    action: tuple[float, ...]
    target: tuple[float, ...]

class PolicySession:
    """One policy package, primed continuously before ownership is possible."""

    def __init__(self, package: ValidatedPolicyPackage, *, device: str = "cpu") -> None:
        self.package = package
        self.device = torch.device(device)
        self.codec = ProprioCodec(package.codec_spec)
        network = package.manifest["network"]
        actor_arch = network["actor"]
        adapter_arch = network["adapter"]
        self.actor = RuntimeActor(actor_arch).to(self.device).eval()
        self.adapter = RuntimeAdapter(
            int(adapter_arch["frame_dim"]),
            int(adapter_arch["history_length"]),
            int(adapter_arch["output_dim"]),
        ).to(self.device).eval()
        actor_state, adapter_state = package.load_tensors(str(self.device))
        self.actor.load_state_dict(actor_state, strict=True)
        self.adapter.load_state_dict(adapter_state, strict=True)

        transform = package.manifest["action_transform"]
        self._delta_scale = float(transform["delta_scale_rad"])
        # Keep the manifest's Python-float limits as the canonical values used
        # at the policy/safety boundary. The actor runs in float32, where a
        # value such as -0.17 can round a few nanoradians outside the manifest
        # limit even after torch.clamp.
        self._position_lower = tuple(
            float(value) for value in transform["position_lower_rad"]
        )
        self._position_upper = tuple(
            float(value) for value in transform["position_upper_rad"]
        )
        self._lower = torch.tensor(
            self._position_lower, dtype=torch.float32, device=self.device
        ).reshape(1, -1)
        self._upper = torch.tensor(
            self._position_upper, dtype=torch.float32, device=self.device
        ).reshape(1, -1)
        task = package.manifest["task"]
        hand = package.manifest["hand"]
        calibration = package.manifest["calibration_compatibility"][0]
        self._task_id = str(task["id"])
        self._task_version = str(task["version"])
        self._hand_model = str(hand["model"])
        self._hand_side = str(hand["side"])
        self._semantic_schema_id = str(hand["semantic_schema_id"])
        self._calibration_id = str(calibration["calibration_id"])

        self._state = PolicySessionState.LOADED
        self._history = self.codec.empty_history(
            (1,), device=self.device, dtype=torch.float32
        )
        self._history_count = 0
        self._effective_target: torch.Tensor | None = None
        self._latest_tick: int | None = None
        self._latest_scheduled_ns: int | None = None
        self._latest_state_sequence: int | None = None
        self._control_session_id: str | None = None
        self._source_id: str | None = None
        self._control_epoch: int | None = None
        self._preview_sequence = 0
        self._cached_tick: int | None = None
        self._cached_preview: PolicyHandCandidate | None = None
        self._last_action_max_abs: float | None = None
        self._last_inference: PolicyInferenceTrace | None = None

    @property
    def status(self) -> PolicySessionStatus:
        return PolicySessionStatus(
            self._state,
            self.package.descriptor.package_id,
            self.codec.spec.codec_id,
            self._history_count,
            self.codec.spec.history_length,
            self._latest_tick,
            self._control_epoch,
            self._preview_sequence,
            self._last_action_max_abs,
        )

    @property
    def last_inference(self) -> PolicyInferenceTrace | None:
        return self._last_inference

    @property
    def last_preview(self) -> PolicyHandCandidate | None:
        return self._cached_preview

    def _row(self, value: Sequence[float] | torch.Tensor, label: str) -> torch.Tensor:
        tensor = torch.as_tensor(value, dtype=torch.float32, device=self.device).reshape(1, -1)
        if tensor.shape[-1] != self.codec.spec.joint_count or not bool(torch.isfinite(tensor).all()):
            raise ValueError(f"{label} must contain finite canonical semantic joints")
        return tensor

    # Effective targets are read back from quantized hardware (integer native
    # slots) and blended in float, so a target that sits on an action bound can
    # land a fraction of a native step outside it.  Admit that measurement noise:
    # the action clamp below stays exact and the HandSafetySupervisor still
    # enforces the deployment limits on every command.
    _LIMIT_TOLERANCE_RAD = 0.02

    def _require_target_within_limits(self, target: torch.Tensor) -> None:
        low = self._lower - self._LIMIT_TOLERANCE_RAD
        high = self._upper + self._LIMIT_TOLERANCE_RAD
        if bool(((target < low) | (target > high)).any()):
            raise ValueError("effective target lies outside package action limits")

    def reset(
        self,
        measured_position_rad: Sequence[float] | torch.Tensor,
        effective_target_rad: Sequence[float] | torch.Tensor,
        *,
        control_session_id: str,
        source_id: str,
        control_epoch: int,
    ) -> None:
        if self._state not in (PolicySessionState.LOADED, PolicySessionState.DEACTIVATED):
            raise RuntimeError(f"cannot reset a policy session from {self._state.value}")
        if not control_session_id or not source_id or control_epoch < 0:
            raise ValueError("session/source identity and a non-negative epoch are required")
        measured = self._row(measured_position_rad, "measured position")
        effective = self._row(effective_target_rad, "effective target")
        self._require_target_within_limits(effective)
        self.codec.encode_frame(measured, effective)
        self._history = self.codec.empty_history(
            (1,), device=self.device, dtype=torch.float32
        )
        self._history_count = 0
        self._effective_target = effective
        self._latest_tick = None
        self._latest_scheduled_ns = None
        self._latest_state_sequence = None
        self._control_session_id = control_session_id
        self._source_id = source_id
        self._control_epoch = control_epoch
        self._preview_sequence = 0
        self._cached_tick = None
        self._cached_preview = None
        self._last_action_max_abs = None
        self._last_inference = None
        self._state = PolicySessionState.SHADOW

    def synchronize_to_effective_target(
        self, effective_target_rad: Sequence[float] | torch.Tensor
    ) -> None:
        if self._state not in (PolicySessionState.SHADOW, PolicySessionState.ACTIVE):
            raise RuntimeError("policy target synchronization requires shadow or active state")
        target = self._row(effective_target_rad, "effective target")
        self._require_target_within_limits(target)
        self._effective_target = target
        self._cached_tick = None
        self._cached_preview = None

    def observe(
        self,
        measured_position_rad: Sequence[float] | torch.Tensor,
        effective_target_rad: Sequence[float] | torch.Tensor,
        *,
        tick: int,
        scheduled_time_ns: int,
        state_sequence: int,
    ) -> None:
        if self._state not in (PolicySessionState.SHADOW, PolicySessionState.ACTIVE):
            raise RuntimeError("policy observation requires shadow or active state")
        if tick < 0 or scheduled_time_ns < 0 or state_sequence < 0:
            raise ValueError("policy tick, time, and state sequence must be non-negative")
        if self._latest_tick is not None:
            if tick != self._latest_tick + 1:
                raise ValueError("policy ticks must be consecutive; duplicate or skipped tick")
            if scheduled_time_ns != self._latest_scheduled_ns + self.codec.spec.control_period_ns:
                raise ValueError("policy observation did not follow package-declared cadence")
            if state_sequence <= self._latest_state_sequence:
                raise ValueError("hand state sequence must increase on every policy tick")
        measured = self._row(measured_position_rad, "measured position")
        effective = self._row(effective_target_rad, "effective target")
        self._require_target_within_limits(effective)
        frame = self.codec.encode_frame(measured, effective)
        self._history = self.codec.append(self._history, frame)
        self._history_count = min(self.codec.spec.history_length, self._history_count + 1)
        self._effective_target = effective
        self._latest_tick = tick
        self._latest_scheduled_ns = scheduled_time_ns
        self._latest_state_sequence = state_sequence
        self._cached_tick = None
        self._cached_preview = None

    def _identity(self) -> MessageIdentity:
        if self._control_session_id is None or self._source_id is None or self._control_epoch is None:
            raise RuntimeError("policy session identity is not initialized")
        return MessageIdentity(
            protocol_version=PROTOCOL_VERSION,
            control_session_id=self._control_session_id,
            source_id=self._source_id,
            resource_id=ResourceId.HAND,
            hand_model=self._hand_model,
            hand_side=self._hand_side,
            semantic_schema_id=self._semantic_schema_id,
            task_id=self._task_id,
            task_version=self._task_version,
            policy_package_id=self.package.descriptor.package_id,
            calibration_id=self._calibration_id,
            control_epoch=self._control_epoch,
            sequence=self._preview_sequence,
        )

    @torch.no_grad()
    def preview(self) -> PolicyHandCandidate:
        if self._state not in (PolicySessionState.SHADOW, PolicySessionState.ACTIVE):
            raise RuntimeError("policy preview requires shadow or active state")
        if self._history_count < self.codec.spec.history_length:
            raise RuntimeError("policy history is not ready")
        if self._latest_tick is None or self._latest_scheduled_ns is None or self._effective_target is None:
            raise RuntimeError("policy has no current observation")
        if self._cached_tick == self._latest_tick and self._cached_preview is not None:
            return self._cached_preview
        actor_input = self.codec.assemble_actor_input(self._history)
        latent = self.adapter(self._history)
        action = torch.clamp(self.actor(actor_input, latent), -1.0, 1.0)
        target = torch.clamp(
            self._effective_target + self._delta_scale * action,
            self._lower,
            self._upper,
        )
        target_values = tuple(
            min(max(float(value), lower), upper)
            for value, lower, upper in zip(
                target[0].tolist(), self._position_lower, self._position_upper
            )
        )
        maximum = float(action.abs().max().item())
        if not math.isfinite(maximum):
            raise RuntimeError("policy produced a non-finite action")
        self._last_inference = PolicyInferenceTrace(
            tick=self._latest_tick,
            scheduled_time_ns=self._latest_scheduled_ns,
            state_sequence=self._latest_state_sequence,
            codec_input=tuple(float(value) for value in actor_input[0].tolist()),
            latent=tuple(float(value) for value in latent[0].tolist()),
            action=tuple(float(value) for value in action[0].tolist()),
            target=target_values,
        )
        candidate = PolicyHandCandidate(
            identity=self._identity(),
            semantic_position=target_values,
            generated_time_ns=self._latest_scheduled_ns,
            valid_until_ns=self._latest_scheduled_ns + self.codec.spec.control_period_ns,
            source_state_sequence=self._latest_state_sequence,
            diagnostics=(
                ("codec_id", self.codec.spec.codec_id),
                ("history_count", self._history_count),
                ("action_max_abs", maximum),
            ),
            confidence=None,
        )
        self._preview_sequence += 1
        self._last_action_max_abs = maximum
        self._cached_tick = self._latest_tick
        self._cached_preview = candidate
        return candidate

    def activate(self, *, tick: int, control_epoch: int) -> PolicyHandCandidate:
        if self._state is not PolicySessionState.SHADOW:
            raise RuntimeError("only a shadow policy session can activate")
        if self._cached_tick != tick or self._cached_preview is None:
            raise RuntimeError("activation requires a same-tick policy preview")
        if self._control_epoch is None or control_epoch <= self._control_epoch:
            raise ValueError("activation control epoch must increase")
        self._control_epoch = control_epoch
        identity = replace(
            self._cached_preview.identity,
            control_epoch=control_epoch,
        )
        self._cached_preview = replace(self._cached_preview, identity=identity)
        self._state = PolicySessionState.ACTIVE
        return self._cached_preview

    def step(
        self,
        measured_position_rad: Sequence[float] | torch.Tensor,
        effective_target_rad: Sequence[float] | torch.Tensor,
        *,
        tick: int,
        scheduled_time_ns: int,
        state_sequence: int,
    ) -> PolicyHandCandidate:
        if self._state is not PolicySessionState.ACTIVE:
            raise RuntimeError("policy step requires active state")
        self.observe(
            measured_position_rad,
            effective_target_rad,
            tick=tick,
            scheduled_time_ns=scheduled_time_ns,
            state_sequence=state_sequence,
        )
        return self.preview()

    def deactivate(self) -> None:
        if self._state not in (PolicySessionState.SHADOW, PolicySessionState.ACTIVE):
            raise RuntimeError("only a shadow or active policy can deactivate")
        self._state = PolicySessionState.DEACTIVATED
        self._cached_tick = None
        self._cached_preview = None

    def close(self) -> None:
        self._state = PolicySessionState.CLOSED
        self._cached_tick = None
        self._cached_preview = None
