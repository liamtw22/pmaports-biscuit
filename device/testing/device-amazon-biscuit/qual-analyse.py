#!/usr/bin/env python3
"""Replay qualification captures through both mic profiles and score them.

Runs ON the device, after a sitting. Reads what qual-capture.py wrote and, for
every capture, runs the raw 8-channel audio through each profile and scores the
result with every wake-word model. Nothing here needs the owner.

WHAT MAKES THE COMPARISON HONEST
--------------------------------
Both profiles see the SAME BYTES. 3.13 asks for matched conditions across two
profiles; replaying one capture is stronger than matching, because there is no
condition left to differ. The cost is that this measures the CHAINS, not the
whole live system - anything that depends on timing, on the hub, or on the
assistant's own behaviour is not in scope here and belongs to the live half of
the sitting.

The model is scored on what it hears in production: the chain's output after
the assistant's own wake-input conditioning (WakeInputAgc), taken from the
installed code with the service's flags. microWakeWord's score depends strongly
on absolute level, and on the device the AGC is what sets that level. Scoring
the raw chain output, as this file did until 2026-09-25, measured a level
nobody ships. Chain levels are still reported, for information.

INTENT COMES FROM THE PROMPTS, NEVER FROM STT (3.5)
---------------------------------------------------
A detection inside a prompted window is a true positive. A detection outside
every prompted window, or anywhere in a capture recorded as negative, is a false
accept. That label is independent of what any transcript said, which is the whole
reason 3.5 is still open.

    qual-analyse.py --session s1
    qual-analyse.py --session s1 --json report.json
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import math
import os
import pathlib
import shlex
import struct
import subprocess
import sys

RATE = 16000
LVA = "/usr/lib/linux-voice-assistant"
SITE = LVA + "/lib/python3.14/site-packages"
TFLITE_LIB = "/usr/lib/biscuit-tflite/libtensorflowlite_c.so"
TFLITE_DIR = "/usr/lib/biscuit-tflite"
MODELS = SITE + "/pymicro_wakeword/models"
CAL = "/run/biscuit-miccal/gains"
LVA_MAIN = SITE + "/linux_voice_assistant/__main__.py"
VA_SERVICE = "/etc/init.d/biscuit-voice-assistant"

ASSETS = "/usr/share/biscuit/fireos6"
TFLITE = "/usr/lib/biscuit-tflite"

# THE SHIPPING FLAGS, NOT THE BINARIES' DEFAULTS.
#
# These must match what biscuit-mic-stream.sh execs, because a replay that runs
# the chain differently from production is measuring something nobody ships.
# The first version of this file ran biscuit-beamform as `-w WEIGHTS -q` and
# biscuit-mic-fireos6 without --gain, which meant:
#
#   * biscuit-beamform fell back to its built-in `gain = 0.25f` instead of
#     production's `-g 1` - exactly 20*log10(4) = 12.04 dB quiet - and ran with
#     the echo canceller (-a) and adaptive beamformer (-A) OFF;
#   * biscuit-mic-fireos6 ran without the adaptive stage, the DNN gate and
#     `--gain 13`.
#
# The two errors did not cancel. They produced a 14.5 dB difference between the
# profiles that read exactly like a property of the chains, and the level-gap
# warning below faithfully reported it as one. The harness was the defect.
#
# qual-capture.py records the argv of whichever chain was actually running;
# check_against_live() below compares these to it and says so when they differ.
PROFILES = {
    "pmos": ["biscuit-beamform",
             "-w", "/usr/share/biscuit/biscuit-beam-weights.bin",
             "-a", "-A", "-g", "1", "-q", "-c", "8"],
    # The 8-beam chain: the same binary as fireos6, on its own open
    # coefficients (biscuit-mic-stream.sh run_pmos8).
    "pmos8": ["biscuit-mic-fireos6", "--allow-unqualified", "--open-coefficients",
              "--no-aec", "--adaptive", "--adapt-mask", "255", "--clean-on-echo",
              "--merge-all", "--gain", "13"],
    "fireos6": ["biscuit-mic-fireos6", "--allow-unqualified", "--assets", ASSETS,
                "--adaptive", "--adapt-mask", "255",
                "--dnn-vad", ASSETS + "/vad_lite.tflite",
                "--dnn-lib", TFLITE, "--dnn-threshold", "0.50",
                "--freeze-on-speech", "--no-aec",
                "--gain", "13"],
}

# Calibration is appended per profile under its own flag name, matching
# biscuit-mic-stream.sh, which passes the same per-unit Q14 file to every
# generation so a cross-profile comparison is not confounded by calibration.
CAL_FLAG = {"pmos": "-k", "pmos8": "--cal", "fireos6": "--cal"}


# THE WAKE MODEL'S INPUT, CONDITIONED AS PRODUCTION CONDITIONS IT.
#
# microWakeWord never hears the chain's output as it leaves the chain. The
# assistant first passes it through WakeInputAgc (--wake-input-agc). That stage
# aims the peak at -12 dBFS with up to +36 dB of gain, stops before the room's
# noise passes -38 dBFS, and slews 1 dB/s up and 3 dB/s down, in 1024-sample
# blocks. Until 2026-09-25 this file scored the raw chain output. That was the
# second time it measured something nobody ships; the first is under PROFILES.
#
# - Scored raw, far field lost 6 of 50 prompts for pmOS and 4 for Fire OS 6
#   that the device catches.
# - The "level-matched" rows turned Fire OS 6 down 16 dB and scored it raw,
#   and reported a collapse to 17/50. The device never produces that
#   condition, because the AGC gives both chains their gain back. Those rows
#   are gone.
#
# Nothing here is copied from production:
# - The class comes from the INSTALLED __main__.py, lifted by AST so that none
#   of the assistant's own imports run.
# - Its settings are the service's own flags over the parser's defaults.
# A change to either reaches this replay without anyone editing this file.
WAKE_FLAGS = {"--wake-agc-target-dbfs": "target_dbfs",
              "--wake-agc-max-gain-db": "max_gain_db",
              "--wake-agc-noise-ceiling-dbfs": "noise_ceiling_dbfs"}


def service_args():
    """The assistant's argv tokens, from its service's command_args line."""
    for line in pathlib.Path(VA_SERVICE).read_text().splitlines():
        if line.startswith("command_args="):
            return shlex.split(shlex.split(line[len("command_args="):])[0])
    raise SystemExit("no command_args in %s" % VA_SERVICE)


def flag_value(tokens, flag):
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def wake_input():
    """How production conditions the model's input: (description, factory, block).

    factory() returns a fresh stage whose .process(bytes) returns bytes. It is
    None when production feeds the chain output to the model unchanged.
    """
    sys.path.insert(0, SITE)
    import numpy as np                                   # noqa: E402
    source = pathlib.Path(LVA_MAIN).read_bytes()
    tree = ast.parse(source)
    defaults = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant)):
            for kw in node.keywords:
                if kw.arg == "default":
                    try:
                        defaults[node.args[0].value] = ast.literal_eval(kw.value)
                    except ValueError:
                        pass
    tokens = service_args()

    def setting(flag, cast):
        value = flag_value(tokens, flag)
        return cast(value if value is not None else defaults[flag])

    block = setting("--audio-input-block-size", int)
    where = "%s (sha256 %s) and %s" % (LVA_MAIN, hashlib.sha256(source).hexdigest()[:12],
                                        VA_SERVICE)
    if "--processed-mic-profile" not in tokens:
        # Without it the assistant also applies the owner's WebRTC AGC and noise
        # suppression preferences, which this replay does not reproduce.
        raise SystemExit("the assistant is not in processed-mic mode; this replay "
                         "does not reproduce its WebRTC stage")
    if "--wake-input-agc" in tokens:
        node = next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "WakeInputAgc")
        ns = {"np": np, "math": math}
        exec(compile(ast.Module(body=[node], type_ignores=[]), LVA_MAIN, "exec"), ns)
        params = {key: setting(flag, float) for flag, key in WAKE_FLAGS.items()}
        text = "WakeInputAgc %s, %d-sample blocks, from %s" % (
            ", ".join("%s=%g" % kv for kv in sorted(params.items())), block, where)
        return text, (lambda: ns["WakeInputAgc"](**params)), block
    gain_db = setting("--wake-input-gain-db", float)
    if abs(gain_db) < 1e-9:
        return "none (chain output unchanged), from %s" % where, None, block

    class FixedGain:
        """The assistant's fixed wake-input gain: scale, round, clip."""
        def __init__(self):
            self.gain_db = gain_db

        def process(self, chunk):
            x = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
            y = np.clip(np.rint(x * 10.0 ** (gain_db / 20.0)), -32768.0, 32767.0)
            return y.astype("<i2").tobytes()
    return "fixed gain %+.1f dB, from %s" % (gain_db, where), FixedGain, block


