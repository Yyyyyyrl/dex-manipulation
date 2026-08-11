#!/usr/bin/env python3
"""Reproducible hardware-free soak for the live control console.

The child command is deliberately fixed to fake transport, synthetic policy,
fake OpenXR, fake D435, and fake arm telemetry. It never calls switch/stop and
cannot be configured to open CAN, OpenXR, camera, or Hitbot hardware.
"""

from __future__ import annotations

import argparse
import http.client
import json
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from pathlib import Path

SOURCE_NAMES = ("runtime", "openxr", "linker", "hitbot", "d435")


def build_fake_command(
    python: str,
    repo_root: Path,
    *,
    http_port: int,
    openxr_port: int,
    arm_port: int,
) -> list[str]:
    return [
        python,
        str(repo_root / "tools" / "switch_web_demo.py"),
        "--transport",
        "fake",
        "--policy",
        "synthetic",
        "--vr",
        "fake",
        "--vr-python",
        python,
        "--arm-telemetry",
        "fake",
        "--camera",
        "fake",
        "--host",
        "127.0.0.1",
        "--port",
        str(http_port),
        "--vr-udp-port",
        str(openxr_port),
        "--arm-udp-port",
        str(arm_port),
    ]


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    p95_index = round(0.95 * (len(ordered) - 1))
    return {
        "count": len(values),
        "mean_ms": round(statistics.mean(values), 6),
        "p95_ms": round(ordered[p95_index], 6),
        "max_ms": round(max(values), 6),
    }


def _free_port(kind: int) -> int:
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _snapshot(url: str, timeout_s: float = 2.0) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        if response.status != 200:
            raise RuntimeError(f"snapshot returned HTTP {response.status}")
        return json.load(response)


def _rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError(f"VmRSS is unavailable for PID {pid}")


def _history_lengths(snapshot: dict[str, object]) -> dict[str, int]:
    sources = snapshot.get("sources", {})
    return {
        name: len(sources.get(name, {}).get("payload", {}).get("latency_history_ms", []))
        for name in ("openxr", "linker", "hitbot")
    }


def _ready(snapshot: dict[str, object]) -> bool:
    sources = snapshot.get("sources", {})
    if set(sources) != set(SOURCE_NAMES):
        return False
    if any(sources[name].get("health") != "healthy" for name in SOURCE_NAMES):
        return False
    runtime = sources["runtime"].get("payload", {})
    if runtime.get("readiness_ready") is not True:
        return False
    return all(length == 200 for length in _history_lengths(snapshot).values())


class RefreshViewer:
    def __init__(self, url: str, stop: threading.Event) -> None:
        self.url = url
        self.stop = stop
        self.requests = 0
        self.errors: list[str] = []
        self.last_revision = -1
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout_s: float = 3.0) -> None:
        self.thread.join(timeout_s)
        if self.thread.is_alive():
            raise TimeoutError("refresh viewer did not stop")

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                snapshot = _snapshot(self.url, 1.0)
                revision = int(snapshot["revision"])
                if revision < self.last_revision:
                    raise RuntimeError("snapshot revision moved backwards")
                if set(snapshot.get("sources", {})) != set(SOURCE_NAMES):
                    raise RuntimeError("snapshot did not contain all four sources")
                self.last_revision = revision
                self.requests += 1
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                if not self.stop.is_set():
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            self.stop.wait(0.05)


class SlowSSEViewer:
    """Open one SSE response and intentionally never consume its body."""

    def __init__(self, port: int) -> None:
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
        self.connection.request("GET", "/api/live")
        response = self.connection.getresponse()
        if response.status != 200:
            self.connection.close()
            raise RuntimeError(f"slow SSE viewer returned HTTP {response.status}")
        self.response = response

    def close(self) -> None:
        self.connection.close()


def _drain_output(
    process: subprocess.Popen[str],
    recent: deque[str],
) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        recent.append(line.rstrip())


