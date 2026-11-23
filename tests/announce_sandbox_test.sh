#!/bin/bash
# Drive the Announce card with NOTHING real: HC_HA_FAKE=1 turns every Home Assistant call into a log
# line (no network, no local web server), HC_FAKE_MIC=1 records a tone instead of the microphone, and
# the screen, keys and settings are sandboxed as in sandbox_test.sh. Nothing can play in the house.
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
RUN=${XDG_RUNTIME_DIR:?}
OUT=$RUN/hc-ann-test; rm -rf "$OUT"; mkdir -p "$OUT/config/home-control/devices"
cp "$SRC/tests/announce-sample.json" "$OUT/config/home-control/devices/announce.json"
export HC_HA_FAKE=1 HC_FAKE_MIC=1
export XDG_CONFIG_HOME=$OUT/config HC_FBDEV=$OUT/fb.bin HC_KEYDEV=/nonexistent HC_TEST_KEYS=$OUT/keys \
       HC_RUNDIR=$OUT HC_IR_LOG=$OUT/ha.log HC_DEVICES=$OUT/none
[ "$HC_HA_FAKE" = 1 ] || { echo "refusing: HC_HA_FAKE not set"; exit 1; }
mkdir -p "$HC_DEVICES"; head -c 108800 /dev/zero > "$HC_FBDEV"; mkfifo "$HC_TEST_KEYS"
n=0
start() { (cd "$SRC" && exec python3 -m homectl 2>>"$OUT/app.err") & APP=$!; sleep 2.5; }
put()   { timeout 2 sh -c "echo \"$1\" > \"$HC_TEST_KEYS\"" || echo "key not delivered: $1"; }
key()   { put "$1 1"; sleep 0.08; put "$1 0"; sleep "${2:-0.4}"; }
shot()  { n=$((n+1)); rm -f "$OUT/hc-screen.png"; kill -USR1 $APP 2>/dev/null; sleep 0.5; cp "$OUT/hc-screen.png" "$OUT/$(printf %02d $n)-$1.png" 2>/dev/null || echo "no shot: $1"; }
start
shot pick-all
key 17;                 shot beam-off             # W toggles BEAM off
key 31; key 31;         shot vol-70               # S twice: volume 50 -> 70
key 30;                 shot vol-60               # A: 70 -> 60
key 106;                shot vol-70-right         # the keyboard's right arrow: 60 -> 70
key 105;                shot vol-60-left          # ... and left: 70 -> 60
put "57 1"; sleep 1.2;  shot recording            # hold SPACE
put "57 0"; sleep 0.6;  shot confirm
key 28 1.0;             shot sent                 # ENTER: send (logged only)
key 9 0.4;              shot none                 # 8: None
put "57 1"; sleep 0.3; put "57 0"; sleep 0.4; shot pick-first   # SPACE with nothing picked
key 8 0.4                                          # 7: All
put "38 1"; sleep 1.0; put "38 0"; sleep 0.4; shot alarm-early-letgo
put "38 1"; sleep 1.5;  shot alarm-holding
sleep 2.2; put "38 0"; sleep 0.3; shot alarm-confirm
key 28 0.8;             shot alarm-on             # ENTER: alarm (logged only)
key 1 0.8;              shot alarm-stopped        # ESC: stop (logged only)
put "38 1"; sleep 3.5; put "38 0"; key 30 0.5; shot alarm-declined   # confirm shown, A = no
put "1 1"; sleep 1.2; shot esc-hint; sleep 2.6; put "1 0"; sleep 0.5   # hold ESC: the hint, then out at 3 s
kill -0 $APP 2>/dev/null && echo "FAIL: still running after ESC hold" || echo "ok: exited on ESC hold"
echo "--- what would have gone to Home Assistant"; cat "$OUT/ha.log"
echo "--- errors"; cat "$OUT/app.err"