def condition(pcm, factory, block):
    """Run one chain output through a FRESH wake-input stage, block by block.

    Fresh per capture: the AGC primes its gain on the first block, as it does
    when the assistant starts, and every capture opens on at least a second of
    room before the first prompt, so it primes on the room rather than on
    speech. Returns (pcm, gains): the gain in dB after each block, or an empty
    list when there is no stage.
    """
    if factory is None:
        return pcm, []
    stage, out, gains, step = factory(), [], [], block * 2
    for i in range(0, len(pcm), step):
        out.append(stage.process(pcm[i:i + step]))
        gains.append(stage.gain_db)
    return b"".join(out), gains


def check_against_live(meta):
    """Say plainly whether what we replay is what was running.

    Not fatal: a capture taken under one profile can only ever witness that
    profile's chain, and the other is still worth replaying. But an unreported
    divergence is how the 12 dB gain error survived a whole measurement round.
    """
    live = (meta.get("environment") or {}).get("live_chain")
    if not live:
        print("  no live chain recorded in this capture - cannot verify the")
        print("  replay matches production (re-capture with a current qual-capture.py)")
        return
    name = os.path.basename(live[0])
    for profile, cmd in PROFILES.items():
        if os.path.basename(cmd[0]) != name:
            continue
        # fireos6 and pmos8 share a binary; --open-coefficients tells them apart.
        if ("--open-coefficients" in cmd) != ("--open-coefficients" in live):
            continue
        ours = set(cmd[1:])
        theirs = set(a for a in live[1:])
        # the calibration path is added at run time, so exclude it either way
        for drop in ("-k", "--cal", CAL, "/run/biscuit-miccal/gains"):
            ours.discard(drop)
            theirs.discard(drop)
        if ours == theirs:
            print("  replay of %s matches the chain that was running" % profile)
        else:
            print("  WARNING: the %s replay does NOT match the running chain." % profile)
            print("    only in production : %s" % (" ".join(sorted(theirs - ours)) or "-"))
            print("    only in this replay: %s" % (" ".join(sorted(ours - theirs)) or "-"))
            print("    Detection is level-dependent, so a flag that changes gain")
            print("    invalidates the comparison. Fix PROFILES before trusting this.")
        return
    print("  live chain was %s, which is not one of the replayed profiles" % name)


