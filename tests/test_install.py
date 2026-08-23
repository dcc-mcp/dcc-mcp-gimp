import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcc_mcp_gimp.__version__ import __version__
from dcc_mcp_gimp.install import doctor, install


@pytest.fixture
def lifecycle_env(tmp_path, monkeypatch):
    host = tmp_path / "gimp-3.0.exe"
    host.write_bytes(b"")
    plugins = tmp_path / "profile" / "plug-ins"

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        elif "importable" in command[-1]:
            payload = json.dumps({"importable": True, "version": __version__})
        else:
            payload = json.dumps(
                {"dcc-mcp-core": "0.19.91", "dcc-mcp-gimp": __version__}
            )
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("dcc_mcp_gimp.install_host.subprocess.run", fake_run)
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda **_kwargs: {"success": True, "status": "ready"},
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


def test_default_plugin_dir_uses_gimp_application_support_on_macos(
    tmp_path, monkeypatch
):
    from dcc_mcp_gimp.install import default_plugin_dir

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert default_plugin_dir() == (
        tmp_path / "Library/Application Support/GIMP/3.0/plug-ins"
    )


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
    assert report["schema_version"] == "1"
    assert report["dcc_type"] == "gimp"
    assert report["verb"] == "status"
    assert report["status"] == "fresh"
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

    install_report, install_code, _ = run(
        ["install", "--yes", *lifecycle_env.common]
    )
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

    install_report, install_code, _ = run(
        ["install", "--yes", *lifecycle_env.common]
    )
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


def test_standard_entrypoint_dispatches_lifecycle_verbs_without_starting_server(
    tmp_path, capsys
):
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
    host.write_bytes(b"")

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        else:
            payload = json.dumps(
                {"dcc-mcp-core": "0.19.91", "dcc-mcp-gimp": __version__}
            )
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


def test_status_prefers_the_host_adjacent_python_when_no_override_is_given(
    tmp_path, monkeypatch
):
    from dcc_mcp_gimp.install import run

    host = tmp_path / "GIMP 3" / "bin" / "gimp-3.0.exe"
    embedded_python = host.with_name("python.exe")
    host.parent.mkdir(parents=True)
    host.write_bytes(b"")
    embedded_python.write_bytes(b"")

    def fake_run(command, **_kwargs):
        if Path(command[0]) == host:
            payload = "GNU Image Manipulation Program version 3.0.8\n"
        else:
            assert Path(command[0]) == embedded_python
            payload = json.dumps(
                {"dcc-mcp-core": "0.19.91", "dcc-mcp-gimp": __version__}
            )
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

    install_report, install_code, _ = run(
        ["install", "--yes", *lifecycle_env.common]
    )
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

    previous = lifecycle_env.plugins / "dcc_mcp_gimp"
    previous.mkdir(parents=True)
    sentinel = previous / "previous.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files._write_json_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt unavailable")),
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_INSTALL == 30
    assert report["verify"]["failure_stage"] == "install"
    assert "rolled back" in report["verify"]["failure_reason"]
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (
        lifecycle_env.root / ".dcc-mcp" / "receipts" / "gimp.json"
    ).exists()


def test_install_reports_missing_bundled_artifact_as_acquire_failure(
    lifecycle_env, monkeypatch
):
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
    assert report["verify"]["failure_reason"] == "Close GIMP and retry"
    assert not (lifecycle_env.plugins / "dcc_mcp_gimp").exists()


def test_atomic_receipt_write_removes_temp_when_commit_fails(tmp_path, monkeypatch):
    from dcc_mcp_gimp.install import _write_json_atomic

    receipt = tmp_path / "receipts" / "gimp.json"
    monkeypatch.setattr(
        "dcc_mcp_gimp.install_files._replace_path",
        lambda *_args: (_ for _ in ()).throw(PermissionError("locked")),
    )

    with pytest.raises(PermissionError, match="locked"):
        _write_json_atomic(receipt, {"schema_version": "1"})

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


def test_install_returns_executable_next_steps_until_live_readiness(
    lifecycle_env, monkeypatch
):
    from dcc_mcp_gimp.install import EXIT_REQUIRES_RESTART, run

    monkeypatch.setattr(
        "dcc_mcp_gimp.install_lifecycle.wait_for_sidecar_ready",
        lambda **_kwargs: {"success": False, "message": "host is not ready"},
    )

    report, code, _ = run(["install", "--yes", *lifecycle_env.common])

    assert code == EXIT_REQUIRES_RESTART == 50
    assert report["status"] == "requires_restart"
    assert report["verify"]["failure_stage"] == "readiness"
    assert all(step["command"] for step in report["next_steps"])
    assert {step["id"] for step in report["next_steps"]} == {
        "restart-gimp",
        "start-adapter",
        "verify-ready",
    }
