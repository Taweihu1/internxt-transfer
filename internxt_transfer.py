#!/usr/bin/env python3
"""
Internxt Drive (drive.internxt.com) upload/download automation.

Internxt end-to-end encrypts file content client-side in the web app's
JS, so we cannot bridge to a plain `requests` session the way the
Gmail/Outlook recipes in the playwright-spa-auth skill do. Instead this
script drives the real drive.internxt.com UI with Playwright and lets
the web app do the crypto itself.

All selectors below were confirmed live against a real Internxt
account (data-cy attributes where the app exposes them, exact visible
text otherwise). Two things that are NOT obvious and matter a lot if
you need to touch this file again:

1. Row navigation into a folder is a SINGLE click on its name, not a
   double-click — double-click seems to do nothing on folders here.
2. Context-menu items (下載/移到垃圾桶/...) MUST be clicked via
   `js_click()` (dispatches el.click() directly in the page), not a
   Playwright mouse click at computed coordinates. Two menu items can
   sit close enough that a coordinate-based click (even force=True)
   lands on the wrong one — this bug silently turned "delete" into
   "move" and made "download" look broken during testing, until
   switching to js_click fixed both.
3. The "下載" right-click context-menu action, confirmed by inspecting
   the actual downloaded bytes, does NOT scope to the row you
   right-clicked once you're inside any subfolder — it zips whatever
   folder you're currently viewing instead. It only downloads exactly
   the clicked item when that item sits at Drive root. This is a real
   Internxt frontend quirk (reproduced with pre-existing,
   non-automation-created files too), not an artifact of this script.
   do_download()'s whole-directory branch uses this quirk on purpose
   (the resulting zip reliably matches exactly that folder's contents).
   Single-file downloads do NOT use this path any more (see point 3b).
3b. Single-file downloads instead select the target row's checkbox
   ([data-cy=driveListItemCheckboxN], N = the row's index in
   list_current_folder()'s output) and click the per-selection toolbar
   download button — confirmed via live DOM inspection that Internxt's
   in-page transfer-progress panel then shows the file's own name and
   byte progress, not the containing folder's, i.e. this genuinely
   downloads just that one item without zipping its whole folder. The
   toolbar download button has no data-cy; it's located by its SVG
   icon path (DL_ICON_PATH_PREFIX) among buttons positioned in the
   toolbar's row — that same icon path also appears hidden inside each
   row's version-history panel, so the bounding-box position filter is
   required, not just the path match. See _download_item_direct().
4. KNOWN LIMITATION, unresolved: uploading files roughly ≥100MB
   reliably crashes the Playwright Node driver itself ~20-40s into the
   upload with a fatal `ERR_STRING_TOO_LONG` ("Cannot create a string
   longer than 0x1fffffe8 characters"). This was confirmed to happen
   even with ZERO Playwright calls in flight during that window (using
   plain time.sleep() instead of page.wait_for_timeout()) — so it is
   not something this script's polling triggers. Something the browser
   pushes to the CDP driver asynchronously during a large upload (most
   likely Internxt's own page code doing something with the whole file
   — e.g. a client-side integrity hash — that ends up serialized into a
   single CDP message) exceeds Node's hard max string length. A 500MB
   upload DID complete successfully end-to-end in testing (verified
   byte-identical via sha256 after download) — but only because the
   browser process kept running and finished the upload on its own
   after the driver had already died; this script had no way to
   observe or report that success. Practical effect: do_upload() will
   raise/mark-failed on large files even though the upload might still
   silently succeed server-side. If you hit this, check the Internxt
   web UI directly before assuming the file is missing, and consider
   this file size range unsupported until Playwright/Node fixes the
   underlying limit or a workaround is found.
5. WORKAROUND for point 4: files >= CHUNK_THRESHOLD_BYTES are split
   into CHUNK_SIZE_BYTES pieces and uploaded individually — each piece
   stays comfortably under the size that triggers the driver crash.
   Chunk/manifest filenames all embed CHUNK_MARKER / MANIFEST_MARKER
   plus an 8-hex id derived from the source file's path+size+mtime, so
   they can't be mistaken for a real file the user uploaded (and the
   same source file always re-derives the same id, which is what lets
   a killed-and-rerun upload resume mid-file at the chunk level instead
   of restarting). do_download() detects a manifest for the requested
   filename and transparently reconstructs + sha256-verifies it; a
   whole-directory download also finds and reassembles any chunked
   files inside it, leaving no chunk fragments behind on success. A
   single-file download of a chunked file (point 3b) downloads each
   chunk individually into a temp dir named after the transfer_id, and
   only deletes that temp dir once reassembly + hash check succeeds —
   so a killed-and-rerun single-file download resumes at the chunk
   level too, same as chunked upload does.
6. `upload --verify` downloads each file straight back after upload
   and compares sha256 against the local source — catches exactly the
   "driver crashed but the browser silently finished the upload
   anyway" scenario from point 4, which otherwise looks like a failure
   even when the data landed fine (or vice versa, corruption that
   looked like success).

Usage:
    python internxt_transfer.py login
    python internxt_transfer.py upload   <local/path> <remote/folder/path> [--interval 30] [--retries 3] [--verify]
    python internxt_transfer.py download <remote/path> <local/dest/dir>   [--interval 30] [--retries 3]
    python internxt_transfer.py status
    python internxt_transfer.py audit
    python internxt_transfer.py debug-dump --remote <remote/folder/path>
"""
import argparse
import hashlib
import json
import shutil
import sys
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://drive.internxt.com"
SCRIPT_DIR = Path(__file__).parent
AUTH_STATE_FILE = SCRIPT_DIR / "auth_state.json"
MANIFEST_FILE = SCRIPT_DIR / "transfer_manifest.json"

