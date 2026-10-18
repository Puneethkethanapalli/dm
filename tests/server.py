#!/usr/bin/env python3
"""Misbehaving HTTP server for dm's fault-injection tests.

Every file is generated from its name, so a test knows the expected SHA-256
without storing anything. Routes (size in bytes via ?size=, default 4 MiB):

  /ok/NAME            Range, ETag, Last-Modified; the well-behaved case
  /norange/NAME       ignores Range, always 200 with the whole file
  /drop/NAME          first download request dies half-way, later ones work
  /norange-drop/NAME  both of the above
  /changing/NAME      first request dies half-way, then the file changes
                      (same size, new bytes, new ETag)
  /slow/NAME          ~rate bytes/s (?rate=, default 256 KiB/s)
  /redirect/N/NAME    N redirects, then /ok/NAME
  /disposition/NAME   name comes from a UTF-8 Content-Disposition (?as=)
  /status/CODE        always answers CODE
  /once/NAME          the first request gets the file, every later one 410
                      (like a one-time download link)
  /page?target=PATH   sets Cookie session=ok, then sends the browser to PATH
                      after ?delay= seconds (default 3), like a "your download
                      will start shortly" page
  /login/NAME         the file only with Cookie session=ok, else a 200
                      HTML login page; every request's Cookie, Referer and
                      User-Agent are recorded in `seen`

A one-byte ranged GET (dm's validator probe) never counts as a download
request, so it doesn't consume the one-shot failures above.

Run: python -I server.py PORT
"""

import hashlib
import http.server
import random
import sys
import threading
import time
import urllib.parse

lock = threading.Lock()
requests_seen = {}
versions = {}
seen = []  # (path, cookie, referer, user agent) of /login requests


def content(name, size, version=0):
    return random.Random("%s:%d:%d" % (name, size, version)).randbytes(size)


def expected_sha(name, size, version=0):
    return hashlib.sha256(content(name, size, version)).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def parse(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/")]
        return parts, query

    def is_probe(self):
        return self.headers.get("Range") == "bytes=0-0"

    def do_GET(self):
        parts, q = self.parse()
        route = parts[0] if parts else ""
        size = int(q.get("size", 4 << 20))
        if route == "status":
            return self.reply_status(int(parts[1]))
        if route == "page":
            target = q.get("target", "/ok/file.bin").replace('"', "%22")
            body = ('<!doctype html><meta http-equiv="refresh" content="%d;url=%s">'
                    '<p>Your download will start shortly.</p>' % (int(q.get("delay", 3)), target)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "session=ok; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "redirect":
            n = int(parts[1])
            rest = "/".join(parts[2:])
            target = "/redirect/%d/%s" % (n - 1, rest) if n > 1 else "/ok/%s" % rest
            if q:
                target += "?" + urllib.parse.urlencode(q)
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = "/".join(parts[1:]) or "file.bin"
        if route == "login":
            with lock:
                seen.append((self.path, self.headers.get("Cookie"), self.headers.get("Referer"),
                             self.headers.get("User-Agent")))
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                body = b"<html><body>Please log in</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        key = route + "/" + name + "?" + str(size)
        if route == "once":
            with lock:
                used = requests_seen.get(key, 0)
                requests_seen[key] = used + 1
            if used:
                return self.reply_status(410)
        first = False
        if not self.is_probe():
            with lock:
                requests_seen[key] = requests_seen.get(key, 0) + 1
                first = requests_seen[key] == 1
        version = versions.get(key, 0)
        data = content(name, size, version)
        ranges = route not in ("norange", "norange-drop")
        drop = first and route in ("drop", "norange-drop", "changing")
        rate = int(q.get("rate", 256 << 10 if route == "slow" else 0))
        disposition = q.get("as") if route == "disposition" else None
        def dropped():
            if route == "changing":
                with lock:
                    versions[key] = 1  # every later request sees the new file
        self.send_file(data, "%s-v%d" % (name, version), ranges, drop, rate, disposition, dropped)

    def reply_status(self, code):
        body = b"status %d\n" % code
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, data, tag, ranges, drop, rate, disposition, dropped=None):
        start, end = 0, len(data) - 1
        header = self.headers.get("Range", "")
        partial = False
        if ranges and header.startswith("bytes="):
            a, _, b = header[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = min(int(b), end) if b else end
            elif b:
                start = max(0, len(data) - int(b))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % len(data))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True
        body = data[start:end + 1]
        self.send_response(206 if partial else 200)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"%s"' % hashlib.md5(tag.encode()).hexdigest())
        self.send_header("Last-Modified", "Mon, 01 Jun 2026 12:00:00 GMT")
        if disposition:
            self.send_header("Content-Disposition",
                             "attachment; filename=\"fallback.bin\"; filename*=UTF-8''" + urllib.parse.quote(disposition))
        self.end_headers()
        cut = len(body) // 2 if drop else len(body)
        chunk = 64 << 10
        sent = 0
        try:
            while sent < cut:
                n = min(chunk, cut - sent)
                self.wfile.write(body[sent:sent + n])
                sent += n
                if rate:
                    time.sleep(n / rate)
                elif drop:
                    time.sleep(0.01)  # slow enough that the probe runs first
            if drop:
                if dropped:
                    dropped()
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve(port):
    server = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print("listening on %d" % srv.server_address[1], flush=True)
    threading.Event().wait()
