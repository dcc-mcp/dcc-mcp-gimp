import os

from dcc_mcp_gimp.install import doctor, install


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
