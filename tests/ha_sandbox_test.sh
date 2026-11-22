#!/bin/bash
# Drive a Home Assistant button card (Find: discovered phones) with NOTHING real: HC_HA_FAKE=1 turns every Home
# Assistant call into a log line, so no phone rings. Screen, keys and settings are sandboxed.
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
RUN=${XDG_RUNTIME_DIR:?}
OUT=$RUN/hc-ha-test; rm -rf "$OUT"; mkdir -p "$OUT/config/home-control/devices"
cp "$SRC/tests/ha-sample.json" "$OUT/config/home-control/devices/phone.json"
export HC_HA_FAKE=1
export XDG_CONFIG_HOME=$OUT/config HC_FBDEV=$OUT/fb.bin HC_KEYDEV=/nonexistent HC_TEST_KEYS=$OUT/keys \
       HC_RUNDIR=$OUT HC_IR_LOG=$OUT/ha.log HC_DEVICES=$OUT/none
[ "$HC_HA_FAKE" = 1 ] || { echo "refusing: HC_HA_FAKE not set"; exit 1; }
mkdir -p "$HC_DEVICES"; head -c 108800 /dev/zero > "$HC_FBDEV"; mkfifo "$HC_TEST_KEYS"
n=0
(cd "$SRC" && exec python3 -m homectl 2>>"$OUT/app.err") & APP=$!; sleep 2.5
put()   { timeout 2 sh -c "echo \"$1\" > \"$HC_TEST_KEYS\"" || echo "key not delivered: $1"; }
key()   { put "$1 1"; sleep 0.08; put "$1 0"; sleep "${2:-0.4}"; }
shot()  { n=$((n+1)); rm -f "$OUT/hc-screen.png"; kill -USR1 $APP 2>/dev/null; sleep 0.5; cp "$OUT/hc-screen.png" "$OUT/$(printf %02d $n)-$1.png" 2>/dev/null || echo "no shot: $1"; }
shot card
key 16 0.5;  shot ringing-1          # Q: ring (repeats every 2 s in the sample)
sleep 4.2;   shot ringing-3
key 16 0.5;  shot stopped-by-q       # Q again: stop
sleep 2.5
key 16 0.5                           # ring again ...
key 1 0.5;   shot stopped-by-esc     # ... ESC stops it and stays on the card
sleep 2.5
key 8 1.0;   shot refreshed          # 7: Refresh (asks HA again)
put "1 1"; sleep 1.5; put "1 0"; sleep 0.5
kill -0 $APP 2>/dev/null && echo "FAIL: still running after ESC hold" || echo "ok: exited on ESC hold"
echo "--- what would have gone to Home Assistant"; cat "$OUT/ha.log"
echo "--- errors"; cat "$OUT/app.err"
