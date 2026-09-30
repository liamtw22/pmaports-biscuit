#!/usr/lib/sendspin/bin/python3
"""Print the WebSocket URL of the Music Assistant Sendspin server on this network.

Browses _sendspin-server._tcp over mDNS for a few seconds. If several servers
answer, the one sendspin last played from wins (its settings keep that id);
otherwise the first. Prints nothing and exits 1 if none answers in time.

    biscuit-sendspin-server [SETTINGS_DIR] [SECONDS]

Why this exists (2.31): with sendspin listening, Music Assistant is the side
that connects, so after a stall the speaker comes back only if Music Assistant
retries or hears sendspin's mDNS re-announce. On 2026-09-27 it did neither for
nearly eight hours, through six watchdog restarts. Connecting out instead puts
the retry on this device: a 150 s stall recovered 25 s after the network came
back, and Music Assistant resumed the stream by itself.
"""
import json, sys, time
from pathlib import Path
from zeroconf import ServiceBrowser, Zeroconf

TYPE = "_sendspin-server._tcp.local."
settings_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/persist/sendspin")
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

try:
    preferred = json.loads((settings_dir / "settings-daemon.json").read_text()).get("last_played_server_id")
except (OSError, ValueError):
    preferred = None

found = {}


class Listener:
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name, 2000)
        if info is None:
            return
        v4 = [a for a in info.parsed_addresses() if ":" not in a]
        if not v4 or not info.port:
            return
        path = (info.properties or {}).get(b"path") or b"/sendspin"
        found[name[: -len(TYPE) - 1]] = "ws://%s:%d%s" % (v4[0], info.port, path.decode(errors="replace"))

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_, name):
        pass


zc = Zeroconf()
try:
    ServiceBrowser(zc, TYPE, Listener())
    end = time.monotonic() + seconds
    while time.monotonic() < end and (not found or (preferred and preferred not in found)):
        time.sleep(0.2)
finally:
    zc.close()

if not found:
    sys.exit(1)
print(found.get(preferred) or next(iter(found.values())))
