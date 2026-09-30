#!/usr/bin/env python3
"""The device's settings pages, served on :8080 (or /opt/persist/settings-port).

Everything the device can be set to, from Wi-Fi, Bluetooth and accounts to the
microphones, sound and light ring, plus updates, storage and the stock files an
owner imports from their backup. Standalone: it works on a device with no Home
Assistant and no voice assistant installed.

HOW IT RELATES TO HOME ASSISTANT
--------------------------------
Home Assistant sees the device through linux-voice-assistant, which is forked
in lva-src/. The LED agent (biscuit-va-leds.py) registers an HA entity for most
settings here - named as the row is here - and both sides write the SAME files
under /opt/persist through the agent's functions, which this server loads as a
module (`agent`). Neither side caches: this server re-reads the files on every
request, and the agent pushes any change to HA within about a second. So a
change on either side shows on the other; the Microphone page polls for it.

Settings with no HA entity are the ones that belong on a page rather than a
dashboard: per-activity animations and sounds, network, security, updates. The
animation map is one of them:

    HA light entity   -> live manual control (colour, effect, on/off) right now
    this page         -> which animation each ACTIVITY should use, persistently

It lives in /opt/persist/led-map.json, which holds ONLY overrides: an activity
left on "Default" is absent from it entirely, rather than written out with the
stock value. That keeps the file small, makes "reset" a deletion, and means any
future correction to the defaults reaches users who never customised that
activity. biscuit-va-leds stat()s the file on every pipeline event, so a save
takes effect on the next wake word with no service restart.

THE PORTAL IS NOT A HOME FOR THIS
---------------------------------
biscuit-portal.py is the first-boot captive portal. It only runs during setup,
serves one form, and writes to /run - nothing it does survives to normal
operation. This is a long-running service on the device's real address.

Standard library only, matching the rest of the device's Python: the image has
no Flask and adding one for a settings page is not a trade worth making.
"""

import base64
import binascii
import errno
import gzip
import hashlib
import hmac
import html
import secrets
import time
import importlib.util
import ipaddress
import json
import biscuit_ring_config as ring_config
import biscuit_services as services
import biscuit_system as system
import biscuit_eq as eq_store
import shutil

# Stock files uploaded for an import wait here until the import checks them.
STOCK_STAGING = "/run/biscuit-stock-upload"
STOCK_UPLOAD_MAX = 2 << 20
# Uploads wait in /run, which is memory, until the import checks them. Each
# file is capped above; this caps them all together.
STOCK_STAGING_MAX = 16 << 20
import os
import socket
import socketserver
import subprocess
import sys
import urllib.parse
import threading
from http.server import BaseHTTPRequestHandler

try:
    from nacl.public import Box, PrivateKey, PublicKey
    _HAVE_NACL = True
except ImportError:  # pragma: no cover - only on a device missing py3-pynacl
    _HAVE_NACL = False
NACL_JS = "/usr/share/biscuit/nacl-fast.min.js"

def _durable_replace(tmp, dest):
    """os.replace, then make the rename ITSELF durable.

    os.replace is atomic - a reader never sees a half-written file - but it is
    not durable. After it returns, the new directory entry can still be only in
    page cache, and this device is power-cycled rather than shut down: every log
    on it carries NUL runs from exactly that. fsync on the containing directory
    is what commits the rename.

    The caller is expected to have fsynced the file's own contents first; this
    closes the other half. Best effort on purpose - the rename has already
    happened and succeeded by this point, so failing to sync is not worth
    raising over, and a directory that cannot be opened read-only is not a
    situation this can improve.

    Only used for destinations that must survive a power cut. Writes under /run
    are tmpfs, discarded at every boot, and deliberately still use os.replace.
    """
    os.replace(tmp, dest)
    try:
        fd = os.open(os.path.dirname(os.fspath(dest)) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _log(fmt, *args):
    """Module-level logging, matching the handler's [settings] prefix."""
    sys.stderr.write("[settings] " + (fmt % args if args else fmt) + "\n")
    sys.stderr.flush()


class _Logger:
    error = warning = info = staticmethod(_log)


_LOGGER = _Logger()

AGENT = "/usr/bin/biscuit-va-leds.py"
FX_MODULE = "/usr/bin/biscuit-ring-fx.py"
NETCFG = "/usr/bin/biscuit-netcfg.py"
STOCK_DIR = "/usr/share/biscuit-ring/led-resources"
CONTROL_FIFO = "/run/biscuit-ring/control"
# The port is the owner's choice (About > Name, or setup): http://<name>.local
# with 80, :8080 by default. The environment overrides it, for tests.
PORT = int(os.environ.get("BISCUIT_SETTINGS_PORT") or system.settings_port())

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# The credentials are the device user's own - the account created during
# first-boot setup - so there is no second password to invent, forget or ship.
#
# Verified against /etc/shadow directly. Python 3.14 REMOVED the `crypt` module,
# so the hash is recomputed with busybox `cryptpw` using the salt already stored
# in the entry, and compared. The password is fed on **stdin** (`-P 0`) rather
# than as an argument, because argv is visible to every user in `ps`.
#
# WHAT THIS IS NOT: the page is plain HTTP, so the password crosses the LAN in
# clear. That is the same exposure the first-boot captive portal already has for
# the Wi-Fi passphrase, and it is a real limitation rather than an oversight -
# see the note in the login page itself. A self-signed certificate would encrypt
# it at the cost of a browser warning on every visit; that trade is worth making
# deliberately, not by default.
SHADOW = "/etc/shadow"
PASSWD = "/etc/passwd"
SESSION_COOKIE = "biscuit_session"
SESSION_TTL_S = 12 * 3600
MIN_UID = 1000                  # never allow root or a system account

# Failed attempts per client address. A settings page on the LAN is not a
# high-value target, but an unthrottled login on a device with a human-chosen
# password is careless.
LOCKOUT_AFTER = 5
LOCKOUT_S = 300

# A settings page is used from one or two browsers. Anything beyond this is
# either abandoned sessions accumulating or someone collecting them.
MAX_SESSIONS = 8

_sessions = {}                  # token -> expiry
_failures = {}                  # client ip -> [count, first_failure_at]


def local_users():
    """Accounts a person could legitimately log in as."""
    users = set()
    try:
        with open(PASSWD) as f:
            for line in f:
                parts = line.split(":")
                if len(parts) > 6 and parts[6].strip() not in (
                        "/sbin/nologin", "/bin/false", "/usr/sbin/nologin"):
                    try:
                        if int(parts[2]) >= MIN_UID and int(parts[2]) < 65534:
                            users.add(parts[0])
                    except ValueError:
                        continue
    except OSError:
        pass
    return users


def verify_password(username, password):
    """True when `password` matches the account's stored hash."""
    if not username or not password or username not in local_users():
        return False
    stored = None
    try:
        with open(SHADOW) as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == username:
                    stored = parts[1]
                    break
    except OSError as err:
        _LOGGER.error("cannot read %s: %s", SHADOW, err)
        return False
    # "!" or "*" means the account is locked; an empty field means no password
    # at all, which must never be treated as "any password works".
    if not stored or stored[0] in "!*":
        return False
    fields = stored.split("$")
    if len(fields) < 4:
        return False
    algo, salt = fields[1], fields[2]
    try:
        out = subprocess.run(["cryptpw", "-P", "0", "-m", "sha512" if algo == "6" else algo,
                              "-S", salt],
                             input=password, capture_output=True, text=True, timeout=10)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("cryptpw failed: %s", err)
        return False
    return hmac.compare_digest(out.stdout.strip(), stored)


def stored_hash(username):
    """The account's stored password field, or None.

    Split out of verify_password so a session can be tied to the credential it
    was issued against.
    """
    try:
        with open(SHADOW) as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == username:
                    return parts[1] or None
    except OSError:
        return None
    return None


def credential_fingerprint(username):
    """A digest of the stored hash - never the hash itself.

    Kept in memory beside the session so a password change can invalidate it.
    Digested rather than stored directly so a memory disclosure does not hand
    over the shadow entry, which is the thing an attacker would take offline.
    """
    stored = stored_hash(username)
    if not stored:
        return None
    return hashlib.sha256(stored.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sealed secrets
#
# The page is plain HTTP on the LAN, so the sign-in password - the one SSH
# also accepts, for an account with passwordless root - used to cross the
# network readable by anything capturing it. The browser now boxes it with
# NaCl for a key this process makes when it starts, as the setup portal does
# for the Wi-Fi passphrase: PyNaCl here, the same vendored TweetNaCl there.
#
# Every box carries a one-time challenge from /seal. Without one, a captured
# box could simply be posted again to sign an eavesdropper in without their
# ever reading it.
#
# What this does not stop, as in setup: someone able to change the traffic,
# not just read it, serving their own page with their own key. That needs
# HTTPS with a certificate the browser trusts, which this device cannot have.
# ---------------------------------------------------------------------------

class Sealer:
    TTL = 300        # seconds a challenge stays good
    PER_CLIENT = 4   # outstanding challenges per address; its oldest go first
    MAX = 512        # all together, as a bound on memory

    def __init__(self):
        self.key = PrivateKey.generate() if _HAVE_NACL else None
        self.challenges = {}        # challenge -> (issued, client address)
        self.lock = threading.Lock()

    def issue(self, addr):
        """A challenge for one client. Kept per address, so a host asking
        for thousands evicts only its own and cannot expire the owner's
        between fetching one and posting the form."""
        if self.key is None:
            return {"key": "", "challenge": ""}
        now = time.monotonic()
        with self.lock:
            for c, (t, _a) in list(self.challenges.items()):
                if now - t > self.TTL:
                    del self.challenges[c]
            mine = sorted((t, c) for c, (t, a) in self.challenges.items() if a == addr)
            for _t, c in mine[:max(0, len(mine) - self.PER_CLIENT + 1)]:
                del self.challenges[c]
            while len(self.challenges) >= self.MAX:
                del self.challenges[min(self.challenges, key=lambda c: self.challenges[c][0])]
            c = secrets.token_urlsafe(18)
            self.challenges[c] = (now, addr)
        return {"key": base64.b64encode(bytes(self.key.public_key)).decode("ascii"),
                "challenge": c}

    def open(self, blob, addr):
        """'clientpub.nonce.ciphertext', all base64, to a dict. ValueError for
        anything that does not open, or a challenge not issued or used."""
        if self.key is None:
            raise ValueError("no key")
        try:
            pub, nonce, ct = (base64.b64decode(x, validate=True) for x in blob.split("."))
            plain = Box(self.key, PublicKey(pub)).decrypt(ct, nonce)
            data = json.loads(plain.decode("utf-8"))
        except Exception as err:                      # nacl raises CryptoError
            raise ValueError("could not open: %s" % err)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        c = data.pop("_c", None)
        with self.lock:
            entry = self.challenges.get(c) if isinstance(c, str) else None
            if entry is not None and entry[1] == addr:
                del self.challenges[c]
            else:
                entry = None
        if entry is None or time.monotonic() - entry[0] > self.TTL:
            raise ValueError("stale, reused or another client's challenge")
        return data


SEALER = Sealer()


def unseal(body, addr):
    """A request's fields, with any sealed ones opened into it."""
    if isinstance(body, dict) and "sealed" in body:
        data = SEALER.open(str(body["sealed"]), addr)
        out = {k: v for k, v in body.items() if k != "sealed"}
        out.update(data)
        return out
    return body


def throttled(addr):
    entry = _failures.get(addr)
    if not entry:
        return False
    count, first = entry
    if time.monotonic() - first > LOCKOUT_S:
        _failures.pop(addr, None)
        return False
    return count >= LOCKOUT_AFTER


def note_failure(addr):
    count, first = _failures.get(addr, (0, time.monotonic()))
    _failures[addr] = (count + 1, first)


def new_session(username, addr):
    """Issue a session bound to the account, its credential, and the client.

    A bare expiry was not enough for three reasons, each of which leaves a
    session usable when it should not be:

      - Changing the password did nothing to sessions already issued. Someone
        who had the old password kept a working session for up to 12 hours,
        which is the opposite of what changing a password is for.
      - Nothing bounded the number of live sessions. The dict was only pruned
        when a new session was created, so repeated logins grew it without
        limit.
      - A stolen cookie worked from anywhere on the LAN.
    """
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    _sessions[token] = {
        "expires": now + SESSION_TTL_S,
        "user": username,
        "cred": credential_fingerprint(username),
        "addr": addr,
        "created": now,
    }
    for tok, rec in list(_sessions.items()):
        if rec["expires"] < now:
            _sessions.pop(tok, None)
    # Oldest first, so an attacker cannot push a legitimate session out by
    # logging in repeatedly without also holding the password.
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=lambda t: _sessions[t]["created"])
        _sessions.pop(oldest, None)
        _LOGGER.info("session limit %d reached; dropped the oldest", MAX_SESSIONS)
    return token


def valid_session(token, addr=None):
    rec = _sessions.get(token)
    if rec is None:
        return False
    if rec["expires"] < time.monotonic():
        _sessions.pop(token, None)
        return False
    # The credential moved under it - a password change, or the account being
    # locked or removed. Either way this session is no longer authorised.
    if rec["cred"] != credential_fingerprint(rec["user"]):
        _sessions.pop(token, None)
        _LOGGER.info("session dropped: the credential for %s changed", rec["user"])
        return False
    if addr is not None and rec["addr"] != addr:
        _LOGGER.warning("session for %s presented from %s, issued to %s",
                        rec["user"], addr, rec["addr"])
        return False
    return True


# ---------------------------------------------------------------------------
# The shipped account, fenced to the USB cable
#
# The image ships postmarketOS's documented account, user / 147147, in wheel
# with passwordless sudo. While that password is live, biscuit-firstboot tells
# sshd to refuse PASSWORD logins for it except over the USB network. This page
# has to hold the same line: it accepted the same password from anywhere, and a
# session here can add an SSH key - which sshd does accept - so the SSH fence
# was one hop from root for anyone on the house network. So while the password
# is live, "user" can sign in here, and use a session, only over the cable:
#
#     this end 172.16.42.1, the other end in 172.16.42.0/24
#
# "Live" is decided exactly as biscuit-firstboot's default_password_live()
# decides it - the same hash check with cryptpw, and the same fail-safe answer
# when the hash cannot be checked - and the rule file that function leaves for
# sshd is consulted too. If the two disagree, sshpolicy is asked to look again,
# so sshd and this page agree; until it has, the stricter answer wins.
# ---------------------------------------------------------------------------

SHIPPED_ACCOUNT = "user"
SHIPPED_PASSWORD = "147147"
SSHD_DEFAULT_RULE = "/run/biscuit-sshd/default-account.conf"
PROVISIONED_MARKER = "/opt/persist/provisioned"
USB_LOCAL = "172.16.42.1"
USB_NET = ipaddress.ip_network("172.16.42.0/24")
# The open setup network (biscuit-setup.sh ADDR/PREFIX). SETUP_NET_RULE exists
# exactly while it is up - biscuit-setup writes it before hostapd starts and
# removes it after the network is down - and sshd reads the same file.
SETUP_ADDR = "192.168.4.1"
SETUP_NET = ipaddress.ip_network("192.168.4.0/24")
SETUP_NET_RULE = "/run/biscuit-sshd/setup-network.conf"
SSH_POLICY_CMD = ["rc-service", "biscuit-firstboot", "sshpolicy"]
SSH_POLICY_RETRY_S = 30

_shipped_cache = {}             # (hash, marker) -> bool
_policy_asked = [0.0]


def _plain_ip(addr):
    addr = str(addr or "")
    return addr[7:] if addr.startswith("::ffff:") else addr


def over_usb(local, peer):
    """True for a connection over the USB network: to 172.16.42.1, from
    172.16.42.0/24. Both ends, so a house network that happens to use
    172.16.42.0/24 is not mistaken for the cable."""
    try:
        return (_plain_ip(local) == USB_LOCAL and
                ipaddress.ip_address(_plain_ip(peer)) in USB_NET)
    except ValueError:
        return False


def setup_network_client(local, peer):
    """True for a connection from the open setup network: always when it is to
    the setup address itself, and from 192.168.4.0/24 while setup runs."""
    if _plain_ip(local) == SETUP_ADDR:
        return True
    if not os.path.exists(SETUP_NET_RULE):
        return False
    try:
        return ipaddress.ip_address(_plain_ip(peer)) in SETUP_NET
    except ValueError:
        return False


def _read_marker():
    try:
        with open(PROVISIONED_MARKER) as f:
            return f.read().strip()
    except OSError:
        return ""


def shipped_password_live():
    """biscuit-firstboot's default_password_live(), in Python.

    True when SHIPPED_ACCOUNT can still sign in with SHIPPED_PASSWORD, or when
    that cannot be ruled out; False when it cannot sign in with it (no such
    account, locked, no password, or another password)."""
    try:
        with open(PASSWD) as f:
            if not any(line.split(":", 1)[0] == SHIPPED_ACCOUNT for line in f):
                return False
    except OSError:
        return False
    stored = None
    readable = True
    try:
        with open(SHADOW) as f:
            for line in f:
                parts = line.rstrip("\n").split(":")
                if parts[0] == SHIPPED_ACCOUNT:
                    stored = parts[1] if len(parts) > 1 else ""
                    break
    except OSError:
        readable = False
    method = salt = None
    if readable:
        stored = stored or ""
        # Locked, or no password at all (which sshd refuses anyway, and which
        # verify_password never accepts): no password login to guard.
        if not stored or stored[0] in "!*":
            return False
        fields = stored.split("$")
        salt = fields[2] if len(fields) > 2 else ""
        method = {"6": "sha512", "5": "sha256", "1": "md5"}.get(
            fields[1] if len(fields) > 1 else "")
        if not salt or salt.startswith("rounds="):
            method = None
    marker = _read_marker()
    key = (stored if readable else None, marker)
    if key in _shipped_cache:
        return _shipped_cache[key]
    result = None
    if method:
        try:
            out = subprocess.run(["cryptpw", "-P", "0", "-m", method, "-S", salt],
                                 input=SHIPPED_PASSWORD, capture_output=True,
                                 text=True, timeout=10).stdout.strip()
        except Exception as err:                  # noqa: BLE001
            _LOGGER.warning("cryptpw failed checking the shipped account: %s", err)
            out = ""
        if out:
            result = hmac.compare_digest(out, stored)
    if result is None:
        # Not checkable. An account setup made for the owner under this name
        # holds a password the owner typed; anything else is the shipped
        # account, so assume it still has the shipped password.
        result = marker != SHIPPED_ACCOUNT
    if len(_shipped_cache) > 16:
        _shipped_cache.clear()
    _shipped_cache[key] = result
    return result


def shipped_account_fenced():
    """True while SHIPPED_ACCOUNT may be used only over the USB cable."""
    live = shipped_password_live()
    rule = os.path.exists(SSHD_DEFAULT_RULE)
    if live != rule and time.monotonic() - _policy_asked[0] > SSH_POLICY_RETRY_S:
        # sshd's rule is stale or missing. Ask biscuit-firstboot to look again;
        # it writes or removes the rule and reloads sshd.
        _policy_asked[0] = time.monotonic()
        try:
            subprocess.run(SSH_POLICY_CMD, capture_output=True, timeout=20)
        except Exception as err:                  # noqa: BLE001
            _LOGGER.warning("could not re-run the SSH policy: %s", err)
        rule = os.path.exists(SSHD_DEFAULT_RULE)
        _LOGGER.info("shipped account: password %s, sshd rule %s after sshpolicy",
                     "live" if live else "changed", "present" if rule else "absent")
    return live or rule


def shipped_refusal():
    where = "http://%s%s/" % (USB_LOCAL, "" if PORT == 80 else ":%d" % PORT)
    return ("This device still has the published default account (user / "
            "147147), so that account can be used here only over the USB "
            "cable, at %s. Run setup to create your own account, or sign in "
            "over USB and change its password." % where)


def shipped_refused(username, local, peer):
    """True when `username` must not be used over this connection."""
    return (username == SHIPPED_ACCOUNT and not over_usb(local, peer)
            and shipped_account_fenced())

# How long a preview runs before retiring itself. Previews deliberately use the
# ring's lifetime argument rather than play/stop: this process is not the ring's
# owner, and a preview that outlived the page would sit on the ring forever if
# the browser tab were closed mid-preview.
PREVIEW_SECONDS = 4


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load(FX_MODULE, "biscuit_ring_fx")
# DEFAULT_MAP and LED_MAP_FILE are read from the agent rather than copied here.
# Two hand-maintained copies of the stock mapping would drift, and the drift
# would show up as "the UI says Default but the ring does something else".
agent = _load(AGENT, "biscuit_va_leds")
DEFAULT_MAP = agent.DEFAULT_MAP
FIREOS5_MAP = agent.FIREOS5_MAP
# The stock generations are owner-imported and may be absent; fos_shortcuts()
# filters both against what is actually installed. DEFAULT_MAP is no longer one
# of them - the shipped defaults are generated effects.
FIREOS6_MAP = agent.FIREOS6_MAP
LED_MAP_FILE = agent.LED_MAP_FILE
# Files from stock lists the imported animations by the agent's labels.
system.led_catalogue = agent.stock_catalogue

# Wi-Fi, Bluetooth and the account. Deliberately NOT in the agent: none of them
# is exposed to Home Assistant, and reconfiguring the network from a page that
# is only reachable over that network is this service's job alone.
netcfg = _load(NETCFG, "biscuit_netcfg")

# Friendly names and grouping. The keys are the underlying event names, which are
# precise but not something to show a user.
#
# Only the "Voice assistant" group is played by biscuit-va-leds. The rest belong
# to other services - the boot script, wifi.start, biscuit-setup.sh,
# biscuit-pair-session, biscuit-btring - which read the resolved shell file this
# service writes. Every activity listed here is one that genuinely fires on this
# device; nothing is offered that would never light the ring.
ACTIVITIES = [
    ("wake_word_detected", "Wake word heard",        "Voice assistant"),
    ("listening",          "Listening",              "Voice assistant"),
    ("stt_text",           "You are speaking",       "Voice assistant"),
    ("thinking",           "Thinking",               "Voice assistant"),
    ("tts_speaking",       "Speaking",               "Voice assistant"),
    ("tts_finished",       "Finished replying",      "Voice assistant"),
    ("pipeline_error",     "Error",                  "Voice assistant"),
    ("timer_ticking",      "Timer running",          "Voice assistant"),
    ("timer_ringing",      "Timer ringing",          "Voice assistant"),

    ("booting",            "While booting",          "Device"),
    ("music",              "Music playing",          "Device"),
    ("boot",               "Finished booting",       "Device"),
    ("mute",               "Microphones muted",      "Device"),
    # The volume ramp lights the ring on every volume change but had no
    # activity, so its colour was the only thing on the ring a user could not
    # change. The ANIMATION is not really a choice here - biscuit-audio picks
    # volume_step-NN by level - but the colours are, and publish_for_shell
    # renders all 31 levels from them.
    ("volume_changed",     "Volume changed",         "Device"),

    ("wifi_connecting",    "Connecting to Wi-Fi",    "Setup and network"),
    ("wifi_error",         "Wi-Fi failed",           "Setup and network"),
    ("setup_mode",         "Setup mode",             "Setup and network"),
    ("setup_success",      "Setup succeeded",        "Setup and network"),
    ("setup_error",        "Setup failed",           "Setup and network"),

    ("bt_pairing",         "Bluetooth pairing",      "Bluetooth"),
    ("bt_connected",       "Bluetooth connected",    "Bluetooth"),
    ("bt_disconnected",    "Bluetooth disconnected", "Bluetooth"),
]
LABELS = {k: l for k, l, _g in ACTIVITIES}

# The sound map's keys are NOT the ring's. Some activities make a noise but do
# not light up (volume changed), some light up but make no noise (thinking,
# listening), and two of them - start_listening, unmute - exist only on the
# sound side. Keeping a separate ordered table is what lets each page show only
# the activities it can actually change, instead of showing every row greyed out
# on whichever page does not own it.
SOUND_ACTIVITIES = [
    ("wake_word_detected", "Wake word heard",       "Voice assistant"),
    ("start_listening",    "Listening started",     "Voice assistant"),
    ("stt_text",           "Finished speaking",     "Voice assistant"),
    ("thinking",           "Thinking",              "Voice assistant"),
    ("pipeline_error",     "Error",                 "Voice assistant"),
    ("timer_ringing",      "Timer ringing",         "Voice assistant"),

    ("mute",               "Microphones muted",     "Device"),
    ("unmute",             "Microphones unmuted",   "Device"),
    ("volume_changed",     "Volume changed",        "Device"),
    ("boot",               "Finished booting",      "Device"),

    ("wifi_error",         "Wi-Fi failed",          "Setup and network"),
    ("setup_mode",         "Setup mode",            "Setup and network"),
    ("setup_success",      "Setup succeeded",       "Setup and network"),
    ("setup_error",        "Setup failed",          "Setup and network"),

    ("bt_pairing",         "Bluetooth pairing",     "Bluetooth"),
    ("bt_connected",       "Bluetooth connected",   "Bluetooth"),
    ("bt_disconnected",    "Bluetooth disconnected", "Bluetooth"),
]
SOUND_LABELS = {k: l for k, l, _g in SOUND_ACTIVITIES}

# Where the non-Python consumers read their answer from.
#
# /run is a tmpfs, so a GENERATED effect chosen for an activity that fires early
# in boot would simply not exist yet. Anything a shell script needs therefore
# lands on the persist partition instead, written once at save time:
#
#   led-anim.env  shell-sourceable LED_<activity>='<animation>' for every activity
#   led-fx/       the generated .animation files those names refer to
#
# The env file is written for EVERY activity, not just the customised ones, so a
# consumer sources one file and reads one variable rather than reimplementing the
# default lookup in shell.
# Everything below is read from the agent module rather than restated here.
# Two copies of "what the microphone modes are" would drift, and the drift would
# show up as a settings page that offers a mode the pump cannot start.
PERSIST_DIR = "/opt/persist"

USBMODE = "/usr/bin/biscuit-usbmode"

# What the device presents itself as over USB. The labels are what the plug
# does, not what the gadget is called: nobody picking a setting cares that the
# network function is RNDIS.
# What the computer sees over the cable, one switch per feature. These were six
# fixed modes: two identical, and three that took the network along whether it
# was wanted or not. Any combination is now possible, and all off is charging
# only. biscuit-usbmode composes it and still reads the old mode names.
USB_FEATURES = [
    ("net", "Network",
     "The computer sees a network adapter, so this Echo can be reached over the "
     "cable at 172.16.42.1 - this page and SSH - even with no Wi-Fi."),
    ("mic", "Microphone",
     "The computer sees a USB microphone: this Echo's seven microphones, "
     "processed and steered, at 48 kHz."),
    ("spk", "Speaker",
     "The computer sees a USB speaker: what it plays comes out of this Echo."),
]


def usb_label(on):
    names = [label.lower() for fid, label, _h in USB_FEATURES if fid in on]
    if not names:
        return "Charging only"
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return text[0].upper() + text[1:]


def usb_state():
    """Which USB features are on, and whether this kernel can do audio at all.

    The audio features need a kernel built with the USB Audio Class gadget.
    Without it they are shown unavailable rather than offered and then
    failing, because a switch that silently does nothing is worse than one
    that is visibly greyed out.
    """
    mode = "power"          # biscuit-usbmode's default since r272
    audio_ok = False
    try:
        out = subprocess.run([USBMODE, "get"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            mode = out.stdout.strip()
    except Exception:
        pass
    try:
        out = subprocess.run([USBMODE, "modes"], capture_output=True, text=True, timeout=15)
        audio_ok = "needs a kernel" not in out.stdout
    except Exception:
        pass
    on = set() if mode == "power" else set(mode.split())
    return {
        "mode": mode,
        "on": sorted(on),
        "label": usb_label(on),
        "audio_available": audio_ok,
        "features": [{"id": f, "label": l, "help": h, "on": f in on,
                      "available": audio_ok or f == "net"}
                     for f, l, h in USB_FEATURES],
    }


def set_usb_features(on):
    known = [f for f, _l, _h in USB_FEATURES]
    if not isinstance(on, list) or any(f not in known for f in on):
        return False, "Unknown USB feature."
    words = [f for f in known if f in on]
    try:
        out = subprocess.run([USBMODE, "set", "features=" + " ".join(words)],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, "Could not apply the USB setting: %s" % e
    if out.returncode == 2:
        return False, "This kernel has no USB audio support, so that cannot be turned on."
    if out.returncode != 0:
        return False, ((out.stderr or "").strip().splitlines() or
                       ["Applying the USB setting failed."])[-1]
    return True, "USB: " + usb_label(set(words)).lower() + "."
ANIM_ENV = os.path.join(PERSIST_DIR, "led-anim.env")
PERSIST_FX = os.path.join(PERSIST_DIR, "led-fx")

# Effects that draw something only one activity has: the volume ramp shows a
# level. Offered for "Volume changed" alone. The effects module may name its
# own set; this is the fallback for one that does not.
VOLUME_ONLY = fx.VOLUME_ONLY
# Activities whose ANIMATION is not a choice, only its colours: biscuit-audio
# plays volume_step-NN by level, rendered from these colours by
# publish_for_shell, so a stock file, Off or another effect did nothing there.
COLOURS_ONLY = frozenset({"volume_changed"})


def ring(line):
    """One command to the ring FIFO, never blocking.

    Same contract as the agent's helper: the ring service being down is a normal
    condition for a cosmetic feature, not an error worth failing a request over.
    """
    try:
        fd = os.open(CONTROL_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as err:
        if err.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, (line + "\n").encode())
        return True
    except OSError as err:
        if err.errno == errno.EPIPE:
            return False
        raise
    finally:
        os.close(fd)


def stock_animation_exists(name):
    """Shipped with the image, or imported from the owner's backup - fos6_
    names through Fire OS 6's own file for the activity."""
    return agent.stock_source(name) is not None


def stock_ok(name):
    """Whether an activity may be set to this stock name: any imported file
    under a name the store accepts, or a fos6_<activity> shortcut whose file
    is here. Everything else is refused - an unchecked name is a path into
    the ring's player."""
    if not isinstance(name, str):
        return False
    if name.startswith("fos6_"):
        return name[len("fos6_"):] in agent.FOS6_SOURCES and stock_animation_exists(name)
    return (system.LED_NAME.match(name + ".animation") is not None and
            not name.startswith(system.LED_OWN_PREFIXES) and stock_animation_exists(name))


def fos_shortcuts(key):
    """An activity's own Fire OS 6 and Fire OS 5 animations, where imported:
    (fos6 name or None, Fire OS 5 file or None). The Fire OS 5 one is left
    out when it is the very file the Fire OS 6 one plays, as most are."""
    fos6 = FIREOS6_MAP.get(key, {}).get("animation")
    src6 = agent.stock_source(fos6)
    fos5 = FIREOS5_MAP.get(key, {}).get("animation")
    src5 = agent.stock_source(fos5)
    return (fos6 if src6 else None, fos5 if src5 and src5 != src6 else None)


def stock_pick_label(label):
    """A stock animation's label where it sits beside the built-in effects:
    Amazon's "Fire" file and the Fire effect would read the same in a row,
    its value and a toast, so the stock one says where it is from."""
    if label and label.lower() in {n.lower() for n in fx.EFFECTS}:
        return label + " (Fire OS)"
    return label


def stock_choices():
    """Every imported animation, for the pickers: (offered, labels, aliases).

    `offered` is every file worth choosing, in picker order, each once: a
    byte-identical copy is folded into the one kept (its label rides along in
    `also`, so filtering for "Timer" still finds "Alarm"), and single frames,
    blank files and the pointer frames are left out. `labels` names every
    stock name an override could hold - hidden files and fos6_ shortcuts too -
    so a saved choice always reads as words. `aliases` points a folded copy,
    or another activity's fos6_ shortcut, at the offered file that looks the
    same, which is how such a saved choice is shown. A label that is also an
    effect's name says "(Fire OS)" - see stock_pick_label()."""
    cat = agent.stock_catalogue()
    labels = {e["name"]: stock_pick_label(e["label"]) for e in cat}
    aliases = {e["name"]: e["same"] for e in cat if e["same"]}
    offered = []
    for e in cat:
        if not e["hidden"] and not e["same"]:
            offered.append({"value": e["name"], "label": labels[e["name"]], "group": e["group"],
                            "also": [x["label"] for x in cat if x["same"] == e["name"]]})
    shown = {o["value"] for o in offered}
    for key, label, _group in ACTIVITIES:
        fos6 = fos_shortcuts(key)[0]
        if fos6:
            labels[fos6] = "Fire OS 6 · " + label
            src = agent.FOS6_SOURCES.get(key)
            src = aliases.get(src, src)
            if src in shown:
                aliases[fos6] = src
    return offered, labels, aliases


def led_usage():
    """Which activities are set to each imported animation, by file name, so
    removing one can say what it takes away."""
    used = {}
    for key, spec in read_overrides().items():
        spec = agent.normalise_spec(spec)
        if isinstance(spec, dict) and spec.get("animation") and not spec.get("effect"):
            src = agent.stock_source(spec["animation"])
            if src:
                name = os.path.basename(src)[:-len(".animation")]
                used.setdefault(name, []).append(LABELS.get(key, key))
    return used


def refresh_stock_choices():
    """After an import or a removal: activities set to an animation that is
    no longer here go back to their default, and the per-activity copies are
    published again - a replaced file must not keep playing its old frames."""
    ov = read_overrides()
    kept = {k: v for k, v in ov.items()
            if not (isinstance(v, dict) and v.get("animation") and not v.get("effect")
                    and not v.get("off") and not stock_animation_exists(v["animation"]))}
    if kept != ov:
        write_overrides(kept)
    else:
        publish_for_shell(ov)
    return sorted(set(ov) - set(kept))


def read_overrides():
    try:
        with open(LED_MAP_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_overrides(data):
    """Atomic replace onto the persist partition.

    A half-written map is worse than none: the agent parses this file on a live
    event, so a torn read would drop every activity back to defaults mid-sentence.
    """
    os.makedirs(os.path.dirname(LED_MAP_FILE), exist_ok=True)
    tmp = LED_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, LED_MAP_FILE)
    publish_for_shell(data)


def publish_for_shell(overrides):
    """Resolve every activity to a concrete animation name for the non-Python
    consumers, and materialise any generated effects somewhere that survives a
    reboot.

    Generated effect files are named per ACTIVITY, not per effect. `fx.generate`
    names its output after the effect, so two activities using the same effect in
    different colours would write the same filename and the second would silently
    repaint the first. Boot in orange Comet and Thinking in blue Comet have to be
    two files.
    """
    os.makedirs(PERSIST_FX, exist_ok=True)
    tmpdir = os.path.join(PERSIST_FX, ".tmp")
    os.makedirs(tmpdir, exist_ok=True)

    lines, keep = [], set()
    for key, _label, _group in ACTIVITIES:
        spec = agent.normalise_spec(dict(overrides.get(key) or DEFAULT_MAP.get(key) or {}))
        anim = None
        src = None
        if spec.get("animation") and not spec.get("effect") and not spec.get("off"):
            # A stock animation plays under the activity's own name, like an
            # effect: the ring takes the layer from the name, and most of
            # Amazon's names are not in its table. One that is gone falls back
            # to the default rather than leaving the activity dark.
            src = agent.stock_source(spec["animation"])
            if src is None:
                spec = dict(DEFAULT_MAP.get(key) or {})
        if src:
            anim = "act_" + key
            try:
                # Repeating when the activity is held, as the agent's own copy
                # does: a one-shot chosen for thinking must not go dark.
                agent.stage_stock(src, os.path.join(tmpdir, anim + ".animation"),
                                  agent.held_activity(key))
                _durable_replace(os.path.join(tmpdir, anim + ".animation"),
                                 os.path.join(PERSIST_FX, anim + ".animation"))
                keep.add(anim + ".animation")
            except OSError:
                # The file under its own name in the imported store, which the
                # ring can load - a fos6_ name exists only as a mapping.
                anim = os.path.basename(src)[:-len(".animation")]
        elif spec.get("off"):
            anim = "act_" + key
            try:
                fx.generate_blank(out_dir=tmpdir, name=anim)
                _durable_replace(os.path.join(tmpdir, anim + ".animation"),
                                 os.path.join(PERSIST_FX, anim + ".animation"))
                keep.add(anim + ".animation")
            except Exception:                   # noqa: BLE001
                anim = None                     # fall through to the default
        elif spec.get("effect"):
            try:
                slug = fx.generate(spec["effect"],
                                   tuple(spec.get("colour") or (255, 255, 255)),
                                   out_dir=tmpdir, colours=spec.get("colours"))
                anim = "act_" + key
                _durable_replace(os.path.join(tmpdir, slug + ".animation"),
                           os.path.join(PERSIST_FX, anim + ".animation"))
                keep.add(anim + ".animation")
            except Exception:                       # noqa: BLE001
                anim = None                         # fall back to the default below
        if not anim:
            anim = spec.get("animation") or DEFAULT_MAP.get(key, {}).get("animation", "")
        lines.append("LED_%s='%s'" % (key, anim.replace("'", "")))

    # The volume ramp, from the volume_changed activity's colours. These are
    # played BY NAME rather than per activity - biscuit-audio chooses
    # volume_step-NN from the level it just applied - so they are one file per
    # LEVEL, not one per activity, and the act_* cleanup below leaves them be.
    vol = dict(overrides.get("volume_changed")
               or DEFAULT_MAP.get("volume_changed") or {})
    try:
        fx.generate_volume_ramp(colours=vol.get("colours"), out_dir=PERSIST_FX)
    except Exception:                           # noqa: BLE001
        pass                                    # a bad palette must not break the rest

    for stale in os.listdir(PERSIST_FX):
        if stale.startswith("act_") and stale not in keep:
            try:
                os.unlink(os.path.join(PERSIST_FX, stale))
            except OSError:
                pass

    tmp = ANIM_ENV + ".tmp"
    with open(tmp, "w") as f:
        header = ("# Generated by biscuit-settings. Do not edit by hand;"
                  " edit it from the settings page at http://<device>:8080/")
        f.write(header + "\n")
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, ANIM_ENV)


def write_sound_overrides(changes):
    """Merge per-activity sound choices into the persisted override map.

    Same shape as the ring's override file and the same reasoning: an activity
    left on its default is ABSENT rather than written out with the stock value,
    so "reset" is a deletion and a later correction to Amazon's defaults still
    reaches anyone who never customised that activity.

    The one asymmetry is silence. `{"sound": null, "silent": true}` is a real
    choice that has to be stored, because it is not the same as "use the
    default". The Off and Assistant-defaults sets write a bare null, which for
    the assistant's own activities means different things under each set.
    """
    path = agent.SOUND_MAP_FILE
    try:
        with open(path) as f:
            current = json.load(f)
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}

    for key, value in changes.items():
        if key not in SOUND_LABELS:
            raise ValueError("unknown activity %r" % key)
        default = agent.DEFAULT_SOUNDS.get(key, {}).get("sound")
        if value == "__default__":
            # A default of silence (Thinking) is marked too while the set
            # hands an unmarked null to the assistant, which then played its
            # own sound under a choice labelled "Default (silent)".
            if (default is None and key in earcon_map.LVA_FLAGS and
                    earcon_map.assistant_sound(key, {})[1] == "assistant"):
                current[key] = {"sound": None, "silent": True}
            else:
                current.pop(key, None)
            continue
        if value in (None, "", "__silent__"):
            # Marked, because the "Assistant defaults" set stores a bare null
            # for "the assistant's own sound" - see assistant_sound() in the
            # earcon map. Without the mark, Silent under that set played it.
            current[key] = {"sound": None, "silent": True}
            continue
        if not isinstance(value, str) or not _installed_sound(value):
            raise ValueError("no sound named %r is installed" % value)
        if value == default:
            current.pop(key, None)            # same as the default: store nothing
        else:
            current[key] = {"sound": value}

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(current, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, path)
    return current


# The core activity->sound table, for what the assistant is told to play. The
# agent loads the same file; this is its own handle so the rules the assistant's
# launch uses (assistant_flags) are asked directly rather than through it.
earcon_map = _load("/usr/bin/biscuit-earcon-map.py", "biscuit_earcon_map")

VA_SERVICE = "biscuit-voice-assistant"
VA_PREFS = "/opt/persist/voice-assistant-prefs.json"
ASSISTANT_RESTART_DELAY_S = 2.0
_assistant_restart = None
_assistant_restart_lock = threading.Lock()


def restart_assistant_soon():
    """Restart the voice assistant so it picks up changed sounds.

    Deferred and coalesced: choosing three sounds in a row is one restart, not
    three, each of which leaves it deaf for a few seconds. --ifstarted, so a
    paused or stopped assistant stays that way; it reads the new sounds when it
    next starts anyway. Returns whether a restart was scheduled.
    """
    global _assistant_restart
    if not _assistant_installed():
        return False

    def run():
        try:
            subprocess.Popen(["rc-service", "--ifstarted", VA_SERVICE, "restart"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as err:
            _LOGGER.warning("could not restart %s: %s", VA_SERVICE, err)

    with _assistant_restart_lock:
        if _assistant_restart is not None:
            _assistant_restart.cancel()
        _assistant_restart = threading.Timer(ASSISTANT_RESTART_DELAY_S, run)
        _assistant_restart.daemon = True
        _assistant_restart.start()
    return True


def thinking_sound_on():
    """Home Assistant's "Thinking Sound" switch, which gates the Thinking sound.

    Read-only, from the assistant's own preferences: it plays its processing
    sound only while that switch is on, whatever is chosen here. Absent or
    unreadable reads as off, which is what the assistant itself assumes.
    """
    try:
        with open(VA_PREFS) as f:
            return json.load(f).get("thinking_sound") == 1
    except (OSError, ValueError, AttributeError):
        return False


def _installed_sound(name):
    """True when a bare sound name resolves to a file we actually ship.

    Absolute paths are rejected outright here, unlike in the earcon module: that
    module allows one so a user can point an activity at their own file over
    SSH, but accepting one over HTTP would let any authenticated session make
    the device open an arbitrary path.
    """
    if not isinstance(name, str) or os.path.isabs(name) or "/" in name:
        return False
    return any(
        os.path.exists(os.path.join(directory, name + ext))
        for directory in agent.earcon_dirs()
        for ext in (".flac", ".wav", ".ogg", ".mp3"))


# Published by biscuit-audio, its only owner: "1" plugged, "0" not, and no
# file at all when it does not know (stopped, or the jack could not be read).
HEADPHONES_STATE = "/run/biscuit-audio/headphones"


def read_headphones():
    """The 3.5 mm jack: True, False, or None when it is unknown."""
    try:
        with open(HEADPHONES_STATE) as f:
            value = f.read().strip()
    except OSError:
        return None
    return {"1": True, "0": False}.get(value)


def jack_state():
    """The jack on its own: two file reads, nothing that forks."""
    return {"headphones": read_headphones(),
            "cast_active": bool(netcfg.cast_active())}


def sound_live():
    """What the open Sound page follows while it is open, polled every 2 s.

    Only what can change from somewhere else - Home Assistant, the buttons, a
    plug - and only file reads, so the poll costs nothing. audio_state() also
    reads the microphone chain and the buttons, which this page never shows.
    """
    duck = agent.load_duck()
    return dict(jack_state(),
                volume=agent.read_device_volume(),
                duck_enabled=bool(duck["enabled"]),
                duck_level=int(duck["level"]),
                eq=eq_store.load(),
                earcon_set=agent.current_earcon_set())


def audio_state():
    """Everything the audio panel shows, in one shape."""
    duck = agent.load_duck()
    lux = agent.read_lux()
    return {
        "mic_profile": agent.load_mic_settings()["mic_profile"],
        "mic_profile_status": agent.mic_profile_status(),
        # The speaker chain. Granular microphone controls stay in Home
        # Assistant on purpose - see the Custom preset message below - but the
        # speaker side had nothing here at all, which left the EQ and the sound
        # set reachable only from Home Assistant.
        "eq": eq_store.load(),
        "eq_modes": eq_store.EQ_MODES,
        # False until the owner imports the factory curve: Stock is then flat.
        "eq_curve": eq_store.factory_curve_imported(),
        "eq_min": eq_store.EQ_DB_MIN,
        "eq_max": eq_store.EQ_DB_MAX,
        "earcon_sets": agent.EARCON_SETS,
        "earcon_set": agent.current_earcon_set(),
        "duck_enabled": bool(duck["enabled"]),
        "duck_level": int(duck["level"]),
        "lux": lux,
        "pairing": agent.pairing_open(),
        "button_actions": agent.BUTTON_ACTION_LABELS,
        "buttons": [{"gesture": g, "label": l, "action": agent.load_buttons()[g]}
                    for g, l, _d in agent.BUTTON_GESTURES],
        "als_present": agent.als_path() is not None,
        "volume": agent.read_device_volume(),
        "muted": agent.read_hw_muted(),
        "headphones": read_headphones(),
        "autodim": agent.load_autodim(),
        "mic_led": agent.load_mic_led(),
        "mic_led_modes": agent.MIC_LED_MODES,
        "mute_led": agent.load_mute_led(),
        "mute_led_modes": agent.MUTE_LED_MODES,
        # So the Sound page can say that the equaliser below it is not in the
        # signal path at the moment, which is otherwise very confusing.
        "cast_active": netcfg.cast_active(),
    }


# ---------------------------------------------------------------------------
# The subpages the Phosh-style shell navigates to
# ---------------------------------------------------------------------------
#
# One builder per page rather than one big /api/state. The pages are visited one
# at a time, and a Wi-Fi scan or a `bluetoothctl info` per paired peer costs
# seconds on this CPU - paying for all of them on every poll would make the
# whole page feel broken.

def mic_state():
    """The granular chain, which used to be reachable only from Home Assistant.

    The Custom preset exists because the six controls interact; now that they
    are all here, the preset row and the individual rows are two views of the
    same file and the page keeps them in step.
    """
    s = agent.load_mic_settings()
    # From the backup the installer left on the device, if nobody has imported
    # one; see biscuit_call_profile.ensure_stock.
    try:
        stock_call = agent.call_profile.ensure_stock()
    except (AttributeError, OSError):
        stock_call = agent.call_profile.stock_info()
    return {
        "profiles": agent.mic_profile_options(),
        "profile_notes": {p: agent.mic_profile_unavailable(p)
                          for p in agent.MIC_PROFILES},
        "array_only_profiles": list(agent.MIC_PROFILE_ARRAY_ONLY),
        # Fire OS 6 only, and offered with whatever this device can run, so the
        # page shows the same choices the agent would accept on save.
        "vads": agent.mic_vad_options(),
        "vad_notes": {v: agent.mic_vad_unavailable(v) for v in agent.MIC_VADS},
        "vad_applies": agent.mic_vad_applies(s),
        # What the chain is actually running, which is not always what is
        # selected: a stock generation whose assets are absent at start falls
        # back to pmOS rather than leaving the device deaf. None until the
        # microphone branch has published once.
        "profile_status": agent.mic_profile_status(),
        # How the last restart went. It is detached and takes about ten seconds,
        # so a refusal during a call, a rollback, or a chain that never came
        # back would otherwise be invisible to whoever pressed the button.
        "restart_outcome": agent.mic_restart_outcome(),
        "vad_unavailable_reason": ("" if agent.mic_vad_applies(s) else
                                   "Only the Fire OS 6 chain has a selectable adaptation gate."),
        "settings": s,
        "aec_available": agent.mic_aec_available(s),
        "aec_active": agent.mic_aec_available(s) and s["mic_aec"] == "On",
        # Two different reasons, and the page used to give the first for both.
        "aec_unavailable_reason": ("" if agent.mic_aec_available(s) else
                                   "A single microphone bypasses echo cancellation. Calls have their own, below."
                                   if s["mic_source"] == "Single microphone" else
                                   s["mic_profile"] + " handles echo itself: its canceller runs only "
                                   "while adaptive beamforming is off."),
        "sources": agent.MIC_SOURCES,
        "capsules": agent.MIC_CAPSULES,
        "call_profiles": (agent.call_profile.PROFILE_LABELS if stock_call
                          else ["Open-source defaults"]),
        "stock_call_import": stock_call,
        "on_off": agent.ON_OFF,
        "pga_min": agent.MIC_GAIN_DB_MIN,
        "pga_max": agent.MIC_GAIN_DB_MAX,
        "pga_default": agent.MIC_GAIN_DB_DEFAULT,
        "miccal": agent.load_miccal_mode(),
        "miccal_modes": agent.MICCAL_MODES,
        "mic_led": agent.load_mic_led(),
        "mic_led_modes": agent.MIC_LED_MODES,
        "mute_led": agent.load_mute_led(),
        "mute_led_modes": agent.MUTE_LED_MODES,
        # So the page's poll sees the mute button too, without /api/audio.
        "muted": agent.read_hw_muted(),
    }


def _assistant_installed():
    """Is the voice assistant actually on this device?

    The venv directory rather than an apk query: it is what every consumer of
    these sounds actually needs to exist, it costs a stat instead of forking
    apk on every settings page load, and it stays right if the bundle is ever
    installed by some route other than the package.
    """
    return os.path.isdir("/usr/lib/linux-voice-assistant")


def sound_state():
    """Per-activity earcons, plus the set that chooses them wholesale.

    `available` is every sound actually installed, so the picker can never offer
    a file the player would fail to open. The default for each activity is shown
    alongside, for the same reason the ring page shows its defaults: "Default"
    is only a useful choice if you can see what it means.
    """
    mapping = agent.load_sound_map()
    available = []
    try:
        # Every directory, not just one: core ships no sounds at all, so the
        # list is whatever the owner extracted plus whatever the assistant
        # brought with it. Deduplicated, keeping the first - earcon_dirs() is
        # in resolution order, so the entry shown is the one that would play.
        seen = set()
        for directory in agent.earcon_dirs():
            for name in sorted(os.listdir(directory)):
                stem, ext = os.path.splitext(name)
                if ext.lower() in (".flac", ".wav", ".ogg", ".mp3") and stem not in seen:
                    seen.add(stem)
                    available.append(stem)
        available.sort()
    except OSError:
        pass
    # THE SOUND FILES ARE ALL IN CORE; THE ACTIVITIES ARE NOT ALL AVAILABLE.
    #
    # Every earcon ships in the core package, because boot, Wi-Fi, setup and
    # Bluetooth all play sounds on a device with no assistant. But the
    # activities owned by `lva` and `agent` - wake word, thinking, mute - only
    # ever fire when the assistant is running. Offering them on a core-only
    # device is offering a setting that cannot do anything, which is worse than
    # not offering it: someone picks a sound, hears nothing, and reasonably
    # concludes the audio is broken.
    #
    # So they are still returned, flagged rather than dropped, and the page
    # shows them greyed with the reason. Dropping them outright would make the
    # list silently change shape when an app is installed, with no hint that
    # the missing rows were ever there.
    assistant = _assistant_installed()
    set_label = earcon_map.earcon_set()
    rows = []
    for key, label, group in SOUND_ACTIVITIES:
        default = agent.DEFAULT_SOUNDS.get(key, {})
        cur = mapping.get(key, {})
        owner = default.get("owner", "")
        sound = cur.get("sound") or ""
        # "Assistant defaults" stores no sound for these, and the assistant then
        # plays its own - so name that sound rather than showing Silent.
        if owner == "lva" and key in earcon_map.LVA_FLAGS and cur.get("owner", owner) == "lva":
            _path, why = earcon_map.assistant_sound(key, cur, set_label)
            if why == "assistant":
                sound = earcon_map.LVA_FLAGS[key][1]
        rows.append({
            "key": key,
            "label": label,
            "group": group,
            "default": default.get("sound") or "",
            "sound": sound,
            "owner": owner,
            "needs_assistant": owner in ("lva", "agent") and not assistant,
        })
    return {
        "assistant_installed": assistant,
        # The assistant plays Thinking only while this Home Assistant switch is
        # on; the page says so beside the choice.
        "thinking_sound": thinking_sound_on() if assistant else None,
        "sets": agent.EARCON_SETS,
        # The set in force, or "Custom" once sounds were chosen one by one:
        # with the stored name shown instead, choosing that set again was no
        # change, and the page could not put it back.
        "set": agent.current_earcon_set(),
        "available": available,
        "activities": rows,
    }


def wifi_state(scan=False):
    found = netcfg.wifi_scan(with_status=True) if scan else {"networks": [], "scan": None}
    st = netcfg.wifi_status()
    return {
        "status": st,
        "saved": netcfg.wifi_saved(),
        "networks": found["networks"],
        "scan": found["scan"],
        "scanned": bool(scan),
        "btproxy": btproxy_state(),
    }


BTPROXY_CONF = "/opt/persist/btproxy.env"
BTPROXY_STATE = "/run/biscuit-btproxy/state"

# Named cadences rather than two raw numbers. The cost of scanning is the thing
# a user actually cares about, and "10 s every 60 s" does not communicate it -
# measured on this hardware a continuous scan roughly HALVES 2.4 GHz Wi-Fi
# throughput, so these are presented as how much airtime you are prepared to
# give away. Values are (scan_secs, idle_secs).
BTPROXY_CADENCES = [
    ("light", "Light", 10, 110),
    ("balanced", "Balanced", 10, 50),
    ("responsive", "Responsive", 15, 25),
]


def _read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k:
                    out[k.strip().lower().replace("btproxy_", "")] = v.strip().strip('"')
    except OSError:
        pass
    return out


def btproxy_state():
    """Config plus what the service is actually doing.

    Both, because they can disagree: the config is intent, the run state is
    what the running service last acted on. Showing only the config would make
    the page claim the radio is scanning when the service has not restarted.
    """
    cfg = _read_kv(BTPROXY_CONF)
    run = _read_kv(BTPROXY_STATE)
    scan = int(run.get("scan_secs") or cfg.get("scan_secs") or 10)
    idle = int(run.get("idle_secs") or cfg.get("idle_secs") or 50)
    cadence = "custom"
    for key, _label, sc, idl in BTPROXY_CADENCES:
        if sc == scan and idl == idle:
            cadence = key
            break
    truthy = ("1", "yes", "true", "on")
    return {
        "running": bool(run),
        "enabled": (run.get("enabled") or cfg.get("enabled") or "0") in truthy,
        # Active is reported from the RUN state where the service publishes it,
        # falling back to config only before the first restart. Same reason as
        # `enabled`: intent and reality can differ for a few seconds.
        "active": (run.get("active") or cfg.get("active") or "0") in truthy,
        "connections": int(run.get("connections") or 0),
        "max_connections": int(run.get("max_connections") or cfg.get("max_connections") or 3),
        "subscribed": run.get("subscribed") == "1",
        "clients": int(run.get("clients") or 0),
        "adverts_seen": int(run.get("adverts_seen") or 0),
        "scan_cycles": int(run.get("scan_cycles") or 0),
        "scan_secs": scan,
        "idle_secs": idle,
        "cadence": cadence,
        "port": int(run.get("port") or cfg.get("port") or 6054),
        "cadences": [{"value": k, "label": l} for k, l, _s, _i in BTPROXY_CADENCES],
    }


def set_btproxy(enabled=None, cadence=None, active=None):
    """Write the config and restart the service.

    The service is always in the runlevel and reads enabled-or-idle from this
    file, so a toggle is a restart, not an rc-update - one place holds the
    state instead of two that can disagree.
    """
    cur = _read_kv(BTPROXY_CONF)
    if enabled is not None:
        cur["enabled"] = "1" if enabled else "0"
    if active is not None:
        cur["active"] = "1" if active else "0"
    if cadence:
        for key, _label, sc, idl in BTPROXY_CADENCES:
            if key == cadence:
                cur["scan_secs"] = str(sc)
                cur["idle_secs"] = str(idl)
                break
    body = [
        "BTPROXY_ENABLED=" + (cur.get("enabled") or "0"),
        "BTPROXY_SCAN_SECS=" + (cur.get("scan_secs") or "10"),
        "BTPROXY_IDLE_SECS=" + (cur.get("idle_secs") or "50"),
        "BTPROXY_PORT=" + (cur.get("port") or "6054"),
        "BTPROXY_NAME=" + (cur.get("name") or "biscuit-btproxy"),
        "BTPROXY_ACTIVE=" + (cur.get("active") or "0"),
        "BTPROXY_MAX_CONNECTIONS=" + (cur.get("max_connections") or "3"),
    ]
    os.makedirs(os.path.dirname(BTPROXY_CONF), exist_ok=True)
    tmp = BTPROXY_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write(chr(10).join(body) + chr(10))
        f.flush()
        os.fsync(f.fileno())
    _durable_replace(tmp, BTPROXY_CONF)
    subprocess.run(["rc-service", "biscuit-btproxy", "restart"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False, timeout=20)

    # WAIT FOR THE SERVICE TO REPUBLISH ITS STATE.
    #
    # btproxy_state() prefers the run state over the config, because that is
    # what the service last acted on. Read immediately after a restart it is
    # still the PREVIOUS run's values, so turning the proxy off answered
    # "enabled" and changing the cadence answered "custom" - the page then
    # redrew with the switch in the position the user had just left.
    #
    # Exactly the same trap the pairing switch above documents. Bounded, and a
    # threaded HTTP handler can afford to block here.
    #
    # EVERY field that can change has to be in the condition, not just some. The
    # first version omitted "active", so toggling active alone matched on the
    # very first iteration - against the previous run's state, which still said
    # active - and returned before the restart had even happened. The switch
    # then snapped back in the UI and the toggle looked broken while the config
    # write and the restart had both been perfectly correct.
    # A DISABLED proxy idles without building the active side, so it publishes
    # active=0 whatever the config says. Requiring a match there would spin for
    # the full 8 s and log a warning every time the proxy is switched off with
    # active still configured on, so active is only awaited while enabled.
    truthy = ("1", "yes", "true", "on")
    want_enabled = (cur.get("enabled") or "0") in truthy
    want_active = want_enabled and (cur.get("active") or "0") in truthy
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        st = btproxy_state()
        if (st["enabled"] == want_enabled
                and st["active"] == want_active
                and str(st["scan_secs"]) == (cur.get("scan_secs") or "10")
                and str(st["idle_secs"]) == (cur.get("idle_secs") or "50")):
            return st
        time.sleep(0.25)
    _LOGGER.warning("biscuit-btproxy did not republish its state within 8s")
    return btproxy_state()


def bt_state(scan=False):
    adapter = netcfg.bt_adapter()
    adapter["pairing_window"] = agent.pairing_open()
    return {
        "adapter": adapter,
        "cast_target": netcfg.cast_target(),
        "cast_active": netcfg.cast_active(),
        "devices": netcfg.bt_devices(),
        "discovered": netcfg.bt_scan() if scan else netcfg.bt_discovered(),
        "scanned": bool(scan),
    }


# postmarketOS's published default. Nothing here treats it as valid; it is the
# one string that has to be recognised in order to warn about it.
PMOS_DEFAULT_PASSWORD = "147147"


def _has_default_password(user):
    """True when the device's own account still has the shipped password.

    WHY THIS EXISTS SEPARATELY FROM netcfg's stock_account_active(). That check
    asks whether a SECOND, leftover `user` account is still enabled, and answers
    no as soon as `user` is the real account. On a device where setup wrote
    /opt/persist/provisioned but the owner kept both the name AND the default
    password - which is the state the first device to run this page was actually
    in - the old check reported everything fine, which is precisely backwards:
    that is the most exposed configuration, not the least.

    Checked against the account's own stored hash with the same verifier the
    login form uses, so it stays correct if the default ever changes.
    """
    if not user.get("exists"):
        return False
    try:
        return verify_password(user["username"], PMOS_DEFAULT_PASSWORD)
    except Exception as err:                      # noqa: BLE001
        _LOGGER.warning("could not check for the default password: %s", err)
        return False


def packages_state():
    """The optional apps and any running install. Kept for older pages; the
    Apps page reads apps_state()."""
    return {
        "packages": netcfg.available_packages(),
        "job": netcfg.package_job(),
    }


# ---------------------------------------------------------------------------
# Starting package jobs
#
# The package job's status file is the lock - nothing starts while it says
# running - but the job writes it a moment AFTER it is started, and there are
# now three things here that start jobs by themselves or on a click: the daily
# check, the retry of the apps chosen at setup, and the owner. Two of them
# looking in that moment would both see nothing running and start two apk runs
# at once. So every start in this server goes through start_pkg_job, which
# checks and starts under one lock, and counts a job it started in the last
# few seconds as running until the job has had time to say so itself.
# ---------------------------------------------------------------------------

_PKG_START = threading.Lock()
_pkg_started_at = [-1e9]
PKG_START_SETTLE = 3.0


def running_job():
    """The package job running now, whatever it is, or None."""
    job = netcfg.package_job()
    return job if job and job.get("state") == "running" else None


def pkg_job_busy():
    """Whether a package job, or a service change, is under way - so nothing
    else may start."""
    if running_job():
        return True
    if services.JOB and services.JOB.get("state") == "running":
        return True
    return time.monotonic() - _pkg_started_at[0] < PKG_START_SETTLE


def start_pkg_job(start):
    """Call start() - which starts a package job - unless one is under way.
    Returns what start() returned, or None when it was not called.

    The apps chosen at setup are pruned first. A package leaves that list
    once it is installed, but the retry only prunes once a minute - so an app
    the retry installed and the owner removed straight away, as the page
    tells them they can, was still on the list after the removal, and the
    retry installed and started it again. A removal only ever runs on an
    installed app, so pruning before any job starts takes it off the list
    before anything can remove it."""
    with _PKG_START:
        if pkg_job_busy():
            return None
        try:
            system.prune_pending()
        except (OSError, ValueError) as err:
            _LOGGER.warning("could not prune the apps chosen at setup: %s", err)
        result = start()
        _pkg_started_at[0] = time.monotonic()
        return result


def and_list(items):
    """"A", "A and B", "A, B and C"."""
    items = [str(i) for i in items if i]
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _with_labels(job):
    """A job record with the names of the apps it concerns, and its time only
    when the clock was set as it was written."""
    if not job:
        return job
    job = dict(job)
    job["labels"] = [a["label"] for a in system.app_labels(job.get("packages"))]
    job["time"] = system.plausible_time(job.get("time"))
    return job


def pending_state():
    """The apps chosen at setup that are not installed yet, and what is being
    done about them, for the Apps, home and Home Assistant pages - or None,
    which is also what a device set up before r295 always has."""
    pending = system.pending_apps()
    if not pending:
        return None
    return system.pending_view(pending, netcfg.package_job(netcfg.PKG_APPS_STATUS), running_job(),
                               system.retry_load(), system.uptime(), system.clock_set())


def apps_state():
    """The Apps page: each app from apps.json with its package and service
    state, the device software itself, and the package jobs.

    The same manifest drives the setup portal's checkboxes, so the two cannot
    describe an app differently.

    job      the last app install or removal, from its own record, so an
             update check that ran since cannot hide how it ended
    running  any package job running now - an update check included, which
             the page names and waits for rather than offering Install
    pending  the apps chosen at setup that are not installed yet
    """
    cat = system.apps()
    snap = services.snapshot()
    svc = {s["id"]: s for s in snap.get("services", [])}
    for app in cat["apps"]:
        s = svc.get(app["id"], {})
        app.update(running=bool(s.get("running")), enabled=bool(s.get("enabled")),
                   paused=bool(s.get("paused")), clear_data=bool(s.get("clear_data")),
                   reset=s.get("reset", ""), settings=s.get("settings"))
    cat["job"] = _with_labels(netcfg.package_job(netcfg.PKG_APPS_STATUS))
    cat["running"] = _with_labels(running_job())
    cat["busy"] = pkg_job_busy()
    cat["pending"] = pending_state()
    cat["service_job"] = snap.get("job")
    cat["now"] = int(time.time())
    return cat


_inv_job = None


def inventory_state(fresh=False):
    """The inventory, with each app service's own on / paused / off state and
    the job any control started. The app states come from biscuit_services,
    which the inventory module does not know about.

    Read afresh whenever the job has moved on since the last reading: the
    page asks for this the moment an action finishes, and a two-second cache
    handed it the state from before the action.
    """
    global _inv_job
    if services.JOB is not _inv_job:
        fresh = True
        _inv_job = services.JOB
    inv = system.inventory(max_age=0 if fresh else 2.0)
    snap = {s["service"]: s for s in services.snapshot().get("services", [])}
    for name, svc in inv["services"].items():
        app = services.SERVICE_APPS.get(name)
        svc["app"] = app
        svc["app_state"] = None
        if app and name in snap:
            x = snap[name]
            svc["app_state"] = ("off" if not x.get("enabled") else
                                "paused" if x.get("paused") else "on")
    return dict(inv, job=services.JOB)


def updates_state():
    """The Updates page: what is installed, the last check, and any job.

    `job` is only ever an update job. An app install that is running instead
    is `app_job`, so the page can say what it is waiting for rather than
    offering buttons the server would refuse."""
    u = system.updates()
    job = netcfg.package_job()
    u["job"] = job if job and job.get("action") in system.JOB_ACTIONS else None
    run = running_job()
    u["app_job"] = _with_labels(run) if run and run.get("action") in ("add", "del") else None
    u["now"] = int(time.time())
    return u


def ha_state():
    """What this Echo offers Home Assistant and Music Assistant, and whether
    each is connected - from the live connections, not a setting."""
    conns = system.connections()
    snap = {s["id"]: s for s in services.snapshot().get("services", [])}
    names = {a["id"]: a for a in system.apps()["apps"]}
    voice, music = snap.get("voice", {}), snap.get("sendspin", {})
    return {
        "ip": netcfg.wifi_status().get("ip", ""),
        "voice": {"installed": names.get("voice", {}).get("installed", False),
                  "running": bool(voice.get("running")),
                  "peers": conns["voice_peers"], "port": conns["voice_port"]},
        "sendspin": {"installed": names.get("sendspin", {}).get("installed", False),
                     "running": bool(music.get("running")),
                     "servers": conns["music_servers"]},
        "btproxy": btproxy_state(),
        # An app chosen at setup and not installed yet is not the same as one
        # nobody asked for, and the page says which.
        "pending": pending_state(),
    }


def about_state():
    """The About page, and the one-line summaries the root list shows.

    The root list needs "which network" and "how many Bluetooth devices", and
    fetching a whole /api/wifi and /api/bluetooth for two strings would make
    opening the settings pay for a Bluetooth enumeration every time. Both are
    the cheap half of those calls - a STATUS query and a paired-device count,
    neither of which touches the radio.
    """
    info = netcfg.system_info()
    info["user"] = netcfg.user_info()
    info["services"] = netcfg.service_states()
    info["user"]["default_password"] = _has_default_password(info["user"])
    adapter = netcfg.bt_adapter()
    info["bluetooth_name"] = adapter.get("alias", "")
    info["wifi"] = netcfg.wifi_status()
    info["bluetooth_connected"] = len(netcfg.bt_connected_macs())
    # Two file reads, so the root list can say "playing through Bluetooth"
    # without paying for a device enumeration.
    info["cast_active"] = netcfg.cast_active()
    info["cast_target"] = netcfg.cast_target()
    # For the home page's summaries. All cheap: the apk database is read as a
    # file, the connections come from /proc, and nothing here asks the network.
    db = system.installed()
    upd = system.updates()
    cat = system.apps(db)
    conns = system.connections()
    info["version"] = upd["version"]
    info["update_available"] = upd["target"]
    info["checked_at"] = upd["checked_at"]
    info["reboot_required"] = upd["reboot_required"]
    info["apps_installed"] = [a["id"] for a in cat["apps"] if a["installed"]]
    info["apps_total"] = len(cat["apps"])
    # The home page's notice for apps chosen at setup that are not installed
    # yet. Setup's last page promises the settings page says so.
    info["apps_pending"] = pending_state()
    info["ha_connected"] = bool(conns["voice_peers"])
    info["ma_connected"] = bool(conns["music_servers"])
    info["kernel_update"] = upd["kernel_available"]
    boot = upd.get("boot") or {}
    # Not after a deliberate Put back: the owner chose the older image.
    info["boot_differs"] = bool(boot.get("ok") and boot.get("differs") and not boot.get("rolled_back"))
    info["boot_damaged"] = upd["boot_damaged"]
    info["settings_port"] = PORT
    info["settings_url"] = system.settings_url(info.get("device_name"), PORT)
    root = next((d for d in info.get("storage") or [] if d.get("path") == "/"), None)
    info["storage_free"] = root["free"] if root else None
    info["now"] = int(time.time())
    return info


def default_label(key):
    """What "Default" is for one activity, in the words the picker uses."""
    spec = DEFAULT_MAP.get(key) or {}
    if spec.get("effect"):
        return spec["effect"]
    return stock_pick_label(agent.stock_label(spec["animation"])) if spec.get("animation") else ""


def state():
    offered, labels, aliases = stock_choices()
    activities = []
    for k, l, g in ACTIVITIES:
        fos6, fos5 = fos_shortcuts(k)
        activities.append({"key": k, "label": l, "group": g, "fireos6": fos6, "fireos5": fos5,
                           "default_label": default_label(k), "colours_only": k in COLOURS_ONLY})
    return {
        "activities": activities,
        "stock": offered,
        "stock_labels": labels,
        "stock_aliases": aliases,
        "effects": [{"value": n, "label": n, "colourable": bool(fx.COLOUR_ROLES[n]),
                     "colour_roles": fx.COLOUR_ROLES[n], "colour_defaults": fx.default_colours(n),
                     "volume_only": n in VOLUME_ONLY}
                    for n in sorted(fx.EFFECTS)],
        "overrides": read_overrides(),
        "activity_colours": {key: (spec.get("colours") or fx.default_colours(
            spec["effect"], spec.get("colour", [51,153,255])))
            for key, spec in read_overrides().items()
            if isinstance(spec, dict) and spec.get("effect") in fx.EFFECTS},
    }


def ring_label(name):
    """A name the ring is playing, as a person would say it: act_thinking is
    the Thinking activity, fx_arc_spin the Arc Spin effect, and a stock file
    its label. The raw names are the ring's business."""
    if not isinstance(name, str) or not name:
        return name
    if name.startswith("act_"):
        return LABELS.get(name[len("act_"):], agent.stock_label(name[len("act_"):]))
    fixed = {"fx_preview": "Preview", "fx_manual": "Test pattern", "fx_off": "Off",
             "volume-muted": "Volume muted"}
    if name in fixed:
        return fixed[name]
    if name.startswith("volume_step-"):
        # The ramp's step out of 30, shown as the percentage the Sound page
        # shows for the same level.
        try:
            return "Volume %d%%" % round(int(name[len("volume_step-"):]) * 100 / 30)
        except ValueError:
            return "Volume"
    if name.startswith("fx_"):
        effects = {fx.slug(n): n for n in fx.EFFECTS}
        return effects.get(name, stock_pick_label(agent.stock_label(name[len("fx_"):])))
    return stock_pick_label(agent.stock_label(name))


RING_STATE = "/run/biscuit-ring/state"


def ring_state():
    """What the ring is showing and why, straight from biscuit-ring.

    Read rather than derived. Only the single highest-priority animation
    renders, so the interesting facts - which one won, and what it is masking -
    live in the player and nowhere else; recomputing them here would be a second
    copy of the priority model to drift out of step with the first.

    Missing or unreadable means the ring service is not running, which is a
    legitimate state to display rather than an error to raise.
    """
    try:
        with open(RING_STATE) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return dict(ring_config.state(), viz=ring_config.load_viz(),
                    running=False)
    doc.update(ring_config.state())
    doc["viz"] = ring_config.load_viz()
    doc["running"] = True
    doc["visible_label"] = ring_label(doc.get("visible"))
    for entry in doc.get("active") or []:
        if isinstance(entry, dict):
            entry["label"] = ring_label(entry.get("name"))
    return doc


def parse_colour(value):
    """'#RRGGBB' or [r,g,b] -> (r, g, b), or None if it is neither."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return [max(0, min(255, int(c))) for c in value[:3]]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 6:
            try:
                return [int(s[i:i + 2], 16) for i in (0, 2, 4)]
            except ValueError:
                return None
    return None


def validate_spec(spec, where="selection"):
    """One {kind, animation|effect, colour} choice -> an agent spec, or ValueError.

    Everything is checked against the lists this process itself published. An
    unknown effect name would otherwise raise inside the agent during a live
    pipeline event, which is a bad place to discover a typo, and an unchecked
    animation name is a path traversal into the ring's player.

    Returns None for "use the default", which the caller stores by leaving
    the activity out of the file rather than by writing the stock value into it.
    """
    if not isinstance(spec, dict):
        raise ValueError("bad %s" % where)
    kind = spec.get("kind")
    if kind in (None, "", "default"):
        return None
    if kind == "off":
        # Every activity can be turned off, including the ones with no stock
        # equivalent. Carried as a flag rather than an effect name so it cannot
        # collide with a real animation, and so an override of {"off": true}
        # reads for itself in led-map.json.
        return {"off": True}
    if kind == "stock":
        anim = spec.get("animation")
        if anim in ('alexa_point-at-user','alexa_point-at-noise'):
            return {'effect':'Point at speaker' if anim.endswith('user') else 'Point at noise', 'colours':[[0,255,255],[0,0,255]]}
        # Any imported animation, as the pickers offer every one - including
        # the ones they leave out as single frames or copies, which an older
        # choice may still name.
        if not stock_ok(anim):
            raise ValueError("The animation for %s is not on this Echo." % where)
        return {"animation": anim}
    if kind == "effect":
        name = spec.get("effect")
        if name not in fx.EFFECTS:
            raise ValueError("unknown effect %r for %s" % (name, where))
        entry = {"effect": name}
        if "colours" in spec:
            colours = spec["colours"]
            if not isinstance(colours, list) or len(colours) != len(fx.COLOUR_ROLES[name]):
                raise ValueError("wrong number of colours for %s" % name)
            parsed = [parse_colour(c) for c in colours]
            if any(c is None for c in parsed):
                raise ValueError("bad palette colour for %s" % name)
            entry["colours"] = parsed
            if parsed:
                entry["colour"] = parsed[0]
            return entry
        if fx.EFFECTS[name][1]:
            colour = parse_colour(spec.get("colour"))
            if colour is None:
                raise ValueError("bad colour for %s" % where)
            entry["colour"] = colour
        return entry
    raise ValueError("unknown kind %r for %s" % (kind, where))


def validate(posted):
    """The whole form -> the override map to write.

    {"kind": "keep"} leaves an activity exactly as it is stored. The page
    sends it for every row nobody touched, so saving one change never rewrites
    another - least of all one the page cannot show as a choice, like an
    animation whose file has since gone, which would otherwise be refused or
    turned into "Default" by a save that had nothing to do with it.
    """
    out = {}
    stored = None
    for key, label, _group in ACTIVITIES:
        if key not in posted:
            continue
        if isinstance(posted[key], dict) and posted[key].get("kind") == "keep":
            if stored is None:
                stored = read_overrides()
            if key in stored:
                out[key] = stored[key]
            continue
        entry = validate_spec(posted[key], label)
        if entry is None:
            continue                       # absent means "use the default"
        # The volume ramp is drawn from its colours alone; anything else
        # chosen for it would be saved and then do nothing. And the ramp
        # shows a level, which no other activity has.
        if key in COLOURS_ONLY and entry.get("effect") not in VOLUME_ONLY:
            raise ValueError("%s takes colours only." % label)
        if key not in COLOURS_ONLY and entry.get("effect") in VOLUME_ONLY:
            raise ValueError("%s is only for %s." % (entry["effect"], LABELS["volume_changed"]))
        # Carry the stock lifetime across. One-shot activities like "wake word
        # heard" are one-shot because of what they MEAN, not because of which
        # animation was picked, so swapping the animation must not leave the
        # ring lit permanently.
        lifetime = DEFAULT_MAP.get(key, {}).get("lifetime")
        if lifetime:
            entry["lifetime"] = lifetime
        out[key] = entry
    return out


class StaticPage:
    """A page that does not change while this process runs: compressed once,
    and named by a digest so a browser can keep it and only ask whether it
    is still current. It was 171 KB, re-sent on every visit."""

    def __init__(self, text, ctype="text/html; charset=utf-8"):
        self.raw = text.encode("utf-8") if isinstance(text, str) else text
        self.gz = gzip.compress(self.raw, 9)
        self.etag = '"%s"' % hashlib.sha256(self.raw).hexdigest()[:20]
        self.ctype = ctype


_STATIC = {}


def static_page(name):
    if name not in _STATIC:
        if name == "main":
            _STATIC[name] = StaticPage(PAGE)
        elif name == "login":
            _STATIC[name] = StaticPage(LOGIN_PAGE)
        elif name == "nacl":
            with open(NACL_JS, "rb") as f:
                _STATIC[name] = StaticPage(f.read(), "application/javascript")
    return _STATIC[name]


class Handler(BaseHTTPRequestHandler):
    server_version = "biscuit-settings"
    protocol_version = "HTTP/1.1"

    def _static(self, page, cache="no-cache"):
        """no-cache means "ask first", not "never keep": the browser sends
        its ETag and an unchanged page costs a 304 with no body."""
        if page.etag in (self.headers.get("If-None-Match") or ""):
            self.send_response(304)
            self.send_header("ETag", page.etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = page.gz if gz else page.raw
        self.send_response(200)
        self.send_header("Content-Type", page.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", page.etag)
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def _reauth(self, body):
        """The owner's password again, before something that cannot be taken
        back - as a phone asks for its PIN. None when it matches, else why."""
        addr = self._client()
        if throttled(addr):
            return "Too many attempts. Wait a few minutes."
        rec = _sessions.get(self._cookie_token()) or {}
        given = str(body.get("password") or "")
        if not given:
            return "Enter your password."
        if not verify_password(rec.get("user"), given):
            note_failure(addr)
            return "That is not the password."
        _failures.pop(addr, None)
        return None

    def _pending_action(self, action):
        """The apps chosen at setup, as one: install them now rather than at
        the next automatic try, or not at all. Behind the same session and
        request checks as Install, which is what each of them amounts to."""
        pending = system.pending_apps()
        if not pending:
            return self._json(400, {"error": "Nothing chosen at setup is waiting to be installed."})
        labels = [a["label"] for a in system.app_labels(pending)]
        names = and_list(labels)
        if action == "cancel":
            # Under the start lock, so the automatic retry cannot start the
            # install in the same moment the owner calls it off. An install
            # running now cannot be stopped, so what it covers stays on the
            # list (it leaves once installed); the others are called off -
            # the page offers this only for them.
            with _PKG_START:
                run = running_job()
                covered = set(run.get("packages") or []) if run and run.get("action") == "add" else set()
                drop = [p for p in pending if p not in covered]
                if not drop:
                    return self._json(409, {"error": "The install is already under way. Once it has "
                                                     "finished, an app you do not want can be removed "
                                                     "from its page."})
                if time.monotonic() - _pkg_started_at[0] < PKG_START_SETTLE:
                    return self._json(409, {"error": "A package job has just started. Try again in "
                                                     "a few seconds."})
                try:
                    if len(drop) == len(pending):
                        system.cancel_pending()
                    else:
                        system.drop_pending(drop)
                except OSError as err:
                    return self._json(500, {"error": "The choice could not be cleared: %s" % err})
            labels = [a["label"] for a in system.app_labels(drop)]
            names = and_list(labels)
            # The failure it was going to be tried again for no longer means
            # anything, and left on the page it would read as a problem.
            rec = netcfg.package_job(netcfg.PKG_APPS_STATUS)
            if (rec and rec.get("finished") and rec.get("state") == "failed" and
                    set(rec.get("packages") or []) <= set(drop)):
                try:
                    os.unlink(netcfg.PKG_APPS_STATUS)
                except OSError:
                    pass
            _LOGGER.info("apps chosen at setup cancelled: %s", " ".join(drop))
            them = "It" if len(labels) == 1 else "They"
            return self._json(200, dict(apps_state(), ok=True, message=(
                "%s will not be installed. %s can still be installed from this page."
                % (names, them))))
        if services.JOB and services.JOB.get("state") == "running":
            return self._json(409, {"error": "Wait for the other change to finish."})
        try:
            # Read again under the lock: what matters is the list as it is
            # when the job starts.
            started = start_pkg_job(lambda: system.start_app_install(system.pending_apps()))
        except (ValueError, OSError) as err:
            return self._json(400, {"error": "Could not start the install: %s" % err})
        if started is None:
            return self._json(409, {"error": "Another package operation is still running."})
        _LOGGER.info("installing the apps chosen at setup, as asked: %s", " ".join(started))
        return self._json(200, dict(apps_state(), ok=True, message=(
            "Installing %s. This can take a few minutes." % names)))

    def log_message(self, fmt, *args):
        sys.stderr.write("[settings] %s\n" % (fmt % args))

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        # Every answer names the public release this Echo runs, so the page
        # can call its build "v1.0" wherever it shows it, whichever endpoint
        # it came from. One small file read.
        if code == 200 and isinstance(obj, dict) and "_release" not in obj:
            obj = dict(obj, _release=system.release())
        self._send(code, json.dumps(obj))

    def _client(self):
        return self.client_address[0] if self.client_address else "?"

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _local(self):
        """This device's end of the connection."""
        try:
            return self.connection.getsockname()[0]
        except (OSError, AttributeError, IndexError):
            return "?"

    def _authed(self):
        token = self._cookie_token()
        if not valid_session(token, self._client()):
            return False
        # Every request, not only the sign-in: a session is refused the moment
        # the shipped account's password is found to be live again, and one
        # issued over the cable is useless off it (the address binding above
        # already sees to the far end; this sees to this end).
        user = (_sessions.get(token) or {}).get("user")
        if shipped_refused(user, self._local(), self._client()):
            _LOGGER.warning("session for %s refused from %s: the default "
                            "password is live and this is not the USB link",
                            user, self._client())
            return False
        return True

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self):
        """A browser gets the login page; anything else gets a plain 401.

        Redirecting an API call to HTML would make a failure look like success
        to anything that only checks the status code.
        """
        if "text/html" in (self.headers.get("Accept") or ""):
            self._redirect("/login")
        else:
            self._send(401, json.dumps({"error": "not authenticated"}))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            raise ValueError("bad request size")
        return json.loads(self.rfile.read(n).decode())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            self._static(static_page("login"))
            return
        if path == "/seal":
            # A key and a one-time challenge for sealing a form. Open to all:
            # the sign-in page needs it before anyone is signed in.
            self._json(200, SEALER.issue(self._client()))
            return
        if path == "/n.js":
            try:
                self._static(static_page("nacl"), cache="max-age=86400")
            except OSError:
                self._send(404, "not found", "text/plain")
            return
        if path == "/logout":
            _sessions.pop(self._cookie_token(), None)
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie",
                             "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                             % SESSION_COOKIE)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authed():
            self._deny()
            return
        if path in ("/", "/index.html"):
            self._static(static_page("main"))
        elif path == "/api/leds":
            self._json(200, state())
        elif path == "/api/ring":
            self._json(200, ring_config.state())
        elif path == "/api/audio":
            self._json(200, audio_state())
        elif path == "/api/audio/live":
            self._json(200, sound_live())
        elif path == "/api/jack":
            # Polled on its own too, by the pages that show the jack.
            self._json(200, jack_state())
        elif path == "/api/ringstate":
            # Polled on its own, like /api/lux: it changes whenever an
            # animation starts, and folding it into /api/leds would make the
            # ring page re-fetch every dropdown to update one line.
            self._json(200, ring_state())
        elif path == "/api/lux":
            # Polled on its own so the page can refresh a live reading without
            # re-fetching the whole panel and fighting the user's own edits.
            self._json(200, {"lux": agent.read_lux()})
        elif path == "/api/mic":
            self._json(200, mic_state())
        elif path == "/api/sounds":
            self._json(200, sound_state())
        elif path == "/api/wifi":
            # A scan takes seconds and holds the radio, so it happens only when
            # the page asks for one - opening the panel shows what is already
            # known and offers a Scan button.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(200, wifi_state(scan=q.get("scan", ["0"])[0] == "1"))
        elif path == "/api/bluetooth":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(200, bt_state(scan=q.get("scan", ["0"])[0] == "1"))
        elif path == "/api/ssh":
            self._json(200, netcfg.ssh_state())
        elif path == "/api/usb":
            self._json(200, usb_state())
        elif path == "/api/about":
            self._json(200, about_state())
        elif path == "/api/services":
            self._json(200, services.snapshot())
        elif path == "/api/packages":
            self._json(200, packages_state())
        elif path == "/api/apps":
            self._json(200, apps_state())
        elif path == "/api/apps/detail":
            # Seconds, not milliseconds: apk is asked what the app's removal
            # or installation would change. The page loads it after drawing.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._json(200, system.app_detail(q.get("id", [""])[0]))
            except ValueError as err:
                self._json(400, {"error": str(err)})
        elif path == "/api/updates":
            self._json(200, updates_state())
        elif path == "/api/homeassistant":
            self._json(200, ha_state())
        elif path == "/api/inventory":
            self._json(200, inventory_state())
        elif path == "/api/datetime":
            self._json(200, system.datetime_state())
        elif path == "/api/stock":
            self._json(200, dict(system.stock_state(), led_used=led_usage()))
        elif path == "/api/stock/file":
            # A stock file, to keep a copy of. Only files inside the store, by
            # name; stock_file_path refuses anything else.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                fp = system.stock_file_path(q.get("kind", [""])[0], q.get("name", [""])[0])
                with open(fp, "rb") as f:
                    data = f.read()
            except (ValueError, OSError) as err:
                return self._json(404, {"error": str(err)})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename=%s"
                             % os.path.basename(fp).replace(";", "_").replace(" ", "_"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/reset":
            self._json(200, system.reset_plan())
        elif path == "/api/storage":
            self._json(200, system.storage_state())
        elif path == "/api/package":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._json(200, system.package_detail(q.get("name", [""])[0]))
            except ValueError as err:
                self._json(404, {"error": str(err)})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            addr = self._client()
            if throttled(addr):
                _LOGGER.warning("login throttled for %s", addr)
                return self._json(429, {"error":
                                        "too many attempts; wait a few minutes"})
            try:
                body = self._body()
            except Exception:  # noqa: BLE001
                return self._json(400, {"error": "bad request"})
            was_sealed = isinstance(body, dict) and "sealed" in body
            try:
                body = unseal(body, addr)
            except ValueError as err:
                _LOGGER.warning("sealed login from %s refused: %s", addr, err)
                return self._json(400, {"error": "The sign-in form expired. Try again."})
            if not was_sealed and SEALER.key is not None:
                _LOGGER.warning("login from %s sent the password unsealed", addr)
            user = str(body.get("username", "")).strip()
            if shipped_refused(user, self._local(), addr):
                # Before the password is checked, so this is no oracle for it,
                # and not counted as a failure: nothing was guessed.
                _LOGGER.warning("login as %s from %s refused: the default "
                                "password is live and this is not the USB link",
                                user, addr)
                return self._json(403, {"error": shipped_refusal()})
            if verify_password(user, str(body.get("password", ""))):
                _failures.pop(addr, None)
                token = new_session(user, addr)
                _LOGGER.info("login ok for %s from %s", user, addr)
                payload = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Set-Cookie",
                    "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                    % (SESSION_COOKIE, token, SESSION_TTL_S))
                self.end_headers()
                self.wfile.write(payload)
                return
            note_failure(addr)
            _LOGGER.warning("login failed for %r from %s", user, addr)
            # Deliberately vague: distinguishing "no such user" from "wrong
            # password" tells an attacker which half to keep trying.
            return self._json(401, {"error": "incorrect username or password"})

        if not self._authed():
            self._deny()
            return

        if path == "/api/stock/upload":
            # One file of a stock import, as raw bytes: the page sends each
            # file it was given, then asks for the import, which checks every
            # one against the manifest before anything is installed.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = q.get("kind", [""])[0]
            name = os.path.basename(q.get("name", [""])[0])
            n = int(self.headers.get("Content-Length") or 0)
            if not name or not kind.isalnum() or n <= 0 or n > STOCK_UPLOAD_MAX:
                # Read what was sent before answering: a refusal written while
                # the client is still sending reaches it as a broken
                # connection, not as this message. Past 32 MB, just close.
                left = n if 0 < n <= 32 << 20 else 0
                while left:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                if n > 32 << 20:
                    self.close_connection = True
                return self._json(400, {"error": "a file of at most 2 MB is needed"})
            d = os.path.join(STOCK_STAGING, kind)
            # The first file of a batch clears what any abandoned batch left:
            # the page imports one kind at a time, and a leftover of another
            # kind would otherwise count against the cap with no way to clear it.
            if q.get("first") == ["1"]:
                shutil.rmtree(STOCK_STAGING, ignore_errors=True)
            staged = 0
            for root, _dirs, files in os.walk(STOCK_STAGING):
                for fn in files:
                    try:
                        staged += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
            if staged + n > STOCK_STAGING_MAX:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self._json(400, {"error": "At most %d MB can wait to be imported at once. "
                                                 "Import these, then add the rest."
                                                 % (STOCK_STAGING_MAX >> 20)})
            os.makedirs(d, exist_ok=True)
            left = n
            with open(os.path.join(d, name), "wb") as f:
                while left:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return self._json(200, {"ok": True, "received": n - left})

        try:
            body = self._body()
        except Exception as err:                      # noqa: BLE001
            return self._json(400, {"error": str(err)})
        try:
            body = unseal(body, self._client())
        except ValueError as err:
            _LOGGER.warning("sealed request to %s refused: %s", path, err)
            return self._json(400, {"error": "This form expired. Try again."})

        if path == "/api/settings-port":
            try:
                port = system.set_settings_port(body.get("port"), PORT)
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})
            url = system.settings_url(netcfg.device_name(), port)
            return self._json(200, {"ok": True, "port": port, "url": url, "moved": port != PORT})

        if path == "/api/leds":
            try:
                overrides = validate(body)
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            write_overrides(overrides)
            return self._json(200, {"saved": len(overrides)})

        if path == "/api/audio":
            try:
                out = {}
                if "duck_level" in body or "duck_enabled" in body:
                    duck = agent.load_duck()
                    level = int(body.get("duck_level", duck["level"]))
                    enabled = bool(body.get("duck_enabled", duck["enabled"]))
                    if not 0 <= level <= 100:
                        raise ValueError("ducking level must be 0-100")
                    agent.save_duck({"enabled": enabled, "level": level})
                    out["duck"] = {"enabled": enabled, "level": level}
                if body.get("buttons"):
                    cfg = agent.load_buttons()
                    for gesture, action in body["buttons"].items():
                        if gesture not in agent.BUTTON_DEFAULTS:
                            raise ValueError("unknown gesture %r" % gesture)
                        if action not in agent.BUTTON_ACTIONS:
                            raise ValueError("unknown action %r" % action)
                        cfg[gesture] = action
                    agent.save_buttons(cfg)
                    out["buttons"] = cfg
                if "pairing" in body:
                    if bool(body["pairing"]):
                        agent.start_pairing()
                    else:
                        agent.stop_pairing()
                    out["pairing"] = bool(body["pairing"])
                if "eq" in body:
                    # Through biscuit_eq, the same code biscuit-dsp runs at
                    # start, so what is saved here is what a reboot applies.
                    want = body["eq"] or {}
                    cur = eq_store.load()
                    mode = str(want.get("mode", cur["eq_mode"]))
                    if mode not in eq_store.EQ_MODES:
                        raise ValueError("unknown EQ mode %r" % mode)
                    cur["eq_mode"] = mode
                    for band in ("bass", "mid", "treble"):
                        if band in want:
                            try:
                                v = float(want[band])
                            except (TypeError, ValueError):
                                raise ValueError("EQ %s must be a number" % band)
                            if not eq_store.EQ_DB_MIN <= v <= eq_store.EQ_DB_MAX:
                                raise ValueError(
                                    "EQ %s must be %g..%g dB"
                                    % (band, eq_store.EQ_DB_MIN, eq_store.EQ_DB_MAX))
                            cur["eq_" + band] = v
                    eq_store.save(cur)
                    out["eq"] = cur
                if body.get("earcon_set"):
                    want = str(body["earcon_set"])
                    if want not in agent.EARCON_SETS:
                        raise ValueError("unknown sound set %r" % want)
                    agent.save_earcon_set(want)
                    out["earcon_set"] = want
                if "volume" in body:
                    vol = int(body["volume"])
                    if not 0 <= vol <= 100:
                        raise ValueError("volume must be 0-100")
                    # biscuit-audio owns the codec, the DSP loudness and the
                    # ring feedback together; nothing here attenuates on its own.
                    agent.request_volume(vol)
                    out["volume"] = vol
                if "muted" in body:
                    agent.request_mute(bool(body["muted"]))
                    out["muted"] = bool(body["muted"])
                if "autodim" in body:
                    agent.save_autodim(bool(body["autodim"]))
                    out["autodim"] = bool(body["autodim"])
                for field, modes, save in (
                        ("mic_led", agent.MIC_LED_MODES, agent.save_mic_led),
                        ("mute_led", agent.MUTE_LED_MODES, agent.save_mute_led)):
                    if body.get(field):
                        want = str(body[field])
                        if want not in modes:
                            raise ValueError("unknown %s mode %r" % (field, want))
                        save(want)
                        out[field] = want
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            return self._json(200, out)

        if path == "/api/mic/call-stock":
            try:
                imported = agent.call_profile.import_stock(body.get("afe_cfg"))
                # Existing active profile is reloaded by the hub on request.
                agent.restart_capture("stock call profile import")
                return self._json(200, {"imported": imported})
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})

        if path == "/api/mic":
            if body.get("reset"):
                # "Reset to recommended": the same values a fresh device runs,
                # put through the same checks as any other change below. The
                # call settings are separate and left alone.
                body = {"mic_source": "Array",
                        "mic_profile": agent.default_mic_profile(),
                        "mic_vad": agent.default_mic_vad(),
                        "mic_aec": "On", "mic_beam": "On",
                        "mic_capsule": "MK7 (centre)",
                        "mic_pga_gain": agent.MIC_GAIN_DB_DEFAULT,
                        "miccal": "Factory"}
                # The recommended chains decide echo cancellation themselves,
                # and naming it for one of them is refused below.
                if not agent.mic_aec_available(dict(agent.load_mic_settings(), **body)):
                    body.pop("mic_aec")
            try:
                cur = agent.load_mic_settings()
                want = dict(cur)
                fields = (("mic_source", agent.MIC_SOURCES),
                          ("mic_profile", agent.MIC_PROFILES),
                          ("mic_vad", agent.MIC_VADS),
                          ("mic_aec", agent.ON_OFF),
                          ("mic_beam", agent.ON_OFF),
                          ("mic_capsule", agent.MIC_CAPSULES),
                          ("call_mic_capsule", agent.MIC_CAPSULES),
                          ("call_processing", agent.ON_OFF),
                          ("call_profile", agent.call_profile.PROFILE_LABELS))
                for field, choices in fields:
                    if field in body:
                        value = str(body[field])
                        if value not in choices:
                            raise ValueError("unknown %s %r" % (field, value))
                        want[field] = value
                if want.get("call_profile") == "Stock-derived tuning" and not agent.call_profile.ensure_stock():
                    raise ValueError("import stock AFE.cfg first")
                if "mic_pga_gain" in body:
                    gain = float(body["mic_pga_gain"])
                    if not agent.MIC_GAIN_DB_MIN <= gain <= agent.MIC_GAIN_DB_MAX:
                        raise ValueError("microphone gain must be %g..%g dB"
                                         % (agent.MIC_GAIN_DB_MIN,
                                            agent.MIC_GAIN_DB_MAX))
                    want["mic_pga_gain"] = gain
                # Presets are gone: every control here is its own setting, so
                # there is no second view of this file to fall out of step.
                _vwhy = agent.mic_vad_unavailable(want["mic_vad"])
                if _vwhy and want["mic_vad"] != cur["mic_vad"]:
                    raise ValueError("%s cannot be selected: %s"
                                     % (want["mic_vad"], _vwhy))
                _why = agent.mic_profile_unavailable(want["mic_profile"])
                if _why and want["mic_profile"] != cur["mic_profile"]:
                    raise ValueError("%s cannot be selected: %s"
                                     % (want["mic_profile"], _why))
                if (want["mic_profile"] in agent.MIC_PROFILE_ARRAY_ONLY
                        and want["mic_source"] == "Single microphone"):
                    raise ValueError("%s has no single-microphone path"
                                     % want["mic_profile"])
                if "mic_aec" in body and not agent.mic_aec_available(want):
                    raise ValueError("echo cancellation is unavailable for the assistant single-mic path")
                miccal = None
                if body.get("miccal"):
                    miccal = str(body["miccal"])
                    if miccal not in agent.MICCAL_MODES:
                        raise ValueError("unknown calibration mode %r" % miccal)
                    if miccal == agent.load_miccal_mode():
                        miccal = None
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            # Applying restarts capture and the assistant with it - about ten
            # seconds deaf - so only a real change is allowed to do it. And only
            # ONE restart, however many things changed: calibration and the
            # chain used to restart separately, the second was refused as busy,
            # and the page reported a change that had been applied as one that
            # had not (Reset to recommended did this every time).
            changed = want != cur or miccal is not None
            refused = None
            if miccal is not None:
                agent.save_miccal_mode(miccal, restart=False)
            if want != cur:
                try:
                    agent.save_mic_settings(want, restart=False)
                except ValueError as err:
                    refused = str(err)
                    changed = miccal is not None
            if changed:
                agent.restart_capture("microphone settings")
            if refused:
                return self._json(400, {"error": refused})
            return self._json(200, dict(mic_state(), restarting=changed))

        if path == "/api/sounds":
            try:
                out = {}
                if body.get("set"):
                    want = str(body["set"])
                    if want not in agent.EARCON_SETS:
                        raise ValueError("unknown sound set %r" % want)
                    agent.save_earcon_set(want)
                    out["set"] = want
                if isinstance(body.get("sounds"), dict):
                    before = earcon_map.assistant_flags()
                    write_sound_overrides(body["sounds"])
                    out["saved"] = len(body["sounds"])
                    # The assistant reads its sounds once, at launch. Restart
                    # it when - and only when - what it would be told changed;
                    # the set above already restarts it on its own.
                    if not body.get("set") and earcon_map.assistant_flags() != before:
                        out["assistant_restart"] = restart_assistant_soon()
                if body.get("preview"):
                    name = str(body["preview"])
                    if not _installed_sound(name):
                        raise ValueError("no sound named %r is installed" % name)
                    # Through biscuit-earcon rather than a player of our own:
                    # it is the one path that knows about the DSP's codec wake
                    # and the cached earcon table.
                    subprocess.Popen(
                        ["/usr/bin/biscuit-earcon", "--file", name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
                    out["previewing"] = name
            except (ValueError, TypeError, OSError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200, dict(out, **sound_state()))

        if path == "/api/wifi":
            action = str(body.get("action", ""))
            try:
                if action == "connect":
                    ok, msg = netcfg.wifi_connect(
                        str(body.get("ssid", "")),
                        body.get("psk"),
                        bool(body.get("hidden")))
                elif action == "forget":
                    ok, msg = netcfg.wifi_forget(int(body.get("id", -1)))
                elif action == "select":
                    ok, msg = netcfg.wifi_select(int(body.get("id", -1)))
                elif action == "reconnect":
                    ok, msg = netcfg.wifi_reconnect()
                else:
                    raise ValueError("unknown Wi-Fi action %r" % action)
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            # The state is returned WITHOUT a scan: the caller has just changed
            # the association, and asking the radio to scan at the same moment
            # is what makes a join look like it failed when it was only slow.
            return self._json(200 if ok else 400,
                              dict(wifi_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/bluetooth":
            action = str(body.get("action", ""))
            mac = str(body.get("mac", ""))
            try:
                if action == "btproxy":
                    # Restarting the service takes a moment; return the state
                    # the service actually reports afterwards rather than what
                    # was asked for, so the page cannot show a switch that is
                    # on while the radio is idle.
                    enabled = body.get("enabled")
                    act = body.get("active")
                    return self._json(200, {"btproxy": set_btproxy(
                        enabled=None if enabled is None else bool(enabled),
                        cadence=str(body.get("cadence") or "") or None,
                        active=None if act is None else bool(act))})
                if action == "pairing":
                    if bool(body.get("open")):
                        agent.start_pairing()
                        # WAIT FOR THE WINDOW TO ACTUALLY OPEN.
                        #
                        # start_pairing only spawns biscuit-pair-session, which
                        # is detached and takes about four seconds to get its
                        # agent up and write the marker. Answering immediately
                        # meant this handler read pairing_open() == False and
                        # the page redrew with the switch still OFF, so turning
                        # pairing on looked like it had done nothing at all.
                        #
                        # Bounded and cheap: a threaded HTTP handler can afford
                        # to block here, unlike the agent's websocket loop.
                        deadline = time.monotonic() + 8.0
                        while time.monotonic() < deadline:
                            if agent.pairing_open():
                                break
                            time.sleep(0.25)
                        else:
                            _LOGGER.warning(
                                "pairing window did not open within 8s")
                    else:
                        agent.stop_pairing()
                    ok, msg = True, ("Pairing is open for three minutes."
                                     if body.get("open") else "Pairing closed.")
                elif action == "scan":
                    ok, msg = True, "Scan finished."
                    return self._json(200, dict(bt_state(scan=True),
                                                ok=ok, message=msg))
                elif action == "pair":
                    ok, msg = netcfg.bt_pair(mac)
                elif action == "connect":
                    ok, msg = netcfg.bt_connect(mac)
                elif action == "disconnect":
                    ok, msg = netcfg.bt_disconnect(mac)
                elif action == "remove":
                    ok, msg = netcfg.bt_remove(mac)
                elif action == "alias":
                    ok, msg = netcfg.bt_set_alias(str(body.get("alias", "")))
                elif action == "cast":
                    # An empty mac means "stop casting", which is a normal
                    # request rather than a missing argument.
                    ok, msg = netcfg.set_cast_target(mac)
                else:
                    raise ValueError("unknown Bluetooth action %r" % action)
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200 if ok else 400,
                              dict(bt_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/service":
            try:
                inv = inventory_state(fresh=True)
                result = services.service_start(str(body.get("name", "")),
                                                str(body.get("action", "")),
                                                inv["services"], netcfg.package_job())
                return self._json(200, result)
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})

        if path == "/api/services":
            try:
                result = services.start(body, netcfg.package_job())
                return self._json(200, result)
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})

        if path == "/api/packages":
            action = str(body.get("action", ""))
            pkg_id = str(body.get("id", ""))
            # "install"/"remove" read better in the UI than apk's own verbs.
            verb = {"install": "add", "remove": "del"}.get(action)
            if verb is None:
                return self._json(400, {"error": "unknown action %r" % action})
            if pkg_id not in ("voice", "sendspin"):
                return self._json(400, {"error": "This system component cannot be removed."})
            if services.JOB and services.JOB.get("state") == "running":
                return self._json(409, {"error": "Wait for the service operation to finish."})
            # Through the same start lock as the Apps page's Install, so this
            # older endpoint cannot start an install alongside the daily check
            # or the retry of the apps chosen at setup.
            res = start_pkg_job(lambda: netcfg.start_package_job(verb, pkg_id))
            ok, msg = res if res is not None else (False, "Another package operation is still running.")
            return self._json(200 if ok else 400,
                              dict(packages_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/apps":
            action = str(body.get("action", ""))
            if action in ("retry", "cancel"):
                return self._pending_action(action)
            app_id = str(body.get("id", ""))
            if app_id not in [a["id"] for a in system.apps()["apps"]]:
                return self._json(400, {"error": "No such app."})
            verb = {"install": "add", "remove": "del"}.get(action)
            if verb is None:
                return self._json(400, {"error": "unknown action %r" % action})
            if services.JOB and services.JOB.get("state") == "running":
                return self._json(409, {"error": "Wait for the other change to finish."})
            res = start_pkg_job(lambda: netcfg.start_package_job(verb, app_id))
            ok, msg = res if res is not None else (False, "Another package operation is still running.")
            return self._json(200 if ok else 400,
                              dict(apps_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/datetime":
            try:
                return self._json(200, system.set_timezone(body.get("timezone")))
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})

        if path == "/api/stock":
            action = str(body.get("action", ""))
            kind = str(body.get("kind", ""))
            try:
                if action == "remove":
                    msg = system.stock_remove(kind, body.get("name") or None)
                    if kind == "led" and body.get("name"):
                        msg = "Removed %s." % agent.stock_label(str(body["name"]))
                elif action == "import":
                    staging = os.path.join(STOCK_STAGING, kind)
                    if not kind.isalnum() or not os.path.isdir(staging):
                        raise ValueError("nothing was uploaded")
                    try:
                        msg = system.stock_import(kind, staging)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                    if kind == "fireos6":
                        # The call tuning is derived from AFE.cfg; take the new one.
                        try:
                            os.unlink(agent.call_profile.STOCK_FILE)
                        except (AttributeError, OSError):
                            pass
                else:
                    raise ValueError("unknown action")
            except (ValueError, OSError, subprocess.SubprocessError) as err:
                return self._json(400, {"error": str(err)})
            if kind == "led":
                reset = refresh_stock_choices()
                if reset:
                    msg += " %s went back to %s default." % (
                        ", ".join(LABELS.get(k, k) for k in reset),
                        "its" if len(reset) == 1 else "their")
            return self._json(200, dict(system.stock_state(), led_used=led_usage(), ok=True, message=msg))

        if path == "/api/reset":
            scope = str(body.get("scope", ""))
            if body.get("confirm") is not True:
                return self._json(400, {"error": "confirm the reset"})
            job = netcfg.package_job()
            if job and job.get("state") == "running":
                return self._json(409, {"error": "Wait for the running update to finish."})
            if system.boot_damaged():
                return self._json(409, {"error": "A boot partition may be damaged. Install the boot "
                                                 "image again before resetting."})
            if scope == "all":
                why = self._reauth(body)
                if why:
                    return self._json(403, {"error": why})
            try:
                system.request_reset(scope)
            except (ValueError, OSError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200, {"ok": True, "message": "Restarting to reset."})

        if path == "/api/storage":
            if body.get("action") != "clear_cache":
                return self._json(400, {"error": "unknown action"})
            p = subprocess.run(["apk", "cache", "clean"], capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                return self._json(400, {"error": (p.stderr or p.stdout).strip()[-300:] or "failed"})
            return self._json(200, dict(system.storage_state(), ok=True))

        if path == "/api/updates":
            action = str(body.get("action", ""))
            running = netcfg.package_job()
            if action == "restart" and running and running.get("state") == "running":
                # A restart kills the job, and one writing the boot partitions
                # would leave a device that does not start.
                return self._json(409, {"error": "Wait for the running update to finish."})
            if action == "restart" and system.boot_damaged():
                return self._json(409, {"error": "A boot partition may be damaged. Install the boot "
                                                 "image again before restarting."})
            if action == "restart":
                # Detached, so the answer reaches the page before the device
                # goes away.
                subprocess.Popen(["setsid", "sh", "-c", "sleep 2; reboot"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return self._json(200, {"ok": True, "message": "Restarting."})
            if action == "auto_check":
                system.set_auto_check(bool(body.get("on")))
                return self._json(200, dict(updates_state(), ok=True))
            if action not in system.JOB_ACTIONS:
                return self._json(400, {"error": "unknown action %r" % action})
            if action in ("sysupdate", "boot", "bootrollback"):
                why = self._reauth(body)
                if why:
                    return self._json(403, {"error": why})
            job = netcfg.package_job()
            if job and job.get("state") == "running":
                return self._json(409, {"error": "Another package change is still running."})
            if services.JOB and services.JOB.get("state") == "running":
                return self._json(409, {"error": "Wait for the other change to finish."})
            pkgs = body.get("packages") or []
            if action == "sysupdate":
                known = system.apk_db()["pkgs"]
                if (not isinstance(pkgs, list) or
                        any(not isinstance(x, str) or x not in known or
                            system.is_ours(x) for x in pkgs)):
                    return self._json(400, {"error": "unknown system package"})
            if start_pkg_job(lambda: system.start_job(action, pkgs if action == "sysupdate" else None) or True) is None:
                return self._json(409, {"error": "Another package change is still running."})
            return self._json(200, dict(updates_state(), ok=True))

        if path == "/api/usb":
            ok, msg = set_usb_features(body.get("features"))
            return self._json(200 if ok else 400,
                              dict(usb_state(), ok=ok,
                                   **({"message": msg} if ok else
                                      {"error": msg})))

        if path == "/api/ssh":
            action = str(body.get("action", ""))
            # Adding a key, or turning SSH or password sign-in on, opens a way
            # in to an account with passwordless sudo - everything the
            # password re-check guards, one step away. So they ask for it too.
            # Removing access does not.
            if action == "add_key" or (action in ("password_auth", "enabled") and body.get("enabled")):
                why = self._reauth(body)
                if why:
                    return self._json(403, {"error": why})
            try:
                if action == "add_key":
                    ok, msg = netcfg.add_ssh_key(str(body.get("key", "")))
                elif action == "remove_key":
                    ok, msg = netcfg.remove_ssh_key(
                        str(body.get("fingerprint", "")))
                elif action == "password_auth":
                    ok, msg = netcfg.set_ssh_password_auth(
                        bool(body.get("enabled")))
                elif action == "enabled":
                    ok, msg = netcfg.set_sshd_enabled(bool(body.get("enabled")))
                else:
                    raise ValueError("unknown ssh action %r" % action)
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200 if ok else 400,
                              dict(netcfg.ssh_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/account":
            action = str(body.get("action", ""))
            info = netcfg.user_info()
            if not info.get("exists"):
                return self._json(400, {"error": "no account on this device"})
            username = info["username"]
            try:
                if action == "password":
                    # The CURRENT password is checked here rather than in
                    # netcfg, against the same hash the login form uses. A
                    # session cookie alone is not enough to change the password
                    # it was obtained with: an unattended browser would
                    # otherwise be a permanent takeover.
                    #
                    # Under the same lockout as signing in, and with the new
                    # password checked first: otherwise this was an unlimited
                    # guessing oracle for anyone at a signed-in browser - a
                    # wrong guess said 403, a right one with an empty new
                    # password said 400.
                    addr = self._client()
                    if throttled(addr):
                        return self._json(429, {"error": "Too many attempts. Wait a few minutes."})
                    new = str(body.get("new", ""))
                    if len(new) < 8 or "\n" in new or "\r" in new:
                        return self._json(400, {"error": "Choose a password of at least 8 characters, on one line."})
                    if not verify_password(username,
                                           str(body.get("current", ""))):
                        note_failure(addr)
                        return self._json(403,
                                          {"error": "That is not the current password."})
                    _failures.pop(addr, None)
                    ok, msg = netcfg.set_password(username,
                                                  str(body.get("new", "")))
                elif action == "full_name":
                    ok, msg = netcfg.set_full_name(username,
                                                   str(body.get("full_name", "")))
                elif action == "device_name":
                    ok, msg = netcfg.set_device_name(str(body.get("name", "")))
                else:
                    raise ValueError("unknown account action %r" % action)
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200 if ok else 400,
                              dict(about_state(), ok=ok,
                                   **({"message": msg} if ok else {"error": msg})))

        if path == "/api/ring":
            try:
                # Validate the complete request before changing any setting.
                if "autodim" in body and not isinstance(body["autodim"], bool):
                    raise ValueError("autodim must be true or false")
                if "brightness" in body:
                    value = body["brightness"]
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                        raise ValueError("brightness must be an integer from 0 to 255")
                if 'alignment' in body:
                    value=body['alignment']
                    if not isinstance(value,dict) or not isinstance(value.get('offset'),(int,float)) or isinstance(value.get('offset'),bool) or not __import__('math').isfinite(value['offset']) or not isinstance(value.get('reverse'),bool):
                        raise ValueError('invalid ring alignment')
                if "palette" in body:
                    palette = body["palette"]
                    if not isinstance(palette, dict) or palette.get("preset") not in ring_config.PALETTES:
                        raise ValueError("unknown accent palette")
                    if "colours" in palette:
                        ring_config.validate_colours(palette["colours"])
                if "accent" in body:
                    # One of Home Assistant's Ring Colour 2 and 3 lights, set
                    # the way the entity sets it (ring_config.save_accent), so
                    # a change here keeps the other's on/off and brightness.
                    # Saving whole colours with the palette used to reset both.
                    acc = body["accent"]
                    if not isinstance(acc, dict) or acc.get("index") not in (0, 1):
                        raise ValueError("accent index must be 0 or 1")
                    cmd = {"rgb_changed": False, "brightness_changed": False,
                           "state_changed": False}
                    if "rgb" in acc:
                        rgb = acc["rgb"]
                        if (not isinstance(rgb, list) or len(rgb) != 3 or any(
                                isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255
                                for v in rgb)):
                            raise ValueError("accent colour must be an RGB triple")
                        cmd.update(rgb_changed=True, red=rgb[0] / 255,
                                   green=rgb[1] / 255, blue=rgb[2] / 255)
                    if "level" in acc:
                        lv = acc["level"]
                        if isinstance(lv, bool) or not isinstance(lv, int) or not 0 <= lv <= 255:
                            raise ValueError("accent level must be 0 to 255")
                        cmd.update(brightness_changed=True, brightness=lv / 255)
                    if "on" in acc:
                        if not isinstance(acc["on"], bool):
                            raise ValueError("accent on must be true or false")
                        cmd.update(state_changed=True, state=acc["on"])
                if "viz" in body:
                    if not isinstance(body["viz"], dict):
                        raise ValueError("viz must be an object")
                    for key, value in body["viz"].items():
                        if key not in ring_config.VIZ_SETTINGS:
                            raise ValueError("unknown visualiser setting %s" % key)
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            raise ValueError("%s must be a number" % key)
                if 'alignment' in body:
                    ring_config.save_alignment(body['alignment'])
                if "brightness" in body:
                    ring_config.save_brightness(body["brightness"])
                if "autodim" in body:
                    ring_config.save_autodim(body["autodim"])
                if "palette" in body:
                    ring_config.save_palette(palette["preset"], palette.get("colours"))
                if "accent" in body:
                    ring_config.save_accent(acc["index"], cmd)
                if "viz" in body:
                    ring_config.save_viz(body["viz"])
            except (ValueError, TypeError) as err:
                return self._json(400, {"error": str(err)})
            return self._json(200, ring_config.state())

        if path == "/api/preview":
            # The manual control at the top of the LED page uses this too, so
            # that driving the ring by hand needs no activity and changes no
            # stored mapping. `stop` clears whatever it started; `hold` runs
            # without a lifetime, for someone setting a colour and looking at
            # it. Everything else is the ordinary bounded row preview.
            if body.get("stop"):
                last = self.server.manual_anim
                self.server.manual_anim = None
                ok = ring("clear") if last is None else ring("stop %s" % last)
                return self._json(200, {"stopped": last, "ring": ok})
            try:
                spec = validate_spec(body, "preview")
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            if spec is None:
                # "Default" is a real, previewable choice - it just happens to be
                # the stock animation for whichever activity the row represents,
                # so the row has to say which one it is.
                key = body.get("activity")
                if key not in LABELS:
                    return self._json(400, {"error": "no activity to take a default from"})
                spec = dict(DEFAULT_MAP.get(key) or {})
                spec.pop("lifetime", None)          # the preview sets its own
                if not spec:
                    return self._json(400, {"error": "that activity has no default"})
            # Under a name of its own, on the top layer: a preview of the
            # animation an activity is holding must not replace that playback
            # (its ring would end with the preview), and must not be hidden.
            # A held pattern (the manual control) has no lifetime, so it keeps
            # the old bottom place (fx_manual is not in the layer table) under
            # voice, volume and mute - and a name apart from the previews.
            # A one-shot file repeats where it would in use: in a held
            # activity's row, and in the manual control, which holds until
            # stopped. Elsewhere it plays as it is, once.
            loop = bool(body.get("hold")) or agent.held_activity(body.get("activity"))
            anim = agent.resolve_spec(spec, as_name="fx_manual" if body.get("hold") else "fx_preview",
                                      loop=loop)
            if not anim:
                return self._json(500, {"error": "could not build that animation"})
            if body.get("hold"):
                # No lifetime, so it stays until stopped. Remembered because the
                # stop above has to name the animation it actually started - a
                # bare `clear` would also retire mute, music and anything else
                # holding the ring at the time.
                if self.server.manual_anim and self.server.manual_anim != anim:
                    ring("stop %s" % self.server.manual_anim)
                self.server.manual_anim = anim
                ok = ring("play %s" % anim)
            else:
                ok = ring("play %s %d" % (anim, PREVIEW_SECONDS))
            return self._json(200, {"playing": anim, "ring": ok})

        self._send(404, "not found", "text/plain")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    # What the manual ring control is currently holding, if anything. On the
    # server rather than in the page, so closing the tab does not orphan an
    # animation that nothing remembers the name of.
    manual_anim = None
    daemon_threads = True
    address_family = socket.AF_INET

    def verify_request(self, request, client_address):
        """Nothing from the open setup network.

        biscuit-setup pauses this page for every setup session, but anything
        can start it again while one runs, and it listens on every address -
        the setup network's included. So a connection to 192.168.4.1, or from
        192.168.4.0/24 while the setup network is up, is closed before a byte
        of it is read. That subnet is somebody's house network outside setup
        (eero uses 192.168.4.0/22), so only the address itself is refused
        then, as sshd refuses it (40-biscuit.conf)."""
        try:
            local = request.getsockname()[0]
        except OSError:
            return False
        peer = client_address[0] if client_address else ""
        if setup_network_client(local, peer):
            if peer not in self._setup_refused:
                if len(self._setup_refused) > 256:
                    self._setup_refused.clear()
                self._setup_refused.add(peer)
                _LOGGER.warning("refused %s -> %s: the setup network does not "
                                "reach the settings page", peer, local)
            return False
        return True

    _setup_refused = set()

    def handle_error(self, request, client_address):
        """Swallow client disconnects; report everything else.

        HTTP/1.1 keep-alive means a browser routinely closes an idle connection,
        and http.server logs a full traceback for each one. Left alone, this log
        is mostly ConnectionResetError and a genuine failure would be invisible
        in the noise.
        """
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        socketserver.ThreadingTCPServer.handle_error(self, request, client_address)


# Self-contained for the same reason the rest of the page is: the device may be
# on a network with no route out, and a login screen that needs a CDN fails
# exactly when someone is trying to fix their network.
LOGIN_PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Sign in</title>
<style>
/* Matches the settings page it leads to: same Adwaita tokens, same card. */
:root {
  --bg:#fafafa; --card:#fff; --fg:#2e3436; --dim:rgba(0,0,0,.55);
  --line:rgba(0,0,0,.08); --accent:#3584e4; --accent-fg:#fff; --danger:#e01b24;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark) {
  :root {
    --bg:#242424; --card:#303030; --fg:#fff; --dim:rgba(255,255,255,.55);
    --line:rgba(255,255,255,.1); --accent:#78aeed; --accent-fg:#12212f; --danger:#ff7b63;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; min-height:100vh; display:grid; place-items:center; padding:1.5rem;
  background:var(--bg); color:var(--fg);
  font:15px/1.45 -apple-system,system-ui,"Cantarell","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
form {
  width:min(21rem,100%); background:var(--card); border-radius:14px;
  box-shadow:0 1px 2px rgba(0,0,0,.06), 0 2px 12px rgba(0,0,0,.06);
  padding:1.5rem; display:flex; flex-direction:column; gap:.85rem;
}
.mark { display:flex; justify-content:center; color:var(--accent); margin-bottom:.2rem; }
h1 { font-size:1.15rem; margin:0; text-align:center; }
p.sub { color:var(--dim); margin:0 0 .4rem; font-size:.87rem; text-align:center; }
label { display:flex; flex-direction:column; gap:.25rem; font-size:.82rem; color:var(--dim); }
input {
  font:inherit; color:var(--fg); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; padding:.5rem .6rem; width:100%;
}
input:focus { outline:2px solid var(--accent); outline-offset:-1px; }
button {
  font:inherit; font-weight:600; padding:.55rem; cursor:pointer; margin-top:.2rem;
  background:var(--accent); color:var(--accent-fg); border:none; border-radius:8px;
}
button:hover:not(:disabled) { filter:brightness(1.08); }
button:disabled { opacity:.6; cursor:default; }
#msg { min-height:1.2em; font-size:.85rem; color:var(--danger); text-align:center; }
.warn { font-size:.78rem; color:var(--dim); border-top:1px solid var(--line);
        padding-top:.7rem; margin-top:.1rem; text-align:center; }

</style>
<form id="f">
  <div class="mark">
    <svg width="40" height="40" viewBox="0 0 16 16" fill="none" stroke="currentColor"
         stroke-width="1.3" aria-hidden="true">
      <circle cx="8" cy="8" r="6.5"/><circle cx="8" cy="8" r="2.6"/>
    </svg>
  </div>
  <h1>Echo settings</h1>
  <p class="sub">Sign in with the username and password you set up on this device.</p>
  <label>Username <input id="u" autocomplete="username" autocapitalize="none"
                         autocorrect="off" spellcheck="false" autofocus required></label>
  <label>Password <input id="p" type="password" autocomplete="current-password" required></label>
  <button id="go">Sign in</button>
  <div id="msg" role="alert"></div>
  <div class="warn" id="note">Your password is encrypted in this browser before it
    is sent. The rest of this page is plain HTTP on your local network.</div>
</form>
<script src="/n.js"></script>
<script>
/* The password is boxed for this device's key with a one-time challenge, so
   nobody capturing the network can read it or replay it. A browser that
   cannot do this still signs in, and the note says what it did instead. */
async function seal(obj) {
  if (!window.nacl || !window.fetch) return obj;
  const k = await (await fetch("/seal", { cache: "no-store" })).json();
  if (!k.key) return obj;
  const u8 = (b) => Uint8Array.from(atob(b), (c) => c.charCodeAt(0));
  const b64 = (u) => btoa(String.fromCharCode.apply(null, u));
  const msg = new TextEncoder().encode(JSON.stringify(Object.assign({}, obj, { _c: k.challenge })));
  const eph = nacl.box.keyPair();
  const nonce = nacl.randomBytes(nacl.box.nonceLength);
  return { sealed: b64(eph.publicKey) + "." + b64(nonce) + "." +
                   b64(nacl.box(msg, nonce, u8(k.key), eph.secretKey)) };
}
if (!window.nacl) document.getElementById("note").textContent =
  "This page is plain HTTP on your local network, and this browser could not " +
  "encrypt your password, so it is sent as typed.";
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const msg = document.getElementById("msg");
  const go = document.getElementById("go");
  msg.textContent = "";
  go.disabled = true;
  try {
    const body = await seal({username: document.getElementById("u").value,
                             password: document.getElementById("p").value});
    const r = await fetch("/login", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
    if (r.ok) { location.href = "/"; return; }
    let err = "Sign in failed.";
    try { err = (await r.json()).error || err; } catch (e2) {}
    msg.textContent = err;
  } catch (e3) {
    msg.textContent = "Could not reach the device.";
  }
  go.disabled = false;
  document.getElementById("p").value = "";
  document.getElementById("p").focus();
};
</script>
"""


# Self-contained by necessity as much as by taste: the device may be on a network
# with no route out, and a settings page that needs a CDN is a settings page that
# fails exactly when someone is trying to fix their network.
PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Echo settings</title>
<style>
/* Adwaita, as Phosh draws it. The device's own settings should not look like a
   router admin page, and the users most likely to open this one already know
   what a GNOME boxed list means. Hand-written rather than pulled from libadwaita:
   the device may be on a network with no route out, and a settings page that
   needs a CDN is a settings page that fails exactly when someone is trying to
   fix their network. */
:root {
  --bg:#fafafa; --card:#fff; --hdr:#ebebeb; --fg:#2e3436;
  --dim:rgba(0,0,0,.55); --line:rgba(0,0,0,.08); --hover:rgba(0,0,0,.04);
  --accent:#3584e4; --accent-fg:#fff; --danger:#e01b24; --ok:#2ec27e; --warn:#c88800;
  --shadow:0 1px 2px rgba(0,0,0,.06), 0 2px 8px rgba(0,0,0,.04);
  --r:12px;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark) {
  :root {
    --bg:#242424; --card:#303030; --hdr:#303030; --fg:#fff;
    --dim:rgba(255,255,255,.55); --line:rgba(255,255,255,.1); --hover:rgba(255,255,255,.06);
    --accent:#78aeed; --accent-fg:#12212f; --danger:#ff7b63; --ok:#8ff0a4; --warn:#f8e45c;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 2px 8px rgba(0,0,0,.2);
  }
}
* { box-sizing:border-box; }
html,body { margin:0; padding:0; }
/* The UA's own [hidden] rule loses to any author rule that sets display, and
   `button { display:inline-flex }` below is exactly such a rule - which left
   the back button visible on the root page. */
[hidden] { display:none !important; }
body {
  background:var(--bg); color:var(--fg);
  font:15px/1.45 -apple-system,system-ui,"Cantarell","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
  padding-bottom:env(safe-area-inset-bottom);
}

/* --- header bar ------------------------------------------------------- */
.hdr {
  position:sticky; top:0; z-index:20;
  display:grid; grid-template-columns:minmax(max-content,1fr) minmax(0,auto) minmax(max-content,1fr);
  align-items:center; gap:.5rem;
  padding:.55rem .75rem; padding-top:calc(.55rem + env(safe-area-inset-top));
  background:var(--hdr); border-bottom:1px solid var(--line);
}
.hdr h1 { font-size:1rem; font-weight:700; margin:0; text-align:center; white-space:nowrap;
          overflow:hidden; text-overflow:ellipsis; }
.hdr .right { display:flex; justify-content:flex-end; gap:.35rem; }
.hdr .left  { display:flex; justify-content:flex-start; }

/* --- buttons ---------------------------------------------------------- */
button, .btn {
  font:inherit; color:var(--fg); background:var(--card);
  border:1px solid var(--line); border-radius:8px;
  padding:.42rem .8rem; cursor:pointer; display:inline-flex; align-items:center;
  gap:.35rem; line-height:1.2;
}
button:hover:not(:disabled) { background:var(--hover); }
button:disabled { opacity:.5; cursor:default; }
button.flat { background:none; border-color:transparent; padding:.42rem .55rem; }
button.flat:hover:not(:disabled) { background:var(--hover); }
button.suggested { background:var(--accent); color:var(--accent-fg); border-color:transparent; font-weight:600; }
button.suggested:hover:not(:disabled) { filter:brightness(1.08); }
button.destructive { color:var(--danger); }
button.destructive.solid { background:var(--danger); color:#fff; border-color:transparent; font-weight:600; }
button.small { padding:.28rem .6rem; font-size:.85rem; }
.spin { animation:spin 1s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }

/* --- page ------------------------------------------------------------- */
main { max-width:44rem; margin:0 auto; padding:1rem .9rem 4rem; }
.group { margin:0 0 1.4rem; }
.group > h2 {
  font-size:.78rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
  color:var(--dim); margin:0 0 .45rem .15rem;
}
.group > p.help { font-size:.85rem; color:var(--dim); margin:.55rem .15rem 0; }

/* --- boxed list ------------------------------------------------------- */
.boxed { background:var(--card); border-radius:var(--r); box-shadow:var(--shadow); overflow:hidden; }
.row {
  display:flex; align-items:center; gap:.75rem;
  padding:.7rem .9rem; min-height:3rem;
  border-top:1px solid var(--line);
}
.boxed > .row:first-child { border-top:none; }
.row.tap { cursor:pointer; }
.row.tap:hover { background:var(--hover); }
.row .icon { flex:0 0 auto; color:var(--dim); display:flex; }
.row .txt { flex:1 1 auto; min-width:0; }
.row .title { font-weight:500; display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; }
.row .sub { font-size:.83rem; color:var(--dim); margin-top:.05rem;
            overflow-wrap:anywhere; }
/* Values wrap rather than ellipsing. A truncated "Amazon Echo D…" or
   "postmarketOS …" is worse than a second line: the whole point of the row is
   the value, and this is a phone-width layout by default. */
.row .val { color:var(--dim); font-size:.9rem; flex:0 1 auto; text-align:right;
            max-width:16rem; overflow-wrap:break-word; }
.row .end { flex:0 0 auto; display:flex; align-items:center; gap:.5rem; }
.row.stack { flex-direction:column; align-items:stretch; gap:.55rem; }

/* --- switch ----------------------------------------------------------- */
.sw { position:relative; width:46px; height:26px; flex:0 0 auto; }
.sw input { position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; z-index:1; }
.sw span {
  position:absolute; inset:0; border-radius:13px; background:var(--line);
  transition:background .15s ease;
}
.sw span::after {
  content:""; position:absolute; top:3px; left:3px; width:20px; height:20px;
  border-radius:50%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.3);
  transition:transform .15s ease;
}
.sw input:checked + span { background:var(--accent); }
.sw input:checked + span::after { transform:translateX(20px); }
.sw input:disabled + span { opacity:.5; }

/* --- form fields ------------------------------------------------------ */
input[type=text], input[type=password], input[type=number], select {
  font:inherit; color:var(--fg); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; padding:.45rem .55rem; width:100%;
}
select { cursor:pointer; }
input:focus, select:focus { outline:2px solid var(--accent); outline-offset:-1px; }
label.field { display:flex; flex-direction:column; gap:.25rem; font-size:.83rem; color:var(--dim); }
.row .val select, .row .end select { width:auto; max-width:13rem; }
input[type=range] { accent-color:var(--accent); width:100%; }
input[type=color] { width:2.4rem; height:1.9rem; padding:0; background:none;
                    border:1px solid var(--line); border-radius:6px; cursor:pointer; }
output.num { font-variant-numeric:tabular-nums; color:var(--dim); font-size:.85rem;
             min-width:3.4rem; text-align:right; }
.slider { display:flex; align-items:center; gap:.6rem; }

/* --- badges, meters --------------------------------------------------- */
.badge { font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
         padding:.1rem .4rem; border-radius:5px; background:var(--line); color:var(--dim); }
.badge.ok { background:color-mix(in srgb, var(--ok) 25%, transparent); color:var(--fg); }
.badge.accent { background:color-mix(in srgb, var(--accent) 25%, transparent); color:var(--fg); }
.badge.warn { background:color-mix(in srgb, var(--warn) 30%, transparent); color:var(--fg); }
.meter { height:6px; border-radius:3px; background:var(--line); overflow:hidden; margin-top:.35rem; }
.meter > i { display:block; height:100%; background:var(--accent); border-radius:3px; }
.meter.warn > i { background:var(--warn); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--line); flex:0 0 auto; }
.dot.on { background:var(--ok); }
.dot.off { background:var(--danger); }

/* --- status page (empty states) --------------------------------------- */
.status { text-align:center; padding:2.5rem 1rem; color:var(--dim); }
.status svg { opacity:.4; margin-bottom:.6rem; }
.status .t { font-weight:700; color:var(--fg); font-size:1rem; margin-bottom:.25rem; }
.status .d { font-size:.88rem; max-width:22rem; margin:0 auto; }

/* --- toast ------------------------------------------------------------ */
#toast {
  position:fixed; left:50%; bottom:1.2rem; transform:translate(-50%,3rem);
  background:#303030; color:#fff; padding:.6rem 1rem; border-radius:10px;
  font-size:.88rem; box-shadow:0 4px 16px rgba(0,0,0,.3); opacity:0;
  transition:opacity .2s ease, transform .2s ease; pointer-events:none; z-index:50;
  max-width:calc(100vw - 2rem); text-align:center;
}
#toast.show { opacity:1; transform:translate(-50%,0); }
#toast.bad { background:var(--danger); color:#fff; }

/* --- dialog ----------------------------------------------------------- */
#scrim {
  position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:40;
  display:flex; align-items:center; justify-content:center; padding:1rem;
}
#scrim[hidden] { display:none; }
.dlg {
  background:var(--card); border-radius:14px; box-shadow:0 8px 32px rgba(0,0,0,.35);
  width:min(23rem,100%); max-height:90vh; overflow:auto; padding:1.1rem;
}
.dlg h3 { margin:0 0 .3rem; font-size:1.05rem; }
.dlg p { margin:0 0 .9rem; font-size:.88rem; color:var(--dim); }
.dlg .fields { display:flex; flex-direction:column; gap:.7rem; margin-bottom:1rem; }
.dlg .acts { display:flex; gap:.5rem; justify-content:flex-end; }
.dlg .err { color:var(--danger); font-size:.85rem; margin:0 0 .7rem; min-height:1.1em; }

/* --- misc ------------------------------------------------------------- */
.skeleton { color:var(--dim); text-align:center; padding:2rem; }
.mono { font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace; font-size:.85rem; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fill,minmax(9rem,1fr)); gap:.5rem .9rem; }
/* At phone width a title, a subtitle and a dropdown do not fit on one line, and
   squeezing the dropdown is what turned every option into "Stock chain (defa…".
   Phosh's own answer is to let the control drop to its own full-width line, so
   rows that carry one are marked and do exactly that. */
/* At phone width a title, a subtitle and a control do not fit on one line, and
   squeezing the control is what turned every option into "Stock chain (defa…"
   and "Living-Room-Echo" into three stacked fragments. Phosh's own answer is to
   drop the control to a line of its own.

   Only the control moves. Giving `.txt` the full basis instead pushed the
   leading icon and the trailing chevron onto lines of their own, which reads as
   a broken row rather than a wrapped one - so the wrapping element is ordered
   last and everything else stays put. */
@media (max-width:32rem) {
  .row.ctl, .row.hasval { flex-wrap:wrap; }
  /* flex-basis:auto sizes .txt to its text, and once wrapping is allowed a long
     subtitle takes a whole line rather than shrinking - which left the leading
     icon stranded above the title. A zero basis makes it shrink instead. */
  .row.ctl .txt, .row.hasval .txt { flex:1 1 0; }
  .row.ctl .end { flex:1 1 100%; justify-content:flex-end; order:10; margin-top:.1rem; }
  .row.ctl .end select { max-width:none; flex:1 1 auto; min-width:0; }
  /* A flex item will not shrink below its content unless told, and an option
     naming two sounds is wider than a phone. */
  .row.ctl .end { min-width:0; }
  .row.hasval .val { flex:1 1 100%; max-width:none; order:10; margin-top:.1rem; }
}
.pattern-colours { display:flex; flex-wrap:nowrap; gap:4px; justify-content:flex-end; }
.pattern-colours:empty, .pattern-colours[hidden] { display:none; }
.pattern-colours label { display:flex; align-items:center; }
/* The swatch IS the control - its role is on the tooltip and the aria-label,
   so the row stays on ONE line instead of carrying "Colour"/"Background"
   text and pushing the colours onto a second row of their own. */
.pattern-colours input[type=color] { width:26px; height:24px; padding:0;
  border:1px solid var(--line); border-radius:5px; background:none; cursor:pointer; }
.ring-choice .end { flex:0 1 auto; flex-wrap:nowrap; max-width:100%; justify-content:flex-end; gap:6px; }
.ring-choice .end select { min-width:0; }
.ring-level { display:flex; align-items:center; gap:10px; }
.ring-level input { width:180px; max-width:40vw; }
/* --- folds: advanced settings and long lists --------------------------- */
/* A <details> styled as a group heading, so a folded section reads as the same
   kind of thing as the groups around it, with a disclosure arrow. */
details.fold { margin:0 0 1.4rem; }
details.fold > summary {
  list-style:none; cursor:pointer; display:flex; align-items:center; gap:.35rem;
  font-size:.78rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
  color:var(--dim); margin:0 0 .45rem .15rem; user-select:none;
}
details.fold > summary::-webkit-details-marker { display:none; }
details.fold > summary::before {
  content:""; width:.42rem; height:.42rem; border:solid currentColor;
  border-width:0 2px 2px 0; transform:rotate(-45deg); transition:transform .15s ease;
  margin-right:.15rem;
}
details.fold[open] > summary::before { transform:rotate(45deg); }
details.fold > .group { margin-bottom:.9rem; }
details.fold > p.help { font-size:.85rem; color:var(--dim); margin:.55rem .15rem 0; }
.row .title.big { font-size:1.05rem; font-weight:700; }
.row .sub a, .group > p.help a, .dlg p a { color:var(--accent); }
.notice { border-left:3px solid var(--warn); }
.notice.accent { border-left-color:var(--accent); }

/* --- choices, Android's list preference -------------------------------- */
.row .cur { color:var(--accent); font-size:.88rem; margin-top:.05rem; }
.row .cur.curbtn { display:block; background:none; border:none; padding:0; font:inherit; font-size:.88rem;
                   color:var(--accent); text-align:left; cursor:pointer; line-height:1.45; }
.row .cur.curbtn:hover:not(:disabled) { background:none; text-decoration:underline; }
.row .cur.curbtn:disabled { color:var(--dim); cursor:default; }
.row.off { opacity:.5; }
.row.off.tap { cursor:default; }
.opts { display:flex; flex-direction:column; margin:0 -.3rem .8rem; max-height:60vh; overflow:auto; }
.opt { display:flex; align-items:center; gap:.75rem; padding:.62rem .3rem; cursor:pointer;
       border-radius:8px; font-size:.95rem; }
.opt:hover { background:var(--hover); }
.opt input { accent-color:var(--accent); width:1.1rem; height:1.1rem; margin:0; flex:0 0 auto; }
/* A long list in sections: the heading stays in view while its options scroll. */
.optsec { position:sticky; top:0; z-index:1; background:var(--card); font-size:.72rem; font-weight:700;
          text-transform:uppercase; letter-spacing:.07em; color:var(--dim); padding:.75rem .3rem .3rem; }
.opt.gone { cursor:default; color:var(--dim); }
.opt.gone:hover { background:none; }
.opt small { margin-left:auto; color:var(--dim); font-size:.78rem; }
.sheet { display:flex; flex-direction:column; gap:.4rem; margin:0 0 .9rem; }
.sheet button { justify-content:flex-start; flex-direction:column; align-items:flex-start;
                gap:.1rem; padding:.6rem .8rem; }
.sheet button small { color:var(--dim); font-size:.78rem; font-weight:400; }

/* --- apps and processes ------------------------------------------------ */
.tile { width:2.1rem; height:2.1rem; border-radius:10px; flex:0 0 auto; display:flex;
        align-items:center; justify-content:center; font-weight:700; font-size:.95rem;
        background:var(--line); color:var(--dim); text-transform:lowercase; }
.tile.user { background:color-mix(in srgb, var(--accent) 22%, transparent); color:var(--fg); }
.tile.big { width:3.4rem; height:3.4rem; border-radius:16px; font-size:1.5rem; }
.apphead { display:flex; flex-direction:column; align-items:center; text-align:center;
           gap:.35rem; padding:.4rem 0 1.1rem; }
.apphead .name { font-size:1.2rem; font-weight:700; overflow-wrap:anywhere; }
.apphead .pkg { font-size:.85rem; color:var(--dim); overflow-wrap:anywhere; }
.appbar { display:flex; gap:.5rem; margin:0 0 1.4rem; }
.appbar button { flex:1 1 0; flex-direction:column; gap:.25rem; padding:.7rem .3rem;
                 font-size:.82rem; background:var(--card); box-shadow:var(--shadow);
                 border-color:transparent; }
.chips { display:flex; gap:.4rem; flex-wrap:wrap; margin:0 0 1rem; }
.chips button { border-radius:999px; padding:.3rem .8rem; font-size:.85rem; }
.chips button.on { background:color-mix(in srgb, var(--accent) 25%, transparent);
                   border-color:transparent; font-weight:600; }
.dot.warn { background:var(--warn); }
.dot.idle { background:var(--dim); opacity:.5; }

/* --- search, dialog text, link buttons ---------------------------------- */
.search { display:flex; align-items:center; gap:.6rem; background:var(--card); box-shadow:var(--shadow);
          border-radius:999px; padding:.55rem 1rem; margin:0 0 1.2rem; color:var(--dim); }
.search input { border:none; background:none; padding:0; outline:none; color:var(--fg); font-size:1rem; }
.search input:focus { outline:none; }
.dlg .dtext { margin:0 0 .9rem; font-size:.88rem; color:var(--dim); }
.dlg .dtext ul { margin:.4rem 0 .6rem; padding-left:1.2rem; color:var(--fg); }
.optfilter { margin:0 0 .6rem; }
a.btn { text-decoration:none; color:var(--fg); }
.btn.flat { background:none; border-color:transparent; padding:.42rem .55rem; }
.btn.flat:hover { background:var(--hover); }
main > p.help { font-size:.85rem; color:var(--dim); margin:.2rem .15rem 1rem; }

/* --- keyboard and screen readers ---------------------------------------- */
/* Rows sit inside a card that clips, so their ring is drawn inside them. */
.row.tap:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
button:focus-visible, a:focus-visible, summary:focus-visible,
.sw input:focus-visible + span, input[type=range]:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px; }
.sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
.hdr h1:focus { outline:none; }
/* A search result, found on its page. */
.row.flash, summary.flash, .group > h2.flash { animation:flash 2.6s ease-out; }
@keyframes flash { 0%, 45% { background:color-mix(in srgb, var(--accent) 24%, transparent); }
                   100% { background:transparent; } }
@media (prefers-reduced-motion:reduce) {
  .row.flash, summary.flash, .group > h2.flash { animation:none;
    background:color-mix(in srgb, var(--accent) 16%, transparent); }
}
</style>

<header class="hdr">
  <div class="left"><button class="flat" id="back" hidden></button></div>
  <h1 id="title" tabindex="-1">Settings</h1>
  <div class="right" id="hdracts"></div>
</header>
<main id="view"><div class="skeleton">Loading…</div></main>
<div id="toast" role="status" aria-live="polite"></div>
<div id="scrim" hidden></div>
<script src="/n.js"></script>

<script>
"use strict";

/* ===================================================================== *
 * Icons. Adwaita symbolics, traced to 16px paths so the whole page stays
 * one file - an icon font or an SVG sprite would be another asset to fetch,
 * and this page has to work with no route out.
 * ===================================================================== */
const ICON = {
  chevron: "M6 3l5 5-5 5",
  wifi: "M1.5 5.5a9 9 0 0 1 13 0M4 8.3a5.5 5.5 0 0 1 8 0M8 12h.01",
  bluetooth: "M5 4.5L11 11 8 13.5V2.5L11 5 5 11.5",
  speaker: "M2 6v4h2.5L8 13V3L4.5 6H2zM10.5 6a3 3 0 0 1 0 4M12.5 4a6 6 0 0 1 0 8",
  mic: "M8 1.5a2 2 0 0 1 2 2v4a2 2 0 1 1-4 0v-4a2 2 0 0 1 2-2zM3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5",
  ring: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zm0 3.5a3 3 0 1 1 0 6 3 3 0 0 1 0-6z",
  note: "M6 12.5a2 2 0 1 1-2-2 2 2 0 0 1 2 2V3l8-1.5v9M14 10.5a2 2 0 1 1-2-2 2 2 0 0 1 2 2z",
  button: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM8 5v3.5",
  info: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM8 7v4.5M8 4.6v.01",
  sun: "M8 4.5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7zM8 .8v1.6M8 13.6v1.6M2.9 2.9l1.1 1.1M12 12l1.1 1.1M.8 8h1.6M13.6 8h1.6M2.9 13.1L4 12M12 4l1.1-1.1",
  lock: "M4 7V5a4 4 0 0 1 8 0v2M3 7h10v7H3z",
  refresh: "M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 1.5V5H10",
  check: "M3 8.5l3.5 3.5L13 4.5",
  plus: "M8 3v10M3 8h10",
  // A USB plug: the trident body with the cable running down from it.
  plug: "M8 15v-4M8 11L4.5 7.5V4M8 11l3.5-3.5V4M4.5 4V1.5M11.5 4V1.5M8 11V6",
  user: "M8 1.8a3.1 3.1 0 1 1 0 6.2 3.1 3.1 0 0 1 0-6.2zM2.2 14.5a5.8 5.8 0 0 1 11.6 0",
  link: "M6.5 9.5l3-3M6 4.5l1.2-1.2a3 3 0 0 1 4.3 4.3L10.3 8.8M10 11.5l-1.2 1.2a3 3 0 0 1-4.3-4.3L5.7 7.2",
  trash: "M2.5 4h11M6 4V2.5h4V4M4 4l.7 10h6.6L12 4",
  play: "M5 3l7 5-7 5z",
  phone: "M4.5 1.5h7v13h-7zM7 12.8h2",
  head: "M3 10V8a5 5 0 0 1 10 0v2M2 10h2.5v4H3.5A1.5 1.5 0 0 1 2 12.5zM14 10h-2.5v4h1A1.5 1.5 0 0 0 14 12.5z",
  tag: "M2 2.5h5.5l6.5 6.5-5 5L2.5 7.5zM5.3 5.3h.01",
  home: "M2 7.5L8 2.5l6 5M3.5 6.3V13.5h9V6.3",
  download: "M8 2v8.5M4.5 7L8 10.5 11.5 7M2.5 13.5h11",
  gear: "M8 5.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM8 1.5v1.8M8 12.7v1.8M1.5 8h1.8M12.7 8h1.8M3.4 3.4l1.3 1.3M11.3 11.3l1.3 1.3M3.4 12.6l1.3-1.3M11.3 4.7l1.3-1.3",
  stop: "M4 4h8v8H4z",
  search: "M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zM10.3 10.3l3.2 3.2",
  disk: "M2.5 4.5h11v7h-11zM2.5 9h11M11 10.25h.01",
  clock: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM8 4.5V8l2.5 1.5",
  globe: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM1.5 8h13M8 1.5c2 2 2.8 4.2 2.8 6.5S10 12.5 8 14.5C6 12.5 5.2 10.3 5.2 8S6 3.5 8 1.5z",
  power: "M8 1.5v6M4.5 3.8a5.5 5.5 0 1 0 7 0",
  chart: "M2.5 13.5h11M4.5 11V7.5M8 11V3.5M11.5 11V6",
};
function icon(name, size) {
  size = size || 16;
  const fill = name === "ring" || name === "play";
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 16 16" fill="none" ' +
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true"><path d="' + ICON[name] + '"' +
    (name === "play" ? ' fill="currentColor"' : "") + '/></svg>';
}
/* Signal strength as four rising bars, the way every other Wi-Fi picker draws
   it. Raw dBm means nothing to most people; bars are directly comparable with
   what the same network shows on a phone standing in the same place. */
function bars(level) {
  let s = '<svg width="18" height="16" viewBox="0 0 18 16" aria-hidden="true">';
  for (let i = 0; i < 4; i++) {
    s += '<rect x="' + (i * 4.5) + '" y="' + (13 - i * 3.4) + '" width="3" height="' +
         (2.6 + i * 3.4) + '" rx="1" fill="currentColor" opacity="' +
         (i < level ? "1" : ".22") + '"/>';
  }
  return s + "</svg>";
}

/* ===================================================================== *
 * Primitives
 * ===================================================================== */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (html) => { const t = document.createElement("template");
                       t.innerHTML = html.trim(); return t.content.firstElementChild; };

let toastTimer = null;
function toast(msg, bad) {
  const t = $("toast");
  // An error is announced at once; anything else waits its turn.
  t.setAttribute("aria-live", bad ? "assertive" : "polite");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, bad ? 5000 : 3000);
}

/* Box a request's fields for this device's key, with a one-time challenge -
   for anything carrying a password. The page is plain HTTP on the LAN, and the
   account's password is also its SSH password. See Sealer in the server. */
async function seal(obj) {
  if (!window.nacl) return obj;
  const k = await (await fetch("/seal", { cache: "no-store" })).json();
  if (!k.key) return obj;
  const u8 = (b) => Uint8Array.from(atob(b), (c) => c.charCodeAt(0));
  const b64 = (u) => btoa(String.fromCharCode.apply(null, u));
  const msg = new TextEncoder().encode(JSON.stringify(Object.assign({}, obj, { _c: k.challenge })));
  const eph = nacl.box.keyPair();
  const nonce = nacl.randomBytes(nacl.box.nonceLength);
  return { sealed: b64(eph.publicKey) + "." + b64(nonce) + "." +
                   b64(nacl.box(msg, nonce, u8(k.key), eph.secretKey)) };
}

async function api(path, body, how) {
  if (body && how && how.seal) body = await seal(body);
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* a 500 with no body */ }
  if (!r.ok) throw new Error(data.error || ("Request failed (" + r.status + ")"));
  if (data && data._release) RELEASE = data._release;
  return data;
}

/* A boxed list. Rows are built as elements rather than one HTML string so a
   control's handler can close over its own row. */
function boxed(rows) {
  const box = el('<div class="boxed"></div>');
  rows.filter(Boolean).forEach((r) => box.appendChild(r));
  return box;
}
function group(title, content, help) {
  const g = el('<div class="group"></div>');
  if (title) g.appendChild(el("<h2>" + esc(title) + "</h2>"));
  (Array.isArray(content) ? content : [content]).filter(Boolean)
    .forEach((c) => g.appendChild(c));
  if (help) g.appendChild(el('<p class="help">' + help + "</p>"));
  return g;
}

/* One list row. `opts.end` is an element placed at the trailing edge; `opts.go`
   makes the whole row a navigation target and adds the chevron. */
// The microphone chain can end up running something other than what is
// selected: a stock generation whose files are absent at start falls back to
// pmOS rather than leaving the device deaf. Null when the two agree, and also
// when the branch has not published yet - an unknown is not an agreement.
// The capture restart is detached, so its result arrives after the page has
// already moved on. Anything other than a plain success is worth saying.
function micRestartNote(o) {
  if (!o || o.ok === true && !o.rolled_back) return null;
  if (o.refused === "busy") return "A restart was already running, so this change has not been applied yet.";
  if (o.refused === "call") return "Not applied: a call was in progress. Try again once the call ends.";
  if (o.refused === "alarm") return "Not applied: an alarm or timer was ringing.";
  if (o.rolled_back && o.ok) return "That setting did not start, so the microphone was put back to " + esc(String(o.effective)) + ".";
  if (o.ok === false) return "The microphone did not come back within " + esc(String(o.seconds || "?")) + "s, and neither did the previous setting. Check the logs.";
  return null;
}

function micFallback(status) {
  if (!status || !status.effective || status.effective === status.requested) return null;
  return status;
}

function micFallbackText(status) {
  return esc(status.requested) + " is selected, but the microphone is running "
    + esc(status.effective) + "."
    + (status.reason && status.reason !== "ok" ? " " + esc(status.reason) + "." : "");
}

function row(opts) {
  const r = el('<div class="row"></div>');
  r.dataset.key = opts.title || "";
  if (opts.icon) r.appendChild(el('<div class="icon">' + icon(opts.icon, 18) + "</div>"));
  if (opts.lead) r.appendChild(opts.lead);
  const txt = el('<div class="txt"></div>');
  txt.appendChild(el('<div class="title">' + esc(opts.title) +
    (opts.badge ? ' <span class="badge ' + (opts.badgeKind || "") + '">' +
                  esc(opts.badge) + "</span>" : "") + "</div>"));
  if (opts.sub) txt.appendChild(el('<div class="sub">' + opts.sub + "</div>"));
  r.appendChild(txt);
  if (opts.value != null) {
    r.appendChild(el('<div class="val">' + esc(opts.value) + "</div>"));
    // A long value gets a line of its own at phone width; a short one - a
    // size, a percentage - stays on the right, where it is read at a glance.
    if (opts.sub && String(opts.value).length > 14) r.classList.add("hasval");
  }
  if (opts.end) {
    const end = el('<div class="end"></div>');
    const items = (Array.isArray(opts.end) ? opts.end : [opts.end]).filter(Boolean);
    items.forEach((e) => end.appendChild(e));
    r.appendChild(end);
    // A choice is shown the way Android shows one: its current value under
    // the title, and a list of options when the row is tapped. The <select>
    // stays in the row, hidden, as the value's store - so every page that
    // reads it or listens for its change event works unchanged. A native
    // dropdown squeezed into a phone-width row was what looked off.
    const sel = items.find((e) => e.tagName === "SELECT");
    if (sel) {
      sel.hidden = true;
      // With the choice alone in the row, the whole row is the control. With
      // other controls beside it - a Play button, colour wells - a row that is
      // a button would hide them from screen readers, so the current value
      // becomes a button of its own and the row stays a plain container.
      const solo = items.length === 1;
      const cur = el(solo ? '<div class="cur"></div>'
                          : '<button type="button" class="cur curbtn" aria-haspopup="dialog"></button>');
      const sync = () => {
        const o = sel.options[sel.selectedIndex];
        cur.textContent = o ? o.textContent : "";
        r.classList.toggle("off", sel.disabled);
        if (!solo) {
          cur.disabled = sel.disabled;
          cur.setAttribute("aria-label", opts.title + ": " + cur.textContent);
        } else if (r.hasAttribute("role")) {
          // Not a Tab stop, and said to be unavailable, when it cannot be used.
          r.tabIndex = sel.disabled ? -1 : 0;
          r.setAttribute("aria-disabled", String(sel.disabled));
        }
      };
      r._sync = sync;
      sync();
      sel.addEventListener("change", sync);
      txt.insertBefore(cur, txt.children[1] || null);
      const choose = async () => {
        if (sel.disabled) return;
        const v = await pick({
          title: opts.title, value: sel.value, preview: opts.preview, filter: opts.filter,
          onclose: opts.previewEnd,
          options: [...sel.options].map((o) => ({
            value: o.value, label: o.textContent, disabled: o.disabled, hint: o.dataset.hint || "",
            section: o.parentElement.tagName === "OPTGROUP" ? o.parentElement.label : null })),
        });
        if (v != null && v !== sel.value) {
          sel.value = v;
          sel.dispatchEvent(new Event("change"));
        }
      };
      r.classList.add("tap", "choice");
      if (!solo) {
        r.classList.add("multi");
        cur.addEventListener("click", (ev) => { ev.stopPropagation(); choose(); });
      }
      r.addEventListener("click", (ev) => {
        if (ev.target.closest("button, input, a, label")) return;
        choose();
      });
    }
  }
  if (opts.go || opts.onclick) {
    r.classList.add("tap");
    if (opts.go) r.appendChild(el('<div class="icon">' + icon("chevron", 14) + "</div>"));
    r.addEventListener("click", (ev) => {
      // A control inside the row owns its own clicks; a switch would otherwise
      // fire twice and land back where it started.
      if (ev.target.closest("button, input, select, a, label")) return;
      if (opts.go) location.hash = opts.go; else opts.onclick(ev);
    });
  }
  // A row that does something is a button to the keyboard and to a screen
  // reader: it takes focus, and Enter or Space works it. It used to be a
  // <div> that only a pointer could use.
  if (r.classList.contains("tap") && !r.classList.contains("multi")) {
    r.tabIndex = 0;
    r.setAttribute("role", opts.go ? "link" : "button");
    if (r.classList.contains("choice")) r.setAttribute("aria-haspopup", "dialog");
    r.addEventListener("keydown", (ev) => {
      if (ev.target !== r || (ev.key !== "Enter" && ev.key !== " ")) return;
      ev.preventDefault();
      r.click();
    });
    if (r._sync) r._sync();
  }
  // Controls without a visible label of their own are named by their row.
  r.querySelectorAll(".end input, .end select, .end button").forEach((c) => {
    if (!c.getAttribute("aria-label") && !(c.tagName === "BUTTON" && c.textContent.trim()))
      c.setAttribute("aria-label", opts.title);
  });
  return r;
}

function switchEl(checked, onchange, disabled, label) {
  const s = el('<label class="sw"><input type="checkbox" role="switch"' + (checked ? " checked" : "") +
               (disabled ? " disabled" : "") + "><span></span></label>");
  const input = s.querySelector("input");
  if (label) input.setAttribute("aria-label", label);
  input.addEventListener("change", (e) => onchange(e.target.checked, e.target));
  return s;
}

/* Options are strings, or {value, label} with optionally a `section` (runs of
   options sharing one become an <optgroup>, and headings in the list), a
   `hint` (more words the list's filter matches) and `disabled`. */
function selectEl(options, value, onchange, disabled = false) {
  const s = el("<select></select>");
  s.disabled = disabled;
  let into = s, section = null;
  options.forEach((o) => {
    const v = typeof o === "string" ? o : o.value;
    const l = typeof o === "string" ? o : o.label;
    const sec = (typeof o === "string" ? null : o.section) || null;
    if (sec !== section) {
      section = sec;
      into = sec ? s.appendChild(el('<optgroup label="' + esc(sec) + '"></optgroup>')) : s;
    }
    const opt = el('<option value="' + esc(v) + '">' + esc(l) + "</option>");
    if (typeof o !== "string" && o.hint) opt.dataset.hint = o.hint;
    if (typeof o !== "string" && o.disabled) opt.disabled = true;
    if (v === value) opt.selected = true;
    into.appendChild(opt);
  });
  s.addEventListener("change", () => onchange(s.value, s));
  return s;
}

/* What an icon-only button does, for a screen reader and a tooltip. */
const ICON_LABEL = { trash: "Remove", play: "Play", refresh: "Refresh", download: "Download",
                     stop: "Stop", plus: "Add", chevron: "Open" };
function btn(label, opts) {
  opts = opts || {};
  const b = el("<button" + (opts.cls ? ' class="' + opts.cls + '"' : "") +
               (opts.disabled ? " disabled" : "") + ">" +
               (opts.icon ? icon(opts.icon, 15) : "") +
               (label ? "<span>" + esc(label) + "</span>" : "") + "</button>");
  if (!label && opts.icon) {
    const name = opts.aria || ICON_LABEL[opts.icon] || opts.icon;
    b.setAttribute("aria-label", name);
    b.title = name;
  }
  if (opts.onclick) b.addEventListener("click", (e) => { e.stopPropagation(); opts.onclick(b, e); });
  return b;
}

/* A slider row. The value is committed on `change` (pointer up) rather than on
   every `input`: each commit here is a write to the persist partition or a
   command to the DSP, and dragging would issue fifty of them. */
function sliderRow(opts) {
  const out = el('<output class="num">' + esc(opts.format(opts.value)) + "</output>");
  const rng = el('<input type="range" min="' + opts.min + '" max="' + opts.max +
                 '" step="' + (opts.step || 1) + '" value="' + opts.value + '" aria-label="' +
                 esc(opts.title) + '">');
  rng.setAttribute("aria-valuetext", opts.format(opts.value));
  rng.addEventListener("input", () => rng.setAttribute("aria-valuetext", opts.format(Number(rng.value))));
  rng.addEventListener("input", () => { out.textContent = opts.format(Number(rng.value)); });
  rng.addEventListener("change", () => opts.onchange(Number(rng.value)));
  const wrap = el('<div class="slider"></div>');
  wrap.appendChild(rng);
  wrap.appendChild(out);
  const r = el('<div class="row stack"></div>');
  r.dataset.key = opts.title || "";
  r.appendChild(el('<div class="txt"><div class="title">' + esc(opts.title) + "</div>" +
    (opts.sub ? '<div class="sub">' + opts.sub + "</div>" : "") + "</div>"));
  r.appendChild(wrap);
  r.rangeEl = rng;
  return r;
}

function statusPage(iconName, title, text) {
  return el('<div class="status">' + icon(iconName, 44) +
            '<div class="t">' + esc(title) + "</div>" +
            '<div class="d">' + text + "</div></div>");
}

/* Every dialog goes through here: announced as a dialog, the keyboard kept
   inside it while it is open, Escape and a tap outside close it, and focus
   goes back to whatever opened it - or to the page title, if a re-render
   took that away. Returns the function that closes it. */
function openModal(d, dismiss) {
  const scrim = $("scrim");
  const before = document.activeElement;
  d.setAttribute("role", "dialog");
  d.setAttribute("aria-modal", "true");
  const h = d.querySelector("h3");
  if (h) { h.id = "dlg-title"; d.setAttribute("aria-labelledby", "dlg-title"); }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); dismiss(); return; }
    if (e.key !== "Tab") return;
    const f = [...d.querySelectorAll("button, input, select, a[href]")].filter((x) =>
      !x.disabled && x.offsetParent !== null &&
      (x.type !== "radio" || x.checked || !d.querySelector('input[name="' + x.name + '"]:checked')));
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || !d.contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !d.contains(document.activeElement))) {
      e.preventDefault(); first.focus();
    }
  }
  const onScrim = (e) => { if (e.target === scrim) dismiss(); };
  document.addEventListener("keydown", onKey);
  scrim.addEventListener("click", onScrim);
  scrim.innerHTML = "";
  scrim.appendChild(d);
  scrim.hidden = false;
  return function close() {
    document.removeEventListener("keydown", onKey);
    scrim.removeEventListener("click", onScrim);
    scrim.hidden = true;
    scrim.innerHTML = "";
    const back = before && before.isConnected ? before : $("title");
    try { back.focus({ preventScroll: true }); } catch (e) { /* nothing to return to */ }
  };
}

/* A modal. Returns a promise for the field values, or null if dismissed. The
   caller's `onsubmit` may throw to keep the dialog open with an error, which is
   what "wrong Wi-Fi password" needs. */
function dialog(opts) {
  return new Promise((resolve) => {
    const d = el('<div class="dlg"></div>');
    d.appendChild(el("<h3>" + esc(opts.title) + "</h3>"));
    if (opts.text) d.appendChild(el('<div class="dtext">' + opts.text + "</div>"));
    const errEl = el('<p class="err" role="alert"></p>');
    d.appendChild(errEl);
    const fields = el('<div class="fields"></div>');
    const inputs = {};
    (opts.fields || []).forEach((f) => {
      const wrap = el('<label class="field">' + esc(f.label) + "</label>");
      const i = el('<input type="' + (f.type || "text") + '"' +
        (f.value ? ' value="' + esc(f.value) + '"' : "") +
        (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : "") +
        (f.autocomplete ? ' autocomplete="' + f.autocomplete + '"' : "") +
        ' autocapitalize="none" autocorrect="off" spellcheck="false">');
      inputs[f.name] = i;
      wrap.appendChild(i);
      fields.appendChild(wrap);
    });
    if (opts.fields && opts.fields.length) d.appendChild(fields);
    const acts = el('<div class="acts"></div>');
    const cancel = btn(opts.cancelLabel || "Cancel", { cls: "flat" });
    const ok = btn(opts.okLabel || "OK",
                   { cls: opts.destructive ? "destructive solid" : "suggested" });
    acts.appendChild(cancel);
    acts.appendChild(ok);
    d.appendChild(acts);

    let shut = null;
    // While the request is in flight it cannot be dismissed: the device may
    // already be acting on it (a reset, an install), and "cancelled" would be
    // untrue. And it closes exactly once.
    let pending = false, finished = false;
    function close(result) {
      if (finished) return;
      finished = true;
      shut();
      resolve(result);
    }
    const dismiss = () => { if (!pending) close(null); };
    // Enter in a field submits, as in any form.
    d.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.tagName === "INPUT" && !ok.disabled) { e.preventDefault(); submit(); }
    });
    async function submit() {
      const values = {};
      Object.keys(inputs).forEach((k) => { values[k] = inputs[k].value; });
      if (!opts.onsubmit) return close(values);
      ok.disabled = true;
      cancel.disabled = true;
      pending = true;
      errEl.textContent = "";
      try {
        await opts.onsubmit(values);
        pending = false;
        close(values);
      } catch (err) {
        pending = false;
        errEl.textContent = err.message;
        ok.disabled = false;
        cancel.disabled = false;
        const first = Object.values(inputs)[0];
        if (first) first.focus();
      }
    }
    cancel.addEventListener("click", dismiss);
    ok.addEventListener("click", submit);
    shut = openModal(d, dismiss);
    const first = Object.values(inputs)[0];
    (first || ok).focus();
  });
}

/* A list of options with radio buttons, Android's list preference. Resolves
   with the chosen value, or null. Without a preview, tapping an option chooses
   it; with one, tapping previews it (a sound, a ring pattern) and OK chooses.

   An option may name a `section`: a run of options sharing one is listed under
   it as a heading, a group to a screen reader. `hint` is more words the filter
   matches than the label shows - the other names of a copy listed once. And a
   `disabled` option is a saved choice the list no longer offers: shown, so the
   list says what is set, but it cannot be chosen again. A long list gets a
   filter box whether or not the caller asked for one. `onclose` runs however
   the list closes. */
const PICK_FILTER_AT = 15;
function pick(opts) {
  return new Promise((resolve) => {
    const d = el('<div class="dlg"></div>');
    d.appendChild(el("<h3>" + esc(opts.title) + "</h3>"));
    if (opts.text) d.appendChild(el("<p>" + opts.text + "</p>"));
    const list = el('<div class="opts" role="radiogroup" aria-label="' + esc(opts.title) + '"></div>');
    const items = [];      // {item, radio, value, hay, box} per option, in order
    const boxes = [];      // the sections, each hidden when the filter empties it
    if (opts.filter || opts.options.length > PICK_FILTER_AT) {
      const f = el('<input type="search" class="optfilter" placeholder="Filter" aria-label="Filter">');
      const visible = () => items.filter((x) => !x.item.hidden && !x.radio.disabled);
      f.addEventListener("input", () => {
        const q = f.value.trim().toLowerCase();
        items.forEach((x) => { x.item.hidden = !!q && !x.hay.includes(q); });
        boxes.forEach((b) => { b.hidden = !!q && !items.some((x) => x.box === b && !x.item.hidden); });
        // A checked radio that is filtered out would be the group's only
        // Tab stop, and the matches would be unreachable by keyboard.
        const on = list.querySelector("input:checked");
        if (on && on.closest(".opt").hidden) on.checked = false;
      });
      // Down goes into the matches; Enter takes the first one. With a
      // preview, Down also selects and plays the first match, as an arrow
      // does inside the list - focus alone left the old choice to OK.
      f.addEventListener("keydown", (e) => {
        const first = visible()[0];
        if (!first) return;
        if (e.key === "ArrowDown") {
          e.preventDefault();
          first.radio.focus();
          if (opts.preview && !first.radio.checked) {
            first.radio.checked = true;
            first.radio.dispatchEvent(new Event("change"));
          }
        } else if (e.key === "Enter") {
          e.preventDefault();
          close(first.value);
        }
      });
      d.appendChild(f);
      setTimeout(() => f.focus(), 0);
    }
    let chosen = opts.value;
    let arrowing = false;
    let box = list, section = null;
    const name = "pick" + Math.random().toString(36).slice(2);
    opts.options.forEach((o) => {
      const sec = o.section || null;
      if (sec !== section) {
        section = sec;
        box = list;
        if (sec) {
          box = el('<div role="group" aria-label="' + esc(sec) + '"></div>');
          box.appendChild(el('<div class="optsec" aria-hidden="true">' + esc(sec) + "</div>"));
          list.appendChild(box);
          boxes.push(box);
        }
      }
      // A disabled option is never checked: a checked radio that cannot take
      // focus would leave the list with no Tab stop at all.
      const item = el('<label class="opt' + (o.disabled ? " gone" : "") + '"><input type="radio" name="' + name + '"' +
                      (o.value === opts.value && !o.disabled ? " checked" : "") + (o.disabled ? " disabled" : "") +
                      "><span>" + esc(o.label) + "</span>" + (o.disabled ? "<small>Current</small>" : "") + "</label>");
      const radio = item.querySelector("input");
      items.push({ item, radio, value: o.value, box: box === list ? null : box,
                   hay: (o.label + " " + (o.hint || "")).toLowerCase() });
      radio.addEventListener("change", () => {
        chosen = o.value;
        if (opts.preview) {
          try { opts.preview(o.value); } catch (e) { /* a preview is best effort */ }
        } else if (!arrowing) {
          close(o.value);
        }
        arrowing = false;
      });
      // Arrow keys move through the list without choosing - a radio group
      // checks what it moves to, and closing on that made the list
      // impossible to browse from the keyboard. Enter or Space chooses.
      radio.addEventListener("keydown", (e) => {
        if (e.key.startsWith("Arrow")) {
          arrowing = true;
        } else if ((e.key === "Enter" || e.key === " ") && !opts.preview) {
          e.preventDefault(); close(o.value);
        } else if (e.key === "Enter" && opts.preview) {
          // The option under the focus, which after the filter's Down or a
          // Tab is not always the last one previewed.
          e.preventDefault(); close(o.value);
        }
      });
      box.appendChild(item);
    });
    d.appendChild(list);
    const acts = el('<div class="acts"></div>');
    const cancel = btn("Cancel", { cls: "flat" });
    acts.appendChild(cancel);
    if (opts.preview) {
      const ok = btn("OK", { cls: "suggested" });
      ok.addEventListener("click", () => close(chosen));
      acts.appendChild(ok);
    }
    d.appendChild(acts);
    let shut = null;
    function close(result) {
      // A preview still waiting to start is for a list that is gone.
      if (opts.onclose) { try { opts.onclose(); } catch (e) { /* best effort */ } }
      shut();
      resolve(result);
    }
    cancel.addEventListener("click", () => close(null));
    shut = openModal(d, () => close(null));
    // The current choice in view - a disabled one too, which is the one
    // worth seeing - and the first one that can be chosen under the focus.
    const cur = items.find((x) => x.value === opts.value);
    const on = list.querySelector("input:checked") || list.querySelector("input:not(:disabled)");
    if (cur || on) (cur ? cur.item : on.closest(".opt")).scrollIntoView({ block: "center" });
    if (on && !(opts.filter || opts.options.length > PICK_FILTER_AT)) on.focus();
  });
}

/* A short list of actions for one thing - a service - with an explanation
   above them. Resolves with the chosen action's value, or null. */
function actionSheet(title, text, actions) {
  return new Promise((resolve) => {
    const d = el('<div class="dlg"></div>');
    d.appendChild(el("<h3>" + esc(title) + "</h3>"));
    if (text) d.appendChild(el("<p>" + text + "</p>"));
    const list = el('<div class="sheet"></div>');
    let shut = null;
    function close(result) {
      shut();
      resolve(result);
    }
    actions.filter(Boolean).forEach((a) => {
      const b = btn(a.label, { cls: a.destructive ? "destructive" : "" });
      if (a.sub) b.appendChild(el('<small>' + esc(a.sub) + "</small>"));
      b.addEventListener("click", () => close(a.value));
      list.appendChild(b);
    });
    d.appendChild(list);
    const acts = el('<div class="acts"></div>');
    const cancel = btn("Close", { cls: "flat" });
    cancel.addEventListener("click", () => close(null));
    acts.appendChild(cancel);
    d.appendChild(acts);
    shut = openModal(d, () => close(null));
    (list.querySelector("button") || cancel).focus();
  });
}

/* The account's password, asked again before something that cannot be taken
   back - as a phone asks for its PIN. `send(password)` makes the request and
   may throw to keep the dialog open, as a wrong password does. Resolves true
   once it succeeded. */
function withPassword(title, text, okLabel, send, destructive) {
  return dialog({
    title, text, okLabel, destructive,
    fields: [{ name: "password", label: "Your password", type: "password", autocomplete: "current-password" }],
    onsubmit: async (v) => {
      if (!v.password) throw new Error("Enter your password.");
      await send(v.password);
    },
  }).then((r) => r !== null);
}

function confirmDialog(title, text, okLabel, destructive) {
  return dialog({ title, text, okLabel: okLabel || "Confirm", destructive })
    .then((r) => r !== null);
}

/* A collapsible section. Advanced settings and long lists stay out of the way
   until someone asks for them, and the heading looks like every other group's
   so a folded section does not read as a different kind of thing. */
function fold(title, content, help, open) {
  const d = el('<details class="fold"' + (open ? " open" : "") + "><summary>" +
               esc(title) + "</summary></details>");
  // Known by its title without a trailing count ("Animation files · 234"),
  // so removing one item does not close it on the refresh.
  d.dataset.key = String(title).replace(/ · \d+$/, "");
  (Array.isArray(content) ? content : [content]).filter(Boolean)
    .forEach((c) => d.appendChild(c));
  if (help) d.appendChild(el('<p class="help">' + help + "</p>"));
  return d;
}

/* A row that opens a page elsewhere - source code, release notes - in a new
   tab, so the settings page stays where it was. */
function linkRow(opts) {
  return row(Object.assign({}, opts, {
    onclick: () => window.open(opts.href, "_blank", "noopener"),
  }));
}

/* "3 minutes ago". The device's clock is the reference: the timestamps it
   compares are its own. */
function ago(epoch, now) {
  const s = Math.max(0, Math.round((now || Date.now() / 1000) - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + (s < 120 ? " minute ago" : " minutes ago");
  if (s < 86400) return Math.round(s / 3600) + (s < 7200 ? " hour ago" : " hours ago");
  return Math.round(s / 86400) + (s < 172800 ? " day ago" : " days ago");
}

/* The public release this Echo runs, {name: "v1.0", build: "6-r295"}, as
   every API answer carries it (api() keeps the latest). Empty on a build from
   before v1.0. */
let RELEASE = { name: "", build: "" };

/* How a build is named everywhere a person sees it: the build this Echo runs
   by its release ("6-r295" -> "v1.0"), any other by its number ("6-r296" ->
   "r296") - an update's release name is not known until it is installed. */
const rel = (v) => {
  if (v && RELEASE.name && v === RELEASE.build) return RELEASE.name;
  const m = /-r(\d+)$/.exec(v || ""); return m ? "r" + m[1] : (v || "");
};

/* "A", "A and B", "A, B and C". */
function andList(items) {
  const a = (items || []).filter(Boolean);
  return a.length < 2 ? (a[0] || "") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1];
}

/* A package job that is not an app's, named for the Apps page, which waits for
   it rather than offering an Install the server would refuse. */
const JOB_WORDS = { check: "Checking for updates", update: "Updating the device software",
                    sysupdate: "Updating system packages", boot: "Installing the boot image",
                    bootrollback: "Putting back the previous boot image" };

/* "in about 20 minutes", for a time still to come. */
function inAbout(s) {
  if (s < 90) return "within a minute or so";
  if (s < 3300) return "in about " + Math.round(s / 60) + " minutes";
  const h = Math.round(s / 3600);
  return h === 1 ? "in about an hour" : "in about " + h + " hours";
}

/* The apps chosen at setup that are not installed yet: the badge, and why and
   what happens next, in the same words on every page that mentions them. The
   next try is the settings server's own (see apps_retry_loop).

   An install running now may cover only some of them (p.installing) - one
   app's Install on the Apps page, or a setup re-entered for one app. The
   state and the words then describe the others (p.rest), and say first what
   is being installed: "Installing now" is never said of an app nobody is
   installing. */
function pendingBadge(p) {
  if (p.state === "installing") return ["Installing", "accent"];
  return [p.state === "failed" ? "Failed" : "Pending", "warn"];
}
function pendingWhy(p, now, brief) {
  if (p.state === "installing") {
    return (p.message ? esc(p.message) + ", then installing." : "Installing now.") +
           " This can take a few minutes.";
  }
  const inst = p.installing && (p.installing.labels || []).length ? p.installing : null;
  const rest = inst && p.rest ? p.rest.labels : (p.labels || []);
  const one = rest.length === 1;
  const lead = inst ? "Installing " + esc(andList(inst.labels)) + " now. " : "";
  // A failure is said of the apps it was about - all of them, unless an
  // install of only some failed (p.failed), or others are installing now.
  const who = p.failed && (p.failed.labels || []).length ? p.failed.labels : inst ? rest : null;
  let why;
  if (p.state === "failed") {
    why = (who ? esc(andList(who)) + ": " : "") + esc(p.message || "The last attempt did not complete.") +
          (p.tried_at && !brief ? " Last tried " + ago(p.tried_at, now) + "." : "");
  } else if (p.state === "busy") {
    why = inst ? esc(andList(rest)) + (one ? " waits" : " wait") + " for that to finish."
               : "Another package job is running.";
  } else if (p.reason === "busy" && p.message) {
    why = esc(p.message);        // another apk had the database: not a failure
  } else if (p.waiting === "clock") {
    // Nothing is tried before the clock is set, so after a restart on a
    // network with no internet no attempt ever says why: this does.
    why = (one ? "It installs" : "They install") + " once this Echo's clock is set: the package feed's " +
          "security certificate cannot be checked before that. The clock is set from the internet, so if " +
          "this lasts, check that the Wi-Fi network this Echo is on is connected to the internet.";
  } else {
    why = (one ? "It installs" : "They install") + " in the background once " +
          (one ? "it" : "they") + " can be downloaded.";
  }
  // The schedule's time, and what else the try waits for, together: the try
  // needs both. The retry looks once a minute, so a time under a minute and a
  // half is "as soon as" the other thing - and only then is it said that way.
  const soon = p.retry_in == null || p.retry_in < 90;
  const gate = p.waiting === "clock" ? "the clock is set"
    : p.waiting !== "job" ? ""
    : inst ? "that has finished"
    : p.state === "busy" ? "it has finished" : "the package job running now has finished";
  // "... wait for that to finish" has already said it.
  const next = inst && p.state === "busy" && soon ? ""
    : gate ? (soon ? "Next try " + (p.waiting === "clock" ? "as soon as " : "once ") + gate + "."
                   : "Next try " + inAbout(p.retry_in) + ", and not before " + gate + ".")
    : p.retry_in == null ? "" : "Next try " + inAbout(p.retry_in) + ".";
  return lead + why + (next ? " " + next : "");
}

/* The three states an app's service can be in, as one control. The backend
   has two flags - enabled at boot, and paused until the next restart - and
   showing both as separate controls asked people to work out what "enabled
   but paused" means. */
const APP_STATES = ["On", "Paused until restart", "Off"];
function appState(svc) {
  if (!svc.enabled) return "Off";
  return svc.paused ? "Paused until restart" : "On";
}
function setAppState(id, svc, want) {
  if (want === appState(svc)) return Promise.resolve(null);
  const state = { "On": "on", "Paused until restart": "paused", "Off": "off" }[want];
  return api("/api/services", { id, action: "state", state });
}

/* ===================================================================== *
 * Formatting
 * ===================================================================== */
function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
}
function duration(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60);
  if (d) return d + (d === 1 ? " day, " : " days, ") + h + "h " + m + "m";
  if (h) return h + "h " + m + "m";
  return m + "m";
}
const db = (v) => (v > 0 ? "+" : "") + v.toFixed(1) + " dB";

/* ===================================================================== *
 * Router
 * ===================================================================== */
const PAGES = {};        // route -> async () => {title, body, actions?}
let RENDERING = 0;
let LAST_ROUTE = null;   // a render of the same route is a refresh, which keeps its place
let NAVIGATED = false;   // set by a route change, so focus can move to the new page
let FIND = null;         // a search result's label, to open and highlight on its page
let PAGE_TIMERS = [];    // a page's own polls, all cleared whenever the route changes

/* Re-render when something changed OUTSIDE the page.
 *
 * Bluetooth state moves on its own - a speaker is switched off, a phone walks
 * out of range - and a settings page that only updates when you press a button
 * will confidently show a device that left ten minutes ago.
 *
 * Two guards, because a poll that fights the user is worse than a stale page:
 * nothing is re-rendered while a dialog is open or while any control has focus,
 * and only when the signature actually differs - so an idle page is quiet and a
 * page being used is left alone.
 */
function pollPage(fetcher, signature, everyMs) {
  let last = null;
  // The route this poll belongs to. render() clears PAGE_TIMER before awaiting
  // the next page, but a slow page that loses the navigation race still gets to
  // run its pollPage afterwards - so the interval also checks for itself, and
  // stops when it finds it is watching a page nobody is looking at.
  const owner = location.hash;
  const id = setInterval(async () => {
    if (location.hash !== owner) { clearInterval(id); return; }
    if (!$("scrim").hidden) return;
    // Held back only while something is being typed or dragged, or while a
    // keyboard user sits on a control. A mouse click leaves focus on a switch
    // or a section heading, and holding back for that froze pages - a job
    // that never finished, a pairing that never appeared - since a refresh
    // restores focus and open sections itself.
    const active = document.activeElement;
    if (active && active.closest("main") &&
        (active.matches('select, textarea, input:not([type=checkbox]):not([type=radio]):not([type=color]):not([type=file])') ||
         (active.matches("button, summary, input, .row.tap, [tabindex]") && active.matches(":focus-visible")))) return;
    let data;
    try { data = await fetcher(); } catch (e) { return; }
    const sig = signature(data);
    if (last !== null && sig !== last) render();
    last = sig;
  }, everyMs);
  PAGE_TIMERS.push(id);
}

/* Live values WITHOUT a re-render.
 *
 * pollPage() rebuilds the page when its signature changes, which is right when
 * the content changed underneath the user. It is wrong for a value that moves
 * while they are reading it: the ring's state changes every time an animation
 * starts, and rebuilding a page full of dropdowns under someone's cursor to
 * update one line is not a fair trade. This updates nodes in place instead,
 * and takes the same ownership precautions - a poll belongs to the route that
 * started it, and PAGE_TIMER is cleared by render() on navigation.
 */
function pollInto(fetcher, apply, everyMs) {
  const owner = location.hash;
  let id = null;
  const tick = async () => {
    if (location.hash !== owner) { clearInterval(id); return; }
    let data;
    try { data = await fetcher(); } catch (e) { return; }
    try { apply(data); } catch (e) { /* a stale node after navigation */ }
  };
  id = setInterval(tick, everyMs);
  PAGE_TIMERS.push(id);
  tick();
}

async function render() {
  const route = location.hash.replace(/^#\/?/, "") || "";
  const slash = route.indexOf("/");
  const param = slash > 0 ? route.slice(slash + 1) : "";
  const page = PAGES[route] || (slash > 0 && PAGES[route.slice(0, slash) + "/*"]) || PAGES[""];
  const token = ++RENDERING;
  // A poll belongs to the page that started it; leaving one running would have
  // it re-render whatever the user navigated to instead.
  PAGE_TIMERS.forEach(clearInterval);
  PAGE_TIMERS = [];

  $("back").hidden = route === "";
  // Back goes to the page's PARENT, always - Android's Up, not the browser's
  // back. It used to fall back to history for pages without a parent, and two
  // pages that linked to each other then sent Back between them for ever,
  // never reaching home. Every chain here ends at home.
  const parent = PARENT[slash > 0 ? route.slice(0, slash) + "/*" : route] || PARENT[""];
  $("back").dataset.up = parent[0];
  $("back").innerHTML = icon("chevron", 15).replace('d="M6 3l5 5-5 5"', 'd="M10 3L5 8l5 5"') +
                        "<span>" + parent[1] + "</span>";
  // A refresh of the page being read - after a change, or a poll - keeps
  // where the reader was, which sections they had opened and what had the
  // keyboard focus, and leaves the old content (and a spinning Scan button)
  // up until the new is ready. Every one used to jump to the top.
  const refresh = route === LAST_ROUTE;
  LAST_ROUTE = route;
  const nav = NAVIGATED;
  NAVIGATED = false;
  // A search result's label, taken now: a failed or overtaken render must
  // not leave it for an unrelated page to find later.
  const find = FIND;
  FIND = null;
  // Filled in just before the old page goes: the reader may scroll, open a
  // section or move focus while a slow page (a scan) loads.
  const keep = refresh ? {} : null;
  if (!refresh) LAST_FOCUS = null;
  if (!refresh) {
    $("hdracts").innerHTML = "";
    $("view").innerHTML = '<div class="skeleton">Loading…</div>';
  }

  let result;
  try {
    result = await page(param);
  } catch (err) {
    if (token !== RENDERING) return;
    $("title").textContent = "Settings";
    $("hdracts").innerHTML = "";
    $("view").innerHTML = "";
    $("view").appendChild(statusPage("info", "Could not load this page", esc(err.message)));
    return;
  }
  // A slower page that lost the race must not paint over the one the user is
  // now looking at.
  if (token !== RENDERING) return;

  $("title").textContent = result.title;
  document.title = result.title === "Settings" ? "Echo settings"
                                               : result.title + " — Echo settings";
  // Taken now, just before the old page goes: the reader may have moved on
  // while this one loaded - into a dialog, even, which is left alone.
  if (keep) {
    keep.y = window.scrollY;
    keep.open = [...$("view").querySelectorAll("details.fold[open]")].map((d) => d.dataset.key);
    // A button that disabled itself while working has already lost focus to
    // the page; the last control focused before that stands in for it.
    keep.focus = !$("scrim").hidden ? null
      : focusKey(document.activeElement) || (document.activeElement === document.body ? LAST_FOCUS : null);
  }
  $("view").innerHTML = "";
  (Array.isArray(result.body) ? result.body : [result.body])
    .filter(Boolean).forEach((b) => $("view").appendChild(b));
  $("hdracts").innerHTML = "";
  (result.actions || []).forEach((a) => $("hdracts").appendChild(a));
  if (keep) {
    $("view").querySelectorAll("details.fold").forEach((d) => {
      if (keep.open.includes(d.dataset.key)) d.open = true;
    });
    window.scrollTo(0, keep.y);
    if (keep.focus) refocus(keep.focus);
    return;
  }
  window.scrollTo(0, 0);
  // A result that IS the page needs nothing found on it.
  if (find && find.toLowerCase() !== result.title.toLowerCase() && reveal(find)) return;
  // A new page: a screen reader starts at its title, and so does Tab.
  if (nav) $("title").focus({ preventScroll: true });
}

/* What has the keyboard focus, described so the same control can be found
   again once a refresh has rebuilt the page: its row (or section) by title,
   and which of the row's controls it was. */
const FOCUSABLE = "button, input, select, a[href], [tabindex]";
let LAST_FOCUS = null;
document.addEventListener("focusin", (e) => { const k = focusKey(e.target); if (k) LAST_FOCUS = k; });
const accName = (e) => (e.getAttribute("aria-label") || e.textContent || "").trim();
function focusKey(a) {
  if (!a || a === document.body) return null;
  if ($("hdracts").contains(a)) return { area: "hdracts", name: accName(a) };
  if (!$("view").contains(a)) return null;
  const rowEl = a.closest(".row");
  if (rowEl) {
    return { row: rowEl.dataset.key || "", at: a === rowEl ? -1 : [...rowEl.querySelectorAll(FOCUSABLE)].indexOf(a) };
  }
  const sum = a.closest("summary");
  if (sum) return { summary: sum.parentElement.dataset.key || sum.textContent };
  return { area: "view", tag: a.tagName, name: accName(a) };
}
function refocus(k) {
  // Not if a dialog opened meanwhile, or focus already went somewhere else.
  if (!$("scrim").hidden) return;
  const now = document.activeElement;
  if (now && now !== document.body && !$("view").contains(now) && !$("hdracts").contains(now)) return;
  let target = null;
  if (k.summary != null) {
    target = [...$("view").querySelectorAll("summary")]
      .find((x) => (x.parentElement.dataset.key || x.textContent) === k.summary);
  } else if (k.row != null) {
    // Rows are known by their title alone: a badge that changes with the
    // state ("Connected") must not lose them.
    const rowEl = [...$("view").querySelectorAll(".row")].find((x) => x.dataset.key === k.row);
    if (rowEl) target = k.at >= 0 ? [...rowEl.querySelectorAll(FOCUSABLE)][k.at] || rowEl : rowEl;
  } else if (k.area) {
    target = [...$(k.area).querySelectorAll(FOCUSABLE)]
      .find((x) => accName(x) === k.name && (!k.tag || x.tagName === k.tag));
  }
  if (!(target && (target.tabIndex >= 0 || target.matches(FOCUSABLE)))) target = $("title");
  try { target.focus({ preventScroll: true }); } catch (e) { /* gone again */ }
}

/* Open the section holding a setting, bring it into view and highlight it.
   The label is the one the page shows; a search result carries it. */
function reveal(label) {
  const norm = (t) => t.replace(/\s+/g, " ").trim().toLowerCase();
  const want = norm(label);
  const own = (n) => norm(n.firstChild && n.firstChild.nodeType === 3 ? n.firstChild.textContent : n.textContent);
  const nodes = [...$("view").querySelectorAll(".row .title, details.fold > summary, .group > h2")];
  const hit = nodes.find((n) => own(n) === want) || nodes.find((n) => own(n).startsWith(want)) ||
              nodes.find((n) => own(n).includes(want));
  if (!hit) return false;
  const target = hit.closest(".row") || (hit.tagName === "SUMMARY" ? hit : hit);
  for (let d = target.parentElement; d; d = d.parentElement) {
    if (d.tagName === "DETAILS") d.open = true;
  }
  target.scrollIntoView({ block: "center" });
  target.classList.remove("flash");
  void target.offsetWidth;
  target.classList.add("flash");
  if (target.tabIndex >= 0) target.focus({ preventScroll: true });
  return true;
}

/* Where Back goes from each page. Anything not listed goes home. */
const HOME = ["#/", "Settings"];
const PARENT = {
  "": HOME,
  "bluetooth": ["#/devices", "Connected devices"], "usb": ["#/devices", "Connected devices"],
  "app/*": ["#/apps", "Apps"], "stock": ["#/storage", "Storage"],
  "updates": ["#/system", "System"], "datetime": ["#/system", "System"],
  "reset": ["#/system", "System"], "processes": ["#/system", "System"],
  "function/*": ["#/processes", "Processes"],
  "name": ["#/about", "About device"],
};

$("back").addEventListener("click", () => {
  location.hash = $("back").dataset.up || "#/";
});
window.addEventListener("hashchange", () => { NAVIGATED = true; render(); });

/* Re-render the current page after an action changed something. */
const reload = () => render();
/* Re-render in a moment - after a change the device takes a second to act
   on - but only if the person is still on the page that asked. A timer from
   the page they just left would otherwise redraw the one they are reading. */
function reloadSoon(ms) {
  const here = location.hash;
  setTimeout(() => { if (location.hash === here) render(); }, ms);
}
</script>
<script>
"use strict";


/* ===================================================================== *
 * Wi-Fi
 * ===================================================================== */
function wifiBands(net) {
  return (net.bands || [net.band]).filter(Boolean).join(" / ");
}

/* One scan at a time per radio, whichever button asked: a second would hold
   the radio twice as long, at Wi-Fi's cost. Every scan button of the page
   shows it running. */
const SCANNING = {};
function scanButton(kind, header) {
  const b = header
    ? btn("", { cls: "flat", icon: "refresh", aria: kind === "wifi" ? "Scan for networks" : "Scan for devices",
                onclick: (x) => startScan(kind, x) })
    : btn("Scan", { cls: "small suggested", onclick: (x) => startScan(kind, x) });
  b.dataset.scan = kind;
  return b;
}
async function startScan(kind, b) {
  if (SCANNING[kind]) return;
  SCANNING[kind] = true;
  document.querySelectorAll("#hdracts button, #view button").forEach((x) => {
    if (x.dataset.scan === kind || x === b) {
      x.disabled = true;
      const svg = x.querySelector("svg");
      if (svg) svg.classList.add("spin"); else x.textContent = "Scanning…";
    }
  });
  if (kind === "wifi") window.__wifiScan = true; else window.__btScan = true;
  try { await render(); } finally { SCANNING[kind] = false; }
}

async function joinNetwork(net) {
  if (net.open) {
    const go = await confirmDialog("Join " + net.ssid + "?",
      "This network has no password, so anything on it can see traffic to and " +
      "from this device.", "Join");
    if (!go) return;
    await api("/api/wifi", { action: "connect", ssid: net.ssid });
    toast("Connecting to " + net.ssid + "…");
    reloadSoon(3500);
    return;
  }
  const r = await dialog({
    title: "Join " + net.ssid,
    text: wifiBands(net) + " · " + net.security,
    okLabel: "Join",
    fields: [{ name: "psk", label: "Password", type: "password",
               autocomplete: "current-password" }],
    onsubmit: async (v) => {
      if (!v.psk || v.psk.length < 8) throw new Error("A Wi-Fi password is at least 8 characters.");
      await api("/api/wifi", { action: "connect", ssid: net.ssid, psk: v.psk }, { seal: true });
    },
  });
  if (r) {
    toast("Connecting to " + net.ssid + "…");
    // Association plus DHCP is up to fifteen seconds on this radio. Reloading
    // immediately would show "Not connected" and read as a failure.
    reloadSoon(4000);
  }
}

PAGES["wifi"] = async function () {
  const scan = window.__wifiScan;
  window.__wifiScan = false;
  const st = await api("/api/wifi" + (scan ? "?scan=1" : ""));
  const s = st.status;

  if (!s.available) {
    return { title: "Wi-Fi",
             body: statusPage("wifi", "Wi-Fi is not running",
               "wpa_supplicant is not up on this device, so there is nothing to " +
               "configure. The USB connection still works.") };
  }

  const scanBtn = scanButton("wifi", true);

  const blocks = [];

  // --- the current connection ---
  if (s.associated || s.connected) {
    blocks.push(group(s.connected ? "Connected" : "Waiting for network address", boxed([
      row({ lead: el('<div class="icon">' + bars(s.signal) + "</div>"),
            title: s.ssid, badge: s.connected ? "Connected" : "Waiting for DHCP",
            badgeKind: s.connected ? "ok" : "warn",
            sub: s.connected ? s.band + " · " + s.security_label :
                 "Wi-Fi is associated, but the router has not supplied an IP address.",
            end: btn("Disconnect", { cls: "small", onclick: async () => {
              const saved = st.saved.find((n) => n.current);
              if (!saved) return;
              if (!await confirmDialog("Forget " + s.ssid + "?",
                    "The device will stop reconnecting to this network, and you " +
                    "will need the password again to rejoin it.", "Forget", true)) return;
              await api("/api/wifi", { action: "forget", id: saved.id });
              toast("Forgotten.");
              reload();
            } }) }),
      row({ title: "IP address", value: s.ip || "—" }),
      row({ title: "Signal", value: (s.rssi != null ? s.rssi + " dBm" : "—") }),
      row({ title: "Frequency", value: s.freq ? s.freq + " MHz" : "—" }),
      row({ title: "MAC address", value: s.mac || "—" }),
    ])));
  } else {
    blocks.push(group("Status", boxed([
      row({ icon: "wifi", title: "Not connected",
            sub: "State: " + esc(s.state) +
                 (s.ip ? " · stale address " + esc(s.ip) : ""),
            end: btn("Retry", { cls: "small", onclick: async () => {
              await api("/api/wifi", { action: "reconnect" });
              toast("Reconnecting…");
              reloadSoon(3000);
            } }) }),
    ])));
  }

  // --- what is on the air ---
  const current = (s.associated || s.connected) ? s.ssid : null;
  const currentScan = (st.networks || []).find((n) => n.ssid === current);
  if (currentScan && (currentScan.bands || []).length > 1) {
    blocks.push(group(st.scan && st.scan.cached ? "Previously detected bands" : "Available bands", boxed([row({
      title: current, sub: wifiBands(currentScan),
    })])));
  }
  const found = (st.networks || []).filter((n) => n.ssid !== current);
  if (st.scan && st.scan.cached) {
    blocks.push(group("Scan status", boxed([row({
      title: st.scan.state === "timeout" ? "Scan is taking longer than expected" : "Scan did not complete",
      sub: (st.networks || []).length ? "Showing previously discovered networks. Try scanning again." :
                         "No fresh results are available. Try scanning again.",
    })])));
  }
  if (st.scanned && !found.length) {
    blocks.push(group("Available networks",
      boxed([row({ title: st.scan && st.scan.cached ? "No other cached networks" : "No other networks found",
                   sub: "Try scanning again." })])));
  } else if (found.length) {
    blocks.push(group("Available networks", boxed(found.map((n) =>
      row({
        lead: el('<div class="icon">' + bars(n.signal) + "</div>"),
        title: n.ssid,
        sub: wifiBands(n) + " · " + n.security + (n.open ? " (unsecured)" : ""),
        end: n.open ? null : el('<div class="icon">' + icon("lock", 14) + "</div>"),
        onclick: () => joinNetwork(n),
      })))));
  } else {
    blocks.push(group("Available networks", boxed([
      row({ title: "Not scanned yet",
            sub: "Scanning takes a few seconds and briefly interrupts Bluetooth.",
            end: scanButton("wifi") }),
    ])));
  }

  // --- saved ---
  const saved = (st.saved || []).filter((n) => !(s.connected && n.current));
  if (saved.length) {
    blocks.push(group("Saved networks", boxed(saved.map((n) =>
      row({
        icon: "wifi",
        title: n.ssid,
        sub: n.disabled ? "Disabled" : "Saved on this device",
        end: [
          btn("Connect", { cls: "small", onclick: async () => {
            await api("/api/wifi", { action: "select", id: n.id });
            toast("Switching to " + n.ssid + "…");
            reloadSoon(4000);
          } }),
          btn("", { cls: "flat destructive", icon: "trash", onclick: async () => {
            if (!await confirmDialog("Forget " + n.ssid + "?",
                  "You will need the password again to rejoin it.", "Forget", true)) return;
            await api("/api/wifi", { action: "forget", id: n.id });
            toast("Forgotten.");
            reload();
          } }),
        ],
      })))));
  }

  blocks.push(group("", boxed([
    row({ icon: "plus", title: "Join a hidden network",
          sub: "For a network that does not broadcast its name",
          onclick: async () => {
            const r = await dialog({
              title: "Join a hidden network",
              okLabel: "Join",
              fields: [
                { name: "ssid", label: "Network name" },
                { name: "psk", label: "Password (leave empty if open)", type: "password" },
              ],
              onsubmit: async (v) => {
                if (!v.ssid.trim()) throw new Error("A network name is required.");
                await api("/api/wifi", { action: "connect", ssid: v.ssid.trim(),
                                         psk: v.psk || null, hidden: true }, { seal: !!v.psk });
              },
            });
            if (r) { toast("Connecting…"); reloadSoon(4000); }
          } }),
  ])));

  return { title: "Wi-Fi", body: blocks, actions: [scanBtn] };
};

/* ===================================================================== *
 * Bluetooth
 * ===================================================================== */
function btDeviceRow(d, opts) {
  opts = opts || {};
  // `link_only` is CONNECTED WITH NO AUDIO PROFILE OPEN: a phone linked for the
  // Bluetooth proxy or hands-free, or a peer that paired and linked but never
  // opened A2DP. That is a normal state and is badged neutrally, not as a
  // warning.
  //
  // It is NOT "a speaker that was switched off", which this comment claimed for
  // a long time. That assumed the audio transport drops immediately while the
  // ACL link lingers to the supervision timeout, leaving a window to show.
  // Measured twice on 2026-09-28 by cutting a speaker's power at the wall, with
  // and without a client holding the PCM: the device went connected -> gone
  // inside one one-second sample both times and `link_only` never appeared.
  // bluealsa's transports ARE BlueZ MediaTransport1 objects, so there is no
  // second event source and no window. See item 2.5.
  //
  // The state IS reachable for about half a second while a peer negotiates
  // its profiles - caught by sampling a reconnect at 5 Hz. So this badge
  // appears on EVERY ordinary connection, which is the real reason it must
  // not say "No audio" in warn styling.
  const parts = [];
  if (d.audio_ready) parts.push("Connected");
  else if (d.link_only) parts.push("Linked, no audio profile");
  else if (d.paired) parts.push("Paired");
  if (d.battery != null) parts.push("Battery " + d.battery + "%");
  if (d.we_play_to && d.plays_to_us) parts.push("Plays both ways");
  else if (d.we_play_to) parts.push("This device plays to it");
  else if (d.plays_to_us) parts.push("It plays to this device");

  const acts = [];
  // The cast switch, for anything we can play TO. Chosen even while the speaker
  // is disconnected: biscuit-btcast resumes by itself when it comes back, so
  // the switch is a standing preference rather than a live action.
  if (opts.cast && d.paired && d.we_play_to) {
    acts.push(switchEl(opts.castTarget === d.mac, async (on, input) => {
      input.disabled = true;
      try {
        const r = await api("/api/bluetooth",
                            { action: "cast", mac: on ? d.mac : "" });
        toast(r.message || "Saved.");
      } catch (e) { toast(e.message, true); }
      reloadSoon(900);
    }));
  }
  if (d.paired) {
    acts.push(btn(d.connected ? "Disconnect" : "Connect", { cls: "small", onclick: async (b) => {
      b.disabled = true;
      try {
        const r = await api("/api/bluetooth",
          { action: d.connected ? "disconnect" : "connect", mac: d.mac });
        toast(r.message || "Done.");
      } catch (e) { toast(e.message, true); }
      reload();
    } }));
    acts.push(btn("", { cls: "flat destructive", icon: "trash", onclick: async () => {
      if (!await confirmDialog("Remove " + d.name + "?",
            "The pairing is deleted. To use it again you will have to pair it " +
            "from scratch.", "Remove", true)) return;
      try {
        await api("/api/bluetooth", { action: "remove", mac: d.mac });
        toast("Removed.");
      } catch (e) { toast(e.message, true); }
      reload();
    } }));
  } else {
    acts.push(btn("Pair", { cls: "small suggested", onclick: async (b) => {
      b.disabled = true;
      b.textContent = "Pairing…";
      try {
        const r = await api("/api/bluetooth", { action: "pair", mac: d.mac });
        toast(r.message || "Paired.");
      } catch (e) { toast(e.message, true); }
      reload();
    } }));
  }

  return row({
    icon: d.we_play_to && !d.plays_to_us ? "head" : (d.plays_to_us ? "phone" : "bluetooth"),
    title: d.name,
    // "Linked" rather than "No audio": the state means no audio profile is
    // open, which for a phone acting as a proxy peer is exactly right and not
    // something to warn about. `warn` here made a normal phone look broken.
    badge: d.audio_ready ? "Connected" : (d.link_only ? "Linked" : null),
    badgeKind: d.audio_ready ? "ok" : (d.link_only ? "accent" : "warn"),
    sub: (parts.join(" · ") || "Not paired") + '<br><span class="mono">' + esc(d.mac) + "</span>",
    end: acts,
  });
}






/* ===================================================================== *
 * USB
 * ===================================================================== */
PAGES["usb"] = async function () {
  const u = await api("/api/usb");
  // Reached over the cable itself: turning the network off ends this page.
  const overUsb = location.hostname === "172.16.42.1";

  async function toggle(f, on, input) {
    const want = u.features.filter((x) => (x.id === f.id ? on : x.on)).map((x) => x.id);
    if (f.id === "net" && !on) {
      const text = overUsb
        ? "You are using this page over the cable. Turning the network off ends the connection; " +
          "reach the Echo over Wi-Fi afterwards."
        : "The computer will no longer reach this Echo over the cable. Wi-Fi still works, and " +
          "a device that cannot join its Wi-Fi opens its setup hotspot, so restarting it gets you back in.";
      if (!await confirmDialog("Turn off USB networking?", text, "Turn off", true)) {
        input.checked = true;
        return;
      }
    }
    input.disabled = true;
    try {
      const r = await api("/api/usb", { features: want });
      toast(r.message || "Saved.");
    } catch (e) { toast(e.message, true); }
    // Changing it re-enumerates the USB device, so a browser reached over the
    // cable stalls briefly here. That is the change working, not a failure.
    reloadSoon(2500);
  }

  const rows = u.features.map((f) => row({
    icon: { net: "link", mic: "mic", spk: "speaker" }[f.id], title: f.label, sub: esc(f.help),
    end: f.available ? switchEl(f.on, (on, input) => toggle(f, on, input))
                     : el('<div class="sub">Needs USB audio in the kernel</div>'),
  }));

  return {
    title: "USB",
    body: [
      group("", boxed([row({ icon: "plug", title: "When plugged into a computer", value: u.label })])),
      group("Features", boxed(rows),
        "With everything off the cable only charges, which is the default. Audio over USB runs " +
        "at 48 kHz - better than Bluetooth, whose calls top out at 16 kHz - and every computer " +
        "takes it without a driver."),
    ],
  };
};




</script>

<script>
"use strict";


/* ===================================================================== *
 * Name
 *
 * One place for both names. The device name is the network's, Home
 * Assistant's and Music Assistant's; the Bluetooth name follows it unless
 * someone chooses a different one here.
 * ===================================================================== */
/* Move this page to another port: the server restarts on it, so the page
   follows it there. Sessions do not survive the restart. */
async function changePort(about) {
  let moved = null;
  const r = await dialog({
    title: "Settings address", okLabel: "Move",
    text: "The port this page is served on: 8080 by default, or 80 to leave the number out " +
          "of the address. You will sign in again at the new address.",
    fields: [{ name: "port", label: "Port", type: "number", value: String(about.settings_port) }],
    onsubmit: async (v) => { moved = await api("/api/settings-port", { port: v.port.trim() }); },
  });
  if (!r || !moved) return;
  if (!moved.moved) { toast("It is already there."); return; }
  const next = location.protocol + "//" + location.hostname + (moved.port === 80 ? "" : ":" + moved.port) + "/";
  $("view").replaceChildren(statusPage("link", "Moving to port " + moved.port,
    'The page opens at <a href="' + esc(next) + '">' + esc(next) + "</a> by itself in a moment. " +
    "On other devices, it is now " + esc(moved.url) + "."));
  waitForReturn(moved.port, 2500);
}

PAGES["name"] = async function () {
  const [about, bt] = await Promise.all([api("/api/about"), api("/api/bluetooth")]);
  const name = about.device_name;
  const alias = (bt.adapter || {}).alias || "";
  const follows = alias === name;
  return {
    title: "Name",
    body: group("This device", boxed([
      row({ icon: "tag", title: "Device name", value: name,
            sub: "On your network as " + esc(name) + ".local, and in Home Assistant and Music Assistant.",
            onclick: async () => {
              const r = await dialog({
                title: "Device name", okLabel: "Rename",
                text: "Letters, numbers and hyphens. The voice assistant and music " +
                      "speaker restart to take the new name, so they are unavailable " +
                      "for about ten seconds.",
                fields: [{ name: "name", label: "Name", value: name }],
                onsubmit: (v) => api("/api/account", { action: "device_name", name: v.name }),
              });
              if (r) { toast("Renamed."); reloadSoon(2500); }
            } }),
      row({ icon: "bluetooth", title: "Bluetooth name", value: alias || "—",
            sub: follows ? "The same as the device name."
                         : "Chosen separately. Clear it to use the device name.",
            onclick: async () => {
              const r = await dialog({
                title: "Bluetooth name", okLabel: "Save",
                text: "What phones show when they look for this speaker. Leave it " +
                      "empty to use the device name.",
                fields: [{ name: "alias", label: "Bluetooth name",
                           value: follows ? "" : alias, placeholder: name }],
                onsubmit: (v) => api("/api/bluetooth", { action: "alias", alias: v.alias }),
              });
              if (r) { toast("Bluetooth name saved."); reload(); }
            } }),
      row({ icon: "link", title: "Settings address", value: about.settings_url,
            sub: "Where this page is. The name part is the device name.",
            onclick: () => changePort(about) }),
    ]), "A new device name reaches Bluetooth at the next restart."),
  };
};

/* ===================================================================== *
 * Home Assistant
 *
 * Everything this Echo offers Home Assistant and Music Assistant, and whether
 * each is actually connected - read from the connections themselves, so
 * "connected" means a live connection rather than a setting.
 * ===================================================================== */
PAGES["homeassistant"] = async function () {
  const h = await api("/api/homeassistant");
  const v = h.voice || {}, m = h.sendspin || {};
  const blocks = [];
  // An app ticked at setup that is not installed yet says so, and why: "Not
  // installed" alone read as if it had never been asked for.
  const pend = h.pending;
  const notYet = (id, iconName) => {
    const i = pend ? (pend.ids || []).indexOf(id) : -1;
    if (i < 0) return null;
    // This app alone, so the words are "it", not "they" - and in its own
    // state: an install running now may be of the other app only.
    const inst = pend.installing && (pend.installing.ids || []).indexOf(id) >= 0;
    const one = inst ? { state: "installing", message: pend.installing.message, labels: [pend.labels[i]] }
                     : Object.assign({}, pend, { labels: [pend.labels[i]], rest: { labels: [pend.labels[i]] },
                                                 failed: null });
    if (!inst && pend.state === "failed" && pend.failed && (pend.failed.ids || []).indexOf(id) < 0) {
      // What failed was an install of the other app; this one was not tried.
      Object.assign(one, { state: pend.installing ? "busy" : "pending", message: "", reason: "", tried_at: null });
    }
    const [badge, badgeKind] = pendingBadge(one);
    return row({ icon: iconName, title: "Not installed yet", badge, badgeKind, go: "#/apps",
                 sub: "Chosen at setup. " + pendingWhy(one, Date.now() / 1000, true) });
  };

  if (!v.installed) {
    blocks.push(group("Voice assistant", boxed([
      notYet("voice", "mic") ||
      row({ icon: "mic", title: "Not installed",
            sub: "Install the voice assistant to use this Echo with Assist.", go: "#/apps" }),
    ])));
  } else {
    const rows = [];
    const on = (v.peers || []).length > 0;
    rows.push(row({ icon: "mic",
      title: on ? "Connected" : (v.running ? "Waiting for Home Assistant" : "Not running"),
      badge: on ? "Connected" : null, badgeKind: "ok",
      sub: on ? "Home Assistant at " + esc(v.peers.join(", "))
              : (v.running ? "Home Assistant usually finds it by itself within a minute."
                           : "Turn it on on the Apps page.") }));
    if (!on && v.running) {
      rows.push(row({ icon: "info", title: "Add it by hand",
        sub: "In Home Assistant: Settings → Devices &amp; services → Add integration → " +
             "ESPHome. Host <b>" + esc(h.ip || "this device's address") + "</b>, port <b>" +
             esc(v.port) + "</b>." }));
    }
    rows.push(row({ icon: "button", title: "Button events",
      sub: "Every press and hold reaches Home Assistant as an event, whatever the " +
           "button is set to do.", go: "#/buttons" }));
    blocks.push(group("Voice assistant", boxed(rows)));
  }

  if (!m.installed) {
    blocks.push(group("Music Assistant", boxed([
      notYet("sendspin", "note") ||
      row({ icon: "note", title: "Not installed",
            sub: "Install the music speaker to play to this Echo from Music Assistant.", go: "#/apps" }),
    ])));
  } else {
    const on = (m.servers || []).length > 0;
    blocks.push(group("Music Assistant", boxed([
      row({ icon: "note",
        title: on ? "Connected" : (m.running ? "Waiting for Music Assistant" : "Not running"),
        badge: on ? "Connected" : null, badgeKind: "ok",
        sub: on ? "Music Assistant at " + esc(m.servers.join(", "))
                : (m.running ? "It is found on the network once Music Assistant's Sendspin " +
                               "provider is on."
                             : "Turn it on on the Apps page.") }),
    ])));
  }

  // The Bluetooth proxy. Off by default and said plainly, because the cost is
  // not obvious: a continuous scan roughly halves 2.4 GHz Wi-Fi throughput on
  // this hardware. The cadence is offered as named presets, since what a
  // person is really choosing is how much airtime to give away.
  {
    const bp = h.btproxy || {};
    const rows = [];
    rows.push(row({ icon: "bluetooth", title: "Bluetooth proxy",
      sub: bp.enabled
        ? (bp.subscribed
            ? "Home Assistant is listening · " + (bp.adverts_seen || 0) + " advertisements seen"
            : "On, waiting for Home Assistant to connect")
        : "Passes nearby Bluetooth sensors on to Home Assistant.",
      end: switchEl(!!bp.enabled, async (on) => {
        try {
          await api("/api/bluetooth", { action: "btproxy", enabled: on });
          toast(on ? "Bluetooth proxy on." : "Bluetooth proxy off.");
          reload();
        } catch (e) { toast(e.message, true); }
      }) }));
    if (bp.enabled) {
      rows.push(row({ title: "Scanning",
        sub: "Scanning shares the radio with Wi-Fi: less of it means faster Wi-Fi and " +
             "slower sensor updates.",
        end: selectEl((bp.cadences || []).map((c) => ({ value: c.value, label: c.label })),
          bp.cadence, async (val) => {
            try {
              await api("/api/bluetooth", { action: "btproxy", cadence: val });
              toast("Scanning updated.");
              reload();
            } catch (e) { toast(e.message, true); }
          }) }));
      rows.push(row({ title: "Let Home Assistant connect to devices",
        sub: bp.active
          ? "On · " + (bp.connections || 0) + " of " + (bp.max_connections || 3) +
            " connections in use. Scanning pauses while any are open."
          : "Only needed for devices it has to connect to, such as locks. " +
            "Sensors that broadcast work without it.",
        end: switchEl(!!bp.active, async (on) => {
          try {
            await api("/api/bluetooth", { action: "btproxy", active: on });
            toast(on ? "Connections on." : "Connections off.");
            reload();
          } catch (e) { toast(e.message, true); }
        }) }));
      rows.push(row({ icon: "info", title: "Add in Home Assistant", value: "port " + (bp.port || 6054),
        sub: "Found automatically as an ESPHome device. If not, add this device's " +
             "address in the ESPHome integration." }));
    }
    blocks.push(group("Bluetooth proxy", boxed(rows),
      "Works with sensors that broadcast, such as BTHome, Xiaomi and Govee."));
  }

  pollPage(() => api("/api/homeassistant"),
    (d) => JSON.stringify([d.voice && [d.voice.peers, d.voice.running, d.voice.installed],
                           d.sendspin && [d.sendspin.servers, d.sendspin.running, d.sendspin.installed],
                           d.pending && [d.pending.state, d.pending.waiting, d.pending.packages],
                           d.btproxy && [d.btproxy.enabled, d.btproxy.subscribed,
                                         d.btproxy.cadence, d.btproxy.active,
                                         d.btproxy.connections]]),
    6000);

  return { title: "Home Assistant", body: blocks };
};

/* ===================================================================== *
 * Bluetooth
 * ===================================================================== */
PAGES["bluetooth"] = async function () {
  const scan = window.__btScan;
  window.__btScan = false;
  const st = await api("/api/bluetooth" + (scan ? "?scan=1" : ""));
  const a = st.adapter;

  if (!a.available) {
    return { title: "Bluetooth",
             body: statusPage("bluetooth", "Bluetooth is not available",
               "The controller did not answer. Check that the bluetooth service " +
               "is running.") };
  }

  const scanBtn = scanButton("bt", true);

  const blocks = [];

  blocks.push(group("Pairing", boxed([
    row({ icon: "link", title: "Pairing mode",
          sub: a.pairing_window
            ? "Open. A phone or computer can pair now."
            : "Closed. Nothing new can pair until you open it.",
          end: switchEl(!!a.pairing_window, async (on) => {
            try {
              const r = await api("/api/bluetooth", { action: "pairing", open: on });
              toast(r.message || (on ? "Pairing open." : "Pairing closed."));
            } catch (e) { toast(e.message, true); }
            reloadSoon(600);
          }) }),
  ]), "Phones see this Echo as <b>" + esc(a.alias) + "</b> (<a href=\"#/name\">change</a>). " +
      "Pairing stays open for three minutes, and anything that pairs then is trusted " +
      "automatically, because this device has no screen to confirm on."));

  const outgoing = (st.devices || []).filter((d) => d.we_play_to);
  const castOpts = { cast: true, castTarget: st.cast_target };
  blocks.push(group("Speakers and headphones",
    outgoing.length
      ? boxed(outgoing.map((d) => btDeviceRow(d, castOpts)))
      : boxed([row({ icon: "head", title: "None paired",
                     sub: "Pair a speaker or headphones to play this device's audio " +
                          "through them." })]),
    outgoing.length
      ? ("Turn one on to play everything - the assistant, music and sounds - through " +
         "it instead of this device's speaker. " +
         (st.cast_active
            ? "<b>Playing through Bluetooth now.</b>"
            : (st.cast_target
                 ? "Chosen, but not connected: it starts by itself when the speaker is back."
                 : "")))
      : null));

  const incoming = (st.devices || []).filter((d) => d.plays_to_us && !d.we_play_to);
  blocks.push(group("Phones and computers",
    incoming.length
      ? boxed(incoming.map((d) => btDeviceRow(d)))
      : boxed([row({ icon: "phone", title: "None paired",
                     sub: "Open pairing mode, then pair from the phone or computer to " +
                          "play to this Echo or take calls on it." })])));

  const other = (st.devices || []).filter((d) => !d.we_play_to && !d.plays_to_us);
  if (other.length) {
    blocks.push(group("Other paired devices", boxed(other.map((d) => btDeviceRow(d)))));
  }

  const found = st.discovered || [];
  blocks.push(group("Nearby", found.length
    ? boxed(found.map((d) => btDeviceRow(d)))
    : boxed([row({ title: st.scanned ? "Nothing found" : "Not scanned yet",
                   sub: st.scanned
                     ? "Put the other device into pairing mode and scan again."
                     : "Scanning takes about eight seconds.",
                   end: scanButton("bt") })])));

  // Never with ?scan=1: the poll must not hold the radio every few seconds.
  pollPage(
    () => api("/api/bluetooth"),
    (d) => JSON.stringify([
      (d.devices || []).map((x) => [x.mac, x.audio_ready, x.link_only, x.connected]),
      (d.discovered || []).map((x) => x.mac),
      d.cast_active, d.cast_target,
      d.adapter && d.adapter.pairing_window, d.adapter && d.adapter.alias,
    ]),
    6000);

  return { title: "Bluetooth", body: blocks, actions: [scanBtn] };
};


/* ===================================================================== *
 * Microphone
 *
 * The everyday controls first. The tuning - which chain, which detector, the
 * preamp gain - is folded away with a way back to the recommended settings,
 * because those were chosen by measurement and are easy to make worse.
 * ===================================================================== */
PAGES["mic"] = async function () {
  const [m, audio] = await Promise.all([api("/api/mic"), api("/api/audio")]);
  const apply = async (body, msg) => {
    try {
      const r = await api("/api/mic", body);
      toast(r.restarting
        ? (msg || "Applied.") + " The microphones are restarting."
        : (msg || "Applied."));
      reloadSoon(r.restarting ? 1500 : 300);
    } catch (e) { toast(e.message, true); }
  };
  const s = m.settings;
  const blocks = [];

  const restartNote = micRestartNote(m.restart_outcome);
  if (restartNote) {
    blocks.push(group("", boxed([
      row({ title: "Last change", badge: "Attention", badgeKind: "warn", sub: restartNote }),
    ])));
  }

  blocks.push(group("Microphones", boxed([
    row({ icon: "mic", title: "Mute",
          sub: "The same as the mute button on the device.",
          end: switchEl(!!audio.muted, async (on) => {
            try {
              await api("/api/audio", { muted: on });
              toast(on ? "Microphones muted." : "Microphones on.");
            } catch (e) { toast(e.message, true); }
          }) }),
  ])));

  blocks.push(group("Mute light", boxed([
    row({ title: "While muted",
          sub: "The red light on the mute button. Auto dims it in a dark room.",
          end: selectEl(m.mute_led_modes, m.mute_led, async (v) => {
            try { await api("/api/audio", { mute_led: v }); toast("Mute light: " + v + "."); }
            catch (e) { toast(e.message, true); }
          }) }),
    row({ title: "Also when not muted",
          sub: "Lights it even while the microphones are on. Off leaves it to follow mute.",
          end: selectEl(m.mic_led_modes, m.mic_led, async (v) => {
            try { await api("/api/audio", { mic_led: v }); toast("Saved."); }
            catch (e) { toast(e.message, true); }
          }) }),
  ])));

  const notes = (obj) => Object.keys(obj || {}).filter((k) => obj[k])
    .map((k) => " " + esc(k) + " is unavailable: " + esc(obj[k]) + ".").join("");
  const array = s.mic_source === "Array";
  const tuning = boxed([
    row({ title: "Microphones used",
          sub: "All seven, steered towards whoever is speaking, or a single one.",
          end: selectEl(m.sources, s.mic_source, (v) => apply({ mic_source: v })) }),
    row({ title: "Single microphone",
          sub: "Which one, when only one is used. MK7 is the centre.",
          end: selectEl(m.capsules, s.mic_capsule, (v) => apply({ mic_capsule: v }), array) }),
    row({ title: "Processing",
          badge: micFallback(m.profile_status) ? "fallback" : null, badgeKind: "warn",
          sub: (micFallback(m.profile_status) ? "<b>" + micFallbackText(m.profile_status) + "</b> " : "") +
               "pmOS 8-beam is tuned for this Echo. The Fire OS choices run Amazon's " +
               "filters from your own backup. Changing it pauses the assistant for about " +
               "ten seconds." + notes(m.profile_notes),
          end: selectEl(m.profiles, s.mic_profile, (v) => apply({ mic_profile: v })) }),
    row({ title: "Voice detector",
          sub: m.vad_applies
            ? "Decides when the microphones adapt to the room. Stock DNN copes better " +
              "with music playing; Energy suits a quiet room." + notes(m.vad_notes)
            : esc(m.vad_unavailable_reason),
          end: selectEl(m.vads, s.mic_vad, (v) => apply({ mic_vad: v }), !m.vad_applies) }),
    row({ title: "Echo cancellation",
          sub: m.aec_available
            ? "Takes this Echo's own playback out of what it hears, so it can hear you over music."
            : esc(m.aec_unavailable_reason),
          end: selectEl(m.on_off, m.aec_available ? s.mic_aec : "Off",
                        (v) => apply({ mic_aec: v }), !m.aec_available) }),
    row({ title: "Adaptive beamforming",
          sub: array ? "Steers away from a steady noise, such as a fan."
                     : "Needs all seven microphones.",
          end: selectEl(m.on_off, s.mic_beam, (v) => apply({ mic_beam: v }), !array) }),
    sliderRow({ title: "Input gain",
                sub: "The microphone preamp. 20 dB is Amazon's setting; more clips loud voices.",
                min: m.pga_min, max: m.pga_max, step: 0.5, value: s.mic_pga_gain,
                format: (v) => v.toFixed(1) + " dB",
                onchange: (v) => apply({ mic_pga_gain: v }, "Input gain " + v.toFixed(1) + " dB.") }),
    row({ title: "Calibration",
          sub: "Factory uses the corrections measured for each microphone when this Echo was made.",
          end: selectEl(m.miccal_modes, m.miccal, (v) => apply({ miccal: v }, "Calibration: " + v + ".")) }),
    row({ icon: "refresh", title: "Reset to recommended",
          sub: "All seven microphones, pmOS 8-beam, adaptive beamforming and echo " +
               "cancellation on, 20 dB, factory calibration.",
          end: btn("Reset", { cls: "small", onclick: async () => {
            if (!await confirmDialog("Reset the microphone tuning?",
                  "The assistant pauses for about ten seconds while the microphones restart.",
                  "Reset")) return;
            apply({ reset: true }, "Recommended settings restored.");
          } }) }),
  ]);
  blocks.push(fold("Tuning", [group("", tuning)],
    "The recommended settings were chosen by testing the wake word at different " +
    "distances, with music playing and with other people talking."));

  const stockFile = document.createElement("input");
  stockFile.type = "file"; stockFile.accept = ".cfg";
  stockFile.onchange = async () => {
    if (!stockFile.files.length) return;
    try {
      const file = stockFile.files[0];
      if (file.size > 60000) throw new Error("AFE.cfg is too large");
      await api("/api/mic/call-stock", { afe_cfg: await file.text() });
      toast("Stock call tuning imported."); reload();
    } catch (e) { toast(e.message, true); }
  };
  blocks.push(group("Calls", boxed([
    row({ title: "Call processing",
          sub: "Echo cancellation, noise reduction and level control for Bluetooth calls.",
          end: selectEl(m.on_off, s.call_processing, (v) => apply({ call_processing: v })) }),
  ])));
  blocks.push(fold("Call tuning", [group("", boxed([
    row({ title: "Call microphone",
          sub: "Separate from the assistant's. MK7 is the centre.",
          end: selectEl(m.capsules, s.call_mic_capsule, (v) => apply({ call_mic_capsule: v })) }),
    row({ title: "Processing profile",
          sub: "Both use open-source processing. Stock-derived borrows the settings Amazon's " +
               "tuning uses, not its code.",
          end: selectEl(m.call_profiles, s.call_profile, (v) => apply({ call_profile: v })) }),
    row({ title: "Import stock call tuning",
          sub: m.stock_call_import
            ? "Imported. Applied: " + esc(m.stock_call_import.applied.join(", "))
            : "Choose AFE.cfg from your backup's fireos6 folder.",
          end: stockFile }),
  ]))]));

  // A change made somewhere else - Home Assistant, the mute button, another
  // browser - shows here within a few seconds rather than after a reload. The
  // page is re-rendered only when something in it changed, and never while a
  // control has focus or a dialog is open, so it does not move under the user.
  pollPage(() => api("/api/mic"), (x) => JSON.stringify(x), 3000);

  return { title: "Microphone", body: blocks };
};

/* ===================================================================== *
 * Action button
 * ===================================================================== */
PAGES["buttons"] = async function () {
  const a = await api("/api/audio");
  const rows = a.buttons.map((b) =>
    row({ icon: "button", title: b.label,
          end: selectEl(a.button_actions, b.action, async (v) => {
            try {
              await api("/api/audio", { buttons: { [b.gesture]: v } });
              toast(b.label + ": " + v + ".");
            } catch (e) { toast(e.message, true); }
          }) }));
  return {
    title: "Action button",
    body: group("Gestures", boxed(rows),
      "Every gesture also reaches Home Assistant as an event, so an automation can use " +
      "one even when the action here is Nothing."),
  };
};

/* ===================================================================== *
 * Light ring
 * ===================================================================== */
PAGES["ring"] = async function () {
  const [S, a, svc] = await Promise.all([api("/api/leds"), api("/api/ring"), api("/api/services")]);
  const blocks = [];

  /* --- brightness --- */
  let ringBusy = false;
  const level = el('<input type="range" min="0" max="100" step="1" aria-label="Ring brightness">');
  const levelValue = el("<span></span>");
  const levelBox = el('<div class="ring-level"></div>');
  levelBox.append(level, levelValue);
  const luxRow = row({ icon: "sun", title: "Room light", value: "—" });
  const appliedRow = row({ title: "Brightness now", value: "—" });
  const auto = switchEl(a.autodim, async (on) => {
    ringBusy = true;
    try {
      const current = await api("/api/ring", { autodim: on });
      ringBusy = false;
      syncBrightness(current);
    } catch (e) { ringBusy = false; toast(e.message, true); }
  });
  function syncBrightness(current) {
    luxRow.querySelector(".val").textContent = current.lux == null ? "Unavailable" : current.lux + " lux";
    appliedRow.querySelector(".val").textContent =
      Math.round(current.effective_brightness / 255 * 100) + "%";
    if (!ringBusy && document.activeElement !== level) {
      level.value = Math.round(current.brightness / 255 * 100);
      levelValue.textContent = level.value + "%";
      level.disabled = current.autodim;
      auto.querySelector("input").checked = current.autodim;
    }
  }
  level.addEventListener("input", () => { levelValue.textContent = level.value + "%"; });
  let pendingLevel = null, levelSaving = false;
  async function saveLevel() {
    if (levelSaving) return;
    levelSaving = true; ringBusy = true;
    while (pendingLevel !== null) {
      const value = pendingLevel;
      pendingLevel = null;
      try { await api("/api/ring", { brightness: value }); }
      catch (e) { toast(e.message, true); }
    }
    levelSaving = false; ringBusy = false;
  }
  level.addEventListener("change", () => {
    pendingLevel = Math.round(Number(level.value) * 255 / 100);
    saveLevel();
  });
  syncBrightness(a);
  blocks.push(group("Brightness", boxed([
    row({ icon: "sun", title: "Dim with the room", sub: "Follows the light sensor.", end: auto }),
    row({ title: "Brightness", sub: "Used when dimming with the room is off.", end: levelBox }),
    luxRow, appliedRow,
  ])));

  /* --- direction --- */
  const dir = (svc.services || []).find((x) => x.id === "direction") || {};
  const offset = el('<input type="number" min="0" max="359" step="1" aria-label="Pointer rotation in degrees">');
  offset.value = a.alignment.offset;
  let reversed = a.alignment.reverse;
  const directionRow = row({ title: "Measured now", value: "…" });
  const timing = { "always": "Always ready", "wake": "After the wake word" };
  blocks.push(group("Direction", boxed([
    row({ icon: "ring", title: "Direction light",
          sub: "Points the ring at whoever is speaking.",
          end: selectEl(APP_STATES, appState(dir), async (v) => {
            try { await setAppState("direction", dir, v); toast("Direction light: " + v + "."); }
            catch (e) { toast(e.message, true); }
            reloadSoon(1200);
          }) }),
    row({ title: "Start",
          sub: "Always ready points straight away and uses more of the processor.",
          end: selectEl(Object.values(timing), timing[dir.mode] || timing.always, async (v) => {
            const mode = Object.keys(timing).find((k) => timing[k] === v);
            try { await api("/api/services", { id: "direction", action: "mode", mode }); toast("Saved."); }
            catch (e) { toast(e.message, true); }
          }) }),
    directionRow,
    row({ title: "Rotate pointer", sub: "Degrees. Set it while making a sound from a known side.", end: offset }),
    row({ title: "Reverse", sub: "If the pointer moves the wrong way round.",
          end: switchEl(reversed, (v) => { reversed = v; saveAlignment(); }) }),
  ])));
  // Saved as it changes, like every other setting here. It was the last
  // control with a Save button of its own.
  async function saveAlignment() {
    const deg = Math.min(359, Math.max(0, Math.round(Number(offset.value) || 0)));
    offset.value = deg;
    try {
      await api("/api/ring", { alignment: { offset: deg, reverse: reversed } });
      toast("Alignment saved.");
    } catch (e) { toast(e.message, true); }
  }
  offset.addEventListener("change", saveAlignment);

  pollInto(() => api("/api/ring"), (r) => {
    syncBrightness(r);
    directionRow.querySelector(".val").textContent =
      "Speaker " + (r.direction && r.direction.speaker != null ? Math.round(r.direction.speaker) + "°" : "—") +
      " · noise " + (r.direction && r.direction.noise != null ? Math.round(r.direction.noise) + "°" : "—");
  }, 1500);

  /* --- activities --- */
  function rgbHex(rgb) { return "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join(""); }
  /* The choices, in sections: the generated effects (a new one appears here
     by itself), then every imported animation under its group, each once and
     by name - the server folds byte-identical copies and leaves out single
     frames and blank files. The volume ramp is only for "Volume changed". */
  const effectOpts = () => S.effects.filter((e) => !e.volume_only).map((e) => ({
    value: "effect:" + e.value, section: "Built-in effects",
    label: e.label + (e.colourable ? "" : " (fixed colours)") }));
  const stockOpts = () => S.stock.map((x) => ({ value: "stock:" + x.value, label: x.label,
    section: "Fire OS · " + x.group, hint: (x.also || []).join(" ") }));
  function optionsFor(act) {
    const def = { value: "default", label: "Default" + (act.default_label ? " · " + act.default_label : "") };
    // Its animation is chosen by level; only its colours are a choice.
    if (act.colours_only) {
      const vol = S.effects.filter((e) => e.volume_only);
      return [def].concat(vol.map((e) => ({ value: "effect:" + e.value,
                                            label: vol.length === 1 ? "Custom colours" : e.label })));
    }
    const opts = [def, { value: "off", label: "Off" }];
    // This activity's own animation from each Fire OS, when imported. Other
    // activities' are not repeated: they are files already in the list.
    if (act.fireos6) opts.push({ value: "stock:" + act.fireos6, label: "Fire OS 6" });
    if (act.fireos5) opts.push({ value: "stock:" + act.fireos5, label: "Fire OS 5" });
    return opts.concat(effectOpts(), stockOpts());
  }
  // A saved choice as a list value: a copy, or another activity's Fire OS 6
  // shortcut, shows as the file in the list that looks the same.
  function savedValue(act, o) {
    if (!o) return "default";
    if (o.off) return "off";
    if (o.effect) return "effect:" + o.effect;
    if (!o.animation) return "default";
    const a = o.animation;
    return "stock:" + (a === act.fireos6 || a === act.fireos5 ? a : (S.stock_aliases[a] || a));
  }
  // What a saved choice the list does not offer was, in words.
  function savedLabel(o) {
    if (o.off) return "Off (no longer offered)";
    if (o.effect) return o.effect + " (no longer offered)";
    const label = (S.stock_labels || {})[o.animation];
    return label ? label + " (no longer offered)" : "Removed animation";
  }
  // One list value as a spec. Browsing the list previews each option in the
  // row's own colours where it is the row's effect, else in the effect's.
  function specFor(v, r) {
    if (v === "default") return { kind: "default" };
    if (v === "off") return { kind: "off" };
    if (v.startsWith("stock:")) return { kind: "stock", animation: v.slice(6) };
    const effect = S.effects.find((e) => e.value === v.slice(7));
    const colours = r && r.sel && r.sel.value === v && r.colours.length ? r.colours.map((c) => c.value)
                  : effect ? effect.colour_defaults.map(rgbHex) : [];
    return { kind: "effect", effect: v.slice(7), colours };
  }
  function specOf(r) {
    const v = r.sel.value;
    if (!v.startsWith("effect:")) return specFor(v, r);
    const spec = { kind: "effect", effect: v.slice(7) };
    // An untouched older selection keeps its original one-colour rendering.
    if (!r.dirty && r.original && r.original.effect === spec.effect && !r.original.colours) {
      if (r.original.colour) spec.colour = rgbHex(r.original.colour);
      return spec;
    }
    if (r.colours.length) spec.colours = r.colours.map((c) => c.value);
    return spec;
  }
  // Browsing a list plays what is under the cursor, a moment after it
  // stops moving: holding an arrow key must not queue a preview per option.
  // One still waiting when the list closes is dropped (browseEnd).
  let browseTimer = null;
  const browse = (activity, r) => (v) => {
    clearTimeout(browseTimer);
    browseTimer = setTimeout(() => {
      api("/api/preview", Object.assign(activity ? { activity } : {}, specFor(v, r)))
        .catch((e) => toast(e.message, true));
    }, 250);
  };
  const browseEnd = () => clearTimeout(browseTimer);
  // The colour wells appear only for an effect that takes colours.
  function syncColour(r) {
    r.colBox.replaceChildren();
    r.colours = [];
    if (!r.sel.value.startsWith("effect:")) return;
    // Not for a choice the list no longer offers: a new colour would save it.
    const current = r.sel.options[r.sel.selectedIndex];
    if (current && current.disabled) return;
    const effect = S.effects.find((e) => e.value === r.sel.value.slice(7));
    if (!effect) return;
    const saved = r.original && r.original.effect === effect.value
      ? (r.original.colours || S.activity_colours[r.key]) : null;
    effect.colour_roles.forEach((label, i) => {
      const input = el('<input type="color">');
      input.value = rgbHex((saved || effect.colour_defaults)[i]);
      input.title = label;
      input.setAttribute("aria-label", r.label + " - " + label);
      input.addEventListener("change", () => { r.dirty = true; saveActivities(); });
      const wrapper = el("<label></label>");
      wrapper.append(input);
      r.colBox.append(wrapper);
      r.colours.push(input);
    });
  }
  const rows = [];
  let saving = null;
  async function saveActivities() {
    // Every change saves the whole set, as the old Save button did; the server
    // takes the full map. Serialised so two quick changes cannot race. A row
    // nobody has touched is sent as "keep", so the server leaves it exactly as
    // stored - a save never rewrites a choice this page could not show.
    const payload = {};
    rows.forEach((r) => { payload[r.key] = r.dirty ? specOf(r) : { kind: "keep" }; });
    const prev = saving;
    saving = (async () => {
      if (prev) { try { await prev; } catch (e) { /* reported already */ } }
      try { await api("/api/leds", payload); toast("Saved."); }
      catch (e) { toast(e.message, true); }
    })();
    return saving;
  }
  const actBlocks = [];
  let currentGroup = null, bucket = [];
  const flush = () => { if (bucket.length) actBlocks.push(group(currentGroup, boxed(bucket))); bucket = []; };
  S.activities.forEach((act) => {
    if (act.group !== currentGroup) { flush(); currentGroup = act.group; }
    let o = S.overrides[act.key];
    if (o && ["alexa_point-at-user", "alexa_point-at-noise"].includes(o.animation))
      o = { effect: o.animation.endsWith("user") ? "Point at speaker" : "Point at noise",
            colours: [[0, 255, 255], [0, 0, 255]] };
    const value = savedValue(act, o);
    const options = optionsFor(act);
    // Saved, but not in the list - its file was removed, it is one the list
    // leaves out, or it does nothing for this activity. Shown as what it is
    // rather than as "Default", which it is not, until someone changes it.
    if (o && !options.some((x) => x.value === value))
      options.unshift({ value, label: savedLabel(o), disabled: true });
    const colBox = el('<div class="pattern-colours"></div>');
    const entry = { key: act.key, label: act.label, sel: null, colBox, colours: [], original: o, dirty: false };
    entry.sel = selectEl(options, value, () => {
      entry.dirty = true; syncColour(entry); saveActivities();
    });
    rows.push(entry);
    syncColour(entry);
    const preview = btn("", { cls: "flat", icon: "play", aria: "Show on the ring", onclick: async () => {
      try { await api("/api/preview", Object.assign({ activity: act.key }, specOf(entry))); }
      catch (e) { toast(e.message, true); }
    } });
    const r = row({ title: act.label, end: [entry.sel, colBox, preview], preview: browse(act.key, entry),
                    previewEnd: browseEnd });
    r.classList.add("ring-choice");
    bucket.push(r);
  });
  flush();
  blocks.push(fold("Animations", actBlocks,
    "What the ring shows for each thing the device does. Changes save as you make them; " +
    "the play button shows one now, and each option plays on the ring as you move through the list." +
    (S.stock.length ? "" : " Amazon's own Fire OS animations become choices here once imported from your " +
                           'backup under <a href="#/stock">Files from stock</a>.')));

  /* --- the Home Assistant light's colours ---
     Home Assistant sees this ring as a light, "Light Ring", whose effects
     use its own colour plus two more: the "Ring Colour 2" and "Ring Colour 3"
     lights, each with its own on/off and brightness. These rows are those two
     entities - the same settings, not a second copy of them - and changing
     one in Home Assistant switches "Use Ring Colour 2 and 3" on. */
  {
    const pal = a.palette;
    const custom = pal.preset === "Custom accents";
    const hex = (rgb) => "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");
    const setAccent = async (index, change, msg) => {
      try { await api("/api/ring", { accent: Object.assign({ index }, change) }); if (msg) toast(msg); }
      catch (e) { toast(e.message, true); }
    };
    const colourRow = (i) => {
      const input = el('<input type="color" aria-label="Ring Colour ' + (i + 2) + '">');
      input.value = hex(pal.colours[i]);
      input.addEventListener("change", () => setAccent(i,
        { rgb: [1, 3, 5].map((k) => parseInt(input.value.slice(k, k + 2), 16)) },
        "Ring Colour " + (i + 2) + " saved."));
      const on = pal.enabled[i];
      return row({ title: "Ring Colour " + (i + 2),
        sub: on ? "On · " + Math.round(pal.levels[i] / 255 * 100) + "% brightness" : "Off: black in effects",
        end: [input, switchEl(on, (v) => setAccent(i, { on: v }, v ? "On." : "Off."))] });
    };
    const rows = [row({ title: "Use Ring Colour 2 and 3",
      sub: custom ? "Effects use the two colours below."
                  : "Off: each effect uses its own second and third colours.",
      end: switchEl(custom, async (v) => {
        try {
          await api("/api/ring", { palette: { preset: v ? "Custom accents" : "Pattern defaults" } });
          toast(v ? "Using Ring Colour 2 and 3." : "Using each effect's own colours.");
          reloadSoon(300);
        } catch (e) { toast(e.message, true); }
      }) })];
    if (custom) rows.push(colourRow(0), colourRow(1));
    blocks.push(group("Home Assistant light", boxed(rows),
      "In Home Assistant this ring is the \"Light Ring\" light. Its effects use the light's own " +
      "colour first, then Ring Colour 2 and 3, which are lights of their own there. The " +
      "animations above are separate and not affected."));
  }

  /* --- music visualiser --- */
  {
    const v = a.viz || {};
    const pct = (x) => Math.round((x || 0) * 100);
    // One file the player re-reads every second, so these apply while music
    // plays; restarting the player would drop Music Assistant's connection.
    const saveViz = (changes, msg) =>
      api("/api/ring", { viz: changes }).then((res) => {
        Object.assign(a.viz, res.viz || {});
        toast(msg);
      }).catch((e) => toast(e.message, true));
    const slider = (title, sub, key, fromPct, toPct) => sliderRow({
      title, sub, min: 0, max: 100, step: 5, value: toPct(v[key]),
      format: (x) => x + "%",
      onchange: (x) => { const c = {}; c[key] = fromPct(x); saveViz(c, title + " " + x + "%."); } });
    const plain = (x) => x / 100;
    blocks.push(fold("Music visualiser", [group("", boxed([
      row({ title: "Visualiser", sub: "Drives the ring from the music while Music Assistant plays.",
            end: switchEl(v.enabled > 0, (on) => saveViz({ enabled: on ? 1 : 0 },
              on ? "Visualiser on." : "Visualiser off.")) }),
      slider("Contrast", "Higher keeps quiet bands dark so loud ones stand out.", "gamma",
             (x) => 1 + 2 * x / 100, (g) => Math.round(((g || 1) - 1) / 2 * 100)),
      slider("Beat punch", "How much a drum hit lifts the whole ring.", "punch", plain, pct),
      slider("Sweep", "A pulse that travels round the ring on the beat.", "motion", plain, pct),
      slider("Steadiness", "Holds the overall level steady so the bands show against each other.",
             "relative", plain, pct),
      slider("Top fade", "How gradually the two colours meet opposite the split.", "fade_top", plain, pct),
      slider("Bottom fade", "How gradually they meet at the split.", "fade_bottom", plain, pct),
      row({ title: "Even colours", sub: "Picks the second colour to match the first in brightness.",
            end: switchEl(v.balance > 0, (on) => saveViz({ balance: on ? 1 : 0 },
              on ? "Matching the colours." : "Using the track's own colours.")) }),
    ]))], "Music Assistant sends the spectrum and a colour for each track; these decide how " +
          "the ring draws them."));
  }

  /* --- try a pattern, and what is showing --- */
  {
    // The same list as an activity's, less what only an activity can mean.
    const opts = effectOpts().concat(stockOpts());
    const colBox = el('<div class="pattern-colours"></div>');
    const manual = { key: "__manual__", label: "Ring", sel: null, colBox, colours: [], original: null, dirty: false };
    manual.sel = selectEl(opts, opts.length ? opts[0].value : "", () => { manual.dirty = true; syncColourManual(); });
    function syncColourManual() {
      // The same colour wells as an activity row, without saving anything.
      colBox.replaceChildren(); manual.colours = [];
      if (!manual.sel.value.startsWith("effect:")) return;
      const effect = S.effects.find((e) => e.value === manual.sel.value.slice(7));
      if (!effect) return;
      effect.colour_roles.forEach((label, i) => {
        const input = el('<input type="color">');
        input.value = rgbHex(effect.colour_defaults[i]);
        input.title = label;
        const w = el("<label></label>"); w.append(input); colBox.append(w);
        manual.colours.push(input);
      });
    }
    syncColourManual();
    const nowRow = row({ icon: "ring", title: "Showing now", value: "—", sub: "Reading the ring's state…" });
    const tryRow = row({ title: "Show a pattern", preview: browse(null, manual), previewEnd: browseEnd,
      sub: "Plays it now without changing any animation. Stop hands the ring back.",
      end: [manual.sel, colBox,
            btn("Play", { icon: "play", cls: "small", onclick: async () => {
              try {
                await api("/api/preview", Object.assign({ hold: true }, specOf(manual)));
                const o = manual.sel.options[manual.sel.selectedIndex];
                toast("Showing " + (o ? o.textContent : "that pattern") + ".");
              } catch (e) { toast(e.message, true); }
            } }),
            btn("Stop", { cls: "small flat", onclick: async () => {
              try { await api("/api/preview", { stop: true }); toast("Ring released."); }
              catch (e) { toast(e.message, true); }
            } })] });
    tryRow.classList.add("ring-choice");
    blocks.push(fold("Try and troubleshoot", [group("", boxed([tryRow, nowRow]))],
      "Only the highest-priority animation is drawn, so when one does not appear, " +
      "\"Showing now\" says what is holding it down."));
    pollInto(() => api("/api/ringstate"), (r) => {
      const val = nowRow.querySelector(".val"), sub = nowRow.querySelector(".sub");
      if (!r.running) { val.textContent = "Not running"; sub.textContent = "The light ring service is not publishing its state."; return; }
      if (!r.visible) { val.textContent = "Idle"; sub.textContent = "Nothing is playing."; return; }
      val.textContent = r.visible_label || r.visible;
      const masked = (r.active || []).filter((e) => !e.visible);
      const bits = ["layer " + r.layer];
      if (r.persistent) bits.push("holds until stopped");
      if (masked.length) bits.push("covering " + masked.map((e) => esc(e.label || e.name) + " (layer " + e.layer + ")").join(", "));
      sub.innerHTML = bits.join(" · ");
    }, 1500);
  }

  const resetBtn = btn("", { cls: "flat", icon: "refresh", aria: "Reset every animation", onclick: async () => {
    if (!await confirmDialog("Reset every animation?",
          "Every activity goes back to this Echo's default animation.", "Reset", true)) return;
    try { await api("/api/leds", {}); toast("Animations reset."); reload(); }
    catch (e) { toast(e.message, true); }
  } });

  return { title: "Light ring", body: blocks, actions: [resetBtn] };
};
/* ===================================================================== *
 * Account and security
 *
 * The account and SSH on one page: they are the same login. The SSH part
 * exists so a key can be installed WITHOUT a password login, because OpenSSH
 * cannot supply a password non-interactively. Everything destructive is
 * guarded by the backend rather than confirmed away: it refuses to remove the
 * last key while password sign-in is off, and refuses to turn password sign-in
 * off while no key is installed.
 * ===================================================================== */
async function changePassword(text) {
  return dialog({
    title: "Change password", okLabel: "Change", text,
    fields: [
      { name: "current", label: "Current password", type: "password", autocomplete: "current-password" },
      { name: "next", label: "New password", type: "password", autocomplete: "new-password" },
      { name: "confirm", label: "Repeat new password", type: "password", autocomplete: "new-password" },
    ],
    onsubmit: async (v) => {
      if (v.next.length < 8) throw new Error("Choose at least 8 characters.");
      if (v.next !== v.confirm) throw new Error("The two new passwords do not match.");
      await api("/api/account", { action: "password", current: v.current, new: v.next }, { seal: true });
    },
  });
}

PAGES["security"] = async function () {
  const [s, about] = await Promise.all([api("/api/ssh"), api("/api/about")]);
  const u = about.user || {};
  const keys = s.keys || [];
  const blocks = [];

  if (u.default_password) {
    blocks.push(group("", boxed([
      row({ icon: "lock", title: "This account still uses the default password",
            badge: "Change this", badgeKind: "warn",
            sub: "<b>" + esc(u.username) + "</b> signs in with postmarketOS's published " +
                 "default, so anyone who can reach this device can sign in to this page " +
                 "and over SSH.",
            end: btn("Change", { cls: "small suggested", onclick: async () => {
              if (await changePassword("This is the login for this page and for SSH.")) {
                toast("Password changed."); reload();
              }
            } }) }),
    ])));
  }
  if (u.stock_account_active) {
    blocks.push(group("", boxed([
      row({ icon: "lock", title: "The default account is still enabled",
            badge: "Check this", badgeKind: "warn",
            sub: "postmarketOS ships an account called <b>user</b> with a published default " +
                 "password. Setup normally locks it; until then anyone on this network can " +
                 "sign in as it." }),
    ])));
  }

  if (u.exists) {
    blocks.push(group("Account", boxed([
      row({ icon: "user", title: u.username, badge: u.admin ? "Administrator" : null,
            badgeKind: "accent", sub: "Signs in to this page and over SSH." }),
      row({ title: "Display name", value: u.full_name || "Not set",
            onclick: async () => {
              const r = await dialog({
                title: "Display name", okLabel: "Save",
                text: "Shown here only. It does not change how you sign in.",
                fields: [{ name: "full_name", label: "Name", value: u.full_name }],
                onsubmit: (v) => api("/api/account", { action: "full_name", full_name: v.full_name }),
              });
              if (r) { toast("Name updated."); reload(); }
            } }),
      row({ title: "Password",
            sub: u.password_age_days != null
              ? "Last changed " + (u.password_age_days === 0 ? "today"
                  : u.password_age_days + (u.password_age_days === 1 ? " day ago" : " days ago"))
              : "The one you chose in setup.",
            end: btn("Change", { cls: "small", onclick: async () => {
              if (await changePassword()) toast("Password changed.");
            } }) }),
    ])));
  }

  if (!s.available) {
    blocks.push(group("SSH", boxed([row({ icon: "lock", title: "Not installed",
      sub: "This image has no SSH server." })])));
  } else {
    blocks.push(group("SSH", boxed([
      row({ icon: "lock", title: "SSH",
            sub: s.running ? "On. Sign in as <b>" + esc(s.username) + "</b>."
                           : "Off. The command line is not reachable over the network.",
            end: switchEl(!!s.running, async (on) => {
              // Turning it on opens a way in, so it asks for the password;
              // turning it off does not.
              if (on) {
                const ok = await withPassword("Turn on SSH?",
                  "The command line becomes reachable over the network, as <b>" + esc(s.username) + "</b>.", "Turn on",
                  (password) => api("/api/ssh", { action: "enabled", enabled: true, password }, { seal: true }));
                if (ok) toast("SSH on.");
                reload();
                return;
              }
              try {
                const r = await api("/api/ssh", { action: "enabled", enabled: false });
                toast(r.message || "SSH off."); reload();
              } catch (e) { toast(e.message, true); }
            }) }),
      row({ title: "Password sign-in",
            sub: s.password_auth
              ? (keys.length ? "Allowed. Turn it off once your key works."
                             : "Allowed. It is the only way in until you add a key.")
              : "Off: keys only.",
            end: switchEl(!!s.password_auth, async (on) => {
              if (on) {
                const ok = await withPassword("Allow password sign-in?",
                  "SSH then accepts this account's password as well as its keys.", "Allow",
                  (password) => api("/api/ssh", { action: "password_auth", enabled: true, password }, { seal: true }));
                if (ok) toast("Password sign-in allowed.");
                reload();
                return;
              }
              try {
                const r = await api("/api/ssh", { action: "password_auth", enabled: false });
                toast(r.message || "Updated."); reload();
              } catch (e) { toast(e.message, true); }
            }) }),
    ]), s.password_auth && !keys.length
          ? "Add a key before turning password sign-in off. With neither, there is no " +
            "way back in." : null));

    const keyRows = keys.map((k) => row({
      icon: "check", title: k.comment || (k.type ? k.type + " key" : "Key"),
      sub: '<span class="mono">' + esc(k.fingerprint) + "</span>" +
           (k.type ? " · " + esc(k.type) : "") + (k.bits ? " · " + esc(k.bits) + " bits" : ""),
      end: btn("Remove", { cls: "small flat destructive", onclick: async () => {
        try {
          const r = await api("/api/ssh", { action: "remove_key", fingerprint: k.fingerprint });
          toast(r.message || "Key removed."); reload();
        } catch (e) { toast(e.message, true); }
      } }),
    }));
    if (!keyRows.length) {
      keyRows.push(row({ icon: "info", title: "No keys yet",
        sub: "Paste the contents of your public key file, normally ~/.ssh/id_ed25519.pub - " +
             "never the file without .pub on the end." }));
    }
    keyRows.push(row({ icon: "plus", title: "Add a key", onclick: async () => {
      const r = await dialog({
        title: "Add an SSH key", okLabel: "Add",
        text: "Paste your <b>public</b> key, usually <code>~/.ssh/id_ed25519.pub</code>. It " +
              "starts with <code>ssh-ed25519</code> or <code>ssh-rsa</code>.",
        fields: [{ name: "key", label: "Public key", placeholder: "ssh-ed25519 AAAAC3... you@laptop" },
                 { name: "password", label: "Your password", type: "password", autocomplete: "current-password" }],
        onsubmit: (v) => {
          if (!v.password) throw new Error("Enter your password.");
          return api("/api/ssh", { action: "add_key", key: v.key, password: v.password }, { seal: true });
        },
      });
      if (r) { toast("Key added."); reload(); }
    } }));
    blocks.push(group("SSH keys", boxed(keyRows),
      "Anyone with the matching private key can sign in as " + esc(s.username) + "."));

    const info = [];
    if (s.host_fingerprint) {
      info.push(row({ title: "This device's fingerprint",
        sub: '<span class="mono">' + esc(s.host_fingerprint) + "</span>" }));
    }
    info.push(row({ icon: s.persisted ? "check" : "info",
      title: s.persisted ? "Kept through updates" : "Not being kept",
      sub: s.persisted
        ? "Keys and this device's identity survive an update or reinstall, so SSH never " +
          "asks you to accept a new fingerprint."
        : "The settings store is not mounted, so keys added now are lost at the next update." }));
    blocks.push(group("Identity", boxed(info)));
  }

  blocks.push(group("", boxed([
    row({ icon: "lock", title: "Sign out", onclick: () => { location.href = "/logout"; } }),
  ])));
  return { title: "Security", body: blocks };
};
PAGES["ssh"] = PAGES["security"];





</script>

<script>
"use strict";

/* ===================================================================== *
 * Shared by the Apps and Processes pages
 * ===================================================================== */
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* An app's tile: its first letter, tinted for apps the owner installed. */
function tile(p, big) {
  const name = String(p.title || p.name || "?").replace(/^device-amazon-/, "");
  return el('<div class="tile' + (p.kind === "user" ? " user" : "") + (big ? " big" : "") +
            '">' + esc(name.charAt(0)) + "</div>");
}

/* How a service is doing, as a dot and a few words. */
function svcState(s) {
  if (s.app_state === "paused") return { dot: "warn", text: "Paused until restart" };
  if (s.app_state === "off") return { dot: "off", text: "Off" };
  if (s.oneshot) return { dot: "idle", text: "Ran at start-up" };
  if (s.started) return { dot: "on", text: "Running" };
  return { dot: "off", text: "Stopped" };
}

function fnLabel(inv, id) {
  return ((inv.functions || []).find((f) => f.id === id) || { label: id }).label;
}

function serviceRow(s, inv) {
  const st = svcState(s);
  const bits = [esc(st.text)];
  if (s.pids.length) bits.push(bytes(s.mem) + (s.cpu ? " · " + s.cpu + "% CPU" : ""));
  return row({ lead: el('<div class="dot ' + st.dot + '"></div>'), title: s.name,
               sub: bits.join(" · ") + (s.desc ? "<br>" + esc(s.desc) : ""),
               onclick: () => serviceMenu(s, inv) });
}

/* What can be done to one service, and what else it would take with it. */
async function serviceMenu(s, inv) {
  const running = (names) => names.filter((n) => inv.services[n] && inv.services[n].started);
  const text = [];
  text.push("From <b>" + esc(s.package || "an unknown package") + "</b>.");
  if (s.functions.length) text.push("Part of " + esc(s.functions.map((f) => fnLabel(inv, f)).join(", ")) + ".");
  if (s.need.length) text.push("Needs " + esc(s.need.join(", ")) + ".");
  const deps = running(s.needed_by);
  if (deps.length) text.push("Needed by " + esc(deps.join(", ")) + ".");
  const acts = [];
  if (s.control === "none") {
    text.push("<br><br>Part of the system; not controlled from here.");
  } else {
    const live = s.started && !s.oneshot;
    if (live || s.app_state === "on") {
      acts.push({ label: "Restart", value: "restart",
                  sub: deps.length ? "Restarts " + deps.join(", ") + " with it" : "" });
    }
    if (s.control === "full") {
      if (live) {
        acts.push({ label: "Stop", value: "stop", destructive: true,
                    sub: (deps.length ? "Also stops " + deps.join(", ") + ". " : "") +
                         "Until the next restart." });
      } else if (!s.oneshot) {
        acts.push({ label: "Start", value: "start" });
      }
      if (s.app) {
        acts.push(s.app_state === "off"
          ? { label: "Start at boot", value: "boot_on" }
          : { label: "Turn off", value: "boot_off", destructive: true,
              sub: "Stops it and keeps it from starting at boot" });
      }
    }
    if (s.control === "restart") text.push("<br><br>Only restart is offered: stopping it would lock you out.");
  }
  const a = await actionSheet(s.name, text.join(" "), acts);
  if (a) await runService(s.name, a);
}

/* Start a service action and follow it to the end. */
async function runService(name, action) {
  try { await api("/api/service", { name, action }); }
  catch (e) { toast(e.message, true); return; }
  toast("Working…");
  for (let i = 0; i < 90; i++) {
    await wait(1000);
    let j;
    try { j = (await api("/api/inventory")).job; } catch (e) { continue; }
    if (!j) break;           // this page restarted, and with it the record
    if (j.state !== "running") { toast(j.message || "Done.", j.state === "failed"); break; }
  }
  reload();
}

/* The packages behind a function, as one line: its own first. */
function pkgLine(f) {
  const own = Object.keys(f.packages || {});
  const more = Object.keys(f.relied_packages || {}).filter((p) => !own.includes(p));
  return esc(own.join(", ")) + (more.length ? "<br>Relies on " + esc(more.join(", ")) : "");
}

const FN_DOT = { running: "on", partial: "warn", stopped: "off", "not-installed": "idle" };
const FN_TEXT = { running: "Running", partial: "Partly running", stopped: "Stopped",
                  "not-installed": "Not installed" };


/* ===================================================================== *
 * Sound: volume, the device's sounds, tone and ducking, on one page
 * ===================================================================== */
PAGES["sound"] = async function () {
  const [a, s] = await Promise.all([api("/api/audio"), api("/api/sounds")]);
  const save = async (body, msg) => {
    try { await api("/api/audio", body); if (msg) toast(msg); }
    catch (e) { toast(e.message, true); }
  };
  const blocks = [];

  // The equaliser corrects THIS device's speaker. While audio goes out over
  // Bluetooth it is not in the path, and live-looking sliders that do nothing
  // are worse than a sentence saying so.
  if (a.cast_active) {
    blocks.push(group("", boxed([
      row({ icon: "bluetooth", title: "Playing through a Bluetooth speaker",
            badge: "Casting", badgeKind: "accent",
            sub: "Tone applies to this device's own speaker, so it is not in use now. Volume still works.",
            end: btn("Manage", { cls: "small", onclick: () => { location.hash = "#/bluetooth"; } }) }),
    ])));
  }

  // Controls the poll below must leave alone: one being dragged, one a
  // keyboard user is on, and one just changed - until the device has had a
  // moment to publish the new value, the old one would snap it back.
  const heldUntil = new Map();
  const hold = (c) => heldUntil.set(c, Date.now() + 3000);
  const held = (c) => !c || c._dragging || (heldUntil.get(c) || 0) > Date.now() ||
    (document.activeElement === c && c.matches(":focus-visible"));
  const guard = (c) => {
    if (!c) return c;
    c.addEventListener("pointerdown", () => { c._dragging = true; });
    ["pointerup", "pointercancel", "blur"].forEach((ev) =>
      c.addEventListener(ev, () => { c._dragging = false; }));
    ["input", "change"].forEach((ev) => c.addEventListener(ev, () => hold(c)));
    return c;
  };
  const setSlider = (r, v, format) => {
    if (!r || held(r.rangeEl) || Number(r.rangeEl.value) === v) return;
    r.rangeEl.value = v;
    r.querySelector("output").textContent = format(v);
    r.rangeEl.setAttribute("aria-valuetext", format(v));
  };
  const setChoice = (r, v) => {
    const sel = r && r.querySelector("select");
    if (!sel || held(sel) || sel.value === v) return;
    sel.value = v;
    if (r._sync) r._sync();
  };

  // The 3.5 mm jack, as biscuit-audio publishes it.
  // No-break spaces: a value that wraps onto two lines reads as two values.
  const jackVal = (h) => h === true ? "Plugged in" : (h === false ? "Not plugged in" : "Unknown");
  const jackSub = (j) => {
    let t = j.headphones === true
      ? "Playing through the headphones; the speaker is off. Volume and the equaliser apply to them too."
      : (j.headphones === false
        ? "Plug in headphones or a powered speaker to play through them instead."
        : "The audio service is not reporting the jack.");
    if (j.cast_active) t += " While casting, everything plays on the Bluetooth speaker.";
    return esc(t);
  };
  const jackRow = row({ icon: "head", title: "Headphone jack", value: jackVal(a.headphones), sub: jackSub(a) });

  // The device has thirty steps, and a percentage is only a view of one:
  // biscuit-audio rounds whatever it is sent to round(pct * 30 / 100). A 0-100
  // slider in fives offered values it cannot hold (5% plays as 7%) and could
  // not show the ones it does (23%). So the slider moves in steps and shows
  // each step as the percentage the device reports for it.
  const pct = (level) => Math.round(level * 100 / 30);
  const step = (p) => Math.round(p * 30 / 100);
  const volFmt = (level) => pct(level) + "%";
  const volRow = a.volume == null
    ? row({ icon: "speaker", title: "Volume", sub: "The audio service is not running." })
    : sliderRow({ title: "Volume", min: 0, max: 30, step: 1, value: step(a.volume),
                  format: volFmt, onchange: (level) => save({ volume: pct(level) }) });
  guard(volRow.rangeEl);
  blocks.push(group("Volume", boxed([jackRow, volRow])));

  /* --- the device's sounds --- */
  // After sounds are chosen one by one the set in force is "Custom": shown as
  // the current value, which cannot be chosen, so choosing the set whose name
  // is stored is a change and puts it back.
  const setOpts = (cur) => s.sets.includes(cur) ? s.sets
    : [{ value: cur, label: cur, disabled: true }].concat(s.sets);
  const setRow = row({ icon: "note", title: "Sounds",
    sub: "Amazon: the sounds this Echo came with. Assistant defaults: the voice assistant's " +
         "own sounds for its parts. Off: silent.",
    end: selectEl(setOpts(s.set), s.set, async (v) => {
      try {
        await api("/api/sounds", { set: v });
        toast("Sounds: " + v + ".");
        reloadSoon(1200);
      } catch (e) { toast(e.message, true); }
    }) });
  blocks.push(group("Sounds", boxed([setRow])));

  const playSound = async (name) => {
    if (!name) { toast("That choice is silent."); return; }
    try { await api("/api/sounds", { preview: name }); } catch (e) { toast(e.message, true); }
  };
  const soundGroups = [];
  let currentGroup = null, rows = [];
  const flush = () => { if (rows.length) soundGroups.push(group(currentGroup, boxed(rows))); rows = []; };
  s.activities.forEach((act) => {
    if (act.group !== currentGroup) { flush(); currentGroup = act.group; }
    const defaults = [].concat(act.default || []).join(" + ");
    const options = [{ value: "__default__", label: "Default" + (defaults ? " (" + defaults + ")" : " (silent)") },
                     { value: "__silent__", label: "Silent" }]
      .concat(s.available.map((n) => ({ value: n, label: n })));
    // The stored value is an override; absent means default. Silence is
    // stored explicitly, so it is a value rather than an absence.
    let value = "__default__";
    if (act.sound && act.sound !== act.default) value = act.sound;
    else if (!act.sound && act.default) value = "__silent__";
    const named = (v) => v === "__default__" ? act.default : (v === "__silent__" ? "" : v);
    // The assistant plays its thinking sound only while Home Assistant's
    // Thinking Sound switch is on, whatever is chosen here - and with the
    // switch on and silence chosen here, the switch seems to do nothing.
    const note = (v) => act.needs_assistant ? "Needs the voice assistant"
      : act.key !== "thinking" ? null
      : s.thinking_sound === false ? "Plays only when Thinking Sound is on in Home Assistant, and it is off."
      : s.thinking_sound === true && !named(v)
        ? "Thinking Sound is on in Home Assistant, but no sound is chosen here: choose one" +
          (s.available.includes("processing") ? ", such as processing," : "") + " to hear it."
      : null;
    let r = null;
    const sel = selectEl(options, value, async (v) => {
      try {
        const res = await api("/api/sounds", { sounds: { [act.key]: v } });
        toast(act.label + (res.assistant_restart ? ": saved. The voice assistant restarts to use it." : ": saved."));
      } catch (e) { toast(e.message, true); }
      if (act.key === "thinking") {
        let subEl = r.querySelector(".sub");
        if (!subEl) subEl = r.querySelector(".txt").appendChild(el('<div class="sub"></div>'));
        subEl.textContent = note(sel.value) || "";
        subEl.hidden = !subEl.textContent;
      }
    }, !!act.needs_assistant);
    const play = btn("", { cls: "flat", icon: "play", aria: "Play " + act.label, onclick: () => playSound(named(sel.value)) });
    r = row({ title: act.label, sub: note(value), end: [sel, play], preview: (v) => playSound(named(v)) });
    rows.push(r);
  });
  flush();
  blocks.push(fold("Individual sounds", soundGroups,
    "What plays for each thing the device does. Choosing one plays it; OK keeps it."));

  /* --- tone --- */
  // The bands only mean anything in Custom, but they are shown always: moving
  // one switches the mode to Custom.
  // The factory curve is the owner's own file, imported from their backup,
  // never shipped. Without it Stock has nothing to apply and plays flat.
  const modeRow = row({ title: "Equaliser",
    sub: (a.eq_curve
          ? "Stock is Amazon's correction for this speaker; Off removes it. "
          : "No factory curve is imported, so Stock plays flat, the same as Off. Import " +
            'EQ_50.cfg from your backup under <a href="#/stock">Files from stock</a>. ') +
         "Moving a slider below switches to Custom.",
    end: selectEl(a.eq_modes, a.eq.eq_mode, async (v) => {
      await save({ eq: { mode: v } }, "Equaliser: " + v + ".");
      reload();
    }) });
  const bands = ["bass", "mid", "treble"].map((band) => {
    const label = band[0].toUpperCase() + band.slice(1);
    return sliderRow({ title: label, min: a.eq_min, max: a.eq_max, step: 0.5,
      value: a.eq["eq_" + band], format: db,
      onchange: async (v) => {
        const body = { eq: { mode: "Custom" } };
        body.eq[band] = v;
        await save(body, label + " " + db(v));
        const sel = modeRow.querySelector("select");
        if (sel) { sel.value = "Custom"; sel.dispatchEvent(new Event("change")); }
      } });
  });
  blocks.push(group("Tone", boxed([modeRow].concat(bands)),
    a.eq_curve ? "The bands add to Amazon's correction rather than replacing it."
               : "The bands shape the flat speaker; with a factory curve imported they add to it."));

  // Each control sends only its own field and the server merges it into
  // duck.json. Sending both, from the values the page was loaded with, let
  // either control quietly put back what the other had just changed.
  const duckSwitch = switchEl(a.duck_enabled, (on) =>
    save({ duck_enabled: on }, on ? "On." : "Off."));
  const duckRow = sliderRow({ title: "Turned down to", min: 0, max: 100, step: 5, value: a.duck_level,
                sub: "Of each stream's own volume", format: (v) => v + "%",
                onchange: (v) => save({ duck_level: v }, "Turned down to " + v + "%.") });
  guard(duckSwitch.querySelector("input"));
  guard(duckRow.rangeEl);
  blocks.push(group("While you speak", boxed([
    row({ title: "Turn other audio down",
          sub: "Music and Bluetooth get quieter while the assistant is listening, so it hears you.",
          end: duckSwitch }),
    duckRow,
  ])));

  /* --- follow changes made elsewhere --- */
  // Home Assistant, the volume buttons and a plug all change what this page
  // shows. Updated in place, never re-rendered, and never a control that is in
  // use - see held() above.
  [setRow, modeRow].forEach((r) => guard(r.querySelector("select")));
  bands.forEach((r) => guard(r.rangeEl));
  pollInto(() => api("/api/audio/live"), (x) => {
    if (!$("scrim").hidden) return;             // a choice is being made
    // A change of shape - the audio service starting or stopping, casting
    // starting or ending - is a new page, not a new value.
    if ((x.volume == null) !== (a.volume == null) || !!x.cast_active !== !!a.cast_active) {
      render(); return;
    }
    jackRow.querySelector(".val").textContent = jackVal(x.headphones);
    jackRow.querySelector(".sub").innerHTML = jackSub(x);
    if (x.volume != null) setSlider(volRow, step(x.volume), volFmt);
    const sw = duckSwitch.querySelector("input");
    if (!held(sw)) sw.checked = !!x.duck_enabled;
    setSlider(duckRow, x.duck_level, (v) => v + "%");
    setChoice(modeRow, x.eq.eq_mode);
    ["bass", "mid", "treble"].forEach((band, i) => setSlider(bands[i], x.eq["eq_" + band], db));
    // "Custom" comes and goes as sounds are chosen here or a set in Home
    // Assistant, so its option does too.
    const setSel = setRow.querySelector("select");
    if (setSel && x.earcon_set && !held(setSel) && setSel.value !== x.earcon_set) {
      const cur = [...setSel.options].find((o) => o.disabled);
      if (cur && cur.value !== x.earcon_set) cur.remove();
      if (!s.sets.includes(x.earcon_set) && !(cur && cur.value === x.earcon_set))
        setSel.prepend(el('<option value="' + esc(x.earcon_set) + '" disabled>' + esc(x.earcon_set) + "</option>"));
      setSel.value = x.earcon_set;
      if (setRow._sync) setRow._sync();
    }
  }, 2000);

  return { title: "Sound", body: blocks };
};
PAGES["sounds"] = PAGES["sound"];

/* ===================================================================== *
 * Apps
 *
 * Every package that runs something here, as Android lists apps: the ones
 * the owner installed, and the system's. Named by what they are - sendspin,
 * pipewire - with the exact package beneath.
 * ===================================================================== */
function pkgRow(p) {
  const bits = [];
  if (p.name !== p.title) bits.push('<span class="mono">' + esc(p.name) + "</span>");
  bits.push(esc(p.ours ? rel(p.version) : p.version));
  let state = "";
  if (p.services.length) {
    state = p.running === p.services.length ? "Running"
          : (p.running ? p.running + " of " + p.services.length + " services running" : "Stopped");
  } else if (p.processes) {
    state = "Running";
  }
  const line2 = [state, p.mem ? bytes(p.mem) + " memory" : "", bytes(p.size) + " installed"].filter(Boolean);
  return row({ lead: tile(p), title: p.title,
               badge: p.update ? (p.ours ? "Update" : "Newer version") : null,
               badgeKind: p.ours ? "accent" : "",
               sub: bits.join(" · ") + "<br>" + esc(line2.join(" · ")),
               go: "#/app/" + encodeURIComponent(p.name) });
}

PAGES["apps"] = async function () {
  const [inv, apps] = await Promise.all([api("/api/inventory"), api("/api/apps")]);
  const pk = Object.values(inv.packages);
  const filter = window.__appsFilter || "all";
  // `running` is any package job, `job` the last app install or removal in
  // its own record - so an update check that ran since cannot hide how an
  // install ended, and Install waits while the check runs instead of failing.
  const running = apps.running;
  const busy = !!apps.busy || !!running;
  const appRun = running && ["add", "del"].includes(running.action) ? running : null;
  // Only an install or removal. apps-status never holds anything else, but
  // before r295 `job` was the shared status's last job - a check among them -
  // and a capture of that (the settings mock replays them) drew a removal
  // that never happened.
  const job = apps.job && ["add", "del"].includes(apps.job.action) && !appRun ? apps.job : null;
  const p = apps.pending;
  const pendingPkgs = new Set(p ? p.packages : []);
  const blocks = [];
  if (busy || p) {
    pollPage(() => api("/api/apps"), (d) => JSON.stringify([
      d.busy, d.running && [d.running.action, d.running.message], d.job && d.job.id,
      d.pending && [d.pending.state, d.pending.waiting, d.pending.packages, Math.round((d.pending.retry_in || 0) / 300)]]),
      busy ? 3000 : 15000);
  }

  const counts = {
    all: pk.length, user: pk.filter((p) => p.kind === "user").length,
    system: pk.filter((p) => p.kind === "system").length, updates: pk.filter((p) => p.update).length,
  };
  const chips = el('<div class="chips"></div>');
  [["all", "All"], ["user", "Yours"], ["system", "System"], ["updates", "Updates"]].forEach(([k, l]) => {
    chips.appendChild(btn(l + " " + counts[k], { cls: k === filter ? "on" : "",
      onclick: () => { window.__appsFilter = k; render(); } }));
  });
  blocks.push(chips);

  const top = [];
  if (appRun) {
    top.push(row({ icon: "refresh", badge: "Working", badgeKind: "accent",
      title: (appRun.action === "add" ? "Installing " : "Removing ") + andList(appRun.labels),
      sub: (appRun.message ? esc(appRun.message) + ". " : "") +
           "This can take a few minutes. You can leave this page." }));
  } else if (running) {
    top.push(row({ icon: "refresh", title: JOB_WORDS[running.action] || "Working", badge: "Working",
      badgeKind: "accent", sub: "Apps can be installed or removed once this has finished.", go: "#/updates" }));
  }
  // How the last install or removal ended - unless it is the failure of the
  // apps chosen at setup, which their own group below explains.
  if (job && job.finished && !(p && job.action === "add" && job.state === "failed" &&
                               job.packages.some((x) => pendingPkgs.has(x)))) {
    const ok = job.state === "ok";
    const add = job.action === "add";
    top.push(row({ icon: ok ? "check" : "info",
      title: (ok ? (add ? "Installed " : "Removed ") : (add ? "Could not install " : "Could not remove ")) +
             andList(job.labels),
      badge: ok ? "Done" : "Failed", badgeKind: ok ? "ok" : "warn",
      sub: esc(job.message || "") + (job.time ? "<br>" + esc(ago(job.time, apps.now)).replace(/^./, (c) => c.toUpperCase()) : "") }));
  }
  if (top.length) blocks.push(group("", boxed(top)));

  // The apps ticked during setup that are not installed yet. They are tried
  // again by themselves; this says why they are not there yet, when the next
  // try is, and offers it now - or not at all.
  // While an install covers some of them, this is about the others: the
  // install itself is the row at the top, and cannot be called off.
  if (p && p.state !== "installing" && (filter === "all" || filter === "user")) {
    const names = p.installing && p.rest ? p.rest.labels : p.labels;
    const one = names.length === 1;
    const [badge, badgeKind] = pendingBadge(p);
    blocks.push(group("Chosen at setup", boxed([
      row({ icon: "download", title: andList(names) + (one ? " is" : " are") + " not installed yet",
            badge, badgeKind, sub: pendingWhy(p, apps.now) }),
      row({ title: "Try again now", sub: busy ? "Waits for the package job running now." : "Instead of waiting for the next try.",
            end: btn("Try now", { cls: "small suggested", disabled: busy, onclick: async (b) => {
              b.disabled = true;
              try { const r = await api("/api/apps", { action: "retry" }); toast(r.message || "Installing…"); }
              catch (e) { toast(e.message, true); }
              reload();
            } }) }),
      row({ title: one ? "Don't install it" : "Don't install them",
            sub: "Stops the tries. " + (one ? "It stays" : "They stay") + " under Available to install.",
            end: btn("Don't install", { cls: "small", onclick: async (b) => {
              if (!await confirmDialog("Don't install " + andList(names) + "?",
                    "This Echo stops trying to install " + (one ? "it" : "them") + ". You can install " +
                    (one ? "it" : "them") + " from this page at any time.", "Don't install")) return;
              b.disabled = true;
              try { const r = await api("/api/apps", { action: "cancel" }); toast(r.message || "Cancelled."); }
              catch (e) { toast(e.message, true); }
              reload();
            } }) }),
    ])));
  }

  const show = (p) => filter === "all" || (filter === "updates" ? !!p.update : p.kind === filter);
  const avail = (apps.apps || []).filter((a) => !a.installed);
  if (avail.length && (filter === "all" || filter === "user")) {
    blocks.push(group("Available to install", boxed(avail.map((a) => row({
      lead: tile({ title: a.name, kind: "user" }), title: a.name,
      badge: a.packages.some((x) => pendingPkgs.has(x)) ? "Chosen at setup" : null,
      sub: esc(a.summary) + '<br><span class="mono">' + esc(a.packages.join(" ")) + "</span> · about " +
           a.size_mb + " MB",
      end: btn("Install", { cls: "small suggested", disabled: busy, onclick: async (b) => {
        b.disabled = true;
        try { const r = await api("/api/apps", { action: "install", id: a.id }); toast(r.message || "Installing…"); }
        catch (e) { toast(e.message, true); }
        reload();
      } }) }))), "Installing downloads it, so it needs an internet connection."));
  }

  const byName = (a, b) => (b.ours - a.ours) || a.title.localeCompare(b.title);
  const user = pk.filter((p) => p.kind === "user" && show(p)).sort(byName);
  const sys = pk.filter((p) => p.kind === "system" && show(p)).sort(byName);
  if (user.length) {
    blocks.push(group("Installed by you", boxed(user.map(pkgRow)),
      "Optional apps. Each can be stopped, cleared or removed."));
  }
  if (sys.length) {
    blocks.push(group("System", boxed(sys.map(pkgRow)),
      "This device's own software and the postmarketOS packages that run here. Their " +
      "services can be restarted; the packages stay."));
  }
  if (!user.length && !sys.length) {
    blocks.push(statusPage("check", filter === "updates" ? "No updates" : "Nothing here",
      filter === "updates" ? 'Nothing newer was found the last time this checked. <a href="#/updates">Check now</a>.' : ""));
  }
  return { title: "Apps", body: blocks };
};
PAGES["services"] = PAGES["apps"];

/* ===================================================================== *
 * One app, as Android's app info: what it is, its services, what it is
 * used for, its storage and data, and everything it is built from.
 * ===================================================================== */
PAGES["app/*"] = async function (param) {
  const legacy = { voice: "device-amazon-biscuit-voice", sendspin: "device-amazon-biscuit-sendspin" };
  const name = legacy[param] || decodeURIComponent(param || "");
  const [d, inv, apps] = await Promise.all([
    api("/api/package?name=" + encodeURIComponent(name)), api("/api/inventory"), api("/api/apps")]);
  const reg = (apps.apps || []).find((a) => (a.packages || []).includes(name));
  const svcs = (d.service_info || []).map((s) => inv.services[s.name] || s);
  const blocks = [];

  /* --- header --- */
  const head = el('<div class="apphead"></div>');
  head.appendChild(tile(d, true));
  head.appendChild(el('<div class="name">' + esc(d.title) + "</div>"));
  head.appendChild(el('<div class="pkg"><span class="mono">' + esc(d.name) + "</span> · " +
                      esc(d.version) + "</div>"));
  head.appendChild(el("<div>" + '<span class="badge ' + (d.kind === "user" ? "accent" : "") + '">' +
    (d.kind === "user" ? "Installed by you" : "System") + "</span>" +
    (d.update ? ' <span class="badge ' + (d.ours ? "accent" : "") + '">' +
                (d.ours ? "Update" : "Newer version") + "</span>" : "") + "</div>"));
  if (d.label) head.appendChild(el('<div class="pkg">' + esc(d.label) + "</div>"));
  blocks.push(head);

  /* --- the three things you most often do --- */
  // A system package used by several parts of the device has no one
  // settings page of its own, so it gets a Settings button only when all of
  // them point at the same one.
  const routes = [...new Set((d.function_info || [])
    .map((f) => (inv.functions.find((x) => x.id === f.id) || {}).route)
    .filter((r) => r !== undefined && r !== null && r !== ""))];
  const route = reg ? reg.settings : (routes.length === 1 ? routes[0] : null);
  const bar = el('<div class="appbar"></div>');
  if (route) bar.appendChild(btn("Settings", { icon: "gear", onclick: () => { location.hash = "#/" + route; } }));
  const own = svcs.filter((s) => s.control === "full" && !s.oneshot);
  if (d.kind === "user" && own.length) {
    const up = own.some((s) => s.started);
    bar.appendChild(btn(up ? "Stop" : "Start", { icon: up ? "stop" : "play", onclick: async () => {
      for (const s of own) {
        if (up ? s.started : !s.started) await runService(s.name, up ? "stop" : "start");
      }
    } }));
  }
  if (d.kind === "user" && reg) {
    bar.appendChild(btn("Remove", { icon: "trash", cls: "destructive", onclick: async () => {
      if (!await confirmDialog("Remove " + d.title + "?",
            "Its files are deleted and it stops. Installing it again downloads it again.",
            "Remove", true)) return;
      try { const r = await api("/api/apps", { action: "remove", id: reg.id }); toast(r.message || "Removing…"); }
      catch (e) { toast(e.message, true); }
      location.hash = "#/apps";
    } }));
  }
  if (d.update && d.ours) {
    bar.appendChild(btn("Update", { icon: "download", onclick: () => { location.hash = "#/updates"; } }));
  }
  if (bar.children.length) blocks.push(bar);

  if (d.update) {
    blocks.push(group("", boxed([d.ours
      ? row({ icon: "download", title: "Update available: " + rel(d.update),
              sub: "The device software and its apps are released together, so they update together.",
              go: "#/updates" })
      : row({ icon: "download", title: "Update available: " + d.update,
              sub: "Updates this package, and anything it needs, from postmarketOS. Python stays " +
                   "at its current version.",
              end: btn("Update", { cls: "small suggested", onclick: async () => {
                const ok = await withPassword("Update " + d.name + "?",
                  "It installs in the background. Restart when it finishes.", "Update",
                  (password) => api("/api/updates", { action: "sysupdate", packages: [d.name], password }, { seal: true }));
                if (!ok) return;
                toast("Updating…");
                location.hash = "#/updates";
              } }) })])));
  }

  /* --- services --- */
  blocks.push(group("Services", svcs.length
    ? boxed(svcs.map((s) => serviceRow(s, inv)))
    : boxed([row({ icon: "info", title: "Runs nothing by itself",
        sub: d.processes ? "Its programs run as part of other services." :
             "Files other packages use: libraries, firmware or configuration." })]),
    svcs.length ? "Tap a service to restart or stop it." : null));

  /* --- used for --- */
  if ((d.function_info || []).length) {
    blocks.push(group("Used for", boxed(d.function_info.map((f) => row({
      lead: el('<div class="dot ' + FN_DOT[f.state] + '"></div>'), title: f.label,
      sub: FN_TEXT[f.state], go: "#/function/" + f.id })))));
  }

  /* --- storage and data --- */
  const sizeRow = row({ title: "Installed", value: bytes(d.size),
    sub: d.kind === "user" ? "This package alone. Measuring what it installs with it…" : "This package alone." });
  const storage = [sizeRow];
  if (reg && reg.clear_data) {
    storage.push(row({ title: "Data", sub: esc(reg.reset),
      end: btn("Clear", { cls: "small destructive", onclick: async () => {
        if (!await confirmDialog("Clear " + d.title + "'s data?",
              esc(reg.reset) + " It stops briefly if it is running.", "Clear", true)) return;
        try { await api("/api/services", { id: reg.id, action: "clear", confirm: true }); toast("Cleared."); }
        catch (e) { toast(e.message, true); }
        reloadSoon(1500);
      } }) }));
  }
  blocks.push(group("Storage", boxed(storage)));
  if (d.kind === "user" && reg) {
    api("/api/apps/detail?id=" + encodeURIComponent(reg.id)).then((x) => {
      if (!x.footprint_ok) return;
      sizeRow.querySelector(".val").textContent = bytes(x.size);
      sizeRow.querySelector(".sub").textContent = "With the " + (x.packages.length - 1) +
        " packages only it uses. Removing it frees this.";
    }).catch(() => {});
  }

  /* --- processes --- */
  if ((d.process_info || []).length) {
    blocks.push(fold("Processes · " + d.process_info.length, [group("", boxed(d.process_info.map((p) => row({
      title: p.program.split("/").pop(),
      sub: "pid " + p.pid + (p.service ? " · " + esc(p.service) : "") + '<br><span class="mono">' +
           esc(p.cmd) + "</span>",
      value: bytes(p.mem) + (p.cpu ? " · " + p.cpu + "%" : "") }))))]));
  }

  /* --- what it is built from --- */
  if (d.app) {
    const app = d.app;
    blocks.push(group("Built on", boxed((app.upstream || []).map((u) =>
      linkRow({ icon: "link", title: u.name, sub: esc(u.version) + " · " + esc(u.license), href: u.url })))));
    blocks.push(fold("Our changes · " + (app.changes || []).length, [group("", boxed((app.changes || []).map((c) =>
      linkRow({ title: c.file, sub: esc(c.why), href: c.url }))))],
      "Every change this device makes to those projects, as the file is in " + esc(rel(d.version)) + "."));
    const run = [];
    run.push(row({ title: "Listens on", sub: (app.ports || []).length
      ? app.ports.map((p) => "Port " + p.port + ": " + esc(p.what)).join("<br>") : "Nothing. It only connects out." }));
    run.push(row({ title: "Talks to", sub: (app.connects_to || []).map(esc).join("<br>") }));
    run.push(row({ title: "Keeps", sub: '<span class="mono">' + (app.data || []).map(esc).join("<br>") + "</span>" }));
    blocks.push(group("On the network", boxed(run)));
  }

  /* --- details --- */
  const det = [];
  if (d.desc) det.push(row({ title: "Description", sub: esc(d.desc) }));
  det.push(row({ title: "Version", value: d.version }));
  if (d.license) det.push(row({ title: "Licence", value: d.license }));
  if (d.links && d.links.tree) det.push(linkRow({ icon: "link", title: "Source code", sub: "As installed", href: d.links.tree }));
  else if (d.url) det.push(linkRow({ icon: "link", title: "Project page", sub: esc(d.url), href: d.url }));
  blocks.push(group("Details", boxed(det)));
  const pkgLink = (n) => row({ title: n, go: "#/app/" + encodeURIComponent(n) });
  if ((d.depends || []).length) {
    blocks.push(fold("Depends on · " + d.depends.length, [group("", boxed(d.depends.map(pkgLink)))],
      "Installed because this package needs them."));
  }
  if ((d.required_by || []).length) {
    blocks.push(fold("Required by · " + d.required_by.length, [group("", boxed(d.required_by.map(pkgLink)))]));
  }

  pollPage(() => api("/api/inventory"), (x) => JSON.stringify(
    (d.services || []).map((n) => x.services[n] && [x.services[n].started, x.services[n].app_state])), 5000);
  return { title: d.title, body: blocks };
};

/* ===================================================================== *
 * Processes
 *
 * What the device is doing, by what it is for: each function with the
 * packages behind it. Its own services and processes come first; "+" lists
 * what it relies on through other services.
 * ===================================================================== */
PAGES["processes"] = async function () {
  const inv = await api("/api/inventory");
  const blocks = [];
  const cpu = inv.processes.reduce((n, p) => n + (p.cpu || 0), 0);
  const memRow = el('<div class="row stack"></div>');
  const pct = Math.round(100 * inv.memory.used / inv.memory.total);
  memRow.appendChild(el('<div class="txt"><div class="title">Memory</div><div class="sub">' +
    bytes(inv.memory.used) + " of " + bytes(inv.memory.total) + " in use · " + pct + "%</div></div>"));
  memRow.appendChild(el('<div class="meter' + (pct >= 90 ? " warn" : "") + '"><i style="width:' + pct + '%"></i></div>'));
  blocks.push(group("", boxed([memRow, row({ title: "Processor", value: Math.round(cpu) + "% in use",
    sub: inv.processes.length + " processes on " + inv.ncpu + " cores" })])));

  const fns = inv.functions.filter((f) => f.state !== "not-installed");
  blocks.push(group("What's running", boxed(fns.map((f) => row({
    lead: el('<div class="dot ' + FN_DOT[f.state] + '"></div>'), title: f.label,
    badge: f.state === "running" ? null : FN_TEXT[f.state], badgeKind: "warn",
    sub: pkgLine(f), value: bytes(f.mem) + (f.cpu >= 1 ? " · " + Math.round(f.cpu) + "%" : ""),
    go: "#/function/" + f.id }))),
    "Each thing the device does, and the packages that make it happen: its own first, " +
    "then what it relies on through another part."));
  const missing = inv.functions.filter((f) => f.state === "not-installed");
  if (missing.length) {
    blocks.push(group("Not installed", boxed(missing.map((f) => row({
      lead: el('<div class="dot idle"></div>'), title: f.label, sub: esc(f.summary), go: "#/apps" })))));
  }

  const others = inv.processes.filter((p) => !p.service && !p.helper &&
    !inv.functions.some((f) => f.processes.includes(p.pid)));
  const top = inv.processes.slice().sort((a, b) => b.mem - a.mem);
  blocks.push(fold("All processes · " + inv.processes.length, [group("", boxed(top.map((p) => row({
    title: (p.program || p.cmd).split("/").pop(),
    sub: "pid " + p.pid + " · " + esc(p.service || (p.session ? "SSH session" : "no service")) +
         (p.package ? " · " + esc(p.package) : ""),
    value: bytes(p.mem) + (p.cpu ? " · " + p.cpu + "%" : ""),
    go: p.package ? "#/app/" + encodeURIComponent(p.package) : null }))))],
    "Largest first. Memory is each process's share, counting libraries shared with others " +
    "once. " + others.length + " belong to no service: the console logins, init and your own sessions."));

  pollPage(() => api("/api/inventory"),
    (x) => JSON.stringify(x.functions.map((f) => f.state)), 6000);
  return { title: "Processes", body: blocks };
};

PAGES["function/*"] = async function (id) {
  const inv = await api("/api/inventory");
  const f = inv.functions.find((x) => x.id === id);
  if (!f) return { title: "Processes", body: statusPage("info", "Nothing called that", "") };
  const blocks = [];
  const fnOf = (svc) => (inv.services[svc].functions || []).filter((x) => x !== f.id).map((x) => fnLabel(inv, x));

  blocks.push(group("", boxed([
    row({ lead: el('<div class="dot ' + FN_DOT[f.state] + '"></div>'), title: f.label,
          badge: FN_TEXT[f.state], badgeKind: f.state === "running" ? "ok" : "warn",
          sub: esc(f.summary), value: bytes(f.mem) }),
    f.route != null && f.route !== undefined ? row({ icon: "gear", title: "Settings", go: "#/" + f.route }) : null,
  ])));

  const runs = f.services.map((n) => serviceRow(inv.services[n], inv));
  inv.processes.filter((p) => f.processes.includes(p.pid)).forEach((p) => runs.push(row({
    lead: el('<div class="dot on"></div>'), title: (p.program || p.cmd).split("/").pop(),
    sub: "Running · " + bytes(p.mem) + ' · started by the system<br><span class="mono">' + esc(p.cmd) + "</span>" })));
  blocks.push(group("Runs", boxed(runs), "Tap a service to restart or stop it."));

  if (f.relies_on.length) {
    blocks.push(group("Relies on", boxed(f.relies_on.map((n) => {
      const r = serviceRow(inv.services[n], inv);
      const also = fnOf(n);
      if (also.length) r.querySelector(".sub").insertAdjacentHTML("beforeend", "<br>Part of " + esc(also.join(", ")));
      return r;
    })), "Other services this one cannot work without. Stopping one of them stops this too."));
  }

  const pkgRows = (map, what) => Object.keys(map).sort().map((n) => {
    const p = inv.packages[n] || { name: n, title: n, kind: "system", version: "" };
    return row({ lead: tile(p), title: p.title,
      sub: (p.name !== p.title ? '<span class="mono">' + esc(p.name) + "</span> · " : "") +
           what + " " + esc(map[n].join(", ")),
      value: p.ours ? rel(p.version) : p.version, go: "#/app/" + encodeURIComponent(n) });
  });
  blocks.push(group("Packages", boxed(pkgRows(f.packages, "runs")),
    "The packages whose programs this runs."));
  const extra = {};
  Object.keys(f.relied_packages).forEach((n) => { if (!f.packages[n]) extra[n] = f.relied_packages[n]; });
  if (Object.keys(extra).length) {
    blocks.push(group("Through what it relies on", boxed(pkgRows(extra, "in"))));
  }
  const app = f.app && Object.values(inv.packages).find((p) => p.app_id === f.app);
  if (app) {
    blocks.push(group("", boxed([row({ icon: "info", title: "Libraries it installs",
      sub: "The packages installed with " + esc(app.title) + ", on its app page.",
      go: "#/app/" + encodeURIComponent(app.name) })])));
  }

  pollPage(() => api("/api/inventory"), (x) => JSON.stringify(
    f.services.concat(f.relies_on).map((n) => x.services[n] && [x.services[n].started, x.services[n].app_state])), 5000);
  return { title: f.label, body: blocks };
};
</script>

<script>
"use strict";

/* ===================================================================== *
 * Search
 *
 * Every setting, by the words someone would look for it by. Written down
 * rather than found by rendering every page: that would mean a dozen API calls
 * per keystroke. Apps, services and what the device does are added from the
 * inventory, which is on the device already.
 * ===================================================================== */
const SEARCH = [
  // [title, route, where, other words, label on the page if not the title]
  ["Network & internet", "wifi", "", "wifi wireless internet", ""],
  ["Wi-Fi", "wifi", "Network & internet", "wireless network ssid password join signal"],
  ["Join a hidden network", "wifi", "Network & internet › Wi-Fi", "ssid"],
  ["Saved networks", "wifi", "Network & internet › Wi-Fi", "forget"],
  ["IP address", "wifi", "Network & internet › Wi-Fi", "mac address frequency band"],
  ["Connected devices", "devices", "", "bluetooth usb"],
  ["Bluetooth", "bluetooth", "Connected devices", "pair phone speaker headphones"],
  ["Pair new device", "bluetooth", "Connected devices › Bluetooth", "pairing mode discoverable", "Pairing mode"],
  ["Play through a Bluetooth speaker", "bluetooth", "Connected devices › Bluetooth", "cast headphones output", "Speakers and headphones"],
  ["USB", "usb", "Connected devices", "computer cable rndis network microphone speaker audio gadget charging"],
  ["Home Assistant", "homeassistant", "", "esphome assist voice satellite connection"],
  ["Music Assistant", "homeassistant", "Home Assistant", "sendspin speaker"],
  ["Bluetooth proxy", "homeassistant", "Home Assistant", "ble sensors bthome xiaomi govee"],
  ["Apps", "apps", "", "packages installed uninstall remove system update"],
  ["Storage", "storage", "", "disk space full cache logs"],
  ["Files from stock", "stock", "Storage", "firmware fire os afe amazon backup import earcons microphone files speaker correction curve"],
  ["Sounds from stock", "stock", "Storage › Files from stock", "earcons amazon chimes", "Sounds"],
  ["Ring animations from stock", "stock", "Storage › Files from stock", "led light ring animations amazon fire os import", "Light ring animations"],
  ["Package cache", "storage", "Storage", "apk clear cache"],
  ["Sound", "sound", "", "audio"],
  ["Volume", "sound", "Sound", "loud quiet"],
  ["Headphone jack", "sound", "Sound › Volume", "headphones wired 3.5mm aux plug output"],
  ["Sounds", "sound", "Sound", "earcons chimes sound set amazon assistant"],
  ["Individual sounds", "sound", "Sound › Sounds", "wake word timer alarm earcon"],
  ["Equaliser", "sound", "Sound › Tone", "eq bass mid treble stock custom"],
  ["Turn other audio down", "sound", "Sound › While you speak", "ducking duck quieter"],
  ["Microphone", "mic", "", "mics"],
  ["Mute", "mic", "Microphone", "mute button privacy"],
  ["Mute light", "mic", "Microphone", "red led"],
  ["Processing", "mic", "Microphone › Tuning", "beamforming pmos fire os 8-beam profile chain"],
  ["Voice detector", "mic", "Microphone › Tuning", "vad dnn energy"],
  ["Echo cancellation", "mic", "Microphone › Tuning", "aec"],
  ["Input gain", "mic", "Microphone › Tuning", "pga preamp level"],
  ["Calibration", "mic", "Microphone › Tuning", "miccal factory"],
  ["Call processing", "mic", "Microphone › Calls", "hands-free hfp phone call stock-derived tuning"],
  ["Light ring", "ring", "", "led lights"],
  ["Ring brightness", "ring", "Light ring", "dim ambient light sensor autodim room", "Brightness"],
  ["Direction light", "ring", "Light ring", "point speaker noise"],
  ["Animations", "ring", "Light ring", "effects patterns activities ambient"],
  ["Fire OS animations", "ring", "Light ring › Animations", "amazon stock ambient disco fireflies jellyfish lava flow rainbow wave comets shooting stars magenta pulse paparazzi turbo boost tortoise hare overlappers", "Animations"],
  // Every word of every name in biscuit-ring-fx.py's EFFECTS: a new effect
  // adds its words here.
  ["Built-in effects", "ring", "Light ring › Animations",
   "arc spin inverse blink blue fireflies breathe candle colour color cycle shimmer comet crossing " +
   "comets disco dot orbit drain out fire firelight heartbeat highlight jellyfish larson scanner " +
   "lava flow mirror scan paparazzi point at noise speaker police pulse rainbow swirl running " +
   "lights shooting stars soft bloom solid split wipe spotlight theater chase tortoise and hare " +
   "turbo boost twin twinkle volume ramp wave in", "Animations"],
  ["Show a pattern", "ring", "Light ring › Try and troubleshoot", "test pattern preview play effect animation"],
  ["Home Assistant light", "ring", "Light ring", "ring colour color 2 3 accents palette effects"],
  ["Music visualiser", "ring", "Light ring", "visualizer spectrum beat"],
  ["Action button", "buttons", "", "press hold double triple gesture"],
  ["Security", "security", "", "account password ssh login"],
  ["Change password", "security", "Security", "account login", "Password"],
  ["SSH keys", "security", "Security", "authorized key remote access command line"],
  ["Password sign-in", "security", "Security › SSH", ""],
  ["System", "system", "", ""],
  ["Updates", "updates", "System", "software update upgrade version release"],
  ["System packages", "updates", "System › Updates", "postmarketos apk upgrade"],
  ["Check for updates automatically", "updates", "System › Updates", "daily automatic check", "Check automatically"],
  ["Kernel and boot image", "updates", "System › Updates", "kernel boot partition initramfs linux"],
  ["Date & time", "datetime", "System", "time zone timezone clock ntp chrony"],
  ["Processes", "processes", "System", "running services memory cpu ram"],
  ["Reset options", "reset", "System", "factory reset erase wipe"],
  ["Reset Wi-Fi and Bluetooth", "reset", "System › Reset options", "forget networks pairings"],
  ["Reset settings", "reset", "System › Reset options", "defaults"],
  ["Erase everything", "reset", "System › Reset options", "factory reset wipe"],
  ["Restart", "system", "System", "reboot"],
  ["About device", "about", "", "model version kernel"],
  ["Device name", "name", "About device", "hostname rename local"],
  ["Bluetooth name", "name", "About device", "alias"],
  ["Settings address", "name", "About device", "url port 8080 web page link local"],
  ["Source code", "about", "About device", "github report problem issue"],
];

let SEARCH_EXTRA = null;     // from the inventory, fetched once per page load

function searchIndex(inv) {
  const out = [];
  Object.values(inv.packages || {}).forEach((p) => out.push(
    [p.title, "app/" + encodeURIComponent(p.name), "Apps", p.name + " " + (p.label || "") + " package"]));
  Object.values(inv.services || {}).forEach((s) => s.package && out.push(
    [s.name, "app/" + encodeURIComponent(s.package), "Apps › " + s.package, "service " + (s.desc || "")]));
  (inv.functions || []).forEach((f) => out.push(
    [f.label, "function/" + f.id, "System › Processes", f.summary]));
  return out;
}

function searchResults(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = [];
  // Settings first, then apps and services: someone typing "time" wants the
  // clock before a service with time in its name. A setting is found on its
  // page and highlighted; an app is a page of its own.
  SEARCH.map((e) => ({ e, weight: 0, find: e[4] !== undefined ? e[4] : e[0] }))
    .concat((SEARCH_EXTRA || []).map((e) => ({ e, weight: 3, find: null })))
    .forEach(({ e: [t, r, w, k], weight, find }) => {
    // Each word must begin a word here: "eq" finds the equaliser, not the
    // "eq" inside "frequency".
    const hay = (t + " " + w + " " + k).toLowerCase().split(/[^a-z0-9.]+/);
    if (!words.every((x) => hay.some((h) => h.startsWith(x)))) return;
    const tw = t.toLowerCase().split(/[^a-z0-9.]+/);
    const score = weight + (tw.some((x) => x.startsWith(words[0])) ? 0 :
                            t.toLowerCase().includes(words[0]) ? 1 : 2);
    hits.push({ t, r, w, score, find });
  });
  const seen = new Set();
  return hits.sort((a, b) => a.score - b.score || a.t.localeCompare(b.t))
    .filter((h) => { const key = h.t + "|" + h.r; if (seen.has(key)) return false; seen.add(key); return true; })
    .slice(0, 40);
}

/* ===================================================================== *
 * Settings (the home page), grouped as Android groups its own
 * ===================================================================== */
PAGES[""] = async function () {
  const [audio, about] = await Promise.all([api("/api/audio"), api("/api/about")]);
  const wifi = about.wifi || {};
  const u = about.user || {};
  const apps = about.apps_installed || [];
  const main = el("<div></div>");
  const results = el("<div></div>");

  const box = el('<label class="search">' + icon("search", 16) +
                 '<input type="search" placeholder="Search settings" aria-label="Search settings"></label>');
  const input = box.querySelector("input");
  input.value = window.__search || "";
  const runSearch = () => {
    const q = input.value.trim();
    window.__search = q;
    main.hidden = !!q;
    results.replaceChildren();
    if (!q) return;
    const hits = searchResults(q);
    results.appendChild(hits.length
      ? group("", boxed(hits.map((h) => {
          const r = row({ title: h.t, sub: esc(h.w || "Settings"), go: "#/" + h.r });
          // Before the row navigates: the page it opens finds this setting.
          r.addEventListener("click", () => { FIND = h.find; }, true);
          return r;
        })))
      : statusPage("search", "Nothing found", "Try another word."));
  };
  input.addEventListener("input", runSearch);

  const notices = [];
  if (u.default_password || u.stock_account_active) {
    notices.push(row({ icon: "lock",
      title: u.default_password ? "Change the default password" : "The default account is still enabled",
      badge: "Security", badgeKind: "warn",
      sub: "Anyone on this network can sign in with postmarketOS's published default.",
      go: "#/security" }));
  }
  if (about.boot_damaged) {
    notices.push(row({ icon: "download", title: "A boot partition may be damaged", badge: "Do not restart",
      badgeKind: "warn", sub: "Install the boot image again before restarting.", go: "#/updates" }));
  } else if (about.boot_differs) {
    notices.push(row({ icon: "download", title: "Install the new boot image",
      sub: "The installed kernel is not the one this Echo starts from yet.", go: "#/updates" }));
  } else if (about.reboot_required) {
    notices.push(row({ icon: "download", title: "Restart to finish updating",
      sub: "The update is installed. A restart puts it into use.", go: "#/updates" }));
  }
  if (about.update_available) {
    notices.push(row({ icon: "download", title: "Update available: " + rel(about.update_available),
      badge: "New", badgeKind: "accent", go: "#/updates" }));
  } else if (about.kernel_update) {
    notices.push(row({ icon: "download", title: "Kernel update available",
      badge: "New", badgeKind: "accent", go: "#/updates" }));
  }
  // Setup's last page promises that this page says so when the apps ticked
  // there could not be installed yet - here, where the owner lands first.
  const pend = about.apps_pending;
  if (pend) {
    // Installing, while part of the list is and nothing has failed.
    const [badge, badgeKind] = pend.installing && pend.state !== "failed" ? ["Installing", "accent"]
                                                                          : pendingBadge(pend);
    notices.push(row({ icon: "download", badge, badgeKind, go: "#/apps",
      title: pend.labels.length === 1 ? "An app chosen at setup is not installed yet"
                                      : "Apps chosen at setup are not installed yet",
      sub: esc(andList(pend.labels)) + ". " + pendingWhy(pend, about.now, true) }));
  }
  if (notices.length) main.appendChild(group("", boxed(notices)));

  const ha = [];
  if (apps.includes("voice")) ha.push(about.ha_connected ? "Assistant connected" : "Assistant waiting");
  if (apps.includes("sendspin")) ha.push(about.ma_connected ? "Music Assistant connected" : "Music Assistant waiting");
  if (!ha.length && pend && pend.ids.some((id) => id === "voice" || id === "sendspin")) {
    ha.push("Chosen at setup, not installed yet");
  }
  const bt = about.cast_active ? "Playing through a Bluetooth speaker"
    : (about.bluetooth_connected ? about.bluetooth_connected + " connected" : "Bluetooth, USB");

  main.appendChild(group("", boxed([
    row({ icon: "wifi", title: "Network & internet",
          sub: esc(wifi.connected ? wifi.ssid + " · " + wifi.band : (wifi.available ? "Not connected" : "Wi-Fi unavailable")),
          go: "#/wifi" }),
    row({ icon: "bluetooth", title: "Connected devices", sub: esc(bt), go: "#/devices" }),
    row({ icon: "home", title: "Home Assistant",
          sub: esc(ha.length ? ha.join(" · ") : "Needs the voice assistant or music speaker"),
          go: "#/homeassistant" }),
  ])));

  const appsRow = row({ icon: "plus", title: "Apps", sub: "Counting…", go: "#/apps" });
  const storeRow = row({ icon: "disk", title: "Storage",
    sub: (about.storage_free != null ? bytes(about.storage_free) + " free · " : "") + "files from stock",
    go: "#/storage" });
  main.appendChild(group("", boxed([appsRow, storeRow])));

  main.appendChild(group("", boxed([
    row({ icon: "speaker", title: "Sound",
          sub: (audio.volume == null ? "Volume unavailable" : "Volume " + audio.volume + "%") +
               (audio.headphones ? " · headphones" : "") +
               " · sounds: " + esc(audio.earcon_set), go: "#/sound" }),
    row({ icon: "mic", title: "Microphone",
          badge: audio.muted ? "Muted" : (micFallback(audio.mic_profile_status) ? "fallback" : null),
          badgeKind: "warn",
          sub: micFallback(audio.mic_profile_status) ? micFallbackText(audio.mic_profile_status)
                                                     : esc(audio.mic_profile),
          go: "#/mic" }),
    row({ icon: "ring", title: "Light ring", sub: "Brightness, animations, direction and music", go: "#/ring" }),
    row({ icon: "button", title: "Action button", sub: "What each press and hold does", go: "#/buttons" }),
  ])));

  main.appendChild(group("", boxed([
    row({ icon: "lock", title: "Security",
          sub: u.exists ? "Signed in as " + esc(u.username) + " · password and SSH" : "Password and SSH",
          go: "#/security" }),
  ])));

  let sys = rel(about.version);
  if (about.update_available) sys += " · update available";
  main.appendChild(group("", boxed([
    row({ icon: "gear", title: "System", sub: esc(sys) + " · date and time, reset, processes", go: "#/system" }),
    row({ icon: "info", title: "About device", sub: esc(about.device_name) + " · " + esc(about.model), go: "#/about" }),
  ])));

  // Counts, and the search index for apps and services, once the inventory
  // arrives: it walks /proc and apk's database, which the page should not
  // wait for.
  api("/api/inventory").then((inv) => {
    SEARCH_EXTRA = searchIndex(inv);
    const pk = Object.values(inv.packages);
    const user = pk.filter((p) => p.kind === "user").length;
    const upd = pk.filter((p) => p.update).length;
    appsRow.querySelector(".sub").textContent = user + " installed by you · " + (pk.length - user) +
      " system" + (upd ? " · " + upd + " with updates" : "");
    if (input.value.trim()) runSearch();
  }).catch(() => {});

  runSearch();
  if (input.value) setTimeout(() => input.focus(), 0);
  return { title: about.device_name || "Settings", body: [box, results, main] };
};

/* ===================================================================== *
 * Connected devices
 * ===================================================================== */
PAGES["devices"] = async function () {
  // The jack is a nicety here, so a failure to read it must not cost the page.
  const [bt, usb, jack] = await Promise.all([api("/api/bluetooth"), api("/api/usb"),
                                             api("/api/jack").catch(() => ({}))]);
  const blocks = [];
  const now = (bt.devices || []).filter((d) => d.connected)
    .map((d) => row({ icon: d.we_play_to && !d.plays_to_us ? "head" : "phone", title: d.name,
        sub: d.audio_ready ? "Connected" : "Linked", go: "#/bluetooth" }));
  if (jack.headphones) now.unshift(row({ icon: "head", title: "Wired headphones", sub: "3.5 mm jack", go: "#/sound" }));
  blocks.push(group("Connected now", boxed(now.length ? now
    : [row({ icon: "bluetooth", title: "Nothing connected", sub: "Paired devices connect by themselves when they are in range." })])));
  blocks.push(group("", boxed([row({ icon: "plus", title: "Pair new device",
    sub: "Opens pairing for three minutes. Then pair from the phone or computer.",
    onclick: async () => {
      try { await api("/api/bluetooth", { action: "pairing", open: true }); toast("Pairing is open."); }
      catch (e) { toast(e.message, true); }
      location.hash = "#/bluetooth";
    } })])));

  blocks.push(group("Connection preferences", boxed([
    row({ icon: "bluetooth", title: "Bluetooth", sub: "Visible as " + esc((bt.adapter || {}).alias || "?") +
          " · " + ((bt.devices || []).filter((d) => d.paired).length) + " paired", go: "#/bluetooth" }),
    row({ icon: "head", title: "Play through a Bluetooth speaker",
          sub: bt.cast_active ? "Playing through Bluetooth now" : (bt.cast_target ? "Chosen, not connected" : "Off"),
          go: "#/bluetooth" }),
    row({ icon: "plug", title: "USB", sub: esc(usb.label || "Charging only"), go: "#/usb" }),
  ])));
  return { title: "Connected devices", body: blocks };
};

/* ===================================================================== *
 * System
 * ===================================================================== */
PAGES["system"] = async function () {
  const [u, dt, inv] = await Promise.all([api("/api/updates"), api("/api/datetime"), api("/api/inventory")]);
  let upd = rel(u.version);
  if (u.boot_damaged) upd += " · a boot partition may be damaged";
  else if (u.reboot_required) upd += " · restart to finish";
  else if (u.target) upd += " · " + rel(u.target) + " available";
  else if ((u.system || []).length) upd += " · " + u.system.length + " system updates";
  const fns = inv.functions.filter((f) => f.state !== "not-installed");
  const bad = fns.filter((f) => f.state !== "running").length;
  return {
    title: "System",
    body: [
      group("", boxed([
        row({ icon: "download", title: "Updates", sub: esc(upd), go: "#/updates" }),
        row({ icon: "clock", title: "Date & time", sub: esc(dt.local) + " · " + esc(dt.timezone), go: "#/datetime" }),
      ])),
      group("", boxed([
        row({ icon: "chart", title: "Processes", sub: bytes(inv.memory.used) + " of " + bytes(inv.memory.total) +
              " memory in use" + (bad ? " · " + bad + " not fully running" : ""), go: "#/processes" }),
      ])),
      group("", boxed([
        row({ icon: "refresh", title: "Reset options", sub: "Wi-Fi and Bluetooth, settings, or everything", go: "#/reset" }),
        u.boot_damaged
          ? row({ icon: "power", title: "Restart", badge: "Not now", badgeKind: "warn",
                  sub: "A boot partition may be damaged. Install the boot image again first.", go: "#/updates" })
          : row({ icon: "power", title: "Restart", sub: "Back in about a minute and a half", onclick: restartNow }),
      ])),
    ],
  };
};

async function restartNow() {
  if (!await confirmDialog("Restart now?", "The Echo is back in about a minute and a half.", "Restart")) return;
  try { await api("/api/updates", { action: "restart" }); } catch (e) { toast(e.message, true); return; }
  $("view").replaceChildren(statusPage("power", "Restarting",
    "This page comes back by itself once the Echo is up again."));
  waitForReturn();
}

/* After a restart, reload once the device answers again. */
async function waitForReturn(port, first) {
  // The page can come back at another address: a reset of settings puts it
  // back on 8080. Another port is another origin, so it is asked with
  // no-cors - an opaque answer still says it is up.
  const here = location.port || (location.protocol === "https:" ? "443" : "80");
  const moved = port != null && String(port) !== here;
  const base = moved ? location.protocol + "//" + location.hostname + (Number(port) === 80 ? "" : ":" + port) : "";
  await wait(first == null ? 20000 : first);
  for (let i = 0; i < 90; i++) {
    try {
      if (moved) { await fetch(base + "/login", { mode: "no-cors", cache: "no-store" }); location.href = base + "/"; return; }
      const r = await fetch("/api/about", { cache: "no-store" });
      if (r.ok || r.status === 401) { location.reload(); return; }
    } catch (e) { /* still down */ }
    await wait(3000);
  }
}

/* ===================================================================== *
 * Date & time
 * ===================================================================== */
function zoneOffset(z) {
  try {
    const part = new Intl.DateTimeFormat("en-GB", { timeZone: z, timeZoneName: "shortOffset" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    return part ? part.value.replace("GMT", "UTC") : "";
  } catch (e) { return ""; }
}
function zoneLabel(z) {
  const off = zoneOffset(z);
  return z.replace(/_/g, " ") + (off ? " (" + (off === "UTC" ? "UTC+0" : off) + ")" : "");
}

PAGES["datetime"] = async function () {
  const d = await api("/api/datetime");
  const s = d.sync || {};
  const nowRow = row({ icon: "clock", title: "Now", value: d.local.split(", ").pop(),
                       sub: esc(d.local.split(", ")[0]) + " · UTC" + d.utc_offset });
  const opts = [];
  const seen = new Set();
  d.suggested.forEach((z) => { opts.push({ value: z, label: zoneLabel(z) }); seen.add(z); });
  d.zones.filter((z) => !seen.has(z)).forEach((z) => opts.push({ value: z, label: zoneLabel(z) }));
  const tzRow = row({ icon: "globe", title: "Time zone",
    sub: d.suggested.length ? "Zones for " + esc(d.region) + " are listed first." : "",
    onclick: async () => {
      const v = await pick({ title: "Time zone", value: d.timezone, options: opts, filter: true });
      if (!v || v === d.timezone) return;
      try { await api("/api/datetime", { timezone: v }); toast("Time zone: " + v.replace(/_/g, " ") + ". Restart to use it everywhere."); }
      catch (e) { toast(e.message, true); }
      reload();
    } });
  tzRow.querySelector(".txt").insertBefore(el('<div class="cur">' + esc(zoneLabel(d.timezone)) + "</div>"),
                                           tzRow.querySelector(".sub"));
  pollInto(() => api("/api/datetime"), (x) => {
    nowRow.querySelector(".val").textContent = x.local.split(", ").pop();
  }, 15000);
  // Programs read the zone when they start, so after a change the ones
  // already running keep the old one until the Echo restarts.
  const stale = d.restart_needed ? group("", boxed([row({ icon: "info", title: "Restart to use it everywhere",
    sub: "Services already running keep the previous time zone in their logs until the Echo restarts.",
    end: btn("Restart", { cls: "small", onclick: restartNow }) })])) : null;
  return {
    title: "Date & time",
    body: [
      group("", boxed([nowRow])),
      stale,
      group("", boxed([
        row({ icon: s.synced ? "check" : "info", title: "Set automatically",
              sub: s.synced
                ? "From the network, via " + esc(s.source) + (s.offset_ms != null ? " · within " + Math.abs(s.offset_ms) + " ms" : "")
                : "From the network, once it can reach a time source. This Echo has no clock battery." }),
        tzRow,
      ]), "The time zone is used for the device's clock and logs. Home Assistant keeps its own."),
    ],
  };
};

/* ===================================================================== *
 * Reset options
 * ===================================================================== */
PAGES["reset"] = async function () {
  const r = await api("/api/reset");
  const list = (items) => "<ul>" + items.map((i) => "<li>" + esc(i) + "</li>").join("") + "</ul>";
  const go = async (scope, title, text, word) => {
    const fields = word ? [{ name: "word", label: 'Type "' + word + '" to confirm' }] : [];
    // Erasing everything also asks for the password, as a phone asks for its
    // PIN: a session left open in a browser must not be enough.
    if (scope === "all") fields.push({ name: "password", label: "Your password", type: "password",
                                       autocomplete: "current-password" });
    const ok = await dialog({ title, text, fields, okLabel: "Reset and restart", destructive: true,
      onsubmit: async (v) => {
        if (word && v.word.trim().toLowerCase() !== word) throw new Error('Type "' + word + '".');
        if (scope === "all" && !v.password) throw new Error("Enter your password.");
        await api("/api/reset", { scope, confirm: true, password: v.password }, { seal: scope === "all" });
      } });
    if (!ok) return;
    $("view").replaceChildren(statusPage("refresh", "Resetting",
      scope === "settings" ? "The Echo restarts with default settings. This page comes back by itself."
        : "The Echo restarts and opens its setup hotspot, <b>biscuit-</b> and four letters. Join it " +
          "from a phone to set the Echo up again."));
    // A reset of settings also puts this page back on port 8080.
    if (scope === "settings") waitForReturn(8080);
  };
  const kept = "Never erased: this Echo's own firmware, microphone files and sounds from its backup " +
               '(<a href="#/stock">Files from stock</a>), and the installed apps.';
  return {
    title: "Reset options",
    body: [
      group("", boxed([
        row({ icon: "wifi", title: "Reset Wi-Fi and Bluetooth",
              sub: "Forgets every network and pairing, then opens the setup hotspot.",
              onclick: () => go("network", "Reset Wi-Fi and Bluetooth?",
                "This erases:" + list(r.plan.network) + "The Echo then opens its setup hotspot to join a network again.") }),
        row({ icon: "refresh", title: "Reset settings",
              sub: "Sound, microphone, light ring, buttons and app preferences back to how they started.",
              onclick: () => go("settings", "Reset settings?",
                "This erases:" + list(r.plan.settings) + "Wi-Fi, Bluetooth, the account and SSH are kept.") }),
        row({ icon: "trash", title: "Erase everything",
              sub: "Back to how it was after installing: the account, Wi-Fi, pairings, SSH and every setting.",
              onclick: () => go("all", "Erase everything?",
                "This erases:" + list(r.plan.all) + "Nobody can sign in until it is set up again from its hotspot.",
                "erase") }),
      ]), kept),
    ],
  };
};

/* ===================================================================== *
 * Storage, and the files from stock
 * ===================================================================== */
function meter(title, used, total, extra) {
  const pct = total ? Math.round(100 * used / total) : 0;
  const r = el('<div class="row stack"></div>');
  r.dataset.key = title || "";
  r.appendChild(el('<div class="txt"><div class="title">' + esc(title) + '</div><div class="sub">' +
    bytes(used) + " of " + bytes(total) + " used · " + pct + "%" + (extra ? " · " + extra : "") + "</div></div>"));
  r.appendChild(el('<div class="meter' + (pct >= 90 ? " warn" : "") + '"><i style="width:' + pct + '%"></i></div>'));
  return r;
}

PAGES["storage"] = async function () {
  const [st, sf] = await Promise.all([api("/api/storage"), api("/api/stock")]);
  const imported = sf.profiles.filter((p) => p.imported);
  const problems = sf.profiles.filter((p) => p.state === "problem").length;
  return {
    title: "Storage",
    body: [
      group("", boxed(st.disks.map((d) => meter(d.label, d.used, d.total)))),
      group("", boxed([
        row({ icon: "disk", title: "Files from stock",
              badge: problems ? "Problem" : null, badgeKind: "warn",
              sub: esc(imported.map((p) => p.label).join(", ") || "None imported") +
                   (sf.earcons.length ? " · " + sf.earcons.length + " sounds" : ""),
              go: "#/stock" }),
        row({ icon: "plus", title: "Apps", value: bytes(st.packages),
              sub: st.package_count + " packages", go: "#/apps" }),
        row({ icon: "download", title: "Package cache", value: bytes(st.apk_cache),
              sub: "Downloaded packages kept after installing.",
              end: btn("Clear", { cls: "small", onclick: async () => {
                try { await api("/api/storage", { action: "clear_cache" }); toast("Cache cleared."); }
                catch (e) { toast(e.message, true); }
                reload();
              } }) }),
        row({ icon: "note", title: "Logs", value: bytes(st.logs), sub: "Each log is trimmed as it grows." }),
      ])),
    ],
  };
};

/* Upload files for a stock import: each as raw bytes, then the import, which
   checks every one before anything is installed. */
async function importStock(kind, files) {
  try {
    // They wait in the Echo's memory until checked, so a batch is capped.
    if (files.reduce((n, f) => n + f.size, 0) > 16 * 1024 * 1024)
      throw new Error("Choose at most 16 MB of files at a time.");
    if (files.length > 1000) throw new Error("Choose at most 1000 files at a time.");
    const big = files.find((f) => f.size > 2 * 1024 * 1024);
    if (big) throw new Error(big.name + " is over 2 MB.");
    let first = true, sent = 0;
    for (const f of files) {
      // A few hundred files take a while; say how far it has got.
      if (files.length > 20 && ++sent % 25 === 0) toast("Uploading " + sent + " of " + files.length + "…");
      const r = await fetch("/api/stock/upload?kind=" + encodeURIComponent(kind) + "&name=" +
                            encodeURIComponent(f.name) + (first ? "&first=1" : ""), { method: "POST", body: f });
      first = false;
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Upload failed.");
    }
    const r = await api("/api/stock", { action: "import", kind });
    toast(r.message || "Imported.");
  } catch (e) { toast(e.message, true); }
  reload();
}

function filePicker(kind, accept, folder) {
  const input = document.createElement("input");
  input.type = "file"; input.multiple = true; if (accept) input.accept = accept;
  // A whole folder where the browser can choose one; files are filtered to
  // the accepted type, since a folder brings everything in it.
  if (folder) input.webkitdirectory = true;
  input.hidden = true;
  input.addEventListener("change", () => {
    const picked = input.files.length;       // clearing the input empties the list
    let files = [...input.files];
    if (folder && accept) {
      const exts = accept.split(",").map((x) => x.trim().toLowerCase());
      files = files.filter((f) => exts.some((x) => f.name.toLowerCase().endsWith(x)));
    }
    input.value = "";
    if (files.length) importStock(kind, files);
    else if (picked) toast("No " + accept + " files in that folder.", true);
  });
  return input;
}
const canPickFolder = "webkitdirectory" in document.createElement("input");

PAGES["stock"] = async function () {
  const s = await api("/api/stock");
  const blocks = [];
  const dl = (kind, name) => "/api/stock/file?kind=" + encodeURIComponent(kind) + "&name=" + encodeURIComponent(name);
  const STATE = { ok: ["Verified", "ok"], problem: ["Problem", "warn"], absent: ["Not imported", ""] };
  s.profiles.forEach((p) => {
    if (p.id === "fireos5" && !p.imported) return;   // Fire OS 6 devices cannot supply it
    const rows = [row({ title: p.label, badge: STATE[p.state][0], badgeKind: STATE[p.state][1], sub: esc(p.what) })];
    if (p.imported) {
      p.files.forEach((f) => {
        const a = el('<a class="btn flat" title="Download" href="' + dl(p.id, f.name) + '" download>' + icon("download", 15) + "</a>");
        rows.push(row({ title: f.name.split("/").pop(),
          sub: (f.state === "ok" ? "Verified" : f.state === "corrupt" ? "<b>Does not match</b> - re-import it" : "Missing") +
               (f.size ? " · " + bytes(f.size) : "") + (f.note ? " · " + esc(f.note) : "") +
               (f.state === "ok" && !f.in_place ? " · not in use yet" : ""),
          end: f.state !== "absent" ? a : null }));
      });
    }
    const picker = filePicker(p.id);
    const acts = [btn(p.imported ? "Import again" : "Import", { cls: "small", onclick: () => picker.click() })];
    if (p.imported && !p.required) {
      acts.push(btn("Remove", { cls: "small destructive", onclick: async () => {
        if (!await confirmDialog("Remove " + p.label + "?",
              "They are deleted from this Echo. Keep your backup: they cannot be downloaded again.", "Remove", true)) return;
        try { const r = await api("/api/stock", { action: "remove", kind: p.id }); toast(r.message || "Removed."); }
        catch (e) { toast(e.message, true); }
        reload();
      } }));
    }
    // The speaker curve is owner-imported from r294: backups made by an older
    // backup zip do not hold it, so say where else it is. Until it is
    // imported the Stock equaliser is flat.
    const where = p.id === "speaker"
      ? "Choose EQ_50.cfg from the backup's assets/speaker folder (an older backup zip did not " +
        "save it), or from Fire OS's system/vendor/etc/audio-algorithms. "
      : "Choose the files from the backup's folder. ";
    rows.push(row({ title: p.imported ? "Replace from your backup" : "Import from your backup",
      sub: where + "Each is checked against its known size and " +
           "SHA-256 before anything is replaced.", end: acts.concat([picker]) }));
    blocks.push(group("", boxed(rows)));
  });

  const earPicker = filePicker("earcon", ".mp3,.ogg,.wav,.flac");
  const play = (n) => api("/api/sounds", { preview: n.replace(/\.[a-z0-9]+$/i, "") }).catch((e) => toast(e.message, true));
  const earRows = s.earcons.map((e) => row({ title: e.name, sub: bytes(e.size), end: [
    btn("", { cls: "flat", icon: "play", onclick: () => play(e.name) }),
    el('<a class="btn flat" title="Download" href="' + dl("earcon", e.name) + '" download>' + icon("download", 15) + "</a>"),
  ] }));
  blocks.push(group("", boxed([
    row({ title: "Sounds", badge: s.earcons.length ? s.earcons.length + " files" : "Not imported", badgeKind: s.earcons.length ? "ok" : "",
          sub: "Amazon's sounds, used when the sound set is Amazon. Without them the voice " +
               "assistant's own sounds play instead.",
          end: [btn("Import", { cls: "small", onclick: () => earPicker.click() }), earPicker,
                s.earcons.length ? btn("Remove all", { cls: "small destructive", onclick: async () => {
                  if (!await confirmDialog("Remove every sound?", "Keep your backup: they cannot be downloaded again.", "Remove", true)) return;
                  try { await api("/api/stock", { action: "remove", kind: "earcon" }); toast("Removed."); }
                  catch (e) { toast(e.message, true); }
                  reload();
                } }) : null] }),
  ])));
  if (earRows.length) blocks.push(fold("Sound files · " + earRows.length, [group("", boxed(earRows))]));

  /* Amazon's light ring animations. Imported, every one becomes a choice
     for every activity under Light ring > Animations; each can be shown on
     the ring from here. Listed by name in the pickers' groups, with the
     file's own name underneath for anyone matching it to their backup. */
  const led = s.led || [];
  const ledFiles = filePicker("led", ".animation");
  const ledFolder = canPickFolder ? filePicker("led", ".animation", true) : null;
  const show = (a) => api("/api/preview", { kind: "stock", animation: a.name })
    .then(() => toast("Showing " + (a.label || a.name) + " on the ring."), (e) => toast(e.message, true));
  // Why a file is not among the choices, where it is not.
  const HIDDEN = { step: "one frame of the listening arc, not offered as a choice",
                   blank: "blank, not offered as a choice", pointer: "plays as the Point effects" };
  const ledRow = (a) => { const label = a.label || a.name; return row({ title: label,
    sub: esc(a.name) + " · " + bytes(a.size) + (a.same_label ? " · same as " + esc(a.same_label) : "") +
         (HIDDEN[a.hidden] ? " · " + HIDDEN[a.hidden] : ""), end: [
    btn("", { cls: "flat", icon: "play", aria: "Show " + label + " on the ring", onclick: () => show(a) }),
    el('<a class="btn flat" title="Download" aria-label="Download ' + esc(label) + '" href="' +
       dl("led", a.name) + '" download>' + icon("download", 15) + "</a>"),
    btn("", { cls: "flat destructive", icon: "trash", aria: "Remove " + label, onclick: async () => {
      const users = (s.led_used || {})[a.name] || [];
      if (!await confirmDialog("Remove " + label + "?",
            (users.length ? "<b>" + esc(users.join(", ")) + "</b> " + (users.length === 1 ? "uses" : "use") +
                            " it, and will go back to the default. " : "") +
            "Keep your backup: it cannot be downloaded again.", "Remove", true)) return;
      try { const r = await api("/api/stock", { action: "remove", kind: "led", name: a.name }); toast(r.message || "Removed."); }
      catch (e) { toast(e.message, true); }
      reload();
    } }),
  ] }); };
  // One group per picker section, in the server's order.
  const ledGroups = [];
  led.forEach((a) => {
    const g = a.group || "Other";
    if (!ledGroups.length || ledGroups[ledGroups.length - 1][0] !== g) ledGroups.push([g, []]);
    ledGroups[ledGroups.length - 1][1].push(ledRow(a));
  });
  blocks.push(group("", boxed([
    row({ title: "Light ring animations", badge: led.length ? led.length + " files" : "Not imported",
          badgeKind: led.length ? "ok" : "",
          sub: "Amazon's own ring animations. Imported, each can be chosen for any activity " +
               'under <a href="#/ring">Light ring</a>, and shown on the ring here. In a backup ' +
               "they are in the <b>led</b> folder. Older backups may not include them; backups made " +
               "with the current backup zip do.",
          end: [ledFolder ? btn("Folder", { cls: "small", onclick: () => ledFolder.click() }) : null,
                btn("Files", { cls: "small", onclick: () => ledFiles.click() }), ledFiles, ledFolder,
                led.length ? btn("Remove all", { cls: "small destructive", onclick: async () => {
                  const users = [].concat(...Object.values(s.led_used || {}));
                  if (!await confirmDialog("Remove every animation?",
                        (users.length ? "<b>" + esc(users.join(", ")) + "</b> will go back to the default. "
                                      : "No activity uses them. ") +
                        "Keep your backup: they cannot be downloaded again.", "Remove", true)) return;
                  try { const r = await api("/api/stock", { action: "remove", kind: "led" }); toast(r.message || "Removed."); }
                  catch (e) { toast(e.message, true); }
                  reload();
                } }) : null] }),
  ])));
  if (led.length) blocks.push(fold("Animation files · " + led.length,
    ledGroups.map(([g, rows]) => group(g, boxed(rows)))));
  if (s.store) blocks.push(group("", boxed([meter("Settings store", s.store.used, s.store.total, bytes(s.store.free) + " free")])));
  return { title: "Files from stock", body: blocks.concat([el('<p class="help">' +
    "Copied from this Echo's own backup when postmarketOS was installed, or imported here. They are " +
    "Amazon's files, so they are never shipped or downloaded - keep your backup. No reset erases them.</p>")]) };
};

/* ===================================================================== *
 * About device
 * ===================================================================== */
PAGES["about"] = async function () {
  const [s, svc, upd] = await Promise.all([api("/api/about"), api("/api/services"), api("/api/updates")]);
  const links = upd.links || {};
  const blocks = [];
  blocks.push(group("", boxed([
    row({ icon: "tag", title: "Device name", value: s.device_name, go: "#/name" }),
    row({ icon: "bluetooth", title: "Bluetooth name", value: s.bluetooth_name || "—", go: "#/name" }),
    row({ icon: "link", title: "Settings address", value: s.settings_url, go: "#/name" }),
  ])));
  blocks.push(group("Device", boxed([
    row({ title: "Model", value: s.model }),
    row({ title: "Operating system", value: s.os }),
    row({ title: "Device software", value: rel(s.version), sub: '<span class="mono">' + esc(s.device_pkg) + "</span>" }),
    row({ title: "Kernel", value: s.kernel, sub: '<span class="mono">' + esc(s.kernel_pkg) + "</span>" }),
    row({ title: "Wi-Fi address", value: (s.wifi || {}).mac || "—" }),
    row({ title: "Up for", value: duration(s.uptime) }),
  ])));
  if (s.memory) {
    blocks.push(group("", boxed([meter("Memory", s.memory.used, s.memory.total)].concat(
      (svc.temperatures || []).map((t) => row({
        title: ({ soc: "Chip", cpu: "Processor", gpu: "Graphics" }[t.name.replace(/-thermal$/, "")] ||
                t.name.replace(/-thermal$/, "")) + " temperature",
        value: t.celsius + " °C" }))))));
  }
  blocks.push(group("Source and support", boxed([
    links.tree ? linkRow({ icon: "link", title: "Source code",
      sub: "This device's software, as installed (" + esc(rel(s.version)) + ")", href: links.tree }) : null,
    links.issues ? linkRow({ icon: "info", title: "Report a problem",
      sub: "Opens the issue tracker. Mention " + esc(rel(s.version)) + ".", href: links.issues }) : null,
  ]), "This device's software is MIT-licensed. Each app lists its own on its page."));
  return { title: "About device", body: blocks };
};

/* ===================================================================== *
 * Updates
 *
 * The device's own packages come from its feed and update together. System
 * packages - postmarketOS's own - update only when asked, with Python held at
 * the minor version the apps' environments were built for.
 * ===================================================================== */
PAGES["updates"] = async function () {
  const u = await api("/api/updates");
  const job = u.job;
  // An app install holds the same lock as an update, so while one runs the
  // buttons wait too - and the page says what for.
  const appJob = u.app_job;
  const jobBusy = !!(job && job.state === "running");
  const busy = jobBusy || !!appJob;
  const boot = u.boot;
  const blocks = [];
  pollPage(() => api("/api/updates"),
    (d) => JSON.stringify([d.job && d.job.state, d.job && d.job.message, !!d.app_job, d.checked_at, d.target,
                           d.kernel_available, d.reboot_required, (d.system || []).length, d.auto_check,
                           d.boot && [d.boot.differs, d.boot.checked_at, d.boot.rolled_back,
                                      d.boot.rollback && d.boot.rollback.available]]),
    busy ? 2500 : 10000);

  const start = async (body, title, text) => {
    if (title && !await confirmDialog(title, text, "Update")) return;
    try { await api("/api/updates", body); toast("Working…"); }
    catch (e) { toast(e.message, true); }
    reloadSoon(800);
  };
  // The same, after the password: for what changes the system under the apps.
  const guarded = async (body, title, text, okLabel) => {
    const ok = await withPassword(title, text, okLabel,
      (password) => api("/api/updates", Object.assign({}, body, { password }), { seal: true }));
    if (ok) { toast("Working…"); reloadSoon(800); }
  };
  const restart = async () => {
    if (!await confirmDialog("Restart now?", "The Echo is back in about a minute and a half.", "Restart")) return;
    try { await api("/api/updates", { action: "restart" }); } catch (e) { toast(e.message, true); return; }
    $("view").replaceChildren(statusPage("power", "Restarting", "This page comes back by itself."));
    waitForReturn();
  };

  // Before the first check after a restart the status is unknown (ok null);
  // the record still says whether the last act was a Put back.
  const rolledBack = !!(boot && (boot.ok ? boot.rolled_back
                                         : boot.ok === null && boot.last_apply && boot.last_apply.rolled_back));
  const damaged = !!u.boot_damaged;
  const bootNew = !!(boot && boot.ok && boot.differs && !rolledBack);
  const failed = job && job.state === "failed";
  const top = [];
  if (jobBusy) {
    top.push(row({ icon: "refresh", title: job.message || "Working…", badge: "Working", badgeKind: "accent",
                   sub: "This can take a few minutes. You can leave this page." }));
  } else if (appJob) {
    top.push(row({ icon: "refresh", badge: "Working", badgeKind: "accent", go: "#/apps",
      title: (appJob.action === "add" ? "Installing " : "Removing ") + andList(appJob.labels),
      sub: "Updates can be checked or installed once this has finished." }));
  }
  if (!jobBusy && failed) {
    top.push(row({ icon: "info", title: "The last attempt failed", badge: "Failed", badgeKind: "warn", sub: esc(job.message || "") }));
  }
  if (!busy && damaged) {
    // Nothing that restarts is offered: the Echo would start from the
    // damaged slot. Installing again, or putting back, rewrites both.
    top.push(row({ icon: "info", title: "A boot partition may be damaged", badge: "Do not restart", badgeKind: "warn",
      sub: "An install could not put a slot back as it was. Install the boot image again, or put back the " +
           "previous one, before restarting. If that fails too, reinstall from TWRP.",
      end: btn("Install again", { cls: "small suggested", onclick: () => guarded({ action: "boot" },
        "Install the boot image again?", "Both boot slots are written again and read back.", "Install") }) }));
  } else if (!busy && u.reboot_required) {
    // Restart is always offered once something waits for it; when a new
    // kernel is not yet in the boot partitions, the text says to install it
    // first, because the old kernel would start with the new one's modules.
    top.push(bootNew
      ? row({ icon: "download", title: "Install the boot image, then restart",
              sub: "The update brought a new kernel or start-up files. They are used once the boot image is installed, below.",
              end: btn("Restart", { cls: "small", onclick: restart }) })
      : row({ icon: "download", title: "Restart to finish", sub: "Changes are installed. Restarting puts them into use.",
              end: btn("Restart", { cls: "small suggested", onclick: restart }) }));
  }
  if (top.length) blocks.push(group("", boxed(top)));

  // Why the last check could not be made, kept with the check itself: the
  // job's own status is replaced by the next job of any kind. It used to say
  // "the package feed could not be reached" whatever had happened.
  const CHECK_WHY = {
    clock: "The clock is not set yet, so the package feed's security certificate could not be checked.",
    offline: "No package server could be reached.",
    feed: "This Echo's package feed is not available at the moment.",
    local: "This Echo's package database could not be opened.",
  };
  const couldNot = CHECK_WHY[u.check_why] || "The package lists could not be updated.";
  // A check made while a postmarketOS or Alpine mirror did not answer: the
  // feed did, so the device software is known; the system list may be short.
  const partial = u.check_ok && u.check_why === "partial"
    ? " Some package servers could not be reached, so this list may be incomplete." : "";
  const ours = [row({ icon: "info", title: "Installed", value: rel(u.version),
                      sub: "The device software and its apps, which update together." })];
  const updateRow = (title) => row({ icon: "download", title, badge: "New", badgeKind: "accent",
    end: btn("Update", { cls: "small suggested", disabled: busy, onclick: () => start({ action: "update" },
      title.replace(/^.*available: /, "Update to ") + "?",
      "It installs in the background; everything keeps working. Restart when it finishes.") }) });
  if (u.target) {
    ours.push(updateRow("Update available: " + rel(u.target)));
    if (u.target_links && u.target_links.release) ours.push(linkRow({ icon: "note", title: "What's new in " + rel(u.target), href: u.target_links.release }));
  } else if (u.kernel_available) {
    ours.push(updateRow("Kernel update available: " + u.kernel_available));
  } else if (u.checked_at && u.check_ok) {
    ours.push(row({ icon: "check", title: "Up to date", sub: "Checked " + ago(u.checked_at, u.now) + "." }));
  } else if (u.checked_at) {
    ours.push(row({ icon: "info", title: "Could not check", sub: couldNot }));
  } else {
    ours.push(row({ icon: "info", title: "Not checked yet",
      sub: u.auto_check ? "It checks once a day, starting a few minutes after the Echo starts."
                        : "This device checks only when you ask." }));
  }
  ours.push(row({ title: "Check for updates", end: btn("Check now", { cls: "small", disabled: busy,
    onclick: () => start({ action: "check" }) }) }));
  ours.push(row({ title: "Check automatically",
    sub: "Once a day. Updates are only ever installed when you ask.",
    end: switchEl(!!u.auto_check, async (on, input) => {
      input.disabled = true;
      try { await api("/api/updates", { action: "auto_check", on }); toast(on ? "Checking daily." : "Checking only when you ask."); }
      catch (e) { toast(e.message, true); }
      reload();
    }) }));
  if (!u.target && u.links && u.links.release) ours.push(linkRow({ icon: "note", title: "Release notes for " + rel(u.version), href: u.links.release }));
  blocks.push(group("Device software", boxed(ours)));

  // The kernel and initramfs start from the boot partitions, which no
  // package writes. A kernel update is installed to /boot, and used once the
  // boot image built from it is installed here.
  const bootRows = [row({ icon: "info", title: "Kernel", value: u.running_kernel || "—",
    sub: u.kernel ? '<span class="mono">linux-amazon-biscuit-' + esc(u.kernel) + "</span>" : "" })];
  if (!boot || boot.ok === null) {
    bootRows.push(row({ icon: "info", title: "Boot image not checked yet",
      sub: "It is compared with the installed kernel whenever updates are checked." }));
  } else if (rolledBack) {
    bootRows.push(row({ icon: "info", title: "The previous boot image is in use",
      sub: "Put back " + ago((boot.last_apply && boot.last_apply.rolled_back || {}).when || u.now, u.now) +
           ". The installed kernel's image is not in use; install it again when you want it.",
      end: btn("Install", { cls: "small", disabled: busy, onclick: () => guarded({ action: "boot" },
        "Install the boot image again?",
        "The image built from the installed kernel is written to both boot slots. Restart afterwards to use it.", "Install") }) }));
  } else if (!boot.ok && boot.unsupported) {
    // The older, hand-flashed layout: the partition named boot_a is the
    // bootloader there, so nothing here writes to it.
    bootRows.push(row({ icon: "info", title: "Boot image is installed by hand on this Echo",
      sub: "It uses the older install layout, so a kernel update is written to its boot partition by hand." }));
  } else if (!boot.ok) {
    bootRows.push(row({ icon: "info", title: "The boot image could not be checked", badge: "Problem", badgeKind: "warn",
      sub: esc(boot.error || "") }));
  } else if (boot.differs) {
    const what = boot.kernel_differs && boot.initramfs_differs ? "kernel and start-up files are"
               : boot.kernel_differs ? "kernel is" : "start-up files are";
    bootRows.push(row({ icon: "download", title: "New boot image ready", badge: "New", badgeKind: "accent",
      sub: "The installed " + what + " newer than what this Echo starts from. Built " + esc(boot.built) + ".",
      end: btn("Install", { cls: "small suggested", disabled: busy, onclick: () => guarded({ action: "boot" },
        "Install the boot image?",
        "It is checked before anything is written, then written to both boot slots and read back. The image " +
        "it replaces is kept, so it can be put back. Restart afterwards to use it.", "Install") }) }));
  } else {
    bootRows.push(row({ icon: "check", title: "Boot image is current",
      sub: "This Echo starts from the installed kernel. Checked " + ago(boot.checked_at, u.now) + "." }));
  }
  const back = boot && boot.rollback;
  if (back && back.available && boot.last_apply && (damaged || !rolledBack)) {
    // Only the boot partitions go back. The kernel package's modules stay
    // the installed one's, so an older kernel may not load them - the Wi-Fi
    // driver among them. Unknown counts as possibly different.
    const tail = ", whose modules stay: Wi-Fi may not start, and then only USB networking or a " +
                 "reinstall from TWRP reaches the Echo.";
    const warn = back.kernel_differs === false ? ""
      : back.kernel_differs ? " <b>Its kernel is older than the installed one</b>" + tail
      : " <b>If its kernel is older than the installed one</b>" + tail;
    const when = boot.last_apply.when || (boot.last_apply.damaged || {}).when;
    bootRows.push(row({ title: "Put back the previous boot image",
      sub: (when ? "The last install was " + ago(when, u.now) + "." : "The last install did not finish.") +
           " For a new one that misbehaves.",
      end: btn("Put back", { cls: "small", disabled: busy, onclick: () => guarded({ action: "bootrollback" },
        "Put back the previous boot image?",
        "The image from before the last install is written to both boot slots. Restart afterwards." + warn, "Put back") }) }));
  }
  blocks.push(group("Kernel and boot image", boxed(bootRows)));

  const sys = u.system || [];
  const sysRows = [];
  if (sys.length) {
    sysRows.push(row({ icon: "download", title: sys.length + " system update" + (sys.length === 1 ? "" : "s") + " available",
      sub: "postmarketOS's own packages, from its edge channel." + partial,
      end: btn("Update all", { cls: "small suggested", disabled: busy, onclick: () => guarded({ action: "sysupdate" },
        "Update " + sys.length + " system packages?",
        "Installs the newer postmarketOS packages. Python stays at its current version, so the apps keep " +
        "working. Takes a few minutes; restart when it finishes.", "Update") }) }));
  } else if (u.checked_at && !u.check_ok) {
    // Nothing could be checked: not "up to date".
    sysRows.push(row({ icon: "info", title: "Could not check", sub: couldNot }));
  } else {
    sysRows.push(row({ icon: "check",
      title: !u.checked_at ? "Not checked yet" : partial ? "No system updates found" : "System packages are up to date",
      sub: u.checked_at ? "Checked " + ago(u.checked_at, u.now) + "." + partial : "Check for updates above." }));
  }
  blocks.push(group("System packages", boxed(sysRows),
    "Python is held at its current minor version, because the apps carry environments built for it; " +
    "a system update that would move it is refused and says so."));
  if (sys.length) {
    blocks.push(fold("Available · " + sys.length, [group("", boxed(sys.map(([n, from, to]) => row({
      title: n, sub: esc(from) + " → " + esc(to), go: "#/app/" + encodeURIComponent(n) }))))]));
  }
  return { title: "Updates", body: blocks };
};
</script>

<script>
/* Kick the router once every page definition above exists. */
render();
</script>
"""


# The optional daily check. It only looks - nothing is installed without the
# owner - so the home page's "update available" can appear without anyone
# having to ask first. Not in the first minutes after boot, so it never
# competes with start-up for the CPU.
AUTO_CHECK_EVERY = 24 * 3600
AUTO_CHECK_AFTER_BOOT = 600


def _uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def auto_check_tick():
    """One pass of the daily check. True when it started one."""
    if not system.auto_check_enabled() or _uptime() < AUTO_CHECK_AFTER_BOOT:
        return False
    last = system.updates().get("checked_at")
    if last and 0 <= time.time() - last < AUTO_CHECK_EVERY:
        return False
    # Not while any other package job runs - an app install included; the next
    # pass tries again.
    if start_pkg_job(lambda: system.start_job("check") or True) is None:
        return False
    _LOGGER.info("daily update check")
    return True


def auto_check_loop():
    while True:
        time.sleep(300)
        try:
            auto_check_tick()
        except Exception as err:                  # noqa: BLE001
            _LOGGER.warning("daily update check failed to start: %s", err)


# The apps chosen at setup that could not be installed then are tried again
# from here, by biscuit_system.retry_step's schedule: a few minutes after a
# failure, growing to an hour; as soon as the clock is set when that was all
# that stopped them; never before it is set; never alongside another job. The
# owner asked for these at setup, so this is the one install that happens
# without a click here - and it stops the moment the list is empty or the
# owner cancels it on the Apps page. Checked every minute, which is a file
# read when nothing is pending.
APPS_RETRY_EVERY = 60


def apps_retry_tick():
    pending = system.prune_pending()
    kept = system.retry_load()
    record = netcfg.package_job(netcfg.PKG_APPS_STATUS)
    # A removal started anywhere but here - biscuit-pkgjob.sh over SSH - that
    # finished before a prune saw the app installed. (One started here was
    # pruned before it started: see start_pkg_job.)
    pending = system.drop_removed(pending, record, kept)
    st, due = system.retry_step(pending, record,
                                pkg_job_busy(), system.uptime(), system.clock_set(), kept)
    if due:
        try:
            # The list read again under the start lock, which a cancel on the
            # Apps page also takes: a cancel that lands between the two reads
            # leaves nothing to install, not an install nobody wants.
            started = start_pkg_job(lambda: system.start_app_install(system.pending_apps(), "retry"))
        except (ValueError, OSError) as err:
            # Counted as a try all the same, so a job that cannot even start
            # is not started again every minute.
            _LOGGER.warning("could not start the install of the apps chosen at setup: %s", err)
            started = ()
        if started is not None:
            if started:
                _LOGGER.info("trying again to install the apps chosen at setup: %s",
                             " ".join(started))
            st = system.retry_started(st, system.uptime())
    if st != kept:
        system.retry_save(st)


def apps_retry_loop():
    while True:
        time.sleep(APPS_RETRY_EVERY)
        try:
            apps_retry_tick()
        except Exception as err:                  # noqa: BLE001
            _LOGGER.warning("retrying the apps chosen at setup: %s", err)


def main():
    global PORT
    # Publish the resolved table at start-up, not only on save. The shell
    # consumers all fall back to their stock name when the file is missing, so
    # this is not required for correctness - but it means the file exists on a
    # fresh device, which makes "what will the ring actually do" answerable by
    # reading one file instead of by reading five scripts.
    try:
        publish_for_shell(read_overrides())
    except Exception as err:                      # noqa: BLE001
        sys.stderr.write("[settings] could not publish %s: %s\n" % (ANIM_ENV, err))

    try:
        srv = Server(("0.0.0.0", PORT), Handler)
    except OSError as err:
        # A chosen port something else took must not leave the device with
        # no settings page at all: fall back to the default one.
        if PORT == system.DEFAULT_SETTINGS_PORT:
            raise
        sys.stderr.write("[settings] cannot listen on :%d (%s); using :%d\n"
                         % (PORT, err, system.DEFAULT_SETTINGS_PORT))
        PORT = system.DEFAULT_SETTINGS_PORT
        srv = Server(("0.0.0.0", PORT), Handler)
    sys.stderr.write("[settings] listening on :%d, editing %s\n" % (PORT, LED_MAP_FILE))
    threading.Thread(target=auto_check_loop, daemon=True).start()
    threading.Thread(target=apps_retry_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
