"""Home Control on the CardputerZero: a carousel of devices, each with a remote laid out on the keyboard.

Screens: the carousel (one card per device) and a device's remote. On the remote, the Q row and the
A row are the device's buttons, drawn as a keyboard map; keys 4-8, which sit right under the screen,
are the app's own menu, labelled along the bottom edge. Device files are made on a computer
(tools/flipper2device.py) and only read here, so nothing heavy runs on the device.
"""
import glob, json, os, signal, stat, sys, threading, time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import hw
from .announce import Announce, ha_call, ha_error
from .kdeconnect import Client as KdcClient, Gyro, write_atomic

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.environ.get("HC_DEVICES", os.path.join(HERE, "devices"))
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "home-control")
USER_DEVICES = os.path.join(CONFIG, "devices")   # this house's own devices (addresses etc.), kept out of the repo
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
EXIT_HOLD = 1.0   # seconds of ESC to leave, as in M5Stack's own apps
STATUS_EVERY = 60  # seconds between status reads on a Home Assistant card (only while it is shown)
SAVE_DELAY = 3.0  # seconds after the last settings change before it is written to the SD card
LIT_FOR = 0.35    # seconds a key stays lit after its signal went out
IDLE_WAIT = 60.0  # seconds the main loop sleeps when nothing is due (keys and background threads wake it)
LOG_CAP = 256 * 1024   # bytes: the log on the RAM disk is emptied once it passes this

BG = (12, 14, 20); PANEL = (26, 30, 40); DIM = (40, 44, 54); TXT = (235, 238, 245); MUTED = (130, 138, 155)
GROUP = {"pwr": (220, 60, 60), "heat": (240, 140, 40), "cool": (70, 150, 240), "fan": (40, 190, 170),
         "flow": (160, 110, 230), "mode": (150, 155, 170)}
# Keyboard mode: evdev key codes to characters (the Cardputer's keyboard reports a standard US layout)
KEYMAP = {**{v: k for k, v in hw.LETTER.items()}, **{v: k for k, v in hw.DIGIT.items()},
          57: " ", 12: "-", 13: "=", 26: "[", 27: "]", 39: ";", 40: "'", 41: "`", 43: "\\", 51: ",", 52: ".", 53: "/"}
KEYMAP_SHIFT = {**{v: k.upper() for k, v in hw.LETTER.items()},
                **dict(zip([hw.DIGIT[c] for c in "1234567890"], "!@#$%^&*()")),
                57: " ", 12: "_", 13: "+", 26: "{", 27: "}", 39: ":", 40: '"', 41: "~", 43: "|", 51: "<", 52: ">", 53: "?"}
# The keyboard's own Fn keys (evdev codes the Cardputer sends) -> button ids to try on the current card
DIM_UP, DIM_DOWN = 33, 45   # F and X: the Zero's up and down (the CardputerZero's arrow layout, not the Cardputer ADV's)
ZERO_LEFT, ZERO_RIGHT = 44, 46   # Z and C: the Zero's left and right
HOLD_DIM = 0.5              # seconds holding a dimmable light's key before the dimmer opens
KDE_KEYS = ("kde", "mpris", "vol", "url")   # what a KDE Connect card's key can carry
FN = 389
CAROUSEL_KEYS = {hw.DIGIT["3"]: -1, hw.DIGIT["4"]: -1, hw.DIGIT["5"]: -1, hw.DIGIT["6"]: "open",
                 hw.DIGIT["7"]: 1, hw.DIGIT["8"]: 1, hw.DIGIT["9"]: 1}   # the Cardputer's Fn key, as the keyboard reports it (seen 2026-11-22)
NATIVE_KEYS = {103: ("up",), 108: ("down",), 105: ("left",), 106: ("right",),
               115: ("vol_up",), 114: ("vol_down",), 113: ("mute",),
               164: ("play",), 163: ("next", "ff"), 165: ("prev", "rew"),
               102: ("home",), 104: ("ch_up",), 109: ("ch_down",)}
QROW, AROW = "qwertyuio", "asdfghjkl"   # P and ENTER left off so the keys can be wider
SOFT_KEYS = "45678"   # the number keys right under the screen
# Where the middle of each of keys 4-8 sits along the screen's bottom edge, in pixels. Measured
# 2026-11-22 from a photo of a ruler painted on the screen: key 3 is just off the left edge,
# key 9 just off the right, keys 4-8 are about 56.5 px apart starting at 50.
SOFT_CENTERS = (50, 106, 163, 220, 276)


def font(name, size):
    for path in (FONT_DIR + name, "/usr/share/APPLaunch/share/font/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_KEY = font("DejaVuSansMono.ttf", 9)
F_WORD = font("DejaVuSansCondensed-Bold.ttf", 11)
F_SYM = font("DejaVuSans-Bold.ttf", 20)
F_TITLE = font("DejaVuSans-Bold.ttf", 13)
F_SMALL = font("DejaVuSansCondensed.ttf", 10)
F_SOFT = font("DejaVuSansCondensed-Bold.ttf", 10)
F_BIG = font("DejaVuSans-Bold.ttf", 18)
try:   # song titles: a font with Latin and Chinese (index 2 = Simplified Chinese shapes)
    F_SONG = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 10, index=2)
except OSError:
    F_SONG = F_SMALL


def load_devices():
    out = []
    paths = glob.glob(os.path.join(DEVICES, "*.json")) + glob.glob(os.path.join(USER_DEVICES, "*.json"))
    by_id = {}
    for path in sorted(paths, key=os.path.basename):
        try:
            with open(path) as f:
                dev = json.load(f)
        except (OSError, ValueError) as e:
            print(f"skipping {path}: {e}", file=sys.stderr); continue
        if not isinstance(dev, dict) or "id" not in dev or "name" not in dev:
            continue   # not a card (e.g. a settings file dropped in the folder)
        # One card per device: a file in the user's folder with the same id replaces the built-in one
        # (e.g. this house's Wi-Fi remote for the TV instead of the infrared card).
        by_id[dev.get("id", path)] = dev
    return sorted(by_id.values(), key=lambda d: d.get("id", ""))


