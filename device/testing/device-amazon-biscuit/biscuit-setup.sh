#!/bin/sh
# First-boot Wi-Fi setup: broadcast an open network, serve a captive portal,
# take credentials from a phone, join, and confirm - with no host computer.
#
# This is the single entry point for setup mode. Everything that wants to start
# it goes through the service rather than calling in here directly:
#
#     rc-service biscuit-setup restart
#
# which is what /etc/local.d/wifi.start does on an unconfigured device and after
# a failed join, what a button set to "Enter setup mode" does (biscuit-va-leds),
# and what Home Assistant would call to put a working device back into setup.
# "restart", not "start": this is a one-shot and OpenRC keeps it marked started
# after it has run, so a second "start" is refused (see biscuit-setup.initd).
#
# THE INTERFACE MUST BE REGISTERED IN P2P MODE. "echo 1 1" registers ap0, which
# looks like the obvious choice, and it fails silently: hostapd reaches
# AP-ENABLED with state=ENABLED and freq=2437 and the device radiates nothing,
# because p2pStateInit_IDLE fails its eIntendOPMode == OP_MODE_ACCESS_POINT test
# and the AP never re-acquires the channel. "echo 1 0" registers p2p0, the FSM
# takes the GO path, and the same hostapd config beacons at full strength.
# Measured by forced scan from a laptop: 100% signal, stable past three minutes.
#
# Needs kernel r218 or later for /proc/net/wlan/p2p_mode.

set -u

IFACE=p2p0
STA=wlan0
# How many times to ask the radio for a scan before giving up and raising the
# AP anyway. Bounded because the AP is what lets someone type the network in by
# hand, so a dead radio must not keep them off the setup page entirely.
SCAN_TRIES=10
PROC=/proc/net/wlan/p2p_mode
ADDR=192.168.4.1
PREFIX=24
DHCP_FROM=192.168.4.10
DHCP_TO=192.168.4.100
CHANNEL=6
WINDOW=600                     # matches stock's provisioning timeout
# The portal extends the window while someone is using the page, so a person
# halfway through the form is not cut off - but never past this.
MAX_WINDOW=1800
RUNDIR=/run/biscuit-setup
LOCKDIR=/run/biscuit-setup.lock
# Written by the session itself once it holds LOCKDIR, and removed with it; see
# the "run" case at the bottom and biscuit-setup.initd.
PIDFILE=/run/biscuit-setup.pid
LOG=/var/log/biscuit-setup.log
CONF=/etc/wpa_supplicant/wpa_supplicant.conf
HOSTAPD_CONF=$RUNDIR/hostapd.conf
DNSMASQ_CONF=$RUNDIR/dnsmasq.conf
PORTAL=/usr/bin/biscuit-portal.py

log() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }

# biscuit-ring.py's documented FIFO. Silent no-op when the ring is not up, so
# setup never depends on it.
RING_FIFO=/run/biscuit-ring/control
ring() { [ -p "$RING_FIFO" ] && echo "$1" > "$RING_FIFO" 2>/dev/null; return 0; }
earcon() { /usr/bin/biscuit-earcon "$1" >/dev/null 2>&1 || true; }
# The sound that goes with the animation, resolved from the same
# activity->sound table. Silent no-op and always true, so audio can
# never be the thing that fails a boot, a Wi-Fi bring-up or a setup.

# Ring animations are user-customisable from the settings page. biscuit-settings
# writes /opt/persist/led-anim.env with one LED_<activity> per activity, already
# resolved to a concrete animation name. Sourced rather than parsed so no Python
# lands on the boot path, and every use falls back to OUR act_ animation so a
# missing or partial file can never leave the ring dark. The fallbacks used to
# name stock animations; those are no longer shipped, so a fallback to one
# would have left the ring dark in exactly the case it exists to cover.
#
# The `if` form is deliberate: `[ -r f ] && . f` evaluates false when the file is
# absent, which aborts any caller running under `set -e`.
if [ -r /opt/persist/led-anim.env ]; then
	. /opt/persist/led-anim.env
fi
SETUP_ANIM=${LED_setup_mode:-act_setup_mode}
SETUP_OK_ANIM=${LED_setup_success:-act_setup_success}
SETUP_ERR_ANIM=${LED_setup_error:-act_setup_error}
WIFI_ANIM=${LED_wifi_connecting:-act_wifi_connecting}

# ---------------------------------------------------------------------------

# Name the network after the radio, so two devices on a bench do not collide.
# wlan0 exists by the time setup runs - wifi.start loads the driver before it
# looks at whether a network is configured.
# Create the device's user account, once.
#
# The published image ships postmarketOS's documented default account, user /
# 147147, and nothing else. That password is public, so until this function has
# replaced it the account is fenced off: biscuit-firstboot refuses password SSH
# for it except over the USB cable, and the settings page refuses it the same
# way; 40-biscuit.conf refuses every SSH login from the setup network, and
# pause_settings below keeps the settings page down for the length of every
# setup session. This is where the owner's
# account comes into existence, from what the portal collected, and it is the
# same account the settings page at :8080 authenticates against.
#
# Marked on the PERSIST partition, so a reflash does not silently ask again -
# and does not silently discard the account the owner already chose.
PROVISIONED=/opt/persist/provisioned

