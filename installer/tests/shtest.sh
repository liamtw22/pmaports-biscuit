#!/bin/sh
# Host test of the zips' shell logic: backup format, ring animations, asset
# profiles, the install's order. Usage: SHELL tests/shtest.sh [SRC]
# SRC is the zip folder, installer/zip by default. Needs a Linux host with
# /usr/bin/busybox; run it under dash, busybox ash and bash --posix.
# Sources SRC/common/functions.sh with the host busybox, then runs the real
# update-binary sections (cut out by marker) against fabricated backups.
SRC=$(cd "${1:-$(dirname "$0")/../zip}" && pwd)
T=$(mktemp -d)
WORK=$T/work
mkdir -p $WORK/bin $WORK/common
cp /usr/bin/busybox $WORK/bin/busybox
cp $SRC/common/functions.sh $WORK/common/
printf 'ZIP_NAME=amazon-biscuit-restore\nZIP_VERSION=v1\n' > $WORK/zip.prop
LOG=$T/test.log
OUTFD=1
SERIAL=TESTSERIAL
. $WORK/common/functions.sh
umount() { :; }
# Whether "Fire OS here" still holds ring animations: FOS_LED=1 or 0.
FOS_LED=0
fireos_has_led() { [ "$FOS_LED" = 1 ]; }
KIND=v2_merged_geometry
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; fails=$((fails + 1)); }

# cut FILE START_RE END_RE: the lines from the first START match to the next END match
cut_section() {
	awk -v s="$2" -v e="$3" 'f == 0 && $0 ~ s { f = 1 } f == 1 { print } f == 1 && $0 ~ e { exit }' "$1"
}

# mk_backup DIR FORMAT(none|value) LED(yes|no|empty) [SERIAL]
# A format 2 backup gets led_count= as the backup zip writes it, unless
# NOCOUNT=1; LEDCOUNT=n overrides the number.
mk_backup() {
	d=$1
	rm -rf "$d"
	mkdir -p "$d/raw" "$d/assets/firmware/mediatek" "$d/earcon"
	echo fw > "$d/assets/firmware/WIFI_RAM_CODE"
	if [ "${SPEAKER:-0}" = 1 ]; then
		mkdir -p "$d/assets/speaker"
		echo "-0.001," > "$d/assets/speaker/EQ_50.cfg"
	fi
	echo src > "$d/assets/SOURCES"
	echo snd > "$d/earcon/wake.mp3"
	case "$3" in
		yes)
			mkdir -p "$d/led"
			for n in a b+c.x volume_step-01 volume-muted act_listening fx_preview; do
				echo "anim $n new" > "$d/led/$n.animation"
			done
			echo notes > "$d/led/layers.txt" ;;
		empty) mkdir -p "$d/led" ;;
	esac
	{
		echo "serial=${4:-$SERIAL}"
		case "$2" in
			none) ;;
			dup) echo "format=2"; echo "format=2" ;;
			*) echo "format=$2" ;;
		esac
		[ "$2" = none ] || echo "made_by=amazon-biscuit-backup v1"
		if [ "$2" = 2 ] && [ "${NOCOUNT:-0}" = 0 ]; then
			echo "led_count=${LEDCOUNT:-$(ls "$d/led" 2>/dev/null | wc -l | tr -d ' ')}"
		fi
		echo "layout=v2_stock_geometry"
	} > "$d/info.txt"
	( cd "$d" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS )
}

# expect NAME RC OUTPUT_SUBSTRING -- command...
expect() {
	name=$1 want=$2 grepfor=$3
	shift 4
	out=$( ( "$@" ) 2>&1 )
	rc=$?
	if [ $rc = "$want" ] && echo "$out" | grep -q -- "$grepfor"; then pass "$name"; else fail "$name (rc=$rc): $out"; fi
}

check_fmt() {   # DIR
	verify_backup "$1"
	echo "FORMAT=$FORMAT"
	if backup_has_led "$1"; then echo LED=1; else echo LED=0; fi
}

