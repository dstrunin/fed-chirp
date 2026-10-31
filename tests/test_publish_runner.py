"""Publisher integration tests: real local git remotes, fake scan and HTTP."""
import contextlib
import hashlib
import http.server
import json
import importlib.util
import sqlite3
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"

# Captured read-only from https://fedchirp.com/ on 2026-10-31.
CF_TAIL = b'<script>(function(){function c(){var b=a.contentDocument||(a.contentWindow&&a.contentWindow.document);if(b){var d=b.createElement(\'script\');d.innerHTML="window.__CF$cv$params={r:\'a53445133a65b7db\',t:\'MTc5MzQ2NjU5MQ==\'};var l=window.parent.location,o;try{o=new URL(l.origin).origin}catch(e){try{o=new URL(new URL(l.href).origin).origin}catch(e){o=\'\'}}var u=\'/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=a53445133a65b7db\',a=document.createElement(\'script\');a.src=o?new URL(u,o).href:u;document.getElementsByTagName(\'head\')[0].appendChild(a);";b.getElementsByTagName(\'head\')[0].appendChild(d)}}if(document.body){var a=document.createElement(\'iframe\');a.height=1;a.width=1;a.style.position=\'absolute\';a.style.top=0;a.style.left=0;a.style.border=\'none\';a.style.visibility=\'hidden\';document.body.appendChild(a);if(\'loading\'!==document.readyState)c();else if(window.addEventListener)document.addEventListener(\'DOMContentLoaded\',c);else{var e=document.onreadystatechange||function(){};document.onreadystatechange=function(b){e(b);\'loading\'!==document.readyState&&(document.onreadystatechange=e,c())}}}})();</script>'


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT, text=True).strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(root)], check=True, capture_output=True)
    git(root, "checkout", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "data").mkdir()
    (root / "dashboard").mkdir()
    (root / "data/fed_chirp.sqlite").write_bytes(b"database")
    (root / "dashboard/index.html").write_bytes(b"old")
    (root / "source.py").write_text("source = 1\n")
    (root / ".gitignore").write_text(".publish-state/\nfed_chirp/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "push", "-u", "origin", "main")
    fake = root / "fed_chirp"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "cli.py").write_text('''import os, pathlib, sys, time
p = pathlib.Path('.')
(p / '.publish-state/scanned').touch()
(p / '.publish-state/model').write_text(os.environ.get('FED_CHIRP_HERMES_MODEL', ''))
time.sleep(float(os.environ.get('FAKE_SCAN_DELAY', '0')))
if os.environ.get('FAKE_NO_CHANGE') != '1':
    (p / 'dashboard/index.html').write_bytes(b'new dashboard')
    (p / 'data/fed_chirp.sqlite').write_bytes(b'new database')
sys.exit(int(os.environ.get('FAKE_SCAN_EXIT', '0')))
''')
    return root, remote


@contextlib.contextmanager
def live(body=b"new dashboard", status=200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body() if callable(body) else body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def environment(root, url, **extra):
    return dict(os.environ, FED_CHIRP_REPO=str(root), FED_CHIRP_PYTHON=sys.executable,
                FED_CHIRP_LIVE_URL=url, FED_CHIRP_VERIFY_TIMEOUT="0.15",
                FED_CHIRP_VERIFY_INTERVAL="0.02", PYTHONPATH=str(root), **extra)


def run(root, url, **extra):
    return subprocess.run([sys.executable, str(SCRIPT)], env=environment(root, url, **extra),
                          text=True, capture_output=True, timeout=15)


def state(root):
    return json.loads((root / ".publish-state/status.json").read_text())


def test_success_pushes_and_verifies_digest(repo):
    root, remote = repo
    with live() as url:
        result = run(root, url)
    assert result.returncode == 0, result.stdout + result.stderr
    status = state(root)
    assert status["status"] == "success"
    assert status["stage"] == "verified"
    assert status["last_verified_success"]
    assert status["digest"] == hashlib.sha256(b"new dashboard").hexdigest()
    assert status["sha"] == git(remote, "rev-parse", "main")
    assert not git(root, "status", "--porcelain")


def test_generated_dirty_retry_is_allowed(repo):
    root, _ = repo
    (root / "dashboard/index.html").write_text("retry")
    with live() as url:
        result = run(root, url)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["unstaged", "staged", "untracked"])
