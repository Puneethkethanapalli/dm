#!/usr/bin/env python3
"""Fault-injection tests for dm. Needs aria2c.

Runs a private dmd (its own state, runtime and download folders under a work
directory outside the repo) against tests/server.py, then checks every
download ends `done` with the right bytes, or `failed` with a clear reason and
no half-written file in the download folder.

  python -I tests/run.py [--work DIR] [-k NAME]
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.dont_write_bytecode = True

import server  # noqa: E402

DMD = os.path.join(ROOT, "daemon", "dmd.py")
DM = os.path.join(ROOT, "bin", "dm")


class Failure(Exception):
    pass


def check(cond, message):
    if not cond:
        raise Failure(message)


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Instance:
    """One isolated dmd plus helpers to drive it through the real CLI."""

    def __init__(self, work, name, base_url, wrap=None, config=None):
        self.root = os.path.join(work, name)
        shutil.rmtree(self.root, ignore_errors=True)
        self.downloads = os.path.join(self.root, "downloads")
        os.makedirs(self.downloads)
        for d in ("state", "config/dm"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        # Unix socket paths max out at 108 bytes, so the runtime folder lives
        # where real ones do, not under the (long) work dir.
        self.run_dir = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "dm-tests", name)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        os.makedirs(self.run_dir, mode=0o700)
        cfg = {"download_dir": self.downloads, "notify": False, "max_retries": 2, "max_concurrent": 3}
        cfg.update(config or {})
        with open(os.path.join(self.root, "config/dm/config.json"), "w") as f:
            json.dump(cfg, f)
        self.env = dict(os.environ,
                        XDG_STATE_HOME=os.path.join(self.root, "state"),
                        XDG_CONFIG_HOME=os.path.join(self.root, "config"),
                        XDG_RUNTIME_DIR=self.run_dir,
                        DM_BACKOFF="1,1,1",
                        PYTHONDONTWRITEBYTECODE="1")
        self.base = base_url
        self.wrap = wrap or []
        self.proc = None
        self.log = open(os.path.join(self.root, "dmd.log"), "ab")

    def start(self):
        self.proc = subprocess.Popen(self.wrap + [sys.executable, "-B", DMD], env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: self.state() is not None and self.state()["engine"]["ok"], 10, "dmd did not come up")

    def stop(self, sig=signal.SIGTERM):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # A killed dmd leaves aria2c to notice via --stop-with-process.
        deadline = time.time() + 5
        while time.time() < deadline and self.aria2_pids():
            time.sleep(0.1)

    def aria2_pids(self):
        conf = os.path.join(self.run_dir, "dm", "aria2.conf")
        out = subprocess.run(["pgrep", "-f", "aria2c --conf-path=" + conf], capture_output=True, text=True)
        return [int(p) for p in out.stdout.split()]

    def dm(self, *args):
        out = subprocess.run([sys.executable, "-B", DM, "--json"] + list(args), env=self.env,
                             capture_output=True, text=True, timeout=30)
        try:
            return json.loads(out.stdout)
        except ValueError:
            raise Failure("dm %s printed %r %r" % (" ".join(args), out.stdout, out.stderr))

    def dm_raw(self, payload):
        """Send a raw command to the engine's socket, as the browser host does."""
        code = ("import json, sys; sys.path.insert(0, %r); from client import request; "
                "print(json.dumps(request(json.loads(sys.argv[1]), timeout=25)))" % os.path.join(ROOT, "daemon"))
        out = subprocess.run([sys.executable, "-B", "-c", code, json.dumps(payload)], env=self.env,
                             capture_output=True, text=True, timeout=30)
        try:
            return json.loads(out.stdout)
        except ValueError:
            raise Failure("raw command printed %r %r" % (out.stdout, out.stderr))

    def state(self):
        reply = self.dm("status")
        return reply.get("state") if reply.get("ok") else None

    def job(self, job_id):
        st = self.state()
        return next((j for j in st["jobs"] if j["id"] == job_id), None) if st else None

    def add(self, path, *extra):
        reply = self.dm("add", self.base + path, *extra)
        check(reply.get("ok"), "add failed: %s" % reply)
        return reply["job"]["id"]

    def wait(self, predicate, timeout, message):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if predicate():
                    return
            except Failure:
                pass
            time.sleep(0.2)
        raise Failure(message)

    def finish(self, job_id, timeout=60):
        """Wait for a final state and return the job."""
        holder = {}

        def done():
            j = self.job(job_id)
            holder["job"] = j
            return j and j["status"] in ("done", "failed", "cancelled")
        try:
            self.wait(done, timeout, "")
        except Failure:
            raise Failure("job never finished; last seen %s" % json.dumps(holder.get("job")))
        return holder["job"]

    def assert_clean(self, allowed):
        """Only finished files in the download folder: no partials, no staging."""
        names = sorted(os.listdir(self.downloads))
        check(names == sorted(allowed), "download folder holds %s, expected %s" % (names, sorted(allowed)))