B=$T/bk
mk_backup $B none no;    expect "format 1 (no line), no led" 0 "FORMAT=1" -- check_fmt $B
out=$( ( check_fmt $B ) 2>&1 ); echo "$out" | grep -q "LED=0" && echo "$out" | grep -q "the settings store keeps" && pass "format 1 on postmarketOS's layout: store keeps its own" || fail "format 1 merged message: $out"
mk_backup $B none yes;   expect "r292-style backup (no format line, led files) uses them" 0 "LED=1" -- check_fmt $B
out=$( ( KIND=v2_stock_geometry; FOS_LED=1; check_fmt $B ) 2>&1 ); echo "$out" | grep -q "LED=1" && pass "r292-style backup on stock with Fire OS animations: used, no stop" || fail "r292 stock: $out"
mk_backup $B 2 yes;      expect "format 2 with led" 0 "LED=1" -- check_fmt $B
out=$( ( check_fmt $B ) 2>&1 ); echo "$out" | grep -q "backup format 2, made by amazon-biscuit-backup v1" && pass "format 2 prints made_by" || fail "made_by: $out"
mk_backup $B 2 empty;    expect "format 2 with empty led (led_count=0) on postmarketOS's layout" 0 "LED=0" -- check_fmt $B
mk_backup $B 2 empty; rmdir $B/led
                         expect "format 2 whose empty led was dropped by the copy proceeds" 0 "LED=0" -- check_fmt $B
NOCOUNT=1; mk_backup $B 2 empty; rmdir $B/led; NOCOUNT=0
                         expect "format 2 without led_count or led proceeds" 0 "LED=0" -- check_fmt $B
LEDCOUNT=5; mk_backup $B 2 empty; unset LEDCOUNT
                         expect "format 2 counting 5 but listing none aborts" 1 "counts 5 ring animations but lists none. Copy it again" -- check_fmt $B
LEDCOUNT=x5; mk_backup $B 2 empty; unset LEDCOUNT
                         expect "format 2 with unreadable led_count aborts" 1 "unreadable led_count line: 'x5'" -- check_fmt $B
mk_backup $B 3 yes;      expect "format 3 aborts" 1 "newer than this zip reads (2). Use a newer amazon-biscuit-restore" -- check_fmt $B
mk_backup $B abc yes;    expect "non-numeric format aborts" 1 "unreadable format line: 'abc'" -- check_fmt $B
mk_backup $B 2x yes;     expect "format 2x aborts" 1 "unreadable format line" -- check_fmt $B
mk_backup $B 0 yes;      expect "format 0 aborts" 1 "unreadable format line: '0'" -- check_fmt $B
mk_backup $B dup yes;    expect "two format lines abort" 1 "unreadable format line" -- check_fmt $B
mk_backup $B "" yes;     expect "empty format= is format 1" 0 "FORMAT=1" -- check_fmt $B
mk_backup $B 2 yes OTHER; expect "another Echo's backup aborts" 1 "belongs to another Echo" -- check_fmt $B
mk_backup $B 2 yes; echo tampered >> $B/led/a.animation
                         expect "tampered backup aborts" 1 "does not match its SHA256SUMS" -- check_fmt $B
mk_backup $B 2 yes; rm -r $B/led
                         expect "format 2 whose listed led files are gone aborts" 1 "does not match its SHA256SUMS" -- check_fmt $B

# --- the install's stop: no animations in the backup, Fire OS still has them ---
stock_fmt() {   # DIR FOS_LED
	KIND=v2_stock_geometry
	FOS_LED=$2
	check_fmt "$1"
}
mk_backup $B none no
out=$( ( stock_fmt $B 1 ) 2>&1 ); rc=$?
if [ $rc = 1 ] && echo "$out" | grep -q "(!) The backup has no ring animations. Nothing was changed." &&
   echo "$out" | grep -q "flash amazon-biscuit-backup again now" &&
   echo "$out" | grep -q "adb shell touch /sdcard/biscuit-backup/TESTSERIAL/COPIED" &&
   echo "$out" | grep -q "adb shell touch $B/NO_LED_OK" && ! echo "$out" | grep -q "LED="; then
	pass "format 1 without led on stock with Fire OS animations: stops, says what to do"
else
	fail "format 1 stock stop (rc=$rc): $out"
fi
touch $B/NO_LED_OK
out=$( ( stock_fmt $B 1 ) 2>&1 ); rc=$?
[ $rc = 0 ] && echo "$out" | grep -q "LED=0" && echo "$out" | grep -q "NO_LED_OK: installing without" &&
	pass "same with NO_LED_OK: proceeds without them" || fail "NO_LED_OK (rc=$rc): $out"
