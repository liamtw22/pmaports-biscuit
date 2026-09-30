#!/bin/sh
# Install or remove an optional package, in the background, with a status file.
#
#     biscuit-pkgjob.sh add <apk package>...
#     biscuit-pkgjob.sh del <apk package>...
#     biscuit-pkgjob.sh check <anything>
#     biscuit-pkgjob.sh update <anything>
#     biscuit-pkgjob.sh sysupdate [<apk package>...]
#     biscuit-pkgjob.sh boot | bootrollback
#
# check and update concern this device's own packages - device-amazon-biscuit,
# its subpackages and the kernel - and nothing else; see update_ours. boot
# writes the boot image the installed kernel and initramfs build to boot_a and
# boot_b, which no package can do (see biscuit-boot-update).
#
# WHY THIS IS NOT DONE INSIDE THE SETTINGS SERVER
#
# The voice bundle is about 116 MB and sendspin about 63 MB. `apk add` on this
# CPU over a domestic connection takes minutes, which is far longer than a
# browser will hold a request open - so :8080 starts this detached and polls the
# status file instead. Same shape as the Bluetooth pairing window.
#
# STARTING WHAT WAS INSTALLED
#
# The subpackages ship their own runlevel symlinks, so a reboot would start the
# new services on its own. Nobody should have to reboot after ticking a box, so
# the services the package provides are started here - discovered with
# `apk info -L`, which needs no per-package knowledge and cannot drift from what
# the package actually contains.
#
# On removal the order reverses: stop first, then delete. Deleting the files out
# from under a running service leaves a supervisor respawning a binary that is
# no longer there.
#
# TWO RECORDS, ONE LOCK
#
# `status` is every job's, and while it says running it is the lock: the
# settings page starts nothing else. It used to be the only record, so the
# daily update check, which starts a few minutes after setup, wrote over the
# failure of the install setup had started - and the Apps page, which shows only
# app jobs, went blank. The owner was left with two apps that looked as if
# nobody had ever asked for them. So an install or removal also writes
# `apps-status`, which only another install or removal replaces.
#
# BISCUIT_PKGJOB_ORIGIN says who started the job - "setup", or "retry" for the
# settings page's automatic retry of what setup could not install - and is
# recorded with it; anything else counts as the owner, on the settings page.
#
# ONE APK AT A TIME
#
# apk keeps a lock on its own database while it works, and an apk that finds
# the lock taken fails at once, fetching nothing: "Unable to lock database:
# Resource temporarily unavailable". The status file above keeps the settings
# page's own jobs apart, but nothing keeps this job apart from an apk it did not
# start - an owner's `apk upgrade` over SSH, or setup's install landing on a
# check that is still running. That failure used to be read as "no package
# server could be reached", and the owner was asked to check a network that was
# working. So every apk call here that takes the lock waits for it (apk's own
# --wait; installing an app on this CPU takes minutes, so up to ten of them),
# and a lock still held after that is reported as what it is: "busy".
#
# AN APP INSTALL NEVER MOVES THIS ECHO'S OWN SOFTWARE
#
# Each app depends on the core package at exactly its own release. Installed
# from a feed that carries another release, apk moves the core - and every
# other subpackage installed - to that release to satisfy it: forwards, an
# update nobody asked for, or backwards, undoing every fix since. It happened
# on 2026-09-28 ("Downgrading device-amazon-biscuit (6-r287 -> 6-r286)") when a
# zip was published before its feed. Since r295 the settings page retries an
# install by itself, possibly weeks after setup, when the feed is most likely
# to have moved on; and the add path never touched reboot-required, so the
# services ran old code on new files with no "Restart to finish". So apk is
# first asked what the install would do (--simulate), and one that would move
# our own packages is refused, whoever started it: updating is the Updates
# page's job, which does the bookkeeping. reason=version.

set -u

# Overridable only so a test harness can point them at copies.
: "${BISCUIT_PKGJOB_RUNDIR:=/run/biscuit-packages}"
: "${BISCUIT_PKGJOB_LOG:=/var/log/biscuit-pkgjob.log}"
: "${BISCUIT_APK_WORLD:=/etc/apk/world}"
: "${APK_REPOSITORIES:=/etc/apk/repositories}"
RUNDIR=$BISCUIT_PKGJOB_RUNDIR
STATUS=$RUNDIR/status
APPS_STATUS=$RUNDIR/apps-status
LOG=$BISCUIT_PKGJOB_LOG
WORLD=$BISCUIT_APK_WORLD

# This Echo's own package feed, as biscuit-persist writes its line into the
# repositories file (FEED_URL there). The apps and the device's own updates come
# only from it; everything else is postmarketOS's and Alpine's.
FEED_MATCH=/biscuit-apk/

# Seconds any apk call that changes the database waits for another apk's lock.
APK_WAIT=600

ACTION=${1:-}
shift 2>/dev/null || true
PACKAGES="$*"

case "${BISCUIT_PKGJOB_ORIGIN:-}" in
	setup|retry) ORIGIN=$BISCUIT_PKGJOB_ORIGIN ;;
	*) ORIGIN="" ;;
esac

