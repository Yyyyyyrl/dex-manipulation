"""Command-line entrypoint for preflight, inspection, and the hand-only runtime."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path

from dex_contracts import to_primitive

from .deployment import DeploymentBinding
from .policy_package import PolicyCompatibilityContext, PolicyRegistry, validate_policy_package
from .preflight import preflight_deployment


def _print(value) -> None:
    print(json.dumps(to_primitive(value), sort_keys=True, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dex-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("config")
    run = subparsers.add_parser("run")
    run.add_argument("config")
    listing = subparsers.add_parser("list-policies")
    listing.add_argument("config")
    verify = subparsers.add_parser("verify-package")
    verify.add_argument("package")
    verify.add_argument(
        "--allow-unsigned-local",
        action="store_true",
        help="explicitly trust unsigned packages from a local immutable store",
    )
    return parser


def _confirm_when_connected(application) -> None:
    if not application.wait_until_connected(30.0):
        return
    descriptor = application.preflight.policy_package.descriptor
    try:
        operator_id = input("Operator ID: ").strip()
        token = input(
            f"Type CONFIRM to arm {descriptor.display_name} ({descriptor.package_id}): "
        ).strip()
    except EOFError:
        return
    if operator_id and token == "CONFIRM":
        application.confirm_operator(operator_id)
    else:
        print("Operator confirmation not recorded; teleoperation remains owner.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify-package":
        package = validate_policy_package(
            Path(args.package),
            allow_unsigned_local=args.allow_unsigned_local,
        )
        _print(package.descriptor)
        return 0
    if args.command == "preflight":
        result = preflight_deployment(args.config)
        _print(result.report)
        return 0
    if args.command == "run":
        from .composition import build_hand_only_runtime

        preflight = preflight_deployment(args.config)
        _print(preflight.report)
        application = build_hand_only_runtime(preflight)
        confirmer = threading.Thread(
            target=_confirm_when_connected,
            args=(application,),
            name="operator-confirmation",
            daemon=True,
        )
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
        }
        for signum in previous_handlers:
            signal.signal(signum, lambda *_args: application.request_stop())
        confirmer.start()
        try:
            result = application.run()
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
        _print(result)
        return result.exit_code
    if args.command == "list-policies":
        binding = DeploymentBinding.load(args.config)
        result = preflight_deployment(args.config)
        snapshot = PolicyRegistry(
            binding.policies.stores,
            allow_unsigned_local=binding.policies.allow_unsigned_local,
        ).scan(
            # Preflight already proved the selected package context; reuse its
            # selected package by asking the registry through the same command.
            # The descriptor output remains non-actuating.
            PolicyCompatibilityContext(
                runtime_api_version="1.0",
                protocol_version=binding.protocol_version,
                hand_model=result.mapper.calibration.hand_model,
                hand_side=result.mapper.calibration.hand_side,
                semantic_schema_id=result.mapper.calibration.semantic_schema_id,
                semantic_schema_digest=result.mapper.calibration.semantic_schema_digest,
                calibration_id=result.mapper.calibration.calibration_id,
                calibration_digest=result.mapper.calibration.artifact_digest,
                control_period_ns=None,
                acknowledgement_levels=("sent-to-bus",),
            )
        )
        _print(snapshot)
        return 0
    raise RuntimeError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
