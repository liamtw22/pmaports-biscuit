#!/bin/sh
# Restart sendspin when it has lost Music Assistant (2.31).
#
# Music Assistant holds a connection to sendspin the whole time it knows the
# player, playing or idle - measured 2026-09-26: one ESTABLISHED socket with
# nothing playing. Since r280 sendspin normally connects OUT to the server it
# found over mDNS, and retries by itself (backing off to 300 s), so this is
# the backstop behind that:
#   - the daemon's reconnect loop exits on an unexpected error, which leaves
#     the service crashed - a crashed sendspin is restarted like a lost one;
#   - if Music Assistant moves to another address, the old URL fails for ever,
#     and a restart finds the new one.
# Without a known server sendspin listens on its port instead, and this is the
# original fix: Music Assistant did not come back by itself after dropping the
# player (twice on 2026-09-21), and a restart re-announces over mDNS. On
# 2026-09-27 that was not enough - six restarts over eight hours, and only
# restarting Music Assistant brought it back - which is why connecting out is
# now the default.
#
# So: no connection to Music Assistant for GRACE seconds means it has been
# lost, and sendspin is restarted. An idle player is not affected - the
# connection stays up. If Music Assistant is gone for good (away from home, or
# not installed) the wait doubles after every restart, up to once an hour, so
# this never churns. Nothing is streaming when there is no connection, so a
# restart interrupts nothing. A sendspin stopped on purpose is left alone.

PORT=${SENDSPIN_PORT:-8928}
POLL=${SENDSPIN_WATCHDOG_POLL:-15}
GRACE=${SENDSPIN_WATCHDOG_GRACE:-120}
MAX=${SENDSPIN_WATCHDOG_MAX:-3600}
URL_FILE=/run/biscuit-sendspin/server-url
HEX=$(printf '%04X' "$PORT")

log() {
	echo "$(date '+%F %T') biscuit-sendspin-watchdog: $*"
}

# ESTABLISHED (state 01) sockets, IPv4 and 6, whose LOCAL port is sendspin's
# listening port or, when sendspin connects out, whose REMOTE port is the
# server's.
controllers() {
	sport=$(sed -n 's|^ws://[^/]*:\([0-9][0-9]*\)/.*|\1|p' "$URL_FILE" 2>/dev/null)
	shex=$([ -n "$sport" ] && printf '%04X' "$sport")
	awk -v p=":$HEX" -v s="${shex:+:$shex}" '$4 == "01" &&
		(substr($2, length($2) - 4) == p || (s != "" && substr($3, length($3) - 4) == s)) { n++ }
		END { print n + 0 }' /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

wait_for=$GRACE
idle=0
log "watching sendspin: restart after ${GRACE}s without Music Assistant, backing off to ${MAX}s"
while :; do
	sleep "$POLL"
	# 2>&1, not 2>/dev/null: OpenRC prints "started" on stdout but
	# "crashed" on stderr (exit 32), so discarding stderr hid every crash -
	# a killed sendspin sat crashed for four minutes in r280's test.
	case "$(rc-service biscuit-sendspin status 2>&1)" in
	*started*|*crashed*) ;;
	*) idle=0; continue ;;
	esac
	if [ "$(controllers)" -gt 0 ]; then
		[ "$wait_for" -gt "$GRACE" ] && log "Music Assistant is connected again"
		idle=0
		wait_for=$GRACE
		continue
	fi
	idle=$((idle + POLL))
	[ "$idle" -lt "$wait_for" ] && continue
	log "no Music Assistant connection for ${idle}s: restarting biscuit-sendspin"
	rc-service biscuit-sendspin restart >/dev/null 2>&1 || log "the restart failed"
	idle=0
	wait_for=$((wait_for * 2))
	[ "$wait_for" -gt "$MAX" ] && wait_for=$MAX
done
