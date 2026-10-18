#!/usr/bin/env python3
"""dm daemon: owns the download queue, supervises aria2c, serves `dm` and the bar.

The queue in jobs.json is the source of truth. aria2 only ever holds jobs that
are queued or transferring; a paused job is taken out of aria2 and keeps its
partial file and control file in a per-job staging folder. Every (re)start of
a transfer goes through submit(), so recovery after a crash, a retry and a
resume all take the same path.

Finished files are moved out of staging with a no-clobber link + unlink, so
the download folder never holds a half-written file under its final name.
"""

import sys

# Never write __pycache__ into the plugin folder: the shell reloads every
# plugin when a file under ~/.config/omarchy/plugins/ changes.
sys.dont_write_bytecode = True

import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import aria2
import store

HERE = os.path.dirname(os.path.abspath(__file__))
UNFINISHED = ("queued", "active", "checking", "paused", "retrying", "finishing")
FINISHED = ("done", "failed", "cancelled")
IN_ENGINE = ("queued", "active")
# Seconds between dm-level retries. DM_BACKOFF="1,2" shortens them for tests.
BACKOFF = tuple(int(x) for x in os.environ.get("DM_BACKOFF", "").split(",") if x.strip()) or (5, 15, 45, 120, 300)
STATUS_KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed",
               "errorCode", "errorMessage", "files", "connections"]
SCHEMES = ("http", "https", "ftp", "sftp")
PARTIAL_DIR = ".dm-partial"


def read_version():
    try:
        with open(os.path.join(HERE, "..", "manifest.json")) as f:
            return str(json.load(f).get("version", "0"))
    except (OSError, ValueError):
        return "0"


VERSION = read_version()


def now():
    return time.time()


class Refused(Exception):
    """A command the daemon declines, with a message for the user."""


# ---------------------------------------------------------------- validation

def check_url(url):
    url = str(url or "").strip()
    if not url or len(url) > 8192 or any(c.isspace() for c in url):
        raise Refused("Not a valid URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in SCHEMES or not parts.netloc:
        raise Refused("Only http, https, ftp and sftp links can be downloaded")
    return url


def check_name(name):
    if name is None or name == "":
        return None
    name = str(name)
    if "/" in name or "\0" in name or name in (".", "..") or len(name) > 255:
        raise Refused("File name must be a plain name without folders")
    return name


def check_dir(path, default):
    path = os.path.abspath(os.path.expanduser(str(path))) if path else default
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise Refused("Can't use folder %s: %s" % (path, e.strerror or e))
    if not os.access(path, os.W_OK | os.X_OK):
        raise Refused("Folder %s is not writable" % path)
    return path


def check_sha256(value):
    if not value:
        return None
    value = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Refused("SHA-256 must be 64 hex characters")
    return value


def check_headers(headers):
    out = []
    for h in headers or []:
        h = str(h)
        if "\n" in h or "\r" in h or ":" not in h:
            raise Refused("Headers look like 'Name: value'")
        out.append(h)
    return out


def check_text(value, what, limit):
    if value is None or value == "":
        return None
    value = str(value)
    if any(c in value for c in "\r\n\0") or len(value) > limit:
        raise Refused("%s is not valid" % what)
    return value


def guess_name(url):
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    return base or parts.hostname or "download"


# -------------------------------------------------------------------- files

def staging_dir(job):
    return os.path.join(job["dir"], PARTIAL_DIR, job["id"])


def remove_staging(job):
    path = staging_dir(job)
    # Only ever delete our own per-job folder.
    if os.path.basename(os.path.dirname(path)) == PARTIAL_DIR and len(job["id"]) == 16:
        shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


def candidate_names(directory, name):
    stem, ext = os.path.splitext(name)
    if stem.endswith(".tar"):  # keep .tar.gz and friends together
        stem, ext = stem[:-4], ".tar" + ext
    yield os.path.join(directory, name)
    for n in range(1, 10000):
        yield os.path.join(directory, "%s (%d)%s" % (stem, n, ext))


def place_file(staged, directory, record_target):
    """Move staged into directory without overwriting anything.

    record_target(path) is called (and must persist) before the move so a crash
    in between can be finished idempotently by finish_placement().
    """
    name = os.path.basename(staged)
    for target in candidate_names(directory, name):
        if os.path.lexists(target):
            continue
        record_target(target)
        try:
            os.link(staged, target)
        except FileExistsError:
            continue
        except OSError:
            # No hard links on this filesystem (e.g. exFAT): rename instead.
            if os.path.lexists(target):
                continue
            os.rename(staged, target)
            return target
        os.unlink(staged)
        return target
    raise OSError("no free file name for %s" % name)


# ------------------------------------------------------------------ probing

_PROBE_OPENER = urllib.request.build_opener()


def inspect_remote(remote):
    """Ask the server about a file with a one-byte ranged GET.

    remote is (url, headers, user_agent, referer). Returns status, content
    type, validators (ETag, Last-Modified, length) and whether the server can
    resume; status 0 with an error when it couldn't be reached. A ranged GET
    rather than HEAD: signed links often only allow GET. A 206 answer means
    the server honours ranges; a 200 means it always sends the whole file.
    """
    url, headers, user_agent, referer = remote
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        return {"status": 0, "error": "not http"}
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": user_agent})
    if referer:
        req.add_header("Referer", referer)
    for h in headers or []:
        k, _, v = h.partition(":")
        req.add_header(k.strip(), v.strip())
    try:
        with _PROBE_OPENER.open(req, timeout=10) as resp:
            length = None
            m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
            if m:
                length = int(m.group(1))
            elif resp.status == 200 and resp.headers.get("Content-Length"):
                length = int(resp.headers["Content-Length"])
            etag = resp.headers.get("ETag") or None
            modified = resp.headers.get("Last-Modified") or None
            return {
                "status": resp.status,
                "content_type": (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower(),
                # Weak ETags and a Last-Modified equal to Date (generated per
                # request) say nothing about the bytes.
                "etag": None if etag and etag.startswith("W/") else etag,
                "last_modified": None if modified and modified == resp.headers.get("Date") else modified,
                "length": length,
                "resumable": resp.status == 206,
            }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "HTTP %d" % e.code}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"status": 0, "error": str(getattr(e, "reason", e))}


