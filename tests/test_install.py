import hashlib
import importlib.resources
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from dcc_mcp_gimp.__version__ import __version__
from dcc_mcp_gimp.install import doctor, install


def _target_metadata(python_path):
    repository = Path(__file__).resolve().parents[1]
    return {
        "python_executable": str(Path(python_path).resolve()),
        "core": {
            "version": "0.19.91",
            "module_version": "0.19.91",
            "module_path": str(repository / "tests" / "core-origin.py"),
            "owned": True,
        },
        "adapter": {
            "version": __version__,
            "module_version": __version__,
            "module_path": str(repository / "src" / "dcc_mcp_gimp" / "__init__.py"),
            "owned": True,
        },
    }


@pytest.fixture
def lifecycle_env(tmp_path, monkeypatch):
    host = tmp_path / "gimp-3.0.exe"
    host.write_bytes(b"gimp")
    plugins = tmp_path / "profile" / "plug-ins"
    adapter_module = tmp_path / "site-packages" / "dcc_mcp_gimp" / "__init__.py"
    core_module = tmp_path / "site-packages" / "dcc_mcp_core" / "__init__.py"
    adapter_module.parent.mkdir(parents=True)
    core_module.parent.mkdir(parents=True)
    adapter_module.write_text("# adapter\n", encoding="utf-8")
    core_module.write_text("# core\n", encoding="utf-8")

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        else:
            payload = json.dumps(_target_metadata(sys.executable))
            parsed = json.loads(payload)
            parsed["core"]["module_path"] = str(core_module.resolve())
            parsed["adapter"]["module_path"] = str(adapter_module.resolve())
            payload = json.dumps(parsed)
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("dcc_mcp_gimp.install_host.subprocess.run", fake_run)
    entry = {
        "instance_id": "gimp-test-4242",
        "adapter_version": __version__,
        "mcp_url": "http://127.0.0.1:18812/mcp",
    }
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.query_runtime_state",
        lambda *_args, **_kwargs: {"entries": [entry]},
    )
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {
            "success": True,
            "entry": entry,
            "probe": {
                "result": {
                    "structuredContent": {
                        "success": True,
                        "context": {
                            "ready": True,
                            "authenticated": True,
                            "gimp_pid": 4242,
                            "plugin_pid": 4343,
                            "gimp_version": "3.0.8",
                            "adapter_version": __version__,
                            "bridge_host": "127.0.0.1",
                            "bridge_port": 3848,
                            "plugin_module_path": str(
                                (plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py").resolve()
                            ),
                        },
                    }
                }
            },
        },
    )
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle._process_executable_path", lambda _pid: host.resolve()
    )
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle._process_start_identity",
        lambda _pid: "test-start-4242",
    )
    return SimpleNamespace(
        root=tmp_path,
        host=host,
        plugins=plugins,
        common=[
            "--json",
            "--dcc-path",
            str(host),
            "--python",
            sys.executable,
            "--destination",
            str(plugins),
        ],
    )


