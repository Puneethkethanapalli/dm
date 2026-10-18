"""aria2c process and JSON-RPC client.

aria2 does the transfer work: resume, segmented downloads, redirects,
Content-Disposition names, checksums. dmd is its only client. The RPC port is
bound to 127.0.0.1 and every call carries a secret that only exists in a 0600
config file, so other local programs and web pages can't drive it.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

import store

# aria2 exit/error codes (aria2c(1), EXIT STATUS).
ERRORS = {
    1: "Unknown error",
    2: "Timed out",
    3: "File not found on the server",
    4: "File not found on the server",
    5: "Download was too slow",
    6: "Network problem",
    7: "Interrupted",
    8: "Server can't resume this download",
    9: "Not enough disk space",
    10: "Piece length changed",
    11: "Already downloading this file",
    13: "File already exists",
    14: "Could not rename file",
    15: "Could not open existing file",
    16: "Could not create file",
    17: "File I/O error",
    18: "Could not create folder",
    19: "Could not resolve host name",
    21: "FTP command failed",
    22: "Unexpected HTTP response",
    23: "Too many redirects",
    24: "Authorization failed",
    28: "Invalid option",
    29: "Server is busy or down for maintenance",
    32: "Checksum mismatch",
}

# Worth another try later: the network or the server may recover.
TRANSIENT = {1, 2, 5, 6, 7, 19, 22, 29}


def describe(code, message=""):
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 1
    text = ERRORS.get(code, "Download failed")
    # aria2's own message often carries the HTTP status; keep it short.
    detail = (message or "").strip().splitlines()[0][:160] if message else ""
    return code, text, detail


class RPCError(Exception):
    pass


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Aria2:
    def __init__(self, config):
        self.config = config
        self.proc = None
        self.port = None
        self.secret = None
        self.version = None
        self.binary = shutil.which("aria2c")

    # ------------------------------------------------------------ process

    def _write_conf(self):
        c = self.config
        options = {
            "enable-rpc": "true",
            "rpc-listen-all": "false",
            "rpc-listen-port": str(self.port),
            "rpc-secret": self.secret,
            "rpc-allow-origin-all": "false",
            # dmd re-adds unfinished jobs itself; aria2 only keeps control files.
            "continue": "true",
            "always-resume": "false",
            "max-resume-failure-tries": "0",
            "auto-save-interval": "5",
            "allow-overwrite": "false",
            "auto-file-renaming": "true",
            "remote-time": "true",
            "content-disposition-default-utf8": "true",
            "file-allocation": "falloc",
            "check-integrity": "true",
            "max-concurrent-downloads": str(int(c["max_concurrent"])),
            "split": str(int(c["connections_per_server"])),
            "max-connection-per-server": str(int(c["connections_per_server"])),
            "min-split-size": "4M",
            "max-tries": "3",
            "retry-wait": "3",
            "connect-timeout": "15",
            "timeout": "30",
            "max-overall-download-limit": str(c["speed_limit"] or "0"),
            # No torrents or metalinks fetched behind the user's back.
            "follow-torrent": "false",
            "follow-metalink": "false",
            "stop-with-process": str(os.getpid()),
            "console-log-level": "warn",
            "summary-interval": "0",
            "show-console-readout": "false",
            "download-result": "hide",
            "max-download-result": "1000",
            "keep-unfinished-download-result": "true",
        }
        text = "".join("%s=%s\n" % kv for kv in options.items())
        store.write_atomic(store.ARIA2_CONF_PATH, text, mode=0o600)

    def start(self):
        if not self.binary:
            self.binary = shutil.which("aria2c")
        if not self.binary:
            raise RPCError("aria2c is not installed")
        self.stop()
        last = None
        for _ in range(3):
            self.port = _free_port()
            self.secret = secrets.token_hex(24)
            self._write_conf()
            self.proc = subprocess.Popen(
                [self.binary, "--conf-path=" + store.ARIA2_CONF_PATH],
                stdin=subprocess.DEVNULL,
                start_new_session=False,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                try:
                    self.version = self.call("getVersion").get("version")
                    return
                except (RPCError, OSError) as e:
                    last = e
                    time.sleep(0.1)
            self.stop()
        raise RPCError("aria2c did not start: %s" % last)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.call("shutdown")
            except (RPCError, OSError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None

    # --------------------------------------------------------------- rpc

    # Never route the secret through an http_proxy from the environment.
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, payload):
        if not self.port:
            raise RPCError("aria2c is not running")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/jsonrpc" % self.port,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=5) as resp:
                reply = json.load(resp)
        except urllib.error.HTTPError as e:
            # aria2 answers RPC errors with HTTP 400 and a JSON body.
            try:
                reply = json.load(e)
            except ValueError:
                raise RPCError("aria2 HTTP %d" % e.code)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise RPCError(str(getattr(e, "reason", e)))
        except ValueError:
            raise RPCError("aria2 sent an unreadable reply")
        if isinstance(reply, dict) and "error" in reply:
            raise RPCError(reply["error"].get("message", "aria2 error"))
        return reply.get("result")

    def call(self, method, *params):
        return self._post({
            "jsonrpc": "2.0",
            "id": "dm",
            "method": "aria2." + method,
            "params": ["token:" + (self.secret or "")] + list(params),
        })

    def multicall(self, calls):
        """[(method, *params)] -> one result, or an RPCError, per call."""
        if not calls:
            return []
        token = "token:" + (self.secret or "")
        payload = [{"methodName": "aria2." + m, "params": [token] + list(p)} for m, *p in calls]
        out = []
        for item in self._post({"jsonrpc": "2.0", "id": "dm", "method": "system.multicall", "params": [payload]}):
            if isinstance(item, dict) and "code" in item:
                out.append(RPCError(item.get("message", "aria2 error")))
            else:
                out.append(item[0] if isinstance(item, list) and item else item)
        return out