mk_backup $B none no
out=$( ( stock_fmt $B 0 ) 2>&1 ); rc=$?
[ $rc = 0 ] && echo "$out" | grep -q "LED=0" && echo "$out" | grep -q "Fire OS here has none" && echo "$out" | grep -q "Files from stock" &&
	pass "reinstall after stock-restore (stock layout, no Fire OS animations): proceeds" || fail "stock no fos (rc=$rc): $out"
out=$( ( KIND=v2_merged_geometry; FOS_LED=1; check_fmt $B ) 2>&1 ); rc=$?
[ $rc = 0 ] && echo "$out" | grep -q "LED=0" && echo "$out" | grep -q "the settings store keeps" &&
	pass "reinstall on postmarketOS's layout: proceeds, never looks at Fire OS" || fail "merged (rc=$rc): $out"
mk_backup $B 2 empty
out=$( ( stock_fmt $B 0 ) 2>&1 ); rc=$?
[ $rc = 0 ] && echo "$out" | grep -q "LED=0" && pass "format 2, empty led, led_count=0, stock: proceeds" || fail "fmt2 empty stock (rc=$rc): $out"
expect "format 2 empty led while Fire OS has animations: stops" 1 "Nothing was changed" -- stock_fmt $B 1
# A backup in /tmp is read before a new one on /sdcard: the stop says to remove it.
BACKUP_ROOT_TMP=$T
mk_backup $B none no
expect "a /tmp backup's stop says to remove it" 1 "adb shell rm -r $B (this zip reads /tmp first)" -- stock_fmt $B 1
BACKUP_ROOT_TMP=/tmp/biscuit-backup
KIND=v2_merged_geometry
FOS_LED=0

# --- fireos_has_led itself, with by_name and mount faked ---------------------------
fos_test() {   # A_CONTENT B_CONTENT  (led|none|bad)
	F=$T/fos
	rm -rf $F
	for s in system_a system_b; do
		mkdir -p $F/$s
		case $s in system_a) c=$1 ;; *) c=$2 ;; esac
		case $c in
			led) mkdir -p $F/$s/system/etc/led-resources; echo x > $F/$s/system/etc/led-resources/wake.animation ;;
			led2) mkdir -p $F/$s/etc/led-resources; echo x > $F/$s/etc/led-resources/wake.animation ;;
			other) mkdir -p $F/$s/system/etc/led-resources; echo x > $F/$s/system/etc/led-resources/readme.txt ;;
			bad) echo bad > $F/$s/BAD ;;
		esac
	done
	by_name() { echo "$T/fos/$1"; }
	mount() { [ -e "$5/BAD" ] && return 1; cp -r "$5"/. "$6"/; }
	umount() { rm -rf "$1"; mkdir -p "$1"; }
	. $WORK/common/functions.sh
	unset -f by_name
	by_name() { echo "$T/fos/$1"; }
	fireos_has_led
}
( fos_test led none ) && pass "fireos_has_led: system_a/system/etc" || fail "fireos_has_led system_a"
( fos_test bad led2 ) && pass "fireos_has_led: system_b/etc, system_a unmountable" || fail "fireos_has_led system_b"
( fos_test none other ) && fail "fireos_has_led: no .animation files" || pass "fireos_has_led: none without .animation files"
( fos_test bad bad ) && fail "fireos_has_led: nothing mounts" || pass "fireos_has_led: none when nothing mounts"

for n in a.animation b+c.x.animation; do led_wanted "/x/led/$n" && pass "led_wanted $n" || fail "led_wanted $n"; done
for n in act_mute.animation fx_off.animation volume_step-03.animation volume-muted.animation layers.txt; do
	led_wanted "/x/led/$n" && fail "led_wanted refuses $n" || pass "led_wanted refuses $n"
done

# --- the backup zip's info.txt: led_count ------------------------------------------
INFO_SEC=$T/backup-info.sh
cut_section $SRC/backup/update-binary '^leds=' '^} > "[$]PART/info.txt"' > $INFO_SEC
grep -q 'led_count=' $INFO_SEC && pass "backup info section cut" || fail "backup info section cut"
getprop() { echo 3.2.3; }
for n in 0 3; do
	PART=$T/part
	rm -rf $PART; mkdir -p $PART/led
	i=0; while [ $i -lt $n ]; do echo x > $PART/led/l$i.animation; i=$((i + 1)); done
	out=$( ( ZIP_NAME=amazon-biscuit-backup; BACKUP_FORMAT=2; . $INFO_SEC ) 2>&1 )
	grep -qx "led_count=$n" $PART/info.txt && echo "$out" | grep -q "ring animations: $n file(s)" &&
		pass "backup writes led_count=$n" || fail "backup led_count=$n: $out / $(cat $PART/info.txt)"