def test_default_plugin_dir_uses_gimp_application_support_on_macos(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import default_plugin_dir

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert default_plugin_dir() == (tmp_path / "Library/Application Support/GIMP/3.0/plug-ins")


def test_install_and_doctor_use_canonical_plugin_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("DCC_MCP_GIMP_ALLOWED_ROOTS", str(tmp_path))
    target = install(tmp_path / "plugins")
    script = target / "dcc_mcp_gimp.py"
    assert target.name == "dcc_mcp_gimp"
    assert script.is_file()
    if os.name != "nt":
        assert script.stat().st_mode & 0o100
    result = doctor(tmp_path / "plugins")
    assert result["ready"] is True
    assert result["file_access_enabled"] is True
    assert result["allowed_roots"] == [str(tmp_path.resolve())]


def test_install_replaces_existing_plugin_without_leaving_backup(tmp_path):
    root = tmp_path / "plugins"
    old = root / "dcc_mcp_gimp"
    old.mkdir(parents=True)
    (old / "old.txt").write_text("old", encoding="utf-8")
    target = install(root)
    assert not (target / "old.txt").exists()
    assert (target / "dcc_mcp_gimp.py").is_file()
    assert not list(root.glob(".dcc_mcp_gimp.backup-*"))


def test_doctor_rejects_a_stale_installed_plugin_version(tmp_path):
    target = install(tmp_path / "plugins")
    script = target / "dcc_mcp_gimp.py"
    source = script.read_text(encoding="utf-8")
    script.write_text(
        source.replace(
            'VERSION = "%s"' % __version__,
            'VERSION = "0.0.0"',
            1,
        ),
        encoding="utf-8",
    )

    result = doctor(tmp_path / "plugins")

    assert result["ready"] is False
    assert result["installed_adapter_version"] == "0.0.0"
    assert result["expected_adapter_version"] == __version__
    assert result["version_matches"] is False


def test_status_preflight_records_gimp_profile_and_target_interpreter(lifecycle_env):
    from dcc_mcp_gimp.install import run

    report, code, as_json = run(["status", *lifecycle_env.common])

    assert code == 0
    assert as_json is True
    assert report["schema_version"] == 1
    assert report["dcc_type"] == "gimp"
    assert report["verb"] == "status"
    assert report["status"] == "ok"
    assert report["installation_state"] == "fresh"
    assert report["gimp_version"] == "3.0.8"
    assert report["dcc_path"] == str(lifecycle_env.host.resolve())
    assert report["python"] == str(Path(sys.executable).resolve())
    assert not lifecycle_env.plugins.exists()


def test_install_preflight_rejects_a_missing_host_before_touching_profile(tmp_path):
    from dcc_mcp_gimp.install import EXIT_PREFLIGHT, run

    plugins = tmp_path / "profile" / "plug-ins"
    report, code, _ = run(
        [
            "install",
            "--yes",
            "--json",
            "--dcc-path",
            str(tmp_path / "missing-gimp"),
            "--python",
            sys.executable,
            "--destination",
            str(plugins),
        ]
    )

    assert code == EXIT_PREFLIGHT == 10
    assert report["verify"]["failure_stage"] == "gimp"
    assert "not found" in report["verify"]["failure_reason"]
    assert not plugins.exists()


def test_status_marks_an_unreceipted_plugin_as_partial(lifecycle_env):
    from dcc_mcp_gimp.install import EXIT_VERIFY, run

    install(lifecycle_env.plugins)
    report, code, _ = run(["status", *lifecycle_env.common])

    assert code == EXIT_VERIFY == 40
    assert report["status"] == "partial"
    assert report["installation_state"] == "partial"


def test_install_writes_receipt_and_verifies_to_usable(lifecycle_env):
    from dcc_mcp_gimp.install import run

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert code == 0
    assert report["status"] == "ok"
    assert report["verify"]["directly_usable"] is True
    assert (lifecycle_env.plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py").is_file()
    assert receipt["adapter_version"] == __version__
    assert receipt["gimp_version"] == "3.0.8"
    assert receipt["destination"] == str(lifecycle_env.plugins.resolve())
    assert receipt["host_paths_touched"] == [
        str((lifecycle_env.plugins / "dcc_mcp_gimp").resolve())
    ]
    assert receipt["files"][0]["sha256"]


def test_uninstall_consumes_receipt_and_preserves_unrelated_profile_files(lifecycle_env):
    from dcc_mcp_gimp.install import run

    install_report, install_code, _ = run(["install", "--yes", *lifecycle_env.common])
    unrelated = lifecycle_env.plugins / "another-plugin.txt"
    unrelated.write_text("keep", encoding="utf-8")

    report, code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert install_code == 0, json.dumps(install_report, indent=2)
    assert code == 0
    assert report["status"] == "ok"
    assert report["steps"][-1]["status"] == "ok"
    assert not (lifecycle_env.plugins / "dcc_mcp_gimp").exists()
    assert not (lifecycle_env.root / ".dcc-mcp/receipts/gimp.json").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_verify_fails_closed_when_installed_plugin_differs_from_receipt(lifecycle_env):
    from dcc_mcp_gimp.install import EXIT_VERIFY, run

    install_report, install_code, _ = run(["install", "--yes", *lifecycle_env.common])
    script = lifecycle_env.plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    status_report, status_code, _ = run(["status", *lifecycle_env.common])
    report, code, _ = run(["verify", *lifecycle_env.common])

    assert install_code == 0, install_report
    assert status_code == EXIT_VERIFY
    assert status_report["installation_state"] == "repair"
    assert code == EXIT_VERIFY == 40
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "artifact"


def test_standard_entrypoint_dispatches_lifecycle_verbs_without_starting_server(tmp_path, capsys):
    from dcc_mcp_gimp.server import main

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "status",
                "--json",
                "--dcc-path",
                str(tmp_path / "missing-gimp"),
                "--python",
                sys.executable,
                "--destination",
                str(tmp_path / "plugins"),
            ]
        )

    payload = json.loads(capsys.readouterr().out)
    assert raised.value.code == 10
    assert payload["dcc_type"] == "gimp"
    assert payload["verb"] == "status"
    assert payload["verify"]["failure_stage"] == "gimp"


