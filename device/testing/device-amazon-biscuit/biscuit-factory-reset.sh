#!/bin/sh
# Factory reset: forget the network, the paired devices and the chosen name,
# and drop back into first-boot setup.
#
#   biscuit-factory-reset          ask first
#   biscuit-factory-reset --yes    do not ask
#   biscuit-factory-reset --list   show what would be erased, change nothing
#
# WHAT THIS IS FOR, and the distinction that matters:
#
#   An UPDATE must keep the user's Wi-Fi and Bluetooth. A FACTORY RESET must
#   not. Those pull in opposite directions, so the state lives on /opt
#   (partition 14, the old Android `cache`) rather than in the rootfs:
#
#     - flashing an update writes boot_a and userdata only, so /opt survives
#       and the device comes back on the same network with the same pairings
#     - a factory reset erases /opt/persist, and the setup service raises the
#       onboarding AP again because no wpa_supplicant.conf exists
#
#   The rootfs paths are bind mounts onto /opt/persist (see /etc/fstab), so
#   nothing needs to know where the state really lives.
#
# NOT erased: per-unit calibration in the `persist` partition (p9), which holds
# the Wi-Fi MAC and factory data. That is not ours to reset, and a device that
# lost it would need re-provisioning.
set -e

STORE=/opt/persist
ASSUME_YES=no
LIST_ONLY=no

for a in "$@"; do
	case "$a" in
	--yes|-y) ASSUME_YES=yes ;;
	--list|-l) LIST_ONLY=yes ;;
	*) echo "usage: $0 [--yes] [--list]" >&2; exit 1 ;;
	esac
done

if [ ! -d "$STORE" ]; then
	echo "factory-reset: $STORE is missing - is /opt mounted?" >&2
	exit 1
fi

echo "This device will forget:"
if [ -s "$STORE/wpa_supplicant/wpa_supplicant.conf" ]; then
	ssid=$(sed -n 's/^[[:space:]]*ssid="\(.*\)"/\1/p' \
		"$STORE/wpa_supplicant/wpa_supplicant.conf" 2>/dev/null | head -1)
	echo "  Wi-Fi network      ${ssid:-(configured)}"
else
	echo "  Wi-Fi network      (none configured)"
fi
n=$(find "$STORE/bluetooth" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | wc -l)
echo "  Bluetooth pairings $n"
[ -s "$STORE/etc/hostname" ] && echo "  device name        $(cat "$STORE/etc/hostname")"
k=$(cat "$STORE"/ssh/users/*/authorized_keys 2>/dev/null | grep -c . || true)
echo "  SSH keys           ${k:-0}"
[ -f "$STORE/provisioned" ] && echo "  the owner account  $(cat "$STORE/provisioned" 2>/dev/null)"
[ -s "$STORE/apps-pending" ] && echo "  apps to install    $(tr -s '\n' ' ' < "$STORE/apps-pending" 2>/dev/null)"

if [ "$LIST_ONLY" = yes ]; then
	exit 0
fi

if [ "$ASSUME_YES" != yes ]; then
	printf "\nErase all of it and reboot into setup? [y/N] "
	read -r reply
	case "$reply" in
	y|Y|yes|YES) ;;
	*) echo "cancelled"; exit 1 ;;
	esac
fi

# Stop the services that hold this state open, so nothing rewrites it on the
# way out. Failures are not fatal: a service that is already stopped is fine.
# The settings page among them: its retry of the apps chosen at setup rewrites
# apps-pending, and a rewrite that had read the list just before the rm below
# would put the last owner's choice back, synced, for the next owner. Nothing
# needs it, so stopping it stops nothing else; this script is not run from it.
rc-service wpa_supplicant stop >/dev/null 2>&1 || true
rc-service bluetooth stop >/dev/null 2>&1 || true
rc-service biscuit-settings stop >/dev/null 2>&1 || true

# Empty the directories rather than removing them: they are bind-mount targets,
# and removing a mount point out from under a live bind mount leaves the rootfs
# path dangling until the next boot.
rm -rf "$STORE"/wpa_supplicant/wpa_supplicant.conf
rm -rf "$STORE"/bluetooth/* "$STORE"/bluetooth/.[!.]* 2>/dev/null || true
rm -f "$STORE"/etc/hostname

# The owner's SSH keys are credentials, exactly like the Wi-Fi PSK and the
# Bluetooth link keys above. Leaving them behind means a "reset" device still
# admits whoever set it up last - which is the one thing a factory reset is
# supposed to prevent when a device changes hands.
#
# The HOST keys under ssh/host are deliberately kept: they identify the device,
# not its owner, and regenerating them would make every client that has ever
# connected report REMOTE HOST IDENTIFICATION HAS CHANGED for no security gain.
for _d in "$STORE"/ssh/users/*; do
	[ -d "$_d" ] || continue
	rm -f "$_d"/authorized_keys "$_d"/known_hosts
done

# WITHOUT THIS, A RESET DEVICE NEVER ASKS FOR A NEW OWNER.
#
# The setup AP is raised on the absence of wpa_supplicant.conf, so onboarding
# did run after a reset - but create_account() returns early when this marker
# exists, so setup silently kept the previous owner's account and the new
# person was never asked for one. Network onboarding without account
# onboarding is the wrong half to keep.
rm -f "$STORE"/provisioned

# The apps the last owner ticked at setup and that are not installed yet. The
# settings page keeps trying to install what this names, and a setup where
# nothing is ticked leaves it alone - so kept, it would install the last
# owner's choice for the next one, who was never asked. (The settings page's
# "erase everything" takes it too; see /usr/share/biscuit/reset.conf.)
rm -f "$STORE"/apps-pending

echo "erased; rebooting into setup"
sync
reboot