create_account() {
	# A password reset from setup mode. The portal writes this only after the
	# microphone button was pressed on the device itself while the page asked
	# for it, and only for the account this device already has - so it needs
	# someone in the room, not just someone in radio range.
	if [ -f "$RUNDIR/account_reset" ]; then
		_owner=$(cat "$PROVISIONED" 2>/dev/null)
		_who=$(cat "$RUNDIR/account_reset" 2>/dev/null)
		pass=$(cat "$RUNDIR/account_pass" 2>/dev/null)
		rm -f "$RUNDIR/account_reset" "$RUNDIR/account_pass" "$RUNDIR/account_user"
		if [ -n "$_owner" ] && [ "$_owner" = "$_who" ] && [ -n "$pass" ] &&
			id "$_owner" >/dev/null 2>&1; then
			if printf '%s:%s\n' "$_owner" "$pass" | chpasswd >/dev/null 2>&1; then
				log "password for $_owner reset from setup mode (proven at the device)"
				/usr/bin/biscuit-persist-account save >/dev/null 2>&1 || true
			else
				log "could not reset the password for $_owner"
			fi
		else
			log "ignored a password reset for '${_who:-?}': not this device's account"
		fi
		ssh_policy
		return 0
	fi

	# The marker lives on the persist partition and a flash replaces only the
	# rootfs, so it outlives the account it names. Believing it blindly is how
	# a fresh install silently threw away the credentials someone had just
	# typed into the wizard: setup reported success, no account was created,
	# the shipped default was never retired, and :8080 then refused the very
	# username and password it had just asked for. Observed, not hypothetical.
	#
	# So the marker is only trusted when the account it names actually exists.
	if [ -f "$PROVISIONED" ]; then
		_owner=$(cat "$PROVISIONED" 2>/dev/null)
		if [ -n "$_owner" ] && id "$_owner" >/dev/null 2>&1; then
			return 0
		fi
		log "stale marker: provisioned as '${_owner:-?}' but no such account - reprovisioning"
		rm -f "$PROVISIONED"
	fi
	acct=$(cat "$RUNDIR/account_user" 2>/dev/null)
	pass=$(cat "$RUNDIR/account_pass" 2>/dev/null)
	if [ -z "$acct" ] || [ -z "$pass" ]; then
		log "no account submitted; leaving the device without one"
		return 0
	fi
	if id "$acct" >/dev/null 2>&1; then
		# Only a person's account may be taken over this way. Given a daemon's
		# name (chrony, dnsmasq, messagebus...) this used to set a password on
		# it, record it as the owner and delete 'user' - leaving an "owner"
		# with no shell, no wheel and no settings-page login, and setup never
		# asking again. The portal refuses these names too.
		_uid=$(id -u "$acct" 2>/dev/null)
		case "$_uid" in ""|*[!0-9]*) _uid=0 ;; esac
		if [ "$_uid" -lt 1000 ] || [ "$_uid" -gt 64999 ]; then
			log "refused the account name $acct: an existing system account (uid $_uid)"
			rm -f "$RUNDIR/account_user" "$RUNDIR/account_pass"
			return 1
		fi
		log "account $acct already exists; setting its password only"
	else
		adduser -D -s /bin/ash "$acct" >/dev/null 2>&1 || {
			log "could not create $acct"
			return 1
		}
		# wheel for doas/sudo, and the audio/video groups the device's own
		# services expect an interactive user to be in.
		for g in wheel audio video netdev plugdev; do
			addgroup "$acct" "$g" >/dev/null 2>&1 || true
		done
	fi
	# chpasswd reads from stdin, so the password never appears in argv where
	# any user could read it out of ps.
	printf '%s:%s\n' "$acct" "$pass" | chpasswd >/dev/null 2>&1 || {
		log "could not set the password for $acct"
		return 1
	}
	mkdir -p "$(dirname "$PROVISIONED")"
	printf '%s\n' "$acct" > "$PROVISIONED"
	chmod 600 "$PROVISIONED"
	log "created account $acct"

	# Retire the shipped default now that a real account exists. The image
	# ships postmarketOS's documented user/147147 so a device whose Wi-Fi
	# setup fails still has a way in - but that password is on a public web
	# page, and this device is headless and always on. It must not outlive
	# setup. biscuit-firstboot repeats this at boot as a backstop.
	if [ "$acct" != "user" ] && id user >/dev/null 2>&1; then
		deluser --remove-home user >/dev/null 2>&1 || deluser user >/dev/null 2>&1
		if id user >/dev/null 2>&1; then
			log "could not remove the default account; locking it"
			passwd -l user >/dev/null 2>&1 || true
		else
			log "retired the shipped default account"
		fi
	fi

	# Make the account persistent and reachable NOW, not at the next boot.
	#
	# biscuit-persist snapshots the owner and binds ~/.ssh during boot, and
	# setup runs after that, so a freshly provisioned device had the owner's
	# key already in the store and sshd still refusing it until someone
	# rebooted. Observed on hardware, twice, on an account that had just been
	# created here.
	if [ -x /usr/bin/biscuit-persist-account ]; then
		/usr/bin/biscuit-persist-account provision >/dev/null 2>&1 &&
			log "account $acct snapshotted and its keys bound"
	fi

	# The password has served its purpose; do not leave it in /run.
	rm -f "$RUNDIR/account_user" "$RUNDIR/account_pass"
	ssh_policy
	return 0
}

# The secrets the portal hands over in /run. Each is deleted the moment it has
# been used - the passphrase in run_setup, the account files above - so this
# is for a session that ends before it gets that far: stopped, restarted or
# given up on mid-way. A new session never needs them, because every pass
# waits for a fresh submission, which writes them again. biscuit-setup.initd's
# stop() repeats it for a session killed before this could run.
forget_secrets() {
	rm -f "$RUNDIR/psk" "$RUNDIR/account_pass" "$RUNDIR/account_user" \
		"$RUNDIR/account_reset"
}

# The SSH rule for the shipped default account (see biscuit-firstboot). Asked
# for again whenever the account changes here, so sshd stops refusing an owner
# who chose the name "user" - or starts refusing the default - straight away
# instead of at the next boot. It reloads sshd itself when the rule changes.
SSHD_POLICY=/run/biscuit-sshd/default-account.conf
ssh_policy() {
	rc-service biscuit-firstboot sshpolicy >/dev/null 2>&1 ||
		log "could not re-evaluate the SSH rule for the default account"
	return 0
}

# True while the only account that can sign in is the shipped default, whose
# password is printed on postmarketos.org. biscuit-firstboot's rule file is the
# precise answer - it checks the password hash itself; the fallback covers a
# device where that rule could not be written: no owner account, and the
# shipped one present.
default_account_live() {
	[ -f "$SSHD_POLICY" ] && return 0
	_o=$(cat "$PROVISIONED" 2>/dev/null)
	if [ -n "$_o" ] && id "$_o" >/dev/null 2>&1; then
		return 1
	fi
	id user >/dev/null 2>&1
}

ssid_name() {
	mac=$(cat /sys/class/net/$STA/address 2>/dev/null | tr -d ':' | tr 'a-f' 'A-F')
	if [ -n "$mac" ]; then
		echo "biscuit-$(echo "$mac" | tail -c 5)"
	else
		echo "biscuit-setup"
	fi
}

