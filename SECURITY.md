# Security

This is a hobby port for a device on your home network. It has not had a
security audit. This page says what it exposes, what the defaults are, and how
to report a problem.

## Reporting a vulnerability

Please report security problems privately, through this repository's
**Security** tab ("Report a vulnerability"), not in a public issue. If that
tab does not offer a private report, open an issue that asks for a private
contact and leave the details out of it.
Include the build number (settings page, About, "Device software") and what
someone would need to reproduce it. Only the latest release is fixed; there
are no backports.

## What the device exposes

| Where | What | Protection |
|---|---|---|
| Setup hotspot `biscuit-XXXX`, `192.168.4.1:80` | The setup page | **Open Wi-Fi network**, plain HTTP. Secrets typed into the page are encrypted in the browser (see below) |
| Your network, port 8080 | The settings page | Your account's password; plain HTTP, no TLS. The port can be changed on the settings page. The shipped `user` account is refused here except over USB (see below) |
| Your network, port 22 | SSH | Your account's password or keys |
| Your network, port 6053 | Voice assistant (ESPHome API), only if the voice app is installed | None: the plain ESPHome API with no encryption key, like an ESPHome device set up without one. Anything that can reach the Echo's Wi-Fi address can connect |
| Your network, port 6054 | Bluetooth proxy (ESPHome API). **Off by default**; switched on in the settings page's Home Assistant section | As above |
| USB, if you switch on network mode | SSH and the settings page over the cable | As above |

### The setup hotspot is an open network

The Echo opens a hotspot named `biscuit-XXXX` and plays a chime when it starts
without a working Wi-Fi configuration, or when its saved network keeps failing
at start-up. A device that is already set up can also be put back into setup
on purpose, for example from its action button when that is set to "Enter
setup mode". The hotspot stays up for 10 minutes, extended while someone is
using the setup page, up to 30 minutes. It is an **open** network: anyone
within range can join it while it is up.

- The setup page encrypts the Wi-Fi password and the account password in the
  browser with NaCl (TweetNaCl in the page, PyNaCl on the device) for a key made
  fresh for each setup window, so someone merely listening to the open network
  sees neither.
- It does **not** protect against someone who runs their own hotspot with the
  same name and serves their own page. The "Play a sound on the device" step
  exists for this: only the real Echo in front of you can make the sound.
- **SSH is refused from the setup network**, for every account.
- **The settings page is stopped for every setup session** and started again
  when setup ends, whatever its port and whether or not the device already has
  an owner. While the setup network is up, the settings page also refuses any
  connection from it (`192.168.4.0/24`), and it never answers on the setup
  address `192.168.4.1`.
- **What stays reachable:** the setup network is not firewalled. On a device
  that is already set up, the Echo can stay joined to your Wi-Fi during setup
  (the hotspot runs on a second interface), and a client of the setup network
  can reach the Echo's address on your network through it. The ESPHome APIs
  listen there: the voice assistant on 6053 (if installed) and the Bluetooth
  proxy on 6054 (if switched on; if the Echo has no address on your network,
  the proxy listens on every address, including `192.168.4.1`). Both are
  unauthenticated, as they are on your own network. Only SSH and the settings
  page are closed to the setup network.

Finish setup soon after starting it, and set it up out of range of people you
do not trust if you can.

### The default account

The image ships postmarketOS's documented default account, **`user` /
`147147`**, because it is the only way into a headless device whose Wi-Fi setup
fails. Setup replaces it: it creates the account you choose and deletes
`user`, and every boot checks that `user` is gone once setup has finished.
While `user` still has the password `147147`, it can be used **only over a USB
network connection** (the Echo at `172.16.42.1`, the computer in
`172.16.42.0/24`; USB networking is off by default):

- Password SSH logins for it are refused everywhere else.
- The settings page refuses it everywhere else, both at sign-in and for every
  request of a session, whatever password was typed, and says why. That covers
  adding an SSH key, changing the Wi-Fi and every other setting.