@pytest.mark.skipif(os.name != "nt", reason="Windows GIMP discovery contract")
def test_status_auto_detects_gimp3_under_program_files(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import run

    host = tmp_path / "GIMP 3" / "bin" / "gimp-3.0.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"gimp")

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        else:
            payload = json.dumps(_target_metadata(sys.executable))
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "x86"))
    monkeypatch.setattr("dcc_mcp_gimp.install_host.shutil.which", lambda _name: None)
    monkeypatch.setattr("dcc_mcp_gimp.install_host.subprocess.run", fake_run)

    report, code, _ = run(
        [
            "status",
            "--json",
            "--python",
            sys.executable,
            "--destination",
            str(tmp_path / "profile"),
        ]
    )

    assert code == 0
    assert report["dcc_path"] == str(host.resolve())
    assert report["gimp_version"] == "3.0.8"


def test_status_prefers_the_host_adjacent_python_when_no_override_is_given(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import run

    host = tmp_path / "GIMP 3" / "bin" / "gimp-3.0.exe"
    embedded_python = host.with_name("python.exe")
    host.parent.mkdir(parents=True)
    host.write_bytes(b"gimp")
    embedded_python.write_bytes(b"python")

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        else:
            assert Path(command[0]) == embedded_python
            payload = json.dumps(_target_metadata(embedded_python))
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.delenv("DCC_MCP_INSTALL_PYTHON", raising=False)
    monkeypatch.setattr("dcc_mcp_gimp.install_host.subprocess.run", fake_run)

    report, code, _ = run(
        [
            "status",
            "--json",
            "--dcc-path",
            str(host),
            "--destination",
            str(tmp_path / "profile"),
        ]
    )

    assert code == 0
    assert report["python"] == str(embedded_python.resolve())


def test_doctor_surfaces_captured_gimp_bootstrap_errors(tmp_path, monkeypatch):
    errors = tmp_path / "bootstrap-errors.jsonl"
    errors.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-24T00:00:00+00:00",
                "stage": "gi-import",
                "error_type": "ImportError",
                "message": "gi.repository.Gimp is unavailable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(errors))
    install(tmp_path / "plugins")

    result = doctor(tmp_path / "plugins")

    assert result["bootstrap_errors"]["path"] == str(errors.resolve())
    assert result["bootstrap_errors"]["count"] == 1
    assert result["bootstrap_errors"]["latest"]["stage"] == "gi-import"
    assert "gi.repository.Gimp" in result["bootstrap_errors"]["latest"]["message"]


def test_status_reports_a_stale_version_stamp_as_upgrade(lifecycle_env):
    from dcc_mcp_gimp.install import _files_manifest, _manifest_digest, run

    install_report, install_code, _ = run(["install", "--yes", *lifecycle_env.common])
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            'VERSION = "%s"' % __version__, 'VERSION = "0.2.9"', 1
        ),
        encoding="utf-8",
    )
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["adapter_version"] = "0.2.9"
    receipt["files"] = _files_manifest(target)
    receipt["ownership"]["files"] = receipt["files"]
    receipt["package_digest"] = _manifest_digest(receipt["files"])
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    report, code, _ = run(["status", *lifecycle_env.common])

    assert install_code == 0, json.dumps(install_report, indent=2)
    assert code == 40
    assert report["installation_state"] == "upgrade"
    assert report["installed_adapter_version"] == "0.2.9"
    assert report["expected_adapter_version"] == __version__


def test_install_rolls_back_the_previous_plugin_when_receipt_commit_fails(
    lifecycle_env, monkeypatch
):
    from dcc_mcp_gimp.install import EXIT_INSTALL, run

    installed, installed_code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert installed_code == 0, installed
    previous = lifecycle_env.plugins / "dcc_mcp_gimp"
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    prior_files = {
        path.relative_to(previous).as_posix(): path.read_bytes()
        for path in previous.rglob("*")
        if path.is_file()
    }
    prior_receipt = receipt_path.read_bytes()

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files._write_json_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt unavailable")),
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_INSTALL == 30
    assert report["verify"]["failure_stage"] == "install"
    assert "rolled back" in report["verify"]["failure_reason"]
    assert {
        path.relative_to(previous).as_posix(): path.read_bytes()
        for path in previous.rglob("*")
        if path.is_file()
    } == prior_files
    assert receipt_path.read_bytes() == prior_receipt