# ------------------------------------------------------------------- tests

SIZE = 4 << 20
BIG = 16 << 20


def expect_done(inst, job_id, name, size, version=0, route_name=None, timeout=60):
    job = inst.finish(job_id, timeout)
    check(job["status"] == "done", "expected done, got %s: %s" % (job["status"], job.get("error")))
    check(os.path.basename(job["file"]) == name, "saved as %r, expected %r" % (os.path.basename(job["file"]), name))
    want = server.expected_sha(route_name or name, size, version)
    check(sha_file(job["file"]) == want, "bytes differ from the server's file")
    return job


def t_ok(inst):
    jid = inst.add("/ok/plain.bin?size=%d" % BIG)
    job = expect_done(inst, jid, "plain.bin", BIG)
    check(job["resumable"] is True, "a server that resumes wasn't recognised: %s" % job["resumable"])
    inst.assert_clean(["plain.bin"])


def t_no_range_and_drop(inst):
    # Can't resume, so aria2 has to start over from the beginning.
    jid = inst.add("/norange-drop/norange.bin?size=%d" % SIZE)
    job = expect_done(inst, jid, "norange.bin", SIZE)
    check(job["resumable"] is False, "not marked as can't-resume")
    inst.assert_clean(["norange.bin"])


def t_unresumable_pause(inst):
    # Pausing a can't-resume link warns, and every resume starts clean.
    jid = inst.add("/norange/stream.bin?size=%d&rate=%d" % (2 << 20, 512 << 10))
    inst.wait(lambda: inst.job(jid)["resumable"] is False, 10, "never learned the link can't resume")
    for _ in range(2):
        inst.wait(lambda: inst.job(jid)["completed"] > (200 << 10), 20, "never made progress")
        reply = inst.dm("pause", jid)
        check(reply.get("will_restart") == [jid], "pause didn't warn it will start over: %s" % reply)
        check(inst.dm("resume", jid)["ok"], "resume refused")
    expect_done(inst, jid, "stream.bin", 2 << 20)
    inst.assert_clean(["stream.bin"])


def t_drop_resume(inst):
    jid = inst.add("/drop/drop.bin?size=%d" % BIG)
    expect_done(inst, jid, "drop.bin", BIG)
    inst.assert_clean(["drop.bin"])


def t_file_changes(inst):
    # Half of v0 arrives, then the server swaps in v1: the result must be all v1.
    jid = inst.add("/changing/changing.bin?size=%d" % SIZE)
    job = expect_done(inst, jid, "changing.bin", SIZE, version=1)
    check(job.get("note"), "no note that the download restarted")
    inst.assert_clean(["changing.bin"])


def t_redirects(inst):
    jid = inst.add("/redirect/3/hop.bin?size=%d" % SIZE)
    expect_done(inst, jid, "hop.bin", SIZE)