# ---------------------------------------------------------------------------
# Confirmed UI selectors. The dialog/menu text is the Traditional Chinese
# label shown for this account's locale (zh-TW) — update here if the
# account language changes.
# ---------------------------------------------------------------------------
CY = {
    "upload_file_button": "[data-cy=topBarUploadFilesButton]",
    "upload_folder_button": "[data-cy=topBarUploadFolderButton]",
    "new_folder_button": "[data-cy=topBarCreateFolderButton]",
    "drive_root": "[data-cy=sideNavDriveIcon]",
    "trash": "[data-cy=sideNavTrashIcon]",
}
TEXT = {
    "new_folder_default_name": "無標題文件夾",
    "create_confirm": "創建",
    "cancel": "取消",
    "download_menu_item": "下載",
    "trash_menu_item": "移到垃圾桶",
}
SPINNER_SELECTOR = "[class*='spinner'], [class*='loading'], [role='progressbar']"
ROW_NAME_SELECTOR = "p.truncate"

# Per-selection toolbar "download" icon (see module docstring point 3b).
# No data-cy; identified by its SVG path (a stable Phosphor "tray + down
# arrow" icon). The same path also appears hidden inside each row's
# version-history panel, so callers must additionally filter by the
# toolbar's on-screen position (see _click_toolbar_download_button).
DL_ICON_PATH_PREFIX = "M224,144v64a8,8,0,0,1-8,8H40"
DOWNLOAD_EVENT_TIMEOUT_MS = 5_400_000  # 90 min ceiling for one large file/chunk

# Chunking (see module docstring point 5). 60MB/80MB chosen with a
# safety margin below the confirmed-crashing ~100MB, not an exact
# measured boundary — the real threshold was never pinned down more
# precisely than "100MB reliably crashes, 50MB reliably didn't".
CHUNK_THRESHOLD_BYTES = 80 * 1024 * 1024
CHUNK_SIZE_BYTES = 60 * 1024 * 1024
CHUNK_MARKER = ".ixtchunk-"
MANIFEST_MARKER = ".ixtchunk-manifest."
DOWNLOAD_TMP_ZIP = SCRIPT_DIR / "_download_tmp.zip"

_BROWSER_CANDIDATES = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/brave-browser",
    "/usr/bin/google-chrome",
]


def safe_close(browser):
    """browser.close() itself raises if the Playwright Node driver has
    already died (e.g. the ERR_STRING_TOO_LONG crash seen with large
    uploads) — swallow that so the script can still exit cleanly and
    report the real error instead of an unrelated traceback on cleanup."""
    try:
        browser.close()
    except Exception:
        pass


def find_browser():
    for p in _BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def js_click(locator, timeout=30000):
    """Dispatch a real DOM click directly on the resolved element.
    Playwright's coordinate-based click (even force=True) can land on a
    neighboring element when two rows/menu items sit close together —
    this bit us on both delete and download during testing."""
    locator.evaluate("el => el.click()", timeout=timeout)


def right_click_row(page, locator):
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError("row not visible, cannot right-click")
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.wait_for_timeout(250)
    page.mouse.click(cx, cy, button="right")
    page.wait_for_timeout(500)


def sha256_file(path: Path, block=1024 * 1024, on_progress=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
            if on_progress:
                on_progress(len(b))
    return h.hexdigest()


def _chunk_transfer_id(abs_path: Path, size: int, mtime_ns: int) -> str:
    """Deterministic from path+size+mtime: reruns of the same unchanged
    file always land on the same id (so resume finds the same chunk
    names), while a genuinely different/edited file gets a fresh id
    instead of mixing old and new chunks together."""
    return hashlib.sha1(f"{abs_path}:{size}:{mtime_ns}".encode()).hexdigest()[:8]


def _chunk_name(original_name, i, n, transfer_id):
    return f"{original_name}{CHUNK_MARKER}{i:04d}-of-{n:04d}.{transfer_id}"


def _manifest_name(original_name, transfer_id):
    return f"{original_name}{MANIFEST_MARKER}{transfer_id}.json"


def _find_manifest_entry(names, original_name):
    prefix = f"{original_name}{MANIFEST_MARKER}"
    for n in names:
        if n.startswith(prefix) and n.endswith(".json"):
            return n
    return None


# ---------------------------------------------------------------------------
# Manifest — enables resume: files already marked "done" are skipped on the
# next run, so killing the script and re-running upload/download with the
# same --local/--remote continues where it left off.
# ---------------------------------------------------------------------------
class Manifest:
    def __init__(self, path=MANIFEST_FILE):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self._lock = threading.Lock()

    def status(self, key):
        return self.data.get(key, {}).get("status", "pending")

    def mark(self, key, status, size=None, error=None):
        with self._lock:
            entry = self.data.setdefault(key, {})
            entry["status"] = status
            entry["ts"] = datetime.now().isoformat(timespec="seconds")
            if size is not None:
                entry["size"] = size
            entry["error"] = error
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def summary(self):
        done = sum(1 for v in self.data.values() if v["status"] == "done")
        failed = sum(1 for v in self.data.values() if v["status"] == "failed")
        pending = sum(1 for v in self.data.values() if v["status"] in ("pending", "uploading", "downloading"))
        return done, failed, pending

    def print_status(self):
        if not self.data:
            print("尚無傳輸紀錄。")
            return
        done, failed, pending = self.summary()
        print(f"總計: {len(self.data)}  完成: {done}  失敗: {failed}  待處理: {pending}\n")
        for key, entry in self.data.items():
            err = f"  錯誤: {entry['error']}" if entry.get("error") else ""
            print(f"  [{entry['status']:<10}] {key}  ({entry.get('ts', '')}){err}")


# ---------------------------------------------------------------------------
# Status ticker — background thread, prints overall + in-flight files
# every `interval` seconds (default 30s per requirement).
# ---------------------------------------------------------------------------
class StatusTicker:
    def __init__(self, manifest, interval=30, job_keys=None):
        self.manifest = manifest
        self.interval = interval
        self.job_keys = job_keys or []
        self.current = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._print()

    def set_current(self, name, action=None):
        with self._lock:
            if action is None:
                self.current.pop(name, None)
            else:
                self.current[name] = action

    def _run(self):
        while not self._stop.wait(self.interval):
            self._print()

    def _print(self):
        done = sum(1 for k in self.job_keys if self.manifest.status(k) == "done")
        failed = sum(1 for k in self.job_keys if self.manifest.status(k) == "failed")
        total = len(self.job_keys)
        with self._lock:
            active = list(self.current.items())
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] 進度: {done}/{total} 完成, {failed} 失敗, {total - done - failed} 待處理/進行中")
        if active:
            for name, action in active:
                print(f"   -> 正在{action}: {name}")
        else:
            print("   (目前沒有檔案在傳輸中)")
        sys.stdout.flush()