log() {
	echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

# Written as key=value rather than JSON: this is shell, and a quoting mistake in
# hand-built JSON would be parsed by the settings page as a failure that never
# happened.
#
# reason= is a word the settings page can act on, not only print: "clock" means
# nothing was installed because the time is not known yet (the message says
# whether the network reached the internet meanwhile), so the automatic
# retry waits for the clock rather than for its usual interval, and "busy" that
# another apk had the database, so it tries again a minute later. The others
# are refresh_index's (local, feed, offline, partial, unknown), apk_failure's
# (space, missing, deps, feedoff, apk, and busy and local as for the index)
# and "version" (see moves_ours). time= is this device's clock, and reads 2010
# until it is set; the page leaves out a time that early.
status() {   # STATE MESSAGE FINISHED [REASON]
	mkdir -p "$RUNDIR"
	{
		printf 'action=%s\n' "$ACTION"
		printf 'packages=%s\n' "$PACKAGES"
		printf 'state=%s\n' "$1"
		printf 'message=%s\n' "$2"
		printf 'finished=%s\n' "$3"
		printf 'reason=%s\n' "${4:-}"
		printf 'origin=%s\n' "$ORIGIN"
		printf 'time=%s\n' "$(date +%s)"
	} > "$STATUS.tmp"
	# The app record first, so that by the time the lock says the job is over
	# its outcome is already there to read.
	case "$ACTION" in
	add|del) cp "$STATUS.tmp" "$APPS_STATUS.tmp" && mv "$APPS_STATUS.tmp" "$APPS_STATUS" ;;
	esac
	mv "$STATUS.tmp" "$STATUS"
}

case "$ACTION" in
	add|del|check|update) ;;
	sysupdate|boot|bootrollback) ;;
	*) echo "usage: $0 add|del|check|update|sysupdate|boot|bootrollback <package>..." >&2; exit 2 ;;
esac
# sysupdate with no packages means every system package; boot takes none.
case "$ACTION" in
	sysupdate|boot|bootrollback) ;;
	*) [ -n "$PACKAGES" ] || { echo "no packages given" >&2; exit 2; } ;;
esac

# Ours: the device package, its subpackages, and the kernel.
OURS_RE='^(device-amazon-biscuit|linux-amazon-biscuit)'

# The services the packages named provide, one per line. Empty for a package
# with none, and empty for a package that is not installed - which is why
# removal collects them BEFORE deleting anything.
services_of() {   # PACKAGE...
	for _p in "$@"; do
		apk info -L "$_p" 2>/dev/null | sed -n 's|^etc/init\.d/||p'
	done
}

# Packages come from the feed over HTTPS, and this Echo's RTC starts at
# 2010-01-01. Until chrony steps the clock every certificate is "not yet
# valid", and apk reports the packages as missing - which is what an install
# chosen during setup did, seconds after the device joined Wi-Fi. This script's
# own file carries the package's build time, so the clock is known to be wrong
# while it reads earlier than that. The settings page's retry uses the same
# test, on the same file.
CLOCK_FLOOR=$(stat -c %Y "$0" 2>/dev/null || echo 0)

clock_unset() {
	[ "$(date +%s)" -lt "$CLOCK_FLOOR" ]
}

# How long to wait for it. Setup hands over its install seconds after the Echo
# joins Wi-Fi, when chrony has often not had its first answer yet, so that job
# waits longer: an install that gives up on the clock alone shows the owner a
# failure for something that was about to sort itself out.
CLOCK_WAIT=120
[ "$ORIGIN" = setup ] && CLOCK_WAIT=300

wait_for_clock() {
	clock_unset || return 0
	log "clock reads $(date '+%Y-%m-%d'); waiting up to ${CLOCK_WAIT}s for time sync"
	status running "Waiting for the clock to be set" 0
	chronyc online >/dev/null 2>&1
	chronyc burst 4/4 >/dev/null 2>&1
	_waited=0
	while clock_unset && [ $_waited -lt $CLOCK_WAIT ]; do
		sleep 2
		_waited=$((_waited + 2))
	done
	if clock_unset; then
		log "clock still unset after ${_waited}s"
	else
		log "clock set after ${_waited}s"
	fi
	status running "" 0
}

# Where the time comes from, in words that are true in every state they are
# shown in. An internet time server (NTP), or Home Assistant's own clock - but
# biscuit-timesync finds Home Assistant only through a connection to this
# Echo's voice assistant (6053) or Bluetooth proxy (6054). While the voice
# assistant is the app waiting to be installed, and the proxy is off as it is
# by default, Home Assistant cannot be a source at all; "or from Home
# Assistant" alone offered the owner a fix that could not happen. So it says
# what it takes. A network with no internet at all is at least as likely a
# reason as one that blocks NTP, and refresh_index tells those apart.
CLOCK_HA="Home Assistant can set it too, while it is connected to this Echo's voice assistant or Bluetooth proxy (the proxy is turned on from the Home Assistant page)."
CLOCK_WHY="This Echo has no clock that keeps time while it is unplugged: it is set from an internet time server (NTP) once the network reaches one. $CLOCK_HA"

# Whether apk stopped because another apk had its database: "Resource
# temporarily unavailable" from an apk that did not wait, "Interrupted system
# call" from one whose --wait ran out (both apk-tools 3.0.8, checked against
# apk.static with the lock held). Not "Permission denied" or "Read-only file
# system", which say something about this Echo rather than about another apk:
# those are apk_db_failed's.
apk_busy() {   # OUTPUT
	grep -qiE 'unable to lock database: (resource temporarily unavailable|interrupted system call)' "$1"
}

apk_db_failed() {   # OUTPUT
	grep -qiE 'unable to lock database|failed to open apk database' "$1"
}

