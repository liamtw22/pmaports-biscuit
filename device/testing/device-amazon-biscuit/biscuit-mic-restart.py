#!/usr/bin/env python3
"""Restart the capture chain, but only when it is safe, and prove it came back.

The old helper was nineteen lines: take a lock, restart the hub, restart the
assistant if it was running. It had three holes that only matter when something
goes wrong, which is exactly when a settings change is most likely to be made.

  - It blocked on the lock instead of refusing. Two saves in quick succession
    queued a second restart behind the first, so the device went deaf twice for
    no reason and the second one raced the first one's readiness.
  - It restarted during anything. A microphone profile change ten seconds into a
    phone call cuts the call; during a ringing alarm it silences the thing the
    owner is trying to stop.
  - It never checked the chain came back. rc-service restart returning 0 means
    OpenRC started a process, not that audio is flowing. A profile whose binary
    exits immediately looked exactly like success, and the device sat deaf until
    somebody noticed.

So: refuse rather than queue, refuse during a call or an alarm, wait for frames
to actually advance, and roll back to the last profile that did come back if the
new one does not. The outcome is written where both front ends can read it.

Exit codes, because the callers are a web page and Home Assistant:
  0  the chain came back
  3  refused: a call or an alarm is in progress
  4  came back, but only after rolling back to the last-good profile
  5  did not come back, and the rollback did not either
 75  refused: another restart already holds the lock (EX_TEMPFAIL)
"""
import argparse
import errno
import fcntl
import json
import os
import subprocess
import sys
import time

RUN = "/run/biscuit-mic"
LOCK = os.path.join(RUN, "restart.lock")
STATUS = os.path.join(RUN, "status.json")
PROFILE = os.path.join(RUN, "profile.json")
OUTCOME = os.path.join(RUN, "restart.json")
RING_STATE = "/run/biscuit-ring/state"
MIC_ENV = "/opt/persist/mic.env"
LAST_GOOD = "/opt/persist/mic-lastgood"

# Any of these on the ring means the device is asking for attention right now.
RINGING = ("timer_ringing", "ready-alarm", "active_alarm", "ready-timer-short",
           "ready-alarm-short")


