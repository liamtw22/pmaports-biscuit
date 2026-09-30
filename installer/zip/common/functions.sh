# Shared by the biscuit TWRP zips. Each update-binary unpacks bin/ and common/
# into $WORK and then sources this. TWRP runs the zips on amonet v2's recovery:
# a 32-bit ARM kernel whose own dd cannot seek past 2 GiB, which is why every
# positioned read and write here goes through the zip's own static busybox.

DISK=/dev/block/mmcblk0
BB=$WORK/bin/busybox
BT=$WORK/bin/biscuit-tool
MKE2FS=/sbin/mke2fs     # TWRP's e2fsprogs; busybox's mke2fs only makes ext2

# Busybox's applets first on PATH: TWRP's own tools are 32-bit toybox builds,
# and some (dd) cannot address the whole eMMC.
mkdir -p $WORK/bin/applets
$BB --install -s $WORK/bin/applets 2>/dev/null
export PATH=$WORK/bin/applets:$PATH
# TWRP's internal storage. On the Echo it is userdata's media folder itself
# (Fire OS is not multi-user, so there is no media/0).
BACKUP_ROOT_SDCARD=/sdcard/biscuit-backup
BACKUP_ROOT_TMP=/tmp/biscuit-backup
# The backup's layout, recorded as format= in its info.txt:
#   1  raw/, assets/, earcon/. The backup zips before v1 wrote no format line;
#      the last of them (r292's) also wrote led/, which is used as it is.
#   2  adds led/ (always made, empty if Fire OS had none), made_by= and
#      led_count=, the number of animations in led/.
# The backup zip writes this one; every zip reads every format up to it.
BACKUP_FORMAT=2

# The amonet v2 stock layout's first twelve partitions. Every layout this
# project supports keeps them exactly here; biscuit-tool gpt-kind checks that
# before any of these numbers is used.
PREFIX="kb:2048:4095 dkb:4096:6143 lk_a:32768:34815 tee1:49152:59391
lk_b:65536:67583 tee2:81920:92159 expdb:98304:118783 misc:118784:119808
persist:131072:163839 boot_a:163840:196607 boot_b:196608:229375
recovery:229376:262143"
MERGED_FIRST=294912
STUB=2048

# TWRP hands us a file descriptor for its console. Some builds pass one that
# points into /tmp instead; then the real pipe is found the way amonet does it.
fix_outfd() {
	readlink /proc/$$/fd/$OUTFD 2>/dev/null | grep /tmp >/dev/null || return 0
	OUTFD=0
	for fd in $(ls /proc/$$/fd); do
		if readlink /proc/$$/fd/$fd 2>/dev/null | grep pipe >/dev/null; then
			if ps | grep " 3 $fd " | grep -v grep >/dev/null; then
				OUTFD=$fd
				return 0
			fi
		fi
	done
}

# What TWRP shows, also kept as plain text in $CONSOLE: an Echo has no screen,
# and adb sideload reports success whatever the zip printed.
CONSOLE=${LOG%.log}.txt
: > "$CONSOLE"
# This zip's own name and version (ZIP_NAME, ZIP_VERSION), written into it by
# build_zips.py.
. $WORK/zip.prop

ui_print() {
	echo -e "ui_print $1\nui_print" >> /proc/self/fd/$OUTFD
	echo "$1" >> "$LOG"
	echo "$1" >> "$CONSOLE"
}

# Every zip says first which zip it is: its name and version.
banner() {
	ui_print "$ZIP_NAME $ZIP_VERSION"
	ui_print "$(echo "$ZIP_NAME $ZIP_VERSION" | sed 's/./=/g')"
}

abort() {
	ui_print " "
	ui_print "(!) $1"
	ui_print " "
	ui_print "Log: $LOG  (adb pull $LOG)"
	exit 1
}

# Run a command; on failure, stop with the given message and its output.
run() {
	msg=$1
	shift
	if ! out=$("$@" 2>&1); then
		echo "$out" >> "$LOG"
		abort "$msg: $(echo "$out" | tail -1)"
	fi
	echo "$out" >> "$LOG"
}

