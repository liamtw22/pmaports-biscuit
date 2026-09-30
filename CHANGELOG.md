# Changelog

Public releases are named `vX.Y` (`v1.0`, then `v1.0.1` for fixes and `v1.1`
for features). Each is also tagged with its build number, `rNNN`, the `pkgrel`
of the core package `device-amazon-biscuit`, which keeps counting underneath
because apk orders upgrades by it. The settings page shows the release name as
Device software, with the build beside it. The kernel package,
`linux-amazon-biscuit`, has its own `pkgrel`, and the three TWRP tool zips have
their own version, printed first when they run.

## v1.0 (build r295) - the first public release

Released 2026-09-30.

| | Version |
|---|---|
| `device-amazon-biscuit` and subpackages | 6-r295 |
| `linux-amazon-biscuit` | 7.0.0_rc6-r243 (unchanged since r277) |
| `pmos-amazon-biscuit-v1.0.zip` | v1.0 |
| `amazon-biscuit-backup.zip`, `-restore.zip`, `-stock-restore.zip` | v1 each |

This is the first release with published source, zips and release notes.
Builds r1 to r294 were development builds; r294 was the release candidate and
was never published. Some builds, r232 to r293, were served for a while from an
earlier public package feed at the same address; that feed has been withdrawn
and deleted, and the feed now starts again at v1.0. The development history is
not published: the history of the published repository starts with a single
commit at v1.0.

The tool zips are at v1 because no tool zip was released before v1.0. Tool
zips built by earlier development builds also carried the name `-v1`, with
different contents; only the ones attached to a release are supported. From
v1.0 the tool zips have no version in their file names: use the ones attached
to the same release as the install zip. Each prints its version first.

### What the port does

postmarketOS (Nura) on the Echo Dot 2nd generation (RS03QR, MT8163), with a
7.0-rc6 mainline-based kernel:

- **Setup without an app.** On first boot the Echo opens a Wi-Fi hotspot and a
  setup page: your own account, device name, region, a sound check to prove you
  are talking to the Echo in front of you, the Wi-Fi network, and optional apps.