# True whatever was asked for: nothing was fetched, installed or removed.
BUSY_MSG="Another package operation was using this Echo's package database, so nothing was done."

# Refresh the package index, and when that fails, say which of the reasons it
# was. `apk update` fails when ANY repository does, so on its own it cannot
# tell "this Echo's feed is down" from "there is no internet" - and the one
# message it used to produce, "Check the network", was read over the network it
# blamed. apk-tools 3 prints a WARNING naming each repository it could not
# refresh, and "<description> [<url>]" for each one it has an index for (a
# stale one included, which is why a repository counts as answering only when
# no warning names it):
#
#     WARNING: updating and opening https://.../biscuit-apk/edge/aarch64/APKINDEX.tar.gz: HTTP 404: Not Found
#     v20260929-0-g1 [http://mirror.postmarketos.org/postmarketos/master]
#
# A warning names the index file under the repository, "<url>/<arch>/<file>:";
# apk-tools 2 named the repository itself, "<url>:". Either counts, and nothing
# longer: a repository whose URL begins with another's must not take the other's
# warning.
#
# Sets INDEX_WHY to one word and INDEX_REASON to apk's own reason, for
# index_message:
#     busy     another apk had the database, even after APK_WAIT
#     local    this Echo's own package database could not be opened
#     clock    the clock is not set, so no certificate can be checked; and
#              INDEX_NET says what the rest of the network did:
#                some  a repository that needs no certificate answered
#                none  nothing answered, and not over a certificate
#                ""    nothing to go on
#     feed     this Echo's feed failed and another repository answered
#     offline  no repository answered
#     partial  the feed answered (or is not configured); another did not
#     unknown  it failed, and the output says nothing more
#
# The first two come before anything else: apk then stopped before it asked
# any repository, so what it did not print about them means nothing.
#
# On an unset clock the feed cannot answer - it is HTTPS, and every
# certificate is "not yet valid" - but postmarketOS's and Alpine's mirrors are
# plain HTTP here, with indexes checked by signature, which has no clock in
# it. So whether they answered is what says if this network reaches the
# internet at all: "the clock is not set" alone left an owner on a network
# with no internet looking at their time settings.
refresh_index() {
	INDEX_WHY="" INDEX_REASON="" INDEX_NET=""
	apk --wait "$APK_WAIT" update > "$RUNDIR/update.out" 2>&1
	_rc=$?
	cat "$RUNDIR/update.out" >> "$LOG"
	[ $_rc -eq 0 ] && return 0
	# feed_warned feed_answered others_answered
	set -- $(awk -v feed="$FEED_MATCH" '
		function warned(u,   k, s, p, rest) {
			for (k = 1; k <= nb; k++) {
				s = bad[k]
				while ((p = index(s, u)) > 0) {
					rest = substr(s, p + length(u))
					if (rest ~ /^(:|\/[^\/[:space:]]+\/[^\/[:space:]]+:)/) return 1
					s = substr(s, p + 1)
				}
			}
			return 0
		}
		/^(WARNING|ERROR):/ { bad[++nb] = $0; if (index($0, feed)) fw = 1; next }
		$NF ~ /^\[.+\]$/ { url[++n] = substr($NF, 2, length($NF) - 2) }
		END {
			fa = 0; oa = 0
			for (i = 1; i <= n; i++) {
				if (warned(url[i])) continue
				if (index(url[i], feed)) fa = 1; else oa++
			}
			print fw + 0, fa, oa
		}' "$RUNDIR/update.out")
	_fw=${1:-0} _fa=${2:-0} _oa=${3:-0}
	# Warnings about anything but the feed. With none, a feed that failed while
	# nothing else answered says nothing about the network: there was nothing
	# else to ask.
	_ob=$(grep -E '^(WARNING|ERROR):' "$RUNDIR/update.out" | grep -vcF "$FEED_MATCH")
	# apk's reason: from the feed's own warning when it has one.
	_line=$(grep -E '^(WARNING|ERROR):' "$RUNDIR/update.out" | grep -F "$FEED_MATCH" | head -n1)
	[ -n "$_line" ] || _line=$(grep -E '^(WARNING|ERROR):' "$RUNDIR/update.out" | head -n1)
	INDEX_REASON=$(printf '%s' "$_line" | sed -E 's#^.*://[^[:space:]]*: ##; s#^(WARNING|ERROR): ##' | cut -c1-120)
	if apk_busy "$RUNDIR/update.out"; then
		INDEX_WHY=busy
	elif apk_db_failed "$RUNDIR/update.out"; then
		INDEX_WHY=local
	elif clock_unset; then
		INDEX_WHY=clock
		# Failures of the other repositories that are not certificate ones
		# (apk-tools 3 prefixes those "TLS:"): the network itself.
		_onet=$(grep -E '^(WARNING|ERROR):' "$RUNDIR/update.out" | grep -vF "$FEED_MATCH" | grep -vc 'TLS:')
		if [ "$_oa" -gt 0 ]; then
			INDEX_NET=some
		elif [ "$_fa" = 0 ] && [ "${_onet:-0}" -gt 0 ]; then
			INDEX_NET=none
		fi
	elif [ "$_fw" = 1 ] && [ "$_oa" -gt 0 ]; then
		INDEX_WHY=feed
	elif [ "$_fa" = 0 ] && [ "$_oa" = 0 ] && [ "${_ob:-0}" -gt 0 ]; then
		INDEX_WHY=offline
	elif [ "$_fw" = 0 ] && [ "${_ob:-0}" -gt 0 ]; then
		INDEX_WHY=partial
	else
		INDEX_WHY=unknown
	fi
	log "apk update failed: $INDEX_WHY${INDEX_REASON:+ ($INDEX_REASON)}"
	return 1
}

# What refresh_index found, for the owner. Only "offline" asks them to look at
# their network: every other case is read over a network that works.
index_message() {
	_r=${INDEX_REASON:+ ($INDEX_REASON)}
	case $INDEX_WHY in
	busy) echo "$BUSY_MSG" ;;
	local) echo "This Echo's package database could not be opened$_r." ;;
	clock)
		case $INDEX_NET in
		none) echo "No package server could be reached$_r, and the clock is not set yet. Check that the Wi-Fi network this Echo is on is connected to the internet: the clock is set from there too." ;;
		some) echo "The clock is not set yet, so the package feed's security certificate cannot be checked. Other package servers answered, so the network reaches the internet, but no internet time server (NTP) has answered: the network may be blocking NTP. $CLOCK_HA" ;;
		*) echo "The clock is not set yet, so the package feed's security certificate cannot be checked. $CLOCK_WHY" ;;
		esac ;;
	feed) echo "This Echo's package feed is not available at the moment$_r. Other package servers answered, so the network is working." ;;
	offline) echo "No package server could be reached$_r. Check that the Wi-Fi network this Echo is on is connected to the internet." ;;
	partial) echo "Some package servers could not be reached$_r." ;;
	*) echo "The package lists could not be updated$_r." ;;
	esac
}

