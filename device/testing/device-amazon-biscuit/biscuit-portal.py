#!/usr/bin/env python3
"""Captive portal for first-boot Wi-Fi setup.

Serves a single form on http://192.168.4.1/ and writes what it collects into
/run/biscuit-setup/ for biscuit-setup.sh to act on. It deliberately does not
touch the radio itself: the whole state machine lives in the shell script, and
this process only turns an HTTP request into a set of files.

Python's standard library only - no Flask, no templating engine. The device has
no network at the point this runs, so anything not already in the image cannot
be fetched, and the page has to be self-contained for the same reason: no web
fonts, no CDN, no external assets.

Captive-portal detection works by answering the probe URLs that phones fetch
before they will admit they are online. Android asks for a 204 and treats any
other answer as a portal; iOS and macOS expect a page containing "Success".
Giving both the wrong answer, plus a DNS server that resolves everything here,
is what makes the sign-in sheet open by itself instead of the user having to
find the address.
"""

import base64
import binascii
import glob
import hashlib
import hmac
import html
import json
import os
import pwd
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, "/usr/bin")
try:
    import biscuit_system as system    # the settings page's port rules
except ImportError:                    # pragma: no cover - only off the device
    system = None

RUNDIR = "/run/biscuit-setup"
ADDR = "192.168.4.1"
PORT = 80
APPS_FILE = "/usr/share/biscuit/apps.json"

# ---------------------------------------------------------------------------
# Protecting the two secrets this form carries
# ---------------------------------------------------------------------------
#
# THE PROBLEM. The setup AP is open, and this page is plain HTTP. The form
# collects the home Wi-Fi passphrase AND the password for the account that is
# about to be created on this device - so both cross the air in the clear, to
# anyone in radio range with a capture running, for the length of the setup
# window.
#
# WHY NOT THE OBVIOUS FIXES.
#
#   OWE ("Enhanced Open") is built for exactly this and would need no key
#   handling here at all. It is not available: hostapd 2.12 supports it, but
#   the mt-wifi driver advertises no OWE, no SAE and no external-auth
#   capability. Measured on the device, not assumed.
#
#   WPA2 on the setup AP does not help unless the passphrase is per-device and
#   secret. A published or default passphrase is worthless: knowing it plus the
#   4-way handshake, which is sent in the clear, yields the session keys. And a
#   per-device random one has nowhere to be displayed - no screen, no app.
#
#   HTTPS with a self-signed certificate breaks captive-portal detection and
#   trains people to click through certificate warnings, for the same
#   passive-only protection this gets without either cost.
#
# WHAT THIS DOES. An ephemeral X25519 keypair per setup window; the public half
# goes in the page, the browser boxes the two secrets with NaCl, and only the
# ciphertext is posted. A passive listener sees nothing useful.
#
# WHAT IT DOES NOT DO, AND THE PAGE SAYS SO. It does not stop an attacker who
# stands up their own AP with the same name and serves their own page and their
# own key. That attack works against the plain form today, is not made worse,
# and closing it needs an out-of-band channel this device does not have. This
# is the gap stock fills with the phone app.
#
# NO HAND-ROLLED CRYPTO ON EITHER SIDE. PyNaCl here, TweetNaCl in the browser -
# two audited implementations of the same primitive. TweetNaCl is vendored
# rather than fetched: the setup AP has no internet by definition, so a CDN
# reference would simply fail. The browser cannot use WebCrypto for this at
# all, because crypto.subtle is restricted to secure contexts and this page is
# http:// on a bare IP.

NACL_JS = "/usr/share/biscuit/nacl-fast.min.js"


def log(msg):
    """One line to stderr, which biscuit-setup.sh captures into its log.

    Deliberately NOT BaseHTTPRequestHandler.log_message, which is silenced
    below: that suppresses per-request noise, and these are the few events that
    matter - whether a submission arrived protected, and why one was refused.
    """
    sys.stderr.write("[portal] %s" % msg + chr(10))
    sys.stderr.flush()

try:
    from nacl.public import Box, PrivateKey, PublicKey
    _HAVE_NACL = True
except ImportError:  # pragma: no cover - only on a device missing py3-pynacl
    _HAVE_NACL = False


class SetupKey:
    """One ephemeral keypair for the life of this setup window.

    Kept in memory only. The portal process dies when setup ends, which is
    exactly the lifetime wanted - a key on disk would outlive the window it was
    generated for and could be lifted from a device afterwards to decrypt a
    capture taken during it.
    """

    def __init__(self):
        self.private = PrivateKey.generate() if _HAVE_NACL else None

    @property
    def available(self):
        return self.private is not None

    def public_b64(self):
        if not self.available:
            return ""
        return base64.b64encode(bytes(self.private.public_key)).decode("ascii")

    def open(self, blob):
        """Decrypt 'clientpub.nonce.ciphertext', all base64. Returns a dict.

        Raises ValueError for anything malformed. The caller treats that as a
        failed submission rather than falling back to plaintext: a blob that
        does not decrypt is either a bug or someone poking at the endpoint, and
        neither should quietly downgrade the connection.
        """
        if not self.available:
            raise ValueError("no key material")
        try:
            pub_b64, nonce_b64, ct_b64 = blob.split(".")
            peer = PublicKey(base64.b64decode(pub_b64, validate=True))
            nonce = base64.b64decode(nonce_b64, validate=True)
            ct = base64.b64decode(ct_b64, validate=True)
        except (ValueError, TypeError, binascii.Error) as err:
            raise ValueError("malformed payload: %s" % err)
        try:
            plain = Box(self.private, peer).decrypt(ct, nonce)
        except Exception as err:                      # nacl raises CryptoError
            raise ValueError("decryption failed: %s" % err)
        try:
            data = json.loads(plain.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as err:
            raise ValueError("payload is not JSON: %s" % err)
        if not isinstance(data, dict):
            raise ValueError("payload is not an object")
        return data


KEY = SetupKey()


# Android/Chrome, iOS/macOS, Windows, and Firefox respectively.
PROBE_PATHS = {
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/connecttest.txt",
    "/ncsi.txt",
    "/success.txt",
    "/canonical.html",
}

ERROR_TEXT = {
    # Deliberately does NOT say "wrong password". All the device knows is that
    # it saw the network and never finished associating inside the time it
    # allows. A wrong password is the most common reason and usually the right
    # guess - but not the only one: one network here refused association twice
    # with a passphrase confirmed correct, on a channel the regulatory domain
    # allows, while a different network on the same channel joined fine. So
    # name the symptom, put the likely cause first, and stop asserting a
    # diagnosis the device cannot actually make.
    "auth": (
        "That network was found, but the connection did not complete in time. "
        "A wrong password is the usual cause, so check it - but if you are "
        "sure it is right, simply try again: this sometimes succeeds on a "
        "second attempt."
    ),
    # A submission that will not decrypt is a fault on this device, not
    # something the person typed. Telling them to check their password would
    # send them round a loop that cannot terminate.
    "crypto": (
        "The device could not read what the browser sent. Nothing was saved. "
        "Reload this page to get a fresh key and enter the details again."
    ),
    "dhcp": (
        "Connected to that network, but it did not hand out an address. "
        "If it has a device limit or needs approval for new devices, check there."
    ),
    "notfound": (
        "That network was not on the air when we looked. "
        "Check the name. If it is a 5 GHz network on one of the upper "
        "channels, try its 2.4 GHz side instead."
    ),
    # These four had no text at all, so a refused username or password
    # re-served the form with an empty banner and no hint of what was wrong.
    "user": (
        "Choose a username of lowercase letters, digits, - and _, starting "
        "with a letter or _. Some names, such as root, user and the names of the "
        "device's own services, are taken."
    ),
    "pass": "Choose a password of at least 8 characters, all on one line.",
    "mismatch": (
        "The two passwords did not match. Nothing was saved; type the "
        "password in both boxes again."
    ),
    "heard": (
        "Play the sound on the Echo and tick \u201cI heard the sound\u201d, or "
        "choose \u201cI can\u2019t hear it\u201d if you cannot."
    ),
    "reset": (
        "The password was not reset: the presses on the Echo's microphone "
        "button were not confirmed, or it took too long. Start the check again."
    ),
    "port": (
        "That port cannot be used for the settings page: the Echo uses it "
        "already, or browsers refuse it. Use 8080, 80, or another number from "
        "1024 up."
    ),
    "ssid": "Choose a network, or type its name under Other network.",
    "resetseal": (
        "The password was not reset: this browser could not encrypt the form, "
        "and a reset is only accepted encrypted. Try another browser."
    ),
}


# Accounts that must never be created here, either because they already exist
# on any Alpine system or because they would shadow something that matters.
RESERVED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "operator", "man", "postmaster", "cron", "ftp", "sshd",
    "at", "squid", "xfs", "games", "cyrus", "vpopmail", "ntp", "smmsp",
    "guest", "nobody", "pmos", "user",
}


def system_account(name):
    """True when `name` already exists as an account outside 1000..64999.

    RESERVED_USERS cannot list every daemon a package might add (the image
    has chrony, dnsmasq, messagebus, avahi, klogd and more), and setup, given
    an existing name, only sets its password: an owner "created" as chrony
    would have had no shell, no wheel and no settings-page login, and 'user'
    would have been deleted in its favour. biscuit-setup.sh refuses the same
    names; this says so on the form instead of failing later.
    """
    try:
        uid = pwd.getpwnam(name).pw_uid
    except (KeyError, TypeError, ValueError):
        return False
    return not 1000 <= uid <= 64999


def needs_account():
    """True when this device has no usable owner account yet.

    The marker is on the persist partition so a device that has been set up
    once is not asked again - that is the point of keeping provisioning state
    there. But a flash replaces the rootfs and NOT the persist partition, so
    the marker outlives the account it names, and believing it alone is wrong
    in the one case that matters most.

    Observed on hardware: the marker named an account while the only one on the
    device was the shipped default. The wizard then offered no username or
    password fields at all, so a fresh install could not be given an owner and
    the default account - with a password that is effectively public - stayed
    live on an always-on device.

    So the marker only counts when the account it names actually exists. Same
    rule as create_account() in biscuit-setup.sh; this is the display half.
    """
    try:
        with open("/opt/persist/provisioned") as fh:
            owner = fh.read().strip()
    except OSError:
        return True
    if not owner:
        return True
    try:
        pwd.getpwnam(owner)
    except KeyError:
        return True
    return False


def read_file(name, default=""):
    try:
        with open(os.path.join(RUNDIR, name), "r") as fh:
            return fh.read().strip()
    except OSError:
        return default