done
unset -f getprop

# --- the restore zip's store section ---------------------------------------------
RESTORE=$T/restore-section.sh
cut_section $SRC/restore/update-binary '^S=[$]WORK/opt/persist' 'ring animations: ' > $RESTORE
grep -q 'led.new' $RESTORE && pass "restore section cut" || fail "restore section cut"

mk_store() {
	rm -rf $WORK/opt
	mkdir -p $WORK/opt/persist/biscuit/led $WORK/opt/persist/biscuit/assets/firmware $WORK/opt/persist/earcon
	echo oldfw > $WORK/opt/persist/biscuit/assets/firmware/WIFI_RAM_CODE
	echo oldsnd > $WORK/opt/persist/earcon/old.mp3
	echo "anim a old" > $WORK/opt/persist/biscuit/led/a.animation
	echo "fos5 only" > $WORK/opt/persist/biscuit/led/extra-fos5.animation
	echo stale > $WORK/opt/persist/biscuit/led/volume_step-09.animation
}
run_restore() {   # BACKUP LED
	BACKUP=$1
	LED=$2
	. $RESTORE
	echo "restore done"
}
L=$WORK/opt/persist/biscuit/led
mk_backup $B 2 yes; mk_store
out=$( ( run_restore $B 1 ) 2>&1 )
if echo "$out" | grep -q "restore done" &&
   [ "$(cat $L/a.animation)" = "anim a new" ] &&
   [ -f "$L/b+c.x.animation" ] && [ "$(cat $L/extra-fos5.animation)" = "fos5 only" ] &&
   [ ! -e $L/volume_step-09.animation ] && [ ! -e $L/volume_step-01.animation ] &&
   [ ! -e $L/act_listening.animation ] && [ ! -e $L/fx_preview.animation ] && [ ! -e $L/volume-muted.animation ] &&
   [ ! -e $L/layers.txt ] &&
   [ "$(cat $WORK/opt/persist/biscuit/assets/firmware/WIFI_RAM_CODE)" = fw ] &&
   [ ! -e $WORK/opt/persist/biscuit/assets/SOURCES ] &&
   [ -z "$(ls -d $WORK/opt/persist/biscuit/*.new $WORK/opt/persist/biscuit/*.old $WORK/opt/persist/*.new $WORK/opt/persist/*.old 2>/dev/null)" ] &&
   echo "$out" | grep -q "ring animations: 3 file(s)"; then
	pass "restore format 2: led swapped in, filtered, extras kept, stale ramp dropped"
else
	fail "restore format 2: $out / $(ls -a $L)"
fi

mk_backup $B none yes; mk_store
out=$( ( verify_backup $B; backup_has_led $B && LED=1 || LED=0; run_restore $B $LED ) 2>&1 )
if echo "$out" | grep -q "restore done" && [ "$(cat $L/a.animation)" = "anim a new" ] && [ -f "$L/b+c.x.animation" ]; then
	pass "restore r292-style backup: its led is used"
else
	fail "restore r292-style: $out"
fi

mk_backup $B none no; mk_store
out=$( ( run_restore $B 0 ) 2>&1 )
if echo "$out" | grep -q "restore done" && [ "$(cat $L/a.animation)" = "anim a old" ] &&
   [ -f $L/volume_step-09.animation ] && [ ! -e $WORK/opt/persist/biscuit/led.new ]; then
	pass "restore format 1: the store's led is left alone"
else
	fail "restore format 1: $out"
fi

mk_backup $B 2 empty; mk_store
out=$( ( verify_backup $B; backup_has_led $B && LED=1 || LED=0; run_restore $B $LED ) 2>&1 )
if echo "$out" | grep -q "restore done" && [ "$(cat $L/a.animation)" = "anim a old" ] && [ ! -e $WORK/opt/persist/biscuit/led.new ]; then
	pass "restore format 2 with an empty led: the store's led is left alone"
else
	fail "restore empty led: $out"
fi