# Whether the repositories file has an active line for the feed. The owner can
# turn it off for good (/opt/persist/update-feed; see biscuit-persist), and then
# no app can be downloaded at all.
feed_configured() {
	grep -v '^[[:space:]]*#' "$APK_REPOSITORIES" 2>/dev/null | grep -qF "$FEED_MATCH"
}

# Why `apk add` or `apk del` failed, from its output in $1: APK_MSG for the
# owner, APK_WHY as one word for the settings page. apk's own first ERROR line
# rather than a pointer to the log, which nothing on the settings page can open.
apk_failure() {   # OUTPUT installation|removal
	_it=it
	[ "$(echo $PACKAGES | wc -w)" -gt 1 ] && _it=them
	if apk_busy "$1"; then
		APK_WHY=busy
		APK_MSG=$BUSY_MSG
	elif apk_db_failed "$1"; then
		_why=$(grep -m1 '^ERROR:' "$1" | sed 's/^ERROR: //' | cut -c1-160)
		APK_WHY=local
		APK_MSG="This Echo's package database could not be opened${_why:+ ($_why)}."
	elif grep -q 'No space left on device' "$1"; then
		APK_WHY=space
		APK_MSG="There is not enough free storage for $_it. The Storage page can clear the download cache."
	elif [ "$2" = installation ] && grep -qE 'unable to select packages|no such package' "$1"; then
		# apk names each package it could not find, and what asked for it:
		#
		#     device-amazon-biscuit-sendspin (no such package):
		#       required by: world[device-amazon-biscuit-sendspin]
		#     ffmpeg-libs (no such package):
		#       required by: device-amazon-biscuit-sendspin-6-r295[ffmpeg-libs]
		#
		# One of the packages asked for, missing, is the feed's doing. A
		# package one of them depends on, missing, is postmarketOS's or
		# Alpine's: "the feed does not offer it" would be false - the feed
		# offered it - so that is said as it is.
		_gone=$(sed -n 's/^[[:space:]]*\([^[:space:]]*\) (no such package):.*$/\1/p' "$1")
		_ours="" _deps=""
		for _g in $_gone; do
			case " $(echo $PACKAGES) " in
			*" $_g "*) _ours="$_ours $_g" ;;
			*) _deps="$_deps $_g" ;;
			esac
		done
		_need=needs
		[ "$_it" = them ] && _need=need
		if [ -n "$_ours" ] && ! feed_configured; then
			APK_WHY=feedoff
			APK_MSG="This Echo's package feed is turned off (/opt/persist/update-feed), and the apps come only from it."
		elif [ -n "$_ours" ]; then
			APK_WHY=missing
			# Only some of those asked for: name them, "them" would say all.
			_what=$_it
			[ "$(echo $_ours | wc -w)" -lt "$(echo $PACKAGES | wc -w)" ] && _what=$(echo $_ours)
			APK_MSG="This Echo's package feed does not offer $_what at the moment."
			[ -n "$_deps" ] && APK_MSG="$APK_MSG Something else they need ($(echo $_deps)) is not offered either."
		elif [ -n "$_deps" ]; then
			APK_WHY=deps
			APK_MSG="Something $_it $_need ($(echo $_deps)) is not offered by the package servers this Echo uses at the moment."
		else
			# Selected, but not together: a conflict ("breaks:").
			_why=$(grep -m1 -E '^[[:space:]]+(breaks|conflicts):' "$1" | sed 's/^[[:space:]]*//' | cut -c1-120)
			APK_WHY=apk
			APK_MSG="The installation did not complete: apk could not select the packages${_why:+ ($_why)}."
		fi
	else
		_why=$(grep -m1 '^ERROR:' "$1" | sed 's/^ERROR: //' | cut -c1-160)
		APK_WHY=apk
		APK_MSG="The $2 did not complete${_why:+: $_why}."
	fi
}