def write_secret(path, value):
    """Write a file only its owner (root) can read, from the moment it exists.

    The mode is given to open() rather than applied afterwards, so the file
    never exists with the default one, and set again on the descriptor in case
    an earlier file of the same name was left with a wider mode (open() keeps
    an existing file's). biscuit-setup.sh deletes each of these once used.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(value)


_ESCAPED = re.compile(r"\\x([0-9a-fA-F]{2})")


def unescape_ssid(name):
    """iw prints every byte outside printable ASCII - and a backslash - as
    \\xNN. Joining with that text would ask for a network whose name has
    literal backslashes in it, so "Moose\u2019s Crib" could be seen and never
    joined."""
    if "\\x" not in name:
        return name
    raw = _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), name).encode("latin-1", "replace")
    return raw.decode("utf-8", "replace")


def signal_bars(dbm):
    """0-4, with the settings page's thresholds (NetworkManager's)."""
    for bars, floor in ((4, -55), (3, -66), (2, -77), (1, -88)):
        if dbm >= floor:
            return bars
    return 0


def networks():
    """The networks the shell script's scan found, strongest first:
    [{"ssid", "bars", "band", "secure"}]. From networks.info, which carries
    the signal and bands; the bare list in `networks` is the fallback."""
    out = []
    try:
        with open(os.path.join(RUNDIR, "networks.info"), "r") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 4 or not parts[1]:
                    continue
                try:
                    dbm = int(parts[0])
                except ValueError:
                    dbm = -100
                band = parts[2].replace("/", " / ")
                out.append({"ssid": unescape_ssid(parts[1]), "bars": signal_bars(dbm),
                            "band": band + " GHz" if band else "",
                            "secure": parts[3] == "1"})
        return out
    except OSError:
        pass
    try:
        with open(os.path.join(RUNDIR, "networks"), "r") as fh:
            return [{"ssid": unescape_ssid(line.rstrip("\n")), "bars": 0, "band": "",
                     "secure": True} for line in fh if line.strip()]
    except OSError:
        return []


def write_decoded_networks():
    """The names as they will be joined, for biscuit-setup.sh's "was it on
    the air?" check, which otherwise compares against the escaped form."""
    try:
        with open(os.path.join(RUNDIR, "networks.utf8"), "w", encoding="utf-8") as fh:
            fh.write("".join(n["ssid"] + "\n" for n in networks()))
    except OSError:
        pass


def uptime():
    with open("/proc/uptime") as fh:
        return int(float(fh.read().split()[0]))


# ---------------------------------------------------------------------------
# The setup window
#
# biscuit-setup.sh gives the portal WINDOW seconds, in uptime, and closes the
# network when they run out. Someone halfway through the form should not be
# cut off, so a page in use moves the deadline on - by five minutes at a time,
# never past window_max - and the page counts down, so it never closes on
# someone silently.
# ---------------------------------------------------------------------------

WINDOW_EXTEND = 300
_window_lock = threading.Lock()


def window(extend=False):
    """{"left": seconds, "final": no more extensions}, or None when this
    portal was started without a window (by hand, for a test)."""
    with _window_lock:
        try:
            deadline = int(read_file("deadline"))
            start = int(read_file("window_start"))
            longest = int(read_file("window_max") or "1800")
        except ValueError:
            return None
        now = uptime()
        cap = start + longest
        if extend and now < deadline:
            want = min(max(deadline, now + WINDOW_EXTEND), cap)
            if want > deadline:
                tmp = os.path.join(RUNDIR, "deadline.tmp")
                with open(tmp, "w") as fh:
                    fh.write("%d\n" % want)
                os.replace(tmp, os.path.join(RUNDIR, "deadline"))
                deadline = want
        return {"left": max(0, deadline - now), "final": deadline >= cap}


# ---------------------------------------------------------------------------
# Proof of presence: the microphone button
#
# Resetting the password from setup mode must need someone at the Echo, not
# just someone in range of its open setup network. The page asks for two
# presses of the microphone button, and this reads them from the key device
# biscuit-audio watches (it does not grab it, so both see every press). Two
# presses leave the microphone as it was.
#
# If two phones start a check at once, neither counts: presses cannot be
# told apart, and a stranger's page must not be able to borrow the owner's.
# ---------------------------------------------------------------------------

KEY_DEVICE = "mtk-pmic-keys"
KEY_MUTE = 113
EVENT_FMT = "llHHi"


class Presence:
    NEED = 2
    TTL = 120            # seconds to press the button
    PROVEN_TTL = 900     # seconds to finish the form after that
    START_GAP = 3        # between starts from one phone

    def __init__(self):
        self.lock = threading.Lock()
        self.challenges = {}
        self.last_start = {}
        self.reader = None

    @staticmethod
    def device():
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                with open("/sys/class/input/%s/device/name" % os.path.basename(path)) as fh:
                    if fh.read().strip() == KEY_DEVICE:
                        return path
            except OSError:
                continue
        return None

    def _read(self, path):
        size = struct.calcsize(EVENT_FMT)
        while True:
            try:
                with open(path, "rb", buffering=0) as fh:
                    while True:
                        data = fh.read(size)
                        if len(data) < size:
                            break
                        _s, _us, typ, code, value = struct.unpack(EVENT_FMT, data)
                        if typ == 1 and code == KEY_MUTE and value == 1:
                            self.press()
            except OSError as err:
                log("could not read the buttons: %s" % err)
                time.sleep(5)

    def start(self, addr, proof_hash):
        """A check for one phone. proof_hash is SHA-512 of a secret the page
        keeps and sends only inside the encrypted form: the check's id crosses
        the open network in the clear, so the id alone must never authorise a
        reset."""
        path = self.device()
        if path is None:
            raise LookupError("this Echo's buttons cannot be read")
        if not re.fullmatch(r"[0-9a-f]{128}", proof_hash or ""):
            raise ValueError("no proof hash")
        now = time.monotonic()
        with self.lock:
            if now - self.last_start.get(addr, -60) < self.START_GAP:
                raise PermissionError("too soon")
            self.last_start[addr] = now
            for cid in [c for c, v in self.challenges.items()
                        if now > v["expires"] + self.PROVEN_TTL]:
                del self.challenges[cid]
            # A phone starting again replaces its own unfinished check - after
            # a reload, say - rather than contesting it.
            for cid in [c for c, v in self.challenges.items()
                        if v["addr"] == addr and not v["proven"]]:
                del self.challenges[cid]
            active = [c for c in self.challenges.values()
                      if not c["proven"] and now < c["expires"]]
            cid = secrets.token_urlsafe(12)
            # Every contested check - the new one and those it contests - is
            # told to wait until all of them have run out. Otherwise the
            # earlier phone was told to go again at once, and each phone
            # following its own advice contested the other for ever.
            clear = max([c["expires"] for c in active] + [now + self.TTL]) if active else now
            for c in active:
                c["contested"] = True
                c["clear_at"] = max(c.get("clear_at", now), clear)
            self.challenges[cid] = {"id": cid, "presses": 0, "expires": now + self.TTL,
                                    "proven": None, "contested": bool(active), "used": False,
                                    "clear_at": clear, "addr": addr, "proof_hash": proof_hash}
            if self.reader is None:
                self.reader = threading.Thread(target=self._read, args=(path,), daemon=True)
                self.reader.start()
        log("presence check started from %s%s" % (addr, " - CONTESTED" if active else ""))
        return self.state(cid)

    def press(self):
        now = time.monotonic()
        with self.lock:
            for c in self.challenges.values():
                if not c["proven"] and now < c["expires"]:
                    c["presses"] += 1
                    if c["presses"] >= self.NEED:
                        c["proven"] = now

    def state(self, cid):
        now = time.monotonic()
        with self.lock:
            c = self.challenges.get(cid)
            if c is None:
                return None
            return {"id": cid, "presses": min(c["presses"], self.NEED), "need": self.NEED,
                    "done": bool(c["proven"]) and not c["contested"],
                    "contested": c["contested"],
                    "retry_in": max(0, int(c.get("clear_at", now) - now)) if c["contested"] else 0,
                    "left": 0 if c["proven"] else max(0, int(c["expires"] - now))}

    def consume(self, cid, proof, addr):
        """True once, for a check that was proven, uncontested and recent,
        submitted from the phone that started it with the secret whose hash
        it started with."""
        try:
            digest = hashlib.sha512(bytes.fromhex(proof or "")).hexdigest()
        except ValueError:
            return False
        now = time.monotonic()
        with self.lock:
            c = self.challenges.get(cid or "")
            ok = bool(c and c["proven"] and not c["contested"] and not c["used"]
                      and now - c["proven"] < self.PROVEN_TTL and c["addr"] == addr
                      and len(proof or "") == 64
                      and hmac.compare_digest(digest, c["proof_hash"]))
            if ok:
                c["used"] = True
            return ok


PRESENCE = Presence()

# The confirmation sound. Anyone in range of the open network can press the
# button, so it plays at most this often.
CHIME_GAP = 3.0
_chime = {"last": 0.0}
_chime_lock = threading.Lock()


def owner():
    """The account this device already has, or "" when setup is creating one."""
    try:
        with open("/opt/persist/provisioned") as fh:
            name = fh.read().strip()
        pwd.getpwnam(name)
        return name
    except (OSError, KeyError):
        return ""


def current_port():
    return system.settings_port() if system else 8080


def check_port(value):
    if system:
        return system.check_port(value)
    port = int(value)
    if port != 80 and not 1024 <= port <= 65535:
        raise ValueError("bad port")
    return port


def default_name(ssid):
    """The name field's starting value: the device's own name when it has been
    set up before - re-entering setup to change Wi-Fi must not rename it -
    and the setup network's otherwise."""
    name = socket.gethostname()
    if owner() and name and name not in ("amazon-biscuit", "localhost"):
        return name
    return ssid


def optional_packages():
    """The optional apps offered on the last step, from apps.json.

    The settings page's Apps list reads the same file, so the two cannot
    describe an app differently. What an app installs is WRITTEN DOWN there
    rather than computed: working it out needs the package index, and at this
    point the device is running its own access point with no route anywhere.
    It is also the more useful answer - someone choosing "Voice assistant"
    wants the wake-word engine and the size, not a list of libraries.
    """
    out = []
    try:
        with open(APPS_FILE, encoding="utf-8") as fh:
            apps = json.load(fh).get("apps", [])
    except (OSError, ValueError):
        return out
    for app in apps:
        pkgs = " ".join(app.get("packages") or [])
        if not app.get("id") or not pkgs:
            continue
        desc = app.get("summary", "")
        if app.get("size_mb"):
            desc += " About %d MB." % app["size_mb"]
        parts = ["%s %s" % (u["name"], u["version"]) if u.get("version") else u["name"]
                 for u in app.get("upstream", [])]
        if app.get("changes"):
            parts.append("%d changes of ours, listed on the settings page"
                         % len(app["changes"]))
        out.append({"id": app["id"], "label": app.get("label", app["id"]),
                    "name": app.get("name", ""),
                    "desc": desc, "pkgs": pkgs, "parts": parts})
    return out


