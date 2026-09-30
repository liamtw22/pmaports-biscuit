"""Snapshot every read-only /api endpoint of the settings server, as JSON.

    settings-capture.py [--settings PATH] [--apps PATH] > capture.json

Run as root on a device. It loads biscuit-settings.py as a module (its main()
is guarded) and calls the same functions its GET handler does, so the result
is what the page would be served - for tools/settings-mock.py, which replays it
on a computer. Changes nothing.

--settings and --apps point at a newer biscuit-settings.py (with its helper
modules beside it) and apps.json than the installed ones, to capture pages
that are still being written.
"""
import argparse
import importlib.util
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--settings", default="/usr/bin/biscuit-settings.py")
ap.add_argument("--apps", default=None)
a = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(a.settings)))
sys.path.insert(1, "/usr/bin")
spec = importlib.util.spec_from_file_location("settings", a.settings)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
if a.apps and hasattr(s, "system"):
    s.system.APPS_FILE = a.apps

calls = {
    "/api/leds": lambda: s.state(),
    "/api/ring": lambda: s.ring_config.state(),
    "/api/audio": lambda: s.audio_state(),
    "/api/ringstate": lambda: s.ring_state(),
    "/api/lux": lambda: {"lux": s.agent.read_lux()},
    "/api/mic": lambda: s.mic_state(),
    "/api/sounds": lambda: s.sound_state(),
    "/api/wifi": lambda: s.wifi_state(scan=False),
    "/api/bluetooth": lambda: s.bt_state(scan=False),
    "/api/ssh": lambda: s.netcfg.ssh_state(),
    "/api/usb": lambda: s.usb_state(),
    "/api/about": lambda: s.about_state(),
    "/api/services": lambda: s.services.snapshot(),
    "/api/packages": lambda: s.packages_state(),
}
if hasattr(s, "system"):
    calls.update({
        "/api/apps": lambda: s.apps_state(),
        "/api/updates": lambda: s.updates_state(),
        "/api/homeassistant": lambda: s.ha_state(),
    })
    for path, fn in (("/api/datetime", "datetime_state"), ("/api/stock", "stock_state"),
                     ("/api/reset", "reset_plan"), ("/api/storage", "storage_state")):
        if hasattr(s.system, fn):
            calls[path] = getattr(s.system, fn)
    for app in s.system.manifest().get("apps", []):
        calls["/api/apps/detail?id=" + app["id"]] = (lambda i: lambda: s.system.app_detail(i))(app["id"])
if hasattr(s, "inventory_state"):
    import time
    s.inventory_state(fresh=True)
    time.sleep(2.2)          # a second reading, so the CPU figures exist
    inv = s.inventory_state(fresh=True)
    calls["/api/inventory"] = lambda: inv
    for name in inv["packages"]:
        calls["/api/package?name=" + name] = (lambda n: lambda: s.system.package_detail(n))(name)

out = {}
for path, fn in calls.items():
    try:
        out[path] = fn()
    except Exception as err:  # keep going; record what failed
        out[path] = {"__capture_error__": "%s: %s" % (type(err).__name__, err)}
json.dump(out, sys.stdout, indent=1, default=str)