def read_json(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


CALL_STATE = "/run/biscuit-btcall/call"
CALL_FRESH_S = 20.0


def call_is_live():
    """Is a Bluetooth call up right now?

    Asked of biscuit-btcall, the one process that knows: it writes CALL_STATE
    at call-up, rewrites it every ~4.5 s with the DSP hold, and removes it when
    the call ends. Only a recent mtime counts, so a btcall that dies mid-call
    releases the guard within CALL_FRESH_S instead of blocking restarts for
    ever - the same four-missed-refreshes margin as the DSP's own hold.

    This used to watch the hub's transport_bytes advance. That counter read 0
    through an entire 557 s call, so the guard had never once fired.
    """
    try:
        age = time.time() - os.stat(CALL_STATE).st_mtime
    except OSError:
        return False
    return age <= CALL_FRESH_S


def alarm_is_ringing():
    state = read_json(RING_STATE)
    if not state:
        return False
    names = list(state.get("active") or [])
    visible = state.get("visible")
    if visible:
        names.append(visible)
    return any(any(r in str(n) for r in RINGING) for n in names)


def chain_ready(timeout, settle=1.0):
    """Wait until the hub is serving the assistant AND frames are advancing.

    Both halves are needed. "ready" alone goes true as soon as the hub opens
    capture, which a branch that exits immediately afterwards still satisfies;
    a frame count alone can be a stale file from before the restart.
    """
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        doc = read_json(STATUS)
        if doc and doc.get("ready") and (doc.get("branch_ready") or {}).get("assistant"):
            frames = (doc.get("frames") or {}).get("assistant")
            if isinstance(frames, int):
                if seen is not None and frames > seen:
                    return True
                seen = frames
        time.sleep(settle)
    return False


def env_profile(path=MIC_ENV):
    try:
        with open(path) as handle:
            for line in handle:
                if line.startswith("BISCUIT_MIC_PROFILE="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "pmos"


def set_env_profile(flag, path=MIC_ENV):
    """Rewrite one key in place, preserving everything else and the file's shape."""
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except OSError:
        return False
    out, found = [], False
    for line in lines:
        if line.startswith("BISCUIT_MIC_PROFILE="):
            out.append("BISCUIT_MIC_PROFILE=%s\n" % flag)
            found = True
        else:
            out.append(line)
    if not found:
        out.append("BISCUIT_MIC_PROFILE=%s\n" % flag)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as handle:
            handle.writelines(out)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def read_last_good():
    try:
        with open(LAST_GOOD) as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def write_last_good(flag):
    # Best effort, and deliberately not fatal: losing the record costs a rollback
    # target, not the restart.
    try:
        tmp = LAST_GOOD + ".tmp"
        with open(tmp, "w") as handle:
            handle.write(flag + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, LAST_GOOD)
    except OSError:
        pass


def write_outcome(**fields):
    fields["at"] = time.time()
    try:
        tmp = OUTCOME + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(fields, handle)
        os.replace(tmp, OUTCOME)
    except OSError:
        pass


def restart_services(timeout):
    """Restart the hub, and the assistant only if it was already up."""
    active = subprocess.run(["rc-service", "biscuit-voice-assistant", "status"],
                            capture_output=True, timeout=10).returncode == 0
    subprocess.run(["rc-service", "biscuit-mic-hub", "restart"],
                   check=True, timeout=timeout)
    # LVA ignores repeat registration for existing IDs, so recreating its
    # registration table is what refreshes options and availability after a
    # profile or import change.
    if active:
        subprocess.run(["rc-service", "biscuit-voice-assistant", "restart"],
                       check=True, timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="Restart the capture chain safely.")
    ap.add_argument("--reason", default="", help="what asked for this, for the log")
    ap.add_argument("--force", action="store_true",
                    help="restart even during a call or an alarm")
    ap.add_argument("--timeout", type=float, default=45.0,
                    help="seconds to wait for the chain to come back")
    args = ap.parse_args()

    os.makedirs(RUN, exist_ok=True)
    with open(LOCK, "w") as lock:
        # Non-blocking on purpose. A queued second restart is never what the
        # caller wanted: it doubles the deaf window and races the first one's
        # readiness check.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            if err.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            write_outcome(ok=False, refused="busy", reason=args.reason)
            print("biscuit-mic-restart: another restart is already running",
                  file=sys.stderr)
            return 75

        if not args.force:
            if call_is_live():
                write_outcome(ok=False, refused="call", reason=args.reason)
                print("biscuit-mic-restart: a call is in progress", file=sys.stderr)
                return 3
            if alarm_is_ringing():
                write_outcome(ok=False, refused="alarm", reason=args.reason)
                print("biscuit-mic-restart: an alarm or timer is ringing",
                      file=sys.stderr)
                return 3

        requested = env_profile()
        # Only trust the current chain as a rollback target if it is up right
        # now. Recording the request instead would happily remember a profile
        # that has never once worked.
        before = read_json(STATUS) or {}
        if before.get("ready"):
            running = (read_json(PROFILE) or {}).get("effective")
            if running:
                write_last_good(running)

        started = time.monotonic()
        restart_services(args.timeout)
        if chain_ready(args.timeout):
            effective = (read_json(PROFILE) or {}).get("effective")
            write_last_good(effective or requested)
            write_outcome(ok=True, requested=requested, reason=args.reason,
                          effective=effective,
                          seconds=round(time.monotonic() - started, 1))
            return 0

        # It did not come back. That is not a degraded profile - profile.json
        # already reports those and the chain keeps running - it is no chain at
        # all, so put back whatever worked last and try once more.
        fallback = read_last_good()
        print("biscuit-mic-restart: the chain did not come back within %.0fs"
              % args.timeout, file=sys.stderr)
        if fallback and fallback != requested and set_env_profile(fallback):
            print("biscuit-mic-restart: rolling back to %s" % fallback,
                  file=sys.stderr)
            restart_services(args.timeout)
            if chain_ready(args.timeout):
                write_outcome(ok=True, rolled_back=True, requested=requested,
                              effective=fallback, reason=args.reason,
                              seconds=round(time.monotonic() - started, 1))
                return 4
        write_outcome(ok=False, rolled_back=bool(fallback), requested=requested,
                      reason=args.reason,
                      seconds=round(time.monotonic() - started, 1))
        return 5


if __name__ == "__main__":
    sys.exit(main())