# Regions offered on the first setup step.
#
# NOT the full ISO 3166 list. Every entry here is a country whose rules are in
# wireless-regdb, and the list is deliberately short enough to scroll on a
# phone. The one the beacons suggest is always added if it is missing, so a
# device somewhere unlisted still gets the right answer without an edit here -
# and "Worldwide" stays available as the honest fallback for someone who does
# not know or does not want to say.
REGIONS = [
    ("00", "Worldwide (most restrictive)"),
    ("AR", "Argentina"), ("AT", "Austria"), ("AU", "Australia"),
    ("BE", "Belgium"), ("BR", "Brazil"), ("CA", "Canada"), ("CH", "Switzerland"),
    ("CL", "Chile"), ("CN", "China"), ("CZ", "Czechia"), ("DE", "Germany"),
    ("DK", "Denmark"), ("EE", "Estonia"), ("ES", "Spain"), ("FI", "Finland"),
    ("FR", "France"), ("GB", "United Kingdom"), ("GR", "Greece"),
    ("HK", "Hong Kong"), ("HU", "Hungary"), ("IE", "Ireland"), ("IL", "Israel"),
    ("IN", "India"), ("IS", "Iceland"), ("IT", "Italy"), ("JP", "Japan"),
    ("KR", "South Korea"), ("LT", "Lithuania"), ("LU", "Luxembourg"),
    ("LV", "Latvia"), ("MX", "Mexico"), ("MY", "Malaysia"),
    ("NL", "Netherlands"), ("NO", "Norway"), ("NZ", "New Zealand"),
    ("PH", "Philippines"), ("PL", "Poland"), ("PT", "Portugal"),
    ("RO", "Romania"), ("SE", "Sweden"), ("SG", "Singapore"),
    ("SI", "Slovenia"), ("SK", "Slovakia"), ("TH", "Thailand"),
    ("TR", "Turkey"), ("TW", "Taiwan"), ("US", "United States"),
    ("VN", "Vietnam"), ("ZA", "South Africa"),
]


def region_options():
    """The region <select>, defaulted to whatever the neighbours broadcast.

    Access points carry a country code in their beacons, and biscuit-setup.sh
    writes the most common one to country_hint during the scan. Using it as the
    DEFAULT rather than applying it silently is the point: the radio's legal
    limits should not be decided by whatever the loudest neighbour claims, but
    asking someone to recall their own ISO country code cold is a poor way to
    start a setup wizard.
    """
    hint = read_file("country_hint").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", hint or ""):
        hint = ""

    known = dict(REGIONS)
    entries = list(REGIONS)
    if hint and hint not in known:
        # Somewhere the curated list does not cover. Better to offer the real
        # answer than to make the person pick a neighbouring country.
        entries.insert(1, (hint, hint))

    # Fall back to Worldwide, which is what the kernel does anyway - so the
    # default never claims permissions the device has not been told it has.
    selected = hint or "00"
    out = []
    for code, label in entries:
        out.append(
            '          <option value="{c}"{s}>{l}</option>'.format(
                c=html.escape(code),
                l=html.escape(label),
                s=" selected" if code == selected else "",
            )
        )
    return chr(10).join(out)


ZONE_TAB = "/usr/share/zoneinfo/zone1970.tab"


def zones():
    """Every named time zone, with the countries it covers, from zone1970.tab.
    The same list the settings page's Date & time offers."""
    out = {}
    try:
        with open(ZONE_TAB, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                parts = line.rstrip(chr(10)).split(chr(9))
                if len(parts) >= 3:
                    out[parts[2]] = parts[0].split(",")
    except OSError:
        pass
    out.setdefault("Etc/UTC", [])
    return out


def timezone_options():
    """The time zone <select>: the zones of the country the neighbours
    broadcast first, then every other. UTC is chosen until the page's script
    picks the phone's own zone, which is almost always the right answer."""
    hint = read_file("country_hint").upper()
    all_zones = zones()
    near = sorted(z for z, cc in all_zones.items() if hint and hint in cc)
    rest = sorted(z for z in all_zones if z not in near)

    def opt(z):
        return '<option value="{v}"{s}>{l}</option>'.format(
            v=html.escape(z), l=html.escape(z.replace("_", " ")),
            s=" selected" if z == "Etc/UTC" else "")
    out = []
    if near:
        out.append('<optgroup label="{}">'.format(html.escape(hint)))
        out.extend(opt(z) for z in near)
        out.append("</optgroup>")
        out.append('<optgroup label="Everywhere">')
    out.extend(opt(z) for z in rest)
    if near:
        out.append("</optgroup>")
    return chr(10).join(out)


def reset_note():
    """A line for the top of the form when a reset brought the device here,
    so someone who pressed Erase everything knows why they are in setup."""
    scope = read_file_abs("/run/biscuit-reset-done").strip()
    text = {"network": "This Echo's Wi-Fi networks and Bluetooth pairings were reset.",
            "all": "This Echo was erased: its account, networks and settings are gone."}.get(scope)
    return '<p class="note reset">{}</p>'.format(html.escape(text)) if text else ""


def read_file_abs(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render(template, **values):
    """Fill {name} placeholders, and nothing else.

    NOT str.format. These templates are mostly CSS, and CSS is made of braces:
    with .format every one of them had to be written doubled, which is a rule
    that holds right up until someone adds a rule and forgets. Substituting only
    the handful of known names means the stylesheet can be written as ordinary
    CSS, and an unknown placeholder is left alone rather than raising KeyError
    in the middle of serving the setup page.
    """
    return _PLACEHOLDER.sub(
        lambda m: str(values.get(m.group(1), m.group(0))), template)


ACCOUNT_GROUP = """  <div class="group">
    <h2>Your account</h2>
    <div class="boxed">
      <div class="field">
        <label for="user">Username</label>
        <!-- The hyphen in the character class is ESCAPED, and must stay that
             way. Browsers compile this attribute with the regex `v` flag, and
             `[a-z0-9_-]` is a syntax error under `v` ("Invalid character in
             character class"). A pattern that fails to compile is IGNORED
             rather than reported, so the unescaped version silently validated
             nothing at all - "BadUser!" sailed through. The server check at
             re.match below is the authority and was never affected; this is
             about telling someone before they submit. -->
        <input id="user" name="user" autocapitalize="none" autocorrect="off"
               spellcheck="false" autocomplete="username"
               pattern="[a-z_][a-z0-9\\-_]*" maxlength="32"
               placeholder="lowercase letters, digits, - and _">
      </div>
      <div class="field">
        <label for="pass">Password</label>
        <input id="pass" name="pass" type="password" autocapitalize="none"
               autocorrect="off" autocomplete="new-password" minlength="8"
               aria-describedby="passnote">
        <p class="note" id="passnote">This is the device&#39;s login. You will
          need it to sign in to the settings page and to connect over SSH. At
          least 8 characters.</p>
      </div>
      <div class="field">
        <label for="pass2">Repeat the password</label>
        <input id="pass2" name="pass2" type="password" autocapitalize="none"
               autocorrect="off" autocomplete="new-password" minlength="8">
      </div>
      <div class="field inline js-only" hidden>
        <input type="checkbox" id="showpass" class="reveal" data-for="pass pass2">
        <label for="showpass">Show the password</label>
      </div>
    </div>
  </div>
"""

# For a device that already has an account - setup started from its button to
# change network. The account is kept; forgetting its password is the one
# thing that could otherwise need a reinstall, so it can be reset here, by
# someone who proves they are at the Echo. Script only: the proof is a live
# conversation with the page.
RESET_GROUP = """  <div class="group">
    <h2>Your account</h2>
    <div class="boxed">
      <div class="field">
        <p class="plain">This Echo&#39;s account is <strong>{owner}</strong>. It
          stays as it is; sign in to the settings page with it afterwards.</p>
      </div>
      <div class="pkg choice js-only" hidden>
        <input type="checkbox" id="reset" name="reset" value="1" aria-controls="resetbox">
        <div><label for="reset">Reset its password</label>
        <p class="note">If you have forgotten it. You will need to be next to
          the Echo.</p></div>
      </div>
      <div id="resetbox" hidden>
        <div class="field">
          <p class="plain">Press the <strong>microphone button</strong> on top of
            the Echo <strong>twice</strong>. That shows you are with the device
            itself, not just near its setup network. Two presses leave the
            microphone as it was.</p>
          <button type="button" class="second" id="presence-start">Start</button>
          <p class="note" id="presence-status" role="status" aria-live="polite"></p>
          <input type="hidden" id="presence" name="presence" value="">
        </div>
        <div class="field" id="newpass" hidden>
          <label for="pass">New password</label>
          <input id="pass" name="pass" type="password" autocapitalize="none"
                 autocorrect="off" autocomplete="new-password" minlength="8" disabled>
          <p class="note">For the settings page and SSH. At least 8 characters.</p>
        </div>
        <div class="field" id="newpass2" hidden>
          <label for="pass2">Repeat the new password</label>
          <input id="pass2" name="pass2" type="password" autocapitalize="none"
                 autocorrect="off" autocomplete="new-password" minlength="8" disabled>
        </div>
        <div class="field inline" id="newshow" hidden>
          <input type="checkbox" id="showpass" class="reveal" data-for="pass pass2">
          <label for="showpass">Show the password</label>
        </div>
      </div>
    </div>
  </div>
"""


PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Set up {ssid}</title>
<style>
/* The same Adwaita tokens the :8080 settings page uses, so the device looks
   like one product from the first screen. Self-contained for the same reason,
   only more so: at this point the device has no network at all, and anything
   not already in the image cannot be fetched. */
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
/* The UA's own [hidden] rule loses to any author rule that sets display, and
   .pkg and .field.inline set flex - which showed script-only controls with
   script off, and kept the picker's Filter from hiding anything. */
[hidden] { display:none !important; }
body {
  margin:0; padding:1.75rem 1rem 3rem; background:var(--bg); color:var(--fg);
  font:15px/1.45 -apple-system,system-ui,"Cantarell","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
form, .head, .foot { max-width:30rem; margin-inline:auto; }
.head { text-align:center; margin-bottom:1.6rem; }
.head .mark { color:var(--accent); display:flex; justify-content:center; margin-bottom:.5rem; }
h1 { font-size:1.3rem; margin:0 0 .2rem; }
.head p { color:var(--dim); margin:0; font-size:.9rem; }

.group { margin:0 0 1.3rem; }
.group > h2 {
  font-size:.78rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
  color:var(--dim); margin:0 0 .45rem .15rem;
}
.boxed {
  background:var(--card); border-radius:12px; overflow:hidden;
  box-shadow:0 1px 2px rgba(0,0,0,.06), 0 2px 8px rgba(0,0,0,.04);
}
.field { padding:.7rem .9rem; border-top:1px solid var(--line); }
.boxed > .field:first-child { border-top:none; }
.field > label { display:block; font-weight:500; margin-bottom:.3rem; }
.field input, .field select {
  font:inherit; color:var(--fg); background:var(--bg); width:100%;
  border:1px solid var(--line); border-radius:8px; padding:.55rem .6rem;
}
.field input:focus, .field select:focus { outline:2px solid var(--accent); outline-offset:-1px; }
.note { font-size:.82rem; color:var(--dim); margin:.35rem 0 0; }
.note strong { color:var(--fg); }
.plain { margin:0 0 .4rem; }
.field.inline { display:flex; align-items:center; gap:.55rem; }
.field.inline input { width:auto; margin:0; accent-color:var(--accent); }
.field.inline label { margin:0; font-weight:400; }
button.second { width:auto; margin-top:.2rem; background:var(--card); color:var(--fg);
                border:1px solid var(--line); padding:.5rem 1rem; }
button.linkish { width:auto; margin:0; padding:0; background:none; color:var(--accent);
                 font-weight:500; font-size:.9rem; }
ul.note { padding-left:1.1rem; }
ul.note li { margin:.25rem 0; }
.foot strong { color:var(--fg); font-variant-numeric:tabular-nums; }
.foot.late { color:var(--danger); }
.sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); }
button:focus-visible, input:focus-visible, .dlg .opt:focus-within {
  outline:2px solid var(--accent); outline-offset:2px; }
.dlg .opt .sig { color:var(--dim); display:flex; }
.dlg .opt .meta { display:block; font-size:.78rem; color:var(--dim); }

.pkg { display:flex; gap:.65rem; align-items:flex-start;
       padding:.7rem .9rem; border-top:1px solid var(--line); }
.boxed > .pkg:first-child { border-top:none; }
.pkg input { width:auto; margin:.2rem 0 0; flex:0 0 auto; accent-color:var(--accent); }
.pkg label { font-weight:500; margin:0; }
.pkg .note { margin-top:.1rem; }
.mono { font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace; font-size:.78rem; }
.note.reset { margin-top:.7rem; color:var(--fg); }

/* The settings page's list preference: the current choice in accent colour,
   and a list of options when tapped. Script only; the <select> underneath is
   what the form submits, and without script it is simply shown. */
.choice-btn { display:block; width:100%; text-align:left; background:var(--bg);
              color:var(--accent); border:1px solid var(--line); border-radius:8px;
              padding:.55rem .6rem; font:inherit; cursor:pointer; margin-top:0; }
.scrim { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:40;
         display:flex; align-items:center; justify-content:center; padding:1rem; }
.scrim[hidden] { display:none; }
.dlg { background:var(--card); border-radius:14px; box-shadow:0 8px 32px rgba(0,0,0,.35);
       width:min(23rem,100%); max-height:90vh; display:flex; flex-direction:column; padding:1.1rem; }
.dlg h3 { margin:0 0 .6rem; font-size:1.05rem; }
.dlg .opts { overflow:auto; margin:0 -.3rem .6rem; }
.dlg .opt { display:flex; align-items:center; gap:.75rem; padding:.6rem .3rem; cursor:pointer;
            border-radius:8px; font-weight:400; margin:0; }
.dlg .opt input { accent-color:var(--accent); width:1.1rem; height:1.1rem; margin:0; flex:0 0 auto; }
.dlg .grp { font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
            color:var(--dim); padding:.7rem .3rem .2rem; }
.dlg .filter { margin:0 0 .6rem; }
.dlg .acts { display:flex; justify-content:flex-end; }
.dlg .acts button { width:auto; background:none; color:var(--accent); border:none; margin:0; }

/* THE WIZARD IS PROGRESSIVE ENHANCEMENT, AND THIS IS THE HALF THAT MATTERS.
   Every step is visible by default, so a browser with no script gets exactly
   the long single form this page used to be and the one Connect button
   submits it. Script adds .js to the form, and only then does one-step-at-a-
   time styling apply. Written this way round deliberately: the failure mode
   of the opposite (hide by default, reveal with script) is a device that
   cannot be set up at all. */
.step { display:block; }
form.js .step { display:none; }
form.js .step.on { display:block; }

.steps { display:flex; gap:.4rem; list-style:none; padding:0;
         margin:0 0 1.3rem; counter-reset:s; }
.steps li { counter-increment:s; flex:1 1 0; font-size:.74rem; color:var(--dim);
            text-align:center; padding-top:1.55rem; position:relative;
            line-height:1.2; }
.steps li::before {
  content:counter(s); position:absolute; top:0; left:50%;
  transform:translateX(-50%); width:1.25rem; height:1.25rem; border-radius:50%;
  background:var(--line); color:var(--dim); font-weight:600; line-height:1.25rem;
}
.steps li.on { color:var(--fg); font-weight:600; }
.steps li.on::before { background:var(--accent); color:var(--accent-fg); }
.steps li.done::before { background:var(--accent); color:var(--accent-fg);
                         content:"\\2713"; }

/* The base button rule is width:100%, which a flex row inherits as the
   flex-basis. Left alone, "Back" claims the whole row and cannot shrink
   (flex-shrink:0), and "Next" is squeezed to its text - the exact opposite of
   what is wanted. So width is reset here before any flex sizing is asked for.
   margin-top moves to the row, or each button contributes its own and the gap
   below the form doubles. */
.nav { display:flex; gap:.6rem; margin-top:1.6rem; }
.nav button { width:auto; margin-top:0; }
.nav button#next, .nav button#go { flex:1 1 auto; }
.nav button#back { flex:0 0 auto; min-width:7rem;
                   background:var(--card); color:var(--fg);
                   border:1px solid var(--line); }