def t_utf8_name(inst):
    name = "résumé ✓ 2026.pdf"
    jid = inst.add("/disposition/doc?size=%d&as=%s" % (SIZE, urllib.request.quote(name)))
    expect_done(inst, jid, name, SIZE, route_name="doc")


def t_not_found(inst):
    jid = inst.add("/status/404")
    job = inst.finish(jid, 20)
    check(job["status"] == "failed", "404 should fail, got %s" % job["status"])
    check(job["attempts"] == 0, "404 should not be retried")
    check("not found" in job["error"]["text"].lower(), "unclear error: %s" % job["error"])
    inst.assert_clean([])


def t_forbidden(inst):
    jid = inst.add("/status/403")
    job = inst.finish(jid, 20)
    check(job["status"] == "failed" and job["attempts"] == 0, "403 should fail at once: %s" % job)


def t_server_error_retries(inst):
    jid = inst.add("/status/503")
    job = inst.finish(jid, 60)
    check(job["status"] == "failed", "503 should finally fail, got %s" % job["status"])
    check(job["attempts"] == 2, "expected 2 retries, got %d" % job["attempts"])


def t_checksum(inst):
    good = server.expected_sha("sum.bin", SIZE)
    jid = inst.add("/ok/sum.bin?size=%d" % SIZE, "--sha256", good)
    expect_done(inst, jid, "sum.bin", SIZE)
    bad = inst.add("/ok/sum2.bin?size=%d" % SIZE, "--sha256", "0" * 64)
    job = inst.finish(bad)
    check(job["status"] == "failed" and "checksum" in job["error"]["text"].lower(), "bad sum accepted: %s" % job)
    inst.assert_clean(["sum.bin"])


def t_same_name_twice(inst):
    a = inst.add("/ok/twice.tar.gz?size=%d" % SIZE)
    expect_done(inst, a, "twice.tar.gz", SIZE)
    b = inst.add("/ok/twice.tar.gz?size=%d" % SIZE, "--force")
    expect_done(inst, b, "twice (1).tar.gz", SIZE, route_name="twice.tar.gz")


def t_pause_resume(inst):
    jid = inst.add("/slow/pause.bin?size=%d&rate=%d" % (2 << 20, 512 << 10))
    inst.wait(lambda: inst.job(jid)["completed"] > (300 << 10), 20, "never made progress")
    check(inst.dm("pause", jid[:6])["ok"], "pause refused")
    paused = inst.job(jid)
    check(paused["status"] == "paused", "not paused: %s" % paused["status"])
    time.sleep(1.5)
    check(inst.job(jid)["completed"] >= paused["completed"], "paused job lost progress")
    check(inst.dm("resume", jid)["ok"], "resume refused")
    expect_done(inst, jid, "pause.bin", 2 << 20)


def t_cancel(inst):
    jid = inst.add("/slow/cancel.bin?size=%d&rate=%d" % (2 << 20, 256 << 10))
    inst.wait(lambda: inst.job(jid)["completed"] > 0, 20, "never started")
    check(inst.dm("cancel", jid)["ok"], "cancel refused")
    check(inst.job(jid)["status"] == "cancelled", "not cancelled")
    inst.assert_clean([])


def t_kill_daemon(inst):
    jid = inst.add("/slow/crash.bin?size=%d&rate=%d" % (3 << 20, 512 << 10))
    inst.wait(lambda: inst.job(jid)["completed"] > (1 << 20), 20, "never made progress")
    time.sleep(5.5)  # let aria2 save its control file (every 5 s)
    inst.stop(signal.SIGKILL)
    check(not inst.aria2_pids(), "aria2c outlived a killed dmd")
    inst.start()
    first = inst.job(jid)
    check(first["status"] != "failed", "job failed after restart: %s" % first)
    resumed_from = {}

    def progressing():
        j = inst.job(jid)
        if j["status"] == "active" and j["completed"] > 0:
            resumed_from["bytes"] = j["completed"]
            return True
        return j["status"] == "done"
    inst.wait(progressing, 20, "did not resume")
    check(resumed_from.get("bytes", 3 << 20) > (512 << 10), "restarted from zero instead of resuming")
    expect_done(inst, jid, "crash.bin", 3 << 20)


