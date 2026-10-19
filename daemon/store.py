"""Paths, config, and crash-safe JSON files for dm.

Everything dm writes lives outside the plugin folder: Omarchy's shell reloads
every plugin when any file under ~/.config/omarchy/plugins/ changes.
"""

import json
import os
import re
import subprocess
import tempfile
import time

SCHEMA = 1


def _xdg(name, fallback):
    value = os.environ.get(name, "")
    return value if os.path.isabs(value) else os.path.expanduser(fallback)


STATE_DIR = os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "dm")
CONFIG_PATH = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "dm", "config.json")
RUNTIME_DIR = os.path.join(_xdg("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()), "dm")

JOBS_PATH = os.path.join(STATE_DIR, "jobs.json")
SNAPSHOT_PATH = os.path.join(RUNTIME_DIR, "state.json")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "ctl.sock")
ARIA2_CONF_PATH = os.path.join(RUNTIME_DIR, "aria2.conf")

# What setup installs outside the plugin folder.
UNIT_PATH = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "systemd", "user", "dm.service")
COMMAND_PATH = os.path.expanduser("~/.local/bin/dm")


def xdg_user_dir(name, fallback):
    try:
        out = subprocess.run(["xdg-user-dir", name], capture_output=True, text=True, timeout=3)
        path = out.stdout.strip()
        if out.returncode == 0 and os.path.isabs(path) and path != os.path.expanduser("~"):
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.expanduser(fallback)


def default_download_dir():
    return xdg_user_dir("DOWNLOAD", "~/Downloads")


DEFAULT_CONFIG = {
    "download_dir": None,          # None: the XDG download folder
    "video_dir": None,             # None: the XDG videos folder (video pages)
    "max_concurrent": 3,
    "connections_per_server": 8,
    "max_retries": 5,              # dm-level retries after aria2 gives up
    "speed_limit": "0",            # aria2 syntax, e.g. "2M"; "0" is unlimited
    "inhibit_sleep": False,        # hold off idle suspend while downloading
    "notify": True,
    "keep_finished": 50,           # finished jobs kept in the list
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        if isinstance(user, dict):
            config.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log("ignoring unreadable config %s: %s" % (CONFIG_PATH, e))
    if not re.fullmatch(r"\d+(\.\d+)?[KkMm]?", str(config["speed_limit"]).strip()):
        log("ignoring speed_limit %r (use e.g. \"2M\")" % config["speed_limit"])
        config["speed_limit"] = "0"
    for key in ("max_concurrent", "connections_per_server", "max_retries", "keep_finished"):
        try:
            config[key] = max(0 if key == "max_retries" else 1, int(config[key]))
        except (TypeError, ValueError):
            config[key] = DEFAULT_CONFIG[key]
    config["connections_per_server"] = min(16, config["connections_per_server"])  # aria2's maximum
    if not config["download_dir"]:
        config["download_dir"] = default_download_dir()
    config["download_dir"] = os.path.expanduser(config["download_dir"])
    config["video_dir"] = os.path.expanduser(config["video_dir"] or xdg_user_dir("VIDEOS", "~/Videos"))
    return config


def save_config_value(key, value):
    """Change one setting in config.json, keeping the rest of the file."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    config[key] = value
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    write_atomic(CONFIG_PATH, json.dumps(config, indent=2) + "\n", mode=0o644)


def ensure_dirs():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)


def write_atomic(path, data, mode=0o600, durable=True):
    """Replace path with data so readers only ever see the old or new file."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(data)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if durable:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_jobs():
    """The saved job list; a corrupt file is set aside rather than lost."""
    try:
        with open(JOBS_PATH) as f:
            data = json.load(f)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("no job list")
        return [j for j in jobs if isinstance(j, dict) and j.get("id") and j.get("url")]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        aside = "%s.corrupt-%d" % (JOBS_PATH, int(time.time()))
        try:
            os.replace(JOBS_PATH, aside)
        except OSError:
            pass
        log("job file unreadable (%s), moved to %s" % (e, aside))
        return []


def save_jobs(jobs):
    write_atomic(JOBS_PATH, json.dumps({"schema": SCHEMA, "jobs": jobs}, indent=1))


def save_snapshot(snapshot):
    # tmpfs, rewritten up to twice a second: no fsync needed.
    write_atomic(SNAPSHOT_PATH, json.dumps(snapshot), mode=0o600, durable=False)


def log(message):
    print(message, flush=True)
