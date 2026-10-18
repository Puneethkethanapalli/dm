"""Video and audio pages (YouTube and the other sites yt-dlp supports).

A "media" job is downloaded by its own yt-dlp process instead of aria2. It
lives in the same queue and has the same lifecycle: progress shows in the bar,
pause stops the process (yt-dlp keeps its .part files, so resume continues),
and the finished file is moved out of the job's staging folder like any other.
yt-dlp's options mirror Omarchy's own Download Video integration.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_RUNNING = 2

# Failures that may go away on their own: worth retrying later.
TRANSIENT = re.compile(r"HTTP Error 5\d\d|timed? ?out|Connection|Temporary failure|Network is unreachable"
                       r"|IncompleteRead|Remote end closed|getaddrinfo", re.I)


def available():
    return shutil.which("yt-dlp") is not None


def detect(url):
    """The yt-dlp extractor that would handle url ("Youtube", ...) or None.

    Runs in a short-lived process so the daemon doesn't keep yt-dlp's few
    thousand extractor classes in memory.
    """
    if not available():
        return None
    try:
        out = subprocess.run([sys.executable, "-B", "-s", os.path.join(HERE, "media_detect.py"), url],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def number(text):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


class Runner:
    """Starts, watches and stops yt-dlp processes for media jobs."""

    def __init__(self, daemon):
        self.d = daemon
        self.procs = {}

    def command(self, job):
        argv = [
            "yt-dlp", "--no-playlist", "--no-simulate", "--quiet", "--no-warnings",
            # Never run commands, whatever the user's yt-dlp config says.
            "--no-exec", "--no-exec-before-download",
            "--progress", "--newline",
            "--progress-template",
            "download:DMPROG\t%(progress.downloaded_bytes)s\t%(progress.total_bytes)s"
            "\t%(progress.total_bytes_estimate)s\t%(progress.speed)s",
            "--print", "before_dl:DMTITLE\t%(title)j",
            "--print", "after_move:DMFILE\t%(filepath)j",
            "--paths", self.d.staging_dir(job), "-o", "%(title)s.%(ext)s",
        ]
        limit = str(self.d.config.get("speed_limit") or "0")
        if limit != "0":
            argv += ["--limit-rate", limit]
        return argv + ["--", job["url"]]

    def start(self, job):
        os.makedirs(self.d.staging_dir(job), exist_ok=True)
        proc = subprocess.Popen(self.command(job), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        self.procs[job["id"]] = proc
        job["_media_file"] = None
        job["_media_error"] = ""
        threading.Thread(target=self.watch, args=(job["id"], proc), daemon=True).start()

    def watch(self, job_id, proc):
        for line in proc.stdout:
            line = line.rstrip("\n")
            with self.d.lock:
                job = self.d.by_id(job_id)
                if not job or self.procs.get(job_id) is not proc:
                    continue
                if line.startswith("DMPROG\t"):
                    done, total, estimate, speed = (line.split("\t") + [""] * 4)[1:5]
                    job["completed"] = number(done) or job.get("completed", 0)
                    job["total"] = number(total) or number(estimate) or job.get("total", 0)
                    job["_speed"] = number(speed) or 0
                    self.d.set_status(job, "active")
                elif line.startswith("DMTITLE\t"):
                    try:
                        job["title"] = str(json.loads(line[8:]))[:300]
                    except ValueError:
                        pass
                elif line.startswith("DMFILE\t"):
                    try:
                        job["_media_file"] = str(json.loads(line[7:]))
                    except ValueError:
                        pass
                elif line.startswith("ERROR:"):
                    job["_media_error"] = line[6:].strip()[:300]
        code = proc.wait()
        with self.d.lock:
            job = self.d.by_id(job_id)
            if not job or self.procs.get(job_id) is not proc:
                return  # stopped on purpose (pause, cancel, shutdown)
            del self.procs[job_id]
            staged = job.get("_media_file")
            if code == 0 and staged and os.path.exists(staged):
                job["staged"] = staged
                job["_speed"] = 0
                self.d.place(job)
            else:
                self.d.media_failed(job, job.get("_media_error") or "yt-dlp exited with status %d" % code)
        self.d.wake.set()

    def stop(self, job):
        proc = self.procs.pop(job["id"], None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        job["_speed"] = 0

    def pump(self):
        """Start queued media jobs, a couple at a time."""
        if not available():
            return
        for job in self.d.jobs:
            if len(self.procs) >= MAX_RUNNING:
                return
            if job.get("kind") == "media" and job["status"] == "queued" and job["id"] not in self.procs:
                try:
                    self.start(job)
                except OSError as e:
                    self.d.fail(job, 1, "Could not start yt-dlp", str(e))

    def stop_all(self):
        for job in list(self.d.jobs):
            if job["id"] in self.procs:
                self.stop(job)