def t_kill_aria2(inst):
    jid = inst.add("/slow/engine.bin?size=%d&rate=%d" % (3 << 20, 512 << 10))
    inst.wait(lambda: inst.job(jid)["completed"] > (1 << 20), 20, "never made progress")
    time.sleep(5.5)
    for pid in inst.aria2_pids():
        os.kill(pid, signal.SIGKILL)
    expect_done(inst, jid, "engine.bin", 3 << 20)


FAKE_NMCLI = """#!/bin/sh
net="$(dirname "$0")/net"
case "$*" in
  "-t networking connectivity") cat "$net/connectivity" ;;
  monitor) exec tail -n0 -F "$net/events" ;;
esac
"""


def t_network_outage(inst):
    # NetworkManager is faked: tests drive "connectivity is now ..." lines.
    inst.stop()
    fake = os.path.join(inst.root, "fakebin")
    os.makedirs(os.path.join(fake, "net"), exist_ok=True)
    with open(os.path.join(fake, "nmcli"), "w") as f:
        f.write(FAKE_NMCLI)
    os.chmod(os.path.join(fake, "nmcli"), 0o755)
    with open(os.path.join(fake, "net", "connectivity"), "w") as f:
        f.write("full\n")
    events = os.path.join(fake, "net", "events")
    open(events, "w").close()
    inst.env["PATH"] = fake + os.pathsep + inst.env["PATH"]
    inst.start()

    def say(line):
        with open(events, "a") as f:
            f.write(line + "\n")

    # Nothing answers on this port yet, like a server we can't reach.
    late = server.Server(("127.0.0.1", 0), server.Handler)
    port = late.server_address[1]
    late.server_close()
    jid = inst.dm("add", "http://127.0.0.1:%d/ok/net.bin?size=%d" % (port, SIZE))["job"]["id"]
    inst.wait(lambda: inst.job(jid)["status"] == "retrying", 30, "never went to retrying")
    say("Connectivity is now 'none'")
    inst.wait(lambda: inst.job(jid)["waiting_network"], 10, "didn't wait for the network")
    used = inst.job(jid)["attempts"]
    time.sleep(3)
    check(inst.job(jid)["attempts"] == used, "retried while offline")
    srv = server.Server(("127.0.0.1", port), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        say("Connectivity is now 'full'")
        expect_done(inst, jid, "net.bin", SIZE, timeout=30)
    finally:
        srv.shutdown()


def t_browser_handoff(inst):
    # What the extension sends: cookies, referrer and user agent, verified first.
    reply = inst.dm_raw({"cmd": "add", "url": inst.base + "/login/members.zip?size=%d" % SIZE,
                         "headers": ["Cookie: session=ok; theme=dark"], "referer": "https://example.com/files",
                         "user_agent": "TestBrowser/1.0", "mime": "application/zip", "verify": True,
                         "source": "browser"})
    check(reply.get("ok"), "verified hand-off refused: %s" % reply)
    job = expect_done(inst, reply["job"]["id"], "members.zip", SIZE)
    check(job["source"] == "browser", "source not recorded")
    downloads = [s for s in server.seen if s[0].startswith("/login/members.zip") and s[1]]
    check(downloads, "server never saw the download")
    for path, cookie, referer, agent in downloads:
        check("session=ok" in cookie, "cookie missing on %s" % path)
        check(referer == "https://example.com/files", "referer missing: %r" % referer)
        check(agent == "TestBrowser/1.0", "user agent not passed: %r" % agent)


def t_verify_refuses(inst):
    # No login cookie: the site answers with a login page, so dm must decline
    # and leave the download to the browser, creating no job.
    reply = inst.dm_raw({"cmd": "add", "url": inst.base + "/login/locked.zip", "mime": "application/zip",
                         "verify": True})
    check(reply.get("ok") is False and reply.get("refused"), "login page accepted: %s" % reply)
    check("web page" in reply["error"], "unclear reason: %s" % reply["error"])
    reply = inst.dm_raw({"cmd": "add", "url": inst.base + "/status/403", "verify": True})
    check(reply.get("ok") is False and "403" in reply["error"], "403 accepted: %s" % reply)
    check(inst.state()["jobs"] == [], "a refused hand-off left a job behind")
    # Asking for a web page on purpose is fine.
    reply = inst.dm_raw({"cmd": "add", "url": inst.base + "/login/page.html", "mime": "text/html", "verify": True})
    check(reply.get("ok"), "an expected web page was refused: %s" % reply)


def t_native_host(inst):
    # The bridge the browser starts: length-prefixed JSON on stdin/stdout.
    import struct

    def talk(*messages):
        data = b"".join(struct.pack("=I", len(m)) + m for m in (json.dumps(x).encode() for x in messages))
        out = subprocess.run([os.path.join(ROOT, "bin", "dm-native-host")], input=data, env=inst.env,
                             capture_output=True, timeout=30).stdout
        replies = []
        while out:
            (n,) = struct.unpack("=I", out[:4])
            replies.append(json.loads(out[4:4 + n]))
            out = out[4 + n:]
        return replies

    ping, = talk({"cmd": "ping"})
    check(ping.get("ok") and ping.get("version"), "ping failed: %s" % ping)
    added, = talk({"cmd": "add", "url": inst.base + "/login/viahost.bin?size=%d" % SIZE, "cookies": "session=ok",
                   "referer": "https://example.com/", "user_agent": "TestBrowser/1.0",
                   "mime": "application/octet-stream", "verify": True})
    check(added.get("ok"), "add through the host failed: %s" % added)
    job = expect_done(inst, added["job"]["id"], "viahost.bin", SIZE)
    looked, = talk({"cmd": "job", "id": job["id"]})
    check(looked.get("ok") and looked["job"]["status"] == "done", "job lookup failed: %s" % looked)
    refused, = talk({"cmd": "add", "url": inst.base + "/login/nope.bin", "mime": "application/octet-stream",
                     "verify": True})
    check(refused.get("ok") is False and refused.get("refused"), "host passed a refusal through as ok")
    bad, = talk({"cmd": "add", "url": inst.base + "/ok/x.bin", "cookies": "a=b\r\nX-Evil: 1"})
    check(bad.get("ok") is False, "header injection through cookies accepted")
    inst.stop()
    offline, = talk({"cmd": "ping"})
    check(offline.get("ok") is False and offline.get("offline"), "offline engine not reported: %s" % offline)


def t_security(inst):
    run = os.path.join(inst.run_dir, "dm")
    for name in ("ctl.sock", "aria2.conf"):
        mode = os.stat(os.path.join(run, name)).st_mode & 0o777
        check(mode == 0o600, "%s is mode %o" % (name, mode))
    conf = open(os.path.join(run, "aria2.conf")).read()
    check("on-download" not in conf and "on-bt" not in conf, "aria2 has a completion hook")
    check("rpc-listen-all=false" in conf, "aria2 RPC listens beyond localhost")
    port = next(line.split("=")[1] for line in conf.splitlines() if line.startswith("rpc-listen-port="))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "aria2.getVersion", "params": []}).encode()
    req = urllib.request.Request("http://127.0.0.1:%s/jsonrpc" % port, data=body)
    try:
        reply = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5))
    except urllib.error.HTTPError as e:
        reply = json.load(e)
    check("error" in reply, "aria2 answered without the secret: %s" % reply)
    check(inst.dm("add", "file:///etc/passwd").get("ok") is False, "file:// URL accepted")
    check(inst.dm("add", inst.base + "/ok/x.bin", "--name", "../escape.bin").get("ok") is False, "path in --name accepted")