def probe(remote):
    """Validators and resumability of a file the server serves, or None."""
    found = inspect_remote(remote)
    if found["status"] not in (200, 206):
        return None
    return {k: found[k] for k in ("etag", "last_modified", "length", "resumable")}


def validators_changed(old, new):
    if not old or not new:
        return False
    for key in ("etag", "last_modified", "length"):
        if old.get(key) is not None and new.get(key) is not None and old[key] != new[key]:
            return True
    return False


# ------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self):
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = False
        self.config = store.load_config()
        self.jobs = store.load_jobs()
        self.engine = aria2.Aria2(self.config)
        self.engine_error = ""
        self.next_engine_try = 0
        self.online = True
        self.jobs_dirty = False
        self.last_jobs_save = 0
        self.last_snapshot = None
        self.last_snapshot_at = 0
        self.children = []
        self.inhibitor = None
        self.lost = {}
        for job in self.jobs:
            if job["status"] == "checking":
                job["status"] = "queued"

    # ------------------------------------------------------------ helpers

    def remote(self, job):
        """What a request for this job sends: (url, headers, user agent, referer)."""
        ua = job.get("user_agent") or "aria2/" + (self.engine.version or "1.37.0")
        return (job["url"], list(job.get("headers") or []), ua, job.get("referer"))

    def find(self, ref):
        ref = str(ref or "").strip().lower()
        if not ref:
            raise Refused("Which download?")
        matches = [j for j in self.jobs if j["id"].startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        raise Refused("No download matches %s" % ref if not matches else "%s matches several downloads" % ref)

    def select(self, ref, statuses):
        if ref == "all":
            return [j for j in self.jobs if j["status"] in statuses]
        return [self.find(ref)]

    def touch(self, job, durable=True):
        job["updated"] = now()
        if durable:
            self.jobs_dirty = True

    def set_status(self, job, status):
        if job["status"] != status:
            job["status"] = status
            self.touch(job)

    def persist(self, force=False):
        # Status changes save at once; progress at most every 10 s.
        if force or self.jobs_dirty or now() - self.last_jobs_save > 10:
            durable = [{k: v for k, v in j.items() if not k.startswith("_")} for j in self.jobs]
            try:
                store.save_jobs(durable)
                self.jobs_dirty = False
                self.last_jobs_save = now()
            except OSError as e:
                store.log("could not save jobs: %s" % e)

    # ------------------------------------------------------------- engine

    def ensure_engine(self):
        if self.engine.alive() or now() < self.next_engine_try:
            return
        was_running = self.engine.port is not None
        try:
            self.engine.start()
        except aria2.RPCError as e:
            self.engine_error = str(e)
            self.next_engine_try = now() + 5
            return
        self.engine_error = ""
        if was_running:
            store.log("aria2c restarted; resubmitting unfinished downloads")
        for job in self.jobs:
            if job["status"] in IN_ENGINE:
                self.start_transfer(job)
            elif job["status"] == "finishing":
                self.finish(job, job.get("staged"))

    def start_transfer(self, job):
        """(Re)start a transfer, first checking a partial file is still valid."""
        if job.get("resumable") is False and job.get("completed", 0) > 0:
            # The server can't continue from the middle; start clean.
            remove_staging(job)
            job["completed"] = 0
            self.submit(job)
        elif job.get("completed", 0) > 0 and job.get("validators"):
            self.set_status(job, "checking")
            threading.Thread(target=self._check_then_submit, args=(job["id"],), daemon=True).start()
        else:
            self.submit(job)

    def _check_then_submit(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job:
                return
            remote, old = self.remote(job), job.get("validators")
        fresh = probe(remote)
        with self.lock:
            if job not in self.jobs or job["status"] != "checking":
                return
            if fresh:
                job["resumable"] = fresh.pop("resumable")
            if validators_changed(old, fresh):
                self.start_over(job, fresh)
            else:
                self.submit(job)
        self.wake.set()

    def start_over(self, job, fresh):
        """The remote file changed: throw away the partial download."""
        job["changes"] = job.get("changes", 0) + 1
        remove_staging(job)
        job["completed"] = 0
        job.pop("staged", None)
        if job["changes"] > 2:
            return self.fail(job, 10, "The file kept changing on the server")
        store.log("%s changed on the server; starting over" % job["id"])
        job["validators"] = fresh
        job["note"] = "The file changed on the server, so it restarted from the beginning"
        self.submit(job)

    def submit(self, job):
        if not self.engine.alive():
            self.set_status(job, "queued")  # picked up when aria2 is back
            return
        try:
            os.makedirs(staging_dir(job), exist_ok=True)
        except OSError as e:
            return self.fail(job, 18, "Could not create folder", str(e.strerror or e))
        options = {"gid": job["id"], "dir": staging_dir(job)}
        if job.get("name"):
            options["out"] = job["name"]
        if job.get("sha256"):
            options["checksum"] = "sha-256=" + job["sha256"]
        if job.get("headers"):
            options["header"] = job["headers"]
        if job.get("referer"):
            options["referer"] = job["referer"]
        if job.get("user_agent"):
            options["user-agent"] = job["user_agent"]
        if job.get("resumable") is False:
            # Extra connections would each ask for a middle part and be refused.
            options["split"] = "1"
            options["max-connection-per-server"] = "1"
        try:
            self.engine.call("removeDownloadResult", job["id"])
        except aria2.RPCError:
            pass
        try:
            self.engine.call("addUri", [job["url"]], options)
        except aria2.RPCError as e:
            if "not unique" not in str(e):
                return self.fail(job, 28, "aria2 refused the download", str(e))
        self.set_status(job, "queued")
        job.pop("next_retry", None)
        job.pop("waiting_network", None)
        if not job.get("validators"):
            threading.Thread(target=self._record_validators, args=(job["id"],), daemon=True).start()

    def _record_validators(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job:
                return
            remote = self.remote(job)
        found = probe(remote)
        if found:
            with self.lock:
                if job in self.jobs and not job.get("validators"):
                    job["resumable"] = found.pop("resumable")
                    job["validators"] = found
                    self.touch(job)

    def take_out(self, job):
        """Remove a job from aria2, keeping its partial file and control file."""
        if not self.engine.alive():
            return
        for method in ("forceRemove", "removeDownloadResult"):
            try:
                self.engine.call(method, job["id"])
            except aria2.RPCError:
                pass
        # forceRemove is asynchronous; wait until aria2 has let go of the files.
        for _ in range(40):
            try:
                st = self.engine.call("tellStatus", job["id"], ["status"])
            except aria2.RPCError:
                return
            if st.get("status") in ("removed", "complete", "error"):
                try:
                    self.engine.call("removeDownloadResult", job["id"])
                except aria2.RPCError:
                    pass
                return
            time.sleep(0.05)

    # --------------------------------------------------------------- sync

    def sync(self):
        """Pull transfer state from aria2 into the job list."""
        if not self.engine.alive():
            return
        tracked = [j for j in self.jobs if j["status"] in IN_ENGINE]
        if not tracked:
            return
        try:
            results = self.engine.multicall([("tellStatus", j["id"], STATUS_KEYS) for j in tracked])
        except aria2.RPCError as e:
            store.log("aria2 poll failed: %s" % e)
            return
        for job, st in zip(tracked, results):
            if isinstance(st, aria2.RPCError) or not isinstance(st, dict):
                # aria2 doesn't know it (it restarted under us): add it again.
                self.lost[job["id"]] = self.lost.get(job["id"], 0) + 1
                if self.lost[job["id"]] > 3:
                    self.fail(job, 1, "Lost track of the download", str(st))
                else:
                    self.submit(job)
                continue
            self.lost.pop(job["id"], None)
            self.apply_status(job, st)

    def apply_status(self, job, st):
        total = int(st.get("totalLength") or 0)
        completed = int(st.get("completedLength") or 0)
        job["total"] = total or job.get("total", 0)
        job["completed"] = completed if completed or not job.get("completed") else job["completed"]
        job["_speed"] = int(st.get("downloadSpeed") or 0)
        job["_connections"] = int(st.get("connections") or 0)
        files = st.get("files") or []
        path = files[0].get("path") if files else ""
        if path:
            job["_staged_name"] = os.path.basename(path)
        status = st.get("status")
        if status == "active":
            self.set_status(job, "active")
        elif status == "waiting":
            self.set_status(job, "queued")
        elif status == "complete":
            self.finish(job, path)
        elif status == "error":
            self.handle_error(job, st.get("errorCode"), st.get("errorMessage"))
        elif status == "removed":
            self.set_status(job, "cancelled")

    # ------------------------------------------------------------ outcomes

    def finish(self, job, staged):
        """aria2 is done: make sure the file didn't change mid-way, then place it."""
        if job["status"] != "finishing":
            job["status"] = "finishing"
            job["staged"] = staged
            self.touch(job)
            self.persist(force=True)
        try:
            self.engine.call("removeDownloadResult", job["id"])
        except aria2.RPCError:
            pass
        if job.get("_verifying"):
            return
        # aria2 resumes dropped connections without checking the file is still
        # the same; a checksum already proves it, otherwise ask the server.
        if job.get("validators") and not job.get("sha256") and not job.get("target"):
            job["_verifying"] = True
            threading.Thread(target=self._verify_then_place, args=(job["id"],), daemon=True).start()
        else:
            self.place(job)

    def _verify_then_place(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if not job:
                return
            remote, old = self.remote(job), job.get("validators")
        fresh = probe(remote)
        if fresh:
            fresh.pop("resumable")
        with self.lock:
            job.pop("_verifying", None)
            if job not in self.jobs or job["status"] != "finishing":
                return
            if validators_changed(old, fresh):
                self.start_over(job, fresh)
            else:
                self.place(job)
        self.wake.set()

    def place(self, job):
        staged = job.get("staged")
        target = job.get("target")
        try:
            if target and os.path.lexists(target) and (not staged or not os.path.exists(staged)):
                pass  # moved before a crash
            elif target and staged and os.path.exists(target) and os.path.exists(staged) and os.path.samefile(target, staged):
                os.unlink(staged)  # linked before a crash
            elif staged and os.path.exists(staged):
                def record(path):
                    job["target"] = path
                    self.touch(job)
                    self.persist(force=True)
                target = place_file(staged, job["dir"], record)
            else:
                return self.fail(job, 15, "The finished file went missing", staged or "")
        except OSError as e:
            return self.fail(job, 14, "Could not move the finished file", str(e.strerror or e))
        remove_staging(job)
        job["file"] = target
        job["completed"] = job.get("total") or job.get("completed", 0)
        job["finished_at"] = now()
        job.pop("error", None)
        job.pop("staged", None)
        self.set_status(job, "done")
        self.notify("Download complete", os.path.basename(target), job["dir"])

    def handle_error(self, job, code, message):
        code, text, detail = aria2.describe(code, message)
        try:
            self.engine.call("removeDownloadResult", job["id"])
        except aria2.RPCError:
            pass
        if "No space left" in (message or ""):
            code, text = 9, aria2.ERRORS[9]
        http = re.search(r"status=(\d{3})", message or "")
        status = int(http.group(1)) if http else 0
        client_error = 400 <= status < 500 and status not in (408, 425, 429)
        if code == 3 and not status:
            status = 404  # aria2 says only "Resource not found"
        if status:
            detail = "HTTP %d" % status
            if status in (401, 403):
                text = "The server refused access"
            elif status in (404, 410):
                text = aria2.ERRORS[3]
        if code == 8:
            job["resumable"] = False
        if code == 8 and not job.get("fresh_start"):
            # The server can't resume: start over from the first byte, once.
            job["fresh_start"] = True
            remove_staging(job)
            job["completed"] = 0
            job["note"] = "The server can't resume, so it restarted from the beginning"
            return self.submit(job)
        job["error"] = {"code": code, "text": text, "detail": detail}
        if code in aria2.TRANSIENT and not client_error and job.get("attempts", 0) < int(self.config["max_retries"]):
            self.set_status(job, "retrying")
            if self.online:
                job["attempts"] = job.get("attempts", 0) + 1
                job["next_retry"] = now() + BACKOFF[min(job["attempts"], len(BACKOFF)) - 1]
            self.touch(job)
            return
        self.fail(job, code, text, detail)

    def fail(self, job, code, text, detail=""):
        # Keep a partial file only when a manual retry could resume it.
        if code not in aria2.TRANSIENT:
            remove_staging(job)
            job["completed"] = 0
        job["error"] = {"code": code, "text": text, "detail": detail}
        job["finished_at"] = now()
        self.set_status(job, "failed")
        self.notify("Download failed", "%s: %s" % (self.display_name(job), text), None)

    def schedule(self):
        for job in self.jobs:
            if job["status"] != "retrying":
                continue
            if not self.online:
                # Offline: wait for the network instead of burning attempts.
                if not job.get("waiting_network"):
                    job["waiting_network"] = True
                    job.pop("next_retry", None)
                    self.touch(job)
                continue
            due = job.get("next_retry")
            if job.get("waiting_network") or due is None or now() >= due:
                self.start_transfer(job)

    def prune(self):
        keep = int(self.config["keep_finished"])
        finished = [j for j in self.jobs if j["status"] in FINISHED]
        if len(finished) > keep:
            oldest = sorted(finished, key=lambda j: j.get("finished_at") or 0)[:len(finished) - keep]
            for job in oldest:
                remove_staging(job)
            drop = {j["id"] for j in oldest}
            self.jobs = [j for j in self.jobs if j["id"] not in drop]
            self.jobs_dirty = True

    # ------------------------------------------------------------ commands

    def cmd_add(self, req):
        url = check_url(req.get("url"))
        directory = check_dir(req.get("dir"), self.config["download_dir"])
        name = check_name(req.get("name"))
        sha = check_sha256(req.get("sha256"))
        headers = check_headers(req.get("headers"))
        referer = check_text(req.get("referer"), "Referer", 8192)
        user_agent = check_text(req.get("user_agent"), "User agent", 1024)
        if not req.get("force"):
            for j in self.jobs:
                if j["url"] == url and j["status"] in UNFINISHED:
                    return {"job": self.public(j), "existing": True}
        job = {
            "id": secrets.token_hex(8),
            "url": url,
            "dir": directory,
            "name": name,
            "sha256": sha,
            "headers": headers,
            "referer": referer,
            "user_agent": user_agent,
            "source": check_text(req.get("source"), "Source", 32),
            "status": "queued",
            "created": now(),
            "updated": now(),
            "completed": 0,
            "total": 0,
            "attempts": 0,
        }
        checked = req.get("_checked")
        if checked:
            job["resumable"] = checked["resumable"]
            job["validators"] = {k: checked[k] for k in ("etag", "last_modified", "length")}
        self.jobs.append(job)
        self.jobs_dirty = True
        self.submit(job)
        self.persist(force=True)
        return {"job": self.public(job)}

    def cmd_pause(self, req):
        changed = []
        for job in self.select(req.get("id"), ("queued", "active", "checking", "retrying")):
            if job["status"] in FINISHED or job["status"] in ("paused", "finishing"):
                continue
            self.take_out(job)
            job.pop("next_retry", None)
            job.pop("waiting_network", None)
            self.set_status(job, "paused")
            changed.append(job["id"])
        restart = [j["id"] for j in self.jobs if j["id"] in changed and j.get("resumable") is False]
        return {"changed": changed, "will_restart": restart}

    def cmd_resume(self, req):
        changed = []
        for job in self.select(req.get("id"), ("paused", "retrying")):
            if job["status"] == "paused" or job["status"] == "retrying":
                job["attempts"] = 0
                self.start_transfer(job)
                changed.append(job["id"])
            elif job["status"] == "failed":
                changed += self.cmd_retry({"id": job["id"]})["changed"]
        return {"changed": changed}

    def cmd_retry(self, req):
        changed = []
        for job in self.select(req.get("id"), ("failed",)):
            if job["status"] not in ("failed", "cancelled", "retrying"):
                continue
            job["attempts"] = 0
            job.pop("error", None)
            job.pop("finished_at", None)
            self.start_transfer(job)
            changed.append(job["id"])
        return {"changed": changed}

    def cmd_cancel(self, req):
        changed = []
        for job in self.select(req.get("id"), ("queued", "active", "checking", "paused", "retrying")):
            if job["status"] in FINISHED or job["status"] == "finishing":
                continue
            self.take_out(job)
            remove_staging(job)
            job["completed"] = 0
            job["finished_at"] = now()
            job.pop("next_retry", None)
            self.set_status(job, "cancelled")
            changed.append(job["id"])
        return {"changed": changed}

    def cmd_remove(self, req):
        """Drop finished jobs from the list (the downloaded files stay)."""
        ref = req.get("id")
        if ref == "finished":
            drop = [j for j in self.jobs if j["status"] in FINISHED]
        else:
            job = self.find(ref)
            if job["status"] not in FINISHED:
                self.cmd_cancel({"id": job["id"]})
            drop = [job]
        ids = {j["id"] for j in drop}
        for job in drop:
            remove_staging(job)
        self.jobs = [j for j in self.jobs if j["id"] not in ids]
        self.jobs_dirty = True
        return {"changed": sorted(ids)}

    def cmd_status(self, req):
        return {"state": self.snapshot()}

    def cmd_version(self, req):
        return {"version": VERSION, "aria2": self.engine.version}

    COMMANDS = {
        "add": cmd_add, "pause": cmd_pause, "resume": cmd_resume, "retry": cmd_retry,
        "cancel": cmd_cancel, "remove": cmd_remove, "status": cmd_status, "version": cmd_version,
    }

    def verify(self, req):
        """Check dm can really fetch a download before the browser drops its copy.

        Runs outside the lock: the server may take seconds to answer.
        """
        try:
            url = check_url(req.get("url"))
            headers = check_headers(req.get("headers"))
            ua = check_text(req.get("user_agent"), "User agent", 1024) or "aria2/" + (self.engine.version or "1.37.0")
            referer = check_text(req.get("referer"), "Referer", 8192)
        except Refused as e:
            return str(e)
        found = inspect_remote((url, headers, ua, referer))
        if found["status"] not in (200, 206):
            return "The website wouldn't give dm this file (%s)" % found.get("error", "HTTP %d" % found["status"])
        expected = str(req.get("mime") or "").lower()
        if found["content_type"] == "text/html" and not expected.startswith("text/html"):
            # Typically a login or "are you human" page instead of the file.
            return "The website answered with a web page instead of the file"
        req["_checked"] = found
        return None

    def handle(self, req):
        cmd = self.COMMANDS.get(req.get("cmd") if isinstance(req, dict) else None)
        if not cmd:
            return {"ok": False, "error": "Unknown command"}
        req.pop("_checked", None)  # only verify() may set this
        if req.get("cmd") == "add" and req.get("verify"):
            problem = self.verify(req)
            if problem:
                return {"ok": False, "error": problem, "refused": True}
        with self.lock:
            try:
                result = cmd(self, req)
            except Refused as e:
                return {"ok": False, "error": str(e)}
            self.persist()
            self.publish(force=True)
        self.wake.set()
        result["ok"] = True
        return result

    # ------------------------------------------------------------- output

    def display_name(self, job):
        if job.get("file"):
            return os.path.basename(job["file"])
        return job.get("name") or job.get("_staged_name") or guess_name(job["url"])

    def public(self, job):
        speed = job.get("_speed", 0) if job["status"] == "active" else 0
        total, completed = job.get("total", 0), job.get("completed", 0)
        eta = int((total - completed) / speed) if speed > 0 and total > completed else None
        return {
            "id": job["id"],
            "name": self.display_name(job),
            "url": job["url"],
            "dir": job["dir"],
            "file": job.get("file"),
            "status": job["status"],
            "total": total,
            "completed": completed,
            "speed": speed,
            "eta": eta,
            "connections": job.get("_connections", 0) if job["status"] == "active" else 0,
            "error": job.get("error"),
            "note": job.get("note"),
            "source": job.get("source"),
            # None until known; False: the server can't continue a download.
            "resumable": job.get("resumable"),
            "attempts": job.get("attempts", 0),
            "next_retry": job.get("next_retry"),
            "waiting_network": bool(job.get("waiting_network")),
            "created": job.get("created"),
            "finished_at": job.get("finished_at"),
        }

    def snapshot(self):
        jobs = sorted(self.jobs, key=lambda j: j.get("created") or 0, reverse=True)
        counts = {s: 0 for s in UNFINISHED + FINISHED}
        speed = total = completed = 0
        sized = True
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
            if j["status"] in ("queued", "active", "checking", "retrying", "paused"):
                if j["status"] == "active":
                    speed += j.get("_speed", 0)
                if j.get("total"):
                    total += j["total"]
                    completed += j.get("completed", 0)
                elif j["status"] != "paused":
                    sized = False
        return {
            "schema": store.SCHEMA,
            "version": VERSION,
            "running": True,
            "pid": os.getpid(),
            "engine": {"ok": self.engine.alive(), "error": self.engine_error, "version": self.engine.version},
            "online": self.online,
            "download_dir": self.config["download_dir"],
            "counts": counts,
            "speed": speed,
            "total": total,
            "completed": completed,
            "progress": (completed / total) if total and sized else -1,
            "jobs": [self.public(j) for j in jobs],
        }

    def publish(self, force=False):
        snap = self.snapshot()
        key = json.dumps(snap, sort_keys=True)
        # A heartbeat every 5 s lets the bar tell a live daemon from a stale file.
        if not force and key == self.last_snapshot and now() - self.last_snapshot_at < 5:
            return
        snap["updated"] = now()
        try:
            store.save_snapshot(snap)
            self.last_snapshot = key
            self.last_snapshot_at = now()
        except OSError as e:
            store.log("could not write snapshot: %s" % e)

    def notify(self, headline, body, folder, urgent=False):
        if not self.config["notify"]:
            return
        if shutil.which("omarchy-notification-send"):
            argv = ["omarchy-notification-send", "--app-name", "dm", "-g", "󰇚",
                    "-u", "critical" if urgent else "normal", headline, body]
            if folder:
                argv += ["--exec", "xdg-open", folder]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "-a", "dm", "-u", "critical" if urgent else "normal", headline, body]
        else:
            return
        try:
            with self.lock:
                self.children.append(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL, start_new_session=True))
        except OSError:
            pass

    # -------------------------------------------------------- system state

    def watch_network(self):
        """Follow NetworkManager so retries wait for, then jump on, a connection."""
        if not shutil.which("nmcli"):
            return
        try:
            out = subprocess.run(["nmcli", "-t", "networking", "connectivity"], capture_output=True, text=True, timeout=5)
            with self.lock:
                self.online = out.stdout.strip() != "none"
        except (OSError, subprocess.SubprocessError):
            return
        while not self.stopping:
            try:
                proc = subprocess.Popen(["nmcli", "monitor"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                return
            with self.lock:
                self.children.append(proc)
            for line in proc.stdout:
                line = line.lower()
                if "connectivity is now" in line:
                    online = "'none'" not in line
                    with self.lock:
                        changed = online != self.online
                        self.online = online
                        if changed and online:
                            # Back online: retry now rather than after the backoff.
                            for job in self.jobs:
                                if job["status"] == "retrying":
                                    job["next_retry"] = now()
                    if changed:
                        store.log("network %s" % ("up" if online else "down"))
                        self.wake.set()
            if self.stopping:
                return
            time.sleep(5)

    def update_inhibitor(self):
        busy = any(j["status"] in ("active", "queued", "checking") for j in self.jobs)
        want = bool(self.config["inhibit_sleep"]) and busy
        if want and self.inhibitor is None and shutil.which("systemd-inhibit"):
            try:
                self.inhibitor = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep", "--who=dm", "--why=Downloading files",
                     "--mode=block", "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.inhibitor = None
        elif not want and self.inhibitor is not None:
            self.inhibitor.terminate()
            self.inhibitor = None

    def reap(self):
        with self.lock:
            self.children = [c for c in self.children if c.poll() is None]

    # ---------------------------------------------------------------- run

    def run(self):
        threading.Thread(target=self.watch_network, daemon=True).start()
        while not self.stopping:
            with self.lock:
                self.ensure_engine()
                self.sync()
                self.schedule()
                self.prune()
                self.update_inhibitor()
                self.persist()
                self.publish()
                busy = any(j["status"] in ("active", "queued", "checking", "finishing") for j in self.jobs)
            self.reap()
            self.wake.wait(0.5 if busy else 2.0)
            self.wake.clear()
        self.shutdown()

    def shutdown(self):
        with self.lock:
            # Unfinished jobs keep their status, so the next start resumes them.
            self.persist(force=True)
            self.engine.stop()
            if self.inhibitor is not None:
                self.inhibitor.terminate()
            for child in self.children:
                if child.poll() is None:
                    child.terminate()
            try:
                snap = self.snapshot()
                snap.update(running=False, updated=now())
                store.save_snapshot(snap)
            except OSError:
                pass


# ------------------------------------------------------------------- socket

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(1 << 20)
            req = json.loads(line or b"{}")
        except ValueError:
            reply = {"ok": False, "error": "Bad request"}
        else:
            try:
                reply = self.server.daemon_ref.handle(req)
            except Exception as e:  # never let one request kill the server
                store.log("command failed: %r" % e)
                reply = {"ok": False, "error": "Internal error: %s" % e}
        self.wfile.write((json.dumps(reply) + "\n").encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def single_instance():
    lock = open(os.path.join(store.RUNTIME_DIR, "dmd.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("dmd is already running", file=sys.stderr)
        sys.exit(1)
    return lock


def main():
    store.ensure_dirs()
    lock = single_instance()  # noqa: F841 (held for the process lifetime)
    daemon = Daemon()
    if os.path.lexists(store.SOCKET_PATH):
        os.unlink(store.SOCKET_PATH)
    old_umask = os.umask(0o177)
    server = Server(store.SOCKET_PATH, Handler)
    os.umask(old_umask)
    os.chmod(store.SOCKET_PATH, 0o600)
    server.daemon_ref = daemon
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(*_):
        daemon.stopping = True
        daemon.wake.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store.log("dmd %s started (downloads go to %s)" % (VERSION, daemon.config["download_dir"]))
    try:
        daemon.run()
    finally:
        server.shutdown()
        try:
            os.unlink(store.SOCKET_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
