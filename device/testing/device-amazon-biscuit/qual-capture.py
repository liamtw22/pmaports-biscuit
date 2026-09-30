#!/usr/bin/env python3
"""Capture the raw microphone array for a qualification sitting (3.13, 3.5).

Runs ON the device. One capture per condition, and each one records the state it
was taken in, because 3.13 asks for distance, level, route, assets and
calibration and none of that can be reconstructed afterwards.

WHY RAW, AND WHY ONLY ONE PASS
------------------------------
`biscuit-beamform` and `biscuit-mic-fireos6` are stdin/stdout filters: 8-channel
S24_3LE at 16 kHz in, mono S16_LE out. So a single raw capture replays through
BOTH profiles offline, on byte-identical audio. 3.13 asks for "matched
conditions" across two profiles; this is better than matched, it is identical,
and it costs one pass of the owner's time instead of two.

The hub owns the capture device, so it is stopped for the duration and restarted
afterwards. The assistant is deaf while a capture runs - which is fine, because
nothing here needs the live chain. The live behaviours 3.13 also wants (Stop
targets, barge-in, mute, direction, reconnect) are a separate part of the sitting
and are not what this tool is for.

INTENT LABELS ARE THE POINT OF THE PROMPTS (3.5)
-----------------------------------------------
3.5 exists because a nonempty transcript cannot tell an intended wake from the
television. So intent is established by the PROMPT, not by anything the device
heard: the tool announces a window, the owner speaks inside it, and the window is
recorded. A detection inside a prompted window is a true positive; a detection
outside every prompted window is a candidate false accept, independent of what
STT made of it.

That only holds if the owner speaks when prompted and stays quiet otherwise, so
the prompts are deliberately explicit about which is wanted.

    qual-capture.py --session s1 --condition near-quiet --seconds 30 --prompts 5
    qual-capture.py --session s1 --condition noise-tv --seconds 60 --prompts 0
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

CAPTURE_DEV = "hw:0,2"          # hw:0,0 is the playback codec, owned by biscuit-dsp
CHANNELS = 8
RATE = 16000
FORMAT = "S24_3LE"
OUT_ROOT = "/var/lib/biscuit/captures"   # on /, which has room; NOT /opt
# /opt is the 13.7 MB persist partition. One 30 s capture is 11.5 MB, so a
# default under /opt cannot hold even one, and arecord would have written a
# short file and returned 0 - a silent partial capture is worse than none.
HUB = "biscuit-mic-hub"


def run(*args, **kw):
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


def read(path, default=""):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return default


def sha256_of(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def alsa_gain():
    out = run("amixer", "-c", "0", "cget", "name=PCM Playback Volume").stdout
    for line in out.splitlines():
        if ": values=" in line:
            return line.split(": values=", 1)[1].strip()
    return None


def raw_stats(path, stride=4):
    """Per-channel DC and clipping of the RAW capture.

    Clipping is the mechanism the volume study blamed for the loss at high
    playback gain (0.010% at 80, 0.153% at 100, 9.6% at 127 with an
    interferer), and it is only visible in the raw array - the chain output is
    downstream of gain and limiting, so it can look clean while the ADC railed.

    DC is reported beside it because this array used to carry ~0.11 of full
    scale per channel, enough to eat headroom asymmetrically and wreck any SNR
    computed on raw samples. The ADC high-pass removed it - under 0.00005 FS
    on 2026-09-22 - so a non-zero figure here now means that fix regressed.
    """
    width, full = 3, 1 << 23
    frame = CHANNELS * width
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    frames = len(data) // frame
    if not frames:
        return None
    acc = [0] * CHANNELS
    clip = [0] * CHANNELS
    peak = [0] * CHANNELS
    n = 0
    for f in range(0, frames, stride):
        base = f * frame
        for c in range(CHANNELS):
            o = base + c * width
            v = int.from_bytes(data[o:o + width], "little", signed=True)
            acc[c] += v
            a = abs(v)
            if a > peak[c]:
                peak[c] = a
            if a >= full - 16:
                clip[c] += 1
        n += 1
    return {
        "frames_sampled": n,
        "stride": stride,
        "dc_fraction_fs": [round(acc[c] / n / full, 4) for c in range(CHANNELS)],
        "peak_fraction_fs": [round(peak[c] / full, 4) for c in range(CHANNELS)],
        "clip_percent": [round(100.0 * clip[c] / n, 4) for c in range(CHANNELS)],
    }


def environment():
    """Everything 3.13 wants recorded, gathered at capture time.

    Assets and calibration are hashed rather than named: "fireos6 assets present"
    is not a fact that survives a reimport, and a qualification result that
    cannot say WHICH coefficients produced it is not worth much.
    """
    env = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uptime_s": float(read("/proc/uptime", "0").split()[0] or 0),
        # captured_at is only as good as the clock, and away from a time source
        # the RTC clamp makes it 2010-01-01. boot_id + uptime_s order captures
        # correctly whatever the clock says.
        "boot_id": read("/proc/sys/kernel/random/boot_id"),
        "kernel": read("/proc/sys/kernel/osrelease"),
        "device_volume": read("/run/biscuit-audio/volume"),
        # The ALSA playback gain, which is NOT the same thing as the device
        # volume above and is the independent variable of the volume study.
        # pkgrel 41 set it to 127; the 2026-08-17 measurement was 80 vs 100. A
        # capture that does not record it cannot be compared to anything.
        "pcm_playback_volume": alsa_gain(),
        "muted": read("/run/biscuit-audio/muted"),
        "mic_profile": None,
        "cast_target": read("/opt/persist/btcast"),
        "loadavg": read("/proc/loadavg"),
    }
    try:
        env["mic_profile"] = json.loads(read("/run/biscuit-mic/profile.json", "{}"))
    except ValueError:
        pass
    env["packages"] = {}
    for pkg in ("device-amazon-biscuit", "linux-amazon-biscuit"):
        out = run("apk", "info", "-v", pkg).stdout.strip().split("\n")
        env["packages"][pkg] = out[0] if out and out[0] else None
    env["calibration"] = {
        "gains": sha256_of("/run/biscuit-miccal/gains"),
    }
    env["assets"] = {}
    for name in ("fireos6/AFE.cfg", "fireos6/coefs_FBFV2_LowLatency_8beams.cfg",
                 "fireos6/vad_lite.tflite", "biscuit-beam-weights.bin"):
        digest = sha256_of("/usr/share/biscuit/" + name)
        if digest:
            env["assets"][name] = digest
    return env


def live_chain():
    """The chain argv actually running, read BEFORE the hub is stopped.

    Hardcoding the production flags in the analyser is exactly how a replay
    silently stops being a replay. The shipping chain is assembled in
    biscuit-mic-stream.sh from settings and asset probes, and the first version
    of this harness ran biscuit-beamform at its built-in default gain of 0.25
    instead of production's -g 1 - a 12 dB error that showed up as a "level gap
    between the profiles" and was very nearly compensated for as if it were one.

    Recording what was running lets the analyser PROVE it replayed the same
    thing rather than assert it.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/" + pid + "/cmdline", "rb") as handle:
                parts = handle.read().split(chr(0).encode())
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in parts if a]
        if argv and os.path.basename(argv[0]) in (
                "biscuit-beamform", "biscuit-mic-fireos6"):
            return argv
    return None


