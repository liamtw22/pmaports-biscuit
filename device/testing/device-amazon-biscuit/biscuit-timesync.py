#!/usr/bin/env python3
"""Give chrony a time source that works where NTP does not.

WHY THIS EXISTS
---------------
chrony is already installed and enabled, and away from a permissive network it
is completely inert: `pool.ntp.org` returns NXDOMAIN, `chronyc sources` is
empty, and `/var/lib/chrony/chrony.drift` never gets written because chrony has
never completed a single measurement. So the clock free-runs, `rtcsync` never
fires (the kernel only syncs the RTC while chrony is *synchronised*), and after
a power cut the RTC's garbage becomes the system clock.

Stock does not have a cleverer clock. It has a *reachable* one: it syncs when it
has a network, writes the result back to the RTC, and its system counter is
accurate enough to free-run between syncs - measured at 0 s of error over 57.6
hours with no network at all. The gap here was never accuracy, it was never
having a source.

Home Assistant is reachable by definition on this device, and every HTTP
response carries a `Date` header. That is a second-resolution time source, which
is poor for NTP and entirely sufficient for a device whose failure mode is being
43 years wrong.

WHICH HOME ASSISTANT: THE ONE THAT IS CONNECTED
-----------------------------------------------
There is no configured address. Home Assistant connects TO this device, over
the ESPHome API (the voice assistant on 6053, the Bluetooth proxy on 6054), so
the far end of an established connection on either port is Home Assistant's
own address, found the same way the settings page finds it for its
"Assistant connected" line. Its web server is asked for the time on port 8123,
Home Assistant's default, over plain HTTP and then, for an instance set up with
its own certificate, over HTTPS without verifying the certificate: a clock that
is wrong is exactly what makes verification fail, and the header is taken on
the same trust as the plain-HTTP answer.

With no connection there is no source, and nothing is sent anywhere. Earlier
versions tried two fixed addresses for everyone instead - postmarketOS's USB
host address and the Windows hotspot gateway, where the developer's own Home
Assistant happened to live - so every device asked those addresses for the time
once a minute whether or not anything was there. A Home Assistant reached over
the USB cable is still found: it connects from 172.16.42.2, and that is the
peer this reads.

WHY A chrony REFERENCE CLOCK, NOT `date -s`
-------------------------------------------
The previous approach called `date -s` once at start-up from biscuit-ha-relay,
and only when the year was already implausible - so it escaped 2069 and then
never corrected anything again. Setting the clock behind chrony's back also
leaves chrony permanently unsynchronised, which is what keeps `rtcsync` off.

Feeding chrony instead means chrony stays the single authority:

  * it disciplines the clock smoothly rather than stepping it,
  * it learns the crystal's drift and writes `chrony.drift`, so the rate is
    corrected between samples and across restarts,
  * `rtcsync` starts working, so the kernel writes the RTC every 11 minutes -
    which is the stock-like behaviour that makes a reboot inherit a good time,
  * and a real NTP server, if one ever becomes reachable, simply wins on merit:
    chrony picks the better source, and this one is declared with an honest
    one-second precision so it loses to anything better.

BOOTSTRAP IS SEPARATE, ON PURPOSE
---------------------------------
A refclock sample carrying a 43-year offset is not something to rely on chrony
accepting. If the year is implausible this steps the clock directly first, to
get within a second or two, and only then starts feeding samples. After the
kernel RTC clamp (0117-mt8163-rtc-clamp-implausible-time.patch) the worst case
is 2010 rather than 2069, but the bootstrap costs nothing and covers a kernel
that predates it.
"""

import email.utils
import http.client
import logging
import socket
import ssl
import struct
import subprocess
import sys
import time

# The ESPHome API ports Home Assistant connects to: linux-voice-assistant's and
# biscuit-btproxy's (biscuit_system.VOICE_PORT and BTPROXY_PORT). The voice
# assistant's peer is asked first; either is Home Assistant.
HA_API_PORTS = (6053, 6054)
# Home Assistant's own web server, where the Date header comes from.
HA_HTTP_PORT = 8123
TCP_ESTABLISHED = "01"

# chronyd creates and listens on this; we only ever send to it. The path is
# matched by the refclock line in /etc/chrony/conf.d/50-biscuit-httpdate.conf.
SOCK_PATH = "/run/chrony/httpdate.sock"

# A RANGE, not a floor. The board's bogus year is 2069, which sails past any
# "is it at least 2020" check - an earlier version did exactly that and silently
# corrected nothing.
#
# The same range decides which SAMPLES are believed, not only whether the clock
# needs stepping. The source is whoever holds a connection to an ESPHome API
# port, and those ports are unauthenticated, so any host on the network can be
# "Home Assistant" here. Without a check, one that answered with any date at all
# was stepped into the clock and written to the RTC while the clock was bogus.
# It can still pick a date inside the range - that is what the range cannot
# know - but not keep the device years away from the truth.
#
# The floor is the year the device package was built, from apk's own record of
# it (the t: field), and never earlier than FLOOR_YEAR: a clock that reads
# earlier than the software it is running is wrong by definition. The ceiling is
# YEARS_AHEAD past the floor, which moves on with every update.
FLOOR_YEAR = 2026
YEARS_AHEAD = 10
PACKAGE = "device-amazon-biscuit"
APK_DB = ("/lib/apk/db/installed", "/usr/lib/apk/db/installed")


