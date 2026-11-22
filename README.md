# cardputerzero-home-control

Home Control for the M5Stack CardputerZero: a carousel of the things in the house, each card a
remote laid out on the keyboard. A card talks infrared, Home Assistant over Wi-Fi, or both, or sends
Wake-on-LAN.

Vibe coded on the CardputerZero itself: spoken to a coding agent through
[cardputerzero-herdr-voice-client](https://github.com/TheRealHaoLiu/cardputerzero-herdr-voice-client),
with the [herdr-voice](https://github.com/TheRealHaoLiu/herdr-voice) server on a Mac doing the speech.

## Use

Open the **Home** tile in the launcher.

On a card:

| Key | Does |
|---|---|
| Q row, A row | the card's buttons, as drawn on screen; a key lights while it sends |
| ENTER | OK, on any page |
| 4 | back to the device menu |
| 5 / 6 | previous / next card |
| 7 / 8 | page up / down |
| 9 | switch Wi-Fi / IR on cards that have both (Wi-Fi / Bluetooth on a KDE Connect card); the icon over 9 shows how every card talks |
| 0 or Fn+ENTER | keyboard mode on cards that can type (type a line, ENTER sends it) |
| Fn arrows, volume, mute, play, next/prev, home | the matching button on any card, on any page |
| hold a dimmer's key | a brightness panel: F / X or Z / C change it |
| ESC | back to the device menu; hold one second to leave |

On the device menu: 3, 4, 5 or Z go left; 7, 8, 9 or C go right; 6 or ENTER opens the card.

On a KDE Connect card (an Android device): hold SPACE and move the CardputerZero to steer the pointer,
ENTER taps, holding ENTER is a long press.

## Cards

A card is a JSON file. Built-in infrared cards live in `devices/`, made on a computer by
`tools/flipper2device.py` from Flipper-IRDB files (`third_party/flipper-irdb/`, CC0) and a spec in
`devices/src/`; parsed NEC/NECext entries are turned into timings by
[remoteprotocols](https://github.com/ianchi/remoteprotocols). Nothing is converted on the CardputerZero.

    python3 tools/flipper2device.py devices/src/dyson-hp02.spec.json devices/dyson-hp02.json

A house's own cards go in `~/.config/home-control/devices/`; one with the same `id` as a built-in card
replaces it (e.g. the same TV with a Wi-Fi mode added). Card types (`backend`):

- `ir`: raw signals sent through `ir-ctl` on `/dev/lirc0`.
- `ha`: each key calls a Home Assistant action. Optional: `repeat` (ring a phone every few seconds),
  `adjust` (read an attribute, then toggle / step / cycle it), `state` (show ON / off on the key),
  `dim` (hold the key for a brightness panel), `room` (a coloured band across a room's keys),
  `discover` (fill the keys from Home Assistant, e.g. every phone with the HA app).
- `wol`: Wake-on-LAN, then waits for the machine to answer.
- `kdeconnect`: an Android device with the KDE Connect app, through the small client in
  `homectl/kdeconnect.py`: back / home / recents, scrolling, media keys, typing, and a pointer steered
  by the CardputerZero's gyroscope. The first time, the card asks the device to pair (see Pairing
  below). Over Wi-Fi; a
  Bluetooth link is in the code but does not work yet. Opening a link on the device only posts a
  notification there (KDE Connect's choice).
- `announce`: record a voice clip and play it on speakers through Home Assistant.
- `modes`: one card, several of the above (e.g. `wifi` and `ir`), switched with key 9.
  `start_mode` / `start_fallback`: start in Wi-Fi each time and drop to IR if HA cannot reach it.

A small Home Assistant card, as an example (entity names are placeholders). Keys in one `room` share
a band labelled with the room; the optional `room_names` gives the dimmer panel a longer name for it:

```json
{
 "id": "lights", "name": "Lights", "icon": "light", "backend": "ha", "states": true,
 "room_names": {"BED": "Bedroom"},
 "layout": {
  "q": {"id": "lamp", "label": "LAMP", "group": "heat", "room": "BED", "service": "light.toggle",
        "data": {"entity_id": "light.bedroom_lamp"}, "state": "light.bedroom_lamp", "dim": "light.bedroom_lamp"}
 }
}
```

`tests/announce-sample.json` and `tests/ha-sample.json` are neutral examples of an Announce card (its
speakers and the alarm clip's URL) and of a discovered Find card.

### Pairing (KDE Connect)

Pairing is trust on first use. The card's hello is broadcast on the LAN and any KDE Connect device
can answer it, so before anything is pinned the card shows what answered: the name it gives (which
any device can claim), its IP address, and a key (the first four bytes of the SHA-256 fingerprint of
its certificate, which the phone's KDE Connect lists under its encryption info). ENTER on the
CardputerZero sends the pair request, which the phone then has to accept; ESC says no. Once paired,
only that device id with that certificate is let in, and everything typed on the card goes to it.
To pair a different device, delete `~/.config/home-control/kdeconnect/peers.json`. If that file is
damaged, the card says so and does not pair again by itself.

### Home Assistant

Home Assistant cards (and the Announce card) need two files in `~/.config/home-control/`:

- `ha.json`: `{"url": "https://homeassistant.example:8123"}`. `https://` is checked against the
  system's CA certificates; plain `http://` also works, on a network you trust. No secrets here.
- `ha-token`: a long-lived access token (Home Assistant: your profile, Security), on one line.
  Make it readable by the desktop user only: `chmod 600 ~/.config/home-control/ha-token`.

Both are read once when first needed and kept while the app runs; the token is read again after Home
Assistant turns it down (HTTP 401/403), so after replacing the token the next call uses it. All calls share one
kept-alive connection.

## Install

On the device, the stock image already has what the app needs: `python3` with `python3-pil` and
`python3-numpy`, `ir-ctl` (v4l-utils), `pw-record` (PipeWire) and `openssl`; `sdptool` (BlueZ) only
for the unfinished Bluetooth link. The desktop user must be in the `video` group that owns
`/dev/lirc0`. Copy this folder to the device and run `sudo sh install.sh`: it stops with a message if
any of those is missing, replaces the app's Python files in `/opt/cardputerzero-home-control`
(removing any left from an older version) and adds the launcher tile.

### What is written where

The SD card is written only when something worth keeping changed:

- `~/.config/home-control/settings.json` (last card, modes, Announce choices): a few seconds after
  the last change (or on exit, if a change is still waiting), and only when its content differs
  from what the file holds.
- `~/.config/home-control/kdeconnect/`: the Cardputer's KDE Connect identity (`device_id`,
  `cert.pem`, `key.pem`) once, on the first connect; `peers.json` when a device is paired and when its
  address (or Bluetooth channel) changes, not on every connect. Small files are written to a temp
  file, synced and renamed, so a power cut leaves the old or the new one.

Everything else lives in RAM, in `$XDG_RUNTIME_DIR` (else `/run/user/<uid>`, else `/dev/shm`): the
Announce recording while it is made, IR timings while they are sent, screenshots, and the log,
`home-control.log`. The log holds errors and the Announce card's steps; repeated failures are logged
once with a count; it is cut to its last 200 lines at start when over 64 KB, and emptied by the app
when it passes 256 KB.

## Test

The tests are smoke scripts, not unit tests: they need Linux (bash, `mkfifo`, GNU `timeout`), are
meant to run on the device as any user with `XDG_RUNTIME_DIR` set, and print what they saw for a
person to read. Each drives the app with a fake screen, a key FIFO and a log, and leaves screenshots:

- `tests/sandbox_test.sh`: infrared (the Dyson's 12 buttons).
- `tests/announce_sandbox_test.sh`: the Announce card; every HA call faked (`HC_HA_FAKE=1`), a tone
  for a microphone (`HC_FAKE_MIC=1`).
- `tests/ha_sandbox_test.sh`: a discovered Find card; HA faked.

`HC_FAKE_UNAVAILABLE=<entity>` makes the fake HA report that entity unreachable.

## Licence

Copyright 2026 Hao Liu

Licensed under the Apache License, Version 2.0 (see `LICENSE`). The Flipper-IRDB files in
`third_party/flipper-irdb/` are CC0-1.0 (see the README in that folder).