def hub(action):
    run("rc-service", HUB, action)


def started_services():
    """Every service OpenRC reports as started."""
    out = run("rc-status", "--servicelist").stdout
    return {line.split()[0] for line in out.splitlines()
            if line.split() and "started" in line}


def restore_services(before):
    """Start again whatever stopping the hub took down with it.

    `rc-service biscuit-mic-hub stop` also stops everything that NEEDS the hub
    - biscuit-mic-pump, biscuit-btcall and biscuit-direction - but `start`
    brings back only the hub. Found 2026-09-27: after the comparison sitting
    all three were still down, so the LED ring stopped following the talker
    and hands-free calls and the computer microphone could not have worked
    until a reboot. Every sitting since qual-0924 had done the same. Only what
    was running beforehand is started, so a service the owner paused stays
    paused; each is re-checked first because starting one starts its needs.
    """
    for svc in sorted(before - started_services()):
        if run("rc-service", svc, "status").returncode == 0:
            continue
        r = run("rc-service", svc, "start")
        print("  restarted %s%s" % (svc, "" if r.returncode == 0 else
                                    " - FAILED: %s" % (r.stderr or r.stdout).strip()[:80]),
              flush=True)


# A detection fires when the word ENDS: reaction to the cue plus the word plus
# the model's own latency, roughly 0.8-1.9 s after the cue appears. So the cue
# goes out CUE_LEAD_S before speak_at_s and the window is centred on where the
# detection should land: anything from 0.25 s before the cue to 2.25 s after it
# is a hit. That is narrow enough for prompts 3 s apart (2 * WINDOW_S = 2.5 s,
# leaving a 0.5 s gap), which an owner can keep up with; +/-2 s windows needed
# 4 s and made a 50-prompt condition take twice as long as it has to.
WINDOW_S = 1.25     # a detection within +/- this of speak_at_s is a hit
CUE_LEAD_S = 1.0    # the cue precedes speak_at_s by this much


