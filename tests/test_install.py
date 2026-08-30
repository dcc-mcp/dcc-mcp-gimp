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
from dcc_mcp_gimp.install_contract import InstallFailure


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
                            "gimp_start_identity": "test-start-4242",
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


def test_doctor_rejects_linked_plugin_child(tmp_path, monkeypatch):
    from dcc_mcp_gimp import install_files

    root = tmp_path / "plugins"
    target = install(root)
    script = target / "dcc_mcp_gimp.py"
    original = install_files._is_link
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle._is_link",
        lambda path: path in {target, script} or original(path),
    )

    result = doctor(root)

    assert result["ready"] is False
    assert result["physical_path_error"] == "Managed GIMP plug-in path is linked"
    assert result["plugin_script_exists"] is False


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
    assert isinstance(receipt["entry_point_executable"], bool)


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


def test_exact_interpreter_module_entrypoint_dispatches_lifecycle_verbs(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "dcc_mcp_gimp",
            "status",
            "--json",
            "--dcc-path",
            str(tmp_path / "missing-gimp"),
            "--python",
            sys.executable,
            "--destination",
            str(tmp_path / "plugins"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )

    assert completed.returncode == 10, completed.stderr
    payload = json.loads(completed.stdout)
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


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires elevated Windows rights")
def test_receipt_parent_swap_between_checks_fails_closed(tmp_path, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install_contract import InstallFailure

    receipt = tmp_path / "receipts" / "gimp.json"
    receipt.parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "gimp.json"
    sentinel.write_text("do not overwrite", encoding="utf-8")
    original_safe = install_files._assert_receipt_path_safe
    swapped = False

    def safe_then_swap(path):
        nonlocal swapped
        original_safe(path)
        if not swapped:
            swapped = True
            old = receipt.parent.with_name("receipts-owned")
            receipt.parent.rename(old)
            receipt.parent.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(install_files, "_assert_receipt_path_safe", safe_then_swap)
    with pytest.raises(InstallFailure):
        install_files._write_json_atomic(receipt, {"schema_version": 1})

    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"


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
    assert not any("<" in token or ">" in token for command in commands for token in command)
    selected_python = str(Path(sys.executable).resolve())
    assert commands[0] == [str(lifecycle_env.host.resolve()), "--new-instance"]
    assert commands[1] == [selected_python, "-m", "dcc_mcp_gimp"]
    verify_command = commands[2]
    assert verify_command[:5] == [selected_python, "-m", "dcc_mcp_gimp", "verify", "--json"]
    for value in (
        "--dcc-path",
        str(lifecycle_env.host.resolve()),
        "--python",
        selected_python,
        "--destination",
        str(lifecycle_env.plugins.resolve()),
    ):
        assert value in verify_command
    assert report["next_steps"][0]["profile_selector"] == str(lifecycle_env.plugins.resolve())
    assert report["next_steps"][0]["environment"]["GIMP3_DIRECTORY"] == str(
        lifecycle_env.plugins.resolve().parent
    )


def test_install_readiness_is_bound_to_the_transaction_target(lifecycle_env, monkeypatch):
    import dcc_mcp_gimp.install as install_module

    captured = {}
    original = install_module.verify_install

    def wrapped(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(install_module, "verify_install", wrapped)
    report, code, _ = install_module.run(["install", "--yes", *lifecycle_env.common])

    assert code in {0, 50}
    assert captured["expected_target_identity"] is not None
    assert captured["expected_file_identities"]


def test_next_steps_retain_explicit_instance_and_host_identity(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {"success": False, "message": "host is not ready"},
    )
    report, code, _ = run(
        [
            "verify",
            "--instance-id",
            "selected-instance",
            "--host-pid",
            "4242",
            *lifecycle_env.common,
        ]
    )

    assert code == 40
    verify_step = next(
        step for step in report["next_steps"] if step["id"] == "verify-selected-gimp"
    )
    assert verify_step["command"][-4:] == [
        "--instance-id",
        "selected-instance",
        "--host-pid",
        "4242",
    ]


def test_bootstrap_summary_bounds_oversized_records(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install_host import _bootstrap_error_summary

    path = tmp_path / "bootstrap.jsonl"
    path.write_text(json.dumps({"stage": "bridge-startup", "message": "x" * (1024 * 1024)}) + "\n")
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(path))

    summary = _bootstrap_error_summary()

    assert summary["count"] == 1
    assert summary["latest"]["truncated"] is True
    assert len(summary["latest"]["message"]) <= 64 * 1024


def test_bootstrap_summary_prefers_latest_bounded_records(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install_host import _bootstrap_error_summary

    path = tmp_path / "bootstrap.jsonl"
    path.write_text(
        "".join(
            json.dumps({"stage": "record-%04d" % index, "message": "bounded"}) + "\n"
            for index in range(1500)
        )
    )
    monkeypatch.setenv("DCC_MCP_GIMP_BOOTSTRAP_ERRORS", str(path))

    summary = _bootstrap_error_summary()

    assert summary["count"] <= 1024
    assert summary["latest"]["stage"] == "record-1499"


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


def test_receipt_rejects_tampered_digest_and_top_level_manifest(lifecycle_env):
    from dcc_mcp_gimp.install import _manifest_digest, run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    receipt["package_digest"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    status, code, _ = run(["status", *lifecycle_env.common])

    assert code == 40
    assert status["installation_state"] == "repair"
    assert status["verify"]["failure_stage"] == "receipt"

    receipt["files"] = receipt["ownership"]["files"]
    receipt["package_digest"] = _manifest_digest(receipt["files"])
    receipt["host_paths_touched"] = [str(lifecycle_env.root / "foreign-plugin")]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    status, code, _ = run(["status", *lifecycle_env.common])

    assert code == 40
    assert status["installation_state"] == "repair"
    assert status["verify"]["failure_stage"] == "receipt"

    receipt["package_digest"] = receipt["ownership"]["files"][0]["sha256"]
    receipt["files"] = []
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    status, code, _ = run(["status", *lifecycle_env.common])

    assert code == 40
    assert status["installation_state"] == "repair"
    assert status["verify"]["failure_stage"] == "receipt"


def test_receipt_rejects_unhashable_ownership_entries_without_crashing(lifecycle_env):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["ownership"]["directories"] = [{}]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    status, code, _ = run(["status", *lifecycle_env.common])

    assert code == 40
    assert status["installation_state"] == "repair"
    assert status["verify"]["failure_stage"] == "receipt"


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


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX dirfd receipt writer")
def test_receipt_temp_collision_does_not_remove_foreign_file(tmp_path, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install_contract import InstallFailure

    receipt = tmp_path / "receipts" / "gimp.json"
    receipt.parent.mkdir()
    collision = receipt.with_name(".gimp.json.collision.tmp")
    collision.write_bytes(b"foreign sentinel")

    class CollisionUuid:
        hex = "collision"

    monkeypatch.setattr(install_files.uuid, "uuid4", lambda: CollisionUuid())

    with pytest.raises(InstallFailure):
        install_files._write_bytes_atomic(receipt, b"new receipt")

    assert collision.read_bytes() == b"foreign sentinel"


def test_launch_preflight_rejects_changed_executable_identity(tmp_path):
    from dcc_mcp_gimp.install_contract import InstallFailure
    from dcc_mcp_gimp.install_host import _executable_identity, _gimp_version, _target_versions

    executable = tmp_path / "gimp-3.0.exe"
    executable.write_bytes(b"gimp")
    python = tmp_path / "python"
    python.write_bytes(b"python")
    gimp_identity = _executable_identity(executable)
    python_identity = _executable_identity(python)
    executable.unlink()
    executable.write_bytes(b"attacker")
    with pytest.raises(InstallFailure, match="identity"):
        _gimp_version(executable, expected_identity=gimp_identity)
    python.unlink()
    python.write_bytes(b"attacker")
    with pytest.raises(InstallFailure, match="identity"):
        _target_versions(python, expected_identity=python_identity)


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX profile-root swap")
def test_uninstall_root_swap_does_not_write_foreign_recovery(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    destination = lifecycle_env.plugins
    parked = lifecycle_env.root / "parked-profile"
    foreign = lifecycle_env.root / "foreign-profile"
    foreign.mkdir()
    (foreign / "sentinel.txt").write_text("keep", encoding="utf-8")
    original = install_files._copy_validated_recovery
    swapped = False
    foreign_receipt = None

    def swap_after_snapshot(source, recovery, *args, **kwargs):
        nonlocal swapped, foreign_receipt
        result = original(source, recovery, *args, **kwargs)
        if not swapped:
            transaction_name = recovery.parents[1].name
            destination.rename(parked)
            foreign.rename(destination)
            (destination / transaction_name / "snapshot").mkdir(parents=True)
            (destination / transaction_name / "quarantine").mkdir()
            foreign_receipt = destination / transaction_name / "snapshot" / "gimp.json"
            swapped = True
        return result

    monkeypatch.setattr(install_files, "_copy_validated_recovery", swap_after_snapshot)
    report, code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert code != 0, report
    assert (destination / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert foreign_receipt is not None and not foreign_receipt.exists()


def test_receipt_child_identity_swap_fails_closed(monkeypatch, tmp_path):
    from dcc_mcp_gimp import install_files

    source = tmp_path / "receipt.json"
    parked = tmp_path / "receipt.owned"
    foreign = tmp_path / "foreign.json"
    source.write_bytes(b"owned")
    foreign.write_bytes(b"FOREIGN")
    original = install_files._assert_receipt_file_identity
    swapped = False

    def late(path, expected, stage="receipt"):
        nonlocal swapped
        result = original(path, expected, stage)
        if path == source and not swapped:
            source.rename(parked)
            foreign.rename(source)
            swapped = True
        return result

    monkeypatch.setattr(install_files, "_assert_receipt_file_identity", late)
    with pytest.raises(InstallFailure, match="identity"):
        install_files._unlink_receipt_owned(source)
    assert source.read_bytes() == b"FOREIGN"
    assert parked.read_bytes() == b"owned"


def test_receipt_move_identity_swap_fails_closed(monkeypatch, tmp_path):
    from dcc_mcp_gimp import install_files

    source = tmp_path / "source.json"
    parked = tmp_path / "source.owned"
    foreign = tmp_path / "foreign.json"
    destination = tmp_path / "quarantine.json"
    source.write_bytes(b"owned")
    foreign.write_bytes(b"FOREIGN")
    original = install_files._assert_receipt_file_identity
    swapped = False

    def late(path, expected, stage="receipt"):
        nonlocal swapped
        result = original(path, expected, stage)
        if path == source and not swapped:
            source.rename(parked)
            foreign.rename(source)
            swapped = True
        return result

    monkeypatch.setattr(install_files, "_assert_receipt_file_identity", late)
    with pytest.raises(InstallFailure, match="identity"):
        install_files._replace_receipt_owned(source, destination, tmp_path, None)
    assert source.read_bytes() == b"FOREIGN"
    assert parked.read_bytes() == b"owned"
    assert not destination.exists()


def test_receipt_name_identity_swap_fails_closed(monkeypatch, tmp_path):
    from dcc_mcp_gimp import install_files

    source = tmp_path / "receipt.json"
    parked = tmp_path / "receipt.owned"
    foreign = tmp_path / "foreign.json"
    source.write_bytes(b"owned")
    foreign.write_bytes(b"FOREIGN")
    original = install_files._assert_receipt_name_identity
    swapped = False

    def late(path, expected, stage="receipt"):
        nonlocal swapped
        result = original(path, expected, stage)
        if path == source and not swapped:
            source.rename(parked)
            foreign.rename(source)
            swapped = True
        return result

    monkeypatch.setattr(install_files, "_assert_receipt_name_identity", late)
    with pytest.raises(InstallFailure, match="identity"):
        install_files._unlink_receipt_owned(source)
    assert source.read_bytes() == b"FOREIGN"
    assert parked.read_bytes() == b"owned"


def test_receipt_move_name_identity_swap_fails_closed(monkeypatch, tmp_path):
    from dcc_mcp_gimp import install_files

    source = tmp_path / "source.json"
    parked = tmp_path / "source.owned"
    foreign = tmp_path / "foreign.json"
    destination = tmp_path / "quarantine.json"
    source.write_bytes(b"owned")
    foreign.write_bytes(b"FOREIGN")
    original = install_files._assert_receipt_name_identity
    swapped = False

    def late(path, expected, stage="receipt"):
        nonlocal swapped
        result = original(path, expected, stage)
        if path == source and not swapped:
            source.rename(parked)
            foreign.rename(source)
            swapped = True
        return result

    monkeypatch.setattr(install_files, "_assert_receipt_name_identity", late)
    with pytest.raises(InstallFailure, match="identity"):
        install_files._replace_receipt_owned(source, destination, tmp_path, None)
    assert source.read_bytes() == b"FOREIGN"
    assert parked.read_bytes() == b"owned"
    assert not destination.exists()


def test_recovery_copy_destination_swap_does_not_write_external(tmp_path, monkeypatch):
    from dcc_mcp_gimp import install_files

    source = tmp_path / "source"
    source.mkdir()
    (source / "owned.txt").write_text("owned", encoding="utf-8")
    destination_parent = tmp_path / "transaction" / "snapshot"
    destination_parent.mkdir(parents=True)
    recovery = destination_parent / "dcc_mcp_gimp"
    external = tmp_path / "external"
    external.mkdir()
    parked = tmp_path / "snapshot-owned"
    original = install_files.shutil.copytree
    swapped = False

    def race(src, dst, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            destination_parent.rename(parked)
            destination_parent.symlink_to(external, target_is_directory=True)
            swapped = True
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(install_files.shutil, "copytree", race)
    with pytest.raises(InstallFailure):
        install_files._copy_validated_recovery(source, recovery)
    assert swapped
    assert not (external / "dcc_mcp_gimp" / "owned.txt").exists()


def test_verify_runtime_shape_failure_is_structured(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.query_runtime_state", lambda *_args, **_kwargs: None
    )
    report, code, _ = run(["verify", *lifecycle_env.common])
    assert code != 0
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "readiness"
    assert "runtime" in report["verify"]["failure_reason"].lower()


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_verify_readiness_shape_failure_is_structured(lifecycle_env, monkeypatch, payload):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: payload,
    )
    report, code, _ = run(["verify", *lifecycle_env.common])
    assert code != 0
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "readiness"
    assert "readiness" in report["verify"]["failure_reason"].lower()


def test_verify_readiness_exception_is_structured(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed

    def explode(*_args, **_kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready", explode)
    report, code, _ = run(["verify", *lifecycle_env.common])
    assert code != 0
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "readiness"
    assert report["verify"]["failure_reason"] == "GIMP sidecar readiness probe failed"


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows directory lease")
def test_windows_directory_lease_blocks_profile_root_rename(tmp_path):
    from dcc_mcp_gimp.install_files import _windows_directory_lease

    root = tmp_path / "profile"
    root.mkdir()
    parked = tmp_path / "parked-profile"
    with _windows_directory_lease(root, "profile"):
        with pytest.raises(OSError):
            root.rename(parked)
    assert root.is_dir()
    assert not parked.exists()


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


def test_target_interpreter_rejects_unbounded_metadata(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install_contract import InstallFailure
    from dcc_mcp_gimp.install_host import _target_versions

    python = tmp_path / "python.exe"
    python.write_bytes(b"python")
    oversized = "{" + (" " * (256 * 1024)) + "}"
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_host.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=oversized, stderr=""
        ),
    )

    with pytest.raises(InstallFailure, match="metadata is unbounded"):
        _target_versions(python)


@pytest.mark.parametrize("field", ["core", "adapter"])
@pytest.mark.parametrize("nested", [None, [], "not-an-object", 7])
def test_target_interpreter_rejects_malformed_nested_metadata(tmp_path, monkeypatch, field, nested):
    from dcc_mcp_gimp.install_contract import InstallFailure
    from dcc_mcp_gimp.install_host import _target_versions

    python = tmp_path / "python.exe"
    python.write_bytes(b"python")
    payload = _target_metadata(python)
    payload[field] = nested
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_host.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(InstallFailure, match="incomplete metadata"):
        _target_versions(python)


def test_run_returns_structured_preflight_for_malformed_nested_metadata(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_PREFLIGHT, run

    original = _target_metadata(sys.executable)
    original["adapter"] = None

    def malformed_run(command, **_kwargs):
        if Path(command[0]) == lifecycle_env.host:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="GNU Image Manipulation Program version 3.0.8\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(original), stderr="")

    monkeypatch.setattr("dcc_mcp_gimp.install_host.subprocess.run", malformed_run)
    report, code, as_json = run(["status", *lifecycle_env.common])

    assert as_json is True
    assert code == EXIT_PREFLIGHT == 10
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "python"
    assert "incomplete metadata" in report["verify"]["failure_reason"]


def test_profile_preflight_rejects_regular_file_destination(lifecycle_env):
    from dcc_mcp_gimp.install import EXIT_PREFLIGHT, run

    lifecycle_env.plugins.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_env.plugins.write_text("not a directory", encoding="utf-8")

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_PREFLIGHT == 10
    assert report["verify"]["failure_stage"] == "profile"
    assert report["status"] == "failed"


def test_multiple_destinations_fail_closed_without_overwriting_receipt(lifecycle_env):
    from dcc_mcp_gimp.install import EXIT_PREFLIGHT, run

    first = lifecycle_env.plugins
    second = lifecycle_env.root / "other-profile" / "plug-ins"
    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    prior_receipt = receipt_path.read_bytes()

    second_args = [
        "status",
        "--json",
        "--dcc-path",
        str(lifecycle_env.host),
        "--python",
        str(Path(sys.executable).resolve()),
        "--destination",
        str(second),
    ]
    report, code, _ = run(second_args)

    assert code == EXIT_PREFLIGHT == 10
    assert report["verify"]["failure_stage"] == "receipt"
    assert "another GIMP profile" in report["verify"]["failure_reason"]
    assert receipt_path.read_bytes() == prior_receipt
    assert not second.exists()
    assert first.exists()


def test_legacy_receipt_mode_field_fails_closed_consistently(lifecycle_env):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("entry_point_executable")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    status, status_code, _ = run(["status", *lifecycle_env.common])
    upgrade, upgrade_code, _ = run(["upgrade", "--yes", *lifecycle_env.common])
    uninstall, uninstall_code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert status_code == 40
    assert status["installation_state"] == "repair"
    assert upgrade_code == 30
    assert uninstall_code == 10
    assert upgrade["verify"]["failure_stage"] == "receipt"
    assert uninstall["verify"]["failure_stage"] == "receipt"


def test_plan_maps_symlink_loop_to_structured_preflight(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import EXIT_PREFLIGHT, run

    host = tmp_path / "gimp-3.0.exe"
    host.write_bytes(b"gimp")
    plugins = tmp_path / "profile" / "plug-ins"
    original_resolve = Path.resolve

    def loop_resolve(path, *args, **kwargs):
        if path == plugins:
            raise RuntimeError("Symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", loop_resolve)
    report, code, _ = run(
        [
            "status",
            "--json",
            "--dcc-path",
            str(host),
            "--python",
            sys.executable,
            "--destination",
            str(plugins),
        ]
    )

    assert code == EXIT_PREFLIGHT == 10
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "profile"


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute-bit contract")
def test_verify_rejects_entry_point_execute_bit_drift(lifecycle_env):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    script = lifecycle_env.plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py"
    script.chmod(0o644)

    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["verify"]["failure_stage"] == "artifact"
    assert "executable mode" in report["verify"]["failure_reason"]


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires elevated Windows rights")
def test_receipt_symlink_swap_fails_closed_without_external_write(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    external = lifecycle_env.root / "foreign-receipt.json"
    external.write_text("do not overwrite", encoding="utf-8")
    original_write = install_files._write_json_atomic

    def swap_then_fail(path, payload):
        assert Path(path) == receipt_path
        receipt_path.unlink()
        receipt_path.symlink_to(external)
        raise OSError("simulated receipt race")

    monkeypatch.setattr(install_files, "_write_json_atomic", swap_then_fail)
    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 30
    assert report["status"] == "failed"
    assert external.read_text(encoding="utf-8") == "do not overwrite"
    assert receipt_path.is_symlink()
    monkeypatch.setattr(install_files, "_write_json_atomic", original_write)


def test_root_identity_swap_during_staging_fails_closed(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    original_stage = install_files._stage_plugin

    def stage_then_swap(root):
        stage = original_stage(root)
        old_root = root.with_name("plug-ins-old")
        root.rename(old_root)
        root.mkdir()
        return stage

    monkeypatch.setattr(install_files, "_stage_plugin", stage_then_swap)
    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 10
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "profile"
    assert not (lifecycle_env.plugins / "dcc_mcp_gimp").exists()


def test_verify_target_swap_after_validation_fails_closed(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_lifecycle
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    foreign = lifecycle_env.root / "foreign-plugin"
    foreign.mkdir()
    (foreign / "operator.txt").write_text("do not touch", encoding="utf-8")
    original_validate = install_lifecycle._validate_owned_install

    def validate_then_swap(*args, **kwargs):
        original_validate(*args, **kwargs)
        owned = target.with_name("dcc_mcp_gimp-owned")
        target.rename(owned)
        target.mkdir()
        (target / "operator.txt").write_text("foreign", encoding="utf-8")

    monkeypatch.setattr(install_lifecycle, "_validate_owned_install", validate_then_swap)
    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert "identity" in report["verify"]["failure_reason"]
    assert (foreign / "operator.txt").read_text(encoding="utf-8") == "do not touch"


def test_verify_owned_entry_point_swap_after_validation_fails_closed(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_lifecycle
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    script = lifecycle_env.plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py"
    original_bytes = script.read_bytes()
    original_validate = install_lifecycle._validate_owned_install

    def validate_then_swap(*args, **kwargs):
        original_validate(*args, **kwargs)
        script.unlink()
        script.write_bytes(original_bytes)

    monkeypatch.setattr(install_lifecycle, "_validate_owned_install", validate_then_swap)
    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["status"] == "failed"
    assert report["verify"]["directly_usable"] is False
    assert "identity" in report["verify"]["failure_reason"]


def test_upgrade_target_swap_before_replace_preserves_foreign_tree(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    original_stage = install_files._stage_plugin

    def stage_then_swap(root):
        stage = original_stage(root)
        owned = target.with_name("dcc_mcp_gimp-owned")
        target.rename(owned)
        target.mkdir()
        (target / "operator.txt").write_text("foreign", encoding="utf-8")
        return stage

    monkeypatch.setattr(install_files, "_stage_plugin", stage_then_swap)
    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 10
    assert report["status"] == "failed"
    assert (target / "operator.txt").read_text(encoding="utf-8") == "foreign"


def test_upgrade_owned_child_swap_before_replace_preserves_foreign_file(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    original_stage = install_files._stage_plugin

    def stage_then_swap(root):
        stage = original_stage(root)
        script.unlink()
        script.write_text("foreign operator", encoding="utf-8")
        return stage

    monkeypatch.setattr(install_files, "_stage_plugin", stage_then_swap)
    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 10
    assert report["status"] == "failed"
    assert script.read_text(encoding="utf-8") == "foreign operator"


def test_uninstall_child_swap_after_validation_preserves_foreign_file(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    original_validate = install_files._validate_owned_install
    calls = 0

    def validate_then_swap(*args, **kwargs):
        nonlocal calls
        calls += 1
        original_validate(*args, **kwargs)
        if calls >= 2:
            script.unlink()
            script.write_text("foreign operator", encoding="utf-8")

    monkeypatch.setattr(install_files, "_validate_owned_install", validate_then_swap)
    report, code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert code == 10
    assert report["status"] == "failed"
    assert script.read_text(encoding="utf-8") == "foreign operator"


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires elevated Windows rights")
def test_destination_symlink_is_rejected_before_canonicalization(lifecycle_env):
    from dcc_mcp_gimp.install import run

    actual = lifecycle_env.root / "actual-profile" / "plug-ins"
    actual.mkdir(parents=True)
    linked = lifecycle_env.root / "linked-profile" / "plug-ins"
    linked.parent.mkdir()
    linked.symlink_to(actual, target_is_directory=True)

    report, code, _ = run(
        [
            "status",
            "--json",
            "--dcc-path",
            str(lifecycle_env.host),
            "--python",
            sys.executable,
            "--destination",
            str(linked),
        ]
    )

    assert code == 10
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "profile"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_windows_junction_is_rejected_as_reparse_point(tmp_path):
    from dcc_mcp_gimp.install_files import _is_link

    external = tmp_path / "external"
    external.mkdir()
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")

    assert _is_link(junction) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_destination_junction_is_rejected_before_canonicalization(lifecycle_env):
    from dcc_mcp_gimp.install import run

    actual = lifecycle_env.root / "actual-profile"
    actual.mkdir()
    linked = lifecycle_env.root / "linked-profile"
    process = subprocess.Popen(
        ["cmd", "/c", "mklink", "/J", str(linked), str(actual)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        pytest.skip("junction creation unavailable")
    from dcc_mcp_gimp.install_files import _is_link

    assert linked.exists(), (stdout, stderr)
    assert _is_link(linked) is True
    destination = linked / "plug-ins"

    report, code, _ = run(
        [
            "status",
            "--json",
            "--dcc-path",
            str(lifecycle_env.host),
            "--python",
            sys.executable,
            "--destination",
            str(destination),
        ]
    )

    assert code == 10
    assert report["status"] == "failed"
    assert report["verify"]["failure_stage"] == "profile"


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


def test_verify_rejects_parent_start_identity_captured_before_pid_reuse(lifecycle_env, monkeypatch):
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda *_args, **_kwargs: {
            "success": True,
            "entry": {
                "instance_id": "gimp-test-4242",
                "adapter_version": __version__,
                "mcp_url": "http://127.0.0.1:18812/mcp",
            },
            "probe": {
                "result": {
                    "structuredContent": {
                        "success": True,
                        "context": {
                            "ready": True,
                            "authenticated": True,
                            "gimp_pid": 4242,
                            "gimp_start_identity": "reused-before-verification",
                            "plugin_pid": 4343,
                            "gimp_version": "3.0.8",
                            "adapter_version": __version__,
                            "bridge_host": "127.0.0.1",
                            "bridge_port": 3848,
                            "plugin_module_path": str(
                                (
                                    lifecycle_env.plugins / "dcc_mcp_gimp" / "dcc_mcp_gimp.py"
                                ).resolve()
                            ),
                        },
                    }
                }
            },
        },
    )

    report, code, _ = run(["verify", *lifecycle_env.common])

    assert code == 40
    assert report["verify"]["failure_stage"] == "readiness_identity"
    assert "captured start identity" in report["verify"]["failure_reason"]


def test_partial_verified_backup_cleanup_restores_from_validated_recovery(
    lifecycle_env, monkeypatch
):
    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import _files_manifest, _manifest_digest, run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            'VERSION = "%s"' % __version__, 'VERSION = "0.3.9"', 1
        ),
        encoding="utf-8",
    )
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["adapter_version"] = "0.3.9"
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
    original_cleanup = install_files._cleanup_tree
    injected = False

    def partially_remove_backup(path):
        nonlocal injected
        candidate = Path(path)
        if not injected and candidate.name.endswith(".backup"):
            injected = True
            victim = next(candidate.rglob("*.py"))
            victim.unlink()
            return {"success": False, "requires_restart": False, "message": "injected"}
        return original_cleanup(candidate)

    monkeypatch.setattr(install_files, "_cleanup_tree", partially_remove_backup)

    report, code, _ = run(["upgrade", "--yes", *lifecycle_env.common])

    assert code == 30, report
    assert injected is True
    assert {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    } == prior_files
    assert receipt_path.read_bytes() == prior_receipt


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode preservation contract")
def test_failed_uninstall_rollback_preserves_posix_modes(lifecycle_env, monkeypatch):
    import stat

    from dcc_mcp_gimp import install_files
    from dcc_mcp_gimp.install import run

    installed, code, _ = run(["install", "--yes", *lifecycle_env.common])
    assert code == 0, installed
    target = lifecycle_env.plugins / "dcc_mcp_gimp"
    script = target / "dcc_mcp_gimp.py"
    receipt_path = lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    target.chmod(0o711)
    script.chmod(0o751)
    receipt_path.chmod(0o640)
    original_cleanup = install_files._cleanup_tree
    injected = False

    def fail_after_partial_quarantine_cleanup(path):
        nonlocal injected
        candidate = Path(path)
        if not injected and candidate.name == "quarantine":
            injected = True
            next(candidate.rglob("*.py")).unlink()
            return {"success": False, "requires_restart": False, "message": "injected"}
        return original_cleanup(candidate)

    monkeypatch.setattr(install_files, "_cleanup_tree", fail_after_partial_quarantine_cleanup)

    report, code, _ = run(["uninstall", "--yes", *lifecycle_env.common])

    assert code == 30, report
    assert stat.S_IMODE(target.stat().st_mode) == 0o711
    assert stat.S_IMODE(script.stat().st_mode) == 0o751
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o640
