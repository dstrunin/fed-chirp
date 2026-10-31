#!/usr/bin/env python3
"""Synchronous publisher. Exit 2 means rendered FOMC warning, never success.

State is private/untracked in .publish-state (override FED_CHIRP_STATE_DIR).
No scanner/git output is copied to status or logs: it may contain credentials.
FED_CHIRP_REPO/PYTHON/LIVE_URL and VERIFY_TIMEOUT/INTERVAL provide test seams.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

ARTIFACTS = ("data/fed_chirp.sqlite", "dashboard/index.html")
FOMC_WARNING_EXIT = 2


class PublishFailure(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code if 0 < code < 256 else 1


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_status(path, status):
    """Readers see the old or new complete object, never a partial write."""
    fd, name = tempfile.mkstemp(prefix="status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(status, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


# Fixed Cloudflare JSD tail captured from fedchirp.com, 2026-10-31.
# Deliberately fail closed if CF changes executable code (including whitespace).
# This captured variant has no nonce field; arbitrary nonce attributes are not allowed.
_CF_CHALLENGE = (
    b'<script>(function(){function c(){var b=a.contentDocument||(a.contentWindow&&a.contentWindow.document);if(b){va'
    b'r d=b.createElement(\'script\');d.innerHTML="window.__CF$cv$params={r:\'RAY\',t:\'TIMESTAMP\'};var l=window.parent.l'
    b"ocation,o;try{o=new URL(l.origin).origin}catch(e){try{o=new URL(new URL(l.href).origin).origin}catch(e){o=''}}"
    b"var u='/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=RAY',a=document.createElement('script');a.src=o?new"
    b' URL(u,o).href:u;document.getElementsByTagName(\'head\')[0].appendChild(a);";b.getElementsByTagName(\'head\')[0].a'
    b"ppendChild(d)}}if(document.body){var a=document.createElement('iframe');a.height=1;a.width=1;a.style.position="
    b"'absolute';a.style.top=0;a.style.left=0;a.style.border='none';a.style.visibility='hidden';document.body.append"
    b"Child(a);if('loading'!==document.readyState)c();else if(window.addEventListener)document.addEventListener('DOM"
    b"ContentLoaded',c);else{var e=document.onreadystatechange||function(){};document.onreadystatechange=function(b)"
    b"{e(b);'loading'!==document.readyState&&(document.onreadystatechange=e,c())}}}})();</script>"
)
_CF_CHALLENGE_RE = re.compile(
    re.escape(_CF_CHALLENGE)
    .replace(b"RAY", rb"(?P<ray>[0-9a-f]{16})", 1)
    .replace(b"RAY", rb"(?P=ray)", 1)
    .replace(b"TIMESTAMP", rb"(?P<stamp>[A-Za-z0-9+/]{14}==)", 1)
)


# Exact public Insights tag (including trailing newline) captured from this site.
# Both challenge + beacon and beacon + challenge orders were observed 2026-10-31.
# Beacon-only was not observed and is deliberately not accepted.
_CF_BEACON = (
    b'<script type="module" src="https://static.cloudflareinsights.com/beacon.min.js/'
    b'vce701823e78c4301a35b7e07294d87301792088814092" '
    b'integrity="sha512-UKNk8GbxkABxKcXYuLkO16EleLGUkcvJWtpSNmExE0Too3ZaFAxTscNWEc1OdAv5/BikHawapSCaiD2XCmp+Bw==" '
    b'data-cf-beacon=\'{"version":"2024.11.0","token":"387343d97fba4e7596c99d5f8c8c7c25","r":1,"spa":2}\' '
    b'crossorigin="anonymous"></script>\n'
)


def matches_live(expected, actual):
    """Exact application bytes; allow CF's challenge plus optional pinned beacon.

    Do not strip arbitrary scripts or normalize whitespace/application HTML.
    All bytes before the final </body> and the closing suffix remain bound.
    """
    if actual == expected:
        return True
    prefix, separator, suffix = expected.rpartition(b"</body>")
    if not separator or not actual.startswith(prefix) or not actual.endswith(separator + suffix):
        return False
    injected = actual[len(prefix):len(actual) - len(separator + suffix)]
    # Remove at most one exact tag in either observed position, then require the
    # complete validated challenge. Extra/duplicate scripts still fail fullmatch.
    if injected.endswith(_CF_BEACON):
        injected = injected[:-len(_CF_BEACON)]
    elif injected.startswith(_CF_BEACON):
        injected = injected[len(_CF_BEACON):]
    match = _CF_CHALLENGE_RE.fullmatch(injected)
    if not match:
        return False
    # CF encodes a ten-digit Unix timestamp, not an arbitrary base64 payload.
    stamp = match["stamp"]
    decoded = base64.b64decode(stamp, validate=True)
    return bool(re.fullmatch(rb"[0-9]{10}", decoded) and base64.b64encode(decoded) == stamp)



def verify_live(expected, url, timeout, interval):
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(.001, deadline - time.monotonic())
        try:
            request = Request(url, headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity",
                                            "User-Agent": "FedChirp-Publish-Verify/1"})
            with urlopen(request, timeout=min(15, remaining)) as response:
                actual = response.read(len(expected) + 100_001)
                if response.status == 200 and matches_live(expected, actual):
                    return
        except (URLError, OSError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublishFailure("live dashboard digest did not match before deadline")
        time.sleep(min(interval, remaining))


def main():
    root = Path(os.environ.get("FED_CHIRP_REPO", Path(__file__).resolve().parents[1])).resolve()
    state_dir = Path(os.environ.get("FED_CHIRP_STATE_DIR", root / ".publish-state")).resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = state_dir / "status.json"
    # Never unlink the lock inode: replacing it permits two concurrent owners.
    with (state_dir / "publisher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Fed Chirp: another publisher is active")
            return 75  # Do not overwrite the active owner's status.
        previous = {}
        try:
            previous = json.loads(status_path.read_text())
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            pass
        status = dict(status="running", stage="preflight", started_at=timestamp(), updated_at=timestamp(),
                      finished_at=None, error=None, sha=None, digest=None, scan_exit_code=None,
                      last_verified_success=previous.get("last_verified_success"))

        def update(stage=None, **fields):
            if stage:
                status["stage"] = stage
            status.update(fields, updated_at=timestamp())
            atomic_status(status_path, status)

        def command(args, *, check=True, timeout=120):
            try:
                result = subprocess.run(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        timeout=timeout, pass_fds=(lock.fileno(),))
            except subprocess.TimeoutExpired:
                raise PublishFailure(f"{status['stage']} timed out") from None
            if check and result.returncode:
                raise PublishFailure(f"{status['stage']} exited {result.returncode}", result.returncode)
            return result

        def git(*args, **kwargs):
            return command(["git", *args], **kwargs)

        def check_dirty():
            # NUL-delimited porcelain is safe for whitespace/newlines in paths.
            entries = git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split(b"\0")
            dirty = []
            for entry in entries:
                if not entry:
                    continue
                code, path = entry[:2], os.fsdecode(entry[3:])
                if path not in ARTIFACTS or b"R" in code or b"C" in code or b"D" in code or b"U" in code:
                    raise PublishFailure("source/unexpected working-tree changes; refusing publisher")
                dirty.append(path)
            return dirty

        def preserve_artifacts(dirty):
            """Durably retain both index and worktree before any destructive reset.

            Backups are never replayed automatically: the upstream database is
            authoritative after sync. Partial/failed backups remain for recovery.
            """
            backups = state_dir / "backups"
            backups.mkdir(exist_ok=True, mode=0o700)
            backup = Path(tempfile.mkdtemp(prefix="artifacts-", dir=backups))

            def save(relative, content):
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())

            for path in dirty:
                save("worktree/" + path, (root / path).read_bytes())
                # The index may differ from BOTH HEAD and the worktree.
                indexed = git("show", ":" + path, check=False)
                if indexed.returncode == 0:
                    save("index/" + path, indexed.stdout)
                elif git("ls-files", "--error-unmatch", "--", path, check=False).returncode == 0:
                    raise PublishFailure("cannot preserve staged artifact")
            save("manifest.json", json.dumps(dict(
                head=git("rev-parse", "HEAD").stdout.decode().strip(),
                upstream=git("rev-parse", "origin/main").stdout.decode().strip(),
                created_at=timestamp(), paths=dirty,
            ), sort_keys=True).encode() + b"\n")
            # Persist directory entries bottom-up, including the backup's parents.
            directories = [p for p in backup.rglob("*") if p.is_dir()]
            for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True) + [backup, backups, state_dir]:
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            git("restore", "--source=HEAD", "--staged", "--worktree", "--", *dirty)

        def interrupted(signum, frame):
            raise PublishFailure(f"publisher interrupted by signal {signum}", 128 + signum)

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            update()
            check_dirty()
            update("fetch")
            git("fetch", "origin", "main")
            update("sync")
            # A local-ahead push retry needs no reset; preserve its normal behavior.
            ancestor = git("merge-base", "--is-ancestor", "origin/main", "HEAD", check=False)
            if ancestor.returncode == 1:
                dirty = check_dirty()
                if dirty:
                    preserve_artifacts(dirty)
            elif ancestor.returncode:
                raise PublishFailure("cannot inspect upstream ancestry")
            git("merge", "--ff-only", "origin/main")
            update("scan")
            python = os.environ.get("FED_CHIRP_PYTHON", str(root / ".venv/bin/python"))
            result = command([python, "-m", "fed_chirp.cli", "scan", "--summary-file",
                              str(state_dir / "scan-summary.md")], check=False,
                             timeout=float(os.environ.get("FED_CHIRP_SCAN_TIMEOUT", "7200")))
            update(scan_exit_code=result.returncode)
            if result.returncode not in (0, FOMC_WARNING_EXIT):
                raise PublishFailure(f"scan exited {result.returncode}; nothing published", result.returncode)
            check_dirty()
            # Snapshot immutable application bytes before publishing.
            expected = (root / "dashboard/index.html").read_bytes()
            update("commit", digest=hashlib.sha256(expected).hexdigest())
            git("add", "--", *ARTIFACTS)
            changed = git("diff", "--cached", "--quiet", "--", *ARTIFACTS, check=False)
            if changed.returncode == 1:
                git("-c", "user.name=fed-chirp-local-bot", "-c",
                    "user.email=fed-chirp-local-bot@users.noreply.github.com", "commit", "--only",
                    "-m", f"scan: {timestamp()}", "--", *ARTIFACTS)
            elif changed.returncode:
                raise PublishFailure("cannot inspect staged artifacts")
            sha = git("rev-parse", "HEAD").stdout.decode().strip()
            update("push", sha=sha)
            # Always push: a prior failed push may have left an existing local commit.
            git("push", "origin", "HEAD:main")
            update("verify")
            verify_live(expected, os.environ.get("FED_CHIRP_LIVE_URL", "https://fedchirp.com/"),
                        max(0, float(os.environ.get("FED_CHIRP_VERIFY_TIMEOUT", "600"))),
                        max(.01, float(os.environ.get("FED_CHIRP_VERIFY_INTERVAL", "10"))))
            now = timestamp()
            if result.returncode == FOMC_WARNING_EXIT:
                update("verified", status="failed", finished_at=now, verified_at=now,
                       error="FOMC health scan failed (exit 2); warning artifacts published and verified")
                print("Fed Chirp: FOMC health failed; warning artifacts published and verified")
                return FOMC_WARNING_EXIT
            update("verified", status="success", finished_at=now, verified_at=now, last_verified_success=now)
            print("Fed Chirp: published and live dashboard verified")
            return 0
        except PublishFailure as exc:
            update(status="failed", error=str(exc), finished_at=timestamp())
            print(f"Fed Chirp: {exc}")
            return exc.code
        except Exception as exc:
            # Exception strings and command output may leak URLs/tokens. Keep type only.
            update(status="failed", error=f"{status['stage']} failed ({type(exc).__name__})", finished_at=timestamp())
            print(f"Fed Chirp: {status['error']}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