def t_config_injection(inst):
    # A newline in a setting must not become an extra aria2 option.
    inst.stop()
    with open(os.path.join(inst.root, "config/dm/config.json"), "w") as f:
        json.dump({"download_dir": inst.downloads, "notify": False,
                   "speed_limit": "1M\non-download-complete=/bin/true", "max_concurrent": "lots"}, f)
    inst.start()
    conf = open(os.path.join(inst.run_dir, "dm", "aria2.conf")).read()
    check("on-download-complete" not in conf, "setting injected an aria2 option")
    check("max-overall-download-limit=0\n" in conf, "bad speed_limit not reset")
    check("max-concurrent-downloads=3\n" in conf, "bad max_concurrent not reset")


def t_disk_full(work, base):
    # A 3 MiB tmpfs in a private mount namespace; needs unprivileged user namespaces.
    if subprocess.run(["unshare", "-rm", "true"], capture_output=True).returncode != 0:
        print("    (skipped: no unprivileged user namespaces)")
        return
    inst = Instance(work, "diskfull", base)
    wrap = ["unshare", "-rm", "sh", "-c", 'mount -t tmpfs -o size=3m tmpfs "$0" && exec "$@"', inst.downloads]
    inst.wrap = wrap
    inst.start()
    try:
        jid = inst.add("/ok/huge.bin?size=%d" % (8 << 20))
        job = inst.finish(jid, 30)
        check(job["status"] == "failed", "expected a failure, got %s" % job["status"])
        check("space" in job["error"]["text"].lower(), "unclear error: %s" % job["error"])
        # The tmpfs is only visible inside the namespace, so check status only.
        small = inst.finish(inst.add("/ok/small.bin?size=%d" % (1 << 20)), 30)
        check(small["status"] == "done", "a file that fits failed: %s" % small.get("error"))
    finally:
        inst.stop()