def test_install_reports_missing_bundled_artifact_as_acquire_failure(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_ACQUIRE, run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files._source_file",
        lambda: (_ for _ in ()).throw(FileNotFoundError("bundled plug-in missing")),
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_ACQUIRE == 20
    assert report["verify"]["failure_stage"] == "acquire"
    assert "bundled plug-in missing" in report["verify"]["failure_reason"]
    assert not list(lifecycle_env.plugins.glob(".dcc_mcp_gimp.*.stage"))


def test_install_defers_when_core_reports_a_loaded_plugin(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_REQUIRES_RESTART, run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files.inspect_install_root",
        lambda _target: {
            "requires_restart": True,
            "recommended_next_action": "Close GIMP and retry",
        },
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_REQUIRES_RESTART == 50
    assert report["status"] == "requires_restart"
    assert report["verify"]["failure_stage"] == "install"
    assert report["verify"]["failure_reason"] == "Close GIMP and retry"


def test_atomic_receipt_write_removes_temp_when_commit_fails(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import _write_json_atomic

    receipt = tmp_path / "receipts" / "gimp.json"
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files._replace_path",
        lambda *_args: (_ for _ in ()).throw(PermissionError("locked")),
    )

    with pytest.raises(PermissionError, match="locked"):
        _write_json_atomic(receipt, {"schema_version": 1})

    assert not list(receipt.parent.glob(".gimp.json.*.tmp"))


def test_install_reports_and_cleans_a_staging_failure(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_INSTALL, run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files.shutil.copy2",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_INSTALL == 30
    assert report["verify"]["failure_stage"] == "install"
    assert "disk full" in report["verify"]["failure_reason"]
    assert not list(lifecycle_env.plugins.glob(".dcc_mcp_gimp.*.stage"))


def test_install_returns_executable_next_steps_until_live_readiness(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_REQUIRES_RESTART, run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {"success": False, "message": "host is not ready"},
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_REQUIRES_RESTART == 50
    assert report["status"] == "requires_restart"
    assert report["verify"]["failure_stage"] == "readiness"
    assert all(
        bool(step.get("command")) ^ bool(step.get("file_edit")) for step in report["next_steps"]
    )
    commands = [step["command"] for step in report["next_steps"] if "command" in step]
    assert ["dcc-mcp-gimp"] not in commands
    assert not any("<" in token or ">" in token for command in commands for token in command)
    verify_command = next(command for command in commands if "verify" in command)
    for value in (
        "--dcc-path",
        str(lifecycle_env.host.resolve()),
        "--python",
        str(Path(sys.executable).resolve()),
        "--destination",
        str(lifecycle_env.plugins.resolve()),
    ):
        assert value in verify_command


def _install_schema_validator() -> Draft202012Validator:
    schema_bytes = (
        importlib.resources.files("dcc_mcp_gimp.schemas")
        .joinpath("adapter-install-sop-v1.schema.json")
        .read_bytes()
    )
    assert len(schema_bytes) == 4261
    assert (
        hashlib.sha256(schema_bytes).hexdigest()
        == "3ca25788439917b4d4c0617230a762f9797756b5b54f45c8c4149f975b90f904"
    )
    schema = json.loads(schema_bytes)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_all_public_lifecycle_results_validate_canonical_core_schema(lifecycle_env):
    from dcc_mcp_gimp.install import run

    validator = _install_schema_validator()
    reports = [run(["status", *lifecycle_env.common])[0]]
    reports.append(run(["install", "--yes", *lifecycle_env.common])[0])
    reports.append(run(["verify", *lifecycle_env.common])[0])
    reports.append(run(["upgrade", "--dry-run", *lifecycle_env.common])[0])
    reports.append(run(["uninstall", "--dry-run", *lifecycle_env.common])[0])
    missing = lifecycle_env.root / "missing-gimp.exe"
    reports.append(
        run(
            [
                "status",
                "--json",
                "--dcc-path",
                str(missing),
                "--python",
                sys.executable,
                "--destination",
                str(lifecycle_env.plugins),
            ]
        )[0]
    )
    for report in reports:
        validator.validate(report)


def test_failed_upgrade_live_verify_restores_exact_prior_install(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import _files_manifest, _manifest_digest, run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            'VERSION = "%s"' % __version__, 'VERSION = "0.2.9"', 1
        ),
        encoding="utf-8",
    )
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["adapter_version"] = "0.2.9"
    receipt["files"] = _files_manifest(target)
    receipt["ownership"]["files"] = receipt["files"]
    receipt["package_digest"] = _manifest_digest(receipt["files"])
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    prior_files = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    prior_receipt = receipt_path.read_bytes()
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {"success": False, "message": "injected readiness failure"},
    )

    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 40, report
    assert report["verify"]["failure_stage"] == "readiness"
    assert {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    } == prior_files
    assert receipt_path.read_bytes() == prior_receipt
    assert not list(lifecycle_env.plugins.glob(".dcc_mcp_gimp.*"))


def test_unowned_empty_directory_is_not_deleted(lifecycle_env):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    unowned = target / "operator-owned-empty"
    unowned.mkdir()

    status, status_code, _ = run(["status", *lifecycle_env.common])
    uninstall, uninstall_code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert status_code == 40
    assert status["status"] == "partial"
    assert uninstall_code in {10, 30}
    assert uninstall["verify"]["failure_stage"] == "receipt"
    assert unowned.is_dir()


def test_receipt_rejects_escape_duplicate_and_wrong_ownership_types(lifecycle_env):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt["ownership"]) == {"directories", "files", "links"}
    receipt["ownership"]["directories"] = ["../escape", "../escape"]
    receipt["ownership"]["links"] = "not-a-list"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    report, code, _ = run(["status", *lifecycle_env.common])

    assert code == 40
    assert report["status"] == "partial"
    assert report["verify"]["failure_stage"] == "receipt"


def test_uninstall_cleanup_failure_restores_target_and_receipt(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    prior_files = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    prior_receipt = receipt_path.read_bytes()
    original = install_files.safe_remove_tree

    def fail_quarantine(path):
        candidate = Path(path)
        if "uninstall" in candidate.name or candidate.name == "quarantine":
            victim = next(iter(candidate.rglob("*.py")), None)
            if victim is not None:
                victim.unlink()
            return {
                "success": False,
                "requires_restart": False,
                "message": "injected cleanup failure",
            }
        return original(path)

    monkeypatch.setattr(install_files, "safe_remove_tree", fail_quarantine)
    report, code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert code == 30, report
    assert {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    } == prior_files
    assert receipt_path.read_bytes() == prior_receipt
    assert not list(lifecycle_env.plugins.glob(".dcc_mcp_gimp.*"))


def test_verify_rejects_foreign_gimp_process_identity(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {
            "success": True,
            "entry": {
                "instance_id": "foreign",
                "runtime_pid": 999999,
                "mcp_url": "http://127.0.0.1:1/mcp",
            },
            "probe": {
                "result": {
                    "structuredContent": {
                        "success": True,
                        "context": {
                            "ready": True,
                            "gimp_pid": 999999,
                            "gimp_version": "3.0.8",
                            "adapter_version": __version__,
                        },
                    }
                }
            },
        },
    )

    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["verify"]["failure_stage"] == "readiness_identity"


@pytest.mark.parametrize(
    "value",
    [
        "0.19.38rc1",
        "0.19.38garbage",
        "00.019.038",
        "9" * 128 + ".19.38",
        " 0.19.38 ",
        "0.19",
        "0.19.38.1",
    ],
)
def test_versions_reject_noncanonical_or_unbounded_values(value):
    from dcc_mcp_gimp.install_contract import _version_tuple

    assert _version_tuple(value) == ()


def test_gimp_version_rejects_trailing_output(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install_contract import InstallFailure
    from dcc_mcp_gimp.install_host import _gimp_version

    executable = tmp_path / "gimp-3.0.exe"
    executable.write_bytes(b"gimp")
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_host.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="GNU Image Manipulation Program version 3.0.8 trailing\n",
            stderr="",
        ),
    )

    with pytest.raises(InstallFailure, match="determine GIMP version"):
        _gimp_version(executable)


def test_target_interpreter_rejects_shadowed_distribution(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install_contract import InstallFailure
    from dcc_mcp_gimp.install_host import _target_versions

    python = tmp_path / "python.exe"
    python.write_bytes(b"python")
    payload = _target_metadata(python)
    payload["adapter"]["owned"] = False
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_host.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(InstallFailure, match="outside its selected distribution"):
        _target_versions(python)


def test_verify_rejects_pid_reuse_during_identity_check(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    identities = iter(("before-reuse", "after-reuse"))
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle._process_start_identity",
        lambda _pid: next(identities),
    )

    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["verify"]["failure_stage"] == "readiness_identity"
    assert "start identity" in report["verify"]["failure_reason"]