# Scan BEFORE raising the AP.
#
# wlan0 can in fact still scan while the AP beacons - measured, 7-9 BSSes - so
# this is not strictly required. It is done first anyway because a scan taken
# with the radio otherwise idle is faster and more complete, and because the
# portal should have a list ready the moment the first phone connects rather
# than blocking on a scan mid-request.
scan_networks() {
	ip link set $STA up 2>/dev/null
	# Two passes: a single scan routinely misses a beacon interval, and a
	# network missing from the list is the one thing a user cannot work around
	# except by typing it in.
	#
	# The first scan of a boot usually fails outright, though. wifi.start loads
	# the WLAN driver from local.d, which OpenRC runs LAST, so setup reaches
	# here before wlan0 will accept a scan at all - and "Network is down"
	# yields exactly the same empty list as a house with no neighbours. That
	# shipped and cost a real setup: no networks on the first visit, every
	# network on the second. So retry until the radio answers, and keep the
	# error that explains it rather than sending it to /dev/null.
	: > "$RUNDIR/scan.raw"
	: > "$RUNDIR/scan.err"
	_try=0
	while [ "$_try" -lt "$SCAN_TRIES" ]; do
		_try=$((_try + 1))
		iw dev $STA scan 2>>"$RUNDIR/scan.err" >> "$RUNDIR/scan.raw"
		grep -q '^BSS ' "$RUNDIR/scan.raw" && break
		sleep 2
		ip link set $STA up 2>/dev/null
	done
	if grep -q '^BSS ' "$RUNDIR/scan.raw"; then
		[ "$_try" -gt 1 ] && log "radio answered on scan attempt $_try"
	else
		log "no scan after $_try attempts: $(tail -1 "$RUNDIR/scan.err" 2>/dev/null)"
	fi
	sleep 2
	iw dev $STA scan 2>>"$RUNDIR/scan.err" >> "$RUNDIR/scan.raw"

	# Per network: the best signal seen, the bands it is on and whether it
	# is secured - strongest first, hidden (blank) names dropped. The portal
	# draws signal bars and the band from networks.info, as the settings page
	# does; join() reads the bare names in networks.
	#
	# One record per BSS, closed at the next "BSS" line: the security lines
	# come after the SSID, so nothing can be decided when the name is read.
	awk '
		function close_bss() {
			if (ssid != "") {
				if (!(ssid in best) || sig > best[ssid]) best[ssid] = sig
				if (freq >= 4900) b5[ssid] = 1; else if (freq > 0) b24[ssid] = 1
				if (sec) secure[ssid] = 1
			}
			ssid = ""; sig = -100; freq = 0; sec = 0
		}
		BEGIN              { sig = -100 }
		/^BSS /            { close_bss() }
		/^\tfreq:/         { freq = $2 + 0 }
		/^\tsignal:/       { sig = $2 + 0 }
		/^\tcapability:/   { if ($0 ~ /Privacy/) sec = 1 }
		/^\t(RSN|WPA):/    { sec = 1 }
		/^\tSSID: /        { ssid = substr($0, 8) }
		END {
			close_bss()
			for (s in best) {
				band = (s in b24) ? "2.4" : ""
				if (s in b5) band = (band == "") ? "5" : band "/5"
				printf "%d\t%s\t%s\t%d\n", best[s], s, band, (s in secure)
			}
		}
	' "$RUNDIR/scan.raw" | sort -rn > "$RUNDIR/networks.info"
	cut -f2 "$RUNDIR/networks.info" > "$RUNDIR/networks"

	# Guess the region from the neighbours. Access points broadcast a country
	# code in their beacons, and every one within range of a home is in the
	# same country as the home. Taking the most common answer means the setup
	# form can offer a sensible default instead of asking someone to know their
	# own ISO country code cold - and a wrong guess is one dropdown away from
	# being fixed, because it is only a default.
	#
	# Only a hint. The kernel is NOT told anything here: the region is applied
	# at join time from what the user actually confirmed.
	awk '/^\tCountry: / { print $2 }' "$RUNDIR/scan.raw" \
		| grep -E '^[A-Z][A-Z]$' | sort | uniq -c | sort -rn \
		| head -1 | awk '{ print $2 }' > "$RUNDIR/country_hint" 2>/dev/null
	[ -s "$RUNDIR/country_hint" ] &&
		log "beacons suggest region $(cat "$RUNDIR/country_hint")"

	log "scan found $(wc -l < "$RUNDIR/networks") networks"
}

# The setup network is open, so it is treated as hostile. Clients on it need
# DHCP, DNS and the portal on $ADDR:80, all IPv4, and nothing else.
#
#   * IPv6 off on the interface. Otherwise it gets a link-local address, and a
#     client could reach every service listening on "::" - sshd included -
#     from an address none of the IPv4 rules below or in 40-biscuit.conf name.
#   * Strict reverse-path filtering. A packet arriving here must come from an
#     address that routes back out here, i.e. from the setup subnet. Linux
#     otherwise accepts, on any interface, traffic for any of its addresses, so
#     a client that forges a source outside 192.168.4.0/24 would slip past
#     sshd's "Match Address 192.168.4.0/24". (The kernel exempts DHCP's
#     0.0.0.0 broadcasts from this check, so leases still work.)
#
# Both are per-interface and p2p0 is created afresh for every session, so this
# runs every time the AP is raised. A failure is logged, not fatal: sshd's own
# rules still refuse every setup-network login, and a device that cannot raise
# its setup network cannot be set up at all.
harden_setup_iface() {
	_v6=/proc/sys/net/ipv6/conf/$IFACE/disable_ipv6
	if [ -e "$_v6" ]; then
		{ echo 1 > "$_v6"; } 2>/dev/null || log "could not turn IPv6 off on $IFACE"
	fi
	{ echo 1 > /proc/sys/net/ipv4/conf/$IFACE/rp_filter; } 2>/dev/null ||
		log "could not turn on reverse-path filtering on $IFACE"
}

