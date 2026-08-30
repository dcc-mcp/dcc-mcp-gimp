"""Public CLI orchestration for the GIMP Install SOP v1 lifecycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import dcc_mcp_core

from .__version__ import __version__
from .install_contract import (
    _VERBS,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_REQUIRES_RESTART,
    EXIT_VERIFY,
    SCHEMA_VERSION,
    InstallFailure,
)
from .install_contract import (
    EXIT_ACQUIRE as EXIT_ACQUIRE,
)
from .install_contract import (
    EXIT_INSTALL as EXIT_INSTALL,
)
from .install_contract import (
    MIN_CORE_VERSION as MIN_CORE_VERSION,
)
from .install_contract import (
    RECEIPT_RELATIVE_PATH as RECEIPT_RELATIVE_PATH,
)
from .install_files import (
    _begin_replace_plugin,
    _execute_uninstall,
    install,
)
from .install_files import (
    _files_manifest as _files_manifest,
)
from .install_files import (
    _manifest_digest as _manifest_digest,
)
from .install_files import (
    _write_json_atomic as _write_json_atomic,
)
from .install_host import default_plugin_dir as default_plugin_dir
from .install_lifecycle import _next_steps, doctor, plan, verify_install


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and verify DCC-MCP GIMP")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    for verb in sorted(_VERBS):
        command = subparsers.add_parser(verb)
        command.add_argument("--dcc-path", type=Path)
        command.add_argument("--python", type=Path)
        command.add_argument("--destination", type=Path, help=argparse.SUPPRESS)
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("--yes", action="store_true")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--ready-timeout", type=float, default=0.0, help=argparse.SUPPRESS)
        command.add_argument("--instance-id", help=argparse.SUPPRESS)
        command.add_argument("--host-pid", type=int, help=argparse.SUPPRESS)
    return parser


def _failure_verification(failure: InstallFailure) -> dict[str, Any]:
    return {
        "directly_usable": False,
        "failure_stage": failure.stage,
        "failure_reason": failure.reason,
    }


def run(argv: Sequence[str]) -> tuple[dict[str, Any], int, bool]:
    args = _parser().parse_args(list(argv))
    try:
        report = plan(
            args.verb,
            args.destination,
            args.python,
            args.dcc_path,
            instance_id=args.instance_id,
            host_pid=args.host_pid,
        )
    except InstallFailure as failure:
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "status": "failed",
                "dcc_type": "gimp",
                "verb": args.verb,
                "adapter_version": __version__,
                "core_version": str(getattr(dcc_mcp_core, "__version__", "unknown")),
                "steps": [{"id": failure.stage, "status": "failed", "reason": failure.reason}],
                "next_steps": [],
                "receipt_path": None,
                "verify": _failure_verification(failure),
            },
            failure.exit_code,
            args.as_json,
        )
    if args.verb == "status":
        report["status"] = (
            "ok" if report["installation_state"] in {"fresh", "current"} else "partial"
        )
        report["steps"][-1]["status"] = report["status"]
        if report["status"] == "partial":
            report["verify"] = {
                "directly_usable": False,
                "failure_stage": "receipt",
                "failure_reason": "GIMP installation is %s" % report["installation_state"],
            }
        code = EXIT_OK if report["status"] == "ok" else EXIT_VERIFY
        return report, code, args.as_json
    if args.verb in {"install", "upgrade"}:
        if args.dry_run or not args.yes:
            return report, EXIT_OK, args.as_json
        if args.verb == "upgrade" and report["installation_state"] == "fresh":
            failure = InstallFailure(EXIT_PREFLIGHT, "upgrade", "Nothing is installed; use install")
            report.update(status="failed", verify=_failure_verification(failure))
            return report, failure.exit_code, args.as_json
        transaction = None
        try:
            transaction = _begin_replace_plugin(Path(report["destination"]), report)
            target = transaction.target
            report["steps"][-1] = {
                "id": args.verb,
                "status": "ok",
                "path": str(target),
            }
            report["verify"] = verify_install(
                Path(report["destination"]),
                Path(report["python"]),
                args.ready_timeout,
                Path(report["dcc_path"]),
                report["gimp_version"],
                instance_id=args.instance_id,
                host_pid=args.host_pid,
            )
        except InstallFailure as failure:
            if transaction is not None and not transaction.closed:
                transaction.rollback()
            report["status"] = (
                "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed"
            )
            report["verify"] = _failure_verification(failure)
            return report, failure.exit_code, args.as_json
        if report["verify"]["directly_usable"]:
            try:
                transaction.commit()
            except InstallFailure as failure:
                report["status"] = (
                    "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed"
                )
                report["verify"] = _failure_verification(failure)
                return report, failure.exit_code, args.as_json
            report["status"] = "ok"
            return report, EXIT_OK, args.as_json
        report["next_steps"] = _next_steps(report)
        if transaction.previous_moved:
            transaction.rollback()
            report["status"] = "failed"
            report["previous_install_restored"] = True
            return report, EXIT_VERIFY, args.as_json
        if report["verify"]["failure_stage"] in {"readiness", "readiness_identity"}:
            try:
                transaction.commit()
            except InstallFailure as failure:
                report["status"] = (
                    "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed"
                )
                report["verify"] = _failure_verification(failure)
                return report, failure.exit_code, args.as_json
            report["status"] = "requires_restart"
            return report, EXIT_REQUIRES_RESTART, args.as_json
        transaction.rollback()
        report["status"] = "failed"
        return report, EXIT_VERIFY, args.as_json
    if args.verb == "uninstall":
        if args.dry_run or not args.yes:
            return report, EXIT_OK, args.as_json
        try:
            report, code = _execute_uninstall(report)
        except InstallFailure as failure:
            report["status"] = (
                "requires_restart" if failure.exit_code == EXIT_REQUIRES_RESTART else "failed"
            )
            report["verify"] = _failure_verification(failure)
            return report, failure.exit_code, args.as_json
        return report, code, args.as_json
    if args.verb == "verify":
        report["verify"] = verify_install(
            Path(report["destination"]),
            Path(report["python"]),
            args.ready_timeout,
            Path(report["dcc_path"]),
            report["gimp_version"],
            instance_id=args.instance_id,
            host_pid=args.host_pid,
        )
        report["status"] = "ok" if report["verify"]["directly_usable"] else "failed"
        if report["status"] == "failed":
            report["next_steps"] = _next_steps(report)
        code = EXIT_OK if report["status"] == "ok" else EXIT_VERIFY
        return report, code, args.as_json
    return report, EXIT_OK, args.as_json


def _print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return
    print("DCC-MCP GIMP %s: %s" % (report.get("verb"), report.get("status")))
    if report.get("destination"):
        print("Profile: %s" % report["destination"])
    verification = report.get("verify") or {}
    if verification.get("failure_reason"):
        print("Verification: %s" % verification["failure_reason"])
    for step in report.get("next_steps", []):
        print("Next: %s" % step["description"])


def main(argv: Optional[Sequence[str]] = None) -> None:
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] in _VERBS:
        report, code, as_json = run(resolved)
        _print_report(report, as_json)
        if code:
            raise SystemExit(code)
        return
    parser = argparse.ArgumentParser(description="Install or inspect the DCC-MCP GIMP plug-in")
    parser.add_argument("--destination", type=Path, help="Override the GIMP plug-ins directory")
    parser.add_argument("--doctor", action="store_true", help="Print installation status as JSON")
    args = parser.parse_args(resolved)
    if args.doctor:
        result = doctor(args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ready"]:
            raise SystemExit(1)
        return
    target = install(args.destination)
    print("Installed DCC-MCP GIMP plug-in to %s; restart GIMP and run the bridge." % target)


def doctor_main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the DCC-MCP GIMP plug-in installation")
    parser.add_argument("--destination", type=Path, help="Override the GIMP plug-ins directory")
    result = doctor(parser.parse_args().destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ready"]:
        raise SystemExit(1)