TESTS = [
    ("ok", t_ok), ("no-range-and-drop", t_no_range_and_drop),
    ("unresumable-pause", t_unresumable_pause), ("drop-resume", t_drop_resume),
    ("file-changes", t_file_changes), ("redirects", t_redirects), ("utf8-name", t_utf8_name),
    ("not-found", t_not_found), ("forbidden", t_forbidden), ("server-error-retries", t_server_error_retries),
    ("checksum", t_checksum), ("same-name-twice", t_same_name_twice), ("pause-resume", t_pause_resume),
    ("cancel", t_cancel), ("kill-daemon", t_kill_daemon), ("kill-aria2", t_kill_aria2), ("network-outage", t_network_outage),
    ("browser-handoff", t_browser_handoff), ("verify-refuses", t_verify_refuses), ("native-host", t_native_host),
    ("security", t_security), ("config-injection", t_config_injection),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", default=None, help="scratch folder (default: a new temp dir)")
    parser.add_argument("-k", dest="only", default=None, help="run tests whose name contains this")
    args = parser.parse_args()
    if not shutil.which("aria2c"):
        sys.exit("aria2c is not installed")
    work = args.work or tempfile.mkdtemp(prefix="dm-tests-")
    os.makedirs(work, exist_ok=True)
    srv = server.serve(0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    print("work dir: %s" % work)
    failed = []
    selected = [(n, f) for n, f in TESTS if not args.only or args.only in n]
    for name, fn in selected:
        inst = Instance(work, name, base)
        started = time.time()
        try:
            inst.start()
            fn(inst)
            print("  ok    %-22s %5.1fs" % (name, time.time() - started))
        except Exception as e:
            failed.append(name)
            print("  FAIL  %-22s %s" % (name, e))
            if not isinstance(e, Failure):
                traceback.print_exc()
        finally:
            inst.stop()
    ran = len(selected)
    if not args.only or args.only in "disk-full":
        ran += 1
        started = time.time()
        try:
            t_disk_full(work, base)
            print("  ok    %-22s %5.1fs" % ("disk-full", time.time() - started))
        except Exception as e:
            failed.append("disk-full")
            print("  FAIL  %-22s %s" % ("disk-full", e))
    print("%d passed, %d failed" % (ran - len(failed), len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
