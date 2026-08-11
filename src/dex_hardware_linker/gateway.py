"""Exclusive, epoch-enforcing Linker gateway running on a dedicated thread."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from dex_contracts import (
    PROTOCOL_VERSION,
    AcknowledgementLevel,
    AuthorizedHandCommand,
    CommandMode,
    EffectiveHandTarget,
    GatewayAcknowledgement,
    HandCommandAcknowledgement,
    HandState,
    MessageIdentity,
    OwnerKind,
    OwnershipState,
    ResourceId,
)

from .calibration import LinkerMapper, PreparedCommand
from .transport import LinkerTransport


class GatewayRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    gateway_id: str
    control_session_id: str
    gateway_hz: float
    state_stale_ns: int
    command_watchdog_ns: int
    maximum_round_trip_error_rad: float

    def __post_init__(self) -> None:
        if not self.gateway_id or not self.control_session_id:
            raise ValueError("gateway and session IDs are required")
        if self.gateway_hz <= 0:
            raise ValueError("gateway_hz must be positive")
        if self.state_stale_ns <= 0 or self.command_watchdog_ns <= 0:
            raise ValueError("gateway freshness/watchdog limits must be positive")
        if self.maximum_round_trip_error_rad <= 0:
            raise ValueError("maximum round-trip error must be positive")


@dataclass(frozen=True)
class OwnershipPreparation:
    nonce: str
    ownership: OwnershipState


class SubmissionTicket:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._ack: HandCommandAcknowledgement | None = None
        self._error: BaseException | None = None

    def _resolve(self, acknowledgement: HandCommandAcknowledgement) -> None:
        self._ack = acknowledgement
        self._event.set()

    def _reject(self, error: BaseException) -> None:
        self._error = error
        self._event.set()

    def wait(self, timeout_s: float) -> HandCommandAcknowledgement:
        if not self._event.wait(timeout_s):
            raise TimeoutError("gateway acknowledgement deadline expired")
        if self._error is not None:
            raise self._error
        if self._ack is None:
            raise RuntimeError("submission completed without acknowledgement")
        return self._ack


@dataclass(frozen=True)
class _Envelope:
    command: AuthorizedHandCommand
    ticket: SubmissionTicket


class LinkerGateway:
    """The only component allowed to call a Linker transport.

    Commands enter through a bounded local channel and are accepted only when
    session, owner, epoch, calibration, mapping, and deadline all match the
    gateway's committed ownership state.
    """

    def __init__(
        self,
        config: GatewayConfig,
        mapper: LinkerMapper,
        transport: LinkerTransport,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.config = config
        self.mapper = mapper
        self.transport = transport
        self._clock_ns = clock_ns
        self._queue: queue.Queue[_Envelope] = queue.Queue(maxsize=1)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._prepared: OwnershipPreparation | None = None
        self._ownership: OwnershipState | None = None
        self._latest_state: HandState | None = None
        self._last_effective: EffectiveHandTarget | None = None
        self._last_command_time_ns: int | None = None
        self._fault_reason: str | None = None
        self._state_sequence = 0

    @property
    def fault_reason(self) -> str | None:
        with self._lock:
            return self._fault_reason

    @property
    def ownership(self) -> OwnershipState | None:
        with self._lock:
            return self._ownership

    @property
    def latest_state(self) -> HandState | None:
        with self._lock:
            return self._latest_state

    def start(self, timeout_s: float = 5.0) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("gateway is already started")
            self._thread = threading.Thread(
                target=self._run,
                name=f"{self.config.gateway_id}-thread",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout_s):
            raise TimeoutError("gateway transport did not become ready")
        if self.fault_reason is not None:
            raise RuntimeError(f"gateway failed to start: {self.fault_reason}")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError("gateway thread did not stop")

    def prepare_ownership(self, ownership: OwnershipState) -> OwnershipPreparation:
        now = self._clock_ns()
        if ownership.control_session_id != self.config.control_session_id:
            raise GatewayRejected("ownership session mismatch")
        if ownership.resource_id is not ResourceId.HAND:
            raise GatewayRejected("Linker gateway can own only the hand resource")
        if ownership.command_mode not in (
            CommandMode.SEMANTIC_POSITION,
            CommandMode.SAFE_HOLD,
        ):
            raise GatewayRejected("unsupported Linker command mode")
        if not ownership.valid_at(now):
            raise GatewayRejected("ownership is not valid now")
        with self._lock:
            current_epoch = -1 if self._ownership is None else self._ownership.control_epoch
            if ownership.control_epoch <= current_epoch:
                raise GatewayRejected("ownership epoch must increase")
            prepared = OwnershipPreparation(uuid.uuid4().hex, ownership)
            self._prepared = prepared
            return prepared

    def commit_ownership(self, preparation: OwnershipPreparation) -> None:
        with self._lock:
            if self._prepared != preparation:
                raise GatewayRejected("ownership preparation token is stale or unknown")
            self._ownership = preparation.ownership
            self._prepared = None
            self._last_command_time_ns = self._clock_ns()

    def submit(self, command: AuthorizedHandCommand) -> SubmissionTicket:
        now = self._clock_ns()
        with self._lock:
            ownership = self._ownership
            self._validate_command(command, ownership, now)
        ticket = SubmissionTicket()
        try:
            self._queue.put_nowait(_Envelope(command, ticket))
        except queue.Full as exc:
            raise GatewayRejected("command channel full; command was not dropped silently") from exc
        return ticket

    def _validate_command(
        self,
        command: AuthorizedHandCommand,
        ownership: OwnershipState | None,
        now_ns: int,
    ) -> None:
        identity = command.identity
        if ownership is None or not ownership.valid_at(now_ns):
            raise GatewayRejected("no valid committed hand ownership")
        if identity.control_session_id != self.config.control_session_id:
            raise GatewayRejected("command session mismatch")
        if identity.resource_id is not ResourceId.HAND:
            raise GatewayRejected("command resource mismatch")
        calibration = self.mapper.calibration
        if identity.hand_model != calibration.hand_model:
            raise GatewayRejected("command hand model mismatch")
        if identity.hand_side != calibration.hand_side:
            raise GatewayRejected("command hand side mismatch")
        if identity.semantic_schema_id != calibration.semantic_schema_id:
            raise GatewayRejected("command semantic schema mismatch")
        if identity.calibration_id != calibration.calibration_id:
            raise GatewayRejected("command identity calibration mismatch")
        if identity.control_epoch != ownership.control_epoch:
            raise GatewayRejected("stale or future control epoch")
        if command.owner is not ownership.owner:
            raise GatewayRejected("command owner mismatch")
        if command.command_mode is not ownership.command_mode:
            raise GatewayRejected("command mode mismatch")
        if command.deadline_ns <= now_ns:
            raise GatewayRejected("command deadline expired")
        if command.calibration_id != self.mapper.calibration.calibration_id:
            raise GatewayRejected("calibration ID mismatch")
        if command.mapping_id != self.mapper.calibration.artifact_digest:
            raise GatewayRejected("mapping digest mismatch")

    def _command_identity(
        self,
        sequence: int,
        epoch: int,
        *,
        task_id: str | None = None,
        task_version: str | None = None,
        policy_package_id: str | None = None,
    ) -> MessageIdentity:
        calibration = self.mapper.calibration
        return MessageIdentity(
            protocol_version=PROTOCOL_VERSION,
            control_session_id=self.config.control_session_id,
            source_id=self.config.gateway_id,
            resource_id=ResourceId.HAND,
            hand_model=calibration.hand_model,
            hand_side=calibration.hand_side,
            semantic_schema_id=calibration.semantic_schema_id,
            task_id=task_id,
            task_version=task_version,
            policy_package_id=policy_package_id,
            calibration_id=calibration.calibration_id,
            control_epoch=epoch,
            sequence=sequence,
        )

    def _process(self, envelope: _Envelope, now_ns: int) -> None:
        command = envelope.command
        with self._lock:
            ownership = self._ownership
            self._validate_command(command, ownership, now_ns)
        prepared: PreparedCommand = self.mapper.prepare(command.semantic_position)
        if prepared.preview.saturated_joints:
            raise GatewayRejected(
                "semantic target saturated: " + ",".join(prepared.preview.saturated_joints)
            )
        maximum_error = max(abs(value) for value in prepared.round_trip_error)
        if maximum_error > self.config.maximum_round_trip_error_rad:
            raise GatewayRejected(
                f"mapping round-trip error {maximum_error:.6f} exceeds configured limit"
            )
        self.transport.send(prepared.native_range)
        acknowledged = self._clock_ns()
        level = min(
            self.transport.acknowledgement_level,
            AcknowledgementLevel.SENT_TO_BUS,
        )
        effective = EffectiveHandTarget(
            semantic_position=prepared.diagnostic_semantic,
            command_id=command.command_id,
            evidence_level=level,
            evidence_time_ns=acknowledged,
        )
        gateway_ack = GatewayAcknowledgement(
            identity=self._command_identity(
                command.identity.sequence,
                command.identity.control_epoch,
                task_id=command.identity.task_id,
                task_version=command.identity.task_version,
                policy_package_id=command.identity.policy_package_id,
            ),
            command_id=command.command_id,
            level=level,
            acknowledged_time_ns=acknowledged,
            detail="transport returned after send; no servo-applied evidence claimed",
        )
        with self._lock:
            self._last_effective = effective
            self._last_command_time_ns = acknowledged
            ownership = self._ownership
            if ownership is not None and ownership.valid_at(acknowledged):
                lease_ns = ownership.expiry_time_ns - ownership.start_time_ns
                self._ownership = replace(
                    ownership,
                    start_time_ns=acknowledged,
                    expiry_time_ns=acknowledged + lease_ns,
                )
        envelope.ticket._resolve(HandCommandAcknowledgement(gateway_ack, effective))

    def _update_state(self, now_ns: int) -> None:
        native = self.transport.read_state()
        if native is None:
            return
        semantic = self.mapper.inverse(native.native_range)
        with self._lock:
            epoch = 0 if self._ownership is None else self._ownership.control_epoch
            identity = self._command_identity(self._state_sequence, epoch)
            self._state_sequence += 1
            self._latest_state = HandState(
                identity=identity,
                semantic_position=semantic,
                semantic_velocity=None,
                semantic_effort=None,
                acquisition_time_ns=native.acquisition_time_ns,
                raw_native_state_ref=native.raw_reference,
                state_quality="fresh"
                if now_ns - native.acquisition_time_ns <= self.config.state_stale_ns
                else "stale",
                missing_joint_mask=(False,) * len(semantic),
                hardware_faults=native.faults,
                temperatures_c=native.temperatures_c,
                last_effective_target=self._last_effective,
                acknowledgement_capability=min(
                    self.transport.acknowledgement_level,
                    AcknowledgementLevel.SENT_TO_BUS,
                ),
            )

    def _watchdog(self, now_ns: int) -> None:
        with self._lock:
            ownership = self._ownership
            last_command = self._last_command_time_ns
            if ownership is None or self._fault_reason is not None:
                return
            expired = now_ns >= ownership.expiry_time_ns
            missed = (
                last_command is not None and now_ns - last_command > self.config.command_watchdog_ns
            )
            if expired or missed:
                self._fault_reason = "ownership-expired" if expired else "command-watchdog-expired"
                self._ownership = OwnershipState(
                    control_session_id=self.config.control_session_id,
                    resource_id=ResourceId.HAND,
                    owner=OwnerKind.SAFETY,
                    control_epoch=ownership.control_epoch + 1,
                    command_mode=CommandMode.SAFE_HOLD,
                    start_time_ns=now_ns,
                    expiry_time_ns=now_ns + self.config.command_watchdog_ns,
                    gateway_acknowledged=True,
                    watchdog_healthy=False,
                )

    def _run(self) -> None:
        period_ns = int(1_000_000_000 / self.config.gateway_hz)
        next_tick = self._clock_ns()
        try:
            self.transport.open()
            self._ready.set()
            while not self._stop.is_set():
                now = self._clock_ns()
                self._update_state(now)
                try:
                    envelope = self._queue.get_nowait()
                except queue.Empty:
                    envelope = None
                if envelope is not None:
                    try:
                        self._process(envelope, self._clock_ns())
                    except BaseException as exc:
                        envelope.ticket._reject(exc)
                self._watchdog(self._clock_ns())
                next_tick += period_ns
                wait_ns = next_tick - self._clock_ns()
                if wait_ns <= 0:
                    next_tick = self._clock_ns()
                else:
                    self._stop.wait(wait_ns / 1_000_000_000)
        except BaseException as exc:
            with self._lock:
                self._fault_reason = f"gateway-thread-fault:{type(exc).__name__}:{exc}"
            self._ready.set()
        finally:
            try:
                self.transport.close()
            except BaseException as exc:
                with self._lock:
                    if self._fault_reason is None:
                        self._fault_reason = f"gateway-close-fault:{type(exc).__name__}:{exc}"