def test_source_edits_rejected_before_scan(repo, kind):
    root, remote = repo
    path = root / ("other.py" if kind == "untracked" else "source.py")
    path.write_text("edited\n")
    if kind == "staged":
        git(root, "add", "source.py")
    before = git(remote, "rev-parse", "main")
    result = run(root, "http://127.0.0.1:1/")
    assert result.returncode != 0
    assert not (root / ".publish-state/scanned").exists()
    assert git(remote, "rev-parse", "main") == before
    assert state(root)["status"] == "failed"


def test_unexpected_scan_failure_never_publishes(repo):
    root, remote = repo
    before = git(root, "rev-parse", "HEAD")
    result = run(root, "http://127.0.0.1:1/", FAKE_SCAN_EXIT="7")
    assert result.returncode == 7
    assert git(root, "rev-parse", "HEAD") == before
    assert git(remote, "rev-parse", "main") == before
    assert state(root)["scan_exit_code"] == 7
    assert "7" in state(root)["error"]


def test_health_warning_publishes_but_preserves_failure(repo):
    root, remote = repo
    before = git(remote, "rev-parse", "main")
    with live() as url:
        result = run(root, url, FAKE_SCAN_EXIT="2")
    assert result.returncode == 2
    assert git(remote, "rev-parse", "main") != before
    assert state(root)["status"] == "failed"
    assert state(root)["stage"] == "verified"
    assert not state(root)["last_verified_success"]


