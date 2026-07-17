"""Immutable LinkerHand calibration artifacts and pure mapping stages.

This runtime copy is parity-tested against dex-forge golden fixtures. It has no module-
global calibration switch: callers load one content-addressed artifact and keep
the resulting frozen mapper for the lifetime of a control session.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


_ASSET_ROOT = Path(__file__).resolve().parent / "assets"
DEFAULT_SCHEMA_PATH = (
    _ASSET_ROOT / "calibrations" / "linker_g20_left_semantic_schema_v1.json"
)
DEFAULT_CALIBRATION_PATH = (
    _ASSET_ROOT / "calibrations" / "linker_g20_left_lht20_010_415_v1.json"
)


def canonical_json_digest(payload: Mapping[str, Any], excluded_field: str) -> str:
    """Digest canonical JSON after removing its self-referential digest field."""

    body = dict(payload)
    if excluded_field not in body:
        raise ValueError(f"missing digest field {excluded_field!r}")
    body.pop(excluded_field)
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{source}: expected a JSON object")
    return value


def _number_pair(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label}: expected a two-value list")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError(f"{label}: limits must be numeric")
    lo, hi = float(value[0]), float(value[1])
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        raise ValueError(f"{label}: expected finite upper > lower, got {value!r}")
    return lo, hi


@dataclass(frozen=True)
class SemanticJointSpec:
    name: str
    lower: float
    upper: float
    continuous: bool


@dataclass(frozen=True)
class MimicRelationship:
    joint: str
    source: str
    multiplier: float
    offset: float


@dataclass(frozen=True)
class SemanticJointSchema:
    schema_id: str
    schema_version: int
    hand_model: str
    hand_side: str
    units: str
    joints: tuple[SemanticJointSpec, ...]
    mimic_relationships: tuple[MimicRelationship, ...]
    digest: str

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(joint.name for joint in self.joints)


def load_semantic_schema(path: str | Path = DEFAULT_SCHEMA_PATH) -> SemanticJointSchema:
    raw = _read_object(path)
    actual = canonical_json_digest(raw, "digest")
    declared = raw.get("digest")
    if declared != actual:
        raise ValueError(f"semantic schema digest mismatch: declared {declared}, actual {actual}")
    if raw.get("units") != "radian":
        raise ValueError("semantic schema units must be 'radian'")
    entries = raw.get("ordered_joints")
    if not isinstance(entries, list) or not entries:
        raise ValueError("semantic schema needs a non-empty ordered_joints list")
    joints: list[SemanticJointSpec] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError(f"ordered_joints[{index}] is invalid")
        lower, upper = _number_pair(entry.get("position_limit"), entry["name"])
        continuous = entry.get("continuous")
        if not isinstance(continuous, bool):
            raise ValueError(f"{entry['name']}: continuous must be boolean")
        joints.append(SemanticJointSpec(entry["name"], lower, upper, continuous))
    if len({joint.name for joint in joints}) != len(joints):
        raise ValueError("semantic schema contains duplicate joint names")

    mimic: list[MimicRelationship] = []
    for entry in raw.get("mimic_relationships", []):
        mimic.append(
            MimicRelationship(
                joint=str(entry["joint"]),
                source=str(entry["source"]),
                multiplier=float(entry["multiplier"]),
                offset=float(entry["offset"]),
            )
        )
    return SemanticJointSchema(
        schema_id=str(raw["schema_id"]),
        schema_version=int(raw["schema_version"]),
        hand_model=str(raw["hand_model"]),
        hand_side=str(raw["hand_side"]),
        units=str(raw["units"]),
        joints=tuple(joints),
        mimic_relationships=tuple(mimic),
        digest=str(declared),
    )


@dataclass(frozen=True)
class JointCalibration:
    name: str
    slot: int
    lower: float
    upper: float
    flip: bool
    offset: float


@dataclass(frozen=True)
class LinkerCalibration:
    calibration_id: str
    artifact_digest: str
    hand_model: str
    hand_joint: str
    hand_side: str
    serial_number: str | None
    semantic_schema_id: str
    semantic_schema_digest: str
    joints: tuple[JointCalibration, ...]
    mapping_mode: str
    native_arc_min: tuple[float, ...]
    native_arc_max: tuple[float, ...]
    native_direction: tuple[int, ...]
    inactive_slots: tuple[int, ...]
    inactive_fill: int
    quantization: str
    calibration_author: str
    calibration_date: str


def load_linker_calibration(
    path: str | Path = DEFAULT_CALIBRATION_PATH,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> LinkerCalibration:
    raw = _read_object(path)
    actual = canonical_json_digest(raw, "artifact_digest")
    declared = raw.get("artifact_digest")
    if declared != actual:
        raise ValueError(f"calibration digest mismatch: declared {declared}, actual {actual}")
    schema = load_semantic_schema(schema_path)
    if raw.get("semantic_schema_id") != schema.schema_id:
        raise ValueError("calibration semantic schema ID mismatch")
    if raw.get("semantic_schema_digest") != schema.digest:
        raise ValueError("calibration semantic schema digest mismatch")
    if raw.get("hand_model") != schema.hand_model or raw.get("hand_side") != schema.hand_side:
        raise ValueError("calibration hand identity does not match semantic schema")
    if raw.get("semantic_units") != schema.units:
        raise ValueError("calibration semantic units do not match semantic schema")
    if not raw.get("calibration_author") or not raw.get("calibration_date"):
        raise ValueError("calibration author and date must be explicit")
    if raw.get("serial_number") is None and not raw.get("fleet_applicability"):
        raise ValueError("calibration needs a serial number or fleet applicability")
    if raw.get("mapping_mode") != "normalized-semantic-fraction-to-native-arc":
        raise ValueError(f"unsupported mapping mode {raw.get('mapping_mode')!r}")

    entries = raw.get("semantic_to_native")
    if not isinstance(entries, list) or len(entries) != len(schema.joints):
        raise ValueError("calibration mapping length does not match semantic schema")
    joints: list[JointCalibration] = []
    for spec, entry in zip(schema.joints, entries):
        if entry.get("name") != spec.name:
            raise ValueError(f"mapping order mismatch at {spec.name}")
        lower, upper = _number_pair(entry.get("soft_limit"), spec.name)
        if (lower, upper) != (spec.lower, spec.upper):
            raise ValueError(f"{spec.name}: calibration soft limit differs from schema")
        slot = entry.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ValueError(f"{spec.name}: slot must be an integer")
        flip = entry.get("flip")
        if not isinstance(flip, bool):
            raise ValueError(f"{spec.name}: flip must be boolean")
        offset = entry.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            raise ValueError(f"{spec.name}: offset must be numeric")
        joints.append(JointCalibration(spec.name, slot, lower, upper, flip, float(offset)))

    native = raw.get("native")
    if not isinstance(native, dict):
        raise ValueError("calibration native section is missing")
    slot_count = native.get("slot_count")
    if not isinstance(slot_count, int) or slot_count <= 0:
        raise ValueError("native slot_count must be positive")
    arc_min = tuple(float(v) for v in native.get("arc_min", []))
    arc_max = tuple(float(v) for v in native.get("arc_max", []))
    direction = tuple(int(v) for v in native.get("direction", []))
    if not (len(arc_min) == len(arc_max) == len(direction) == slot_count):
        raise ValueError("native calibration arrays must match slot_count")
    inactive = tuple(int(v) for v in native.get("inactive_slots", []))
    if any(slot < 0 or slot >= slot_count for slot in inactive):
        raise ValueError("inactive slot is out of range")
    slots = [joint.slot for joint in joints]
    if len(set(slots)) != len(slots) or any(slot in inactive for slot in slots):
        raise ValueError("semantic mapping uses a duplicate or inactive slot")
    if any(slot < 0 or slot >= slot_count for slot in slots):
        raise ValueError("semantic mapping slot is out of range")
    for slot in slots:
        if arc_max[slot] <= arc_min[slot] or direction[slot] not in (-1, 1):
            raise ValueError(f"active native slot {slot} has invalid calibration")
    quantization = native.get("quantization")
    if quantization != "round-nearest-integer":
        raise ValueError(f"unsupported quantization {quantization!r}")

    return LinkerCalibration(
        calibration_id=str(raw["calibration_id"]),
        artifact_digest=str(declared),
        hand_model=str(raw["hand_model"]),
        hand_joint=str(raw["hand_joint"]),
        hand_side=str(raw["hand_side"]),
        serial_number=(None if raw.get("serial_number") is None else str(raw["serial_number"])),
        semantic_schema_id=schema.schema_id,
        semantic_schema_digest=schema.digest,
        joints=tuple(joints),
        mapping_mode=str(raw["mapping_mode"]),
        native_arc_min=arc_min,
        native_arc_max=arc_max,
        native_direction=direction,
        inactive_slots=inactive,
        inactive_fill=int(native["inactive_fill"]),
        quantization=str(quantization),
        calibration_author=str(raw["calibration_author"]),
        calibration_date=str(raw["calibration_date"]),
    )


@dataclass(frozen=True)
class MappingPreview:
    semantic_input: tuple[float, ...]
    semantic_clamped: tuple[float, ...]
    native_arc: tuple[float, ...]
    saturated_joints: tuple[str, ...]


@dataclass(frozen=True)
class PreparedCommand:
    preview: MappingPreview
    native_range: tuple[int, ...]
    diagnostic_semantic: tuple[float, ...]
    round_trip_error: tuple[float, ...]


@dataclass(frozen=True)
class LinkerMapper:
    calibration: LinkerCalibration

    @classmethod
    def load(
        cls,
        path: str | Path = DEFAULT_CALIBRATION_PATH,
        schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    ) -> "LinkerMapper":
        return cls(load_linker_calibration(path, schema_path))

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(joint.name for joint in self.calibration.joints)

    def preview(self, semantic: Sequence[float]) -> MappingPreview:
        joints = self.calibration.joints
        if len(semantic) != len(joints):
            raise ValueError(f"expected {len(joints)} semantic joints, got {len(semantic)}")
        incoming = tuple(float(value) for value in semantic)
        if any(not math.isfinite(value) for value in incoming):
            raise ValueError("semantic target contains a non-finite value")
        arc = [0.0] * len(self.calibration.native_arc_min)
        clamped: list[float] = []
        saturated: list[str] = []
        for value, joint in zip(incoming, joints):
            safe = min(max(value, joint.lower), joint.upper)
            if safe != value:
                saturated.append(joint.name)
            clamped.append(safe)
            adjusted = min(max(safe + joint.offset, joint.lower), joint.upper)
            fraction = (adjusted - joint.lower) / (joint.upper - joint.lower)
            if joint.flip:
                fraction = 1.0 - fraction
            lo = self.calibration.native_arc_min[joint.slot]
            hi = self.calibration.native_arc_max[joint.slot]
            arc[joint.slot] = lo + fraction * (hi - lo)
        return MappingPreview(incoming, tuple(clamped), tuple(arc), tuple(saturated))

    def quantize(self, native_arc: Sequence[float]) -> tuple[int, ...]:
        count = len(self.calibration.native_arc_min)
        if len(native_arc) != count:
            raise ValueError(f"expected {count} native arc slots, got {len(native_arc)}")
        command = [self.calibration.inactive_fill] * count
        inactive = set(self.calibration.inactive_slots)
        for slot, value in enumerate(native_arc):
            if slot in inactive:
                continue
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"native arc slot {slot} is not finite")
            lo = self.calibration.native_arc_min[slot]
            hi = self.calibration.native_arc_max[slot]
            if hi <= lo:
                raise ValueError(f"native arc slot {slot} has no active range")
            value = min(max(value, lo), hi)
            fraction = (value - lo) / (hi - lo)
            native = 255.0 * (1.0 - fraction) if self.calibration.native_direction[slot] == -1 else 255.0 * fraction
            command[slot] = int(round(min(max(native, 0.0), 255.0)))
        return tuple(command)

    def inverse(self, native_range: Sequence[float]) -> tuple[float, ...]:
        count = len(self.calibration.native_arc_min)
        if len(native_range) != count:
            raise ValueError(f"expected {count} native range slots, got {len(native_range)}")
        result: list[float] = []
        for joint in self.calibration.joints:
            value = float(native_range[joint.slot])
            if not math.isfinite(value):
                raise ValueError(f"native range slot {joint.slot} is not finite")
            value = min(max(value, 0.0), 255.0)
            fraction = value / 255.0
            if self.calibration.native_direction[joint.slot] == -1:
                fraction = 1.0 - fraction
            if joint.flip:
                fraction = 1.0 - fraction
            semantic = joint.lower + fraction * (joint.upper - joint.lower) - joint.offset
            result.append(min(max(semantic, joint.lower), joint.upper))
        return tuple(result)

    def prepare(self, semantic: Sequence[float]) -> PreparedCommand:
        preview = self.preview(semantic)
        command = self.quantize(preview.native_arc)
        inverse = self.inverse(command)
        error = tuple(
            observed - expected
            for observed, expected in zip(inverse, preview.semantic_clamped)
        )
        return PreparedCommand(preview, command, inverse, error)


def load_default_mapper() -> LinkerMapper:
    return LinkerMapper.load()