mk_backup $B 2 yes; echo corrupt >> "$B/led/b+c.x.animation"; mk_store
out=$( ( run_restore $B 1 ) 2>&1 )
rc=$?
if [ $rc = 1 ] && echo "$out" | grep -q "led/b+c.x.animation did not copy correctly; the store was not changed" &&
   [ "$(cat $L/a.animation)" = "anim a old" ] && [ "$(cat $WORK/opt/persist/biscuit/assets/firmware/WIFI_RAM_CODE)" = oldfw ] &&
   [ ! -e $WORK/opt/persist/biscuit/led.new ] && [ ! -e $WORK/opt/persist/biscuit/assets.new ]; then
	pass "restore: a led file that does not match aborts and cleans up"
else
	fail "restore mismatch (rc=$rc): $out"
fi

# --- the install zip's staging and led store sections ----------------------------
STAGE_SEC=$T/install-stage.sh
cut_section $SRC/install/update-binary '^STAGE=[$]WORK/stage' 'sha256sum -c >> [$]LOG' > $STAGE_SEC
cut_section $SRC/install/update-binary '^if . -d .STAGE/led' '^fi$' >> $STAGE_SEC
grep -q 'dirs=' $STAGE_SEC && grep -q 'led_wanted' $STAGE_SEC && pass "install sections cut" || fail "install sections cut"
run_install() {   # BACKUP LED
	BACKUP=$1
	LED=$2
	rm -rf $WORK/stage $WORK/opt
	mkdir -p $WORK/opt/persist/biscuit
	. $STAGE_SEC
	echo "stage done"
}
mk_backup $B 2 yes
out=$( ( run_install $B 1 ) 2>&1 )
if echo "$out" | grep -q "stage done" && [ -f "$L/b+c.x.animation" ] && [ -f $L/a.animation ] &&
   [ "$(ls $L | wc -l)" = 2 ]; then
	pass "install format 2: led staged, checked and stored with the filter"
else
	fail "install format 2: $out / $(ls $L 2>&1)"
fi
mk_backup $B none yes
out=$( ( KIND=v2_stock_geometry; FOS_LED=1; verify_backup $B; backup_has_led $B && LED=1 || LED=0; run_install $B $LED ) 2>&1 )
if echo "$out" | grep -q "stage done" && [ -f "$L/b+c.x.animation" ] && [ -f $L/a.animation ] && [ "$(ls $L | wc -l)" = 2 ]; then
	pass "install r292-style backup: its led is staged and stored"
else
	fail "install r292-style: $out / $(ls $L 2>&1)"
fi
mk_backup $B none no; touch $B/NO_LED_OK
out=$( ( KIND=v2_stock_geometry; FOS_LED=1; verify_backup $B; backup_has_led $B && LED=1 || LED=0; run_install $B $LED ) 2>&1 )
if echo "$out" | grep -q "stage done" && [ ! -d $WORK/stage/led ] && [ ! -d $L ]; then
	pass "install format 1 with NO_LED_OK: no ring animations, checks pass"
else
	fail "install NO_LED_OK: $out"
fi
mk_backup $B 2 empty
out=$( ( KIND=v2_stock_geometry; verify_backup $B; backup_has_led $B && LED=1 || LED=0; run_install $B $LED ) 2>&1 )
if echo "$out" | grep -q "stage done" && [ ! -d $L ]; then
	pass "install format 2 with an empty led: checks pass, nothing stored"
else
	fail "install empty led: $out"
fi
mk_backup $B 2 yes; echo corrupt >> $B/led/a.animation
out=$( ( run_install $B 1 ) 2>&1 )
[ $? = 1 ] && echo "$out" | grep -q "does not match the backup" && pass "install: a led file that does not match aborts" || fail "install mismatch: $out"