def _sha256_file_with_progress(path: Path, ticker: StatusTicker, key: str, label: str = "計算雜湊中") -> str:
    """sha256_file with a live percentage shown via the ticker. Hashing a
    multi-GB file (a chunked upload's source, or a reconstructed
    download) can take many minutes on a slow disk with nothing else
    touching the ticker in that window — without this, the status
    ticker prints "沒有檔案在傳輸中" the whole time, which reads exactly
    like a hang even though it's just an invisible, legitimately slow
    read+hash."""
    total = path.stat().st_size
    read = 0
    last_pct = -1

    def on_progress(n):
        nonlocal read, last_pct
        read += n
        pct = int(read * 100 / total) if total else 100
        if pct != last_pct:
            last_pct = pct
            ticker.set_current(key, f"{label} {pct}%")

    ticker.set_current(key, f"{label} 0%")
    try:
        return sha256_file(path, on_progress=on_progress)
    finally:
        ticker.set_current(key, None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def cmd_login(playwright):
    exe = find_browser()
    browser = playwright.chromium.launch(headless=False, executable_path=exe)
    ctx = browser.new_context()
    page = ctx.new_page()
    print("開啟 drive.internxt.com — 請在瀏覽器中完成登入 (含兩步驟驗證如果有的話)。")
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    input("登入完成、看到雲端硬碟主畫面後，回到這個終端機按 Enter ... ")
    ctx.storage_state(path=str(AUTH_STATE_FILE))
    print(f"登入狀態已儲存 -> {AUTH_STATE_FILE}")
    safe_close(browser)


def open_authenticated_page(playwright, headless=True):
    if not AUTH_STATE_FILE.exists():
        sys.exit("找不到登入狀態，請先執行: python internxt_transfer.py login")
    exe = find_browser()
    browser = playwright.chromium.launch(headless=headless, executable_path=exe)
    ctx = browser.new_context(storage_state=str(AUTH_STATE_FILE), accept_downloads=True)
    page = ctx.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(8000)
    return browser, ctx, page


# ---------------------------------------------------------------------------
# Folder navigation
# ---------------------------------------------------------------------------
def list_current_folder(page):
    """Names of files/folders visible in the current folder view."""
    page.wait_for_timeout(800)
    names = page.evaluate(
        f"""() => Array.from(document.querySelectorAll('{ROW_NAME_SELECTOR}'))
            .filter(e => e.offsetParent !== null)
            .map(e => e.textContent.trim())
            .filter(Boolean)"""
    )
    return list(dict.fromkeys(names))


def _folder_row(page, name):
    return page.get_by_text(name, exact=True).first


def _open_row(page, name, timeout=8000):
    """Enter a folder (or open a file) — single click on its name."""
    row = _folder_row(page, name)
    row.wait_for(timeout=timeout)
    js_click(row)
    page.wait_for_timeout(1500)


def _folder_exists_here(page, name, timeout=4000):
    try:
        page.get_by_text(name, exact=True).first.wait_for(timeout=timeout)
        return True
    except PWTimeout:
        return False


def _create_folder_here(page, name):
    js_click(page.locator(CY["new_folder_button"]))
    page.wait_for_timeout(800)
    inp = page.locator(f"input[value='{TEXT['new_folder_default_name']}']")
    if inp.count() == 0:
        # fallback: first visible text input inside the open dialog
        inp = page.locator("[role=dialog] input[type=text]").first
    inp.click()
    page.keyboard.press("Control+A")
    page.keyboard.type(name)
    js_click(page.get_by_text(TEXT["create_confirm"], exact=True).first)
    page.wait_for_timeout(2000)


def navigate_existing_only(page, remote_path, hard_reload=False):
    """Like ensure_folder_path but never creates anything, and verifies
    every segment is actually a folder (not just a row with matching
    text) — clicking a segment must change the listing, otherwise it
    was a file and we stop short. Returns False if any segment is
    missing or turns out to be a file rather than a folder; this is
    how do_download() tells apart "path is a directory" from "path is
    a file inside its parent directory."

    hard_reload=True does a real page.goto() instead of a client-side
    SPA navigation click — cheap extra safety against any stale UI
    state between calls."""
    if hard_reload:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5000)
    else:
        js_click(page.locator(CY["drive_root"]))
        page.wait_for_timeout(1500)
    segments = [s for s in remote_path.strip("/").split("/") if s]
    for seg in segments:
        if not _folder_exists_here(page, seg):
            print(f"   ('{seg}' 不存在，停在上一層)")
            return False
        before = list_current_folder(page)
        _open_row(page, seg)
        after = list_current_folder(page)
        if after == before:
            return False  # click did nothing to the listing -> seg is a file, not a folder
    return True