# Whether apk's output in $1 moves one of this Echo's own packages to another
# release, as apk-tools 3 prints it for each package (checked against
# apk.static, with --simulate and without):
#
#     (1/2) Downgrading device-amazon-biscuit (6-r10 -> 6-r9)
#
# and if so VER_MSG says which way for the owner, with the releases as the
# pages name them, and VER_UP=1 when it is forwards.
moves_ours() {   # OUTPUT
	_mv=$(grep -E '^\([0-9]+/[0-9]+\) (Upgrading|Downgrading) (device|linux)-amazon-biscuit' "$1")
	[ -n "$_mv" ] || return 1
	_core=$(printf '%s\n' "$_mv" | grep -E ' device-amazon-biscuit \(' | head -n1)
	_from="" _to=""
	if [ -n "$_core" ]; then
		_from=$(printf '%s' "$_core" | sed -E 's/.*\(([^ ]+) -> ([^)]+)\).*/\1/; s/^.*-(r[0-9]+)$/\1/')
		_to=$(printf '%s' "$_core" | sed -E 's/.*\(([^ ]+) -> ([^)]+)\).*/\2/; s/^.*-(r[0-9]+)$/\1/')
	fi
	VER_UP=""
	if printf '%s\n' "$_mv" | grep -q ') Downgrading '; then
		if [ -n "$_from" ]; then
			VER_MSG="The package feed has an older release ($_to) than this Echo runs ($_from), and installing from it would take this Echo back to that release. Nothing was installed."
		else
			VER_MSG="Installing from the package feed would take this Echo's own software back to an older release. Nothing was installed."
		fi
	else
		VER_UP=1
		if [ -n "$_from" ]; then
			VER_MSG="The package feed has a newer release ($_to) than this Echo runs ($_from), and installing from it would update this Echo as well. Nothing was installed: install the update on the Updates page first."
		else
			VER_MSG="Installing from the package feed would update this Echo's own software as well. Nothing was installed: install the update on the Updates page first."
		fi
	fi
	return 0
}

# Install $1 - unless apk says it would move this Echo's own software to
# another release (see the header). 0 installed; 1 apk failed, and
# $RUNDIR/add.out says why; 3 refused, and VER_MSG says why. --simulate reads
# the index the install would read (apk add fetches none of its own), and
# takes no lock.
install_set() {   # PACKAGES
	# shellcheck disable=SC2086
	apk --wait "$APK_WAIT" add --simulate --no-progress $1 > "$RUNDIR/add.out" 2>&1
	if moves_ours "$RUNDIR/add.out"; then
		cat "$RUNDIR/add.out" >> "$LOG"
		return 3
	fi
	# shellcheck disable=SC2086
	apk --wait "$APK_WAIT" add --no-progress $1 > "$RUNDIR/add.out" 2>&1
	_arc=$?
	cat "$RUNDIR/add.out" >> "$LOG"
	[ $_arc -eq 0 ] || return 1
	# Asked first, so this should not happen. If apk moved our packages all
	# the same, it is finished as an update is: see the add branch.
	moves_ours "$RUNDIR/add.out" && OURS_MOVED=1
	return 0
}

# Our installed packages with a newer version on a repository, one per line as
# "name installed available". `apk version -l '<'` prints "name-ver < newver"
# under a header line; the name is what remains once "-<ver>-r<n>" is cut off.
newer_ours() {
	_ours=$(apk info 2>/dev/null | grep -E "$OURS_RE")
	[ -n "$_ours" ] || return 0
	# shellcheck disable=SC2086
	apk version -l '<' $_ours 2>/dev/null | awk 'NR > 1 && $2 == "<" {
		n = $1; sub(/-[^-]+-r[0-9]+$/, "", n)
		print n, substr($1, length(n) + 2), $3 }'
}

# The settings page reads this to say what is available and when it last looked.
# `sys=` lines are everything else apk could upgrade, which this job never
# does: the Apps page marks them, so the owner can see what a newer image
# would bring. `other` is their count.
#
# `why=` is refresh_index's word for a check that could not be made, or
# "partial" for one made while a postmarketOS or Alpine mirror did not answer
# (so the sys= list may be short). Kept here and not only in the status file,
# which the next job of any kind replaces - an app install included - and the
# Updates page was left saying "the package feed could not be reached"
# whatever the reason had been.
record_check() {   # OK NEWER [WHY]
	mkdir -p "$RUNDIR"
	_sys=$(apk version -l '<' 2>/dev/null | awk 'NR > 1 && $2 == "<" {
		n = $1; sub(/-[^-]+-r[0-9]+$/, "", n)
		print n, substr($1, length(n) + 2), $3 }' | grep -Ev "$OURS_RE")
	{
		echo "checked=$(date +%s)"
		echo "ok=$1"
		echo "why=${3:-}"
		[ -n "$2" ] && printf '%s\n' "$2" | sed 's/^/upd=/'
		[ -n "$_sys" ] && printf '%s\n' "$_sys" | sed 's/^/sys=/'
		echo "other=$(printf '%s' "$_sys" | grep -c .)"
	} > "$RUNDIR/updates.tmp"
	mv "$RUNDIR/updates.tmp" "$RUNDIR/updates"
}