- **Voice assistant** (optional app): linux-voice-assistant with on-device
  microWakeWord, connected to Home Assistant Assist over the ESPHome API. All
  seven microphones feed a beamforming, echo-cancelling front end ("pmOS
  8-beam" by default; other profiles selectable, including ones that use your
  own Echo's Fire OS microphone configuration). Timers on the ring, ducking of
  all playback while you speak, a light sensor and the device settings as
  Home Assistant entities.
- **Music Assistant speaker** (optional app): a Sendspin player that connects
  to Music Assistant itself and drives the ring from the audio.
- **Bluetooth:** speaker (A2DP), casting to another Bluetooth speaker,
  hands-free calls through a paired phone with echo cancellation and
  suppression, and a Home Assistant Bluetooth proxy (off by default). Pairing
  opens for a limited
  window, from the action button, Home Assistant or the settings page.
- **Sound:** one device volume for every source, speaker correction, a
  multiband limiter, a volume ceiling at the stock level, and the headphone
  jack with plug detection.
- **Light ring:** activity patterns with a priority order, talker direction,
  volume, timers, fifteen generated ambient effects, and your own Echo's stock
  animations, all selectable by name.
- **Settings page** on port 8080: Wi-Fi, Bluetooth, sound, microphones, ring,
  apps, processes, updates (including the kernel and boot partitions), USB,
  date and time, storage and imported files, resets, search.
- **USB:** charging only by default; network, microphone and speaker functions
  switch on independently.
- **Updates** from a signed package feed; kernel updates are written to both
  boot partitions from the settings page.
- **Time:** chrony, with Home Assistant's clock as a reference when no NTP
  server answers; the kernel no longer believes the RTC's impossible dates.
- **Crash capture:** kernel logs survive a crash and reset.
- **Installer:** four TWRP zips: back up everything unique to the Echo, install,
  restore this Echo's files, and go back to the stock layout.

See the [README](README.md) for the feature table and
[docs/HARDWARE.md](docs/HARDWARE.md) for the hardware.

### Changes from r293

r293 was the last build on the earlier package feed. v1.0 changes, first those
made after the release candidate:

- **Apps chosen at setup are installed even when they cannot be at first.**
  Setup keeps the choice on the persist partition, and the settings page tries
  again: from 10 minutes after a boot, then after 5, 10, 20 and 40 minutes and
  hourly, waiting for the clock and for any other package job. The Home, Apps
  and Home Assistant pages say what is waiting and why; "Don't install" cancels
  it. Before, a failed setup-time install was shown only until the next update
  check, and forgotten at a restart. Setup's last page now says an install needs
  the internet and is retried.
- **Package failures say what failed.** The package feed being down, the clock
  not set yet, no internet, the package database in use, a package the feed
  does not offer and a full disk each have their own message; only a network
  with no internet is blamed on the network. An install's outcome is kept apart
  from update checks, which used to overwrite it.
- **Installing an app never changes the device software.** An install that
  would upgrade or downgrade the core package or the kernel is refused, and the
  Updates page offers the update instead. An app the feed lacks no longer holds
  back the other, and the Updates page says why a check failed instead of
  "System packages are up to date".
- **Setup forgets the Wi-Fi password once it has used it.** It stayed in `/run`
  until the next restart, and is now created readable by root only. After a
  WPS join, `wpa_supplicant.conf` is kept root-only. A setup retried after a
  failed join keeps the apps ticked, and a finished setup no longer shows as
  "crashed" in `rc-status`.
- **Erasing everything** also forgets apps chosen at setup that are not
  installed yet.
- **Release names:** releases are named `vX.Y`, the install zip is
  `pmos-amazon-biscuit-v1.0.zip`, and the tool zips lose the version from their
  file names.

And those made in the release candidate, r294:

- **The speaker correction curve is now imported, not shipped.** r293 and
  earlier shipped `speaker.fir`, which was Amazon's own `EQ_50.cfg`. It is no
  longer in any package. The backup zip collects your Echo's copy and the
  install stores it with the other imported files; the settings page can
  import it too (Storage, Files from stock). Without it the speaker plays
  flat. A device upgrading from r293 or older keeps its curve: a pre-upgrade
  step moves the old file into the import store before the package removes it.
  If it cannot, the upgrade still succeeds, the speaker plays flat, and
  `/var/lib/biscuit/speaker-curve-not-kept` says why; import `EQ_50.cfg` on the
  settings page then.
- **SSH is refused from the setup network,** for every account, and
  keyboard-interactive login is off, so the settings page's password switch
  covers every password login. An upgrade applies the SSH rules at once; no
  reboot is needed.
- **The settings page is stopped for every setup session,** and while the
  setup network is up it refuses connections from it. The ESPHome APIs are
  not covered; SECURITY.md says what stays reachable.
- **The default account is fenced until setup replaces it.** While `user`
  still has its published password, it is refused for password SSH and on the
  settings page except over a USB network connection. Changing its password on
  the settings page lifts both at once. Setup also refuses the names of the
  device's own service accounts.
- **Time sync asks only the host connected to the device's ESPHome API**
  (normally Home Assistant), instead of two fixed addresses on every device,
  and accepts only dates from 2026 to 2036.
- **Turning the package feed off now lasts.** `off` in
  `/opt/persist/update-feed` keeps the feed out of `/etc/apk/repositories`
  across reboots and reinstalls; a line commented out by hand is recorded the
  same way. `on`, re-enabling the line by hand, or a settings reset turn it
  back on.
- **Restore keeps what the backup lacks.** The restore zip replaces only the
  imported-file profiles the backup holds and keeps the others, such as a
  speaker curve imported after the backup was made.
- **The install checks the boot image before writing anything.** The zip
  builder refuses a boot image without MediaTek's kernel header, and the
  install zip builds this Echo's boot image in memory first, so a bad one
  stops the install with nothing changed.
- **Licences:** licence texts for everything the packages bundle, change
  notices on the modified linux-voice-assistant files (including
  `models.py`), and the unused openWakeWord models (CC BY-NC-SA) removed from
  the voice app.
- **Source:** the kernel's patched tree is published as a git branch, the
  pmaports base is published as a patch series (`base-pmaports/`), and the
  installer's source is in this repository under `installer/`. BUILDING.md
  builds everything from scratch.
- **Wording:** numbers measured from Fire OS's behaviour (volume steps,
  limiter settings, the light sensor's brightness table, beamformer constants,
  ambient effect timings) are labelled as such.

### Known issues

- Bluetooth audio or a call starves 2.4 GHz Wi-Fi; use 5 GHz.
- The clock starts at 2010-01-01 after a power loss until it is synced, and
  HTTPS fails until then.
- The kernel is a 7.0-rc6 fork with an out-of-tree vendor Wi-Fi driver and some
  diagnostic patches.
- The setup hotspot is an open network and is not firewalled, and the
  settings page is HTTP only.
- A Wi-Fi network added on the settings page is saved with its password in
  plain text in `/etc/wpa_supplicant/wpa_supplicant.conf`, readable by root
  only. Setup saves only the key derived from it, except after a WPS join,
  which saves what the router sends.
- Pulling the power can lose the last seconds of logs.
- Going back to Fire OS with the stock-restore zip has not been tested end to
  end.