def _settle_new_folder(page, segments, attempts=5):
    """Hard-reload and re-navigate the full path from scratch, retrying if a
    segment isn't visible yet. Confirms a just-created folder has actually
    persisted server-side before any upload starts — guards against a race
    where the first file uploaded into a brand-new folder reports success
    client-side but silently never lands on the server (seen with
    file_a_500mb.bin's chunk 1/9 in the two-large-file directory test)."""
    for attempt in range(attempts):
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        ok = True
        for seg in segments:
            if not _folder_exists_here(page, seg, timeout=4000):
                ok = False
                break
            _open_row(page, seg)
        if ok:
            return
        print(f"   （新資料夾尚未穩定，重新確認中 {attempt + 1}/{attempts} ...）")
        page.wait_for_timeout(2000)
    # Must raise rather than warn-and-continue: the caller (ensure_folder_path)
    # trusts that returning means `page` is positioned inside the target
    # folder. Silently giving up here previously left `page` wherever the
    # last failed segment-click landed (often one level UP, in the PARENT
    # folder) with no error — do_upload would then proceed to upload the
    # file there, reporting success (the printed path is just the local
    # relative path, not verified against the actual remote location).
    # This is the confirmed cause of a file (IMG_0252.jpg) landing outside
    # its intended freshly-created folder without any failure being logged.
    raise RuntimeError(f"建立資料夾後多次確認仍不穩定（路徑: {'/'.join(segments)}），"
                        f"為避免上傳到錯誤位置而中止，這次嘗試會被視為失敗並重試")


def ensure_folder_path(page, remote_path):
    """Navigate to remote_path from Drive root, creating any missing
    segment along the way. Leaves `page` inside the target folder.

    Verifies every single _open_row click actually changed the folder
    listing (same check navigate_existing_only already did for
    downloads) — a click that silently does nothing previously left
    `page` one level up from where do_upload assumed it was, with no
    error raised, causing files to upload into the wrong (parent)
    folder. Raises RuntimeError instead so the caller's existing
    per-file retry/failure handling catches it."""
    js_click(page.locator(CY["drive_root"]))
    page.wait_for_timeout(1500)
    segments = [s for s in remote_path.strip("/").split("/") if s]
    created_any = False
    for seg in segments:
        before = list_current_folder(page)
        if _folder_exists_here(page, seg):
            _open_row(page, seg)
        else:
            print(f"   建立遠端資料夾: {seg}")
            _create_folder_here(page, seg)
            _open_row(page, seg)
            created_any = True
        after = list_current_folder(page)
        if after == before:
            raise RuntimeError(f"進入資料夾 '{seg}' 似乎沒有生效（列表沒有變化），"
                                f"為避免上傳到錯誤位置而中止")
    if created_any:
        _settle_new_folder(page, segments)
    return segments


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
MIN_UPLOAD_THROUGHPUT_BPS = 1_000_000  # conservative 1MB/s floor, see _wait_upload_settle


def _wait_upload_settle(page, filename, file_size_bytes, timeout=7200):
    """Wait for an upload to actually finish — NOT just for the row to
    appear.

    Internxt adds the row to the listing almost immediately (optimistic
    UI) while the real encrypt+upload keeps running in the background
    with no spinner/progress element this script could find a stable
    selector for. Confirmed by testing: a 500MB upload was reported
    "done" within ~2s by a naive presence+no-spinner check, while the
    file's real completion timestamp in the Drive UI was ~4 minutes
    later — closing the browser on that false-positive risks aborting
    a genuinely still-running upload.

    So: enforce a size-scaled minimum wait (assuming a conservative
    1MB/s floor — real throughput observed in testing was ~2MB/s) on
    top of the presence+no-spinner+no-visible-percentage check, rather
    than trusting the DOM signal alone."""
    min_wait = max(10, file_size_bytes / MIN_UPLOAD_THROUGHPUT_BPS)
    start = time.time()
    deadline = start + max(timeout, min_wait + 60)

    # Don't touch the page at all until min_wait has elapsed. Polling
    # the DOM while a big encrypt+upload is in flight risks the
    # Playwright Node driver itself crashing with a fatal
    # ERR_STRING_TOO_LONG (hit during testing with a 100MB upload,
    # after ~2 polls) — some in-page state Playwright has to serialize
    # over CDP grows with the file and can exceed Node's max string
    # length. Waiting it out untouched avoids poking that path
    # entirely for the bulk of the transfer.
    remaining = min_wait - (time.time() - start)
    if remaining > 0:
        time.sleep(remaining)

    stable = 0
    while time.time() < deadline:
        present = page.get_by_text(filename, exact=True).count() > 0
        spinners = page.locator(SPINNER_SELECTOR).count()
        if present and spinners == 0:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        page.wait_for_timeout(8000)
    return False


def _upload_one(page, abs_path: Path, ticker: StatusTicker, key: str, label="上傳"):
    ticker.set_current(key, label)
    try:
        js_click(page.locator(CY["upload_file_button"]))
        page.wait_for_timeout(400)
        file_input = page.locator("input[type=file]:not([webkitdirectory])").last
        file_input.set_input_files(str(abs_path))
        return _wait_upload_settle(page, abs_path.name, abs_path.stat().st_size)
    finally:
        ticker.set_current(key, None)