# Install exactly our newer versions, nothing else. Not `apk upgrade`, and not
# `apk add -u`, which upgrades every dependency as well: the apps carry Python
# environments built against one Python minor version, and a system upgrade
# that moved Python on would break them. Pinned with name=version so apk takes
# precisely what was checked - then unpinned, because a pin left in the world
# file (or the checksum pin a hand-installed .apk leaves) blocks the next update.
update_ours() {   # NEWER
	_want=$(printf '%s\n' "$1" | awk 'NF == 3 { print $1 "=" $3 }')
	# shellcheck disable=SC2086
	apk --wait "$APK_WAIT" add --no-progress $_want > "$RUNDIR/ours.out" 2>&1
	_rc=$?
	cat "$RUNDIR/ours.out" >> "$LOG"
	if [ $_rc -ne 0 ]; then
		# apk's own reason, as for an app, rather than a pointer to a log
		# nothing on the settings page can open.
		if apk_busy "$RUNDIR/ours.out"; then
			status failed "$BUSY_MSG" 1 busy
		else
			_why=$(grep -m1 '^ERROR:' "$RUNDIR/ours.out" | sed 's/^ERROR: //' | cut -c1-160)
			status failed "The update did not complete${_why:+: $_why}." 1
		fi
		log "apk add failed: $_want"
		return 1
	fi
	sed -i -E 's/^((device|linux)-amazon-biscuit[a-z-]*)(=|><)[^ ]*$/\1/' "$WORLD"
	# Services keep running the old code, and the boot-time parts of the core
	# only run at boot, so the update is finished by a restart.
	touch "$RUNDIR/reboot-required"
	_to=$(printf '%s\n' "$1" | awk '$1 == "device-amazon-biscuit" { print $3 }')
	record_boot
	if boot_differs; then
		status ok "Updated to ${_to:-the latest version}. Install the new boot image, then restart." 1
	else
		status ok "Updated to ${_to:-the latest version}. Restart to finish." 1
	fi
	log "updated: $_want"
}

# Whether boot_a holds what the installed kernel and initramfs build. An apk
# upgrade regenerates /boot/boot.img, and nothing else copies it to the boot
# partitions - so a kernel update is installed but not in use until the owner
# installs the boot image. Recorded for the settings page to show; building the
# image to compare takes seconds, so it is done here and not per page load.
record_boot() {
	[ -x /usr/bin/biscuit-boot-update ] || return 0
	mkdir -p "$RUNDIR"
	if /usr/bin/biscuit-boot-update status > "$RUNDIR/boot-status.json.tmp" 2>>"$LOG"; then
		mv "$RUNDIR/boot-status.json.tmp" "$RUNDIR/boot-status.json"
	else
		rm -f "$RUNDIR/boot-status.json.tmp"
		log "boot image status could not be read"
	fi
}

boot_differs() {
	grep -q '"differs": true' "$RUNDIR/boot-status.json" 2>/dev/null
}

# Hold Python at the minor version the apps' environments were built for.
#
# The voice assistant and music speaker carry their own Python environments,
# built against one minor version, and each compiled module in them is tied to
# it. A system upgrade that moved python3 from 3.14 to 3.15 would leave both
# unable to start. `python3~3.14` in the world file lets patch releases through
# and nothing else; when edge moves on, apk refuses the upgrade and says so,
# which is the right outcome until the apps are rebuilt.
pin_python() {
	_mm=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
	[ -n "$_mm" ] || return 0
	grep -qx "python3~$_mm" "$WORLD" && return 0
	sed -i '/^python3\([~=<>].*\)\{0,1\}$/d' "$WORLD"
	echo "python3~$_mm" >> "$WORLD"
	log "holding python3 at $_mm"
}

# Update postmarketOS's own packages: those named, or all of them. The kernel
# is not on any feed, so it stays as installed; so does anything pinned.
update_system() {
	pin_python
	if [ -n "$PACKAGES" ]; then
		# shellcheck disable=SC2086
		apk --wait "$APK_WAIT" add --no-progress -u $PACKAGES > "$RUNDIR/sysupdate.out" 2>&1
	else
		apk --wait "$APK_WAIT" upgrade --no-progress > "$RUNDIR/sysupdate.out" 2>&1
	fi
	_rc=$?
	cat "$RUNDIR/sysupdate.out" >> "$LOG"
	_n=$(grep -c '^([0-9]*/[0-9]*) Upgrading' "$RUNDIR/sysupdate.out")
	if [ $_rc -ne 0 ]; then
		if apk_busy "$RUNDIR/sysupdate.out"; then
			status failed "$BUSY_MSG" 1 busy
		else
			_why=$(grep -m1 -E 'ERROR|unable to select|breaks:' "$RUNDIR/sysupdate.out" |
				cut -c1-160)
			status failed "The system update did not complete${_why:+: $_why}." 1
		fi
		log "sysupdate failed"
		return 1
	fi
	if [ "$_n" -gt 0 ]; then
		touch "$RUNDIR/reboot-required"
		record_boot
		if boot_differs; then
			status ok "Updated $_n system package(s). Install the new boot image, then restart." 1
		else
			status ok "Updated $_n system package(s). Restart to finish." 1
		fi
	else
		status ok "Nothing to update." 1
	fi
	log "sysupdate: $_n upgraded"
}

status running "" 0
log "$ACTION $PACKAGES${ORIGIN:+ (started by $ORIGIN)}"