# sshd refuses every login from the setup subnet for as long as the setup
# network exists. The rule is written BEFORE hostapd raises the network and
# removed only AFTER it is down, and sshd is reloaded each time, since it reads
# its configuration only when it starts or reloads. It is not a static rule
# because 192.168.4.0/24 is also somebody's house network (eero's default is
# 192.168.4.0/22); see 40-biscuit.conf. The subnet is derived from ADDR, which
# with PREFIX=24 means its first three octets.
#
# A write or reload that fails is logged, not fatal: 40-biscuit.conf refuses
# every login to $ADDR itself statically, and the default account's password
# rule does not depend on this one.
SSHD_RUNDIR=/run/biscuit-sshd
SETUP_NET_RULE=$SSHD_RUNDIR/setup-network.conf
setup_network_ssh() {
	case "$1" in
	closed)
		mkdir -p "$SSHD_RUNDIR" 2>/dev/null
		chmod 755 "$SSHD_RUNDIR" 2>/dev/null
		if printf '%s\n' \
			"# Written by biscuit-setup.sh while the open setup network is up;" \
			"# removed when it goes down. Every login from it is refused." \
			"Match Address ${ADDR%.*}.0/$PREFIX" \
			"	DenyUsers *" \
			"	PasswordAuthentication no" \
			"	KbdInteractiveAuthentication no" \
			"	PubkeyAuthentication no" > "$SETUP_NET_RULE.tmp" 2>/dev/null &&
			mv -f "$SETUP_NET_RULE.tmp" "$SETUP_NET_RULE"; then
			chmod 644 "$SETUP_NET_RULE" 2>/dev/null
		else
			rm -f "$SETUP_NET_RULE.tmp" 2>/dev/null
			log "WARNING: could not write $SETUP_NET_RULE"
			return 0
		fi
		;;
	open)
		[ -f "$SETUP_NET_RULE" ] || return 0
		rm -f "$SETUP_NET_RULE"
		;;
	esac
	rc-service --ifstarted sshd reload >/dev/null 2>&1 ||
		log "WARNING: sshd did not reload after the setup-network SSH rule changed"
	return 0
}

# Confirms harden_setup_iface's work once the interface is up and addressed,
# when an IPv6 address would have appeared if it were going to.
check_setup_iface() {
	if awk -v i="$IFACE" '$6 == i { found = 1 } END { exit !found }' \
		/proc/net/if_inet6 2>/dev/null; then
		log "WARNING: $IFACE has an IPv6 address; flushing it"
		{ echo 1 > /proc/sys/net/ipv6/conf/$IFACE/disable_ipv6; } 2>/dev/null
		ip -f inet6 addr flush dev $IFACE 2>/dev/null
	fi
	[ "$(cat /proc/sys/net/ipv4/conf/$IFACE/rp_filter 2>/dev/null)" = 1 ] ||
		log "WARNING: reverse-path filtering is not on for $IFACE"
}

start_ap() {
	SSID=$(ssid_name)
	echo "$SSID" > "$RUNDIR/ssid"

	# Mode 0 - P2P. See the header; mode 1 does not radiate.
	echo "1 0" > "$PROC" 2>/dev/null
	i=0
	while [ ! -d /sys/class/net/$IFACE ] && [ $i -lt 15 ]; do
		sleep 1
		i=$((i + 1))
	done
	if [ ! -d /sys/class/net/$IFACE ]; then
		log "$IFACE never appeared; cannot start setup AP"
		return 1
	fi
	# Before hostapd raises it, so no client ever sees it otherwise.
	harden_setup_iface
	setup_network_ssh closed

	cat > "$HOSTAPD_CONF" <<EOF
interface=$IFACE
driver=nl80211
ctrl_interface=/var/run/hostapd
ssid=$SSID
hw_mode=g
channel=$CHANNEL
auth_algs=1
wmm_enabled=0
ignore_broadcast_ssid=0
EOF

	ip link set $IFACE up 2>/dev/null
	# Alpine's hostapd is built without CONFIG_DEBUG_FILE, so -f is a silent
	# no-op and there is no log to read. hostapd_cli is the only status.
	hostapd -B "$HOSTAPD_CONF" >/dev/null 2>&1
	sleep 5

	if ! pidof hostapd >/dev/null 2>&1; then
		log "hostapd failed to start"
		return 1
	fi

	state=$(hostapd_cli -i $IFACE -p /var/run/hostapd status 2>/dev/null | sed -n 's/^state=//p')
	freq=$(hostapd_cli -i $IFACE -p /var/run/hostapd status 2>/dev/null | sed -n 's/^freq=//p')
	if [ "$state" != "ENABLED" ] || [ -z "$freq" ]; then
		log "hostapd up but not enabled (state=$state freq=$freq)"
		return 1
	fi

	ip addr flush dev $IFACE 2>/dev/null
	ip addr add $ADDR/$PREFIX dev $IFACE 2>/dev/null
	check_setup_iface

	# DHCP, plus a DNS server that answers every name with our own address so
	# the phone's connectivity check fails closed and the captive-portal sheet
	# opens by itself. Option 114 is the RFC 8910 portal URL, which newer
	# clients honour directly instead of guessing from a hijacked probe.
	cat > "$DNSMASQ_CONF" <<EOF
interface=$IFACE
bind-interfaces
except-interface=lo
dhcp-range=$DHCP_FROM,$DHCP_TO,255.255.255.0,12h
dhcp-option=option:router,$ADDR
dhcp-option=option:dns-server,$ADDR
dhcp-option=114,"http://$ADDR/"
address=/#/$ADDR
no-resolv
no-hosts
log-facility=$RUNDIR/dnsmasq.log
EOF
	# Its stderr goes to the log and its survival is checked. It used to be
	# 2>/dev/null and unchecked, and on 2026-09-23 (away, no known network) setup
	# came up with hostapd beaconing and no dnsmasq at all - an open network
	# that hands out no addresses, with nothing anywhere saying why. An AP
	# without DHCP is worse than no AP: fail here, so the caller tears it down
	# and plays the setup error instead.
	if ! dnsmasq -C "$DNSMASQ_CONF" 2>>"$LOG"; then
		log "dnsmasq failed to start (see the lines above)"
		return 1
	fi
	sleep 1
	if ! pgrep -f "dnsmasq -C $DNSMASQ_CONF" >/dev/null; then
		log "dnsmasq exited right after starting:" \
			"$(tail -n 3 "$RUNDIR/dnsmasq.log" 2>/dev/null | tr '\n' ' ')"
		return 1
	fi

	log "setup AP up: ssid=$SSID freq=$freq addr=$ADDR"
	return 0
}

stop_ap() {
	pkill -f "biscuit-portal.py" 2>/dev/null
	pkill -f "dnsmasq -C $DNSMASQ_CONF" 2>/dev/null
	pkill hostapd 2>/dev/null
	ip addr flush dev $IFACE 2>/dev/null
	ip link set $IFACE down 2>/dev/null
	echo 0 > "$PROC" 2>/dev/null
	# Only once nothing can be associated any more.
	setup_network_ssh open
	sleep 2
	log "setup AP down"
}