def run_chain(profile, raw_bytes):
    """Pipe one capture through a chain. Returns (mono_pcm, stderr)."""
    cmd = list(PROFILES[profile])
    if os.path.exists(CAL):
        cmd += [CAL_FLAG[profile], CAL]
    env = dict(os.environ)
    # musl fixes the search path at process start, and libtensorflowlite_c.so
    # needs libfarmhash, libcpuinfo, libpthreadpool and several libabsl_* from
    # the same directory, none of which carry a SONAME. Without this the DNN
    # gate silently does not load and the chain quietly runs the energy VAD -
    # a different, separately qualified chain wearing the same name.
    env["LD_LIBRARY_PATH"] = TFLITE + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    proc = subprocess.run(cmd, input=raw_bytes, capture_output=True, env=env)
    return proc.stdout, (proc.stderr or b"").decode(errors="replace")


def rms_dbfs(pcm):
    n = len(pcm) // 2
    if not n:
        return None
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    acc = sum(float(v) * v for v in vals)
    r = math.sqrt(acc / n)
    return 20 * math.log10(r / 32768.0) if r > 0 else -float("inf")


def score(pcm, model_json):
    """Detections with their time offsets, using the shipped engine.

    process_streaming takes FEATURES, not audio - passing PCM raises
    "zero-dimensional arrays cannot be concatenated", which names nothing
    relevant. The features stage is mandatory.
    """
    sys.path.insert(0, SITE)
    # LD_LIBRARY_PATH is deliberately NOT set here. musl resolves DT_NEEDED at
    # process start, so assigning it now has no effect on a later dlopen - the
    # previous version did exactly that and died with "Error loading shared
    # library libtensorflow-lite.so ... (needed by libtensorflowlite_c.so)".
    # ensure_tflite_path() re-execs before we ever get here.
    # MicroWakeWordFeatures, not pymicro_features.MicroFrontend. This is how the
    # assistant itself drives it (__main__.py), and it matters: the frontend
    # hands back a bare 1-D array that process_streaming rejects with
    # "axis 1 is out of bounds for array of dimension 1", which names nothing
    # relevant. MicroWakeWordFeatures.process_streaming(audio) yields the framed
    # inputs the model actually wants, zero or more per chunk.
    from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures   # noqa: E402

    # from_config prints the model's config dict to stdout. Four models times two
    # profiles times every capture buries the report, so swallow just that.
    import contextlib, io as _io
    with contextlib.redirect_stdout(_io.StringIO()):
        mww = MicroWakeWord.from_config(model_json, TFLITE_LIB)
    feats = MicroWakeWordFeatures()
    hits = []
    CHUNK = 160 * 2                                     # 10 ms of S16 mono
    for i in range(0, len(pcm) - CHUNK, CHUNK):
        for frame in feats.process_streaming(pcm[i:i + CHUNK]):
            if mww.process_streaming(frame):
                hits.append(round(i / 2 / RATE, 2))
    return hits