def capture(path, seconds, schedule=(), word=""):
    """Record, cueing each prompt LIVE while arecord runs.

    Printing the schedule up front and then blocking in arecord left the owner
    counting seconds in their head against 2 s windows. The cue is printed
    CUE_LEAD_S before speak_at_s, and the moment it actually went out is
    written back into the prompt as cued_at_s, so a late cue is visible in the
    metadata instead of looking like a missed detection.
    """
    proc = subprocess.Popen(
        ["arecord", "-D", CAPTURE_DEV, "-c", str(CHANNELS), "-f", FORMAT,
         "-r", str(RATE), "-d", str(int(seconds)), "-t", "raw", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    try:
        for p in schedule:
            due = t0 + p["speak_at_s"] - CUE_LEAD_S
            while proc.poll() is None and time.monotonic() < due:
                time.sleep(min(0.02, max(0.0, due - time.monotonic())))
            if proc.poll() is not None:
                break
            p["cued_at_s"] = round(time.monotonic() - t0, 3)
            print("  >>> say \"%s\" now   [%d/%d]" % (word, p["index"], len(schedule)),
                  flush=True)
        err = proc.communicate()[1]
    except BaseException:
        # Interrupted: stop recording now. Otherwise arecord keeps the capture
        # device until its own -d runs out, and the hub coming back in main's
        # `finally` cannot open it - measured, a SIGTERM 7 s into a 30 s take
        # left the tool waiting out the remaining 23 s.
        proc.terminate()
        proc.wait()
        raise
    return proc.returncode, (err or "").strip()


def prompt_schedule(seconds, count, every=None):
    """Evenly spaced windows, with a margin at each end.

    With `every`, the step is fixed at that many seconds and the count is
    whatever fits - for a RECORDED talker looped at a fixed interval, where the
    schedule has to match the loop rather than divide the capture evenly.

    Spaced rather than random so the owner can follow them, and margined so a
    detection near a boundary is not ambiguous about which side it fell.

    Neighbouring windows must not overlap. Each spans +/- WINDOW_S, so prompts
    closer than 2 * WINDOW_S apart would let one detection count as a hit for
    two prompts - the old limit allowed exactly that.
    """
    margin = 3.0
    usable = seconds - 2 * margin
    if every:
        if every < 2 * WINDOW_S:
            raise SystemExit("  --prompt-every %.1fs would overlap windows; "
                             "the minimum is %.1fs" % (every, 2 * WINDOW_S))
        count = int(usable // every)
        return [{"index": i + 1,
                 "speak_at_s": round(margin + every * i + every / 2, 2),
                 "window_s": WINDOW_S} for i in range(count)]
    if count <= 0:
        return []
    if usable <= 0 or count > usable / (2 * WINDOW_S):
        raise SystemExit("  %d prompts do not fit in %ds without overlapping"
                         " windows; allow %.1fs per prompt"
                         % (count, seconds, 2 * WINDOW_S))
    step = usable / count
    return [{"index": i + 1,
             "speak_at_s": round(margin + step * i + step / 2, 2),
             "window_s": WINDOW_S} for i in range(count)]


def main():
    ap = argparse.ArgumentParser(description="Capture the raw array for qualification.")
    ap.add_argument("--session", required=True, help="groups captures from one sitting")
    ap.add_argument("--condition", required=True,
                    help="near-quiet, far-noisy, interferer, playback-bt, ...")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--prompts", type=int, default=0,
                    help="how many wake-word utterances to ask for; 0 = a negative capture")
    ap.add_argument("--prompt-every", type=float, metavar="SECONDS",
                    help="a prompt every SECONDS instead of --prompts spread evenly: for a "
                         "recorded talker on a fixed loop - start the loop on the first cue")
    ap.add_argument("--wake-word", default="okay nabu")
    ap.add_argument("--distance-cm", type=float, help="recorded, not measured")
    ap.add_argument("--note", default="", help="anything the numbers will not carry")
    ap.add_argument("--out", default=OUT_ROOT)
    args = ap.parse_args()

    root = pathlib.Path(args.out) / args.session
    root.mkdir(parents=True, exist_ok=True)
    stem = "%s_%s" % (args.condition, time.strftime("%H%M%S"))
    raw_path = root / (stem + ".raw")

    if args.prompt_every and args.prompts:
        raise SystemExit("  use --prompts or --prompt-every, not both")
    schedule = prompt_schedule(args.seconds, args.prompts, args.prompt_every)
    print("  condition : %s" % args.condition)
    print("  length    : %.0fs" % args.seconds)
    if schedule:
        print("  say \"%s\" at: %s" % (args.wake_word,
                                       ", ".join("%.1fs" % p["speak_at_s"] for p in schedule)))
    else:
        print("  NEGATIVE capture: do not say the wake word at all")
    print()

    # Refuse before recording rather than after. arecord stops at ENOSPC and
    # still exits 0, so the only sign would be `short: true` in the metadata,
    # discovered after the owner had already done the sitting.
    need = int(args.seconds) * RATE * CHANNELS * 3
    free = shutil.disk_usage(root).free
    if free < need + (8 << 20):
        raise SystemExit("  %s has %.0f MB free; this capture needs %.0f MB;"
                         " pass --out on a filesystem with room."
                         % (root, free / 1e6, need / 1e6))

    chain = live_chain()     # BEFORE the hub stops, or there is nothing to read
    running = started_services()
    # The likeliest interruption is not Ctrl-C but the ssh session dropping
    # (SIGHUP) or a stop (SIGTERM), and Python dies on those without running
    # `finally`. Turn both into an ordinary exit so the hub comes back.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    hub("stop")
    # Whatever happens from here - a Ctrl-C mid-take, arecord failing, an
    # exception - the hub and everything it took down come back. Before this,
    # an interrupted take left the whole microphone stopped: the assistant
    # deaf until somebody noticed.
    try:
        time.sleep(2.0)
        env = environment()          # after the hub stops, so it reflects the capture
        env["live_chain"] = chain
        print("  recording...%s" % ("  stay quiet except when prompted" if schedule else ""),
              flush=True)
        started = time.time()
        code, err = capture(raw_path, args.seconds, schedule, args.wake_word)
        finished = time.time()
        # biscuit-audio re-pins the codec gain to 127 on every volume change, and the
        # device volume can move under the capture (a button, Home Assistant, a
        # phone's AVRCP). Either change silently undoes the variable under test, so
        # read both again at the end and say so.
        end_gain = alsa_gain()
        end_volume = read("/run/biscuit-audio/volume")
    finally:
        hub("start")
        restore_services(running)

    if code != 0 or not raw_path.exists():
        print("  CAPTURE FAILED: %s" % (err or "arecord returned %d" % code))
        return 1

    size = raw_path.stat().st_size
    expected = int(args.seconds) * RATE * CHANNELS * 3
    meta = {
        "schema": "biscuit-qual-capture-v1",
        "session": args.session,
        "condition": args.condition,
        "raw": raw_path.name,
        "bytes": size,
        "expected_bytes": expected,
        "short": size < expected,
        "seconds": args.seconds,
        "channels": CHANNELS,
        "rate": RATE,
        "format": FORMAT,
        "wake_word": args.wake_word,
        "prompts": schedule,
        "negative": not schedule,
        "pcm_playback_volume_end": end_gain,
        "device_volume_end": end_volume,
        "playback_changed": (end_gain != env["pcm_playback_volume"] or
                             end_volume != env["device_volume"]),
        "distance_cm": args.distance_cm,
        "note": args.note,
        "started_epoch": started,
        "finished_epoch": finished,
        "sha256": sha256_of(raw_path),
        "raw_stats": raw_stats(raw_path),
        "environment": env,
    }
    (root / (stem + ".json")).write_text(json.dumps(meta, indent=2) + "\n")

    print("  captured %d bytes (%s)" % (size, "short!" if meta["short"] else "full length"))
    print("  wrote %s and its .json" % raw_path.name)
    if meta["short"]:
        print("  WARNING: short capture - the hub may not have released the device")
    if meta["playback_changed"]:
        print("  WARNING: playback volume changed during the capture (gain %s -> %s, "
              "volume %s -> %s) - void for any volume comparison"
              % (env["pcm_playback_volume"], end_gain, env["device_volume"], end_volume))
    return 0


if __name__ == "__main__":
    sys.exit(main())