# Join the network the portal was given, and decide what actually happened.
#
# Association is not success: a BSS too weak to carry data still reaches
# wpa_state=COMPLETED and then fails DHCP, leaving the device associated,
# addressless and unreachable. Only an address counts. The distinction matters
# beyond bookkeeping here, because the reason is shown to the user on the portal
# when setup restarts.
join() {
	ssid=$1
	psk=$2
	# Set when the portal's "use my router's WPS button" box was ticked. The
	# passphrase is then never typed, never posted and never written here: the
	# router hands the credentials to wpa_supplicant over the air, and
	# update_config=1 lets the supplicant save them for itself. This is the one
	# path where the home Wi-Fi secret does not cross the setup network at all.
	wps=no
	[ -f "$RUNDIR/wps" ] && wps=yes

	# The region the user confirmed on the first setup step. Applied BEFORE the
	# supplicant starts, because the regulatory domain decides which channels
	# may be used to associate, and the kernel's fallback world domain marks
	# every 5 GHz rule PASSIVE-SCAN.
	#
	# Written into wpa_supplicant.conf as well as set live: `iw reg set` does
	# not survive a reboot, and country= there makes the supplicant reapply it
	# on every start without a service of its own.
	region=$(cat "$RUNDIR/region" 2>/dev/null)
	case "$region" in
	[A-Z][A-Z]) ;;
	*) region="" ;;
	esac
	if [ -n "$region" ]; then
		iw reg set "$region" 2>/dev/null
		mkdir -p /opt/persist
		echo "$region" > /opt/persist/region 2>/dev/null
		log "regulatory region set to $region"
	fi

	pkill wpa_supplicant 2>/dev/null
	sleep 1

	umask 077
	{
		echo "ctrl_interface=/var/run/wpa_supplicant"
		echo "update_config=1"
		[ -n "$region" ] && echo "country=$region"
		if [ "$wps" = yes ]; then
			: # No network block at all - WPS supplies one and it is saved below.
		elif [ -z "$psk" ]; then
			printf 'network={\n\tssid="%s"\n\tkey_mgmt=NONE\n}\n' "$ssid"
		else
			# Only the derived key is kept: the grep drops the "#psk=" comment
			# that carries the passphrase itself. The passphrase is on
			# wpa_passphrase's command line for the few milliseconds it runs,
			# and there is no way round that with this tool: wpa_passphrase
			# 2.11 refuses to read it from a pipe ("tcgetattr: Not a tty",
			# exit 1, no network block), so feeding it on stdin would fail
			# every join.
			wpa_passphrase "$ssid" "$psk" | grep -v '^\s*#'
		fi
	} > "$CONF"
	chmod 600 "$CONF"
	umask 022

	ip link set $STA up 2>/dev/null
	wpa_supplicant -B -i $STA -c "$CONF" -D nl80211 >/dev/null 2>&1

	# WPS_PBC is a control-socket command, not a config option, so it can only
	# be sent once the supplicant is up and its socket exists.
	deadline=30
	if [ "$wps" = yes ]; then
		sleep 2
		if wpa_cli -i $STA wps_pbc 2>/dev/null | grep -q OK; then
			# The router's push-button walk is two minutes, and a person has to
			# physically get to the router, so this waits far longer than an
			# ordinary join. Failing at 60 s would fail on the walk, not the radio.
			deadline=70
			log "WPS started; press the button on your router within two minutes"
		else
			log "WPS was refused by wpa_supplicant"
			return 1
		fi
	fi

	state=""
	i=0
	while [ $i -lt $deadline ]; do
		state=$(wpa_cli -i $STA status 2>/dev/null | sed -n 's/^wpa_state=//p')
		[ "$state" = "COMPLETED" ] && break
		sleep 2
		i=$((i + 1))
	done

	# WPS builds the network block itself; persist it or the credentials are
	# lost on the next boot and the device returns to setup.
	#
	# And take the file back to 0600 straight after. SAVE_CONFIG has
	# wpa_supplicant rewrite it under its own 0022 umask, so the network the
	# router just handed over - its key included - was readable by every local
	# account until wifi.start re-tightened it at the next boot, and the file
	# is on the persist partition. biscuit-netcfg does the same after each of
	# its SAVE_CONFIGs (_protect_wpa_conf).
	if [ "$wps" = yes ] && [ "$state" = "COMPLETED" ]; then
		wpa_cli -i $STA save_config >/dev/null 2>&1
		chmod 600 "$CONF" 2>/dev/null
		ssid=$(wpa_cli -i $STA status 2>/dev/null | sed -n 's/^ssid=//p')
		log "WPS joined ${ssid:-the network}"
	fi

	if [ "$state" != "COMPLETED" ]; then
		# Distinguish "no such network" from "wrong passphrase" by asking
		# whether the SSID is on the air at all. Without this the portal can
		# only say "it did not work", which sends people to re-type a password
		# that was right all along.
		# networks.utf8 is the portal's copy with iw's escapes decoded - the
		# form the name was chosen and joined in.
		if grep -Fxq -e "$ssid" "$RUNDIR/networks" 2>/dev/null ||
			grep -Fxq -e "$ssid" "$RUNDIR/networks.utf8" 2>/dev/null; then
			echo "auth" > "$RUNDIR/result"
			log "join failed: $ssid is visible but never associated - wrong passphrase?"
		else
			echo "notfound" > "$RUNDIR/result"
			log "join failed: $ssid was not seen in the scan"
		fi
		return 1
	fi

	udhcpc -i $STA -b -q -t 4 -T 3 >/dev/null 2>&1
	if ip -o -4 addr show $STA 2>/dev/null | grep -q "inet "; then
		echo "ok" > "$RUNDIR/result"
		log "joined $ssid: $(ip -o -4 addr show $STA | awk '{print $4}')"
		return 0
	fi

	echo "dhcp" > "$RUNDIR/result"
	log "join failed: associated to $ssid but no DHCP lease"
	return 1
}