def _upload_chunked(page, abs_path: Path, ticker: StatusTicker, key: str, manifest: Manifest):
    """Upload a large file as distinctively-named chunks + a manifest
    instead of one single upload (see module docstring point 5).
    Each chunk's own upload goes through the normal _upload_one /
    _wait_upload_settle path, so it's independently resumable: a
    chunk already marked "done" in the local manifest on a rerun is
    skipped, and the transfer id is derived deterministically from the
    source file so a rerun finds the exact same chunk names."""
    size = abs_path.stat().st_size
    transfer_id = _chunk_transfer_id(abs_path, size, abs_path.stat().st_mtime_ns)
    n = -(-size // CHUNK_SIZE_BYTES)  # ceil division
    original_name = abs_path.name

    tmp_dir = SCRIPT_DIR / f"_chunk_tmp_{transfer_id}"
    tmp_dir.mkdir(exist_ok=True)
    try:
        with open(abs_path, "rb") as f:
            for i in range(1, n + 1):
                chunk_key = f"{key}::chunk{i}of{n}::{transfer_id}"
                if manifest.status(chunk_key) == "done":
                    continue
                f.seek((i - 1) * CHUNK_SIZE_BYTES)
                data = f.read(CHUNK_SIZE_BYTES)
                cname = _chunk_name(original_name, i, n, transfer_id)
                chunk_path = tmp_dir / cname
                chunk_path.write_bytes(data)
                try:
                    ok = _upload_one(page, chunk_path, ticker, key, label=f"上傳分片 {i}/{n}")
                finally:
                    chunk_path.unlink(missing_ok=True)
                if not ok:
                    raise RuntimeError(f"分片 {i}/{n} 上傳逾時或未偵測到完成狀態")
                manifest.mark(chunk_key, "done", size=len(data))
    finally:
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

    file_sha256 = _sha256_file_with_progress(abs_path, ticker, key)

    manifest_obj = {
        "original_name": original_name,
        "size": size,
        "sha256": file_sha256,
        "chunk_count": n,
        "chunks": [_chunk_name(original_name, i, n, transfer_id) for i in range(1, n + 1)],
        "transfer_id": transfer_id,
    }
    manifest_path = SCRIPT_DIR / _manifest_name(original_name, transfer_id)
    manifest_path.write_text(json.dumps(manifest_obj, ensure_ascii=False), encoding="utf-8")
    try:
        ok = _upload_one(page, manifest_path, ticker, key, label="上傳分片清單")
    finally:
        manifest_path.unlink(missing_ok=True)
    if not ok:
        raise RuntimeError("分片清單 (manifest) 上傳逾時或未偵測到完成狀態")
    return True


def list_local_files(local_path: Path):
    """Returns [(abs_path, relative_posix_path)]. Single file -> one entry."""
    if local_path.is_file():
        return [(local_path, local_path.name)]
    out = []
    for p in sorted(local_path.rglob("*")):
        if p.is_file():
            out.append((p, p.relative_to(local_path).as_posix()))
    return out


def _verify_upload(page, remote_dir, abs_path: Path, ticker: StatusTicker, key: str):
    """Download the just-uploaded file straight back and compare sha256
    against the local source — see module docstring point 6. Raises on
    mismatch (including "couldn't even find it remotely"), which the
    caller treats as an upload failure eligible for retry."""
    tmp_path = SCRIPT_DIR / f"_verify_tmp_{abs_path.name}"
    expected = _sha256_file_with_progress(abs_path, ticker, key, label="計算本機雜湊中")
    ticker.set_current(key, "驗證中")
    try:
        chunk_verified_hash = _download_and_extract_file(page, remote_dir, abs_path.name, tmp_path, ticker)
        actual = chunk_verified_hash or _sha256_file_with_progress(tmp_path, ticker, key, label="計算下載後雜湊中")
        if actual != expected:
            raise RuntimeError(f"本機雜湊 {expected} != 下載回來後的雜湊 {actual}")
    finally:
        tmp_path.unlink(missing_ok=True)
        ticker.set_current(key, None)


def do_upload(args):
    local_path = Path(args.local).resolve()
    if not local_path.exists():
        sys.exit(f"本機路徑不存在: {local_path}")

    files = list_local_files(local_path)
    if not files:
        sys.exit("本機路徑底下沒有可上傳的檔案。")

    manifest = Manifest()
    job_keys = [f"upload::{args.remote}::{rel}" for _, rel in files]
    ticker = StatusTicker(manifest, interval=args.interval, job_keys=job_keys)
    ticker.start()

    with sync_playwright() as pw:
        browser, ctx, page = open_authenticated_page(pw, headless=True)
        try:
            folder_cache = set()
            for (abs_path, rel), key in zip(files, job_keys):
                if manifest.status(key) == "done":
                    continue
                # rel is posix-style (forward slashes, see list_local_files);
                # split as a plain string rather than round-tripping through
                # Path(rel).parent — on Windows, str(WindowsPath(...)) always
                # renders with backslashes, which would silently corrupt a
                # multi-level relative path (e.g. "ATM/driver/ccid/x.exe")
                # into a single wrong segment "ATM\driver\ccid" once joined
                # back into a "/"-separated remote_dir.
                rel_parent = "/".join(rel.split("/")[:-1])
                remote_dir = f"{args.remote.strip('/')}/{rel_parent}".strip("/") if rel_parent else args.remote

                attempts = 0
                ok = False
                last_err = None
                while attempts < args.retries and not ok:
                    attempts += 1
                    try:
                        if remote_dir not in folder_cache:
                            print(f"進入/建立遠端目錄: {remote_dir or '/'}")
                        ensure_folder_path(page, remote_dir)
                        folder_cache.add(remote_dir)
                        if abs_path.stat().st_size >= CHUNK_THRESHOLD_BYTES:
                            ok = _upload_chunked(page, abs_path, ticker, key, manifest)
                        else:
                            ok = _upload_one(page, abs_path, ticker, key)
                        if not ok:
                            last_err = "上傳逾時或未偵測到完成狀態"
                        elif args.verify:
                            try:
                                _verify_upload(page, remote_dir, abs_path, ticker, key)
                            except Exception as ve:
                                ok = False
                                last_err = f"驗證失敗: {ve}"
                    except Exception as e:
                        last_err = str(e)
                        if "Connection closed" in last_err or "has been closed" in last_err:
                            print(f"   Playwright 驅動程式連線已中斷(可能是大檔案觸發已知的 Node.js")
                            print(f"   ERR_STRING_TOO_LONG 限制,見檔案開頭文件說明)。上傳可能仍在")
                            print(f"   瀏覽器背景繼續進行 —— 請直接到 https://drive.internxt.com")
                            print(f"   確認 {rel} 是否已經上傳成功，這個連線已經壞了，重試也沒用。")
                            break
                    if not ok:
                        print(f"   第 {attempts} 次嘗試失敗: {last_err}")
                        if attempts < args.retries:
                            print(f"   重試 {rel} ({attempts}/{args.retries}) ...")

                if ok:
                    manifest.mark(key, "done", size=abs_path.stat().st_size)
                    print(f"完成上傳: {rel}")
                else:
                    manifest.mark(key, "failed", error=last_err)
                    print(f"上傳失敗: {rel} -> {last_err}")
        finally:
            ticker.stop()
            safe_close(browser)

    manifest.print_status()


# ---------------------------------------------------------------------------
# Download
#
# See module docstring points 3/3b. Whole-directory downloads still zip
# the current folder on purpose (that's exactly what you want for a
# directory target). Single-file downloads instead select just that
# row's checkbox and use the toolbar download button, so they never pull
# down or depend on the rest of the folder's contents.
# ---------------------------------------------------------------------------
def _download_current_folder_zip(page, tmp_path: Path, ticker: StatusTicker, label: str):
    ticker.set_current(label, "下載")
    try:
        names = list_current_folder(page)
        if not names:
            raise RuntimeError("目前資料夾是空的，沒有東西可以下載")
        row = _folder_row(page, names[0])
        right_click_row(page, row)
        dl_item = page.get_by_text(TEXT["download_menu_item"], exact=True).first
        dl_item.wait_for(timeout=8000)
        with page.expect_download(timeout=DOWNLOAD_EVENT_TIMEOUT_MS) as dl_info:
            js_click(dl_item)
        dl_info.value.save_as(str(tmp_path))
        # The right-click context menu can leave an invisible backdrop
        # behind that swallows unrelated clicks elsewhere on the page
        # (confirmed: it silently ate a later click on the sidebar's
        # "雲端硬碟" icon, timing out 15s later with no obvious link
        # back to this download). Press Escape and click a neutral
        # spot so nothing lingers for whatever runs next.
        page.keyboard.press("Escape")
        page.mouse.click(5, 5)
        page.wait_for_timeout(300)
        return tmp_path
    finally:
        ticker.set_current(label, None)


def _reassemble_chunks_in_dir(local_dir: Path, ticker: StatusTicker):
    """Post-process a freshly-extracted directory: find any chunk
    manifests (see docstring point 5), reconstruct + sha256-verify the
    original file from its chunks, and remove the fragments so the
    directory looks like a normal download with no trace of chunking.
    Raises if a manifest's chunks are incomplete or the hash mismatches
    — leaves fragments in place in that case rather than deleting
    evidence of a bad download."""
    for manifest_path in list(local_dir.rglob(f"*{MANIFEST_MARKER}*.json")):
        try:
            obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        folder = manifest_path.parent
        original_name = obj["original_name"]
        chunk_paths = [folder / c for c in obj["chunks"]]
        if not all(p.exists() for p in chunk_paths):
            print(f"   警告: {original_name} 的分片不齊全，保留原始分片檔案")
            continue
        dest = folder / original_name
        with open(dest, "wb") as out:
            for p in chunk_paths:
                out.write(p.read_bytes())
        actual = _sha256_file_with_progress(dest, ticker, original_name, label="計算重組後雜湊中")
        if actual != obj["sha256"]:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"{original_name} 分片重組後雜湊不符（預期 {obj['sha256']}，實際 {actual}）")
        for p in chunk_paths:
            p.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        print(f"   已還原分片檔案: {original_name}")