EVENT_GAP_S = 0.25     # detections are ~30 ms frames; a longer gap is a new event


def events(hits):
    """Merge frame runs into events, each timed at its first frame.

    score() returns every ~30 ms frame the model held above threshold, so one
    spoken word is a run of 10-15 of them. The assistant fires once per word,
    at the first. Counting frames made one out-of-window word look like a dozen
    false accepts, and a run that spilled over a window edge count twice.
    """
    out, last = [], None
    for t in hits:
        if last is None or t - last > EVENT_GAP_S:
            out.append(t)
        last = t
    return out


def windows_of(meta):
    return [(p["speak_at_s"] - p["window_s"], p["speak_at_s"] + p["window_s"])
            for p in meta.get("prompts", [])]


def classify(hits, meta):
    """Split detection EVENTS into true positives and false accepts, by PROMPT."""
    windows = windows_of(meta)
    evs = events(hits)
    tp, fa = [], []
    for t in evs:
        if any(a <= t <= b for a, b in windows):
            tp.append(t)
        else:
            fa.append(t)
    matched = sum(1 for a, b in windows if any(a <= t <= b for t in evs))
    return {"detections": hits, "events": evs, "true_positives": tp,
            "false_accepts": fa, "prompts": len(windows),
            "prompts_detected": matched, "missed": len(windows) - matched}


def speech_rms_dbfs(pcm, meta):
    """Level inside the prompt windows - where the speech is - or None.

    Reported, not used in scoring. Whole-capture RMS in a quiet room is mostly
    noise floor, and the chains treat noise differently, so the level inside
    the prompt windows is the one worth reading.
    """
    spans = sorted(windows_of(meta))
    if not spans:
        return None
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    seg = b"".join(pcm[max(0, int(a * RATE)) * 2:max(0, int(b * RATE)) * 2]
                   for a, b in merged)
    return rms_dbfs(seg)


