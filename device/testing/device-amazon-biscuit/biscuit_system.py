"""Apps, updates and Home Assistant status, for the settings page.

Everything here reads state; the two things that change the device - installing
or updating packages - are handed to biscuit-pkgjob.sh, detached, and only its
status file is read back. The one list kept here is of the apps chosen at setup
that are not installed yet (see "Apps chosen at setup"). The settings page
imports this as `system`.

WHY THE APK DATABASE IS READ DIRECTLY. `apk info` is a subprocess per question
and each costs a few hundred milliseconds on this CPU; the installed database is
a text file with every name, version and installed size in it, and the home
page asks about half a dozen packages on every load.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

APPS_FILE = "/usr/share/biscuit/apps.json"
APK_DB = "/lib/apk/db/installed"
APK_WORLD = "/etc/apk/world"
PKG_JOB = "/usr/bin/biscuit-pkgjob.sh"
PKG_RUNDIR = "/run/biscuit-packages"
UPDATES_FILE = os.path.join(PKG_RUNDIR, "updates")
REBOOT_FLAG = os.path.join(PKG_RUNDIR, "reboot-required")
CORE = "device-amazon-biscuit"
# Our own packages: the device package and its subpackages, and the kernel.
# They come from this device's feed and update together; everything else is
# postmarketOS's.
KERNEL = "linux-amazon-biscuit"
BOOT_STATUS = os.path.join(PKG_RUNDIR, "boot-status.json")
BOOT_TOOL = "/usr/bin/biscuit-boot-update"
BOOT_RECORD = "/var/lib/biscuit/boot-installed.json"
BOOT_PREVIOUS = "/var/lib/biscuit/boot-previous.img"
AUTO_CHECK_FILE = "/opt/persist/update-check"

VOICE_PORT = 6053          # linux-voice-assistant's ESPHome API
BTPROXY_PORT = 6054        # biscuit-btproxy's ESPHome API
SENDSPIN_SERVER_PORT = 8927  # Music Assistant's Sendspin server


def _run(argv, timeout=60):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as err:
        return 1, "", str(err)


# ---------------------------------------------------------------------------
# The manifest and the installed database
# ---------------------------------------------------------------------------

_manifest_cache = (None, None)


def manifest():
    global _manifest_cache
    try:
        mtime = os.stat(APPS_FILE).st_mtime
    except OSError:
        return {"apps": [], "core": {}, "source": {}}
    if _manifest_cache[0] != mtime:
        with open(APPS_FILE, encoding="utf-8") as f:
            _manifest_cache = (mtime, json.load(f))
    return _manifest_cache[1]


_installed_cache = (None, None)


def installed():
    """{name: {"version": v, "size": bytes}} for every installed package.

    Cached until the database changes: the home page asks for this three
    times per load, and parsing the file is a tenth of a second each time.
    Callers must not modify what it returns.
    """
    global _installed_cache
    try:
        mtime = os.stat(APK_DB).st_mtime
    except OSError:
        return {}
    if _installed_cache[0] == mtime:
        return _installed_cache[1]
    out, cur = {}, {}
    try:
        with open(APK_DB, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    if cur.get("P"):
                        out[cur["P"]] = {"version": cur.get("V", ""),
                                         "size": int(cur.get("I") or 0)}
                    cur = {}
                elif len(line) > 2 and line[1] == ":" and line[0] in "PVI":
                    cur[line[0]] = line[2:]
        if cur.get("P"):
            out[cur["P"]] = {"version": cur.get("V", ""), "size": int(cur.get("I") or 0)}
    except OSError:
        return out
    _installed_cache = (mtime, out)
    return out


def is_ours(name):
    return name.startswith(CORE) or name == KERNEL


def ours(db=None):
    """Our installed packages, {name: version}: core, every subpackage, and
    the kernel."""
    db = db if db is not None else installed()
    return {n: p["version"] for n, p in db.items() if is_ours(n)}


def release_tag(version):
    """6-r287 -> r287, which is how the repository tags a build. A public
    release is tagged twice on one commit - v1.0 and r295 - so a build's
    source is always reachable by its r<N> tag, whatever it was released as."""
    m = re.search(r"-r(\d+)$", version or "")
    return ("r" + m.group(1)) if m else ""


# The public release this build is, written by the core package from the
# APKBUILD's _release: release=v1.0 and build=6-r295. Builds before v1.0 have
# no file, and are named by their build number alone.
RELEASE_FILE = "/usr/share/biscuit/release"


def release():
    """{"name": "v1.0", "build": "6-r295"} for the installed core package, or
    empty strings when it names no release. Read on every call: an update
    replaces the file under a running settings server."""
    fields = {}
    for line in _read_text(RELEASE_FILE).splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    name, build = fields.get("release", ""), fields.get("build", "")
    if not (re.fullmatch(r"v[0-9]+\.[0-9]+(\.[0-9]+)?", name) and re.fullmatch(r"[0-9][0-9.]*-r[0-9]+", build)):
        return {"name": "", "build": ""}
    return {"name": name, "build": build}


def release_name(version):
    """v1.0 for the build that release is, "" for any other. Only the build
    this Echo runs carries a name: an update's is not known until it is
    installed."""
    r = release()
    return r["name"] if version and version == r["build"] else ""


def source_links(version):
    src = manifest().get("source", {})
    repo, path = src.get("repo", ""), src.get("path", "")
    tag = release_tag(version)
    name = release_name(version)
    ref = tag or "main"
    return {
        "repo": repo,
        "tree": "%s/tree/%s/%s" % (repo, ref, path) if repo else "",
        "blob": "%s/blob/%s/%s/" % (repo, ref, path) if repo else "",
        # The release page is named after the public release. A build with no
        # known name - an update not yet installed, or one from before v1.0 -
        # has no page of its own, so it gets the list of releases instead.
        "release": ("%s/releases/tag/%s" % (repo, name) if name else "%s/releases" % repo)
                   if repo and tag else "",
        "issues": "%s/issues" % repo if repo else "",
        "tag": tag,
        "name": name,
    }


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

def apps(db=None):
    """The manifest's apps with what is installed. Fast: no subprocesses."""
    db = db if db is not None else installed()
    core_v = db.get(CORE, {}).get("version", "")
    links = source_links(core_v)
    out = []
    for app in manifest().get("apps", []):
        pkgs = app.get("packages", [])
        have = [p for p in pkgs if p in db]
        entry = dict(app)
        entry["installed"] = bool(pkgs) and len(have) == len(pkgs)
        entry["version"] = db[pkgs[0]]["version"] if have else ""
        entry["changes"] = [dict(c, url=links["blob"] + c["file"] if links["blob"] else "")
                            for c in app.get("changes", [])]
        out.append(entry)
    core = dict(manifest().get("core", {}))
    core["version"] = core_v
    core["links"] = links
    return {"apps": out, "core": core}


def _app(app_id):
    return next((a for a in manifest().get("apps", []) if a["id"] == app_id), None)


def _total_mib(db):
    return sum(p["size"] for p in db.values()) / 1048576.0


def _simulate(action, pkgs):
    """apk's own answer to "what would this touch": [(name, version)], MiB after."""
    rc, out, err = _run(["apk", action, "--simulate", "--no-progress"] + pkgs, timeout=90)
    verb = "Purging" if action == "del" else "Installing"
    items = re.findall(r"%s (\S+) \(([^)]+)\)" % verb, out)
    m = re.search(r"OK: ([\d.]+) MiB", out)
    return (rc == 0), items, (float(m.group(1)) if m else None), (err or out).strip()[-300:]


