#!/bin/bash
# Drive the Assistant card with NOTHING real: HC_HA_FAKE=1 answers every turn from the app itself (a
# line in the test log, no network, no sound), HC_FAKE_MIC=1 records a tone instead of the microphone,
# and the screen, keys and settings are sandboxed as in sandbox_test.sh.
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
RUN=${XDG_RUNTIME_DIR:?}
OUT=$RUN/hc-assist-test; rm -rf "$OUT"; mkdir -p "$OUT/config/home-control/devices"
cp "$SRC/tests/assist-sample.json" "$OUT/config/home-control/devices/assist.json"
export HC_HA_FAKE=1 HC_FAKE_MIC=1
export XDG_CONFIG_HOME=$OUT/config HC_FBDEV=$OUT/fb.bin HC_KEYDEV=/nonexistent HC_TEST_KEYS=$OUT/keys \
       HC_RUNDIR=$OUT HC_IR_LOG=$OUT/ha.log HC_DEVICES=$OUT/none
[ "$HC_HA_FAKE" = 1 ] || { echo "refusing: HC_HA_FAKE not set"; exit 1; }
mkdir -p "$HC_DEVICES"; head -c 108800 /dev/zero > "$HC_FBDEV"; mkfifo "$HC_TEST_KEYS"
n=0
start() { (cd "$SRC" && exec python3 -m homectl 2>>"$OUT/app.err") & sleep 2.5; APP=$(pgrep -n -f "^python3 -m homectl"); }
put()   { timeout 2 sh -c "echo \"$1\" > \"$HC_TEST_KEYS\"" || echo "key not delivered: $1"; }
key()   { put "$1 1"; sleep 0.08; put "$1 0"; sleep "${2:-0.4}"; }
shot()  { n=$((n+1)); rm -f "$OUT/hc-screen.png"; kill -USR1 $APP 2>/dev/null; sleep 0.5; cp "$OUT/hc-screen.png" "$OUT/$(printf %02d $n)-$1.png" 2>/dev/null || echo "no shot: $1"; }
start
shot empty
put "57 1"; sleep 1.2;  shot listening            # hold SPACE
put "57 0"; sleep 0.7;  shot heard                # let go: the clip is sent, the words come back
sleep 0.6;              shot speaking             # the answer, being spoken (logged only)
sleep 1.2;              shot answered
put "57 1"; sleep 0.8; put "57 0"; sleep 2.6; shot follow-up   # a second turn in the same conversation
key 8 0.4;              shot agent                # 7: the next agent
key 9 0.4;              shot sound-off            # 8: answers on screen only
key 106 0.4;            shot volume               # right arrow: louder
put "389 1"; key 28 0.3; put "389 0"; sleep 0.3; shot keyboard   # Fn+ENTER: type instead
key 35; key 23; key 28 1.6; shot typed            # "hi", ENTER
put "57 1"; sleep 0.5; key 1 0.4; put "57 0"; sleep 0.4; shot cancelled   # ESC while listening
key 14 0.4;             shot new-conversation     # BACKSPACE
key 5 0.5;              shot carousel             # 4: the device list
put "1 1"; sleep 1.2; shot esc-hint; sleep 2.6; put "1 0"; sleep 0.5   # hold ESC: the hint, then out at 3 s
kill -0 $APP 2>/dev/null && echo "FAIL: still running after ESC hold" || echo "ok: exited on ESC hold"
echo "--- what would have gone to Home Assistant"; cat "$OUT/ha.log"
echo "--- settings"; cat "$XDG_CONFIG_HOME/home-control/settings.json"; echo
echo "--- errors"; cat "$OUT/app.err"