if [ "$ACTION" = "boot" ] || [ "$ACTION" = "bootrollback" ]; then
	# The tool checks the image before writing anything, reads every slot
	# first, and if a write fails puts each slot it touched back as it was.
	# Exit 3 means nothing was written; 4 means a slot may hold a partial
	# image that could not be put back; 5 means every slot was written but
	# the record of it could not be saved.
	status running "$( [ "$ACTION" = boot ] && echo "Installing the boot image" || echo "Putting back the previous boot image")" 0
	_verb=apply
	[ "$ACTION" = bootrollback ] && _verb=rollback
	/usr/bin/biscuit-boot-update "$_verb" > "$RUNDIR/boot.out" 2>&1
	_rc=$?
	cat "$RUNDIR/boot.out" >> "$LOG"
	# A marker in /run as well as the tool's own record: when a failing eMMC
	# has made the root filesystem read-only, the record cannot be written,
	# and restarting must still be refused.
	# boot-verified says every slot was checked good since: it outranks a
	# damage record the tool could not clear because the record itself could
	# not be written (exit 5).
	case $_rc in
	4) touch "$RUNDIR/boot-damaged"; rm -f "$RUNDIR/boot-verified" ;;
	0|5) rm -f "$RUNDIR/boot-damaged"; touch "$RUNDIR/boot-verified" ;;
	esac
	record_boot
	if [ $_rc -eq 0 ]; then
		if grep -q "already holds" "$RUNDIR/boot.out"; then
			status ok "The boot image is already the installed one." 1
		else
			touch "$RUNDIR/reboot-required"
			if [ "$ACTION" = bootrollback ]; then
				status ok "The previous boot image is back. Restart to use it." 1
			else
				status ok "Boot image written. Restart to use it." 1
			fi
		fi
		log "$ACTION complete"
		exit 0
	fi
	# The damaged slot's own line first; otherwise the tool's last word.
	_why=$(grep -m1 'biscuit-boot-update: DAMAGED:' "$RUNDIR/boot.out")
	[ -n "$_why" ] || _why=$(grep 'biscuit-boot-update:' "$RUNDIR/boot.out" | tail -n1)
	_why=$(printf '%s' "$_why" | sed 's/^biscuit-boot-update: //; s/^DAMAGED: //' | cut -c1-200)
	if [ $_rc -eq 5 ]; then
		touch "$RUNDIR/reboot-required"
		status ok "Boot image written, but it could not be recorded (${_why}). Restart to use it." 1
		log "$ACTION written, not recorded"
		exit 0
	elif [ $_rc -eq 4 ]; then
		status failed "A boot partition may be damaged: ${_why}. Do not restart: install the boot image again, or put back the previous one. If that fails too, reinstall from TWRP." 1
	else
		status failed "The boot image was not written${_why:+: $_why}." 1
	fi
	log "$ACTION failed (exit $_rc)"
	exit 1
fi