# Apply the non-network parts of the submission. Deliberately after a successful
# join, because installing packages needs the network that was just configured.
apply_extras() {
	name=$(cat "$RUNDIR/device_name" 2>/dev/null)
	if [ -n "$name" ]; then
		echo "$name" > /etc/hostname
		hostname "$name" 2>/dev/null
		# And the store's copy, now. biscuit-persist restores /etc/hostname from
		# it at every boot and only saves it back on a clean shutdown, which an
		# unplugged Echo never has: without this the name reverted at the next
		# power cut, and the assistant and Sendspin, which take it from the
		# hostname, came back as the image's default.
		mkdir -p /opt/persist/etc
		echo "$name" > /opt/persist/etc/hostname.tmp &&
			mv /opt/persist/etc/hostname.tmp /opt/persist/etc/hostname
		sync
		# Published over the avahi already running in the default runlevel, so
		# the phone can reach http://<name>.local once it is back on the house
		# network. This is the confirmation path that needs no host computer.
		if [ -f /etc/avahi/avahi-daemon.conf ]; then
			sed -i "s/^#\?host-name=.*/host-name=$name/" /etc/avahi/avahi-daemon.conf
			rc-service avahi-daemon restart >/dev/null 2>&1
		fi
		# Keep the Bluetooth speaker name in step with the device name; a
		# phone's Bluetooth list is the other place this device is identified.
		if [ -f /etc/bluetooth/main.conf ]; then
			sed -i "s/^Name = .*/Name = $name/" /etc/bluetooth/main.conf
		fi
		log "device name set to $name"
	fi

	# The settings page's address, if setup chose a port other than the one
	# it has. The page moves when setup finishes (resume_settings).
	_port=$(cat "$RUNDIR/settings_port" 2>/dev/null)
	case "$_port" in ""|*[!0-9]*) _port="" ;; esac
	if [ -n "$_port" ] && [ "$_port" != "$(cat /opt/persist/settings-port 2>/dev/null || echo 8080)" ]; then
		echo "$_port" > /opt/persist/settings-port.tmp &&
			mv /opt/persist/settings-port.tmp /opt/persist/settings-port
		settings_moved=yes
		log "settings page moved to port $_port"
	fi

	# The time zone, chosen on the first step - the phone's own, unless it was
	# changed. Stored where the settings page and biscuit-persist keep it, so a
	# flash or a restart keeps it too.
	tz=$(cat "$RUNDIR/timezone" 2>/dev/null)
	case "$tz" in ""|*..*|/*) tz="" ;; esac
	if [ -n "$tz" ] && [ -f "/usr/share/zoneinfo/$tz" ]; then
		echo "$tz" > /opt/persist/timezone.tmp &&
			mv /opt/persist/timezone.tmp /opt/persist/timezone
		ln -sf "/usr/share/zoneinfo/$tz" /etc/localtime
		echo "$tz" > /etc/timezone
		log "time zone set to $tz"
	fi
	# The port and the time zone above were renamed into place; an Echo is
	# unplugged, not shut down, and a rename can reach the disk before the
	# data it names.
	sync

	apply_apps
}

# THE APPS TICKED ON THE LAST STEP
#
# They are installed by the same background job the settings page uses, not
# here: the job waits for the clock (a fresh Echo still reads 2010 at this point
# and fails every HTTPS certificate), and its progress and outcome are shown on
# the settings page, where an install run from here only ever reached this log.
#
# The choice is also WRITTEN DOWN, in APPS_PENDING on the persist partition,
# before the job starts. It used to live only in /run and in that one job: when
# the job failed - the package feed was offline during a fresh install -
# nothing tried again, a reboot forgot the choice, and the Apps page showed the
# apps as never chosen. The settings side retries what the file names, shows
# that it is waiting, and removes each package once it is installed, whoever
# installed it; the owner can cancel it there. The file is on the persist
# partition so a reflash keeps it too.
#
# Format, shared with biscuit-pkgjob.sh and the settings page: one apk package
# name per line, LF, nothing else. Written only here and only when something
# was ticked. Every name is checked against the apps.json the portal offered
# its checkboxes from, so the file can only ever name an optional app - it
# drives an installer running as root.
#
# A setup re-entered on a working device where nothing is ticked leaves the file
# alone. The boxes start unticked on every visit, so an empty choice means "not
# asked about again", not "cancel the earlier one" - the page to cancel it is
# the settings page, where the owner can see it. For the same reason a new
# choice is added to an earlier one rather than replacing it. Apps that are
# already installed are neither written nor installed again: a reinstall would
# only report a failure, while the feed is down, for an app that works.
#
# Which is why the file is erased with everything else when the Echo changes
# hands ("all" in reset.conf, and biscuit-factory-reset): this rule would
# otherwise carry the last owner's choice into a new owner's setup.
APPS_PENDING=/opt/persist/apps-pending
APPS_FILE=/usr/share/biscuit/apps.json
# What apply_apps started installing, for the closing log line.
apps_started=""

# The package names of the apps the portal offers, one per line, read the way
# its optional_packages() reads them. Empty when apps.json is missing or cannot
# be read, which is exactly when the portal offered no apps at all.
app_packages() {
	python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for app in json.load(fh).get("apps", []):
            if app.get("id"):
                for name in app.get("packages") or []:
                    print(name)
except Exception:
    pass
' "$APPS_FILE" 2>/dev/null
}

# Stdin: package names, separated by any whitespace. Stdout: each one, once and
# in order, that is spelled as apk spells a package (lower case, digits, ._+-,
# not starting with punctuation) AND is in the list in $1. Nothing else comes
# through, so the words can then be used unquoted: no spaces, no glob
# characters.
only_app_packages() {
	tr -s ' \t\r' '\n' | ALLOWED="$1" awk '
		BEGIN { n = split(ENVIRON["ALLOWED"], a, "\n"); for (i = 1; i <= n; i++) ok[a[i]] = 1 }
		/^[a-z0-9][a-z0-9._+-]*$/ && ($0 in ok) && !seen[$0]++
	'
}

# The words of $1 that are not among the words of $2.
words_without() {
	for _w in $1; do
		case " $(echo $2) " in
		*" $_w "*) ;;
		*) echo "$_w" ;;
		esac
	done
}

apply_apps() {
	apps_started=""
	[ -s "$RUNDIR/packages" ] || return 0
	_allowed=$(app_packages)
	# The portal writes one line per ticked app, holding that app's packages
	# separated by spaces.
	_ticked=$(only_app_packages "$_allowed" < "$RUNDIR/packages")
	if [ -z "$_ticked" ]; then
		log "ignored an app choice that names no app this device offers:" \
			"$(tr -s '\n\t' '  ' < "$RUNDIR/packages" | cut -c1-200)"
		return 0
	fi
	_earlier=""
	if [ -f "$APPS_PENDING" ]; then
		_earlier=$(only_app_packages "$_allowed" < "$APPS_PENDING")
	fi
	# One apk call for the lot; it prints the ones that are installed. If apk
	# cannot answer, everything counts as not installed, which is what setup
	# did before it asked.
	_installed=$(apk info -e $_ticked $_earlier 2>/dev/null)
	_new=$(words_without "$_ticked" "$_installed")
	_keep=$(words_without "$_earlier" "$_installed")
	for _p in $_new; do
		case " $(echo $_keep) " in
		*" $_p "*) ;;
		*) _keep="$_keep $_p" ;;
		esac
	done

	# Written before the job starts, so the job - or its first retry - always
	# finds the entry it is about to satisfy. Renamed into place, and synced on
	# both sides of the rename: an Echo is unplugged, not shut down, and the
	# rename can otherwise reach the disk before the list does.
	if [ -n "$(echo $_keep)" ]; then
		mkdir -p "${APPS_PENDING%/*}"
		if printf '%s\n' $_keep > "$APPS_PENDING.tmp" &&
			chmod 644 "$APPS_PENDING.tmp" && sync &&
			mv -f "$APPS_PENDING.tmp" "$APPS_PENDING"; then
			sync
			log "apps to install, kept in $APPS_PENDING until installed: $(echo $_keep)"
		else
			rm -f "$APPS_PENDING.tmp"
			log "WARNING: could not write $APPS_PENDING; a failed install would not be retried"
		fi
	fi

	_already=$(words_without "$_ticked" "$_new")
	if [ -n "$_already" ]; then
		log "chosen apps already installed: $(echo $_already)"
	fi
	[ -n "$_new" ] || return 0
	# The job is told it was started by setup, so it can say so.
	setsid env BISCUIT_PKGJOB_ORIGIN=setup /usr/bin/biscuit-pkgjob.sh add $_new </dev/null >/dev/null 2>&1 &
	apps_started=$(echo $_new)
}

# ---------------------------------------------------------------------------

# The settings page is paused for the length of EVERY setup session, and
# started again afterwards on whatever port setup leaves it. It listens on
# every address, so the open setup network could otherwise reach it - directly
# at 192.168.4.1, or at the house-network address through it - and try the
# owner's password from radio range, or the published user / 147147 on a device
# that has no owner yet. The portal does not need it: everything setup asks for
# is on the portal itself. (It used to be paused only when it was on port 80,
# which the portal needs, or when the default account was the only one.) The
# page refuses the setup network by itself as well, in case anything starts it
# while a session runs.
#
# The pause is recorded in a file, not a variable: a session killed before its
# EXIT trap runs (the service's stop sends KILL after five seconds) would
# otherwise take the fact with it, and the page would stay down until a
# reboot. biscuit-setup.initd's stop() restarts the page if the file is left.
PAUSED=$RUNDIR/settings-paused
settings_moved=no

settings_port() {
	_p=$(cat /opt/persist/settings-port 2>/dev/null)
	case "$_p" in ""|*[!0-9]*) _p=8080 ;; esac
	echo "$_p"
}

# Anything listening on the given TCP port, IPv4 or IPv6.
port_taken() {
	awk -v hex="$(printf '%04X' "$1")" \
		'$4 == "0A" && $2 ~ (":" hex "$") { found = 1 } END { exit !found }' \
		/proc/net/tcp /proc/net/tcp6 2>/dev/null
}

pause_settings() {
	_sp=$(settings_port)
	if [ "$_sp" = 80 ]; then
		_why="setup needs port 80"
	elif default_account_live; then
		_why="the only account has the published default password"
	else
		_why="the setup network is open"
	fi
	mkdir -p "$RUNDIR"
	# Stopped in whatever state it is in - "starting" after an earlier
	# session's restart included - and then waited for, until nothing holds
	# :80. A server stopped by someone else, with no marker of ours, is left
	# alone.
	rc-service biscuit-settings status >/dev/null 2>&1
	_st=$?
	[ $_st -eq 3 ] && [ ! -f "$PAUSED" ] && return 0
	: > "$PAUSED"
	# Free only once the service reads stopped AND nothing holds its port. A
	# stop is refused while an earlier session's restart is still starting it,
	# and the port is not bound yet at that moment - so the port alone said
	# "free" and the server took it a second later.
	_i=0
	while [ $_i -lt 20 ]; do
		rc-service biscuit-settings status >/dev/null 2>&1
		if [ $? -eq 3 ]; then
			port_taken "$_sp" || break
		else
			rc-service biscuit-settings stop >/dev/null 2>&1
		fi
		sleep 1
		_i=$((_i + 1))
	done
	if ! port_taken "$_sp"; then
		log "paused the settings page: $_why"
	elif [ "$_sp" = 80 ]; then
		log "port 80 is still taken; the setup page may not start"
	else
		log "port $_sp is still taken; the settings page may still be answering"
	fi
}

resume_settings() {
	if [ -f "$PAUSED" ] || [ "$settings_moved" = yes ]; then
		rm -f "$PAUSED"
		settings_moved=no
		setsid rc-service biscuit-settings restart </dev/null >/dev/null 2>&1 &
	fi
}

run_setup() {
	mkdir -p "$RUNDIR"
	# The wps marker is cleared here as well as by the portal. The portal
	# removes it whenever the box is unticked, which covers the ordinary retry,
	# but a marker left by a run that died between submission and join would
	# otherwise force WPS on the next attempt and ignore a typed passphrase.
	# The passphrase too: every pass needs a fresh submission, which writes it
	# again, so one found here can only be left over.
	rm -f "$RUNDIR/submitted" "$RUNDIR/result" "$RUNDIR/wps" "$RUNDIR/psk"

	scan_networks

	if ! start_ap; then
		stop_ap
		ring "play $SETUP_ERR_ANIM 4"
		earcon setup_error
		log "could not raise the setup AP"
		return 1
	fi

	# The window, in seconds of uptime rather than clock time: on a fresh
	# Echo the clock reads 2010 and may be stepped at any moment. The portal
	# moves the deadline on while the page is being used, up to MAX_WINDOW.
	_up=$(cut -d. -f1 /proc/uptime)
	echo "$_up" > "$RUNDIR/window_start"
	echo "$MAX_WINDOW" > "$RUNDIR/window_max"
	echo $((_up + WINDOW)) > "$RUNDIR/deadline"

	# Again, now that scanning and raising the AP have taken their time: a
	# settings start an earlier session left in flight has finished by now.
	pause_settings
	python3 "$PORTAL" >> "$LOG" 2>&1 &
	_portal=$!
	# It exits at once if it cannot take 192.168.4.1:80, and an open network
	# with no page behind it is a setup nobody can finish. Retried instead.
	sleep 2
	if ! kill -0 "$_portal" 2>/dev/null; then
		log "the setup page did not start (is port 80 taken?)"
		stop_ap
		ring "play $SETUP_ERR_ANIM 4"
		earcon setup_error
		return 1
	fi
	ring "play $SETUP_ANIM"
	earcon setup_mode
	log "portal listening on http://$ADDR/ - window ${WINDOW}s, up to ${MAX_WINDOW}s while in use"

	# Wait for a submission or for the window to close.
	while [ ! -f "$RUNDIR/submitted" ]; do
		_now=$(cut -d. -f1 /proc/uptime)
		_dl=$(cat "$RUNDIR/deadline" 2>/dev/null)
		case "$_dl" in ""|*[!0-9]*) _dl=0 ;; esac
		[ "$_now" -ge "$_dl" ] && break
		sleep 2
	done

	ring "stop $SETUP_ANIM"

	if [ ! -f "$RUNDIR/submitted" ]; then
		log "setup window expired with no submission"
		stop_ap
		return 2
	fi

	ssid=$(cat "$RUNDIR/ssid_choice" 2>/dev/null)
	# Read once, and deleted as soon as it is read, whatever the join then
	# does - as the account password is once the account exists. It used to
	# stay in /run until the next reboot: root-only, but the home Wi-Fi
	# passphrase in the clear, for as long as the device stayed up, when all
	# that is kept of it anywhere else is the key wpa_passphrase derives. A
	# failed join needs no copy either: the next pass asks for it again.
	psk=$(cat "$RUNDIR/psk" 2>/dev/null)
	rm -f "$RUNDIR/psk"
	create_account
	log "submission received for '$ssid'"

	# Give the portal a moment to finish serving the "connecting" page before
	# the radio it is being served over disappears.
	sleep 3
	stop_ap

	ring "play $WIFI_ANIM"
	join "$ssid" "$psk"
	_joined=$?
	psk=""
	if [ $_joined -eq 0 ]; then
		ring "stop $WIFI_ANIM"
		apply_extras
		ring "play $SETUP_OK_ANIM 4"
		earcon setup_success
		# The apps are only starting to install - and may not manage it yet -
		# so the line must not read as though they were done.
		if [ -n "$apps_started" ]; then
			log "setup complete; apps installing in the background: $apps_started"
		else
			log "setup complete"
		fi
		return 0
	fi

	ring "stop $WIFI_ANIM"
	ring "play $SETUP_ERR_ANIM 4"
	# Leave no half-written config behind: a conf with a bad network block would
	# make wifi.start burn its four attempts on it at every future boot.
	rm -f "$CONF"
	return 1
}

case "${1:-run}" in
run)
	mkdir -p "$RUNDIR"
	# One setup session at a time. wifi.start can ask for setup mode from more
	# than one place in a single boot (no config, then a failed join), and a
	# button or Home Assistant can ask while one is already running. Two
	# sessions would fight over the same radio and the same spool files.
	# mkdir, not flock: busybox has no flock.
	if ! mkdir "$LOCKDIR" 2>/dev/null; then
		log "setup already running; ignoring duplicate start"
		exit 0
	fi
	# The pidfile goes with the session: left behind, a later restart would
	# signal whatever process has since been given the same pid.
	trap 'resume_settings; forget_secrets; rm -f "$PIDFILE"; rmdir "$LOCKDIR" 2>/dev/null' EXIT
	# A signal ends the session, which runs the EXIT trap above. Carrying on
	# after one, as before, left the teardown to the KILL that follows.
	trap 'exit 143' INT TERM
	# Named by the session itself, and only once it holds the lock. It used
	# to be start-stop-daemon's --make-pidfile, in the same call that left
	# OpenRC a daemon record and so made every finished setup read "crashed"
	# (see biscuit-setup.initd) - and that wrote it before the lock was taken,
	# so a duplicate start, which exits just above, replaced the running
	# session's pid with its own. Renamed into place so a stop never reads it
	# half-written.
	echo $$ > "$PIDFILE.tmp" && mv -f "$PIDFILE.tmp" "$PIDFILE"
	# A new session starts with every app box clear; see below.
	rm -f "$RUNDIR/packages.last"
	# Fresh, so the pause below sees a password changed since boot.
	ssh_policy
	pause_settings
	# Retry the whole flow rather than giving up after one wrong password.
	# Each pass re-raises the AP so the phone can reconnect and read what went
	# wrong, which is the only feedback channel available without a screen.
	attempt=1
	while [ $attempt -le 3 ]; do
		run_setup
		rc=$?
		[ $rc -eq 0 ] && exit 0
		if [ $rc -eq 2 ]; then
			log "no submission; leaving the driver loaded. Restart the device to reopen setup (USB networking at 172.16.42.1 only if it was turned on under Connected devices, USB)"
			exit 0
		fi
		# Carry the reason into the next pass so the portal can show it.
		cp "$RUNDIR/result" "$RUNDIR/last_error" 2>/dev/null
		# And the apps that pass ticked, so the page offers them ticked again.
		# The done page had already promised them "once it is connected", and
		# a person retyping a password has no reason to think the boxes need
		# ticking twice - left clear, the apps were silently dropped. Only
		# after a pass that got a submission: one that failed to raise the
		# network has nothing new to carry.
		if [ -f "$RUNDIR/submitted" ]; then
			cp "$RUNDIR/packages" "$RUNDIR/packages.last" 2>/dev/null
		fi
		attempt=$((attempt + 1))
		log "restarting setup (attempt $attempt of 3)"
	done
	log "setup gave up after 3 attempts"
	exit 1
	;;
stop)
	stop_ap
	ring "stop $SETUP_ANIM"
	;;
*)
	echo "usage: $0 {run|stop}" >&2
	exit 2
	;;
esac