/* What a package actually installs. A disclosure rather than a wall of text:
   most people will never open it, and the ones who do are asking a specific
   question. */
/* The WPS row borrows the package row's layout but is not a package -
   a tinted edge keeps a choice about Wi-Fi from reading as an app to
   install, and gives it a selector of its own. */
.pkg.choice { border-left:3px solid var(--accent); padding-left:.6rem; }

.pkg details { margin-top:.4rem; }
.pkg summary { font-size:.8rem; color:var(--accent); cursor:pointer;
               list-style:revert; }
.pkg details ul { margin:.4rem 0 0; padding-left:1.1rem; }
.pkg details li { font-size:.8rem; color:var(--dim); margin:.15rem 0; }

button {
  width:100%; margin-top:1.6rem; padding:.75rem; font:inherit; font-weight:600;
  font-size:1rem; border:0; border-radius:9px;
  background:var(--accent); color:var(--accent-fg); cursor:pointer;
}
button:hover { filter:brightness(1.08); }

.err {
  max-width:30rem; margin:0 auto 1.2rem; padding:.8rem .9rem; border-radius:10px;
  background:color-mix(in srgb, var(--danger) 16%, transparent);
  border:1px solid color-mix(in srgb, var(--danger) 45%, transparent);
  font-size:.9rem;
}
.foot { color:var(--dim); font-size:.82rem; margin-top:1.4rem; text-align:center; }
</style>

<div class="head">
  <div class="mark">
    <svg width="44" height="44" viewBox="0 0 16 16" fill="none" stroke="currentColor"
         stroke-width="1.2" aria-hidden="true">
      <circle cx="8" cy="8" r="6.6"/><circle cx="8" cy="8" r="2.7"/>
    </svg>
  </div>
  <h1>Set up {ssid}</h1>
  {resetnote}
  <p>Three steps: you and this Echo - your login, its region, time zone and
    name - then the Wi-Fi network to join, and any apps you want.</p>
</div>
<div class="sr" id="announce" aria-live="polite"></div>

{error}

<!-- ONE FORM, THREE VIEWS.
     The steps are sections of a single form shown one at a time by script,
     not three pages with three POSTs. Two reasons, both load-bearing. The
     secrets are boxed and posted ONCE at the end, so a half-finished wizard
     never puts a password on the air; and the portal keeps no per-client
     state, which matters because this listens on an OPEN network where any
     phone in range can hit it and a second visitor would otherwise walk into
     the first one's half-filled session.
     Without script every section is simply visible and the single button
     submits the lot - the same form this page used to be. -->
<form method="POST" action="/save" id="setup">
<input type="hidden" id="enc" name="enc" value="">

<ol class="steps" id="steps" aria-label="Steps" hidden>
  <li data-for="1">Account</li>
  <li data-for="2">Wi-Fi</li>
  <li data-for="3">Apps</li>
</ol>

<section class="step" data-step="1">
{account}
  <div class="group">
    <h2>Region</h2>
    <div class="boxed">
      <div class="field">
        <label for="region">Where is this device?</label>
        <select id="region" name="region" class="pick">
{regions}
        </select>
        <p class="note">Sets which Wi-Fi channels the device may use. Without
          it the kernel falls back to a worldwide default that will not
          transmit on most <strong>5&nbsp;GHz</strong> channels, so a 5&nbsp;GHz
          network may be listed on the next step and still refuse to connect.
          Picking the wrong country is not harmless - it is what keeps the radio
          inside the rules where you live.</p>
      </div>
    </div>
  </div>

  <div class="group">
    <h2>Time zone</h2>
    <div class="boxed">
      <div class="field">
        <label for="tz">Time zone</label>
        <select id="tz" name="tz" class="pick">
{timezones}
        </select>
        <p class="note">For the device's clock and logs. Your phone's own time
          zone is chosen for you; it can be changed later in Settings, under
          System.</p>
      </div>
    </div>
  </div>

  <div class="group">
    <h2>Device name</h2>
    <div class="boxed">
      <div class="field">
        <label for="name">Name</label>
        <input id="name" name="name" value="{name}" autocapitalize="none"
               autocorrect="off" spellcheck="false" maxlength="40"
               aria-describedby="namepreview" data-fallback="{ssid}">
        <p class="note" id="namepreview" aria-live="polite">Used for its
          Bluetooth name, in Home Assistant, and to reach its settings at
          <strong>{url}</strong> once it is online (with the name you choose in
          place of {name}).</p>
      </div>
      <div class="field">
        <label for="port">Settings page port</label>
        <input id="port" name="port" value="{port}" inputmode="numeric"
               pattern="[0-9]{2,5}" maxlength="5" aria-describedby="portnote"
               data-reserved="{reserved}">
        <p class="note" id="portnote">8080 unless you want another. 80 leaves
          the number out of the address. It can be changed later, on the
          settings page under About.</p>
      </div>
    </div>
  </div>
</section>

<section class="step" data-step="2">
  <!-- IS THIS THE RIGHT DEVICE?
       The one thing an attacker running a lookalike access point cannot do is
       make the real Echo in your room play a sound. So the check has to come
       BEFORE the password fields below - after them the credentials are
       already gone, and a confirmation would only be telling someone they
       have been robbed. -->
  <div class="group">
    <h2>Check it is your device</h2>
    <div class="boxed">
      <div class="field">
        <button type="button" id="chime">Play a sound on the device</button>
        <p class="note" id="chimenote" role="status" aria-live="polite">Press
          this, and the Echo you are setting up should play a sound. If you hear
          nothing, you may be connected to something pretending to be it.</p>
      </div>
      <div class="pkg choice">
        <input type="checkbox" id="heard" name="heard" value="1">
        <div><label for="heard">I heard the sound</label>
        <p class="note">Tick this once you have actually heard it. Nothing
          below is sent until you press Connect.</p></div>
      </div>
      <div class="field">
        <button type="button" class="linkish js-only" id="cant" hidden
                aria-expanded="false" aria-controls="cantbox">I can&rsquo;t hear it</button>
        <div id="cantbox">
          <ul class="note">
            <li>Turn the Echo up with its <strong>+</strong> button, then play
              the sound again.</li>
            <li>Check your phone is still connected to <strong>{ssid}</strong>,
              this Echo&rsquo;s setup network. Phones sometimes drop back to
              their usual Wi-Fi.</li>
            <li>Still nothing, and you are next to it? You may be connected to
              a different device. The safe choice is to stop here and start
              setup again from the Echo itself.</li>
          </ul>
          <div class="pkg choice">
            <input type="checkbox" id="skip" name="heard" value="skip">
            <div><label for="skip">Continue without the check</label>
            <p class="note">For example if you cannot hear it. Only if you are
              sure this is your Echo.</p></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="group">
    <h2>Wi-Fi</h2>
    <div class="boxed">
      <div class="field">
        <label for="ssid">Network</label>
        <select id="ssid" name="ssid_pick" class="pick">
          {options}
          <option value="__other__">Other network&hellip;</option>
        </select>
        <p class="note">Both bands work. If a <strong>5&nbsp;GHz</strong>
          network is listed but will not connect, try its 2.4&nbsp;GHz side -
          a few upper 5&nbsp;GHz channels are unavailable until the device
          learns its region.</p>
      </div>
      <div class="field" id="otherfield">
        <label for="ssid_other">Network name, if you chose Other</label>
        <input id="ssid_other" name="ssid_other" autocapitalize="none"
               autocorrect="off" spellcheck="false"
               placeholder="Leave blank unless using Other">
      </div>
      <div class="field">
        <label for="psk">Wi-Fi password</label>
        <input id="psk" name="psk" type="password" autocapitalize="none"
               autocorrect="off" placeholder="Leave blank if the network is open"
               minlength="8" maxlength="63">
      </div>
      <div class="field inline js-only" hidden>
        <input type="checkbox" id="showpsk" class="reveal" data-for="psk">
        <label for="showpsk">Show the password</label>
      </div>
      <div class="pkg choice">
        <input type="checkbox" id="wps" name="wps" value="1">
        <div><label for="wps">Use my router&rsquo;s WPS button instead</label>
        <p class="note">The best option if your router has one: your Wi-Fi
          password is never typed here and never crosses the air at all. Tick
          this, press Connect, then press WPS on your router within two minutes.
          Leave the network and password above blank.</p></div>
      </div>
    </div>
  </div>