# --- r294: the speaker correction curve (speaker profile, EQ_50.cfg) --------------
# The backup's collection section, run against a fake Fire OS 6 system_a whose
# audio-algorithms holds EQ_50.cfg and the identical EQ_60.cfg, and an
# assets.list with a speaker line as build_zips makes it from the manifest.
COLLECT=$T/backup-collect.sh
cut_section $SRC/backup/update-binary '^sizes=[$][(]cut' '^ui_print "  ring animations' > $COLLECT
grep -q 'take()' $COLLECT && grep -q 'speaker' $COLLECT && pass "backup collect section cut" || fail "backup collect section cut"
collect_test() {
	F=$T/fos6
	rm -rf $F $T/part
	mkdir -p $F/system_a/system/vendor/etc/audio-algorithms $F/system_a/system/vendor/firmware $F/system_b
	A=$F/system_a/system/vendor/etc/audio-algorithms
	i=0; while [ $i -lt 40 ]; do echo "0.0$i,"; i=$((i + 1)); done > $A/EQ_50.cfg
	cp $A/EQ_50.cfg $A/EQ_60.cfg
	echo "unrelated" > $A/AFE_other.cfg
	echo wifi > $F/system_a/system/vendor/firmware/WIFI_RAM_CODE
	eqsz=$(wc -c < $A/EQ_50.cfg | tr -d ' '); eqh=$(sha256sum < $A/EQ_50.cfg | cut -d' ' -f1)
	fwsz=$(wc -c < $F/system_a/system/vendor/firmware/WIFI_RAM_CODE | tr -d ' ')
	fwh=$(sha256sum < $F/system_a/system/vendor/firmware/WIFI_RAM_CODE | cut -d' ' -f1)
	printf 'firmware|WIFI_RAM_CODE|%s|%s\nspeaker|EQ_50.cfg|%s|%s\n' $fwsz $fwh $eqsz $eqh > $WORK/assets.list
	PART=$T/part
	mkdir -p $PART/raw $PART/assets $PART/earcon $PART/led
	by_name() { case $1 in system_a|system_b) echo "$T/fos6/$1" ;; *) return 1 ;; esac; }
	mount() { cp -r "$5"/. "$6"/; }
	umount() { rm -rf "$1"; }
	BT=false
	. $COLLECT
}
out=$( ( collect_test ) 2>&1 )
P=$T/part/assets
if [ -f $P/speaker/EQ_50.cfg ] && cmp -s $P/speaker/EQ_50.cfg $T/fos6/system_a/system/vendor/etc/audio-algorithms/EQ_50.cfg &&
   [ "$(ls $P/speaker)" = EQ_50.cfg ] && [ -f $P/firmware/WIFI_RAM_CODE ] &&
   grep -q '^speaker/EQ_50.cfg <- system_a:/system/vendor/etc/audio-algorithms/EQ_' $P/SOURCES &&
   ! grep -q AFE_other $P/SOURCES &&
   echo "$out" | grep -q "speaker: 1 file(s)" && echo "$out" | grep -q "firmware: 1 file(s)"; then
	pass "backup collects EQ_50.cfg (by content, once) into assets/speaker and reports it"
else
	fail "backup speaker collect: $out / $(ls -R $P 2>&1) / $(cat $P/SOURCES 2>&1)"
fi
# The same with an assets.list that has no speaker line (an r293 manifest):
# nothing is taken and nothing reported.
out=$( ( collect_test; true ) >/dev/null 2>&1; F=$T/fos6; A=$F/system_a/system/vendor/etc/audio-algorithms
	grep -v '^speaker|' $WORK/assets.list > $T/al; cp $T/al $WORK/assets.list
	rm -rf $T/part; PART=$T/part; mkdir -p $PART/raw $PART/assets $PART/earcon $PART/led
	by_name() { case $1 in system_a|system_b) echo "$T/fos6/$1" ;; *) return 1 ;; esac; }
	mount() { cp -r "$5"/. "$6"/; }; umount() { rm -rf "$1"; }; BT=false
	( . $COLLECT ) 2>&1 )
[ ! -e $T/part/assets/speaker ] && ! echo "$out" | grep -q "speaker:" && pass "backup without a speaker line in assets.list takes no curve" ||
	fail "backup old manifest: $out"

# restore puts assets/speaker back and reports it
SPEAKER=1; mk_backup $B 2 yes; SPEAKER=0; mk_store
out=$( ( run_restore $B 1 ) 2>&1 )
if echo "$out" | grep -q "restore done" && echo "$out" | grep -q "speaker: 1 file(s)" &&
   [ "$(cat $WORK/opt/persist/biscuit/assets/speaker/EQ_50.cfg)" = "-0.001," ]; then
	pass "restore: speaker/EQ_50.cfg lands in persist/biscuit/assets/speaker and is reported"
else
	fail "restore speaker: $out"