def _click_toolbar_download_button(page):
    """Click Internxt's per-selection toolbar download button (see
    docstring point 3b). It has no data-cy, so it's found by its SVG
    icon path AND filtered to the visible toolbar's on-screen position —
    the same icon path also appears hidden inside each row's
    version-history panel, which would otherwise match first. Retries a
    few times with a short wait between attempts: the toolbar can take
    a moment to render after the checkbox click, and a fixed short wait
    was observed to be flaky under automation (worked reliably in
    interactive manual testing, occasionally missed in scripted runs)."""
    js = f"""() => {{
        const btns = Array.from(document.querySelectorAll('button'))
            .filter(b => {{ const r = b.getBoundingClientRect(); return r.top < 130 && r.top > 90 && r.width > 0; }});
        const target = btns.find(b => b.querySelector('svg path[d^="{DL_ICON_PATH_PREFIX}"]'));
        if (target) {{ target.click(); return true; }}
        return false;
    }}"""
    for attempt in range(4):
        if page.evaluate(js):
            return
        page.wait_for_timeout(800)
    raise RuntimeError("找不到工具列的下載按鈕（可能是選取失敗，或 Internxt UI 已變更）")


def _clear_selection(page):
    """Deselect any currently-checked row checkboxes. A prior
    _download_item_direct call leaves its row checked; selecting a new
    row on top of that creates a multi-selection, which changes the
    toolbar (share/copy-link icons disappear for multi-select) enough
    to break the position-based lookup in _click_toolbar_download_button
    (observed: worked for chunk 1, then failed for chunk 2 right after
    it — chunk 1's checkbox was still checked when chunk 2 was clicked).
    Toggling the header checkbox twice (select-all, then deselect-all)
    reliably clears any partial selection regardless of its prior state.
    """
    hdr_cb = page.locator("[data-cy=driveListHeaderCheckbox]")
    if hdr_cb.count():
        js_click(hdr_cb)
        page.wait_for_timeout(300)
        js_click(hdr_cb)
        page.wait_for_timeout(300)


def _download_item_direct(page, name, dest_path: Path, ticker: StatusTicker, label=None):
    """Download exactly one row: select its checkbox, click the toolbar
    download button (see docstring point 3b), save the resulting file.
    Does not zip or otherwise touch the rest of the current folder."""
    ticker.set_current(label or name, "下載")
    try:
        names = list_current_folder(page)
        if name not in names:
            raise RuntimeError(f"找不到要下載的項目: {name}")
        idx = names.index(name)
        _clear_selection(page)
        js_click(page.locator(f"[data-cy=driveListItemCheckbox{idx}]"))
        page.wait_for_timeout(1200)
        with page.expect_download(timeout=DOWNLOAD_EVENT_TIMEOUT_MS) as dl_info:
            _click_toolbar_download_button(page)
        dl_info.value.save_as(str(dest_path))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        return dest_path
    finally:
        ticker.set_current(label or name, None)


