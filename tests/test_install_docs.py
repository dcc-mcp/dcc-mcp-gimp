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
    assert (
        "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-gimp/main/install.md"
        in guide
    )