</section>

<section class="step" data-step="3">
  {packages}
</section>

  <p id="secnote" class="note">{secnote}</p>
  <div class="nav">
    <button type="button" id="back" hidden>Back</button>
    <button type="button" id="next" hidden>Next</button>
    <button type="submit" id="go">Connect</button>
  </div>
</form>
<script src="/n.js"></script>
<script>
/* Box the two secret fields before the form is posted.
 *
 * WebCrypto is not an option here: crypto.subtle is restricted to secure
 * contexts, and this page is http:// on a bare IP. crypto.getRandomValues IS
 * available in an insecure context, though, which is the one piece that cannot
 * be done in pure JS - so the nonce and the ephemeral key come from there and
 * the rest from TweetNaCl.
 *
 * The plaintext inputs are emptied and disabled once boxed, so a browser that
 * helpfully re-submits, or a retry after a validation bounce, cannot leak what
 * this just protected. Disabled fields are not submitted at all.
 */
(function () {
  var form = document.getElementById("setup");
  var pk = "{pubkey}";
  var banner = document.getElementById("secnote");
  if (!form) return;

  var ok = pk && window.nacl && window.crypto && window.crypto.getRandomValues;
  if (!ok) {
    /* Fail OPEN, but say so. This is the only way into a new device: a form
       that refuses to submit would leave someone with no way to set it up at
       all. So the submission still works, and the page stops claiming to be
       protected rather than quietly lying about it. */
    if (banner) {
      banner.className = "note warn";
      banner.textContent =
        "This browser could not secure the connection, so your passwords will "
        + "be sent unprotected over this setup network. They are still only "
        + "sent to this device.";
    }
    return;
  }

  function b64(u8) {
    var s = "";
    for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }
  function unb64(str) {
    var raw = atob(str), u8 = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) u8[i] = raw.charCodeAt(i);
    return u8;
  }

  form.addEventListener("submit", function (ev) {
    var psk = document.getElementById("psk");
    var pass = document.getElementById("pass");
    var pass2 = document.getElementById("pass2");
    var enc = document.getElementById("enc");
    if (!enc || enc.value) return;             /* already boxed */

    try {
      var payload = JSON.stringify({
        psk: psk ? psk.value : "",
        pass: pass && !pass.disabled ? pass.value : "",
        pass2: pass2 && !pass2.disabled ? pass2.value : "",
        proof: window.__presenceProof || ""
      });
      var msg = nacl.util ? nacl.util.decodeUTF8(payload)
                          : new TextEncoder().encode(payload);
      var eph = nacl.box.keyPair();
      var nonce = new Uint8Array(nacl.box.nonceLength);
      window.crypto.getRandomValues(nonce);
      var ct = nacl.box(msg, nonce, unb64(pk), eph.secretKey);
      enc.value = b64(eph.publicKey) + "." + b64(nonce) + "." + b64(ct);

      /* Only now is it safe to drop the plaintext. */
      [psk, pass, pass2].forEach(function (f) {
        if (f) { f.value = ""; f.disabled = true; }
      });
    } catch (e) {
      /* Leave the plaintext fields alone and let the post go through
         unprotected rather than stranding someone mid-setup. */
      ev.returnValue = true;
    }
  });
})();
</script>
<script>
/* The settings page's pickers, for the three lists on this form. */
(function () {
  var zone = "";
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
  var tz = document.getElementById("tz");
  if (tz && zone) {
    for (var i = 0; i < tz.options.length; i++) {
      if (tz.options[i].value === zone) { tz.value = zone; break; }
    }
  }

  /* Parts of the form that only work with script. */
  [].slice.call(document.querySelectorAll(".js-only")).forEach(function (n) { n.hidden = false; });

  var BARS = function (n) {
    var s = '<svg width="18" height="16" viewBox="0 0 18 16" aria-hidden="true">';
    for (var i = 0; i < 4; i++) {
      s += '<rect x="' + (i * 4.5) + '" y="' + (13 - i * 3.4) + '" width="3" height="' +
           (2.6 + i * 3.4) + '" rx="1" fill="currentColor" opacity="' + (i < n ? "1" : ".22") + '"/>';
    }
    return s + "</svg>";
  };

  var scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.hidden = true;
  document.body.appendChild(scrim);
  var closeOpen = null;

  function open(sel, title, done) {
    var dlg = document.createElement("div");
    dlg.className = "dlg";
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-modal", "true");
    var h = document.createElement("h3");
    h.id = "dlg-" + sel.id;
    h.textContent = title;
    dlg.setAttribute("aria-labelledby", h.id);
    dlg.appendChild(h);
    var list = document.createElement("div");
    list.className = "opts";
    list.setAttribute("role", "radiogroup");
    list.setAttribute("aria-labelledby", h.id);
    var name = "p" + Math.random().toString(36).slice(2);
    var arrowing = false;
    [].slice.call(sel.children).forEach(function (node) {
      var items = node.tagName === "OPTGROUP" ? [].slice.call(node.children) : [node];
      if (node.tagName === "OPTGROUP") {
        var g = document.createElement("div");
        g.className = "grp";
        g.textContent = node.label;
        list.appendChild(g);
      }
      items.forEach(function (o) {
        var row = document.createElement("label");
        row.className = "opt";
        var r = document.createElement("input");
        r.type = "radio"; r.name = name; r.checked = o.value === sel.value;
        row.appendChild(r);
        if (o.hasAttribute("data-bars")) {
          var sig = document.createElement("span");
          sig.className = "sig";
          sig.innerHTML = BARS(Number(o.getAttribute("data-bars")));
          row.appendChild(sig);
        }
        var t = document.createElement("span");
        t.textContent = o.textContent;
        var meta = [o.getAttribute("data-band"),
                    o.getAttribute("data-lock") === "0" ? "open" : (o.hasAttribute("data-lock") ? "secured" : "")]
                   .filter(Boolean).join(" · ");
        if (meta) {
          var m = document.createElement("span");
          m.className = "meta";
          m.textContent = meta;
          t.appendChild(m);
        }
        row.appendChild(t);
        var commit = function () {
          sel.value = o.value;
          sel.dispatchEvent(new Event("change"));
          close();
        };
        /* A radio group checks whatever the arrow keys move to, and closing on
           that chose the first neighbour. Arrows browse; Enter or Space, or a
           tap, chooses. */
        r.addEventListener("change", function () {
          if (arrowing) { arrowing = false; return; }
          commit();
        });
        r.addEventListener("keydown", function (e) {
          if (e.key.indexOf("Arrow") === 0) { arrowing = true; }
          else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); commit(); }
        });
        list.appendChild(row);
      });
    });
    if (sel.options.length > 12) {
      var f = document.createElement("input");
      f.type = "search"; f.className = "filter"; f.placeholder = "Filter";
      f.setAttribute("aria-label", "Filter");
      f.addEventListener("input", function () {
        var q = f.value.trim().toLowerCase();
        [].slice.call(list.children).forEach(function (c) {
          c.hidden = !!q && (c.className === "grp" || c.textContent.toLowerCase().indexOf(q) < 0);
        });
      });
      dlg.appendChild(f);
    }
    dlg.appendChild(list);
    var acts = document.createElement("div");
    acts.className = "acts";
    var cancel = document.createElement("button");
    cancel.type = "button"; cancel.textContent = "Cancel";
    cancel.addEventListener("click", close);
    acts.appendChild(cancel);
    dlg.appendChild(acts);
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (e.key !== "Tab") return;
      /* Keep the keyboard inside the dialog while it is open. */
      var f = [].slice.call(dlg.querySelectorAll("input, button")).filter(function (x) {
        return !x.hidden && !x.disabled && x.offsetParent !== null &&
               (x.type !== "radio" || x.checked || !dlg.querySelector('input[name="' + x.name + '"]:checked'));
      });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    function close() {
      document.removeEventListener("keydown", onKey);
      scrim.hidden = true; scrim.innerHTML = ""; closeOpen = null;
      if (done) done();
    }
    closeOpen = close;
    document.addEventListener("keydown", onKey);
    scrim.innerHTML = "";
    scrim.appendChild(dlg);
    scrim.hidden = false;
    var on = list.querySelector("input:checked") || list.querySelector("input");
    if (on) { on.parentNode.scrollIntoView({ block: "center" }); on.focus(); }
  }
  scrim.addEventListener("click", function (e) { if (e.target === scrim && closeOpen) closeOpen(); });

  [].slice.call(document.querySelectorAll("select.pick")).forEach(function (sel) {
    var label = document.querySelector('label[for="' + sel.id + '"]');
    var title = label ? label.textContent : "Choose";
    var b = document.createElement("button");
    b.type = "button"; b.className = "choice-btn";
    b.id = sel.id + "-btn";
    b.setAttribute("aria-haspopup", "dialog");
    if (label) label.setAttribute("for", b.id);
    function sync() {
      var o = sel.options[sel.selectedIndex];
      b.textContent = o ? o.textContent : "";
      b.setAttribute("aria-label", title + ": " + (o ? o.textContent : ""));
    }
    sync();
    sel.addEventListener("change", sync);
    b.addEventListener("click", function () {
      open(sel, title, function () { b.focus(); });
    });
    sel.hidden = true;
    sel.parentNode.insertBefore(b, sel);
  });

  /* The typed network name only matters when "Other" is chosen - or when
     the scan found nothing, whose placeholder has no name either. Without a
     name (and without WPS) the step does not go on: the server would bounce
     the whole form, losing what was typed and any password-reset check. */
  var ssid = document.getElementById("ssid");
  var other = document.getElementById("otherfield");
  var otherName = document.getElementById("ssid_other");
  var wpsBox = document.getElementById("wps");
  if (ssid && other) {
    var show = function () {
      var needName = ssid.value === "__other__" || ssid.value === "";
      other.hidden = !needName;
      if (otherName) otherName.required = needName && !(wpsBox && wpsBox.checked);
    };
    ssid.addEventListener("change", show);
    if (wpsBox) wpsBox.addEventListener("change", show);
    show();
  }

  /* "Show the password": each box lists the fields it reveals. */
  [].slice.call(document.querySelectorAll("input.reveal")).forEach(function (box) {
    box.addEventListener("change", function () {
      box.getAttribute("data-for").split(" ").forEach(function (id) {
        var f = document.getElementById(id);
        if (f) f.type = box.checked ? "text" : "password";
      });
    });
  });

  /* The two password boxes must agree before the step can be left. */
  var pass = document.getElementById("pass"), pass2 = document.getElementById("pass2");
  if (pass && pass2) {
    var same = function () {
      pass2.setCustomValidity(pass2.value === pass.value ? "" : "The two passwords do not match.");
    };
    pass.addEventListener("input", same);
    pass2.addEventListener("input", same);
    same();
  }

  /* What the name becomes, and where the settings will be, as it is typed -
     the same rule the device applies: letters, digits and hyphens. */
  var nameEl = document.getElementById("name"), port = document.getElementById("port");
  var preview = document.getElementById("namepreview");
  if (nameEl && preview) {
    var fallback = nameEl.getAttribute("data-fallback") || "biscuit";
    var draw = function () {
      var n = nameEl.value.replace(/[^A-Za-z0-9-]/g, "-").replace(/^-+|-+$/g, "")
                          .slice(0, 32).replace(/-+$/, "") || fallback;
      var p = port ? port.value.trim() : "8080";
      var url = "http://" + n.toLowerCase() + ".local" + (p === "80" ? "" : ":" + p);
      preview.textContent = "";
      preview.appendChild(document.createTextNode("Will be called "));
      var b1 = document.createElement("strong"); b1.textContent = n; preview.appendChild(b1);
      preview.appendChild(document.createTextNode(" · settings at "));
      var b2 = document.createElement("strong"); b2.textContent = url; preview.appendChild(b2);
      preview.appendChild(document.createTextNode(". Also its Bluetooth name and its name in Home Assistant."));
      if (port) {
        var v = Number(p);
        var taken = (port.getAttribute("data-reserved") || "").split(",").indexOf(String(v)) >= 0;
        port.setCustomValidity(!/^[0-9]+$/.test(p) || !(v === 80 || (v >= 1024 && v <= 65535))
          ? "Use 8080, 80, or a number from 1024 to 65535."
          : taken ? "The Echo uses that port already, or browsers refuse it. Try 8080 or 8000." : "");
      }
    };
    nameEl.addEventListener("input", draw);
    if (port) port.addEventListener("input", draw);
    draw();
  }

  /* The sound check must be answered one way or the other. */
  var heard = document.getElementById("heard"), skip = document.getElementById("skip");
  var cant = document.getElementById("cant"), cantbox = document.getElementById("cantbox");
  if (heard && skip) {
    var answered = function () {
      heard.setCustomValidity(heard.checked || skip.checked ? "" :
        "Play the sound and tick this - or choose “I can’t hear it”.");
    };
    heard.addEventListener("change", function () { if (heard.checked) skip.checked = false; answered(); });
    skip.addEventListener("change", function () { if (skip.checked) heard.checked = false; answered(); });
    answered();
  }
  if (cant && cantbox) {
    cantbox.hidden = true;
    cant.addEventListener("click", function () {
      cantbox.hidden = !cantbox.hidden;
      cant.setAttribute("aria-expanded", String(!cantbox.hidden));
    });
  }

  /* Resetting the password: two presses of the microphone button. */
  var reset = document.getElementById("reset"), box = document.getElementById("resetbox");
  if (reset && box) {
    var startBtn = document.getElementById("presence-start");
    var status = document.getElementById("presence-status");
    var token = document.getElementById("presence");
    var fields = ["newpass", "newpass2", "newshow"].map(function (id) { return document.getElementById(id); });
    var timer = null, proven = false;
    var need = function () {
      reset.setCustomValidity(reset.checked && !proven ?
        "Press the microphone button on the Echo twice first, or untick this." : "");
    };
    var reveal = function (on) {
      fields.forEach(function (f) { if (f) f.hidden = !on; });
      [pass, pass2].forEach(function (f) { if (f) { f.disabled = !on; f.required = on; } });
    };
    reset.addEventListener("change", function () {
      box.hidden = !reset.checked;
      if (!reset.checked) {
        /* A check left running would stop being polled; start clean. */
        reveal(false); clearInterval(timer);
        if (!proven) {
          startBtn.disabled = false; token.value = ""; status.textContent = "";
          window.__presenceProof = "";
        }
      } else if (proven) reveal(true);
      need();
    });
    var hex = function (u8) {
      var out = "";
      for (var i = 0; i < u8.length; i++) out += ("0" + u8[i].toString(16)).slice(-2);
      return out;
    };
    startBtn.addEventListener("click", function () {
      clearInterval(timer);
      /* The check's id travels in the clear on the open setup network, so it
         proves nothing by itself. A secret made here does: only its hash is
         sent now, and the secret itself goes inside the encrypted form. */
      if (!window.nacl || !window.crypto || !window.crypto.getRandomValues) {
        status.textContent = "This browser cannot do the check securely. Try another browser.";
        return;
      }
      var secret = new Uint8Array(32);
      window.crypto.getRandomValues(secret);
      window.__presenceProof = hex(secret);
      startBtn.disabled = true;
      status.textContent = "Starting…";
      fetch("/presence/start", { method: "POST",
                                 headers: { "Content-Type": "application/x-www-form-urlencoded" },
                                 body: "hash=" + hex(nacl.hash(secret)) }).then(function (r) {
        return r.json().then(function (d) { return [r.ok, d]; });
      }).then(function (res) {
        var ok = res[0], d = res[1];
        if (!ok) throw new Error(d.error || "The check could not start.");
        token.value = d.id;
        status.textContent = "Now press the microphone button twice.";
        timer = setInterval(function () {
          fetch("/presence?id=" + encodeURIComponent(d.id)).then(function (r) { return r.json(); }).then(function (st) {
            if (st.contested) {
              clearInterval(timer); startBtn.disabled = false; token.value = "";
              status.textContent = "Another phone on this setup network started the same check, so " +
                "neither counts. If that was not you, stop here." +
                (st.retry_in ? " Otherwise press Start again in " + st.retry_in + " seconds." : " Otherwise press Start again.");
            } else if (st.done) {
              clearInterval(timer); proven = true; need(); reveal(true);
              status.textContent = "Confirmed. Choose the new password.";
              if (pass) pass.focus();
            } else if (!st.left) {
              clearInterval(timer); startBtn.disabled = false; token.value = "";
              status.textContent = "Time ran out. Press Start to try again.";
            } else {
              status.textContent = "Pressed " + st.presses + " of " + st.need + " times · " + st.left + " s left.";
            }
          }).catch(function () {});
        }, 700);
      }).catch(function (e) {
        startBtn.disabled = false;
        status.textContent = e.message;
      });
    });
    need();
  }
})();
</script>
<script>
/* Show one step at a time.
 *
 * The form is already complete and submittable without any of this - see the
 * .step rules in the stylesheet. All this does is hide the sections that are
 * not the current one and move a pointer, so the worst failure here is the
 * long form someone would otherwise have got.
 *
 * Validation is per step rather than only at the end: HTML5 will happily
 * refuse a submit for an invalid field three screens back, and on a phone the
 * browser scrolls to a field the person cannot see with no explanation.
 */