def _python_packages(venv):
    """Every package bundled in an app's venv, from its own dist-info."""
    found = []
    lib = os.path.join(venv, "lib")
    try:
        pys = [d for d in os.listdir(lib) if d.startswith("python")]
    except OSError:
        return found
    for py in pys:
        site = os.path.join(lib, py, "site-packages")
        try:
            names = os.listdir(site)
        except OSError:
            continue
        for d in names:
            if not d.endswith(".dist-info"):
                continue
            meta = {}
            try:
                with open(os.path.join(site, d, "METADATA"), encoding="utf-8",
                          errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            break
                        k, _, v = line.partition(":")
                        if k in ("Name", "Version", "License", "License-Expression") and k not in meta:
                            meta[k] = v.strip()
            except OSError:
                pass
            lic = meta.get("License-Expression") or meta.get("License") or ""
            if len(lic) > 40:     # some packages paste the whole licence text
                lic = lic.split("\n")[0][:40] + "…"
            found.append({"name": meta.get("Name") or d.rsplit("-", 1)[0],
                          "version": meta.get("Version", ""), "license": lic})
    return sorted(found, key=lambda p: p["name"].lower())


def app_detail(app_id):
    """What an app puts on this device, as apk and the files themselves say.

    Slow (seconds): apk is asked what installing or removing it would change,
    which is the exact footprint rather than a figure someone typed.
    """
    app = _app(app_id)
    if app is None:
        raise ValueError("no such app")
    db = installed()
    pkgs = app.get("packages", [])
    is_installed = all(p in db for p in pkgs)
    before = _total_mib(db)
    if is_installed:
        ok, items, after, why = _simulate("del", pkgs)
        packages = [{"name": n, "version": v, "size": db.get(n, {}).get("size", 0)}
                    for n, v in items]
        size = sum(p["size"] for p in packages)
    else:
        ok, items, after, why = _simulate("add", pkgs)
        packages = [{"name": n, "version": v, "size": None} for n, v in items]
        size = int((after - before) * 1048576) if ok and after is not None else None
    lock = []
    if app.get("lockfile") and os.path.exists(app["lockfile"]):
        with open(app["lockfile"], encoding="utf-8", errors="replace") as f:
            lock = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    return {
        "id": app_id,
        "installed": is_installed,
        "footprint_ok": ok,
        "footprint_error": "" if ok else why,
        "packages": packages,
        "size": size,
        "python": _python_packages(app["venv"]) if is_installed and app.get("venv") else [],
        "lockfile": lock,
    }


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

def _kv(path):
    out = {"upd": [], "sys": []}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("=")
                if k in ("upd", "sys"):
                    out[k].append(v.split())
                elif k:
                    out[k] = v
    except OSError:
        return None
    return out


def updates():
    """The last check, what is installed, and whether a restart is owed."""
    db = installed()
    mine = ours(db)
    core_v = mine.get(CORE, "")
    last = _kv(UPDATES_FILE)
    avail = {}
    if last:
        for parts in last["upd"]:
            if len(parts) == 3 and parts[0] in mine:
                avail[parts[0]] = parts[2]
    # The release to name is the device package's. The kernel has versions
    # of its own and is reported separately.
    device = {n: v for n, v in avail.items() if n != KERNEL}
    target = device.get(CORE) or (max(device.values()) if device else "")
    return {
        "installed": mine,
        "version": core_v,
        "checked_at": int(last["checked"]) if last and last.get("checked", "").isdigit() else None,
        "check_ok": bool(last and last.get("ok") == "1"),
        # Why the last check could not be made (the package job's word:
        # clock, offline, feed, local, unknown), or "partial" for one made
        # while a mirror did not answer; "" for a clean one, or a record
        # written before r295.
        "check_why": (last or {}).get("why", ""),
        "available": avail,
        "target": target,
        "target_links": source_links(target) if target else None,
        "links": source_links(core_v),
        "other_updates": int(last.get("other", "0") or 0) if last else None,
        # Newer postmarketOS packages the last check found, installed only when
        # the owner asks: [name, installed, available].
        "system": [parts for parts in (last or {}).get("sys", [])
                   if len(parts) == 3 and parts[0] in db],
        "python": _read_text("/etc/apk/world").count("python3~") > 0,
        "reboot_required": os.path.exists(REBOOT_FLAG),
        "kernel": db.get(KERNEL, {}).get("version", ""),
        "kernel_available": avail.get(KERNEL, ""),
        "running_kernel": os.uname().release,
        "boot": boot_status(),
        "boot_damaged": boot_damaged(),
        "auto_check": auto_check_enabled(),
    }


def boot_status():
    """Whether boot_a holds what the installed kernel and initramfs build, as
    biscuit-boot-update last reported it, or None before any check.

    Written by the package job after every check and update rather than
    computed here: building the image to compare reads and inflates twenty
    megabytes, which is seconds on this CPU.
    """
    try:
        with open(BOOT_STATUS, encoding="utf-8") as f:
            st = json.load(f)
        st["checked_at"] = int(os.path.getmtime(BOOT_STATUS))
    except (OSError, ValueError):
        st = None
    # What can be put back is read from the tool's own files, not only from
    # the status above: /run is empty after the restart that follows an
    # install - exactly when a misbehaving kernel needs Put back - and the
    # next status waits for a check.
    try:
        with open(BOOT_RECORD, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = None
    can = os.path.exists(BOOT_PREVIOUS)
    # Whether the image to put back has another kernel than the installed
    # one, as the last install recorded it - so the warning is there right
    # after the restart that follows an install, before any check.
    recorded = None
    if last and last.get("kernel") and last.get("previous_kernel"):
        recorded = last["kernel"] != last["previous_kernel"]
    if st is None:
        return {"ok": None, "last_apply": last, "damaged": boot_damaged(),
                "rollback": {"available": can, "kernel_differs": recorded}}             if (last or can) else None
    st["last_apply"] = last
    st["damaged"] = boot_damaged()
    st.setdefault("rollback", {"available": can, "kernel_differs": None})
    st["rollback"]["available"] = can
    if st["rollback"].get("kernel_differs") is None:
        st["rollback"]["kernel_differs"] = recorded
    return st


def boot_damaged():
    """A boot partition may hold a partial image (a failed install that could
    not put it back): restarting would start from it. The tool's record, or
    the package job's marker in /run when the record could not be written."""
    if os.path.exists(os.path.join(PKG_RUNDIR, "boot-damaged")):
        return True
    # Every slot checked good since the record was last written, which the
    # record itself could not say when its write failed (exit 5).
    try:
        if os.path.getmtime(os.path.join(PKG_RUNDIR, "boot-verified")) >= os.path.getmtime(BOOT_RECORD):
            return False
    except OSError:
        pass
    try:
        with open(BOOT_RECORD, encoding="utf-8") as f:
            return bool(json.load(f).get("damaged"))
    except (OSError, ValueError, AttributeError):
        return False


def auto_check_enabled():
    """The daily update check: on unless the owner turned it off. It only
    looks; nothing is installed without being asked."""
    return _read_text(AUTO_CHECK_FILE).strip() != "off"


def set_auto_check(on):
    tmp = AUTO_CHECK_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("daily\n" if on else "off\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, AUTO_CHECK_FILE)
    return auto_check_enabled()


JOB_ACTIONS = ("check", "update", "sysupdate", "boot", "bootrollback")


def start_job(action, packages=None):
    """check | update | sysupdate | boot | bootrollback, detached. The caller
    reads the status file afterwards. sysupdate takes system packages, or none
    for all of them; boot writes the boot image the installed kernel builds."""
    if action not in JOB_ACTIONS:
        raise ValueError("unknown action")
    args = list(packages or []) if action == "sysupdate" else [CORE]
    subprocess.Popen(["setsid", PKG_JOB, action] + args,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---------------------------------------------------------------------------
# Apps chosen at setup
# ---------------------------------------------------------------------------
#
# The setup portal offers the apps, and setup hands the ones ticked to one
# background install seconds after the Echo joins Wi-Fi. When that install
# could not be done - the feed was down, the clock not yet set - the choice
# used to live only in /run: nothing tried again, a restart forgot it, and the
# Apps page then showed the apps exactly as if nobody had ever asked for them.
#
# So setup also writes the choice to the persist partition, APPS_PENDING, one
# package name per line, and this side keeps it until it is done:
#
#   - a package leaves the list once it is installed, whoever installed it,
#     and the file goes when the list is empty;
#   - the settings server tries again by itself (retry_step, below), never
#     while the clock is unset, and never alongside another package job;
#   - the owner can try again at once, or cancel, from the Apps page.
#
# Installing what the owner ticked is not "installing without being asked":
# being asked is exactly what the tick was. Nothing outside the list is ever
# installed this way - each name is checked against the apps the manifest
# offers, because the file is on a partition an old release, a reflash or a
# person with a shell may have written.
#
# No file means nothing is pending, which is also what every device set up
# before r295 has.

APPS_PENDING = "/opt/persist/apps-pending"
APPS_RETRY = os.path.join(PKG_RUNDIR, "apps-retry")
PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
PENDING_MAX_BYTES = 4096

# Every change to APPS_PENDING in this process is a read, then a rewrite, and
# they run on different threads: the retry's prune every minute, the owner's
# "Don't install" on a request. Unordered, a prune that read the list before a
# cancel removed it renamed the list back into place - and the app the owner
# had just declined was installed a minute later. So each holds this lock for
# the whole of its read and write. (Setup, the other writer, runs while the
# settings server is stopped; the factory reset stops it first.)
_PENDING_LOCK = threading.Lock()

# When to try again. Not in the first minutes after boot - start-up has the CPU
# then, as the daily check also leaves it - and after a failure at growing
# intervals, from a few minutes to an hour: a feed that is down for the day
# should cost an hourly `apk update`, not one a minute. Uptime, not the clock,
# so a clock that steps from 2010 to today changes nothing, and so a restart
# starts afresh. A failure because the clock was unset is not counted: nothing
# was tried, and the next try comes as soon as the clock is set. Nor is one
# because another apk had the package database ("busy"): that says nothing
# about the feed, and the next try is a minute later.
RETRY_AFTER_BOOT = 600
RETRY_BACKOFF = (300, 600, 1200, 2400, 3600)
RETRY_MIN_GAP = 60


def app_packages():
    """Every package the manifest's apps install - the only names the pending
    list may hold."""
    return {p for a in manifest().get("apps", []) for p in a.get("packages") or []}


def app_labels(packages):
    """[{"id", "label"}] for the apps these packages belong to, in the
    manifest's order; a name no app has is shown as itself."""
    pkgs = list(packages or [])
    out, covered = [], set()
    for a in manifest().get("apps", []):
        mine = [p for p in a.get("packages") or [] if p in pkgs]
        if mine:
            out.append({"id": a.get("id", ""), "label": a.get("label") or a.get("id", "")})
            covered.update(mine)
    out.extend({"id": "", "label": p} for p in pkgs if p not in covered)
    return out


def read_pending():
    """The names in APPS_PENDING that are apps' packages, in order, once each.

    Anything else is ignored: a line that is not a package name, and a name no
    app in this release's manifest installs."""
    try:
        with open(APPS_PENDING, "rb") as f:
            raw = f.read(PENDING_MAX_BYTES)
    except OSError:
        return []
    known = app_packages()
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        name = line.strip()
        if name and PKG_NAME.match(name) and name in known and name not in out:
            out.append(name)
    return out


def pending_apps(db=None):
    """The packages chosen at setup that are not installed yet."""
    db = db if db is not None else installed()
    return [p for p in read_pending() if p not in db]


def _sync_dir(path):
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def prune_pending(db=None):
    """Take installed packages, and anything invalid, out of the list, and
    remove the file once it is empty. Returns what is still pending.

    Written only when something changes, and as setup writes it: a new file
    renamed into place, then the directory synced - an Echo is unplugged,
    not shut down. Nothing is taken out while the manifest cannot be read,
    because every name would then look invalid. A store that cannot be
    written is left as it is: what is pending is worked out afresh on every
    read, so the list is only ever longer than it needs to be, never wrong.
    """
    # The database outside the lock: parsing it is the slow part, and one a
    # moment stale can only leave the list longer, never bring a name back.
    db = db if db is not None else installed()
    with _PENDING_LOCK:
        left = pending_apps(db)
        if not app_packages():
            return left
        try:
            with open(APPS_PENDING, "rb") as f:
                current = f.read(PENDING_MAX_BYTES + 1)
        except OSError:
            return left
        want = "".join(p + "\n" for p in left).encode()
        if current == want:
            return left
        _write_pending(left)
        return left


def _write_pending(names):
    """The list as setup writes it - a new file renamed into place, then the
    directory synced - or no file at all when it is empty. With _PENDING_LOCK
    held. False when the store could not be written."""
    try:
        if not names:
            try:
                os.unlink(APPS_PENDING)
            except FileNotFoundError:
                pass
        else:
            tmp = APPS_PENDING + ".tmp"
            with open(tmp, "wb") as f:
                f.write("".join(p + "\n" for p in names).encode())
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, APPS_PENDING)
    except OSError:
        return False
    _sync_dir(APPS_PENDING)
    return True


def drop_pending(packages):
    """Take these packages out of the list, whether installed or not - the
    owner no longer wants them. Returns what is still pending."""
    drop = set(packages or [])
    with _PENDING_LOCK:
        names = read_pending()
        keep = [p for p in names if p not in drop]
        if keep != names:
            _write_pending(keep)
    return pending_apps()


def drop_removed(pending, record, st):
    """A removal of something on the list that finished since the retry last
    looked withdraws the choice made at setup for what it removed: the owner
    took it off this Echo. Returns what is still pending.

    Only as a backstop - the settings server prunes the list before it starts
    any job, removals included - for a removal started elsewhere, before any
    prune saw the app installed. Only a record the retry has not seen, and
    records live in /run, so neither an old removal nor one from before a
    restart can take off a choice made since. A bare `apk del` over SSH
    writes no record at all, and is not covered."""
    if (pending and record and record.get("action") == "del" and record.get("finished")
            and record.get("state") == "ok" and record.get("id")
            and record["id"] != (st or {}).get("seen")
            and set(record.get("packages") or []) & set(pending)):
        return drop_pending(record["packages"])
    return pending


def cancel_pending():
    """The owner does not want them after all: nothing more is tried."""
    with _PENDING_LOCK:
        try:
            os.unlink(APPS_PENDING)
        except FileNotFoundError:
            pass
        _sync_dir(APPS_PENDING)
        try:
            os.unlink(APPS_RETRY)
        except FileNotFoundError:
            pass


def uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def clock_floor():
    """The earliest the clock can really be: the package job's own file, which
    carries the package's build time - the same test the job makes."""
    try:
        return os.stat(PKG_JOB).st_mtime
    except OSError:
        return 0.0


def clock_set():
    return time.time() >= clock_floor()


def plausible_time(epoch):
    """A time a job recorded, or None when the clock was unset as it wrote it
    - "16 years ago" helps nobody."""
    return epoch if epoch and epoch >= clock_floor() else None


def retry_load():
    """The retry's own record for this boot: {} before the first one."""
    out = {}
    try:
        with open(APPS_RETRY, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in ("failures", "next", "seen", "started"):
                    out[k] = v
    except OSError:
        return {}
    try:
        return {"failures": int(out.get("failures") or 0),
                "next": float(out.get("next") or RETRY_AFTER_BOOT),
                "seen": out.get("seen", ""),
                "started": float(out["started"]) if out.get("started") else None}
    except ValueError:
        return {}


def retry_save(st):
    """In /run, so it survives the settings server restarting and not the
    device doing so."""
    if not st:
        try:
            os.unlink(APPS_RETRY)
        except FileNotFoundError:
            pass
        return
    os.makedirs(PKG_RUNDIR, exist_ok=True)
    tmp = APPS_RETRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("failures=%d\nnext=%.0f\nseen=%s\nstarted=%s\n" % (
            st.get("failures", 0), st.get("next", RETRY_AFTER_BOOT), st.get("seen", ""),
            "" if st.get("started") is None else "%.0f" % st["started"]))
    os.replace(tmp, APPS_RETRY)


def _backoff(failures):
    return RETRY_BACKOFF[min(max(failures, 1), len(RETRY_BACKOFF)) - 1]


def _named(packages):
    """{packages, ids, labels} for these pending packages, as the pages
    name them."""
    apps = app_labels(packages)
    return {"packages": list(packages), "ids": [a["id"] for a in apps if a["id"]],
            "labels": [a["label"] for a in apps]}


def _concerns(record, pending, action="add"):
    return bool(record and record.get("action") == action and
                set(record.get("packages") or []) & set(pending))


def retry_observe(pending, record, now, st):
    """Fold the last app job's outcome into the retry's record, once.

    A failed install of anything still pending pushes the next try back by
    the next step of RETRY_BACKOFF - whoever started it, since an owner's
    Install that failed says as much about the feed as a retry that did. One
    that failed because the clock was unset, or because another apk had the
    package database, does not count: nothing was asked of the feed. The next
    try is then a minute on (and still waits for the clock). A successful one
    starts the count again.
    """
    st = dict(st or {})
    st.setdefault("failures", 0)
    st.setdefault("next", RETRY_AFTER_BOOT)
    st.setdefault("seen", "")
    st.setdefault("started", None)
    if record and record.get("finished") and record.get("id") and record["id"] != st["seen"]:
        st["seen"] = record["id"]
        if _concerns(record, pending) and record.get("state") == "failed":
            if record.get("reason") in ("clock", "busy"):
                st["next"] = now + RETRY_MIN_GAP
            else:
                st["failures"] += 1
                st["next"] = now + _backoff(st["failures"])
        elif record.get("action") == "add" and record.get("state") == "ok":
            # An install just worked, so the feed is answering: whatever is
            # still pending is worth trying soon rather than in an hour.
            st["failures"] = 0
            st["next"] = now + RETRY_MIN_GAP
    return st


def retry_step(pending, record, busy, now, clock_ok, st):
    """One tick of the automatic retry: (record to keep, whether to start).

    Pure, so it can be tested without a device: `now` is uptime in seconds,
    `busy` whether any package or service job is running, `clock_ok` whether
    the clock has been set. The caller starts the install, and then records
    that it did with retry_started.
    """
    if not pending:
        return {}, False
    st = retry_observe(pending, record, now, st)
    return st, bool(not busy and clock_ok and now >= st["next"])


def retry_started(st, now):
    """Record a start. Until its outcome is seen, the next try is one step
    further on - so a job that ends without a word cannot cause a loop."""
    st = dict(st)
    st["started"] = now
    st["next"] = now + _backoff(st.get("failures", 0) + 1)
    return st


def pending_view(pending, record, running, st, now, clock_ok):
    """The pending apps as the pages show them, or None when there are none.

    state       installing  an install of all of them is running now
                busy        another package job is running
                failed      the last attempt failed, and `message` says why
                pending     no failure worth showing: nothing tried since the
                            Echo started, only a clock that is set now, or
                            only another apk holding the database (`reason`
                            "busy", and `message` says so)
    waiting     "clock" or "job" while the next try also waits for that, else
                None
    retry_in    seconds until the next try is due by the schedule - always,
                whatever else it waits for
    installing  {packages, ids, labels, message} of the ones an install
                running now covers, or None
    rest        {packages, ids, labels} of the others, which `state` and the
                fields above describe
    failed      {packages, ids, labels} of those the failure was about, when
                it was about only some of them (an owner's Install of one
                app); else None

    An install can cover only part of the list: the owner can install one of
    the apps from the Apps page, and a setup re-entered for one app starts a
    job for that app alone. Told as one, the whole list read "Installing now"
    for an app nobody was installing.

    The two are given together because retry_step needs both: a try is due
    when its time has come AND the clock is set AND no job runs. Given alone,
    "waits for the clock" read as "as soon as the clock is set" when the
    schedule was still nine minutes off (the first try after a boot), and
    "waits for the job" as "once it has finished" when a backoff was twenty.
    """
    if not pending:
        return None
    st = retry_observe(pending, record, now, st)
    view = dict(_named(pending), state="pending", message="", reason="", tried_at=None,
                failures=st["failures"], waiting=None, retry_in=None, installing=None, failed=None)
    covered = set(running.get("packages") or []) if running and running.get("action") == "add" else set()
    inst = [p for p in pending if p in covered]
    rest = [p for p in pending if p not in covered]
    view["rest"] = _named(rest)
    if inst:
        view["installing"] = dict(_named(inst), message=running.get("message", ""))
    if not rest:
        view["state"] = "installing"
        view["message"] = running.get("message", "")
        return view
    # From here on, the ones no install covers. A failure because the clock
    # was unset says nothing once it is set, and would read as if it still
    # were - the next try is then only a minute off. One because another apk
    # had the package database is no failure of the install: the next try is
    # a minute off, so it is said, not badged.
    if _concerns(record, rest) and record.get("finished") and record.get("state") == "failed":
        if record.get("reason") == "busy":
            view.update(message=record.get("message", ""), reason="busy")
        elif not (record.get("reason") == "clock" and clock_ok):
            view.update(state="failed", message=record.get("message", ""),
                        reason=record.get("reason", ""),
                        tried_at=plausible_time(record.get("time")))
            # The message is about what that install was of; the others
            # were not tried, and are not reported as failed.
            tried = [p for p in rest if p in (record.get("packages") or [])]
            if len(tried) < len(rest):
                view["failed"] = _named(tried)
    if running:
        view["state"] = view["state"] if view["state"] == "failed" else "busy"
        view["waiting"] = "job"
    elif not clock_ok:
        view["waiting"] = "clock"
    view["retry_in"] = max(0, int(st["next"] - now))
    return view


def start_app_install(packages, origin=""):
    """Install apps' packages, detached, as the Install button does - for the
    list chosen at setup. Only names the manifest's apps install."""
    known = app_packages()
    pkgs = [p for p in packages or [] if isinstance(p, str) and PKG_NAME.match(p) and p in known]
    if not pkgs:
        raise ValueError("nothing to install")
    env = {k: v for k, v in os.environ.items() if k != "BISCUIT_PKGJOB_ORIGIN"}
    if origin:
        env["BISCUIT_PKGJOB_ORIGIN"] = origin
    subprocess.Popen(["setsid", PKG_JOB, "add"] + pkgs,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    return pkgs


# ---------------------------------------------------------------------------
# Home Assistant and Music Assistant connections
# ---------------------------------------------------------------------------

def _connections():
    """Established TCP connections: [(local_port, remote_ip, remote_port)]."""
    conns = []
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "01":     # ESTABLISHED
                        continue
                    (lip, lport), (rip, rport) = (p.split(":") for p in parts[1:3])
                    conns.append((int(lport, 16), _ip(rip, v6), int(rport, 16)))
        except (OSError, ValueError, StopIteration):
            continue
    return conns


def _ip(hexaddr, v6):
    raw = bytes.fromhex(hexaddr)
    if not v6:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # /proc stores each 32-bit word little-endian.
    words = struct.unpack("<4I", raw)
    addr = struct.pack(">4I", *words)
    if addr[:12] == b"\0" * 10 + b"\xff\xff":
        return socket.inet_ntop(socket.AF_INET, addr[12:])
    return socket.inet_ntop(socket.AF_INET6, addr)


def connections():
    """Who is connected: Home Assistant to the assistant and the proxy, and
    this device to Music Assistant."""
    c = _connections()
    uniq = lambda xs: sorted(set(xs))
    return {
        "voice_peers": uniq(ip for lp, ip, rp in c if lp == VOICE_PORT),
        "btproxy_peers": uniq(ip for lp, ip, rp in c if lp == BTPROXY_PORT),
        "music_servers": uniq("%s:%d" % (ip, rp) for lp, ip, rp in c if rp == SENDSPIN_SERVER_PORT),
        "voice_port": VOICE_PORT,
    }


# ---------------------------------------------------------------------------
# The inventory: packages, services, processes and what each function uses
#
# Everything is read from the device. The one hand-written part is which
# services make up each function (apps.json `functions`); what those services
# need comes from OpenRC's own dependency tree, which processes they run from
# the process tree and OpenRC's daemon records - this kernel has no cgroups -
# and which package owns each program from apk's installed database.
# ---------------------------------------------------------------------------

OPENRC = "/run/openrc"
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
NCPU = os.cpu_count() or 1
KERNEL_PKG = "linux-amazon-biscuit"
# Processes that belong to a service without being what it is: supervisors,
# and the busybox applets the log rotation runs as. Counted in its memory,
# never named as one of the packages behind it.
HELPERS = {"supervise-daemon", "awk", "cat", "sleep", "logger", "tee"}
# Services with no control at all: stopping one takes too much with it.
NO_CONTROL = {"dbus", "udev", "local", "udev-postmount", "swapfile",
              "postmarketos-zram-swap", "biscuit-persist", "biscuit-firstboot",
              "biscuit-growroot", "logbookd", "unudhcpd.usb0"}
# Restart only: stopping either locks the owner out of this page or SSH.
RESTART_ONLY = {"biscuit-settings", "sshd"}
DESC_RE = re.compile(r"^\s*description=[\"']?(.*?)[\"']?\s*$", re.M)

_db_cache = (None, None)
_cpu_prev = {}
_inv_cache = (0.0, None)


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def apk_db():
    """Parsed /lib/apk/db/installed, cached until it changes.

    {"pkgs": {name: info}, "owner": {path: name}, "provides": {name: pkg}}.
    """
    global _db_cache
    try:
        mtime = os.stat(APK_DB).st_mtime
    except OSError:
        return {"pkgs": {}, "owner": {}, "provides": {}}
    if _db_cache[0] == mtime:
        return _db_cache[1]
    pkgs, owner, provides = {}, {}, {}
    cur, folder = None, ""
    with open(APK_DB, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                cur = None
                continue
            if len(line) < 2 or line[1] != ":":
                continue
            k, v = line[0], line[2:]
            if k == "P":
                cur = {"name": v, "version": "", "size": 0, "desc": "", "url": "",
                       "license": "", "depends": [], "origin": ""}
                pkgs[v] = cur
                provides.setdefault(v, v)
            elif cur is None:
                continue
            elif k == "V":
                cur["version"] = v
            elif k == "I":
                cur["size"] = int(v or 0)
            elif k == "T":
                cur["desc"] = v
            elif k == "U":
                cur["url"] = v
            elif k == "L":
                cur["license"] = v
            elif k == "o":
                cur["origin"] = v
            elif k == "D":
                cur["depends"] = v.split()
            elif k == "p":
                for item in v.split():
                    provides.setdefault(re.split(r"[=<>~]", item, 1)[0], cur["name"])
            elif k == "F":
                folder = "/" + v
            elif k == "R":
                owner[folder + "/" + v] = cur["name"]
    for info in pkgs.values():
        deps = []
        for d in info["depends"]:
            if d.startswith("!"):
                continue
            name = re.split(r"[=<>~]", d, 1)[0]
            pkg = provides.get(name)
            if pkg and pkg != info["name"] and pkg not in deps:
                deps.append(pkg)
        info["deps"] = deps
    db = {"pkgs": pkgs, "owner": owner, "provides": provides}
    _db_cache = (mtime, db)
    return db


def base_package(name, pkgs):
    """bluez-openrc -> bluez: an init script belongs to the program it runs."""
    for suffix in ("-openrc", "-pyc"):
        if name.endswith(suffix) and name[: -len(suffix)] in pkgs:
            return name[: -len(suffix)]
    return name


def deptree():
    """OpenRC's dependency tree: {service: {"need": [...], "use": [...]}}."""
    raw = {}
    for line in _read_text(os.path.join(OPENRC, "deptree")).splitlines():
        m = re.match(r"depinfo_(\d+)_(\w+?)(?:_\d+)?='(.*)'$", line)
        if not m:
            continue
        n, key, val = m.groups()
        raw.setdefault(n, {}).setdefault(key, []).append(val)
    out = {}
    for d in raw.values():
        if "service" in d:
            out[d["service"][0]] = {"need": d.get("ineed", []), "use": d.get("iuse", [])}
    return out


def _processes():
    """Every userspace process, with memory and a CPU share since last asked."""
    global _cpu_prev
    now = time.monotonic()
    seen = {}
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % d, "rb") as f:
                raw = f.read()
            if not raw:
                continue             # a kernel thread
            with open("/proc/%s/stat" % d) as f:
                stat = f.read()
        except OSError:
            continue
        rest = stat[stat.rindex(")") + 2:].split()
        try:
            ppid = int(rest[1])
            ticks = int(rest[11]) + int(rest[12])
            rss = int(rest[21]) * PAGE_SIZE
        except (ValueError, IndexError):
            continue
        pid = int(d)
        mem = rss
        try:
            with open("/proc/%s/smaps_rollup" % d) as f:
                for line in f:
                    if line.startswith("Pss:"):
                        mem = int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError):
            pass
        try:
            exe = os.readlink("/proc/%s/exe" % d)
        except OSError:
            exe = ""
        prev = _cpu_prev.get(pid)
        cpu = None
        if prev and now - prev[1] > 0.2:
            cpu = 100.0 * (ticks - prev[0]) / CLK_TCK / (now - prev[1]) / NCPU
        seen[pid] = (ticks, now)
        argv = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")]
        out.append({"pid": pid, "ppid": ppid, "argv": argv, "exe": exe,
                    "mem": mem, "cpu": round(cpu, 1) if cpu is not None else None})
    _cpu_prev = seen
    return out


BIN_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")
INTERPRETERS = re.compile(r"^(sh|ash|bash|dash|busybox|env|python[0-9.]*)$")


def _owned(path, owner):
    """The path apk recorded for a file, or None. The binary directories are
    merged here, so /usr/bin/wpa_supplicant is recorded as usr/sbin/..."""
    if path in owner:
        return path
    for d in BIN_DIRS:
        if path.startswith(d):
            base = path[len(d):]
            for alt in BIN_DIRS:
                if alt + base in owner:
                    return alt + base
    return None


def _program(p, owner):
    """The file a process is really running: the script, not its interpreter."""
    a0 = p["argv"][0].split(":")[0]
    first = a0 if a0.startswith("/") else p["exe"]
    name = os.path.basename(first)
    if INTERPRETERS.match(name):
        # A venv's own python is the app's; the system one runs a script.
        if first.startswith("/usr/lib/") and _owned(first, owner):
            return _owned(first, owner)
        for arg in p["argv"][1:]:
            if arg == "-m" or arg == "-c":
                break
            if arg.startswith("-") or "=" in arg:
                continue
            if arg.startswith("/") and _owned(arg, owner):
                return _owned(arg, owner)
            break
    return _owned(first, owner) or _owned(p["exe"], owner) or first


def _service_roots(procs):
    """{pid: service} for the process each running service started."""
    by_pid = {p["pid"]: p for p in procs}
    roots = {}
    for p in procs:
        if os.path.basename(p["argv"][0]) == "supervise-daemon" and len(p["argv"]) > 1:
            roots[p["pid"]] = p["argv"][1]
    ddir = os.path.join(OPENRC, "daemons")
    try:
        services = os.listdir(ddir)
    except OSError:
        services = []
    for svc in services:
        try:
            recs = os.listdir(os.path.join(ddir, svc))
        except OSError:
            continue
        for rec in recs:
            kv = {}
            for line in _read_text(os.path.join(ddir, svc, rec)).splitlines():
                k, _, v = line.partition("=")
                kv[k] = v
            pid = None
            if kv.get("pidfile"):
                try:
                    pid = int(_read_text(kv["pidfile"]).split()[0])
                except (IndexError, ValueError):
                    pid = None
            if pid is None or pid not in by_pid:
                keys = sorted((k for k in kv if k.startswith("argv_")), key=lambda k: int(k[5:]))
                want = [kv[k] for k in keys]
                for p in procs:
                    if want and p["argv"][: len(want)] == want:
                        pid = p["pid"]
                        break
            if pid in by_pid and pid not in roots:
                roots[pid] = svc
    # Services OpenRC keeps no record for (avahi-daemon renames itself and
    # writes its own pidfile): a daemon of init's whose program is named after
    # the service.
    have = set(roots.values())
    for svc in _listdir(os.path.join(OPENRC, "started")) - have:
        for p in procs:
            if p["ppid"] != 1 or p["pid"] in roots:
                continue
            prog = os.path.basename(p["exe"] or p["argv"][0].split(":")[0])
            if prog == svc or prog.startswith(svc + "-"):
                roots[p["pid"]] = svc
                break
    return roots


def _listdir(path):
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _description(svc):
    m = DESC_RE.search(_read_text("/etc/init.d/" + svc))
    return m.group(1) if m else ""


def _meminfo():
    m = {}
    for line in _read_text("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        try:
            m[k] = int(v.split()[0]) * 1024
        except (IndexError, ValueError):
            pass
    total = m.get("MemTotal", 0)
    return {"total": total, "available": m.get("MemAvailable", 0),
            "used": total - m.get("MemAvailable", 0)}


def inventory(max_age=2.0):
    """Packages, services, processes and functions, as one consistent picture.

    Cached for a couple of seconds: a page polling it must not walk /proc for
    every request, and the CPU figures need a gap between two readings.
    """
    global _inv_cache
    if _inv_cache[1] is not None and time.monotonic() - _inv_cache[0] < max_age:
        return _inv_cache[1]
    db = apk_db()
    pkgs, owner = db["pkgs"], db["owner"]
    procs = _processes()
    by_pid = {p["pid"]: p for p in procs}
    roots = _service_roots(procs)
    started = _listdir(os.path.join(OPENRC, "started"))
    default = _listdir("/etc/runlevels/default")
    boot = _listdir("/etc/runlevels/boot") | _listdir("/etc/runlevels/sysinit")
    tree = deptree()
    man = manifest()

    # Which service each process belongs to: its own, or its nearest
    # ancestor's.
    for p in procs:
        svc, cur, hops = None, p, 0
        while cur and hops < 64:
            if cur["pid"] in roots:
                svc = roots[cur["pid"]]
                break
            cur = by_pid.get(cur["ppid"])
            hops += 1
        prog = _program(p, owner)
        p["program"] = prog
        who = owner.get(prog) or ""
        p["package"] = base_package(who, pkgs) if who else None
        # A login over SSH is a child of sshd, but it is a session, not sshd.
        if svc == "sshd" and not (p["package"] or "").startswith("openssh"):
            svc = None
            p["session"] = True
        p["service"] = svc
        name = os.path.basename(p["argv"][0])
        p["helper"] = (name in HELPERS or (svc is not None and
                       p["package"] in ("busybox", "busybox-binsh")))
        p["cmd"] = " ".join(p["argv"])[:200]

    # The services worth showing: the default runlevel, and anything else that
    # is running a process of its own.
    running_svcs = {p["service"] for p in procs if p["service"]}
    daemons = _listdir(os.path.join(OPENRC, "daemons"))
    initd = _listdir("/etc/init.d")
    names = sorted(((default | (started & running_svcs)) & initd))
    services = {}
    for svc in names:
        init_owner = owner.get("/etc/init.d/" + svc)
        sp = [p for p in procs if p["service"] == svc]
        services[svc] = {
            "name": svc,
            "desc": _description(svc),
            "started": svc in started,
            # Started but runs nothing: a one-shot that did its job at boot.
            "oneshot": svc in started and not sp and svc not in daemons,
            "at_boot": svc in default or svc in boot,
            "runlevel": "default" if svc in default else ("boot" if svc in boot else ""),
            "package": base_package(init_owner, pkgs) if init_owner else None,
            "pids": [p["pid"] for p in sp],
            "mem": sum(p["mem"] for p in sp),
            "cpu": round(sum(p["cpu"] or 0 for p in sp), 1),
            "need": [n for n in tree.get(svc, {}).get("need", []) if n in names],
            "use": [n for n in tree.get(svc, {}).get("use", []) if n in names],
            "functions": [],
            "control": ("none" if svc in NO_CONTROL or svc in boot
                        else "restart" if svc in RESTART_ONLY else "full"),
        }
    for s in services.values():
        s["needed_by"] = sorted(o["name"] for o in services.values() if s["name"] in o["need"])

    def closure(start):
        seen, todo = set(), list(start)
        while todo:
            n = todo.pop()
            if n in seen or n not in services:
                continue
            seen.add(n)
            todo.extend(services[n]["need"])
        return seen

    def contributors(svcs, extra=()):
        out = {}
        for s in svcs:
            sv = services[s]
            if sv["package"]:
                out.setdefault(sv["package"], set()).add(s)
            for pid in sv["pids"]:
                p = by_pid[pid]
                if p["package"] and not p["helper"]:
                    out.setdefault(p["package"], set()).add(s)
        for p in extra:
            if p["package"]:
                out.setdefault(p["package"], set()).add(os.path.basename(p["argv"][0]))
        return {k: sorted(v) for k, v in out.items()}

    # Functions: their own services, what those need, and the packages behind
    # both.
    apps_by_id = {a["id"]: a for a in man.get("apps", [])}
    functions = []
    for fn in man.get("functions", []):
        own = [s for s in fn.get("services", []) if s in services]
        relies = sorted(closure(own) - set(own))
        named = [p for p in procs if os.path.basename(p["argv"][0]) in fn.get("processes", [])]
        for s in own:
            services[s]["functions"].append(fn["id"])
        app = apps_by_id.get(fn.get("app"))
        installed_app = (app is None) or all(p in pkgs for p in app.get("packages", []))
        up = [s for s in own if services[s]["started"]]
        if not installed_app:
            state = "not-installed"
        elif (own and len(up) == len(own)) or (not own and named):
            state = "running"
        elif up or named:
            state = "partial"
        else:
            state = "stopped"
        functions.append({
            "id": fn["id"], "label": fn["label"], "summary": fn.get("summary", ""),
            "route": fn.get("route"), "app": fn.get("app"), "state": state,
            "services": own, "relies_on": relies,
            "processes": [p["pid"] for p in named],
            "packages": contributors(own, named),
            "relied_packages": contributors(relies),
            "mem": sum(services[s]["mem"] for s in own) + sum(p["mem"] for p in named),
            "cpu": round(sum(services[s]["cpu"] for s in own) +
                         sum(p["cpu"] or 0 for p in named), 1),
        })

    # Apps: ours, the kernel, and every package that runs something here.
    user_pkgs = {p: a for a in man.get("apps", []) for p in a.get("packages", [])}
    fn_pids = {pid for f in functions for pid in f["processes"]}
    wanted = {n for n in pkgs if n.startswith(CORE)} | ({KERNEL_PKG} & set(pkgs))
    wanted |= {s["package"] for s in services.values() if s["package"]}
    wanted |= {p["package"] for p in procs
               if p["package"] and not p["helper"] and
               (p["service"] in services or p["pid"] in fn_pids)}
    upd = _kv(UPDATES_FILE) or {"upd": [], "sys": []}
    avail = {parts[0]: parts[2] for parts in upd["upd"] + upd.get("sys", []) if len(parts) == 3}
    core = man.get("core", {})
    packages = {}
    for n in sorted(wanted):
        info = pkgs.get(n)
        if not info:
            continue
        mine = [s for s in services.values() if s["package"] == n]
        mine_procs = [p for p in procs if p["package"] == n and not p["helper"]]
        app = user_pkgs.get(n)
        title = (app or {}).get("name") or (core.get("name") if n == CORE else n)
        label = (app or {}).get("label") or (core.get("label") if n == CORE else "")
        packages[n] = {
            "name": n, "title": title, "label": label,
            "kind": "user" if app else "system",
            "ours": is_ours(n),
            "app_id": (app or {}).get("id"),
            "version": info["version"],
            "size": info["size"],
            "desc": info["desc"],
            "services": [s["name"] for s in mine],
            "running": sum(1 for s in mine if s["started"]),
            "processes": len(mine_procs),
            "mem": sum(p["mem"] for p in mine_procs),
            "update": avail.get(n),
            "functions": sorted({f["id"] for f in functions
                                 if n in f["packages"] or n in f["relied_packages"]}),
        }

    inv = {
        "now": int(time.time()),
        "services": services,
        "functions": functions,
        "packages": packages,
        "processes": [dict({k: p[k] for k in ("pid", "ppid", "cmd", "program", "package",
                                               "service", "mem", "cpu", "helper")},
                           session=bool(p.get("session")))
                      for p in procs],
        "memory": _meminfo(),
        "ncpu": NCPU,
    }
    _inv_cache = (time.monotonic(), inv)
    return inv


def package_detail(name):
    """One package, for its app page: apk's record, what it depends on and
    what depends on it, its services, its processes and what uses it."""
    db = apk_db()
    info = db["pkgs"].get(name)
    if not info:
        raise ValueError("not installed: %s" % name)
    inv = inventory()
    rdeps = sorted(n for n, i in db["pkgs"].items() if name in i["deps"])
    out = dict(inv["packages"].get(name) or {
        "name": name, "title": name, "label": "", "kind": "system",
        "ours": is_ours(name), "app_id": None, "version": info["version"],
        "size": info["size"], "desc": info["desc"], "services": [], "running": 0,
        "processes": 0, "mem": 0, "update": None, "functions": []})
    out.update(url=info["url"], license=info["license"], depends=info["deps"],
               required_by=rdeps,
               service_info=[inv["services"][s] for s in out["services"]
                             if s in inv["services"]],
               process_info=[p for p in inv["processes"]
                             if p["package"] == name and not p["helper"]],
               function_info=[{k: f[k] for k in ("id", "label", "state")}
                              for f in inv["functions"] if f["id"] in out["functions"]])
    app = next((a for a in manifest().get("apps", []) if name in a.get("packages", [])), None)
    links = source_links(info["version"]) if name.startswith(CORE) else None
    if app:
        out["app"] = dict(app, changes=[dict(c, url=links["blob"] + c["file"] if links["blob"] else "")
                                        for c in app.get("changes", [])])
    if links:
        out["links"] = links
    return out


# ---------------------------------------------------------------------------
# Date and time
# ---------------------------------------------------------------------------

ZONEINFO = "/usr/share/zoneinfo"
TZ_FILE = "/opt/persist/timezone"
TZ_AT_BOOT = "/run/biscuit-timezone-at-boot"
REGION_FILE = "/opt/persist/region"


def _zones():
    """Every time zone, from zone1970.tab (named ones; no bare offsets), with
    the countries each covers."""
    zones = {}
    for line in _read_text(os.path.join(ZONEINFO, "zone1970.tab")).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            zones[parts[2]] = parts[0].split(",")
    zones.setdefault("Etc/UTC", [])
    return zones


def current_timezone():
    tz = _read_text("/etc/timezone").strip()
    if tz:
        return tz
    try:
        target = os.readlink("/etc/localtime")
        return target.split(ZONEINFO + "/", 1)[1]
    except (OSError, IndexError):
        return "Etc/UTC"


def _chrony():
    """Whether the clock is synchronised, and from what."""
    try:
        p = subprocess.run(["chronyc", "-n", "-c", "tracking"], capture_output=True,
                           text=True, timeout=5)
        f = p.stdout.strip().split(",")
        # CSV: ref id, ref name/ip, stratum, ref time, system offset, ...
        if p.returncode == 0 and len(f) > 5:
            stratum = int(f[2])
            return {"synced": stratum > 0 and f[1] not in ("", "0.0.0.0"),
                    "source": f[1], "stratum": stratum,
                    "offset_ms": round(float(f[4]) * 1000, 1)}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"synced": False, "source": "", "stratum": 0, "offset_ms": None}


def datetime_state():
    tz = current_timezone()
    zones = _zones()
    region = _read_text(REGION_FILE).strip().upper()
    suggested = sorted(z for z, cc in zones.items() if region and region in cc)
    try:
        import zoneinfo
        now = __import__("datetime").datetime.now(zoneinfo.ZoneInfo(tz))
        local, offset = now.strftime("%A %d %B %Y, %H:%M"), now.strftime("%z")
    except Exception:  # noqa: BLE001 - an unknown zone must not break the page
        local, offset = time.strftime("%A %d %B %Y, %H:%M UTC", time.gmtime()), "+0000"
    # The zone the running services started with. biscuit-persist records it
    # at boot; a program reads the zone once, when it starts, so after a
    # change they keep the old one until they restart.
    at_boot = _read_text(TZ_AT_BOOT).strip()
    return {"timezone": tz, "local": local, "utc_offset": offset[:3] + ":" + offset[3:],
            "epoch": int(time.time()), "region": region, "suggested": suggested,
            "zones": sorted(zones), "sync": _chrony(),
            "restart_needed": bool(at_boot) and at_boot != tz}


def set_timezone(tz):
    if not isinstance(tz, str) or tz not in _zones() or ".." in tz:
        raise ValueError("unknown time zone")
    path = os.path.join(ZONEINFO, tz)
    if not os.path.isfile(path):
        raise ValueError("no zone file for %s" % tz)
    tmp = TZ_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(tz + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TZ_FILE)
    link_tmp = "/etc/localtime.tmp"
    try:
        os.unlink(link_tmp)
    except OSError:
        pass
    os.symlink(path, link_tmp)
    os.replace(link_tmp, "/etc/localtime")
    with open("/etc/timezone", "w") as f:
        f.write(tz + "\n")
    return datetime_state()


# ---------------------------------------------------------------------------
# Files from stock
#
# This Echo's own files, copied from its backup when postmarketOS was
# installed: firmware without which there is no Wi-Fi or Bluetooth, the Fire
# OS microphone files, the speaker's correction curve, and Amazon's sounds.
# None can be downloaded again, so they are never erased by a reset; this is
# where they are seen and managed.
# ---------------------------------------------------------------------------

ASSET_MANIFEST = "/usr/share/biscuit/biscuit-profile-assets.json"
ASSET_STORE = "/opt/persist/biscuit/assets"
EARCON_DIR = "/opt/persist/earcon"
# Amazon's light ring animations, imported from the owner's backup. The ring
# loads them from here by name, on demand; nothing ships them.
LED_STORE = "/opt/persist/biscuit/led"
RING_MODULE = "/usr/bin/biscuit-ring.py"
LED_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,95}\.animation$")
# Names the device generates for itself, which a stock file must not shadow.
LED_OWN_PREFIXES = ("act_", "fx_", "volume_step-", "volume-muted")
LED_MAX_FILE = 256 << 10
LED_HEADROOM = 1 << 20          # left free in the settings store after an import
IMPORTER = "/usr/bin/biscuit-import-assets"
EARCON_TYPES = (".mp3", ".ogg", ".wav", ".flac")
PROFILE_INFO = {
    "firmware": ("Firmware", "Wi-Fi, Bluetooth and the microphone board's chip. "
                 "Without it this Echo has no Wi-Fi or Bluetooth.", True),
    "fireos6": ("Fire OS 6 microphone files", "Amazon's microphone filters and voice "
                "detector, for the Fire OS 6 processing and the stock call tuning. "
                "Without them the microphones use pmOS processing.", False),
    "fireos5": ("Fire OS 5 microphone files", "The older generation's filters. Fire OS 6 "
                "devices cannot supply them.", False),
    # Not shipped: it is Amazon's file. Any generation supplies it (EQ_50.cfg,
    # EQ_60.cfg and EQ_70.cfg are the same bytes), and biscuit-dsp reads it
    # straight from the store.
    "speaker": ("Speaker correction curve", "Amazon's tuning for this speaker (EQ_50.cfg from "
                "the backup's audio-algorithms folder), used by the Stock and Custom "
                "equaliser. Without it the speaker plays flat.", False),
}
# What removing a profile does to the device, said when it is removed.
PROFILE_REMOVED = {
    "speaker": "Removed. The speaker plays flat until the curve is imported again.",
}
DSP_CONTROL = "/run/biscuit-dsp/control"


def _reload_dsp():
    """Have a running biscuit-dsp re-read its curve. Never blocks; harmless
    when the DSP is not running (it reads the store when it starts)."""
    try:
        fd = os.open(DSP_CONTROL, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"reload\n")
        finally:
            os.close(fd)
    except OSError:
        pass
_digest_cache = {}
# How each imported animation is named and grouped for a person: the LED
# agent's stock_catalogue(), which the settings server puts here once it has
# loaded the agent. This module does not load the agent itself; without it
# the files are listed by name, as before.
led_catalogue = None


def _digest(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _digest_cache:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _digest_cache[key] = h.hexdigest()
    return _digest_cache[key]


def _asset_profiles():
    try:
        with open(ASSET_MANIFEST, encoding="utf-8") as f:
            return json.load(f).get("profiles", {})
    except (OSError, ValueError):
        return {}


def stock_state():
    profiles = []
    for pid, spec in _asset_profiles().items():
        label, what, required = PROFILE_INFO.get(pid, (pid, "", False))
        files = []
        for item in spec.get("files", []):
            stored = os.path.join(ASSET_STORE, pid, item["name"])
            variants = item.get("variants") or [item]
            state, size, note = "absent", 0, ""
            if os.path.isfile(stored):
                size = os.path.getsize(stored)
                got = _digest(stored)
                match = next((v for v in variants if v.get("sha256") == got), None)
                state = "ok" if match else "corrupt"
                note = (match or {}).get("note", "")
            # A profile read straight from the store (the speaker curve) is
            # in use exactly when it is stored.
            live = (stored if spec.get("activation") == "none" else
                    os.path.join(spec.get("directory", "/usr/share/biscuit"), item["name"]))
            files.append({"name": item["name"], "state": state, "size": size, "note": note,
                          "required": bool(item.get("required")), "in_place": os.path.exists(live)})
        present = [f for f in files if f["state"] != "absent"]
        profiles.append({
            "id": pid, "label": label, "what": what, "required": required,
            "files": files, "imported": bool(present),
            "state": ("absent" if not present else
                      "ok" if all(f["state"] == "ok" for f in present) else "problem"),
        })
    earcons = []
    for n in sorted(_listdir(EARCON_DIR)):
        p = os.path.join(EARCON_DIR, n)
        if os.path.isfile(p) and n.lower().endswith(EARCON_TYPES):
            earcons.append({"name": n, "size": os.path.getsize(p)})
    led = []
    try:
        catalogue = {e["name"]: dict(e, order=i) for i, e in enumerate(led_catalogue())}
    except Exception:                   # noqa: BLE001 - no agent, or a bad file
        catalogue = {}
    for n in sorted(_listdir(LED_STORE), key=str.lower):
        p = os.path.join(LED_STORE, n)
        # The device's own names (volume ramp) are left out, as an import
        # leaves them out: an older installer copied them in.
        if os.path.isfile(p) and LED_NAME.match(n) and not n.startswith(LED_OWN_PREFIXES):
            name = n[:-len(".animation")]
            info = catalogue.get(name, {})
            same = catalogue.get(info.get("same"), {})
            led.append({"name": name, "size": os.path.getsize(p),
                        "label": info.get("label", name), "group": info.get("group", "Other"),
                        "hidden": info.get("hidden"), "same_label": same.get("label"),
                        "order": info.get("order", len(catalogue))})
    # In the pickers' order - by group, then label - once there are labels.
    led.sort(key=lambda a: a.pop("order"))
    try:
        usage = shutil.disk_usage("/opt/persist")
        store = {"total": usage.total, "free": usage.free, "used": usage.total - usage.free}
    except OSError:
        store = None
    return {"profiles": profiles, "earcons": earcons, "led": led, "store": store}


def stock_file_path(kind, name):
    """The stored path of one stock file, refusing anything outside the store."""
    if kind == "earcon":
        base = EARCON_DIR
    elif kind == "led":
        base = LED_STORE
        if not name.endswith(".animation"):
            name += ".animation"
    elif kind in _asset_profiles():
        base = os.path.join(ASSET_STORE, kind)
    else:
        raise ValueError("unknown kind")
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(path):
        raise ValueError("no such file")
    return path


def stock_remove(kind, name=None):
    """Remove a microphone profile, or one or all of the sounds. Firmware is
    never removed: this Echo has no Wi-Fi without it."""
    if kind == "earcon":
        names = [name] if name else [e["name"] for e in stock_state()["earcons"]]
        for n in names:
            os.unlink(stock_file_path("earcon", n))
        return "Removed %d sound(s)." % len(names)
    if kind == "led":
        names = [name] if name else [a["name"] for a in stock_state()["led"]]
        for n in names:
            os.unlink(stock_file_path("led", n))
        return "Removed %d animation(s)." % len(names)
    profiles = _asset_profiles()
    if kind not in profiles:
        raise ValueError("unknown kind")
    if PROFILE_INFO.get(kind, ("", "", False))[2]:
        raise ValueError("Firmware cannot be removed: without it this Echo has no Wi-Fi.")
    p = subprocess.run([IMPORTER, "deactivate", "--profile", kind], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise ValueError((p.stderr or p.stdout).strip()[-300:] or "could not remove")
    shutil.rmtree(os.path.join(ASSET_STORE, kind), ignore_errors=True)
    if kind == "speaker":
        _reload_dsp()
    return PROFILE_REMOVED.get(
        kind, "Removed. The microphones use pmOS processing where these were used.")


_ring_mod = None


def _ring():
    """The ring player's own module, for its .animation parser: a file is
    accepted exactly when the ring can play it."""
    global _ring_mod
    if _ring_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("biscuit_ring", RING_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ring_mod = mod
    return _ring_mod


def _import_led(staging):
    """Amazon's ring animations, from the owner's backup. Each file must be a
    sensibly named .animation the ring can play; anything else is skipped and
    said so. Same-named files are replaced; nothing else is touched."""
    ring = _ring()
    good, skipped, own = [], [], 0
    for name in sorted(os.listdir(staging)):
        src = os.path.join(staging, name)
        why = None
        if not name.endswith(".animation"):
            continue        # the folder's layer table and the like: not asked for
        if name.startswith(LED_OWN_PREFIXES):
            # Amazon's volume ring: this Echo draws its own under these names.
            own += 1
            continue
        if not LED_NAME.match(name):
            why = "not a name this Echo accepts"
        elif os.path.getsize(src) > LED_MAX_FILE:
            why = "too large"
        else:
            try:
                if ring.Animation.load(src) is None:
                    why = "no frames the ring can play"
            except (OSError, ValueError, UnicodeDecodeError) as err:
                why = "unreadable (%s)" % err
        if why:
            skipped.append("%s: %s" % (name, why))
        else:
            good.append(name)
    if not good:
        raise ValueError("No light ring animations were found. Choose the .animation files from "
                         "your backup's led folder." + (" Skipped " + skipped[0] + "." if skipped else ""))
    need = sum(os.path.getsize(os.path.join(staging, n)) + 4096 for n in good)
    os.makedirs(LED_STORE, exist_ok=True)
    free = shutil.disk_usage(LED_STORE).free
    if need + LED_HEADROOM > free:
        raise ValueError("Not enough room in the settings store: these need %d KB and %d KB is free."
                         % (need >> 10, max(0, free - LED_HEADROOM) >> 10))
    for name in good:
        tmp = os.path.join(LED_STORE, "." + name + ".tmp")
        shutil.copyfile(os.path.join(staging, name), tmp)
        os.replace(tmp, os.path.join(LED_STORE, name))
    os.sync()
    msg = "Imported %d animation(s)." % len(good)
    if own:
        msg += " Left out %d volume animations: this Echo draws its own." % own
    if skipped:
        msg += " Skipped %d: %s%s." % (len(skipped), "; ".join(skipped[:3]),
                                       "; ..." if len(skipped) > 3 else "")
    return msg


def stock_import(kind, staging):
    """Import uploaded files: a profile through the importer, which checks every
    file against the manifest by size and hash; sounds by type and size."""
    if kind == "earcon":
        n = 0
        for name in os.listdir(staging):
            src = os.path.join(staging, name)
            if not name.lower().endswith(EARCON_TYPES) or os.path.getsize(src) > 2 << 20:
                continue
            shutil.copyfile(src, os.path.join(EARCON_DIR, os.path.basename(name)))
            n += 1
        if not n:
            raise ValueError("no sound files (mp3, ogg, wav or flac, under 2 MB)")
        return "Imported %d sound(s)." % n
    if kind == "led":
        return _import_led(staging)
    if kind not in _asset_profiles():
        raise ValueError("unknown kind")
    p = subprocess.run([IMPORTER, "import", staging, "--profile", kind], capture_output=True,
                       text=True, timeout=120)
    if p.returncode == 2 and kind == "speaker":
        # The importer's own last line is "run 'detect' ...", which means
        # nothing on this page. Any of the three names is the same file.
        raise ValueError("None of these files is the speaker curve. Choose EQ_50.cfg (EQ_60.cfg "
                         "and EQ_70.cfg are the same file) from Fire OS's audio-algorithms folder.")
    if p.returncode != 0:
        raise ValueError((p.stderr or p.stdout).strip().splitlines()[-1][-300:]
                         if (p.stderr or p.stdout).strip() else "import failed")
    if kind == "speaker":
        # The DSP reads the curve from the store; take it now, not at the next
        # restart. Whether it is heard depends on the equaliser: Stock and
        # Custom use it, Off does not.
        _reload_dsp()
        return "Imported. Stock and Custom equaliser now use this speaker's curve."
    return (p.stdout.strip().splitlines() or ["Imported."])[-1]


# ---------------------------------------------------------------------------
# The settings page's address
#
# http://<name>.local:8080 by default. The name is the device name; the port
# can be changed here and in setup. 80 leaves the number out of the address.
# ---------------------------------------------------------------------------

SETTINGS_PORT_FILE = "/opt/persist/settings-port"
DEFAULT_SETTINGS_PORT = 8080
# Ports this device already uses, or that a browser refuses to open.
RESERVED_PORTS = {
    22: "SSH", 53: "the setup network's DNS", 67: "the setup network's DHCP",
    5353: "network discovery", 6053: "the voice assistant",
    6054: "the Bluetooth proxy", 4713: "the sound server",
    # Chrome and Firefox refuse these outright (ERR_UNSAFE_PORT).
    1719: "browsers", 1720: "browsers", 1723: "browsers", 2049: "browsers",
    3659: "browsers", 4045: "browsers", 4190: "browsers", 5060: "browsers",
    5061: "browsers", 6000: "browsers", 6566: "browsers", 6665: "browsers",
    6666: "browsers", 6667: "browsers", 6668: "browsers", 6669: "browsers",
    6679: "browsers", 6697: "browsers", 10080: "browsers",
}


def settings_port():
    """The port the settings page listens on, from the store; 8080 when none
    is chosen or the stored one is unusable."""
    try:
        return check_port(_read_text(SETTINGS_PORT_FILE).strip() or DEFAULT_SETTINGS_PORT)
    except ValueError:
        return DEFAULT_SETTINGS_PORT


def check_port(value, current=None, live=False):
    """The port as an int, or ValueError saying why it cannot be used.

    80, or anything from 1024 up that this device and browsers leave free.
    `live` also tries to bind it, to catch another program already there;
    the port the page is on now is always allowed.
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("A port is a number, such as 8080.")
    if port == current:
        return port
    if port != 80 and not 1024 <= port <= 65535:
        raise ValueError("Use 80, or a number from 1024 to 65535.")
    if port in RESERVED_PORTS:
        raise ValueError("%d is used by %s. Try 8080 or 8000." % (port, RESERVED_PORTS[port]))
    if live:
        # Probed as the server will bind it: with SO_REUSEADDR, so a port that
        # recently served connections (TIME_WAIT for a minute) is not taken
        # for one in use - only another listener is.
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(1)
        except OSError:
            raise ValueError("Something else on this Echo is already using %d." % port)
        finally:
            sock.close()
    return port


def settings_url(name, port):
    host = "%s.local" % (name or "").lower()
    return "http://%s%s/" % (host, "" if port == 80 else ":%d" % port)


def set_settings_port(value, current):
    """Store a new port and restart the settings server on it, a moment after
    the answer has gone back to the page."""
    port = check_port(value, current=current, live=True)
    if port == current:
        return port
    tmp = SETTINGS_PORT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("%d\n" % port)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_PORT_FILE)
    subprocess.Popen(["setsid", "sh", "-c", "sleep 1; rc-service biscuit-settings restart"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return port


# ---------------------------------------------------------------------------
# Resets
# ---------------------------------------------------------------------------

RESET_LIST = "/usr/share/biscuit/reset.conf"
RESET_REQUEST = "/opt/persist/reset-request"
RESET_SCOPES = {"settings": ("settings",), "network": ("network",),
                "all": ("settings", "network", "all")}


def reset_plan():
    """For each reset, what it erases, from the same list the boot-time
    eraser reads."""
    lines = []
    for line in _read_text(RESET_LIST).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) == 3:
            lines.append(parts)
    plan = {}
    for scope, takes in RESET_SCOPES.items():
        seen, items = set(), []
        for s, _path, desc in lines:
            if s in takes and desc not in seen:
                seen.add(desc)
                items.append(desc)
        plan[scope] = items
    return {"plan": plan, "pending": _read_text(RESET_REQUEST).strip() or None}


def request_reset(scope):
    if scope not in RESET_SCOPES:
        raise ValueError("unknown reset")
    with open(RESET_REQUEST, "w") as f:
        f.write(scope + "\n")
        f.flush()
        os.fsync(f.fileno())
    subprocess.Popen(["setsid", "sh", "-c", "sync; sleep 2; reboot"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def storage_state():
    out = []
    for path, label in (("/", "System"), ("/opt", "Settings and stock files"), ("/boot", "Boot")):
        try:
            u = shutil.disk_usage(path)
            out.append({"path": path, "label": label, "total": u.total, "used": u.total - u.free})
        except OSError:
            pass

    def du(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total

    db = apk_db()
    return {"disks": out, "apk_cache": du("/var/cache/apk"), "logs": du("/var/log"),
            "packages": sum(p["size"] for p in db["pkgs"].values()),
            "package_count": len(db["pkgs"])}