def ensure_tflite_path():
    """Re-exec with LD_LIBRARY_PATH set, because musl will not read it later.

    libtensorflowlite_c.so needs libtensorflow-lite.so, libfarmhash, libcpuinfo,
    libpthreadpool, libfft2d_* and several libabsl_* from the same directory,
    none of which carry a SONAME or sit on the default search path. musl fixes
    that path when the process starts, so os.environ is too late and ctypes
    fails at the first model load - after the chains have already been run.
    biscuit-mic-stream.sh solves this the same way, by exporting before exec.
    """
    if os.environ.get("BISCUIT_QUAL_REEXEC"):
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["BISCUIT_QUAL_REEXEC"] = "1"
    os.environ["LD_LIBRARY_PATH"] = TFLITE_DIR + (":" + current if current else "")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    ap = argparse.ArgumentParser(description="Score qualification captures offline.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--root", default="/var/lib/biscuit/captures")
    ap.add_argument("--models", nargs="*", help="default: every model present")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--also-chain-output", action="store_true",
                    help="also score the chain output WITHOUT the assistant's wake-input "
                         "stage (diagnostic; comparable with reports before 2026-09-25)")
    args = ap.parse_args()

    ensure_tflite_path()
    wake_text, wake_factory, wake_block = wake_input()

    root = pathlib.Path(args.root) / args.session
    metas = sorted(root.glob("*.json"))
    if not metas:
        raise SystemExit("no captures under %s" % root)

    models = args.models or sorted(
        p.stem for p in pathlib.Path(MODELS).glob("*.json"))
    report = {"session": args.session, "models": models, "wake_input": wake_text,
              "captures": []}

    print("  session %s: %d capture(s), models: %s"
          % (args.session, len(metas), ", ".join(models)))
    print("  model input: %s" % wake_text)
    print()

    for mpath in metas:
        meta = json.loads(mpath.read_text())
        raw = (root / meta["raw"]).read_bytes()
        entry = {"condition": meta["condition"], "raw": meta["raw"],
                 "negative": meta.get("negative"), "profiles": {}}
        print("  %-22s %.0fs  %s"
              % (meta["condition"], meta["seconds"],
                 "NEGATIVE" if meta.get("negative") else
                 "%d prompt(s)" % len(meta.get("prompts", []))))

        check_against_live(meta)

        for profile in PROFILES:
            pcm, err = run_chain(profile, raw)
            if not pcm:
                print("      %-8s chain produced nothing: %s" % (profile, err[:60]))
                continue
            heard, gains = condition(pcm, wake_factory, wake_block)
            variants = {"production": heard}
            if args.also_chain_output:
                variants["chain_output"] = pcm
            scored = {}
            for variant, x in variants.items():
                scored[variant] = {name: classify(score(x, "%s/%s.json" % (MODELS, name)), meta)
                                   for name in models}
            speech = speech_rms_dbfs(pcm, meta)
            whole = rms_dbfs(pcm)
            entry["profiles"][profile] = {
                "rms_dbfs": whole, "speech_rms_dbfs": speech,
                "wake_gain_db": ({"first": round(gains[0], 2), "min": round(min(gains), 2),
                                  "max": round(max(gains), 2), "last": round(gains[-1], 2)}
                                 if gains else None),
                "variants": scored}
            key = meta.get("wake_word", "").replace(" ", "_")
            level = speech if speech is not None else whole
            gain = ("  gain %+.1f..%+.1f dB" % (min(gains), max(gains))) if gains else ""
            for variant, per_model in scored.items():
                summary = per_model.get(key) or per_model[models[0]]
                print("      %-8s %-12s chain %6.1f dBFS%s  %d/%d prompts, %d false accept(s)"
                      % (profile, variant, level, gain if variant == "production" else "",
                         summary["prompts_detected"], summary["prompts"],
                         len(summary["false_accepts"])))

        report["captures"].append(entry)
        print()

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
        print("  wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