/* The "play a sound" button. Fire-and-forget: the answer that matters arrives
   through the air, not through this request, so nothing here waits on it. */
(function () {
  var b = document.getElementById("chime");
  var note = document.getElementById("chimenote");
  if (!b) return;
  b.addEventListener("click", function () {
    b.disabled = true;
    fetch("/chime").then(function (r) {
      if (note) note.textContent = r.status === 429
        ? "Wait a few seconds before playing it again."
        : "Played. If you heard nothing from the Echo you are setting up, see "
          + "\u201cI can\u2019t hear it\u201d below.";
    }).catch(function () {}).then(function () {
      setTimeout(function () { b.disabled = false; }, 3000);
    });
  });
})();

(function () {
  var form = document.getElementById("setup");
  if (!form) return;
  var steps = [].slice.call(form.querySelectorAll(".step"));
  if (steps.length < 2) return;

  var crumbs = document.getElementById("steps");
  var back = document.getElementById("back");
  var next = document.getElementById("next");
  var go = document.getElementById("go");
  var at = 0;

  form.classList.add("js");
  if (crumbs) crumbs.hidden = false;

  function draw(moved) {
    steps.forEach(function (s, i) { s.classList.toggle("on", i === at); });
    if (crumbs) {
      [].slice.call(crumbs.children).forEach(function (li, i) {
        li.classList.toggle("on", i === at);
        li.classList.toggle("done", i < at);
        if (i === at) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      });
    }
    /* Someone using a screen reader hears where they are, and the keyboard
       starts at the top of the new step rather than on a hidden button. */
    if (moved) {
      var h = steps[at].querySelector("h2");
      if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
      var say = document.getElementById("announce");
      if (say) say.textContent = "Step " + (at + 1) + " of " + steps.length +
        (crumbs ? ": " + crumbs.children[at].textContent : "");
    }
    back.hidden = at === 0;
    next.hidden = at === steps.length - 1;
    go.hidden = at !== steps.length - 1;
    /* Put the top of the new step where the eye is, not wherever the last one
       happened to leave the page. */
    window.scrollTo(0, 0);
  }

  function valid() {
    var fields = [].slice.call(steps[at].querySelectorAll("input, select"))
      .filter(function (f) { return !f.hidden; });
    for (var i = 0; i < fields.length; i++) {
      if (!fields[i].checkValidity()) {
        fields[i].reportValidity();
        return false;
      }
    }
    return true;
  }

  next.addEventListener("click", function () {
    if (!valid()) return;
    at = Math.min(at + 1, steps.length - 1);
    draw(true);
  });
  back.addEventListener("click", function () {
    at = Math.max(at - 1, 0);
    draw(true);
  });

  /* Enter in a text field should advance, not submit from step one - the
     browser's default is to submit the whole form, which would post a
     half-filled wizard. */
  form.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    if (e.target.tagName === "TEXTAREA") return;
    if (at < steps.length - 1) {
      e.preventDefault();
      next.click();
    }
  });

  draw();
})();
</script>

<p class="foot">Everything here is sent over this device's own setup network,
  which shuts down as soon as it has what it needs.</p>
<p class="foot" id="window" hidden></p>
<script>
/* How long the setup network stays up. It closes by itself after ten
   minutes, which used to happen without a word; while the page is being used
   the Echo keeps it open, a few minutes at a time, up to half an hour. */