fi
mk_backup $B 2 yes; mk_store
out=$( ( run_restore $B 1 ) 2>&1 )
echo "$out" | grep -q "restore done" && ! echo "$out" | grep -q "speaker:" &&
	pass "restore: a backup without the curve reports no speaker line" || fail "restore no speaker: $out"

# r294b: an asset profile the backup lacks is kept from the store (merge, not
# replace). An r293 backup has no assets/speaker; the store's curve, put there
# by the r294 pre-upgrade or an import, must survive a restore from it.
A=$WORK/opt/persist/biscuit/assets
mk_store_speaker() {
	mk_store
	mkdir -p $A/speaker $A/fireos6/sub
	echo "store curve" > $A/speaker/EQ_50.cfg
	echo "store fos6" > $A/fireos6/sub/AFE.cfg
}
mk_backup $B 2 yes; mk_store_speaker
out=$( ( run_restore $B 1 ) 2>&1 )
if echo "$out" | grep -q "restore done" &&
   [ "$(cat $A/speaker/EQ_50.cfg 2>/dev/null)" = "store curve" ] &&
   [ "$(cat $A/fireos6/sub/AFE.cfg 2>/dev/null)" = "store fos6" ] &&
   [ "$(cat $A/firmware/WIFI_RAM_CODE)" = fw ] &&
   echo "$out" | grep -q "speaker: 1 file(s)" &&
   echo "$out" | grep -q "kept from the store, not in the backup: fireos6 speaker" &&
   [ -z "$(ls -d $WORK/opt/persist/biscuit/*.new $WORK/opt/persist/biscuit/*.old 2>/dev/null)" ]; then
	pass "restore from a backup without the curve keeps the store's speaker (and any other missing profile, whole)"
else
	fail "restore keep speaker: $out / $(ls -R $A 2>&1)"
fi
# A profile the backup does hold replaces the store's copy whole.
SPEAKER=1; mk_backup $B 2 yes; SPEAKER=0; mk_store_speaker
echo "old extra" > $A/firmware/OLD_ONLY
out=$( ( run_restore $B 1 ) 2>&1 )
if echo "$out" | grep -q "restore done" &&
   [ "$(cat $A/speaker/EQ_50.cfg)" = "-0.001," ] && [ ! -e $A/firmware/OLD_ONLY ] &&
   echo "$out" | grep -q "kept from the store, not in the backup: fireos6$"; then
	pass "restore: a profile in the backup replaces the store's, missing ones are kept"
else
	fail "restore replace speaker: $out / $(ls -R $A 2>&1)"
fi
# Nothing to keep: no "kept" line.
mk_backup $B 2 yes; mk_store
out=$( ( run_restore $B 1 ) 2>&1 )
echo "$out" | grep -q "restore done" && ! echo "$out" | grep -q "kept from the store" &&
	pass "restore: nothing kept, nothing said" || fail "restore no kept line: $out"
# A kept profile is never checked against the backup's SHA256SUMS, and a
# failure later in the staging still leaves the store as it was.
mk_backup $B 2 yes; echo corrupt >> "$B/led/a.animation"; mk_store_speaker
out=$( ( run_restore $B 1 ) 2>&1 ); rc=$?
if [ $rc = 1 ] && [ "$(cat $A/speaker/EQ_50.cfg)" = "store curve" ] && [ ! -e $WORK/opt/persist/biscuit/assets.new ]; then
	pass "restore: a refused backup leaves the store's speaker in place"
else
	fail "restore refused keep (rc=$rc): $out"
fi
# The store is too small to keep a profile: the restore gives up, unchanged.
mk_backup $B 2 yes; mk_store_speaker
out=$( ( cp() { case "$*" in *assets/speaker*) return 1 ;; esac; command cp "$@"; }; run_restore $B 1 ) 2>&1 ); rc=$?
if [ $rc = 1 ] && echo "$out" | grep -q "too small to keep speaker; the store was not changed" &&
   [ "$(cat $A/speaker/EQ_50.cfg)" = "store curve" ] && [ "$(cat $A/firmware/WIFI_RAM_CODE)" = oldfw ] &&
   [ ! -e $WORK/opt/persist/biscuit/assets.new ]; then
	pass "restore: a profile that cannot be kept stops the restore, store unchanged"
else
	fail "restore keep fails (rc=$rc): $out"
fi