def test_no_new_diff_still_pushes_prior_failed_push(repo):
    root, remote = repo
    (root / "dashboard/index.html").write_bytes(b"new dashboard")
    git(root, "add", "dashboard/index.html")
    git(root, "commit", "-m", "previous local commit")
    with live() as url:
        result = run(root, url, FAKE_NO_CHANGE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(remote, "rev-parse", "main") == git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("http_status,body", [(200, b"stale"), (503, b"new dashboard")])
def test_live_mismatch_or_http_error_never_succeeds(repo, http_status, body):
    root, _ = repo
    with live(body, http_status) as url:
        result = run(root, url)
    assert result.returncode != 0
    assert state(root)["status"] == "failed"
    assert state(root)["stage"] == "verify"
    assert not state(root)["last_verified_success"]


def test_last_verified_success_survives_failed_retry(repo):
    root, _ = repo
    with live() as url:
        assert run(root, url).returncode == 0
        previous = state(root)["last_verified_success"]
        assert run(root, url, FAKE_SCAN_EXIT="9").returncode == 9
    assert state(root)["last_verified_success"] == previous


def test_kernel_lock_survives_file_but_not_process_crash(repo):
    root, _ = repo
    with live() as url:
        first = subprocess.Popen([sys.executable, str(SCRIPT)], env=environment(root, url, FAKE_SCAN_DELAY="5"),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (root / ".publish-state/scanned").exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            active = state(root)
            second = run(root, url)
            assert second.returncode != 0
            assert state(root) == active
        finally:
            import signal
            os.killpg(first.pid, signal.SIGKILL)
            first.communicate()
        assert run(root, url).returncode == 0


def test_push_failure_is_nonzero_and_retry_pushes_without_new_diff(repo):
    root, remote = repo
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with live() as url:
        failed = run(root, url)
        assert failed.returncode != 0
        assert state(root)["stage"] == "push"
        assert state(root)["status"] == "failed"
        hook.unlink()
        assert run(root, url, FAKE_NO_CHANGE="1").returncode == 0
    assert git(remote, "rev-parse", "main") == git(root, "rev-parse", "HEAD")


def test_cloudflare_challenge_tail_is_accepted_but_application_mutation_is_not(repo):
    root, _ = repo
    html = b"<html><body><h1>application</h1><script>own()</script>\n</body></html>"
    (root / "dashboard/index.html").write_bytes(html)
    injection = CF_TAIL
    served = [html.replace(b"</body>", injection + b"</body>")]
    with live(lambda: served[0]) as url:
        result = run(root, url, FAKE_NO_CHANGE="1")
        assert result.returncode == 0, result.stdout + result.stderr
        served[0] = served[0].replace(b"application", b"tampered")
        assert run(root, url, FAKE_NO_CHANGE="1").returncode != 0


def test_shell_delegates_synchronously_and_sets_model(repo):
    shell = SCRIPT.with_name("scan_and_publish.sh")
    # Never execute the old hard-coded production-only shell during RED.
    assert "publish.py" in shell.read_text()
    root, _ = repo
    with live() as url:
        env = environment(root, url, FAKE_SCAN_EXIT="7")
        env.pop("FED_CHIRP_HERMES_MODEL", None)
        result = subprocess.run(["bash", str(shell)], env=env, capture_output=True, timeout=15)
    assert result.returncode == 7
    assert (root / ".publish-state/model").read_text() == "gpt-6-astra"


def test_arbitrary_injected_script_is_rejected(repo):
    root, _ = repo
    html = b"<html><body><h1>application</h1></body></html>"
    (root / "dashboard/index.html").write_bytes(html)
    with live(html.replace(b"</body>", b"<script>evil()</script></body>")) as url:
        assert run(root, url, FAKE_NO_CHANGE="1").returncode != 0


def matches_live(expected, actual):
    spec = importlib.util.spec_from_file_location("publisher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.matches_live(expected, actual)


@pytest.mark.parametrize("tail", [
    b"<script>(function(){/* __CF$cv$params /cdn-cgi/challenge-platform/ */evil()})();</script>",
    CF_TAIL.replace(b"a.height=1", b"a.height=1;evil()"),
    CF_TAIL.replace(b"a.height=1", b"a.height = 1"),
    CF_TAIL + CF_TAIL,
    CF_TAIL + b"<script>evil()</script>",
    CF_TAIL.replace(b"a53445133a65b7db", b"a53445133a65b7db;evil()"),
    CF_TAIL.replace(b"MTc5MzQ2NjU5MQ==", b"';evil();//"),
    CF_TAIL.replace(b"?ray=a53445133a65b7db", b"?ray=1111111111111111"),
    CF_TAIL.replace(b"<script>", b'<script nonce="bad;evil()">', 1),
])
def test_live_rejects_non_template_challenges(tail):
    html = b"<html><body>exact application\n</body></html>"
    assert not matches_live(html, html.replace(b"</body>", tail + b"</body>"))


@pytest.mark.parametrize("ray,stamp", [
    (b"a53445133a65b7db", b"MTc5MzQ2NjU5MQ=="),
    (b"0123456789abcdef", b"MTc5MzQ2NjU5Mg=="),
])
def test_live_accepts_captured_template_variable_fields(ray, stamp):
    html = b"<html><body>exact application\n</body></html>"
    tail = CF_TAIL.replace(b"a53445133a65b7db", ray).replace(b"MTc5MzQ2NjU5MQ==", stamp)
    assert matches_live(html, html.replace(b"</body>", tail + b"</body>"))
    assert not matches_live(html, html.replace(b"exact application", b"exact  application"))


def sqlite_bytes(path, value):
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as db:
        db.execute("create table observations (value text)")
        db.execute("insert into observations values (?)", (value,))
    return path.read_bytes()


def upstream_artifacts(root, remote, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-b", "main", str(remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.name", "test")
    git(other, "config", "user.email", "test@example.invalid")
    database = sqlite_bytes(other / "data/fed_chirp.sqlite", "upstream newer data")
    (other / "dashboard/index.html").write_bytes(b"upstream dashboard")
    git(other, "add", ".")
    git(other, "commit", "-m", "upstream scan")
    git(other, "push", "origin", "main")
    return database


def dirty_artifacts(root, kind):
    staged = sqlite_bytes(root / "data/fed_chirp.sqlite", "interrupted staged data")
    (root / "dashboard/index.html").write_bytes(b"interrupted staged dashboard")
    if kind in ("staged", "both"):
        git(root, "add", "data/fed_chirp.sqlite", "dashboard/index.html")
    if kind == "both":
        sqlite_bytes(root / "data/fed_chirp.sqlite", "interrupted unstaged data")
        (root / "dashboard/index.html").write_bytes(b"interrupted unstaged dashboard")
    worktree = {p: (root / p).read_bytes() for p in ("data/fed_chirp.sqlite", "dashboard/index.html")}
    return staged, worktree


def assert_backup(root, staged, worktree, kind):
    backups = list((root / ".publish-state/backups").glob("*/manifest.json"))
    assert len(backups) == 1
    backup = backups[0].parent
    manifest = json.loads(backups[0].read_text())
    assert manifest["head"]
    assert manifest["upstream"]
    for path, content in worktree.items():
        assert (backup / "worktree" / path).read_bytes() == content
    if kind in ("staged", "both"):
        assert (backup / "index/data/fed_chirp.sqlite").read_bytes() == staged
        assert (backup / "index/dashboard/index.html").read_bytes() == b"interrupted staged dashboard"
    with sqlite3.connect(backup / "worktree/data/fed_chirp.sqlite") as db:
        assert db.execute("select value from observations").fetchone()


@pytest.mark.parametrize("kind", ["unstaged", "staged", "both"])
def test_dirty_artifacts_backed_up_before_fast_forward(repo, tmp_path, kind):
    root, remote = repo
    upstream = upstream_artifacts(root, remote, tmp_path)
    staged, worktree = dirty_artifacts(root, kind)
    # Fake scan deliberately preserves input: final bytes must be upstream, not old local DB.
    with live(b"upstream dashboard") as url:
        result = run(root, url, FAKE_NO_CHANGE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "data/fed_chirp.sqlite").read_bytes() == upstream
    assert (root / "dashboard/index.html").read_bytes() == b"upstream dashboard"
    assert (root / ".publish-state/scanned").exists()
    assert not git(root, "status", "--porcelain")
    assert_backup(root, staged, worktree, kind)
    # A later run must never replay a retained backup over upstream.
    with live(b"upstream dashboard") as url:
        assert run(root, url, FAKE_NO_CHANGE="1").returncode == 0
    assert (root / "data/fed_chirp.sqlite").read_bytes() == upstream
    assert_backup(root, staged, worktree, kind)


def test_failed_sync_retains_dirty_artifact_backups(repo, tmp_path):
    root, remote = repo
    upstream_artifacts(root, remote, tmp_path)
    (root / "source.py").write_text("source = 2\n")
    git(root, "add", "source.py")
    git(root, "commit", "-m", "diverged local commit")
    head = git(root, "rev-parse", "HEAD")
    staged, worktree = dirty_artifacts(root, "both")
    result = run(root, "http://127.0.0.1:1/")
    assert result.returncode != 0
    assert state(root)["stage"] == "sync"
    assert not (root / ".publish-state/scanned").exists()
    assert git(root, "rev-parse", "HEAD") == head
    assert_backup(root, staged, worktree, "both")


def test_backup_write_failure_never_clears_worktree_or_index(repo, tmp_path, monkeypatch):
    root, remote = repo
    upstream_artifacts(root, remote, tmp_path)
    staged, worktree = dirty_artifacts(root, "both")
    spec = importlib.util.spec_from_file_location("publisher", SCRIPT)
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    for key, value in environment(root, "http://127.0.0.1:1/").items():
        monkeypatch.setenv(key, value)
    real_fsync = os.fsync
    failed = []

    def fail_backup_fsync(fd):
        for path in (root / ".publish-state/backups").glob("*/worktree/data/fed_chirp.sqlite"):
            if os.fstat(fd).st_ino == path.stat().st_ino:
                failed.append(path)
                raise OSError("simulated backup disk failure")
        return real_fsync(fd)

    # main installs process signal handlers; let monkeypatch restore them afterward.
    monkeypatch.setattr(publisher.signal, "signal", lambda *args: None)
    monkeypatch.setattr(publisher.os, "fsync", fail_backup_fsync)
    assert publisher.main() != 0
    assert failed, "the backup must actually be flushed before reset"
    assert state(root)["stage"] == "sync"
    assert not (root / ".publish-state/scanned").exists()
    for path, content in worktree.items():
        assert (root / path).read_bytes() == content
    assert subprocess.check_output(["git", "-C", str(root), "show", ":data/fed_chirp.sqlite"]) == staged
