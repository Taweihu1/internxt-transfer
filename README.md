# Internxt Drive Transfer Automation

A Playwright-based CLI for uploading and downloading files/directories to and from
[Internxt Drive](https://drive.internxt.com) (drive.internxt.com).

Internxt end-to-end encrypts file content client-side in the web app's JS, so
there's no way to bridge to a plain HTTP/`requests` session — this script
drives the real drive.internxt.com UI with Playwright and lets the web app do
the crypto itself.

## Features

- **Upload / download** single files or whole directories, creating missing
  remote folders automatically.
- **Resumable**: every transfer is tracked in `transfer_manifest.json`; a
  killed-and-rerun job skips whatever already completed and only retries
  what didn't.
- **Large-file chunking**: files ≥80MB are automatically split into ≤60MB
  pieces on upload (works around a Playwright/Node driver crash on very
  large single-shot uploads) and transparently reconstructed + SHA-256
  verified on download. A killed-and-rerun chunked transfer resumes at the
  chunk level. Each file's chunks + manifest live in their own dedicated
  subfolder (rather than flat alongside every other file's pieces) — this
  avoids Internxt's own web UI becoming unreliable once a folder
  accumulates hundreds of chunked files' worth of pieces.
- **`--verify`**: after upload, downloads the file straight back and
  compares SHA-256 against the source, catching cases where the upload
  looked successful but didn't actually land correctly server-side.
- **Direct single-file downloads**: downloading one file doesn't zip its
  whole containing folder — it selects just that file's checkbox and uses
  Internxt's per-selection toolbar download button.
- **`audit`**: read-only cross-check of every upload the manifest marks
  "done" against what's actually visible on Internxt Drive, to catch a
  file that reported success locally but landed in the wrong folder.
- **Skip-if-identical** (on by default, `--no-skip-existing` to disable):
  before uploading, checks whether the remote folder already has a
  same-named file and, if so, downloads and hashes it — skips re-sending
  if it matches the local source, uploads a new version if it doesn't.
- **`--manifest <path>`**: point at a custom transfer_manifest.json.
  Required when running multiple transfers concurrently (e.g. several
  backgrounded jobs) — each must use its own file, since two processes
  sharing one would silently clobber each other's progress.
- **Live status**: a background ticker prints overall progress and
  in-flight items every N seconds (default 30) during upload/download.

## Requirements

- Python 3.9+
- `playwright` (`pip install playwright`, then `playwright install chromium`
  or point it at an installed Chrome/Brave — see `_BROWSER_CANDIDATES` in
  the script)
- A Chromium-based browser (Brave or Chrome) installed locally

## Usage

```bash
# One-time interactive login — opens a real browser window, saves the
# session to auth_state.json
python internxt_transfer.py login

# Upload a file or directory (remote path may be omitted -> uploads to
# the Drive root)
python internxt_transfer.py upload <local/path> [remote/folder/path] [--interval 30] [--retries 3] [--verify]

# Download a file or directory
python internxt_transfer.py download <remote/path> <local/dest/dir> [--interval 30] [--retries 3]

# Show current transfer manifest
python internxt_transfer.py status

# Read-only audit: confirm every "done" upload actually landed where expected
python internxt_transfer.py audit

# Merge separate --manifest files back into one (after running several
# concurrent transfers, each with its own manifest)
python internxt_transfer.py merge-manifest manifestA.json manifestB.json -o merged.json

# Print visible UI buttons/current folder contents — for selector debugging
python internxt_transfer.py debug-dump --remote <remote/folder/path>
```

## Building a standalone .exe

```bash
pip install pyinstaller
pyinstaller --onefile --console --name internxt_transfer --collect-all playwright internxt_transfer.py
```

`--collect-all playwright` is required — Playwright ships a Node.js driver
(`driver/node.exe`) as package data that PyInstaller won't pick up
otherwise, and the frozen exe will fail at runtime without it. The result
is `dist/internxt_transfer.exe`, fully standalone (no Python install
needed on the target machine) — just a Chromium-based browser (Brave or
Chrome). Run it exactly like the script: `internxt_transfer.exe login`,
`internxt_transfer.exe upload ...`, etc. `auth_state.json` and
`transfer_manifest.json` are created next to the .exe itself.

## Known limitations

- Uploading a single file ≥~100MB in one shot can crash the Playwright
  Node driver (`ERR_STRING_TOO_LONG`) — worked around via automatic
  chunking above `CHUNK_THRESHOLD_BYTES`.
- A very large (~3GB+) single **non-chunked** file's direct download can
  have its browser-side blob transfer canceled — a browser memory/size
  ceiling, not something this script can work around. Chunked files (this
  tool's own uploads) are unaffected since each piece stays well under
  that size.
- Selectors are tuned against a Traditional Chinese (zh-TW) Internxt UI;
  update the `TEXT` dict in the script if your account uses a different
  locale.

## Files

- `internxt_transfer.py` — the CLI, self-contained.
- `auth_state.json` — saved login session (created by `login`, gitignored,
  never commit this — it's equivalent to a live session token).
- `transfer_manifest.json` — per-run transfer/resume state (gitignored).
