"""Talk to a running dmd over its control socket (used by `dm` and the browser host)."""

import json
import socket

import store


class Offline(Exception):
    pass


def request(payload, timeout=15):
    """Send one JSON command and return the daemon's JSON reply."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(store.SOCKET_PATH)
            s.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        raise Offline("The dm engine isn't running. Start it with: systemctl --user start dm")
    except OSError as e:
        raise Offline("Could not reach the dm engine: %s" % e)
    try:
        return json.loads(data)
    except ValueError:
        raise Offline("The dm engine sent an unreadable reply")