def _wait_until(
    predicate: Callable[[dict[str, object]], bool],
    snapshot_url: str,
    process: subprocess.Popen[str],
    *,
    timeout_s: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last_error = "no snapshot received"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"fake console exited early with code {process.returncode}")
        try:
            snapshot = _snapshot(snapshot_url)
            if predicate(snapshot):
                return snapshot
            last_error = "snapshot has not reached the required ready state"
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise TimeoutError(f"fake console readiness timed out: {last_error}")


def _trace_timing(
    trace_path: Path,
    *,
    viewer_start_ns: int,
    viewer_end_ns: int,
) -> dict[str, dict[str, float | int | None]]:
    baseline: list[float] = []
    viewers: list[float] = []
    with trace_path.open() as handle:
        for line in handle:
            record = json.loads(line)
            scheduler = record["payload"]["scheduler"]
            actual_ns = int(scheduler["actual_time_ns"])
            lateness_ms = int(scheduler["lateness_ns"]) / 1_000_000
            if actual_ns < viewer_start_ns:
                baseline.append(lateness_ms)
            elif actual_ns <= viewer_end_ns:
                viewers.append(lateness_ms)
    return {"no_viewer": summarize(baseline), "viewers_and_slow_sse": summarize(viewers)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-s", type=float, default=300.0)
    parser.add_argument("--viewer-count", type=int, default=2)
    parser.add_argument("--max-rss-growth-mib", type=float, default=32.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.duration_s < 30.0:
        parser.error("--duration-s must be at least 30 seconds")
    if not 1 <= args.viewer_count <= 8:
        parser.error("--viewer-count must be within 1..8")
    if args.max_rss_growth_mib <= 0:
        parser.error("--max-rss-growth-mib must be positive")
    return args


def run(args: argparse.Namespace) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    http_port = _free_port(socket.SOCK_STREAM)
    openxr_port = _free_port(socket.SOCK_DGRAM)
    arm_port = _free_port(socket.SOCK_DGRAM)
    command = build_fake_command(
        sys.executable,
        repo_root,
        http_port=http_port,
        openxr_port=openxr_port,
        arm_port=arm_port,
    )
    recent_output: deque[str] = deque(maxlen=100)
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    output_thread = threading.Thread(
        target=_drain_output,
        args=(process, recent_output),
        daemon=True,
    )
    output_thread.start()
    viewer_stop = threading.Event()
    viewers: list[RefreshViewer] = []
    slow_viewer: SlowSSEViewer | None = None
    snapshot_url = f"http://127.0.0.1:{http_port}/api/snapshot"
    started_ns = time.monotonic_ns()
    try:
        ready_snapshot = _wait_until(_ready, snapshot_url, process, timeout_s=45.0)
        logs_path = Path(ready_snapshot["sources"]["runtime"]["payload"]["logs_path"])
        initial_rss_kib = _rss_kib(process.pid)
        rss_samples_kib = [initial_rss_kib]
        baseline_s = min(30.0, max(10.0, args.duration_s * 0.2))
        soak_start = time.monotonic()
        viewer_start_ns = 0
        viewer_end_ns = 0
        next_report = soak_start
        unhealthy_samples: list[dict[str, object]] = []
        last_revision = int(ready_snapshot["revision"])
        while time.monotonic() - soak_start < args.duration_s:
            elapsed_s = time.monotonic() - soak_start
            if viewer_start_ns == 0 and elapsed_s >= baseline_s:
                viewer_start_ns = time.monotonic_ns()
                slow_viewer = SlowSSEViewer(http_port)
                viewers = [RefreshViewer(snapshot_url, viewer_stop) for _ in range(args.viewer_count)]
                for viewer in viewers:
                    viewer.start()
            snapshot = _snapshot(snapshot_url)
            last_revision = int(snapshot["revision"])
            rss_samples_kib.append(_rss_kib(process.pid))
            health = {
                name: snapshot["sources"][name]["health"]
                for name in SOURCE_NAMES
            }
            if any(value != "healthy" for value in health.values()):
                unhealthy_samples.append({"elapsed_s": round(elapsed_s, 3), "health": health})
            if time.monotonic() >= next_report:
                print(
                    f"[soak] {elapsed_s:6.1f}/{args.duration_s:.1f}s "
                    f"rss={rss_samples_kib[-1]} KiB revision={last_revision} "
                    f"health={','.join(health.values())}",
                    flush=True,
                )
                next_report = time.monotonic() + 15.0
            time.sleep(1.0)
        viewer_end_ns = time.monotonic_ns()
        viewer_stop.set()
        for viewer in viewers:
            viewer.join()
        if slow_viewer is not None:
            slow_viewer.close()
            slow_viewer = None
        time.sleep(2.0)
        final_snapshot = _snapshot(snapshot_url)
        final_rss_kib = _rss_kib(process.pid)
        rss_samples_kib.append(final_rss_kib)
        sources = final_snapshot["sources"]
        openxr = sources["openxr"]["payload"]
        linker = sources["linker"]["payload"]
        report: dict[str, object] = {
            "schema_version": 1,
            "mode": "hardware-free-console-soak",
            "duration_s": args.duration_s,
            "viewer_count": args.viewer_count,
            "slow_sse_viewers": 1,
            "ports": {"http": http_port, "openxr_udp": openxr_port, "arm_udp": arm_port},
            "process": {
                "pid": process.pid,
                "initial_rss_kib": initial_rss_kib,
                "final_rss_kib": final_rss_kib,
                "maximum_rss_kib": max(rss_samples_kib),
                "rss_growth_kib": max(rss_samples_kib) - initial_rss_kib,
            },
            "snapshot": {
                "initial_revision": int(ready_snapshot["revision"]),
                "final_revision": int(final_snapshot["revision"]),
                "health": {name: sources[name]["health"] for name in SOURCE_NAMES},
                "unhealthy_samples": unhealthy_samples,
                "history_lengths": _history_lengths(final_snapshot),
                "openxr_sequence": openxr.get("source_sequence"),
                "candidate_source_sequence": openxr.get("candidate_source_sequence"),
                "linker_control_sample_sequence": linker.get("control_sample_sequence"),
                "linker_candidate_source_sequence": linker.get("candidate_source_sequence"),
                "openxr_drives_current_command": openxr.get("drives_current_command"),
                "command_identity_match": linker.get("command_identity_match"),
            },
            "viewers": {
                "refresh_requests": [viewer.requests for viewer in viewers],
                "errors": [error for viewer in viewers for error in viewer.errors],
            },
            "timing": _trace_timing(
                logs_path / "trace.jsonl",
                viewer_start_ns=viewer_start_ns,
                viewer_end_ns=viewer_end_ns,
            ),
            "started_monotonic_ns": started_ns,
            "finished_monotonic_ns": time.monotonic_ns(),
        }
        max_growth_kib = round(args.max_rss_growth_mib * 1024)
        failures = []
        if unhealthy_samples:
            failures.append(f"observed {len(unhealthy_samples)} unhealthy aggregate samples")
        if any(viewer.errors for viewer in viewers):
            failures.append("one or more refresh viewers failed")
        if any(value != 200 for value in _history_lengths(final_snapshot).values()):
            failures.append("bounded latency histories were not exactly 200 points")
        if max(rss_samples_kib) - initial_rss_kib > max_growth_kib:
            failures.append("RSS growth exceeded the configured budget")
        if not (
            openxr.get("source_sequence")
            == openxr.get("candidate_source_sequence")
            == linker.get("control_sample_sequence")
            == linker.get("candidate_source_sequence")
        ):
            failures.append("final OpenXR/Linker sample identity did not correlate")
        if openxr.get("drives_current_command") is not True:
            failures.append("final OpenXR sample did not drive the current command")
        if linker.get("command_identity_match") is not True:
            failures.append("final Linker command identity did not match")
        report["failures"] = failures
        report["passed"] = not failures
        return report
    finally:
        viewer_stop.set()
        for viewer in viewers:
            if viewer.thread.is_alive():
                viewer.thread.join(1.0)
        if slow_viewer is not None:
            slow_viewer.close()
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(10.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(3.0)
        output_thread.join(2.0)
        if process.returncode not in (0, -signal.SIGINT):
            print(
                f"[soak] child exit={process.returncode}; recent output:\n"
                + "\n".join(recent_output),
                file=sys.stderr,
            )


def main() -> int:
    args = parse_args()
    report = run(args)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
