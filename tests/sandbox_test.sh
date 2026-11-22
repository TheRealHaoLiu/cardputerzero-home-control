#!/bin/bash
# Drive the app on the device without touching the real screen, keyboard, IR or settings: a fake
# screen file, a key FIFO, IR sends written to a log, its own config folder. Leaves numbered
# screenshots in $OUT. Run as the desktop user with XDG_RUNTIME_DIR set.
set -u
SRC=$(cd "$(dirname "$0")/.." && pwd)
RUN=${XDG_RUNTIME_DIR:?}
OUT=$RUN/hc-test; rm -rf "$OUT"; mkdir -p "$OUT"
export XDG_CONFIG_HOME=$OUT/config HC_FBDEV=$OUT/fb.bin HC_KEYDEV=/nonexistent HC_TEST_KEYS=$OUT/keys \
       HC_RUNDIR=$OUT HC_IR_LOG=$OUT/ir.log
head -c 108800 /dev/zero > "$HC_FBDEV"; mkfifo "$HC_TEST_KEYS"
n=0
start() { (cd "$SRC" && exec python3 -m homectl 2>>"$OUT/app.err") & APP=$!; sleep 2.5; }
put()   { timeout 2 sh -c "echo \"$1\" > \"$HC_TEST_KEYS\"" || echo "key not delivered: $1"; }
key()   { put "$1 1"; sleep 0.08; put "$1 0"; sleep "${2:-0.4}"; }
shot()  { n=$((n+1)); rm -f "$OUT/hc-screen.png"; kill -USR1 $APP 2>/dev/null; sleep 0.5; cp "$OUT/hc-screen.png" "$OUT/$(printf %02d $n)-$1.png" 2>/dev/null || echo "no shot: $1"; }
start
shot remote
put "18 1"; sleep 0.1;  shot lit-heat-up        # E held: lit
put "18 0"; sleep 0.8;  shot dark-again
key 16; key 30; key 17; key 31; key 32; key 33; key 19; key 34; key 20; key 21; key 35   # the other 11 buttons
key 7 0.5;              shot next-device        # 6: next device
key 9 0.5;              shot page-down          # 8: page down (greyed unless the device has pages)
key 5 0.5;              shot carousel           # 4: device list
key 28 0.5;             shot remote-again       # ENTER opens
key 1 0.5;              shot carousel-esc       # short ESC: back to the list
put "1 1"; sleep 1.5; put "1 0"; sleep 0.5
kill -0 $APP 2>/dev/null && echo "FAIL: still running after ESC hold" || echo "ok: exited on ESC hold"
echo "--- ir log (expect 12 lines: the 12 Dyson buttons)"; cat "$OUT/ir.log"
echo "--- settings"; cat "$XDG_CONFIG_HOME/home-control/settings.json"; echo
echo "--- errors"; cat "$OUT/app.err"