def _download_and_extract_file(page, remote_dir, target_name, dest_path: Path, ticker: StatusTicker):
    """Download exactly target_name from remote_dir directly (see
    _download_item_direct / docstring point 3b) — reconstructing it
    from chunks + sha256-verifying if it turns out to be a chunked
    upload rather than a plain file. Returns the verified sha256 if it
    was chunked, else None (caller can hash dest_path itself if needed).
    """
    if not navigate_existing_only(page, remote_dir, hard_reload=True):
        raise RuntimeError(f"遠端目錄 {remote_dir or '/'} 不存在")
    names = list_current_folder(page)

    if target_name in names:
        _download_item_direct(page, target_name, dest_path, ticker, label=target_name)
        return None

    manifest_entry = _find_manifest_entry(names, target_name)
    if manifest_entry:
        tid = manifest_entry.split(MANIFEST_MARKER)[-1].split(".json")[0]
        tmp_dir = SCRIPT_DIR / f"_dl_chunk_tmp_{tid}"
        tmp_dir.mkdir(exist_ok=True)

        manifest_path = tmp_dir / manifest_entry
        if not manifest_path.exists():
            _download_item_direct(page, manifest_entry, manifest_path, ticker,
                                   label=f"{target_name} (分片清單)")
        obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        n = len(obj["chunks"])
        for i, cname in enumerate(obj["chunks"], 1):
            cpath = tmp_dir / cname
            if cpath.exists():
                continue  # already fetched by a previous, interrupted attempt
            if cname not in names:
                raise RuntimeError(f"找不到分片 {cname}")
            _download_item_direct(page, cname, cpath, ticker, label=f"{target_name} 分片 {i}/{n}")

        with open(dest_path, "wb") as out:
            for cname in obj["chunks"]:
                out.write((tmp_dir / cname).read_bytes())
        actual = _sha256_file_with_progress(dest_path, ticker, target_name, label="計算重組後雜湊中")
        if actual != obj["sha256"]:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"分片重組後雜湊不符（預期 {obj['sha256']}，實際 {actual}）")
        shutil.rmtree(tmp_dir, ignore_errors=True)  # only clean up after full success
        return actual

    raise RuntimeError(f"在 {remote_dir or '/'} 底下找不到 {target_name}（也沒有對應的分片清單）")


def do_download(args):
    local_dest = Path(args.local).resolve()
    local_dest.mkdir(parents=True, exist_ok=True)

    remote = args.remote.strip("/")
    segments = [s for s in remote.split("/") if s]
    if not segments:
        sys.exit("--remote 不能是空的或根目錄。")

    manifest = Manifest()
    key = f"download::{remote}"

    with sync_playwright() as pw:
        browser, ctx, page = open_authenticated_page(pw, headless=True)
        try:
            if manifest.status(key) == "done":
                print(f"{remote} 先前已完成下載，略過（刪除 transfer_manifest.json 中對應項目可強制重下）。")
                manifest.print_status()
                return

            job_keys = [key]
            ticker = StatusTicker(manifest, interval=args.interval, job_keys=job_keys)
            ticker.start()

            attempts = 0
            ok = False
            last_err = None
            while attempts < args.retries and not ok:
                attempts += 1
                try:
                    if navigate_existing_only(page, remote, hard_reload=True):
                        # remote is itself a real, enterable directory
                        _download_current_folder_zip(page, DOWNLOAD_TMP_ZIP, ticker, remote)
                        try:
                            with zipfile.ZipFile(DOWNLOAD_TMP_ZIP) as zf:
                                zf.extractall(local_dest)
                        finally:
                            DOWNLOAD_TMP_ZIP.unlink(missing_ok=True)
                        _reassemble_chunks_in_dir(local_dest, ticker)
                        print(f"完成下載目錄: {remote} -> {local_dest}")
                    else:
                        # last segment didn't navigate anywhere -> treat as a filename
                        parent = "/".join(segments[:-1])
                        target_name = segments[-1]
                        _download_and_extract_file(page, parent, target_name, local_dest / target_name, ticker)
                        print(f"完成下載檔案: {remote} -> {local_dest / target_name}")
                    ok = True
                except Exception as e:
                    last_err = str(e)
                if not ok and attempts < args.retries:
                    print(f"   重試 {remote} ({attempts}/{args.retries}) ...")

            if ok:
                manifest.mark(key, "done")
            else:
                manifest.mark(key, "failed", error=last_err)
                print(f"下載失敗: {remote} -> {last_err}")

            ticker.stop()
        finally:
            safe_close(browser)

    manifest.print_status()