(function () {
  var box = document.getElementById("window");
  var say = document.getElementById("announce");
  var go = document.getElementById("go");
  if (!box || !window.fetch) return;
  var left = null, final = false, used = Date.now(), said = {};
  ["input", "change", "click", "keydown", "touchstart"].forEach(function (ev) {
    document.addEventListener(ev, function () { used = Date.now(); }, { passive: true });
  });
  function fmt(s) { var m = Math.floor(s / 60), r = s % 60; return m + ":" + (r < 10 ? "0" : "") + r; }
  function show() {
    if (left === null) return;
    box.hidden = false;
    if (left <= 0) {
      box.className = "foot late";
      box.textContent = "This setup network has closed. Unplug the Echo and plug it back in to start setup again.";
      if (go) go.disabled = true;
      if (say && !said.closed) { said.closed = 1; say.textContent = box.textContent; }
      return;
    }
    box.className = "foot" + (left <= 60 ? " late" : "");
    box.innerHTML = "This setup network closes in <strong>" + fmt(left) + "</strong>" +
      (final ? "." : ", or later while you are using this page.");
    [300, 120, 60].forEach(function (mark) {
      if (left <= mark && left > mark - 5 && !said[mark] && say) {
        said[mark] = 1;
        say.textContent = "Setup closes in " + (mark / 60) + (mark === 60 ? " minute." : " minutes.");
      }
    });
  }
  function sync() {
    var busy = Date.now() - used < 60000;
    fetch("/alive" + (busy ? "?use=1" : ""), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.left != null) { left = d.left; final = !!d.final; show(); } })
      .catch(function () {});
  }
  sync();
  setInterval(function () { if (left !== null && left > 0) { left -= 1; show(); } }, 1000);
  setInterval(sync, 15000);
})();
</script>
"""

DONE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Connecting</title>
<style>
:root {
  --bg:#fafafa; --card:#fff; --fg:#2e3436; --dim:rgba(0,0,0,.55);
  --line:rgba(0,0,0,.08); --accent:#3584e4;
  --ok:#2ec27e; --danger:#e01b24;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark) {
  :root {
    --bg:#242424; --card:#303030; --fg:#fff; --dim:rgba(255,255,255,.55);
    --line:rgba(255,255,255,.1); --accent:#78aeed; --ok:#8ff0a4; --danger:#ff7b63;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; padding:2rem 1rem 3rem; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system,system-ui,"Cantarell","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:30rem; margin-inline:auto; }
.head { text-align:center; margin-bottom:1.5rem; }
h1 { font-size:1.25rem; margin:.6rem 0 .3rem; }
.head p { color:var(--dim); margin:0; font-size:.9rem; }
/* A ring that turns, because the thing the reader is about to stare at is a
   ring that turns. */
.spinner {
  width:44px; height:44px; margin:0 auto; border-radius:50%;
  border:3px solid var(--line); border-top-color:var(--accent);
  animation:spin 1s linear infinite;
}
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion:reduce) { .spinner { animation:none; } }

.boxed {
  background:var(--card); border-radius:12px; overflow:hidden;
  box-shadow:0 1px 2px rgba(0,0,0,.06), 0 2px 8px rgba(0,0,0,.04);
}
.row { display:flex; gap:.75rem; align-items:flex-start;
       padding:.8rem .9rem; border-top:1px solid var(--line); }
.boxed > .row:first-child { border-top:none; }
.swatch { width:14px; height:14px; border-radius:50%; flex:0 0 auto; margin-top:.22rem; }
.swatch.spin  { background:var(--accent); }
.swatch.green { background:var(--ok); }
.swatch.red   { background:var(--danger); }
.row .t { font-weight:600; }
.row .d { color:var(--dim); font-size:.87rem; margin-top:.1rem; }
.row .d strong { color:var(--fg); }
h2 { font-size:.78rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
     color:var(--dim); margin:0 0 .45rem .15rem; }
.foot { color:var(--dim); font-size:.85rem; margin-top:1.3rem; text-align:center; }
</style>
<div class="wrap">
  <div class="head">
    <div class="spinner"></div>
    <h1>Connecting to {ssid}&hellip;</h1>
    <p>This setup network is shutting down now, so your phone will drop back to
      its usual Wi-Fi on its own.</p>
  </div>

  <h2>Watch the light ring</h2>
  <div class="boxed">
    <div class="row">
      <div class="swatch spin"></div>
      <div><div class="t">Spinning</div>
        <div class="d">Joining the network.</div></div>
    </div>
    <div class="row">
      <div class="swatch green"></div>
      <div><div class="t">Green sweep</div>
        <div class="d">Connected. Its settings are at
          <strong>{url}</strong>.</div></div>
    </div>
    <div class="row">
      <div class="swatch red"></div>
      <div><div class="t">Red</div>
        <div class="d">It did not work. <strong>{ssid_setup}</strong> comes back in
          about a minute; reconnect to it and this page will say why.</div></div>
    </div>
  </div>

  {extras}
  <p class="foot">{signin}</p>
</div>
"""

# Shown on the done page only when extras were ticked. They install after the
# device is on the network, in the background, which takes minutes - so without
# this a person looking for the assistant straight away finds nothing and no
# hint of where to look.
#
# It is sent before anything has been tried - before the join, before the
# install - so it may only promise what holds whatever happens next. It used to
# say plainly that the apps install in a few minutes. Then a fresh install met
# an offline package feed: the one attempt failed, and nothing said so or tried
# again. Now the choice is kept on the persist partition, the settings page
# says when the apps are still waiting, and it keeps trying (see apply_apps in
# biscuit-setup.sh), so that is what this says.
DONE_EXTRAS = """<p class="foot">Your apps install in the background once it is
    connected. Installing needs the internet and takes a few minutes. If they
    cannot be installed straight away, the settings page says so and keeps
    trying.</p>"""