def load_settings():
    try:
        with open(os.path.join(CONFIG, "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(text):
    os.makedirs(CONFIG, exist_ok=True)
    write_atomic(os.path.join(CONFIG, "settings.json"), text, 0o644)


def pages_of(dev):
    return dev.get("pages") or [dev.get("layout", {})]


def draw_icon(d, kind, box, colour=(200, 200, 210)):
    """Small drawn pictures of the devices; anything unknown gets a plain rounded square."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if kind == "dyson":  # the loop on a round base
        lw = max(2, w // 8)
        d.ellipse((x0 + w * 0.2, y0, x1 - w * 0.2, y1 - h * 0.18), outline=colour, width=lw)
        d.rounded_rectangle((x0 + w * 0.3, y1 - h * 0.22, x1 - w * 0.3, y1), radius=max(1, w // 10), fill=colour)
    elif kind == "tablet":  # a screen on a stand (the RoamView S6)
        lw = max(2, w // 12)
        d.rounded_rectangle((x0, y0 + h * 0.05, x1, y1 - h * 0.3), radius=max(2, w // 10), outline=colour, width=lw)
        d.line((x0 + w / 2, y1 - h * 0.3, x0 + w / 2, y1), fill=colour, width=lw)
        d.line((x0 + w * 0.25, y1 - lw / 2, x1 - w * 0.25, y1 - lw / 2), fill=colour, width=lw)
    elif kind == "speaker":  # a megaphone for announcements
        lw = max(2, w // 12)
        d.polygon([(x0 + w * 0.1, y0 + h * 0.38), (x0 + w * 0.35, y0 + h * 0.38), (x1 - w * 0.2, y0 + h * 0.1),
                   (x1 - w * 0.2, y1 - h * 0.1), (x0 + w * 0.35, y1 - h * 0.38), (x0 + w * 0.1, y1 - h * 0.38)], outline=colour, width=lw)
        d.arc((x1 - w * 0.25, y0 + h * 0.3, x1, y1 - h * 0.3), start=-60, end=60, fill=colour, width=lw)
    elif kind == "pc":  # a tower
        lw = max(2, w // 12)
        d.rounded_rectangle((x0 + w * 0.25, y0, x1 - w * 0.25, y1), radius=max(2, w // 10), outline=colour, width=lw)
        d.ellipse((x0 + w * 0.42, y0 + h * 0.15, x1 - w * 0.42, y0 + h * 0.15 + w * 0.16), outline=colour, width=lw)
        for i in range(3):
            yy = y0 + h * (0.5 + i * 0.12)
            d.line((x0 + w * 0.35, yy, x1 - w * 0.35, yy), fill=colour, width=max(1, lw // 2))
    elif kind == "light":  # a bulb
        lw = max(2, w // 12)
        d.ellipse((x0 + w * 0.2, y0, x1 - w * 0.2, y0 + h * 0.62), outline=colour, width=lw)
        d.rectangle((x0 + w * 0.38, y0 + h * 0.66, x1 - w * 0.38, y1 - h * 0.08), outline=colour, width=lw)
        d.line((x0 + w * 0.42, y1 - lw / 2, x1 - w * 0.42, y1 - lw / 2), fill=colour, width=lw)
    elif kind == "phone":
        lw = max(2, w // 12)
        d.rounded_rectangle((x0 + w * 0.25, y0, x1 - w * 0.25, y1), radius=max(2, w // 8), outline=colour, width=lw)
        d.line((x0 + w * 0.42, y0 + h * 0.08, x1 - w * 0.42, y0 + h * 0.08), fill=colour, width=max(1, lw // 2))
    elif kind == "find":  # a magnifying glass
        lw = max(2, w // 10)
        d.ellipse((x0 + w * 0.1, y0 + h * 0.1, x0 + w * 0.68, y0 + h * 0.68), outline=colour, width=lw)
        d.line((x0 + w * 0.6, y0 + h * 0.6, x1 - w * 0.08, y1 - h * 0.08), fill=colour, width=lw + 1)
    elif kind == "tv":
        d.rounded_rectangle((x0, y0 + h * 0.1, x1, y1 - h * 0.2), radius=3, outline=colour, width=max(2, w // 12))
        d.line((x0 + w * 0.3, y1, x1 - w * 0.3, y1), fill=colour, width=max(2, w // 12))
    else:
        d.rounded_rectangle(box, radius=max(2, w // 5), outline=colour, width=2)


class App:
    def __init__(self):
        self.devices = load_devices()
        self.settings = load_settings()
        self._saved = json.dumps(self.settings)   # what the SD card holds: a save that changes nothing is skipped
        self.screen, self.keys = hw.Screen(), hw.Keys()
        self._loop_thread = threading.get_ident()
        self.idx = min(self.settings.get("last", 0), max(len(self.devices) - 1, 0))
        self.view = "remote" if self.devices else "carousel"
        self.save_due = None    # when pending settings get written (None: nothing pending)
        self.lit = {}           # key letter -> time it goes dark (None while held or sending)
        self.page = {}          # device id -> page shown (starts on page 1 each time the app opens)
        self.note, self.note_until = "", 0.0
        self.esc_down = None
        self.running, self.dirty, self.frame = True, True, None
        self.lock = threading.Lock()
        self.ann = Announce(self)   # the Announce card's own state (recording, confirm, alarm)
        self.ringing = {}           # Home Assistant buttons that repeat: letter -> threading.Event that stops them
        self.status = {}            # device id -> (text, when fetched) for a Home Assistant card's header
        self.states = {}            # device id -> "busy" | "again" | "done": its keys' ON / off (one read at a time)
        self.states_lock = threading.Lock()
        self.discovered = {}        # device id -> "busy" | "done" for cards that fill themselves from HA
        self._mode_view = {}        # (device id, mode) -> the card as shown in that mode
        self.reach = {}             # device id -> True/False/"busy": can HA reach it (checked on open and on switch)
        self.typing = None          # keyboard mode: the line being typed (None: not typing)
        self.typing_ate_esc = False
        self.shift = False
        self.fn = False
        self.dim = None             # the dimmer panel: {"btn", "pct"} while open
        self.dim_ate_esc = False
        self.dim_want = {}          # light entity -> level still to send (None: its sender is busy, nothing new)
        self.dim_lock = threading.Lock()
        self.hold = None            # (letter, code, when) while a dimmable light's key is held
        self.kdc = KdcClient(log=lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", file=sys.stderr, flush=True),
                             on_change=lambda: setattr(self, "dirty", True))
        self.kdc_ate_esc = False    # the ESC that answered "pair with this device?": its release stays on the card
        self.enter_down = None      # KDE Connect cards: when ENTER went down (tap, or hold = long press)
        self.enter_held = False
        self.gyro = Gyro(self.kdc.send)
        self._kdc_seen = None
        for d in self.devices:      # cards that start in a set mode each time the app opens (e.g. the Dyson)
            if d.get("modes") and d.get("start_mode") in d["modes"]:
                self.settings.setdefault("mode", {})[d["id"]] = d["start_mode"]
                if d.get("start_fallback") in d["modes"]:
                    threading.Thread(target=self._start_check, args=(d,), daemon=True).start()

    # ---- state -------------------------------------------------------------------------------------
    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, value):
        """Set from a background thread, it also wakes the main loop so the change is drawn now."""
        self._dirty = value
        if value and threading.get_ident() != self._loop_thread:
            self.keys.wake()

    @property
    def raw_dev(self):
        return self.devices[self.idx] if self.devices else None

    @property
    def dev(self):
        """The current card. A card with "modes" (e.g. the TV: Wi-Fi through HA, or infrared) is shown as
        its chosen mode: the mode's backend, pages and codes over the card's name and icon (the two modes
        have different features, so different buttons)."""
        d = self.raw_dev
        if not d or "modes" not in d:
            return d
        m = self.mode_of(d)
        key = (d["id"], m)
        if key not in self._mode_view:
            view = {k: v for k, v in d.items() if k != "modes"}
            view.update(d["modes"][m])
            self._mode_view[key] = view
        return self._mode_view[key]

    def mode_of(self, d):
        """The card's mode: the last one used (remembered), else the first listed."""
        names = list(d["modes"])
        m = self.settings.get("mode", {}).get(d["id"])
        return m if m in names else names[0]

    def cycle_mode(self):
        """Key 9 on a card with modes: next mode; Wi-Fi gets a reachability check."""
        d = self.raw_dev
        names = list(d["modes"])
        m = names[(names.index(self.mode_of(d)) + 1) % len(names)]
        self.settings.setdefault("mode", {})[d["id"]] = m
        self.save_soon()
        self.stop_ha(); self.lit.clear()
        self.reach.pop(d["id"], None)
        self.say(d["modes"][m].get("label", m), 1.5)
        self.dirty = True

    def kdc_link(self):
        """Wi-Fi or Bluetooth for the KDE Connect card: the last one picked (explicit, remembered)."""
        return "bt" if self.settings.get("kdc_link") == "bt" else "wifi"

    def cycle_link(self):
        """Key 9 on a KDE Connect card: Wi-Fi <-> Bluetooth. Nothing switches by itself."""
        if not self.kdc.has_bt():
            self.say("Bluetooth is not set up", 2); return
        link = "wifi" if self.kdc_link() == "bt" else "bt"
        self.settings["kdc_link"] = link
        self.save_soon()
        self.gyro.stop(); self.kdc.close()
        self.kdc.transport = link
        self.kdc.start(pair=True)
        self.say("Bluetooth" if link == "bt" else "Wi-Fi", 1.5)
        self.dirty = True

    # ---- keyboard mode: type on the Cardputer, send a line to the TV (key 0, Wi-Fi only)
    # ---- dimmer: hold a dimmable light's key; F / X (or Fn up / down) in 10 % steps
    def open_dim(self, letter):
        btn = self.page_layout(self.dev).get(letter)
        dim = self.dim = {"btn": btn, "pct": None}
        self.dirty = True
        def read():   # into this panel's own dict: a reply after the panel closed changes nothing shown
            try:
                st = ha_call(f"/api/states/{btn['dim']}")
                b = st.get("attributes", {}).get("brightness") if st.get("state") == "on" else 0
                dim["pct"] = int(round((b or 0) / 255 * 10)) * 10
            except Exception as e:
                ha_error(f"ha dim {btn['dim']}", e); dim["pct"] = 50
            self.dirty = True
        threading.Thread(target=read, daemon=True).start()

    def dim_key(self, code, down):
        if not down:
            if code == hw.ESC:
                self.dim_ate_esc = False
            return
        if code in (hw.ESC, hw.ENTER):
            self.dim, self.dim_ate_esc, self.dirty = None, code == hw.ESC, True
            self.esc_down = None
            if self.dev.get("states"):
                self.refresh_states(self.dev)   # re-read the on/off states
            return
        step = {DIM_UP: 10, 103: 10, ZERO_RIGHT: 10, 106: 10,
                DIM_DOWN: -10, 108: -10, ZERO_LEFT: -10, 105: -10}.get(code)   # the bar runs left to right
        if not step or self.dim["pct"] is None:
            return
        pct = max(0, min(100, self.dim["pct"] + step))
        self.dim["pct"] = pct
        ent = self.dim["btn"]["dim"]
        with self.dim_lock:
            busy = ent in self.dim_want
            self.dim_want[ent] = pct
        if not busy:
            threading.Thread(target=self._dim_sender, args=(ent,), daemon=True).start()
        self.dirty = True

    def _dim_sender(self, ent):
        """One sender per light, always sending the newest level: on a slow link the steps in between
        are skipped, so an older request can never land last and leave the light off from the panel."""
        while True:
            with self.dim_lock:
                pct = self.dim_want[ent]
                if pct is None:
                    del self.dim_want[ent]; return
                self.dim_want[ent] = None
            try:
                if pct == 0:
                    ha_call("/api/services/light/turn_off", {"entity_id": ent})
                else:
                    ha_call("/api/services/light/turn_on", {"entity_id": ent, "brightness_pct": pct})
            except Exception as e:
                ha_error(f"ha dim {ent}", e); self.say("HA call failed", 2)

    def draw_dim(self, d):
        btn, pct = self.dim["btn"], self.dim["pct"]
        col = GROUP.get(btn.get("group"), MUTED)
        box = (30, 34, hw.W - 30, hw.H - 30)
        d.rounded_rectangle(box, radius=8, fill=PANEL, outline=col, width=2)
        # A room's short band label can have a longer name for the panel: the card's "room_names"
        name = self.dev.get("room_names", {}).get(btn.get("room"), (btn.get("room") or btn.get("label", "")).title())
        d.text((42, 40), name, font=F_TITLE, fill=TXT)
        t = "\u2026" if pct is None else ("Off" if pct == 0 else f"{pct}%")
        d.text((box[2] - 12 - d.textlength(t, font=F_TITLE), 40), t, font=F_TITLE, fill=col)
        for i in range(10):
            x0 = 42 + i * 23
            d.rounded_rectangle((x0, 70, x0 + 19, 92), radius=3, fill=col if pct and i < pct // 10 else DIM)
        d.text((42, 100), "F/C up   X/Z down   ENTER / ESC done", font=F_SMALL, fill=MUTED)

    def kdc_confirm_key(self, code, down):
        """A first KDE Connect pairing waits for ENTER (pair) or ESC (no) here; other keys do nothing."""
        if not down:
            if code == hw.ESC:
                self.kdc_ate_esc = False
            return
        if code in (hw.ENTER, hw.ESC):
            self.kdc.confirm(code == hw.ENTER)
            self.kdc_ate_esc, self.esc_down, self.dirty = code == hw.ESC, None, True

    def draw_kdc_panel(self, d, col, title, lines):
        box = (16, 30, hw.W - 16, hw.H - 26)
        d.rounded_rectangle(box, radius=8, fill=PANEL, outline=col, width=2)
        d.text((28, 36), title, font=F_TITLE, fill=col)
        for i, line in enumerate(lines):
            d.text((28, 60 + i * 16), line, font=F_SMALL, fill=TXT)

    def open_keyboard(self):
        if self.dev.get("keyboard"):
            self.stop_ha(); self.typing, self.shift, self.dirty = "", False, True
        elif self.raw_dev.get("modes"):
            self.say("Keyboard needs Wi-Fi (9)", 2)
        else:
            self.say("No keyboard on this card", 1.5)

    def type_key(self, code, down):
        if code in (42, 54):                      # shift
            self.shift = down; return
        if code in NATIVE_KEYS:                    # Fn arrows / volume still drive the TV while typing
            if down:
                self.native_key(code)
            return
        if not down:
            if code == hw.ESC and self.typing_ate_esc:
                self.typing_ate_esc = False
            return
        if code == hw.ESC:
            self.typing, self.typing_ate_esc, self.dirty = None, True, True
            self.esc_down = None
            return
        if code == hw.ENTER:
            text = self.typing
            self.typing = ""
            cmd = f"text:{text}" if text else "ENTER"   # an empty line presses the TV's Enter (submit)
            ent = self.dev.get("keyboard")
            if ent == "kdeconnect":                       # the phone's KDE Connect keyboard must be the active one
                ok = self.kdc.send({"key": text}) if text else self.kdc.send({"specialKey": 12})
                self.say(("Typed \u2713" if text else "Enter \u2713") if ok else "Not connected", 1.5)
                self.dirty = True; return
            def run():
                try:
                    ha_call("/api/services/remote/send_command", {"entity_id": ent, "command": cmd})
                    self.say("Typed \u2713" if text else "Enter \u2713", 1.5)
                except Exception as e:
                    ha_error(f"ha keyboard", e); self.say("Send failed", 2)
            threading.Thread(target=run, daemon=True).start()
            self.dirty = True; return
        if code == 14:                            # backspace
            self.typing = self.typing[:-1]; self.dirty = True; return
        ch = KEYMAP_SHIFT.get(code) if self.shift else KEYMAP.get(code)
        if ch and len(self.typing) < 120:
            self.typing += ch; self.dirty = True

    def draw_typing(self, d):
        box = (10, 30, hw.W - 10, hw.H - 30)
        d.rounded_rectangle(box, radius=8, fill=PANEL, outline=(70, 150, 240), width=2)
        d.text((20, 36), "Type to the TV", font=F_TITLE, fill=TXT)
        t = self.typing
        while t and d.textlength(t + "\u258f", font=F_TITLE) > hw.W - 40:
            t = t[1:]                                  # keep the end of a long line in view
        d.rounded_rectangle((18, 62, hw.W - 18, 90), radius=4, fill=BG)
        d.text((24, 66), t + "\u258f", font=F_TITLE, fill=TXT)
        d.text((20, 98), "ENTER send   (empty ENTER = TV Enter)", font=F_SMALL, fill=MUTED)
        d.text((20, 114), "BACKSPACE delete   ESC close", font=F_SMALL, fill=MUTED)

    def _start_check(self, d):
        """At app start: if the start mode's device is unreachable, drop to the fallback mode. The only
        automatic mode switch anywhere: a card can start on Wi-Fi and fall back to infrared when the Wi-Fi
        side is not working; after that, modes change only when the user presses 9."""
        ent = d["modes"][d["start_mode"]].get("check")
        try:
            ok = bool(ent) and ha_call(f"/api/states/{ent}").get("state") not in ("unavailable", "unknown", None)
        except Exception as e:
            ha_error(f"ha state {ent}", e); ok = False
        if not ok:
            self.settings["mode"][d["id"]] = d["start_fallback"]
            self.reach[d["id"]] = False if d["start_fallback"] == d["start_mode"] else self.reach.get(d["id"])
            self.say(f"{d['name']}: no Wi-Fi, using {d['modes'][d['start_fallback']].get('label', 'IR')}", 3)
        self.dirty = True

    def check_reach(self, d):
        """Ask HA once whether the mode's device is reachable; the mode label gets a small x if not."""
        ent = self.dev.get("check")
        if not ent:
            return
        self.reach[d["id"]] = "busy"
        def run():
            try:
                ok = ha_call(f"/api/states/{ent}").get("state") not in ("unavailable", "unknown", None)
            except Exception as e:
                ha_error(f"ha state {ent}", e); ok = False
            self.reach[d["id"]] = ok
            if not ok:   # say why, not just the small x
                self.say("HA can't reach it over Wi-Fi", 3)
            self.dirty = True
        threading.Thread(target=run, daemon=True).start()

    def code_set(self, dev):
        sets = list(dev.get("code_sets", {}))
        chosen = self.settings.get("code_set", {}).get(dev["id"])
        return chosen if chosen in sets else (sets[0] if sets else None)

    def visible_pages(self, dev):
        """Pages with only the buttons this code set has; a page left empty is dropped."""
        sigs = dev.get("code_sets", {}).get(self.code_set(dev), {}).get("signals") if dev.get("code_sets") else None
        out = []
        ok = lambda b: sigs is None or b["id"] in sigs or any(k in b for k in KDE_KEYS)
        fixed = {k: b for k, b in dev.get("fixed", {}).items() if ok(b)}
        for page in pages_of(dev):
            shown = {k: b for k, b in page.items() if ok(b)}
            if shown:
                out.append({**shown, **fixed})
        return out or [fixed]

    def page_layout(self, dev):
        pages = self.visible_pages(dev)
        return pages[self.page.get(dev["id"], 0) % len(pages)]

    def next_page(self, step=1):
        dev = self.dev
        self.page[dev["id"]] = (self.page.get(dev["id"], 0) + step) % len(self.visible_pages(dev))
        self.lit.clear(); self.dirty = True

    def save_soon(self):
        """Settings go to the SD card once, a few seconds after the last change (and on exit), not per key."""
        self.save_due = time.monotonic() + SAVE_DELAY

    def flush_settings(self):
        if self.save_due is not None:
            self.save_due = None
            text = json.dumps(self.settings)
            if text != self._saved:       # e.g. moving off a card and back: nothing to write
                save_settings(text)
                self._saved = text

    def say(self, text, secs=1.6):
        self.note, self.note_until, self.dirty = text, time.monotonic() + secs, True

    def move(self, step):
        if self.devices:
            self.stop_ha()
            self.leave_card()
            self.reach.clear()
            self.status.clear()
            self.forget_states()
            self.idx = (self.idx + step) % len(self.devices)
            self.settings["last"] = self.idx
            self.save_soon()
            self.dirty = True

    def next_code_set(self):
        dev = self.dev
        sets = list(dev.get("code_sets", {}))
        if len(sets) < 2:
            self.say("Only one code set"); return
        new = sets[(sets.index(self.code_set(dev)) + 1) % len(sets)]
        self.settings.setdefault("code_set", {})[dev["id"]] = new
        self.page[dev["id"]] = 0
        self.save_soon()
        self.say(f"Code set {new}")

    def press(self, letter):
        dev = self.dev
        btn = self.page_layout(dev).get(letter)
        if not btn:
            return
        if dev.get("backend") == "ha":
            if letter in self.ringing:          # pressed again: stop repeating
                self.stop_ha(letter); self.say("Stopped"); return
            stop = threading.Event()
            if btn.get("repeat"):
                self.ringing[letter] = stop
            self.lit[letter] = None
            self.dirty = True
            threading.Thread(target=self._ha_press, args=(letter, btn, stop), daemon=True).start()
            return
        if dev.get("backend") == "kdeconnect" and any(k in btn for k in KDE_KEYS):
            if self.kdc.state != "connected":
                self.kdc.start(pair=True); self.say("Connecting\u2026", 2); return
            self.lit[letter] = time.monotonic() + LIT_FOR
            if "mpris" in btn:
                ok = self.kdc.media(btn["mpris"]) or (self.say("Nothing playing", 2) or True)
            elif "vol" in btn:
                ok = self.kdc.volume(btn["vol"]) or (self.say("Nothing playing", 2) or True)
            elif "url" in btn:
                ok = self.kdc.share_url(btn["url"])
                self.say(f"Opening {btn.get('label', 'link').title()}", 1.5)
            else:
                ok = self.kdc.send(btn["kde"])
            if not ok:
                self.say("Not connected", 2)
            self.dirty = True
            return
        if dev.get("backend") == "wol":
            self.lit[letter] = None
            self.dirty = True
            threading.Thread(target=self._wake, args=(letter, dev), daemon=True).start()
            return
        sig = dev["code_sets"][self.code_set(dev)]["signals"].get(btn["id"])
        if not sig:
            self.say(f"Not on codes {self.code_set(dev)}"); return
        self.lit[letter] = None
        self.dirty = True
        threading.Thread(target=self._send, args=(letter, btn, sig), daemon=True).start()

    def press_id(self, button_id):
        """Send a button by its id, wherever it sits; it lights if it is on the page shown.
        Returns False when the card has no such button."""
        dev = self.dev
        here = next((k for k, b in self.page_layout(dev).items() if b["id"] == button_id), None)
        if here:
            self.press(here); return True
        btn = next((b for page in pages_of(dev) + [dev.get("fixed", {})] for b in page.values() if b["id"] == button_id), None)
        if not btn:
            return False
        if dev.get("backend") == "ha":
            threading.Thread(target=self._ha_press, args=("\u21b5", btn, threading.Event()), daemon=True).start()
            return True
        if dev.get("backend") != "ir":
            return False
        sig = dev["code_sets"][self.code_set(dev)]["signals"].get(button_id)
        if sig:
            threading.Thread(target=self._send, args=("\u21b5", btn, sig), daemon=True).start()
        return bool(sig)

    def native_key(self, code):
        """The keyboard's own Fn keys (arrows, volume, media, home) press the matching button on any remote
        card, on any page. True when the key was one of them."""
        ids = NATIVE_KEYS.get(code)
        if not ids or self.view != "remote" or not self.dev or self.dev.get("backend") == "announce":
            return False
        for bid in ids:            # first id the card has wins (e.g. NEXT: "next", else fast-forward)
            if self.press_id(bid):
                break
        return True

    def leave_card(self):
        """Things a card holds open only while it is shown (e.g. the KDE Connect link, the microphone)."""
        self.gyro.stop()
        self.kdc.close()
        self.ann.leave()

    def stop_ha(self, letter=None):
        """Stop repeating Home Assistant buttons (all of them, or one)."""
        for k in [letter] if letter else list(self.ringing):
            ev = self.ringing.pop(k, None)
            if ev:
                ev.set()
            self.lit.pop(k, None)
        self.dirty = True

    def adjusted(self, btn):
        """The action's data. With "adjust", read the entity first and work out the new value:
        op "toggle" flips a true/false attribute, "add" steps a number (min/max), "cycle" walks a list.
        E.g. the Dyson's oscillation, heat target and sleep timer, which HA only sets, never steps."""
        data = dict(btn.get("data", {}))
        adj = btn.get("adjust")
        if not adj:
            return data
        st = ha_call(f"/api/states/{adj['entity']}")
        cur = st.get("attributes", {}).get(adj["attr"]) if adj.get("attr") else st.get("state")
        op = adj.get("op")
        if op == "toggle":
            new = not bool(cur)
        elif op == "add":
            if cur is None:   # never guess a starting point (a heat target of 0 + 1 would be 34 °F)
                raise ValueError(f"{adj['entity']} has no {adj.get('attr', 'state')}")
            new = float(cur) + adj["delta"]
            new = max(adj.get("min", new), min(adj.get("max", new), new))
            new = int(new) if float(new).is_integer() else new
        else:   # cycle
            vals = adj["values"]
            try:
                i = vals.index(type(vals[0])(float(cur)))
            except (ValueError, TypeError):
                i = -1
            new = vals[(i + 1) % len(vals)]
        data[adj["field"]] = new
        return data

    def _ha_press(self, letter, btn, stop):
        """Call the button's Home Assistant action; with "repeat", again every `every` s for `for` s or until stopped."""
        domain, service = btn["service"].split(".", 1)
        rep = btn.get("repeat") or {}
        every, until = rep.get("every", 0), time.monotonic() + rep.get("for", 0)
        n = 0
        while True:
            try:
                ha_call(f"/api/services/{domain}/{service}", self.adjusted(btn))
                n += 1
                self.say(f"{btn.get('label', 'Sent').title()} {n} \u2713" if rep else "Sent \u2713", every + 1 if rep else 2)
            except Exception as e:
                ha_error(f"ha {btn['service']}", e)
                self.say("Press 9 for IR" if self.raw_dev and self.raw_dev.get("modes") else "HA call failed", 3); break
            if not rep or stop.wait(every) or time.monotonic() >= until:
                break
        if self.dev and self.dev.get("states"):
            time.sleep(1.0)                    # give the light a moment, then show its new state
            self.refresh_states(self.dev)
        if self.ringing.get(letter) is stop:
            self.ringing.pop(letter, None)
        if letter in self.lit and self.lit[letter] is None:
            self.lit[letter] = time.monotonic() + LIT_FOR
        self.dirty = True

    def discover(self, dev):
        """Fill a "discover" card (Find) from Home Assistant: one key per device with the HA app."""
        self.discovered[dev["id"]] = "busy"
        self.say("Looking for devices\u2026", 10)
        threading.Thread(target=self._discover, args=(dev,), daemon=True).start()

    def _discover(self, dev):
        cfg = dev["discover"]
        names, exclude = cfg.get("names", {}), set(cfg.get("exclude", []))
        try:
            notify = next((d["services"] for d in ha_call("/api/services") if d["domain"] == "notify"), {})
            slugs = sorted(k[len("mobile_app_"):] for k in notify if k.startswith("mobile_app_"))
            slugs = [x for x in slugs if x not in exclude]
            order = cfg.get("order", [])
            slugs.sort(key=lambda x: order.index(x) if x in order else len(order))
            layout = {}
            for i, (letter, slug) in enumerate(zip(QROW + AROW, slugs)):
                full = slug.replace("_", " ")
                try:
                    full = ha_call(f"/api/states/device_tracker.{slug}")["attributes"].get("friendly_name", full)
                except Exception:
                    pass
                short = names.get(slug) or full.split("\u2019s ", 1)[-1].split("'s ", 1)[-1]
                try:
                    batt = ha_call(f"/api/states/sensor.{slug}_battery_level")["state"]
                    value = f"{float(batt):.0f}%"
                except Exception:
                    value = ""
                data = json.loads(json.dumps(cfg.get("data", {})).replace("{name}", short))
                label = names.get(slug) or short.split()[0]   # one word fits a key; names{} overrides
                layout[letter] = {"id": slug, "label": label.upper()[:6], "value": value,
                                  "group": ("pwr", "cool", "heat", "fan", "flow", "mode")[i % 6],
                                  "service": f"notify.mobile_app_{slug}", "data": data, "repeat": cfg.get("repeat")}
            dev["layout"] = layout
            self.discovered[dev["id"]] = "done"
            self.say(f"{len(layout)} device{'' if len(layout) == 1 else 's'}", 2)
        except Exception as e:
            ha_error(f"ha discover", e)
            self.discovered.pop(dev["id"], None)
            self.say("Could not reach HA", 3)
        self.dirty = True

    def refresh_states(self, dev):
        """Read the keys' states in the background; at most one read per card runs, and a refresh asked
        for meanwhile runs once after it (so mashing keys cannot pile up threads)."""
        with self.states_lock:
            if self.states.get(dev["id"]) in ("busy", "again"):
                self.states[dev["id"]] = "again"; return
            self.states[dev["id"]] = "busy"
        threading.Thread(target=self._fetch_states, args=(dev,), daemon=True).start()

    def forget_states(self):
        """Read the keys' states again when their card is next shown (a read still running finishes)."""
        with self.states_lock:
            for k in [k for k, v in self.states.items() if v == "done"]:
                del self.states[k]

    def _fetch_states(self, dev):
        """Buttons with "state" (an entity) show it on the key: ON / off / -- (e.g. the Lights card).
        One request per entity, all over the one kept-alive connection (see announce.ha_call)."""
        while True:
            for page in pages_of(dev):
                for btn in page.values():
                    ent = btn.get("state")
                    if not ent:
                        continue
                    try:
                        st = ha_call(f"/api/states/{ent}").get("state")
                    except Exception as e:
                        ha_error(f"ha state {ent}", e); st = None
                    btn["value"] = {"on": "ON", "off": "off"}.get(st, "--")
            self.dirty = True
            with self.states_lock:
                if self.states.get(dev["id"]) != "again":
                    self.states[dev["id"]] = "done"; return
                self.states[dev["id"]] = "busy"

    def _fetch_status(self, dev):
        parts = []
        for st in dev.get("status", []):
            try:
                v = ha_call(f"/api/states/{st['entity']}")["state"]
                parts.append(st.get("format", "{state}").format(state=v))
            except Exception as e:
                ha_error(f"ha status {st['entity']}", e)
        self.status[dev["id"]] = ("  ".join(parts), time.monotonic())
        self.dirty = True

    def _wake(self, letter, dev):
        w = dev["wol"]
        err = hw.wol_send(w["mac"], w.get("broadcast", "255.255.255.255"), w.get("port", 9))
        self.lit[letter] = time.monotonic() + LIT_FOR
        if err:
            print(f"wol {dev['id']}: {err}", file=sys.stderr)
            self.say("Wake failed"); return
        host, port = w.get("check_host"), w.get("check_port")
        if not (host and port):
            self.say("Wake sent"); return
        start = time.monotonic()
        while time.monotonic() - start < w.get("check_secs", 45):
            self.say(f"Waking\u2026 {int(time.monotonic() - start)}s", 3)
            if hw.tcp_up(host, port):
                self.say("Awake \u2713", 4); return
            time.sleep(2)
        self.say("No answer yet", 4)

    def _send(self, letter, btn, sig):
        with self.lock:   # one transmission at a time
            err = hw.ir_send(sig, btn["id"])
        if err:
            print(f"ir send {btn['id']}: {err}", file=sys.stderr)
            self.say("IR error")
        self.lit[letter] = time.monotonic() + LIT_FOR
        self.dirty = True

    # ---- drawing -----------------------------------------------------------------------------------
    def header(self, d, title, right, icon=None):
        d.rectangle((0, 0, hw.W, 21), fill=PANEL)
        x = 6
        if icon:
            draw_icon(d, icon, (5, 3, 17, 19)); x = 22
        d.text((x, 3), title, font=F_TITLE, fill=TXT)
        now = time.monotonic()
        text, col = (self.note, (255, 214, 120)) if now < self.note_until else (right, MUTED)
        d.text((hw.W - 6 - d.textlength(text, font=F_SMALL), 5), text, font=F_SMALL, fill=col)

    def soft_keys(self, d, labels):
        """labels: (key, text, enabled). Each label sits over its physical key (SOFT_CENTERS);
        a key with nothing to do is drawn greyed out."""
        y = hw.H - 22
        d.rectangle((0, y, hw.W, hw.H), fill=PANEL)
        if len(labels) != len(SOFT_CENTERS):   # e.g. the single "hold ESC quit" hint
            for k, label, on in labels:
                d.text(((hw.W - d.textlength(label, font=F_SOFT)) / 2, y + 5), label, font=F_SOFT, fill=TXT)
            return
        corner = 22   # the corners hold the device count (over key 3) and page count (by key 9), fenced off
        raw = self.raw_dev if self.view == "remote" else None
        # Every card shows how it talks over key 9: Wi-Fi (HA, Wake-on-LAN) or infrared
        mode = ("ir" if self.dev.get("backend") == "ir" else "wifi") if raw else None
        if raw and self.dev.get("backend") == "kdeconnect":
            mode = self.kdc_link()
        corner_r = corner   # on cards with modes, key 9's corner shows the mode as an icon (Wi-Fi and IR both)
        edges = [corner - 4] + [(a + b) // 2 for a, b in zip(SOFT_CENTERS, SOFT_CENTERS[1:])] + [hw.W - corner_r]
        edges[1] += 3   # a little more room for key 4's "Devices"
        for x in (edges[0], hw.W - corner_r):
            d.line((x, y + 3, x, hw.H - 3), fill=DIM)
        for i, ((k, label, on), cx) in enumerate(zip(labels, SOFT_CENTERS)):
            if i:
                d.line((edges[i], y + 3, edges[i], hw.H - 3), fill=DIM)
            room = edges[i + 1] - edges[i] - 4 - d.textlength(k, font=F_KEY) - 2
            fnt = F_SOFT if d.textlength(label, font=F_SOFT) <= room else F_SMALL
            kw = d.textlength(k, font=F_KEY) + 2
            lw = d.textlength(label, font=fnt) if label else 0
            x0 = cx - (kw + lw) / 2          # the key digit and its label together, centred over the key
            d.text((x0, y + 3), k, font=F_KEY, fill=MUTED if on else (60, 64, 76))
            if label:
                d.text((x0 + kw, y + 5), label, font=fnt, fill=TXT if on else (70, 75, 88))
        if len(self.devices) > 1:   # device count at the far left, over key 3
            t = f"{self.idx + 1}/{len(self.devices)}"
            d.text(((edges[0] - d.textlength(t, font=F_SMALL)) / 2, y + 6), t, font=F_SMALL, fill=MUTED)
        pc = getattr(self, "page_count", "")
        if mode:   # the mode over key 9; a small x in its corner when HA cannot reach the device
            cx, cy = hw.W - corner_r / 2 - 1, y + 13
            if mode == "ir":                  # an emitter: a dot sending three rays
                d.ellipse((cx - 7, cy - 2, cx - 3, cy + 2), fill=TXT)
                for dy in (-5, 0, 5):
                    d.line((cx - 1, cy + dy * 0.6, cx + 6, cy + dy * 1.2), fill=TXT, width=1)
            elif mode == "bt":                # Bluetooth: its rune
                d.line([(cx - 4, cy - 2), (cx + 4, cy + 6), (cx, cy + 10), (cx, cy - 6), (cx + 4, cy - 2), (cx - 4, cy + 6)],
                       fill=TXT, width=1)
            else:                             # Wi-Fi: a dot under two arcs
                d.ellipse((cx - 1.5, cy + 3, cx + 1.5, cy + 6), fill=TXT)
                d.arc((cx - 5, cy - 2, cx + 5, cy + 8), start=225, end=315, fill=TXT, width=2)
                d.arc((cx - 9, cy - 6, cx + 9, cy + 12), start=225, end=315, fill=TXT, width=2)
            if self.reach.get(raw["id"]) is False or (
                    self.dev.get("backend") == "kdeconnect" and self.kdc.state in ("no device", "error", "not paired", "bad peers")):
                d.text((hw.W - 8, y + 1), "x", font=F_KEY, fill=MUTED)   # a small x, not a red cross
        elif pc:   # page count at the far right, over key 9
            d.text((hw.W - corner + (corner - d.textlength(pc, font=F_SMALL)) / 2, y + 6), pc, font=F_SMALL, fill=MUTED)

    def soft_state(self):
        """What each of keys 4-8 does on the current screen: {key: (label, enabled)}.
        The order: 4 device list, 5/6 previous/next device, 7/8 page up/down."""
        many = len(self.devices) > 1
        if self.view == "remote" and self.dev and self.dev.get("backend") == "announce":
            self.page_count = ""
            return self.ann.soft_state()
        if self.view == "remote" and self.dev:
            np_ = len(self.visible_pages(self.dev))
            pg = self.page.get(self.dev["id"], 0) % np_ + 1
            paged = np_ > 1
            self.page_count = f"{pg}/{np_}" if paged else ""
            if self.dev.get("discover"):   # a refresh button; 8 still pages if there are more than 18
                return {"4": ("Devices", True), "5": ("\u25c0 Dev", many), "6": ("Dev \u25b6", many),
                        "7": ("\u21bb Refresh", self.discovered.get(self.dev["id"]) != "busy"), "8": ("\u25bc Pg", paged)}
            return {"4": ("Devices", True), "5": ("\u25c0 Dev", many), "6": ("Dev \u25b6", many),
                    "7": ("\u25b2 Pg", paged), "8": ("\u25bc Pg", paged)}
        self.page_count = ""
        return {"4": ("\u25c0", many), "5": ("\u25c0 Prev", many), "6": ("Open", bool(self.devices)),
                "7": ("Next \u25b6", many), "8": ("\u25b6", many)}

    def key_cell(self, d, x, y, w, h, letter, btn, dev=None):
        if not btn:
            d.rounded_rectangle((x, y, x + w, y + h), radius=5, outline=DIM, width=1)
            d.text((x + 3, y + 2), letter.upper(), font=F_KEY, fill=(70, 75, 88)); return
        col = GROUP.get(btn.get("group"), MUTED)
        lit = letter in self.lit
        d.rounded_rectangle((x, y, x + w, y + h), radius=5, fill=col if lit else PANEL)
        if lit:
            d.rounded_rectangle((x, y, x + w, y + h), radius=5, outline=(255, 255, 255), width=2)
        elif not btn.get("room"):          # keys in a room get one band across the room instead (draw_remote)
            d.rectangle((x + 2, y, x + w - 2, y + 2), fill=col)
        fg = BG if lit else TXT
        top = 11 if btn.get("room") else 0
        d.text((x + 3, y + 4 + top), letter.upper(), font=F_KEY, fill=BG if lit else MUTED)
        sym = btn.get("symbol", "")
        if btn.get("value") is not None and not sym:   # e.g. a battery level on a Find key
            val = btn["value"] or "\u266a"
            vcol = BG if lit else (MUTED if val in ("off", "--") else col)
            d.text((x + (w - d.textlength(val, font=F_WORD)) / 2, y + 20 + top // 2), val, font=F_WORD, fill=vcol)
        else:
            d.text((x + (w - d.textlength(sym, font=F_SYM)) / 2, y + 12), sym, font=F_SYM, fill=BG if lit else col)
        word, fw = btn.get("label", ""), F_WORD
        if d.textlength(word, font=fw) > w - 2:
            fw = F_SMALL
        d.text((x + (w - d.textlength(word, font=fw)) / 2, y + h - 16), word, font=fw, fill=fg)

    def draw_remote(self, d):
        dev = self.dev
        if dev.get("backend") == "announce":
            self.ann.draw(d)
            st = self.soft_state(); self.soft_keys(d, [(k, *st[k]) for k in SOFT_KEYS])
            return
        cs = self.code_set(dev)
        raw = self.raw_dev
        self.soft_state()   # works out page_count for this frame
        right = getattr(self, "page_count", "")   # the corner over key 9 holds the Wi-Fi / IR icon instead
        if raw.get("modes") and raw["id"] not in self.reach:
            self.check_reach(raw)
        if dev.get("backend") == "kdeconnect":
            if self.kdc.state == "idle":
                self.kdc.transport = self.kdc_link()
                self.kdc.start(pair=True)
            right = {"connecting": "Connecting\u2026", "connected": "Connected", "pairing": "Accept on the device",
                     "not paired": "Not paired", "no device": "Not found (press a key)", "error": "Error (press a key)",
                     "bad peers": "Pairing file damaged", "confirm": "Pair? ENTER / ESC"}.get(self.kdc.state, "")
            if self.kdc.state == "connected" and self.kdc.battery and self.kdc.battery[0] is not None:
                bolt = " \u26a1" if self.kdc.battery[1] else ""
                right = f"{self.kdc.battery[0]}%{bolt}  " + right
            if self.gyro.run.is_set():
                right = "Steering"
        if dev.get("discover") and dev["id"] not in self.discovered:
            self.discover(dev)
        if dev.get("backend") == "ha" and dev.get("states") and dev["id"] not in self.states:
            self.refresh_states(dev)
        if dev.get("backend") == "ha" and dev.get("status"):
            text, when = self.status.get(dev["id"], ("", None))
            if when is None or time.monotonic() - when > STATUS_EVERY:
                self.status[dev["id"]] = (text, time.monotonic())   # one fetch at a time
                threading.Thread(target=self._fetch_status, args=(dev,), daemon=True).start()
            right = text
        self.header(d, dev["name"], right, dev.get("icon"))
        cw, ch, gap = 33, 58, 2
        layout = self.page_layout(dev)
        # A card's note (hint, now playing) takes the longest run of unused keys in the bottom row.
        note_col = note_end = 0
        i = 0
        while dev.get("hint") and i < len(AROW):
            j = i
            while j < len(AROW) and not layout.get(AROW[j]):
                j += 1
            if j - i > note_end - note_col:
                note_col, note_end = i, j
            i = j + 1
        for row, (letters, y) in enumerate(((QROW, 25), (AROW, 25 + ch + 3))):
            for i, letter in enumerate(letters):
                if row == 1 and note_col <= i < note_end:
                    continue   # the card's note panel, not empty keys
                self.key_cell(d, 2 + i * (cw + gap), y, cw, ch, letter, layout.get(letter), dev)
            # One band per room across its neighbouring keys, with the room's name, so the grouping shows
            i = 0
            while i < len(letters):
                room = (layout.get(letters[i]) or {}).get("room")
                j = i
                while room and j + 1 < len(letters) and (layout.get(letters[j + 1]) or {}).get("room") == room:
                    j += 1
                if room:
                    col = GROUP.get(layout[letters[i]].get("group"), MUTED)
                    x0, x1 = 2 + i * (cw + gap), 2 + j * (cw + gap) + cw
                    d.rounded_rectangle((x0, y, x1, y + 11), radius=4, fill=col)
                    d.text(((x0 + x1 - d.textlength(room, font=F_KEY)) / 2, y), room, font=F_KEY, fill=BG)
                i = j + 1
        if dev.get("hint"):   # a short how-to in the empty part of the grid
            nx = 2 + note_col * (cw + gap) + 6
            d.text((nx, 92), dev["hint"], font=F_SMALL, fill=MUTED, spacing=2)
        if dev.get("backend") == "kdeconnect" and self.kdc.player():
            st = self.kdc.now.get(self.kdc.player(), {})
            song = " - ".join(x for x in (st.get("title"), st.get("artist")) if x) or self.kdc.player()
            vol = f"  vol {st['volume']}" if "volume" in st else ""
            mark = "\u25b6 " if st.get("isPlaying") else "\u275a\u275a "
            nx = 2 + note_col * (cw + gap) + 6 if dev.get("hint") else 150
            d.text((nx, 134), mark, font=F_SMALL, fill=TXT)
            x = nx + d.textlength(mark, font=F_SMALL)
            room = (2 + note_end * (cw + gap) - 6 if dev.get("hint") else 316) - d.textlength(vol, font=F_SMALL)
            while song and x + d.textlength(song, font=F_SONG) > room:   # by width: Chinese is twice as wide
                song = song[:-1]
            d.text((x, 143), song, font=F_SONG, fill=TXT, anchor="ls")   # on the same baseline as the mark
            d.text((room, 134), vol, font=F_SMALL, fill=TXT)
        st = self.soft_state(); self.soft_keys(d, [(k, *st[k]) for k in SOFT_KEYS])
        if dev.get("backend") == "kdeconnect" and self.kdc.state == "bad peers":
            self.draw_kdc_panel(d, (220, 60, 60), "Pairing file damaged",
                                ["Delete ~/.config/home-control/", "kdeconnect/peers.json to pair", "again, then press a key."])
        ask = self.kdc.asking
        if dev.get("backend") == "kdeconnect" and self.kdc.state == "confirm" and ask:
            self.draw_kdc_panel(d, (70, 150, 240), "Pair with this device?",
                                [ask["name"], f"at {ask['ip']}   key {ask['key']}", "ENTER pair      ESC no"])
        if self.typing is not None:
            self.draw_typing(d)
        if self.dim is not None:
            self.draw_dim(d)

    def draw_carousel(self, d):
        if not self.devices:
            self.header(d, "Home Control", "")
            d.text((12, 60), "No devices installed.", font=F_TITLE, fill=TXT)
            self.soft_keys(d, [("", "hold ESC quit", True)]); return
        dev = self.dev
        self.header(d, "Home Control", "")
        d.rounded_rectangle((60, 28, hw.W - 60, hw.H - 28), radius=10, fill=PANEL)
        draw_icon(d, dev.get("icon"), (hw.W // 2 - 26, 36, hw.W // 2 + 26, 96), colour=TXT)
        name = dev["name"]
        d.text(((hw.W - d.textlength(name, font=F_BIG)) / 2, 102), name, font=F_BIG, fill=TXT)
        unit = "speaker" if dev.get("backend") == "announce" else "button"
        n = len(dev.get("targets", [])) if dev.get("backend") == "announce" else sum(len(p) for p in pages_of(dev)) + len(dev.get("fixed", {}))
        backend = {"wol": "Wake-on-LAN", "announce": "Home Assistant", "ha": "Home Assistant"}.get(dev.get("backend"), dev.get("backend", "").upper())
        if self.raw_dev.get("modes"):
            backend = " + ".join(m.get("label", k) for k, m in self.raw_dev["modes"].items())
        sub = (dev["model"] + "  \u00b7  " if dev.get("model") else "") + f"{backend}  \u00b7  {n} {unit}{'' if n == 1 else 's'}" + ("  \u00b7  more to come" if dev.get("todo") else "")
        d.text(((hw.W - d.textlength(sub, font=F_SMALL)) / 2, 124), sub, font=F_SMALL, fill=MUTED)
        if len(self.devices) > 1:
            d.text((20, 70), "◀", font=F_BIG, fill=MUTED)
            d.text((hw.W - 34, 70), "▶", font=F_BIG, fill=MUTED)
        st = self.soft_state(); self.soft_keys(d, [(k, *st[k]) for k in SOFT_KEYS])

    def render(self):
        img = Image.new("RGB", (hw.W, hw.H), BG)
        d = ImageDraw.Draw(img)
        (self.draw_remote if self.view == "remote" and self.dev else self.draw_carousel)(d)
        self.frame = img
        self.screen.show(np.asarray(img))

    # ---- keys --------------------------------------------------------------------------------------
    def on_key(self, code, down):
        if code == FN:                  # Fn arrives as its own key, held while the other is pressed
            self.fn = down; return
        if self.typing is not None or (self.typing_ate_esc and code == hw.ESC):
            self.type_key(code, down); return
        if self.dim is not None or (self.dim_ate_esc and code == hw.ESC):
            self.dim_key(code, down); return
        if (self.kdc.state == "confirm" and self.view == "remote") or (self.kdc_ate_esc and code == hw.ESC):
            self.kdc_confirm_key(code, down); return
        if self.hold and code == self.hold[1]:
            if not down:                   # let go before the dimmer opened: a plain toggle
                letter, self.hold = self.hold[0], None
                self.press(letter)
            return
        if code == hw.ENTER and down and self.fn and self.view == "remote" and self.dev:
            self.open_keyboard(); return    # Fn+Enter: the universal keyboard key
        if self.view == "remote" and self.dev and self.dev.get("backend") == "announce":
            busy = self.ann.mode != "pick"
            soft = {hw.DIGIT[k]: k for k in SOFT_KEYS}
            if code == hw.ESC and not down and self.ann.ate_esc:   # the release of an ESC the card already used
                self.ann.ate_esc = False
                return
            if busy or (code not in soft and code != hw.ESC):
                if self.ann.key(code, down):
                    if code == hw.ESC and down:
                        self.ann.ate_esc = True
                    return
            elif code in soft and down and soft[code] in "78":
                if self.soft_state()[soft[code]][1]:
                    self.ann.soft(soft[code])
                return
        if code == hw.ESC:
            if down:
                self.esc_down = time.monotonic()
            else:
                held = self.esc_down is not None and time.monotonic() - self.esc_down >= EXIT_HOLD
                self.esc_down = None
                if not held and self.ringing:          # first ESC stops a repeating button, stays on the card
                    self.stop_ha(); self.say("Stopped")
                elif not held and self.view == "remote":
                    self.leave_card()
                    self.reach.clear()
                    self.status.clear()
                    self.forget_states()
                    self.view, self.dirty = "carousel", True
            return
        if code == hw.ENTER and self.view == "remote" and self.dev and self.dev.get("backend") == "kdeconnect" and not self.fn:
            if down:
                self.enter_down, self.enter_held = time.monotonic(), False
            elif self.enter_down is not None:
                self.enter_down = None
                if self.enter_held:
                    self.kdc.send({"singlerelease": True})   # end of a long press / drag
                else:
                    self.press_id("ok")                       # a tap
            return
        if code == 57 and self.view == "remote" and self.dev and self.dev.get("backend") == "kdeconnect":
            if down and self.kdc.state == "connected":
                self.gyro.start()                 # hold Space to steer the pointer
            elif down:
                self.kdc.start(pair=True); self.say("Connecting\u2026", 2)
            else:
                self.gyro.stop()
            self.dirty = True
            return
        if not down:
            return
        if self.view == "carousel" and code in (105, 106, ZERO_LEFT, ZERO_RIGHT):   # left / right flip devices
            self.move(-1 if code in (105, ZERO_LEFT) else 1); return
        if self.view == "carousel" and code in CAROUSEL_KEYS:
            # On the device menu 3/4/5 go left, 7/8/9 go right, 6 (or ENTER) opens
            act = CAROUSEL_KEYS[code]
            if act == "open":
                if self.devices:
                    self.view, self.dirty = "remote", True
            else:
                self.move(act)
            return
        if code in NATIVE_KEYS and self.native_key(code):
            return
        if code == hw.DIGIT["9"] and self.view == "remote" and self.raw_dev and self.raw_dev.get("modes"):
            self.cycle_mode(); return
        if code == hw.DIGIT["9"] and self.view == "remote" and self.dev and self.dev.get("backend") == "kdeconnect":
            self.cycle_link(); return
        if code == hw.DIGIT["0"] and self.view == "remote" and self.dev:
            self.open_keyboard(); return    # 0 too: both keys open it
        soft = {hw.DIGIT[k]: k for k in SOFT_KEYS}
        if code in soft:
            k = soft[code]
            if not self.soft_state()[k][1]:
                return   # greyed out: nothing to do
            if k == "4":
                self.stop_ha()
                self.leave_card()
                self.view, self.dirty = ("carousel" if self.view == "remote" else "remote"), True
            elif k == "5": self.move(-1)
            elif k == "6": self.move(1)
            elif k == "7" and self.view == "remote" and self.dev.get("discover"):
                self.stop_ha(); self.discover(self.dev)
            elif k == "7": self.next_page(-1)
            elif k == "8": self.next_page(1)
            return
        if self.view == "carousel":
            if code == hw.ENTER and self.devices:
                self.view, self.dirty = "remote", True
            return
        if code == hw.ENTER:   # the keyboard's own Enter also sends OK, on any page
            self.press_id("ok"); return
        letter = next((c for c, n in hw.LETTER.items() if n == code), None)
        if letter and letter in self.page_layout(self.dev):
            if self.page_layout(self.dev)[letter].get("dim"):
                self.hold = (letter, code, time.monotonic()); return   # tap toggles, hold dims
            self.press(letter)

    def next_wait(self, now):
        """Seconds until the loop has something to do by itself: the nearest deadline it tracks, else
        IDLE_WAIT. Keys and background threads (through self.dirty) wake it sooner."""
        if self._dirty:                   # a frame to draw (e.g. the first one)
            return 0.0
        due = [t for t in self.lit.values() if t is not None]
        due += [self.note_until if self.note else None, self.save_due,
                self.esc_down + EXIT_HOLD if self.esc_down is not None else None,
                self.enter_down + HOLD_DIM if self.enter_down is not None and not self.enter_held else None,
                self.hold[2] + HOLD_DIM if self.hold else None]
        if self.view == "remote" and self.dev and self.dev.get("backend") == "announce":
            due.append(self.ann.next_due(now))
        due = [t for t in due if t is not None]
        return max(0.0, min(due) - now) if due else IDLE_WAIT

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: (setattr(self, "running", False), self.keys.wake()))
        signal.signal(signal.SIGUSR1, lambda *_: self.frame and self.frame.resize((hw.W * 2, hw.H * 2), Image.NEAREST)
                      .save(os.path.join(hw.RUN, "hc-screen.png")))
        try:
            while self.running:
                for code, down in self.keys.read(self.next_wait(time.monotonic())):
                    self.on_key(code, down)
                now = time.monotonic()
                if self.esc_down is not None and now - self.esc_down >= EXIT_HOLD:
                    break
                if self.view == "remote" and self.dev and self.dev.get("backend") == "announce":
                    self.ann.tick()
                if self.enter_down is not None and not self.enter_held and now - self.enter_down >= HOLD_DIM:
                    self.enter_held = True                    # ENTER held: long press (drag while steering)
                    self.kdc.send({"singlehold": True}); self.say("Long press", 1)
                if self.hold and now - self.hold[2] >= HOLD_DIM:
                    letter, self.hold = self.hold[0], None
                    self.open_dim(letter)
                for k, t in list(self.lit.items()):
                    if t is not None and now >= t:
                        del self.lit[k]; self.dirty = True
                if self.note and now >= self.note_until:
                    self.note, self.dirty = "", True
                seen = (self.kdc.state, self.gyro.run.is_set())
                if seen != self._kdc_seen:          # the link's state changes in its own thread
                    self._kdc_seen, self.dirty = seen, True
                if self.save_due is not None and now >= self.save_due:
                    self.flush_settings()
                if self.dirty:
                    self.dirty = False
                    self.render()
        finally:
            self.stop_ha()
            self.leave_card()
            self.ann.rec.cancel()
            self.flush_settings()
            self.screen.close()


class CappedLog:
    """stderr while it is the log file (packaging/home-control): emptied once it passes LOG_CAP, so no
    failure, however chatty and however long the app runs, can fill the RAM disk."""

    def __init__(self, f):
        self.f = f

    def write(self, text):
        n = self.f.write(text)
        try:
            if os.fstat(self.f.fileno()).st_size > LOG_CAP:
                self.f.flush()
                os.ftruncate(self.f.fileno(), 0)    # opened for appending: writing goes on at the new end
                self.f.write(f"{time.strftime('%H:%M:%S')} log passed {LOG_CAP // 1024} KB and was emptied\n")
        except (OSError, ValueError):
            pass
        return n

    def __getattr__(self, name):
        return getattr(self.f, name)


def main():
    try:
        if stat.S_ISREG(os.fstat(sys.stderr.fileno()).st_mode):
            sys.stderr = CappedLog(sys.stderr)
    except (OSError, ValueError, AttributeError):
        pass
    App().run()
    return 0