if [ "$ACTION" = "add" ]; then
	wait_for_clock
	# Index first. A device that has been offline since it was flashed has no
	# usable index, and `apk add` would fail with a confusing "unable to select
	# packages" rather than saying it could not reach the repository. On a
	# clock still unset too: which repositories answer then is what tells a
	# network with no internet from one that only keeps the time server out
	# (see refresh_index), and it costs one small fetch each.
	#
	# The apps come only from the feed. When the feed answered, what failed
	# was a postmarketOS or Alpine mirror, and the install may still find
	# everything it needs there already - so it goes on, and apk says so if
	# it cannot.
	if ! refresh_index && [ "$INDEX_WHY" != partial ]; then
		[ "$INDEX_WHY" = clock ] && log "clock still unset; nothing installed"
		status failed "$(index_message)" 1 "$INDEX_WHY"
		exit 1
	fi
	[ "$INDEX_WHY" = partial ] && log "the feed answered; installing anyway"
	# Nothing is installed on a clock known to be wrong: the feed's
	# certificate cannot have been checked, and the owner would be told about
	# the feed when the clock was the whole story. An index that refreshed
	# without a failure on it had no feed in it (turned off), or only plain
	# HTTP. The apps chosen at setup are tried again by the settings page as
	# soon as the clock is set.
	if clock_unset; then
		INDEX_WHY=clock INDEX_NET=some INDEX_REASON=""
		status failed "$(index_message)" 1 clock
		log "clock still unset; nothing installed"
		exit 1
	fi
	OURS_MOVED="" VER_UP=""
	_done="" _left="" _apkleft="" _vermsg="" _verup=""
	: > "$RUNDIR/add-failed.out"
	install_set "$PACKAGES"
	_rc=$?
	if [ $_rc -eq 0 ]; then
		_done=$PACKAGES
	elif [ $_rc -eq 1 ] && [ "$(echo $PACKAGES | wc -w)" -gt 1 ] &&
			grep -q 'unable to select packages' "$RUNDIR/add.out"; then
		# apk installs all of them or none: one app the feed does not offer
		# (withheld, or not published), or one whose dependency has gone,
		# held back the other for good - and the retry asked for both again
		# every hour. So each is then tried on its own, and what can be
		# installed is; the record names only what could not.
		log "apk could not select them all; installing each on its own"
		for _p in $PACKAGES; do
			install_set "$_p"
			case $? in
			0) _done="$_done $_p" ;;
			3) _left="$_left $_p"; [ -n "$_vermsg" ] || { _vermsg=$VER_MSG; _verup=$VER_UP; } ;;
			*) _left="$_left $_p"; _apkleft="$_apkleft $_p"; cat "$RUNDIR/add.out" >> "$RUNDIR/add-failed.out" ;;
			esac
		done
	elif [ $_rc -eq 3 ]; then
		_left=$PACKAGES _vermsg=$VER_MSG _verup=$VER_UP
	else
		_left=$PACKAGES _apkleft=$PACKAGES
		cp "$RUNDIR/add.out" "$RUNDIR/add-failed.out"
	fi
	_started=""
	# shellcheck disable=SC2086
	for _svc in $(services_of $_done); do
		if rc-service "$_svc" start >> "$LOG" 2>&1; then
			_started="$_started $_svc"
			log "started $_svc"
		else
			log "could not start $_svc"
		fi
	done
	if [ -n "$_started" ]; then
		_msg="Installed and started:$_started"
	else
		_msg="Installed."
	fi
	if [ -n "$OURS_MOVED" ]; then
		# As update_ours finishes an update: our own services run the old
		# code until a restart, and a new kernel wants its boot image.
		touch "$RUNDIR/reboot-required"
		record_boot
		record_check 1 "$(newer_ours)"
		_msg="${_msg%.}. This Echo's own software changed with it: restart to finish."
		log "our own packages moved with the install"
	fi
	if [ -n "$_left" ]; then
		# The record is of what could not be installed: the pages and the
		# retry both go by its packages, so an app that was installed is not
		# reported as failed, and the retry asks only for the rest.
		[ -n "$_done" ] && log "installed:$_done"
		if [ -n "$_apkleft" ]; then
			# Worded for the ones apk failed on ("it"/"them" counts them).
			PACKAGES=$(echo $_apkleft)
			apk_failure "$RUNDIR/add-failed.out" installation
			PACKAGES=$(echo $_left)
			[ "${INDEX_WHY:-}" = partial ] && APK_MSG="$APK_MSG $(index_message)"
			status failed "$APK_MSG" 1 "$APK_WHY"
			log "apk add failed ($APK_WHY)"
		else
			# A newer release on the feed is an update the owner can
			# install - from the Updates page, which this just told them to
			# use. The index was read moments ago, so that page is told now
			# rather than at the next daily check.
			if [ -n "$_verup" ]; then
				_part=""
				[ "$INDEX_WHY" = partial ] && _part=partial
				record_check 1 "$(newer_ours)" "$_part"
			fi
			PACKAGES=$(echo $_left)
			status failed "$_vermsg" 1 version
			log "refused: installing would move this Echo's own packages"
		fi
		exit 1
	fi
	status ok "$_msg" 1
	log "add complete"
elif [ "$ACTION" = "sysupdate" ]; then
	wait_for_clock
	status running "Updating system packages" 0
	if ! refresh_index; then
		status failed "$(index_message)" 1 "$INDEX_WHY"
		exit 1
	fi
	update_system || exit 1
	record_check 1 "$(newer_ours)"
elif [ "$ACTION" = "check" ] || [ "$ACTION" = "update" ]; then
	wait_for_clock
	status running "$( [ "$ACTION" = check ] && echo "Checking for updates" || echo "Updating")" 0
	# Our packages come only from the feed. When the feed answered and only a
	# postmarketOS or Alpine mirror did not, what is newer of ours is known,
	# and a device update it offers must not be thrown away with the rest:
	# the check goes on, as an install does, and records that the list of
	# system updates may be short.
	if ! refresh_index && [ "$INDEX_WHY" != partial ]; then
		# Another apk had the database, so nothing was asked of any
		# repository: the last check's record still says all that is known.
		# Replaced by "could not check", it would also count as today's check,
		# and the daily one would wait a day instead of trying again at its
		# next pass.
		[ "$INDEX_WHY" = busy ] || record_check 0 "" "$INDEX_WHY"
		# The boot image needs no network to compare, and Put back must not
		# wait for the feed to be reachable.
		record_boot
		status failed "$(index_message)" 1 "$INDEX_WHY"
		exit 1
	fi
	_part=""
	if [ "$INDEX_WHY" = partial ]; then
		_part=partial
		log "the feed answered; checking anyway"
	fi
	_new=$(newer_ours)
	if [ "$ACTION" = "check" ] || [ -z "$_new" ]; then
		record_check 1 "$_new" "$_part"
		record_boot
		if [ -n "$_new" ]; then
			status ok "An update is available." 1
		else
			status ok "Up to date." 1
		fi
		log "$ACTION complete: ${_new:-nothing newer}"
		exit 0
	fi
	update_ours "$_new" || exit 1
	record_check 1 "" "$_part"
	log "update complete"
else
	# Collected before the delete, while the package is still installed.
	# shellcheck disable=SC2086
	_svcs=$(services_of $PACKAGES)
	for _svc in $_svcs; do
		rc-service "$_svc" stop >> "$LOG" 2>&1 && log "stopped $_svc"
	done
	apk --wait "$APK_WAIT" del $PACKAGES > "$RUNDIR/del.out" 2>&1
	_rc=$?
	cat "$RUNDIR/del.out" >> "$LOG"
	if [ $_rc -ne 0 ]; then
		apk_failure "$RUNDIR/del.out" removal
		status failed "$APK_MSG" 1 "$APK_WHY"
		log "apk del failed"
		exit 1
	fi
	status ok "Removed." 1
	log "del complete"
fi
exit 0
