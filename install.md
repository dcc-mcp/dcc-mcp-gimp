# Install DCC-MCP GIMP

This is the canonical Install SOP v1 guide for `dcc-mcp-gimp`. The raw URL for
agent catalogs is:

```text
https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-gimp/main/install.md
```

## Requirements

- GIMP 3.x installed locally.
- Python 3.9 or newer with `dcc-mcp-gimp` and a compatible
  `dcc-mcp-core` installed.
- Permission to write the current user's GIMP 3 plug-in profile.

The installer supports Windows, macOS, and Linux. Default plug-in profiles are:

| Platform | Default profile |
| --- | --- |
| Windows | `%APPDATA%\GIMP\3.0\plug-ins` |
| macOS | `~/Library/Application Support/GIMP/3.0/plug-ins` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/GIMP/3.0/plug-ins` |

GIMP 2.x is not supported. The installer rejects an unsupported host version
before creating or changing a profile.

## Supported versions

| Component | Supported |
| --- | --- |
| GIMP | 3.x |
| Python | 3.9+ |
| `dcc-mcp-core` | 0.19.91 or newer, below 1.0 |

When `--python` is omitted, the installer checks for a Python executable next
to GIMP and then falls back to the interpreter running the CLI. Set
`DCC_MCP_INSTALL_PYTHON` or pass `--python` to choose explicitly.

## Agent quick path

Install the package into the interpreter that will run the adapter:

```bash
python -m pip install dcc-mcp-gimp
```

Inspect the host, interpreter, profile, and current installation without
changing files:

```bash
dcc-mcp-gimp status --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
dcc-mcp-gimp install --dry-run --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
```

Apply the plan non-interactively:

```bash
dcc-mcp-gimp install --yes --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
```

The JSON report uses schema version `1`. Exit codes are stable:

| Code | Meaning |
| ---: | --- |
| 0 | Operation completed successfully |
| 10 | Host, version, interpreter, profile, or receipt preflight failed |
| 20 | The bundled plug-in artifact could not be acquired |
| 30 | Install, upgrade, rollback, or uninstall failed |
| 40 | Artifact, import, or readiness verification failed |
| 50 | GIMP restart or lock release is required |

Exit `50` after a first install means the staged files and receipt were
committed, but live readiness still requires the host steps in
`next_steps[]`. Those steps launch a new selected GIMP instance, start the
adapter with the exact selected Python interpreter, and run the context-bound
verification command. Do not treat installed files alone as usable.

## Manual path

The standard command automates plug-in registration by staging the bundled
script into GIMP's profile, moving an existing version to an adjacent backup,
and committing the replacement and receipt. It never deletes the active
plug-in before a staged replacement is available.

If GIMP is outside normal locations, provide the executable or application
bundle with `--dcc-path`. If its companion Python is not suitable for the
adapter, provide the adapter interpreter with `--python`. Both selected paths
are recorded in the plan and receipt.

Set allowed file roots before starting the bridge. Use `;` between roots on
Windows and `:` on macOS or Linux:

```bash
export DCC_MCP_GIMP_ALLOWED_ROOTS=/absolute/project/root
```

Restart GIMP so it discovers the plug-in. The registered no-argument
`python-fu-dcc-mcp-gimp-bridge` persistent procedure starts automatically.
Use the exact commands returned in `next_steps[]`; they start a new selected
host instance, start the adapter with the selected Python, and then verify it.
The adapter command has this form:

```bash
dcc-mcp-gimp
```

The pinned 3.0.8-1 AppImage CI job verifies the official download checksum,
host version, packaged plug-in syntax, and executable bit.
It does not load or register the plug-in. It does not exercise automatic
persistent-procedure startup, start the bridge, or prove live readiness. Those
remain explicit real-host acceptance boundaries.

The plug-in and adapter authenticate through
`~/.dcc-mcp/gimp-bridge-token`. Never copy that token into commands, logs, or
issue reports.

## Verify

With GIMP, its automatically started persistent bridge, and the adapter
running:

```bash
dcc-mcp-gimp verify --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
```

Verification checks, in order:

1. The installed file manifest and version against the receipt and wheel.
2. Importability and version in the selected adapter interpreter.
3. Live readiness through the shared `gimp_session__get_status` probe, bound
   to the exact adapter instance, GIMP PID/start identity and executable,
   receipted plug-in module, version, and authenticated bridge endpoint.

Only `verify.directly_usable: true` proves the adapter is ready. A failed
report includes `failure_stage`, `failure_reason`, and structured
`next_steps[]`.

The receipt is stored at `~/.dcc-mcp/receipts/gimp.json`. This v1 contract
allows one receipted GIMP profile per user: if `--destination` selects a
different profile while that receipt exists, the operation fails closed with
exit `10` and leaves the existing profile untouched. `status --json`
classifies the selected profile as `fresh`, `current`, `upgrade`, `repair`, or
`partial`. The receipt binds the complete owned manifest and the plug-in entry
point executable bit (POSIX); a chmod-only drift is reported as `repair` or a
failed artifact verification.

Receipts created by an earlier v1 build that do not contain
`entry_point_executable` are treated as legacy and are never silently migrated:
`status` reports `repair` (exit `40`), `upgrade --yes` refuses replacement
(exit `30`), and `uninstall --yes` refuses deletion (exit `10`). Run a fresh
`install --yes` after reviewing the profile if the old installation is yours.
All lifecycle mutations re-check the selected profile and every receipted file
by physical identity immediately before changing or removing it; a pathname
or junction/symlink swap fails closed and leaves the foreign object untouched.

When an explicit `--instance-id` or `--host-pid` is supplied, the same values
are retained in the plan and emitted in the `verify-selected-gimp` command in
`next_steps[]`; execute that command verbatim.

## Upgrade

Upgrade the Python package first, inspect the plan, and then replace the
plug-in transactionally:

```bash
python -m pip install --upgrade dcc-mcp-gimp
dcc-mcp-gimp upgrade --dry-run --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
dcc-mcp-gimp upgrade --yes --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
```

If replacement or receipt commit fails, the previous plug-in and receipt are
restored. A lock-related exit `50` leaves the current install in place; close
GIMP and retry.

## Uninstall

Close GIMP, inspect the uninstall plan, and remove only receipt-owned files:

```bash
dcc-mcp-gimp uninstall --dry-run --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
dcc-mcp-gimp uninstall --yes --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
```

Uninstall refuses to remove an unreceipted plug-in and preserves unrelated
files in the GIMP profile. Its validated recovery preserves owned bytes and
POSIX modes if cleanup fails. Run `install --yes` first to repair ownership.

## Troubleshooting

### Host not found or wrong version

Pass the real GIMP 3 executable or application bundle with `--dcc-path`.
`status --json` reports the detected executable and version. GIMP 2.x fails
preflight with exit `10`.

### Target Python or package metadata fails

Pass the interpreter that contains both `dcc-mcp-gimp` and a supported
`dcc-mcp-core` with `--python`. Use that same interpreter to run
`python -m pip install --upgrade dcc-mcp-gimp`.

### `partial`, `repair`, or stale version

`partial` means a plug-in or receipt exists without its matching owner.
`repair` means the receipt-owned files changed. `upgrade` means the intact
installed version is older than the wheel. Review `status --json`, then run
`install --yes` for partial/repair or `upgrade --yes` for upgrade.

### Restart required or Windows lock

Exit `50` is fail-closed. Close every GIMP process, wait for file handles to
release, and repeat the exact command. The installer uses the shared Core lock
inspection and removal contract and never falls back to delete-then-copy.

### Plug-in bootstrap failure inside GIMP

The plug-in records import and bridge-startup failures as JSON lines at
`~/.dcc-mcp/gimp-bootstrap-errors.jsonl`. Override the location with
`DCC_MCP_GIMP_BOOTSTRAP_ERRORS`. Run `dcc-mcp-gimp-doctor` to see the latest
captured error without exposing bridge credentials.

### Installed but not usable

Follow the returned commands to start a new selected GIMP instance, allow its
no-argument persistent bridge to start automatically, start the adapter with
the selected Python, and run `verify --json` again. Inspect
`verify.failure_stage`: `artifact` indicates files/receipt, `import` indicates
the selected Python, and `readiness` indicates the live host/bridge/adapter
path.