class Portal(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # The default logs every request to stderr, which the service captures into
    # the setup log and drowns the useful lines; probe requests alone are
    # several per second per phone.
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # Phones cache aggressively on captive portals and will happily show a
        # stale form with a stale error banner after a retry.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, obj, status=200):
        self._send(json.dumps(obj), status=status, ctype="application/json")

    def _form(self):
        ssid = read_file("ssid", "biscuit")
        # Signal and band ride along as data attributes for the picker, which
        # draws them as the settings page does; without script they are unseen.
        opts = "".join(
            '<option value="{v}" data-bars="{b}" data-band="{band}" data-lock="{l}">{v}</option>'.format(
                v=html.escape(n["ssid"]), b=n["bars"], band=html.escape(n["band"]),
                l="1" if n["secure"] else "0")
            for n in networks()
        )
        if not opts:
            opts = '<option value="">(no networks found - use Other)</option>'

        err = read_file("last_error")
        banner = ""
        if err in ERROR_TEXT:
            banner = '<div class="err" role="alert">{}</div>'.format(html.escape(ERROR_TEXT[err]))

        pkgs = optional_packages()
        pkg_html = ""
        if pkgs:
            # After a join that failed, the apps the failed attempt ticked are
            # ticked again: the done page had promised them once connected, and
            # someone retyping a password has no reason to think the boxes need
            # ticking twice. biscuit-setup.sh writes packages.last only between
            # the passes of one session, so a fresh visit starts clear. Same
            # form as "packages": one line per app, its packages spaced.
            again = {line.strip() for line in read_file("packages.last").splitlines()
                     if line.strip()}

            # Same boxed-list markup as the other sections, so an extras group
            # looks like a group rather than the fieldset it used to be.
            def _row(p):
                # The disclosure is omitted entirely when an entry lists no
                # parts, rather than rendered empty: a "What this installs"
                # link that opens on nothing is worse than no link.
                detail = ""
                if p["parts"]:
                    items = "".join("<li>{}</li>".format(html.escape(b))
                                    for b in p["parts"])
                    detail = ('<details><summary>What this installs</summary>'
                              '<ul>{}</ul></details>'.format(items))
                # Named as the settings page's Apps list names it: what it
                # is, then what it is for, then the exact package.
                return (
                    '<div class="pkg"><input type="checkbox" id="pkg_{i}" '
                    'name="pkg" value="{i}"{c}><div><label for="pkg_{i}">{n}</label>'
                    '<p class="note"><strong>{l}.</strong> {d}</p>'
                    '<p class="note mono">{k}</p>{x}</div></div>'.format(
                        i=html.escape(p["id"]),
                        c=" checked" if p["pkgs"] in again else "",
                        n=html.escape(p.get("name") or p["label"]),
                        l=html.escape(p["label"]),
                        d=html.escape(p["desc"]),
                        k=html.escape(p["pkgs"]),
                        x=detail,
                    )
                )

            rows = "".join(_row(p) for p in pkgs)
            pkg_html = ('<div class="group"><h2>Apps</h2>'
                        '<div class="boxed">' + rows + "</div></div>")

        # The note is written server-side for the no-key case and overwritten
        # by the page script if the BROWSER cannot do its half - the two
        # failures are different and a person deserves to be told which.
        if KEY.available:
            secnote = ("Your Wi-Fi and account passwords are encrypted in your "
                       "browser before they are sent, so nobody in range can "
                       "read them off the air.")
        else:
            secnote = ("This device cannot secure the connection, so your "
                       "passwords will be sent unprotected over this setup "
                       "network. They are still only sent to this device.")

        # Rendered rather than always present: a device that already has an
        # account is not asked for another - the server refuses one anyway -
        # and a wizard whose first step is a form nobody should fill in is
        # worse than one that starts at the device name.
        #
        # A device that has one is offered a password reset instead, which
        # needs the person at the Echo - see Presence.
        if needs_account():
            account = ACCOUNT_GROUP
        else:
            account = RESET_GROUP.replace("{owner}", html.escape(owner() or "yours"))

        name = default_name(ssid)
        port = current_port()
        url = "http://%s.local%s" % (name.lower(), "" if port == 80 else ":%d" % port)
        reserved = ",".join(str(p) for p in sorted(system.RESERVED_PORTS)) if system else ""
        return render(
            PAGE,
            account=account, name=html.escape(name), port=port, url=html.escape(url),
            reserved=reserved,
            ssid=html.escape(ssid), options=opts, error=banner, packages=pkg_html,
            pubkey=KEY.public_b64(), secnote=html.escape(secnote),
            regions=region_options(),
            timezones=timezone_options(),
            resetnote=reset_note(),
        )

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # Answer every probe with a redirect rather than what it wants to see.
        # That is the whole trick: the phone concludes it is behind a portal and
        # opens the sheet on its own.
        if path in PROBE_PATHS:
            self._redirect("http://{}/".format(ADDR))
            return

        if path == "/":
            # Opening the page counts as using it.
            window(extend=True)
            self._send(self._form())
            return

        if path == "/alive":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(window(extend=q.get("use") == ["1"]) or {"left": None})
            return

        if path == "/presence":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            st = PRESENCE.state((q.get("id") or [""])[0])
            self._json(st or {"error": "no such check"}, status=200 if st else 404)
            return

        # The boxing library. Served from disk rather than inlined so the 32 KB
        # is fetched once instead of on every re-render of a bounced form.
        if path == "/chime":
            # Deliberately a GET with no body: it is triggered by a button on a
            # page that has not submitted anything yet, and it reveals nothing
            # - the only information it carries is audible in the room.
            #
            # Rate-limited, for everyone at once: this listens on an open
            # network, and anyone in range could otherwise play it on a loop.
            with _chime_lock:
                now = time.monotonic()
                wait = CHIME_GAP - (now - _chime["last"])
                if wait > 0:
                    self._json({"wait": round(wait, 1)}, status=429)
                    return
                _chime["last"] = now
            try:
                subprocess.Popen(["/usr/bin/biscuit-earcon", "setup_mode"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as err:
                log("could not play the confirmation sound: %s" % err)
            self._send("ok", ctype="text/plain; charset=utf-8")
            return

        if path == "/n.js":
            try:
                with open(NACL_JS, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send("", status=404, ctype="text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=600")
            self.end_headers()
            self.wfile.write(body)
            return

        # Anything else - and with DNS hijacked, that is every site the phone
        # tries - lands on the form too.
        self._redirect("http://{}/".format(ADDR))

    def _error(self, kind):
        """Re-serve the form with an error marker the template can read."""
        path = os.path.join(RUNDIR, "last_error")
        try:
            with open(path, "w") as fh:
                fh.write(kind)
            self._send(self._form(), status=400)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/presence/start":
            addr = self.client_address[0] if self.client_address else "?"
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("ascii", "replace") if 0 < n <= 1024 else ""
            proof_hash = (urllib.parse.parse_qs(body).get("hash") or [""])[0]
            try:
                self._json(PRESENCE.start(addr, proof_hash))
            except ValueError:
                self._json({"error": "This browser did not send the check's secret. Reload and try again."}, status=400)
            except LookupError as err:
                self._json({"error": "This Echo's buttons cannot be read, so the "
                                     "password cannot be reset here (%s)." % err}, status=503)
            except PermissionError:
                self._json({"error": "Wait a few seconds, then press Start again."}, status=429)
            return
        if path != "/save":
            self._redirect("http://{}/".format(ADDR))
            return

        length = int(self.headers.get("Content-Length") or 0)
        # Bound the read: this listens on an open network, so anyone in range
        # can post to it.
        if length > 64 * 1024:
            self._send("Too large", status=413, ctype="text/plain; charset=utf-8")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)

        def field(key, default=""):
            return (form.get(key) or [default])[0].strip()

        def secret_field(key, default=""):
            """Like field(), but never strips spaces.

            A WPA2 passphrase is 8-63 printable ASCII characters and space is
            printable, so a passphrase may legitimately begin or end with one.
            Stripping it computes a different PSK from the one the user typed
            and the join then fails exactly like a wrong password - silently,
            identically, every attempt, with the passphrase on screen looking
            correct. Only transport line-endings are removed.
            """
            return (form.get(key) or [default])[0].strip("\r\n")

        pick = field("ssid_pick")
        other = field("ssid_other")
        ssid = other if (pick == "__other__" or not pick) else pick
        name = field("name")
        user = field("user")

        # The two secrets arrive boxed when the browser managed it, and in the
        # clear when it did not - see SetupKey. Both are accepted, because this
        # form is the only way into a new device and refusing a submission that
        # cannot be encrypted would strand someone with no way to set it up.
        #
        # A blob that FAILS to decrypt is a different matter and is refused: it
        # is either a bug or someone poking at the endpoint, and silently
        # falling back to the plaintext fields would turn a broken box into a
        # downgrade anyone could trigger.
        blob = field("enc")
        protected = False
        sealed = {}
        if blob:
            try:
                sealed = KEY.open(blob)
            except ValueError as err:
                log("rejected an encrypted submission: %s" % err)
                self._error("crypto")
                return
            # Line-endings only - see secret_field() for why a passphrase must
            # not be stripped of spaces.
            psk = str(sealed.get("psk", "")).strip("\r\n")
            password = str(sealed.get("pass", "")).strip("\r\n")
            password2 = (str(sealed["pass2"]).strip("\r\n")
                         if sealed.get("pass2") is not None else None)
            protected = True
        else:
            psk = secret_field("psk")
            password = secret_field("pass")
            password2 = secret_field("pass2") if "pass2" in form else None
        # The sound check must be answered: heard it, or chose to go on
        # without it (someone who cannot hear it must not be stranded). A
        # lookalike page could skip the question, so this is not a defence on
        # its own - it makes sure the question is asked, and the setup log
        # can say how it was answered.
        heard = form.get("heard") or []
        if "1" in heard:
            log("device-presence check confirmed by the user")
        elif "skip" in heard:
            log("device-presence check SKIPPED: the user could not hear the sound")
        else:
            self._error("heard")
            return

        log("credentials submitted %s" %
            ("encrypted by the browser" if protected
             else "WITHOUT browser encryption"))

        # WPS replaces only the network half of this form: the account and the
        # device name are still wanted, so it is a choice inside the form
        # rather than a separate flow.
        wps = field("wps") == "1"

        # Region. Validated to two letters here rather than trusted: it is fed
        # to `iw reg set` and written into wpa_supplicant.conf, and this
        # endpoint is reachable by anyone in radio range of an open AP.
        tz = field("tz")
        if tz not in zones():
            tz = ""
        region = field("region").upper()
        if not re.fullmatch(r"[A-Z]{2}", region or ""):
            region = ""

        if not ssid and not wps:
            self._error("ssid")
            return

        # A PSK is 8..63 characters; anything else will be rejected by
        # wpa_passphrase later, and finding that out here saves the user a
        # teardown, a failed join and a reconnect to read the error.
        if psk and not (8 <= len(psk) <= 63):
            with open(os.path.join(RUNDIR, "last_error"), "w") as fh:
                fh.write("auth")
            self._send(self._form(), status=400)
            os.remove(os.path.join(RUNDIR, "last_error"))
            return

        # The account is only collected when the device has none, so a device
        # that has already been set up is not asked again and cannot be talked
        # into creating a second one by a crafted POST.
        # A reset is only for the account that exists, and only once the
        # microphone button was pressed on the device (Presence) - and only
        # encrypted, which needs the page's script. Said first, before any
        # password check could point somewhere else.
        reset = field("reset") == "1" and not needs_account()
        if reset and not protected:
            log("password reset refused: the form was not encrypted")
            self._error("resetseal")
            return
        if needs_account() or reset:
            if needs_account():
                # Deliberately the conservative POSIX set. This becomes a real
                # Unix account, and anything clever here is a shell-quoting bug
                # waiting to happen in the script that creates it.
                if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
                    self._error("user")
                    return
                if user in RESERVED_USERS or system_account(user):
                    log("refused the username %r: reserved or a system account" % user)
                    self._error("user")
                    return
            # chpasswd reads "user:password" lines, so a line break would end
            # the password early.
            if len(password) < 8 or "\n" in password or "\r" in password:
                self._error("pass")
                return
            if password2 is not None and password2 != password:
                self._error("mismatch")
                return

        port = None
        if field("port"):
            try:
                port = check_port(field("port"))
            except ValueError:
                self._error("port")
                return

        # Last, so that no other refusal uses the check up on the server. A
        # bounced form still starts a new page without the page's secret, so
        # the page itself stops what it can before sending (psk length, the
        # reserved ports).
        if reset:
            addr = self.client_address[0] if self.client_address else "?"
            if not PRESENCE.consume(field("presence"), str(sealed.get("proof", "")), addr):
                log("password reset refused: the button presses were not confirmed")
                self._error("reset")
                return

        # Hostnames: letters, digits and hyphens, not starting or ending with
        # one. This goes into /etc/hostname and an avahi config, so it is not a
        # place to pass through whatever was typed.
        name = re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-")[:32].rstrip("-")
        if not name:
            name = read_file("ssid", "biscuit")

        chosen = form.get("pkg") or []
        by_id = {p["id"]: p["pkgs"] for p in optional_packages()}
        pkgs = [by_id[c] for c in chosen if c in by_id]

        os.makedirs(RUNDIR, exist_ok=True)
        with open(os.path.join(RUNDIR, "ssid_choice"), "w") as fh:
            fh.write(ssid)
        # The passphrase is written where only root can read it, and the shell
        # script removes it once the join is done.
        secret_files = []
        if needs_account():
            # Same treatment as the passphrase: root-only, and the shell script
            # deletes it the moment the account exists.
            secret_files = [("account_user", user), ("account_pass", password)]
        elif reset:
            secret_files = [("account_reset", owner()), ("account_pass", password)]
            log("password reset for %s requested, proven at the device" % owner())
        for fname, value in secret_files:
            write_secret(os.path.join(RUNDIR, fname), value)
        port_path = os.path.join(RUNDIR, "settings_port")
        if port is not None:
            with open(port_path, "w") as fh:
                fh.write("%d" % port)
        else:
            try:
                os.remove(port_path)
            except OSError:
                pass

        # The marker biscuit-setup.sh looks for. Written before the psk so a
        # crash between the two cannot leave a device that thinks it has a
        # passphrase to use when the user asked for WPS.
        with open(os.path.join(RUNDIR, "region"), "w") as fh:
            # "00" is the kernel's own worldwide default, so writing nothing is
            # the same thing and keeps join() from calling iw for a no-op.
            fh.write("" if region == "00" else region)

        wps_path = os.path.join(RUNDIR, "wps")
        if wps:
            with open(wps_path, "w") as fh:
                fh.write("1")
            log("WPS push-button requested; no passphrase will be collected")
        else:
            try:
                os.remove(wps_path)
            except OSError:
                pass

        # Emptied when WPS was chosen, rather than merely ignored later. The
        # log line above promises the passphrase is not collected, and a psk
        # sitting on disk that nothing reads still contradicts that - it is a
        # secret this device said it would not keep. biscuit-setup.sh already
        # ignores it in the WPS branch, so this costs nothing but honesty.
        #
        # Created 0600, like the account files above, not chmodded afterwards.
        # It was opened with the default mode, written, and only then
        # restricted: for that moment the passphrase sat in a world-readable
        # file, and a descriptor opened in it stays readable after a chmod.
        # biscuit-setup.sh now deletes it as soon as it is read, so it is
        # created afresh for every submission and that moment came every time.
        write_secret(os.path.join(RUNDIR, "psk"), "" if wps else psk)
        with open(os.path.join(RUNDIR, "device_name"), "w") as fh:
            fh.write(name)
        with open(os.path.join(RUNDIR, "timezone"), "w") as fh:
            fh.write(tz)
        with open(os.path.join(RUNDIR, "packages"), "w") as fh:
            fh.write("\n".join(pkgs))
        try:
            os.remove(os.path.join(RUNDIR, "last_error"))
        except OSError:
            pass

        url_port = port if port is not None else current_port()
        if needs_account():
            signin = ("Sign in to the settings page with the username and "
                      "password you just chose.")
        elif reset:
            signin = ("Sign in to the settings page as %s, with the new password."
                      % html.escape(owner()))
        else:
            signin = "Sign in to the settings page with your account, as before."
        body = render(DONE,
            ssid=html.escape(ssid),
            name=html.escape(name),
            url=html.escape("http://%s.local%s" % (name.lower(),
                            "" if url_port == 80 else ":%d" % url_port)),
            signin=signin,
            ssid_setup=html.escape(read_file("ssid", "the setup network")),
            extras=DONE_EXTRAS if pkgs else "",
        )
        self._send(body)

        # Written last, and only after the page is on the wire. It is the
        # trigger the shell script is waiting on, and it tears down the radio
        # this response is travelling over.
        try:
            self.wfile.flush()
        except OSError:
            pass
        with open(os.path.join(RUNDIR, "submitted"), "w") as fh:
            fh.write("1")


class Server(socketserver.ThreadingTCPServer):
    # Phones open several connections at once (the probe, the sheet, favicon),
    # and a single-threaded server makes the sheet look hung.
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.makedirs(RUNDIR, exist_ok=True)
    write_decoded_networks()
    Server((ADDR, PORT), Portal).serve_forever()