def package_build_year():
    """The year PACKAGE was built, from apk's database, or None."""
    for path in APK_DB:
        try:
            with open(path) as f:
                pkg = None
                for line in f:
                    if line.startswith("P:"):
                        pkg = line[2:].strip()
                    elif pkg == PACKAGE and line.startswith("t:"):
                        return time.gmtime(int(line[2:].strip())).tm_year
        except (OSError, ValueError, OverflowError):
            continue
    return None


def plausible_years():
    floor = max(FLOOR_YEAR, package_build_year() or FLOOR_YEAR)
    return (floor, floor + YEARS_AHEAD)


PLAUSIBLE_YEARS = plausible_years()


def year_plausible(year):
    return PLAUSIBLE_YEARS[0] <= year <= PLAUSIBLE_YEARS[1]

SAMPLE_INTERVAL_S = 64          # matches the refclock's poll
HTTP_TIMEOUT_S = 5
# The round trip bounds how wrong the midpoint estimate can be: the Date header
# is stamped somewhere in [t0, t1], so taking the midpoint costs at most RTT/2.
#
# NOT 1.0, which was the first value and rejected every real sample. Home
# Assistant renders its frontend for a HEAD on /, and that measured 1.123 s on
# a USB link that pings in 0.8 ms - so this bounds Home Assistant's own latency,
# not the network's. 2.5 s keeps the worst-case midpoint error near a second,
# which is the same order as the header's own one-second truncation and far
# inside what this device needs.
MAX_RTT_S = 2.5

# struct sock_sample from chrony's refclock_sock.c:
#     struct timeval tv;  double offset;  int pulse, leap, _pad, magic;
# On this LP64 target that is 8+8 +8 +4+4+4+4 = 40 bytes.
SAMPLE_FMT = "<qqdiiii"
SOCK_MAGIC = 0x534F434B         # 'SOCK'

log = logging.getLogger("biscuit-timesync")

# Overridable only so a test can point it at a copy.
PROC_NET_TCP = (("/proc/net/tcp", False), ("/proc/net/tcp6", True))


def _proc_ip(hexaddr, v6):
    """An address as /proc/net/tcp{,6} prints it, as text."""
    raw = bytes.fromhex(hexaddr)
    if not v6:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # /proc stores each 32-bit word little-endian.
    addr = struct.pack(">4I", *struct.unpack("<4I", raw))
    if addr[:12] == b"\0" * 10 + b"\xff\xff":           # v4-mapped
        return socket.inet_ntop(socket.AF_INET, addr[12:])
    return socket.inet_ntop(socket.AF_INET6, addr)


def ha_peers():
    """Home Assistant's addresses: the far end of every established connection
    to one of this device's ESPHome API ports, voice assistant first.

    Link-local IPv6 peers are left out: /proc does not say which interface
    they are on, and without that they cannot be dialled back.
    """
    found = {port: [] for port in HA_API_PORTS}
    for path, v6 in PROC_NET_TCP:
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != TCP_ESTABLISHED:
                        continue
                    (_lip, lport), (rip, _rport) = (p.split(":") for p in parts[1:3])
                    lport = int(lport, 16)
                    if lport not in found:
                        continue
                    ip = _proc_ip(rip, v6)
                    if ip.lower().startswith("fe80:"):
                        continue
                    if ip not in found[lport]:
                        found[lport].append(ip)
        except (OSError, ValueError, StopIteration):
            continue
    peers = []
    for port in HA_API_PORTS:
        peers.extend(ip for ip in found[port] if ip not in peers)
    return peers


def _date_header(host, secure):
    """(Date header, t0, t1) from one HEAD request, or None."""
    if secure:
        # Unverified on purpose; see the module docstring.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(host, HA_HTTP_PORT,
                                           timeout=HTTP_TIMEOUT_S, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, HA_HTTP_PORT, timeout=HTTP_TIMEOUT_S)
    try:
        t0 = time.time()
        conn.request("HEAD", "/")
        header = conn.getresponse().getheader("Date")
        t1 = time.time()
    finally:
        conn.close()
    return header, t0, t1


# Hosts whose last answer was outside PLAUSIBLE_YEARS: said once, not every
# round, and said again only after the host has answered sensibly in between.
_implausible_hosts = set()