# install stages assets/speaker with the rest (checked against the backup's sums)
SPEAKER=1; mk_backup $B 2 yes; SPEAKER=0
out=$( ( run_install $B 1; ls $WORK/stage/assets/speaker ) 2>&1 )
echo "$out" | grep -q "stage done" && echo "$out" | grep -qx "EQ_50.cfg" &&
	pass "install: speaker/EQ_50.cfg staged and checked" || fail "install speaker: $out"
grep -q 'cp -r $STAGE/assets/\* $WORK/opt/persist/biscuit/assets/' $SRC/install/update-binary &&
	pass "install copies every assets profile (speaker included) to persist/biscuit/assets" || fail "install assets copy line changed"

# r294b: the install builds the owner boot image (biscuit-tool owner-boot, which
# refuses a kernel without the MediaTek header) BEFORE the first write.
IU=$SRC/install/update-binary
ob=$(grep -n 'owner-boot' $IU | head -1 | cut -d: -f1)
gm=$(grep -n 'gpt-merge' $IU | head -1 | cut -d: -f1)
fw=$(grep -n 'write_range\|dd if=/dev/zero\|MKE2FS' $IU | head -1 | cut -d: -f1)
[ -n "$ob" ] && [ -n "$gm" ] && [ "$ob" -lt "$gm" ] && [ "$ob" -lt "$fw" ] &&
   [ "$(grep -c 'owner-boot' $IU)" = 1 ] &&
	pass "install: owner-boot runs before gpt-merge and every write" || fail "install order: owner-boot $ob, gpt-merge $gm, first write $fw"
OB_SEC=$T/install-ownerboot.sh
cut_section $IU '^# --- the boot image, built in memory' '^done$' > $OB_SEC
grep -q 'owner-boot' $OB_SEC && grep -q 'boot_sectors' $OB_SEC && pass "install owner-boot section cut" || fail "install owner-boot section cut"
run_ob() {   # FAKE-TOOL-BEHAVIOUR: fail | size BYTES
	STAGE=$WORK/stage
	mkdir -p $WORK/payload
	echo boot > $WORK/payload/boot.img
	BT=$T/fake-tool
	case $1 in
		fail) printf '#!/bin/sh\necho "biscuit-tool: the kernel has no MediaTek header; the image would bootloop" >&2\nexit 1\n' > $BT ;;
		size) printf '#!/bin/sh\nhead -c %s /dev/zero > "$3"\n' "$2" > $BT ;;
	esac
	chmod 755 $BT
	. $OB_SEC
	echo "owner-boot ok"
}
out=$( ( run_ob fail ) 2>&1 ); rc=$?
[ $rc = 1 ] && echo "$out" | grep -q "Could not build the boot image. Nothing was changed: biscuit-tool: the kernel has no MediaTek header" &&
	pass "install: a boot image without the MediaTek header stops before anything is written" || fail "install ob fail (rc=$rc): $out"
out=$( ( run_ob size 16777216 ) 2>&1 ); rc=$?
[ $rc = 0 ] && echo "$out" | grep -q "owner-boot ok" && pass "install: a 16 MiB boot image fits boot_a and boot_b" || fail "install ob fits (rc=$rc): $out"
out=$( ( run_ob size 16777217 ) 2>&1 ); rc=$?
[ $rc = 1 ] && echo "$out" | grep -q "larger than boot_a. Nothing was changed" &&
	pass "install: a boot image larger than the slot stops before anything is written" || fail "install ob too big (rc=$rc): $out"

# stock-restore's printed Fire OS steps: format data, the update from /tmp, pushed twice
SR=$SRC/stock-restore/update-binary
[ "$(grep -c 'adb push update-kindle-biscuit_<version>.bin /tmp/update.zip' $SR)" = 2 ] &&
   [ "$(grep -c 'adb shell twrp install /tmp/update.zip' $SR)" = 2 ] &&
   grep -q 'adb shell twrp format data' $SR && ! grep -q 'twrp wipe data' $SR && ! grep -q '/sdcard/update.zip' $SR &&
	pass "stock-restore prints format data and /tmp pushes" || fail "stock-restore steps"

banner_out=$( ( banner ) 2>&1 )
echo "$banner_out" | grep -q "ui_print amazon-biscuit-restore v1" && echo "$banner_out" | grep -q "ui_print =======================" &&
	pass "banner" || fail "banner: $banner_out"

rm -rf $T
echo "failures: $fails"
[ $fails = 0 ]
