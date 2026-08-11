"""Isolated D435 capture worker using the host's known-good camera Python."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from multiprocessing.connection import Connection


def _watch_parent(stop: threading.Event) -> None:
    """Stop cleanly when the parent closes this worker's stdin pipe."""
    try:
        sys.stdin.buffer.read(1)
    finally:
        stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    output = Connection(sys.stdout.fileno(), readable=False, writable=True)
    stop = threading.Event()
    threading.Thread(target=_watch_parent, args=(stop,), daemon=True).start()
    pipeline = None
    started = False
    try:
        import cv2
        import numpy as np
        import pyrealsense2 as rs

        pipeline = rs.pipeline()
        config = rs.config()
        if args.serial:
            config.enable_device(args.serial)
        # Match dex-forge/tools/realsense_capture.py, the verified host path.
        config.enable_stream(
            rs.stream.color,
            args.width,
            args.height,
            rs.format.bgr8,
            args.fps,
        )
        config.enable_stream(
            rs.stream.depth,
            args.width,
            args.height,
            rs.format.z16,
            args.fps,
        )
        profile = pipeline.start(config)
        started = True
        device = profile.get_device()
        model = device.get_info(rs.camera_info.name)
        serial = device.get_info(rs.camera_info.serial_number)
        if "D435" not in model.upper():
            raise RuntimeError(f"selected RealSense is not a D435 family device: {model}")
        align = rs.align(rs.stream.color)

        # Match the known-good capture utility's auto-exposure warmup.
        for _ in range(30):
            if stop.is_set():
                return 0
            pipeline.wait_for_frames(5000)

        # Keep the same default RGB-control path and second warmup used by the
        # host utility that has been verified with this exact D435.
        color_sensor = next(
            sensor
            for sensor in device.sensors
            if sensor.supports(rs.option.exposure)
            and sensor.supports(rs.option.gain)
            and sensor.supports(rs.option.enable_auto_exposure)
            and sensor.supports(rs.option.white_balance)
            and sensor.supports(rs.option.enable_auto_white_balance)
        )
        for _ in range(45):
            if stop.is_set():
                return 0
            pipeline.wait_for_frames(5000)
        color_sensor.get_option(rs.option.enable_auto_exposure)
        color_sensor.get_option(rs.option.exposure)
        color_sensor.get_option(rs.option.gain)
        color_sensor.get_option(rs.option.enable_auto_white_balance)
        color_sensor.get_option(rs.option.white_balance)

        while not stop.is_set():
            frames = align.process(pipeline.wait_for_frames(1000))
            color_frame = frames.get_color_frame()
            depth_frame = frames.get_depth_frame()
            if not color_frame or not depth_frame:
                continue
            color = np.asanyarray(color_frame.get_data())
            depth_raw = np.asanyarray(depth_frame.get_data())
            depth = cv2.applyColorMap(
                cv2.convertScaleAbs(depth_raw, alpha=0.03),
                cv2.COLORMAP_JET,
            )
            rgb_ok, rgb_encoded = cv2.imencode(
                ".jpg", color, [cv2.IMWRITE_JPEG_QUALITY, 88]
            )
            depth_ok, depth_encoded = cv2.imencode(
                ".jpg", depth, [cv2.IMWRITE_JPEG_QUALITY, 84]
            )
            if not rgb_ok or not depth_ok:
                raise RuntimeError("OpenCV failed to encode a D435 frame")
            output.send(
                (
                    "frame",
                    model,
                    serial,
                    rgb_encoded.tobytes(),
                    depth_encoded.tobytes(),
                    (int(color.shape[1]), int(color.shape[0])),
                    (int(depth.shape[1]), int(depth.shape[0])),
                    time.monotonic_ns(),
                )
            )
    except BaseException as exc:
        try:
            output.send(("fault", f"{type(exc).__name__}: {exc}"))
        except BaseException:
            pass
        return 1
    finally:
        if started and pipeline is not None:
            try:
                pipeline.stop()
            except BaseException:
                pass
        output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
