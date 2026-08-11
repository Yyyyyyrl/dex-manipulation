#!/usr/bin/env python3
"""Capture read-only HIL evidence from an already-authorized live console.

This process only reads the loopback aggregate snapshot. It imports no hardware
SDK, opens no CAN/robot transport, and exposes no control action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request


KNOWN_SOURCES = ("openxr", "linker", "hitbot")


def snapshot_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http":
        raise ValueError("HIL observation URL must use http on loopback")
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("HIL observation is restricted to loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("HIL observation URL cannot contain credentials, query, or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("HIL observation URL must be a server base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("HIL observation URL has an invalid port") from exc
    if port is None:
        port = 80
    return f"http://{parsed.hostname}:{port}/api/snapshot"


def _payload(sources: dict[str, object], name: str) -> dict[str, object]:
    source = sources.get(name)
    if not isinstance(source, dict):
        return {}
    payload = source.get("payload")
    return payload if isinstance(payload, dict) else {}


def validate_snapshot(
    snapshot: dict[str, object],
    required_sources: tuple[str, ...],
) -> list[str]:
    issues: list[str] = []
    sources = snapshot.get("sources")
    if not isinstance(sources, dict):
        return ["aggregate-sources-missing"]

    runtime = sources.get("runtime")
    runtime_payload = _payload(sources, "runtime")
    if not isinstance(runtime, dict) or runtime.get("health") != "healthy":
        issues.append("runtime-not-healthy")
    if runtime_payload.get("readiness_ready") is not True:
        issues.append("runtime-readiness-not-ready")
    providers = runtime_payload.get("readiness_providers")
    if not isinstance(providers, list) or not providers:
        issues.append("runtime-readiness-evidence-missing")
    else:
        for provider in providers:
            if not isinstance(provider, dict):
                issues.append("runtime-readiness-evidence-malformed")
                continue
            if provider.get("valid") is not True or provider.get("result") not in (
                "pass",
                "operator-confirmed",
            ):
                issues.append(f"readiness-provider-failed:{provider.get('provider_id', 'unknown')}")

    for name in required_sources:
        source = sources.get(name)
        if not isinstance(source, dict):
            issues.append(f"source-missing:{name}")
        elif source.get("health") != "healthy":
            issues.append(f"source-not-healthy:{name}:{source.get('health', 'missing')}")

    openxr = _payload(sources, "openxr")
    if "openxr" in required_sources:
        if str(openxr.get("mode", "")).lower() != "real":
            issues.append(f"openxr-not-real:{openxr.get('mode', 'missing')}")
        if openxr.get("layout") != "openxr-hand-26-v1":
            issues.append("openxr-layout-mismatch")
        if openxr.get("side") != "left":
            issues.append("openxr-side-mismatch")
        nodes = openxr.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 26:
            issues.append("openxr-node-count-mismatch")
        if openxr.get("valid_joint_count") != 26:
            issues.append("openxr-valid-joint-count-mismatch")
        if openxr.get("session_focused") is not True:
            issues.append("openxr-session-not-focused")
        if openxr.get("control_correlated") is not True:
            issues.append("openxr-candidate-not-correlated")
        if openxr.get("source_sequence") != openxr.get("candidate_source_sequence"):
            issues.append("openxr-candidate-sequence-mismatch")

    linker = _payload(sources, "linker")
    if "linker" in required_sources:
        joints = linker.get("joints")
        if not isinstance(joints, list) or len(joints) != 16:
            issues.append("linker-joint-count-mismatch")
        if linker.get("epoch_match") is not True:
            issues.append("linker-epoch-mismatch")
        if linker.get("acknowledgement_missing") is True:
            issues.append("linker-acknowledgement-missing")
        if linker.get("command_identity_match") is not True:
            issues.append("linker-command-identity-mismatch")
        if not isinstance(linker.get("gateway_rate_hz"), (int, float)):
            issues.append("linker-gateway-rate-missing")

    if "openxr" in required_sources and "linker" in required_sources:
        sequences = (
            openxr.get("source_sequence"),
            openxr.get("candidate_source_sequence"),
            linker.get("control_sample_sequence"),
            linker.get("candidate_source_sequence"),
        )
        if any(value is None for value in sequences) or len(set(sequences)) != 1:
            issues.append("openxr-linker-control-sequence-mismatch")

    hitbot = _payload(sources, "hitbot")
    if "hitbot" in required_sources:
        if str(hitbot.get("mode", "")).lower() != "live":
            issues.append(f"hitbot-not-live:{hitbot.get('mode', 'missing')}")
        if hitbot.get("connected") is not True:
            issues.append("hitbot-not-connected")
        if hitbot.get("cycle_success") is not True:
            issues.append("hitbot-cycle-failed")
        tracker = hitbot.get("tracker_pose")
        if not isinstance(tracker, list) or len(tracker) != 7:
            issues.append("hitbot-tracker-pose-missing")
        for field in ("tcp_actual", "tcp_target", "ik_result"):
            value = hitbot.get(field)
            if not isinstance(value, list) or len(value) != 6:
                issues.append(f"hitbot-{field.replace('_', '-')}-missing")
    return issues


def _get_snapshot(url: str, timeout_s: float) -> dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        if response.status != 200:
            raise RuntimeError(f"snapshot returned HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("snapshot response is not an object")
    return payload


def _number_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p95": None, "maximum": None}
    ordered = sorted(values)
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 6),
        "p95": round(ordered[round(0.95 * (len(ordered) - 1))], 6),
        "maximum": round(max(values), 6),
    }


def observe(args: argparse.Namespace) -> dict[str, object]:
    url = snapshot_url(args.url)
    required = tuple(args.require)
    startup_deadline = time.monotonic() + args.startup_timeout_s
    last_issues = ["no-snapshot"]
    while time.monotonic() < startup_deadline:
        try:
            snapshot = _get_snapshot(url, args.request_timeout_s)
            last_issues = validate_snapshot(snapshot, required)
            if not last_issues:
                break
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            last_issues = [f"snapshot-read-failed:{type(exc).__name__}:{exc}"]
        time.sleep(0.25)
    else:
        raise TimeoutError("HIL sources did not become valid: " + ",".join(last_issues))

    started_ns = time.monotonic_ns()
    deadline = time.monotonic() + args.duration_s
    interval_s = 1.0 / args.sample_hz
    next_sample = time.monotonic()
    revisions: list[int] = []
    ages: dict[str, list[float]] = {name: [] for name in required}
    failure_samples: list[dict[str, object]] = []
    runtime_states: set[str] = set()
    last_snapshot = snapshot
    samples = 0
    while time.monotonic() < deadline:
        try:
            current = _get_snapshot(url, args.request_timeout_s)
            issues = validate_snapshot(current, required)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            current = {}
            issues = [f"snapshot-read-failed:{type(exc).__name__}:{exc}"]
        if current:
            revision = int(current.get("revision", -1))
            if revisions and revision < revisions[-1]:
                issues.append("aggregate-revision-moved-backwards")
            revisions.append(revision)
            sources = current.get("sources", {})
            runtime_states.add(str(_payload(sources, "runtime").get("state", "missing")))
            for name in required:
                age = sources.get(name, {}).get("age_ms")
                if isinstance(age, (int, float)):
                    ages[name].append(float(age))
            last_snapshot = current
        if issues:
            failure_samples.append(
                {
                    "sample": samples,
                    "monotonic_ns": time.monotonic_ns(),
                    "issues": issues,
                }
            )
        samples += 1
        next_sample += interval_s
        time.sleep(max(0.0, next_sample - time.monotonic()))

    sources = last_snapshot["sources"]
    runtime = _payload(sources, "runtime")
    openxr = _payload(sources, "openxr")
    linker = _payload(sources, "linker")
    hitbot = _payload(sources, "hitbot")
    report: dict[str, object] = {
        "schema_version": 1,
        "mode": "read-only-live-console-observation",
        "url": url,
        "required_sources": list(required),
        "duration_s": args.duration_s,
        "sample_hz": args.sample_hz,
        "samples": samples,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": time.monotonic_ns(),
        "revision": {
            "first": revisions[0] if revisions else None,
            "last": revisions[-1] if revisions else None,
        },
        "age_ms": {name: _number_summary(values) for name, values in ages.items()},
        "runtime_states": sorted(runtime_states),
        "readiness_providers": runtime.get("readiness_providers", []),
        "final_identity": {
            "openxr_sequence": openxr.get("source_sequence"),
            "openxr_candidate_source_sequence": openxr.get("candidate_source_sequence"),
            "linker_control_sample_sequence": linker.get("control_sample_sequence"),
            "linker_candidate_source_sequence": linker.get("candidate_source_sequence"),
            "linker_authorized_command_id": linker.get("authorized_command_id"),
            "linker_acknowledged_command_id": linker.get("acknowledged_command_id"),
            "linker_effective_command_id": linker.get("effective_command_id"),
            "linker_control_epoch": linker.get("control_epoch"),
            "linker_state_epoch": linker.get("state_epoch"),
            "hitbot_sequence": hitbot.get("source_sequence"),
        },
        "failure_samples": failure_samples,
        "passed": samples > 0 and not failure_samples,
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--sample-hz", type=float, default=5.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    parser.add_argument("--request-timeout-s", type=float, default=2.0)
    parser.add_argument(
        "--require",
        choices=KNOWN_SOURCES,
        nargs="+",
        default=list(KNOWN_SOURCES),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.require = list(dict.fromkeys(args.require))
    if args.duration_s < 10.0:
        parser.error("--duration-s must be at least 10 seconds")
    if not 1.0 <= args.sample_hz <= 20.0:
        parser.error("--sample-hz must be within 1..20")
    if min(args.startup_timeout_s, args.request_timeout_s) <= 0:
        parser.error("timeouts must be positive")
    try:
        snapshot_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()
    report = observe(args)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