# ---------------------------------------------------------------------------
# Audit
#
# Cross-checks transfer_manifest.json's "done" upload entries against what
# is actually visible on Internxt — catches files that report success
# locally but never actually landed in their intended remote folder. This
# was a real, confirmed failure mode: a navigation click inside
# ensure_folder_path could silently do nothing, leaving the upload land
# one level up (in the parent folder) with no error raised (see the
# before/after listing check added to ensure_folder_path). Read-only:
# never moves, re-uploads, or modifies anything, remote or local.
# ---------------------------------------------------------------------------
def cmd_audit(args):
    if not MANIFEST_FILE.exists():
        print("找不到 transfer_manifest.json，沒有任何上傳紀錄可稽核。")
        return

    manifest_data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    by_dir = defaultdict(list)
    for key, val in manifest_data.items():
        if not key.startswith("upload::") or "::chunk" in key:
            continue  # per-chunk sub-entries aren't independently placed files
        if val.get("status") != "done":
            continue
        rest = key[len("upload::"):]
        remote, rel = rest.split("::", 1)
        parts = rel.split("/")
        filename = parts[-1]
        rel_parent = "/".join(parts[:-1])
        remote_dir = f"{remote.strip('/')}/{rel_parent}".strip("/") if rel_parent else remote
        by_dir[remote_dir].append(filename)

    total = sum(len(v) for v in by_dir.values())
    if total == 0:
        print("transfer_manifest.json 裡沒有任何已完成的上傳紀錄可稽核。")
        return
    print(f"共 {total} 個已標記完成的檔案，分布在 {len(by_dir)} 個遠端資料夾，開始逐一核對...")

    missing = []
    with sync_playwright() as pw:
        browser, ctx, page = open_authenticated_page(pw, headless=True)
        try:
            for i, (remote_dir, filenames) in enumerate(sorted(by_dir.items()), 1):
                print(f"[{i}/{len(by_dir)}] 檢查 {remote_dir or '/'} ({len(filenames)} 個檔案) ...")
                ok = navigate_existing_only(page, remote_dir, hard_reload=True) if remote_dir else True
                if remote_dir and not ok:
                    for filename in filenames:
                        missing.append((remote_dir, filename, "資料夾本身找不到或導航失敗"))
                    continue
                names = list_current_folder(page)
                for filename in filenames:
                    if filename in names or _find_manifest_entry(names, filename):
                        continue
                    missing.append((remote_dir, filename, "資料夾列表中找不到這個檔名"))
        finally:
            safe_close(browser)

    print()
    print(f"稽核完成: 共檢查 {total} 個檔案")
    if missing:
        print(f"發現 {len(missing)} 個可能放錯位置或遺失的檔案:")
        for remote_dir, filename, reason in missing:
            print(f"  - 預期位置: {remote_dir or '/'}/{filename}  原因: {reason}")
    else:
        print("沒有發現任何放錯位置或遺失的檔案。")


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def cmd_debug_dump(args):
    with sync_playwright() as pw:
        browser, ctx, page = open_authenticated_page(pw, headless=False)
        try:
            if args.remote:
                navigate_existing_only(page, args.remote)
            page.wait_for_timeout(1000)
            buttons = page.evaluate(
                """() => Array.from(document.querySelectorAll('button,[role="button"],[data-cy],[aria-label]'))
                    .filter(b => b.offsetParent !== null)
                    .map(b => ({cy: b.getAttribute('data-cy'),
                                label: b.getAttribute('aria-label') || b.textContent.trim().slice(0,40)}))
                    .filter(b => b.label || b.cy)"""
            )
            print("=== 可見按鈕 ===")
            for b in buttons:
                print(b)
            print("\n=== 目前資料夾內容 ===")
            for r in list_current_folder(page):
                print(" -", r)
            page.screenshot(path=str(SCRIPT_DIR / "debug_screenshot.png"), full_page=True)
            print(f"\n已存截圖 -> {SCRIPT_DIR / 'debug_screenshot.png'}")
            input("按 Enter 關閉瀏覽器 ... ")
        finally:
            safe_close(browser)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Internxt Drive upload/download automation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="開瀏覽器手動登入一次，儲存 session")

    p_up = sub.add_parser("upload", help="上傳本機檔案/目錄到 Internxt Drive")
    p_up.add_argument("local", help="本機檔案或目錄路徑")
    p_up.add_argument("remote", nargs="?", default="",
                       help="遠端目標目錄路徑，例如 Projects/Reports；省略時上傳到遠端根目錄")
    p_up.add_argument("--interval", type=int, default=30, help="狀態回報間隔秒數 (預設 30)")
    p_up.add_argument("--retries", type=int, default=3, help="單檔失敗重試次數 (預設 3)")
    p_up.add_argument("--verify", action="store_true", help="上傳後下載回來比對 sha256，不符則視為失敗並重試")

    p_dl = sub.add_parser("download", help="從 Internxt Drive 下載檔案/目錄到本機")
    p_dl.add_argument("remote", help="遠端來源路徑（檔案或目錄）")
    p_dl.add_argument("local", help="本機存放目錄")
    p_dl.add_argument("--interval", type=int, default=30, help="狀態回報間隔秒數 (預設 30)")
    p_dl.add_argument("--retries", type=int, default=3, help="單檔失敗重試次數 (預設 3)")

    sub.add_parser("status", help="顯示目前傳輸紀錄 (transfer_manifest.json)")

    sub.add_parser("audit", help="核對 transfer_manifest.json 裡標記完成的上傳，是否真的落在正確的遠端資料夾（唯讀）")

    p_dbg = sub.add_parser("debug-dump", help="登入後印出畫面上的按鈕/項目，方便校正選取器")
    p_dbg.add_argument("--remote", default="", help="要先導航進去的遠端目錄（可留空）")

    args = parser.parse_args()

    # Windows argv-parsing footgun: a quoted argument ending in a single
    # backslash before the closing quote (e.g. powershell's 'C:\foo\')
    # gets that backslash+quote pair collapsed into a literal trailing
    # `"` by the OS's C-runtime argv parser. `"` can never legitimately
    # appear in a Windows path or in a remote Internxt path, so stripping
    # a trailing one here undoes exactly that mis-parse.
    for attr in ("local", "remote"):
        val = getattr(args, attr, None)
        if val:
            setattr(args, attr, val.rstrip('"'))

    if args.cmd == "status":
        Manifest().print_status()
        return

    if args.cmd == "login":
        with sync_playwright() as pw:
            cmd_login(pw)
    elif args.cmd == "upload":
        do_upload(args)
    elif args.cmd == "download":
        do_download(args)
    elif args.cmd == "audit":
        cmd_audit(args)
    elif args.cmd == "debug-dump":
        cmd_debug_dump(args)


if __name__ == "__main__":
    main()