def http_date_sample():
    """One time sample from a Home Assistant Date header.

    Returns (local_reference_time, true_time, rtt) or None, and None without
    sending anything when Home Assistant is not connected. A Date outside
    PLAUSIBLE_YEARS is not a sample: that host is passed over for the next one.

    `Date` is truncated to the second, so the true instant is uniformly
    distributed in [Date, Date+1) - hence the half-second added back. The local
    reference is the midpoint of the request, which is the best estimate of when
    the server read its own clock.
    """
    for host in ha_peers():
        for secure in (False, True):
            scheme = "https" if secure else "http"
            try:
                header, t0, t1 = _date_header(host, secure)
            except Exception as err:                  # noqa: BLE001
                log.debug("%s://%s:%d unusable (%s)", scheme, host, HA_HTTP_PORT, err)
                continue
            if not header:
                continue
            rtt = t1 - t0
            if rtt > MAX_RTT_S:
                log.debug("%s://%s:%d round trip %.3fs too slow to use",
                          scheme, host, HA_HTTP_PORT, rtt)
                continue
            try:
                when = email.utils.parsedate_to_datetime(header)
            except (TypeError, ValueError) as err:
                log.debug("%s://%s:%d bad Date %r (%s)", scheme, host,
                          HA_HTTP_PORT, header, err)
                continue
            true_time = when.timestamp() + 0.5
            try:
                year = time.gmtime(true_time).tm_year
            except (OverflowError, OSError, ValueError):
                year = 0
            if not year_plausible(year):
                if host not in _implausible_hosts:
                    _implausible_hosts.add(host)
                    log.warning("ignoring the time from %s: its Date header %r "
                                "is outside %d-%d", host, header,
                                PLAUSIBLE_YEARS[0], PLAUSIBLE_YEARS[1])
                break                   # the other scheme is the same server
            _implausible_hosts.discard(host)
            return (t0 + t1) / 2.0, true_time, rtt
    return None


def send_sample(local, true_time):
    """Hand one sample to chronyd. False if it is not listening yet."""
    offset = true_time - local
    payload = struct.pack(SAMPLE_FMT,
                          int(local), int((local % 1) * 1e6),
                          offset, 0, 0, 0, SOCK_MAGIC)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.connect(SOCK_PATH)
            s.send(payload)
            return True
        finally:
            s.close()
    except OSError as err:
        log.debug("chrony socket %s unavailable (%s)", SOCK_PATH, err)
        return False


def clock_implausible():
    return not year_plausible(time.gmtime().tm_year)


def bootstrap(sample):
    """Step the clock directly to a sample, for a clock whose year is
    obviously wrong.

    Only ever escapes a bogus year. Once the clock is roughly right this is
    never called again and chrony owns every further correction, which is what
    keeps the two from fighting.

    Tried on every round until it succeeds, not once at start-up: the source is
    whichever Home Assistant is connected, and at boot Home Assistant connects
    some time after this service starts.

    The sample's own year is checked again here, whatever supplied it: this
    writes the RTC, so it is the one place a bad sample would outlive a reboot.
    """
    year = time.gmtime().tm_year
    _local, true_time, _rtt = sample
    try:
        sample_year = time.gmtime(true_time).tm_year
    except (OverflowError, OSError, ValueError):
        sample_year = 0
    if not year_plausible(sample_year):
        log.warning("not stepping the clock to a time in %d", sample_year)
        return False
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(true_time))
    if subprocess.call(["date", "-u", "-s", stamp],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL) != 0:
        log.warning("could not step the clock to %s UTC", stamp)
        return False
    # Write it through immediately rather than waiting for rtcsync's 11-minute
    # cycle: the whole point of the bootstrap is that the next boot should not
    # start from garbage again.
    subprocess.call(["hwclock", "-w"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("bootstrapped clock from %d to %s UTC", year, stamp)
    return True


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    warned_socket = False
    warned_year = False
    feeding = False             # are we currently delivering samples?
    while True:
        sample = http_date_sample()
        if sample is None:
            # Normal whenever Home Assistant is not connected. Say so once on
            # the way down rather than every minute.
            if feeding:
                log.info("Home Assistant not connected or not answering; "
                         "stopped feeding chrony")
                feeding = False
            if clock_implausible() and not warned_year:
                log.warning("clock reads %d; waiting for Home Assistant to "
                            "connect to correct it", time.gmtime().tm_year)
                warned_year = True
            log.debug("no time source this round")
        elif clock_implausible():
            # A sample 16 or 43 years off is not something to hand chrony.
            # Step first; the next round feeds from a sane clock.
            bootstrap(sample)
        else:
            local, true_time, rtt = sample
            if send_sample(local, true_time):
                warned_socket = False
                # An INFO line on each transition, DEBUG for the steady state.
                # A service whose success is silent is indistinguishable from
                # one that is quietly broken, which is how the previous
                # start-up-only clock fix went unnoticed for so long.
                if not feeding:
                    log.info("feeding chrony from HTTP Date: offset %+.3fs, "
                             "round trip %.3fs", true_time - local, rtt)
                    feeding = True
                else:
                    log.debug("sample: offset %+.3fs rtt %.3fs",
                              true_time - local, rtt)
            elif not warned_socket:
                # Once, not every round: chronyd may simply not be up yet.
                log.warning("chronyd is not listening on %s; is the refclock "
                            "drop-in installed?", SOCK_PATH)
                warned_socket = True
                feeding = False
        time.sleep(SAMPLE_INTERVAL_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