check_device() {
	[ "$(getprop ro.product.device)" = biscuit ] || abort "This is not an Echo Dot 2 (biscuit)."
	[ -n "$(getprop ro.twrp.version)" ] || abort "Run this from TWRP."
	SERIAL=$(getprop ro.boot.serialno)
	[ -n "$SERIAL" ] || abort "Cannot read this Echo's serial number."
	[ -b $DISK ] || abort "No eMMC at $DISK."
}

# The layout kind, total sectors and last usable sector, from the partition
# table itself. Sets KIND, TOTAL, LAST.
read_layout() {
	info=$($BT gpt-kind $DISK 2>&1) || abort "Cannot read the partition table: $info"
	set -- $info
	KIND=$1
	TOTAL=$2
	LAST=$3
}

prefix_range() {
	for p in $PREFIX; do
		name=${p%%:*}
		rest=${p#*:}
		if [ "$name" = "$1" ]; then
			echo "${rest%%:*} ${rest#*:}"
			return 0
		fi
	done
	return 1
}

# Everything mounted from the eMMC, resolved: TWRP lists devices by their
# by-name path, so matching "/dev/block/mmcblk0" alone finds nothing.
unmount_all() {
	for pass in 1 2 3; do
		left=0
		while read -r dev point rest; do
			case "$dev" in /dev/block/*) ;; *) continue ;; esac
			case "$(readlink -f "$dev")" in
				/dev/block/mmcblk0*) umount "$point" 2>/dev/null; left=1 ;;
			esac
		done < /proc/mounts
		[ $left = 0 ] && return 0
	done
	while read -r dev point rest; do
		case "$(readlink -f "$dev" 2>/dev/null)" in
			/dev/block/mmcblk0*) abort "Could not unmount $point." ;;
		esac
	done < /proc/mounts
}

# Positioned eMMC access, in 512-byte sectors.
read_range() {   # OUT FIRST COUNT
	$BB dd if=$DISK of="$1" bs=512 skip="$2" count="$3" 2>>"$LOG" ||
		abort "Could not read sectors $2+$3."
}

sha_of_range() {   # FIRST COUNT
	$BB dd if=$DISK bs=512 skip="$1" count="$2" 2>/dev/null | $BB sha256sum | cut -d' ' -f1
}

sha_of_file() {
	$BB sha256sum "$1" | cut -d' ' -f1
}

# Write a file at a sector and read it back. Stops on any difference.
write_range() {   # FILE FIRST
	size=$($BB stat -c %s "$1")
	[ $((size % 512)) = 0 ] || abort "$1 is not a whole number of sectors."
	$BB dd if="$1" of=$DISK bs=512 seek="$2" conv=notrunc,fsync 2>>"$LOG" ||
		abort "Could not write $(basename "$1")."
	[ "$(sha_of_range "$2" $((size / 512)))" = "$(sha_of_file "$1")" ] ||
		abort "$(basename "$1") did not read back correctly."
}

# The zip's backup folder for this Echo, wherever it was left. Prints it.
find_backup() {
	for d in "$BACKUP_ROOT_TMP/$SERIAL" "$BACKUP_ROOT_TMP" "$BACKUP_ROOT_SDCARD/$SERIAL"; do
		if [ -f "$d/SHA256SUMS" ] && [ -f "$d/info.txt" ]; then
			echo "$d"
			return 0
		fi
	done
	return 1
}

# Every file the backup lists must still match; it must be this Echo's; and
# its format one this zip reads. Sets FORMAT.
verify_backup() {
	( cd "$1" && $BB sha256sum -c SHA256SUMS >> "$LOG" 2>&1 ) ||
		abort "The backup in $1 does not match its SHA256SUMS. Copy it again."
	grep -qx "serial=$SERIAL" "$1/info.txt" ||
		abort "The backup in $1 belongs to another Echo, not $SERIAL."
	# No format line: the first backup zip's, format 1.
	FORMAT=$(sed -n 's/^format=//p' "$1/info.txt")
	case "$FORMAT" in
		'') FORMAT=1 ;;
		*[!0-9]*) abort "The backup in $1 has an unreadable format line: '$FORMAT'." ;;
	esac
	[ "$FORMAT" -ge 1 ] ||
		abort "The backup in $1 has an unreadable format line: '$FORMAT'."
	[ "$FORMAT" -le $BACKUP_FORMAT ] ||
		abort "The backup in $1 is format $FORMAT, newer than this zip reads ($BACKUP_FORMAT). Use a newer $ZIP_NAME."
	ui_print "  backup format $FORMAT$(sed -n 's/^made_by=/, made by /p' "$1/info.txt")"
}

# Whether Fire OS on this Echo still holds ring animations, in either system
# slot. Only meaningful on the stock layout.
fireos_has_led() {
	for slot in system_a system_b; do
		dev=$(by_name $slot) || continue
		mnt=$WORK/fos-$slot
		mkdir -p $mnt
		mount -t ext4 -o ro "$dev" $mnt >> "$LOG" 2>&1 || continue
		found=$(ls "$mnt"/system/etc/led-resources/*.animation "$mnt"/etc/led-resources/*.animation 2>/dev/null | head -1)
		umount $mnt
		[ -n "$found" ] && return 0
	done
	return 1
}

# Whether to take the ring's animations from the backup (after verify_backup
# and read_layout). Whatever the format, they are there when SHA256SUMS lists
# led/ files: r292's backup zip wrote them with no format line. An led/ that
# lists none is empty, not damaged (a copy may even drop the empty folder),
# unless info.txt counts some. A backup without them stops the install while
# Fire OS still holds them, since installing erases the only copy, unless the
# owner has made NO_LED_OK in the backup folder.
backup_has_led() {
	if [ -d "$1/led" ] && grep -q ' \./led/' "$1/SHA256SUMS"; then
		return 0
	fi
	count=$(sed -n 's/^led_count=//p' "$1/info.txt")
	case "$count" in
		''|0) ;;
		*[!0-9]*) abort "The backup in $1 has an unreadable led_count line: '$count'." ;;
		*) abort "The backup in $1 counts $count ring animations but lists none. Copy it again." ;;
	esac
	if [ "$KIND" = v2_stock_geometry ] && fireos_has_led; then
		if [ ! -f "$1/NO_LED_OK" ]; then
			ui_print " "
			ui_print "This backup has no ring animations, but Fire OS here still has"
			ui_print "them, and installing erases the only copy. Take them first:"
			ui_print "  1. flash amazon-biscuit-backup again now"
			ui_print "  2. copy it off: adb pull /sdcard/biscuit-backup"
			ui_print "  3. adb shell touch /sdcard/biscuit-backup/$SERIAL/COPIED"
			case "$1" in
				"$BACKUP_ROOT_TMP"*) ui_print "  4. adb shell rm -r $1 (this zip reads /tmp first)" ;;
			esac
			ui_print "  then install again."
			ui_print "To install without them: adb shell touch $1/NO_LED_OK"
			abort "The backup has no ring animations. Nothing was changed."
		fi
		ui_print "  (NO_LED_OK: installing without the ring animations. Fire OS's"
		ui_print "   copies are erased; only animation files you already have can be"
		ui_print "   imported later, from Settings > Storage > Files from stock)"
		return 1
	fi
	if [ "$KIND" = v2_merged_geometry ]; then
		ui_print "  (this backup has no ring animations: the settings store keeps"
		ui_print "   the ones it has)"
	else
		ui_print "  (this backup has no ring animations, and Fire OS here has none"
		ui_print "   to lose. Animation files you have can be imported later, from"
		ui_print "   Settings > Storage > Files from stock)"
	fi
	return 1
}

# The ring animations a backup's led/ folder holds that are stored: all but
# the names this Echo draws itself (the volume ramp) and its own act_/fx_
# files, as the settings page's import leaves them out.
led_wanted() {
	case "${1##*/}" in
		act_*|fx_*|volume_step-*|volume-muted*) return 1 ;;
		*.animation) return 0 ;;
	esac
	return 1
}

by_name() {
	for d in /dev/block/by-name /dev/block/platform/bootdevice/by-name /dev/block/platform/mtk-msdc.0/by-name; do
		[ -b "$d/$1" ] && { echo "$d/$1"; return 0; }
	done
	return 1
}

# Fire OS's storage as TWRP presents it. Mounts it if TWRP has not.
mount_storage() {
	mountpoint -q /sdcard && return 0
	mountpoint -q /data || mount /data >> "$LOG" 2>&1
	mountpoint -q /sdcard || mount /sdcard >> "$LOG" 2>&1
	mountpoint -q /sdcard
}