- Where the password hash cannot be checked (an unreadable shadow file or an
  unusual hash format), the device assumes it is still `147147`.

Changing the password on the settings page lifts both rules at once, without a
reboot. A password changed with `passwd` in a shell is picked up at the next
boot, or at once with `sudo rc-service biscuit-firstboot sshpolicy`.

Setup does not accept `user`, the other reserved names, or the name of any of
the device's own service accounts for your account. On a device set up by an
older release whose account is named `user`, make sure its password is not
`147147`; the About page warns while an account still has it.

Accounts made by setup are in the `wheel` group, which has **passwordless
`sudo`**. Anyone who has your account's password has root on the Echo.

### The settings page is HTTP

The settings page on port 8080 is plain HTTP; the device cannot have a
certificate a browser trusts. The sign-in password is encrypted in the browser
(NaCl, with a one-time challenge from the device, so a captured login cannot be
replayed). Everything else on the page, and anyone able to change the traffic
rather than just read it, is not protected. Use it on a network you trust.

### The package feed

The Echo installs updates from this project's signed package feed,
`https://liamtw22.github.io/biscuit-apk/edge`. The feed's public key ships in
the core package, and `apk` refuses an index not signed with it. The feed is
**on by default**: unless you turn it off, the feed line is put back into
`/etc/apk/repositories` on every boot, and so is the key.

The choice is kept in `/opt/persist/update-feed` on the settings partition, so
it survives reinstalling. Case and blanks in that file are ignored.

- **To turn the feed off,** run `echo off | sudo tee /opt/persist/update-feed`.
  At the next boot the active feed line is removed once; a commented-out copy
  is left alone. Commenting the line out by hand
  (`#https://liamtw22.github.io/biscuit-apk/edge`) is recorded as `off` at the
  next boot. Deleting the line without writing `off` does not turn the feed
  off: it comes back at the next boot.
- **To turn it back on,** run `echo on | sudo tee /opt/persist/update-feed`.
  At the next boot a commented-out feed line is uncommented in place, or the
  line is added if it is missing, and the file is then deleted. Uncommenting or
  re-adding the line yourself after `off` has taken effect works too: the next
  boot takes that as turning the feed back on.
- **A settings reset** (and an erase of everything) turns the feed back on,
  whichever way it was turned off. A network reset leaves it alone.
- **Reinstalling** never turns it back on: `off` is applied afresh to the new
  system.
- Any other word in the file gives a warning at boot, and the feed stays on.

Changes to `/etc/apk/repositories` are written to a temporary file, checked and
then renamed into place, so a full disk leaves the file as it was.

The device also makes these outgoing connections on its own:

- the package feed on GitHub Pages, and the Nura/postmarketOS and Alpine package
  mirrors, when checking for or installing updates;
- NTP servers through chrony;
- **a time request to the host connected to its ESPHome API.** The Echo has no
  configured Home Assistant address. It takes the far end of an established
  connection to port 6053 or 6054 (normally Home Assistant) and reads the time
  from the `Date` header of that host's port 8123, over plain HTTP, or over
  HTTPS without checking the certificate. With no such connection nothing is
  sent. A date is used only if its year is between the year the device package
  was built and ten years later (2026 to 2036 for v1.0); a host answering
  outside that range is skipped and logged once. The clock is stepped, and the
  hardware clock written, only from a date in that range. chrony prefers a
  reachable NTP server to this source. A device on your network that holds a
  connection to 6053 or 6054 can therefore still set the Echo's clock to a
  time inside that range.

The voice assistant only sends audio to Home Assistant after the wake word is
heard on the device.

## Firmware and imported files

The Echo's firmware, audio tuning, sounds and animations are copied from the
owner's own Echo by the backup zip and never downloaded. The installer and the
packages check imported files against known SHA-256 hashes before using them.
