from pathlib import Path


def test_install_sop_documents_the_public_agent_contract():
    repository = Path(__file__).resolve().parents[1]
    guide = (repository / "install.md").read_text(encoding="utf-8")

    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in guide
    for platform in ("Windows", "macOS", "Linux"):
        assert platform in guide
    for verb in ("install", "status", "verify", "uninstall", "upgrade"):
        assert "dcc-mcp-gimp %s" % verb in guide
    for flag in ("--json", "--yes", "--dry-run", "--dcc-path", "--python"):
        assert flag in guide
    assert "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-gimp/main/install.md" in guide
    assert "no-argument" in guide and "starts automatically" in guide
    assert "invoke `python-fu-dcc-mcp-gimp-bridge`" not in guide


def test_pinned_appimage_job_states_its_actual_boundary():
    repository = Path(__file__).resolve().parents[1]
    workflow = (repository / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    guide = (repository / "install.md").read_text(encoding="utf-8")

    assert "Pinned AppImage packaging boundary (no plug-in load or readiness)" in workflow
    assert "does not load or register the plug-in" in guide


def test_core_floor_is_projected_into_public_install_surfaces():
    repository = Path(__file__).resolve().parents[1]
    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    guide = (repository / "install.md").read_text(encoding="utf-8")
    skill = (repository / "src/dcc_mcp_gimp/skills/gimp-session/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "dcc-mcp-core>=0.19.91,<1.0.0" in pyproject
    assert "0.19.91 or newer, below 1.0" in guide
    assert "dcc-mcp-core 0.19.91+" in skill
    assert "dcc-mcp-gimp install --dry-run --json" in skill
    assert "verify.directly_usable: true" in skill
    assert "exit 50" in skill
