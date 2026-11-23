"""The Announce card: pick speakers, hold SPACE to record, confirm, and Home Assistant plays it on them.
Also an alarm (a siren clip on every speaker) that is hard to set off by accident.

The recorder follows the herdr-voice client's (left channel only, levelled). The speakers and the
alarm clip's URL come from the card's own file, never from this code.

How a clip reaches the speakers: the CardputerZero serves the one recording over plain HTTP on the
LAN for a minute and a half, and asks Home Assistant (media_player.play_media) to play that URL. Sonos
speakers get `announce: true`, so what they were playing pauses and comes back afterwards.

Needs, on the device (the desktop user's ~/.config/home-control/):
  ha.json   {"url": "https://<home assistant>"}            (no secrets)
  ha-token  a Home Assistant long-lived access token, one line, mode 600, put there by the user
The card's own file (devices/*.json, backend "announce") lists the speakers ("targets") and the
alarm clip ("alarm": {"url": ...}).

Tests: HC_HA_FAKE=1 makes every Home Assistant call a line in the test log and nothing else (no
network, no server); HC_FAKE_MIC=1 records a short tone instead of the microphone.
"""
import ctypes, http.client, http.server, io, json, os, secrets, signal, socket, ssl, subprocess, sys, threading, time
import urllib.parse, wave

import numpy as np

from . import hw

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "home-control")
FAKE = bool(os.environ.get("HC_HA_FAKE"))
FAKE_MIC = bool(os.environ.get("HC_FAKE_MIC"))
REC = os.path.join(hw.RUN, "hc-announce-rec.wav")
SERVE_SECS = 90          # how long the clip stays reachable for the speakers
MAX_SECS = 30            # longest recording
ALARM_HOLD = 3.0         # seconds the alarm key must be held
ALARM_CONFIRM = 6.0      # then ENTER within this many seconds, or it is called off
MAX_GAIN = 40.0
TARGET_RMS = 4500.0    # speech loudness after levelling (about -17 dBFS)
LIMIT = 31000.0        # the soft limiter's ceiling
TRIM_START = 0.25       # seconds cut from the start of a recording (the microphone's opening pop)
SEND_WAIT = 25         # seconds on "Sending" before the card gives up and says so
SPACE, BACKSPACE = 57, 14
PR_SET_PDEATHSIG = 1     # linux/prctl.h
try:
    _LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None
except OSError:
    _LIBC = None
ALARM_KEY = "l"          # the bottom-right key of the grid, labelled on screen
VOL_DOWN, VOL_UP = "a", "s"   # announcement volume, 10 % steps: left is less, like the bar beside them
VOL_CODES = {105: -10, 106: 10, 114: -10, 115: 10}   # the keyboard's own left / right and volume keys too
VOL_DEFAULT = 50              # a moderate start, so a first announcement is not a surprise


def note(line):
    """A timestamped line in the app's log (stderr goes to $XDG_RUNTIME_DIR/home-control.log)."""
    print(f"{time.strftime('%H:%M:%S')} announce: {line}", file=sys.stderr, flush=True)


def log(line):
    if hw.IR_LOG:
        with open(hw.IR_LOG, "a") as f:
            f.write(line + "\n")


# ---- Home Assistant -------------------------------------------------------------------------------
# One kept-alive connection for every call, one TLS context, and the URL and token read once: a card
# with many keys otherwise paid a TCP + TLS handshake (and a read of the CA bundle) per key.
_HA = {"cfg": None, "conn": None, "ctx": None, "failed": 0}
_HA_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


def ha_error(what, e):
    """Log a failed Home Assistant call. Once calls start failing, only the first is logged until one
    gets through again, which logs how many more failed meanwhile: a card reading N entities would
    otherwise log N lines per refresh for as long as the server is unreachable."""
    with _LOG_LOCK:
        _HA["failed"] += 1
        first = _HA["failed"] == 1
    if first:
        print(f"{time.strftime('%H:%M:%S')} {what}: {e} (more failures are counted until Home Assistant answers)",
              file=sys.stderr, flush=True)


def _ha_ok():
    with _LOG_LOCK:
        failed, _HA["failed"] = _HA["failed"], 0
    if failed > 1:
        print(f"{time.strftime('%H:%M:%S')} ha: answering again; {failed - 1} more calls had failed", file=sys.stderr, flush=True)


def ha_config():
    """(url, token) or (None, reason). Read once and kept for the life of the app; read again after Home
    Assistant turns the token down (and while the files are missing)."""
    if FAKE:
        return "https://ha.invalid", "fake"
    if _HA["cfg"]:
        return _HA["cfg"]
    try:
        with open(os.path.join(CONFIG, "ha.json")) as f:
            url = json.load(f)["url"].rstrip("/")
    except (OSError, ValueError, KeyError):
        return None, "Set up ha.json"
    try:
        with open(os.path.join(CONFIG, "ha-token")) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if not token:
        return None, "Set up ha-token"
    _HA["cfg"] = (url, token)
    return url, token


def _ha_connect(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "http":
        return http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    if _HA["ctx"] is None:
        _HA["ctx"] = ssl.create_default_context()
    return http.client.HTTPSConnection(u.hostname, u.port, timeout=10, context=_HA["ctx"])


def ha_call(path, body=None):
    """POST (or GET when body is None) to the Home Assistant REST API. Returns parsed JSON or raises."""
    if FAKE:
        log(f"ha {'POST' if body is not None else 'GET'} {path} {json.dumps(body, sort_keys=True) if body else ''}".rstrip())
        if path == "/api/services":   # two pretend phones for the Find card's discovery
            return [{"domain": "notify", "services": {"mobile_app_test_phone": {}, "mobile_app_test_tablet": {}, "notify": {}}}]
        if path.startswith("/api/states/device_tracker."):
            return {"state": "home", "attributes": {"friendly_name": "Test\u2019s " + path.rsplit(".", 1)[1].replace("_", " ").title()}}
        if os.environ.get("HC_FAKE_UNAVAILABLE") and path.endswith(os.environ["HC_FAKE_UNAVAILABLE"]):
            return {"state": "unavailable"}    # tests: a device HA cannot reach
        return {"state": "60"} if path.startswith("/api/states/") else []
    url, token = ha_config()
    if not url:
        raise RuntimeError(token)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with _HA_LOCK:
        for again in (False, True):
            conn, fresh = _HA["conn"], _HA["conn"] is None
            if fresh:
                conn = _HA["conn"] = _ha_connect(url)
            try:
                conn.request("POST" if data is not None else "GET", urllib.parse.urlsplit(url).path + path,
                             body=data, headers=headers)
                r = conn.getresponse()
                raw = r.read()
                break
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                _HA["conn"] = None
                # A kept-alive connection the server has since closed fails at once: once more on a new
                # one. A new connection failing, or a timeout, is a real failure.
                if again or fresh or isinstance(e, TimeoutError):
                    raise
    if r.status in (401, 403):
        _HA["cfg"] = None             # the token was turned down: read the files again next time
    if r.status >= 400:
        raise RuntimeError(f"HTTP {r.status} {r.reason}")
    _ha_ok()
    return json.loads(raw or b"null")


# ---- the clip ---------------------------------------------------------------------------------------
def _dies_with(parent):
    """For Popen's preexec_fn: on Linux the child gets SIGTERM when the thread that started it ends, so
    a crashed or killed app never leaves the microphone open and the RAM disk filling. Start it from the
    main thread, which lives as long as the app."""
    def arm():
        if _LIBC is not None:
            _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
            if os.getppid() != parent:   # the app was already gone before the signal was armed
                os._exit(1)
    return arm


class Recorder:
    """pw-record in stereo at 48 kHz; the microphone is the left channel and quiet (as in herdr-voice)."""

    def __init__(self):
        self.proc, self.started = None, 0.0
        try:
            os.unlink(REC)               # left over from an app that did not exit cleanly
        except OSError:
            pass

    def start(self):
        self.cancel()
        self.started = time.monotonic()
        if FAKE_MIC:
            return
        self.proc = subprocess.Popen(["pw-record", "--rate", "48000", "--channels", "2", "--format", "s16", REC],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=_dies_with(os.getpid()) if _LIBC is not None else None)

    def _stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def cancel(self):
        self._stop()
        try:
            os.unlink(REC)
        except OSError:
            pass

    def finish(self):
        """(wav bytes, seconds) or (None, seconds) when too short or silent."""
        secs = time.monotonic() - self.started
        if FAKE_MIC:
            t = np.arange(int(48000 * max(secs, 0.5))) / 48000
            return to_wav((np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes(), 1, 48000), secs
        self._stop()
        keep = os.path.exists(os.path.join(hw.RUN, "hc-keep-clips"))   # debugging: keep raw + levelled on the RAM disk
        try:
            x, rate = read_left(REC)
        except (OSError, wave.Error, EOFError):
            return None, secs
        finally:
            try:
                if keep:
                    os.replace(REC, os.path.join(hw.RUN, f"hc-clip-{time.strftime('%H%M%S')}-raw.wav"))
                else:
                    os.unlink(REC)
            except OSError:
                pass
        out = level(x, rate)
        del x
        if keep and out:
            with open(os.path.join(hw.RUN, f"hc-clip-{time.strftime('%H%M%S')}-sent.wav"), "wb") as f:
                f.write(out)
        return out, secs


def read_left(path):
    """(float32 samples of the left channel, rate) from a 16-bit WAV, without the first TRIM_START s.
    Read a second at a time and only the left channel converted, so the stereo recording is never in
    memory whole: a 30 s clip needs its 5.8 MB of float32 and little else."""
    with wave.open(path, "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        # The microphone opens with a full-scale pop (~0.1 s, measured 2026-11-22): drop the start.
        w.readframes(int(rate * TRIM_START))
        # At most what the file holds, whatever its header claims
        n = max(0, min(w.getnframes(), os.path.getsize(path) // (2 * ch)) - int(rate * TRIM_START))
        x = np.empty(n, dtype=np.float32)
        i = 0
        while i < n:
            part = np.frombuffer(w.readframes(min(rate, n - i)), dtype="<i2")
            part = part[: len(part) // ch * ch : ch]
            if not len(part):
                break
            x[i:i + len(part)] = part
            i += len(part)
    return x[:i], rate


def to_wav(raw, ch, rate):
    """16-bit PCM bytes -> the levelled mono WAV (see level). The first TRIM_START s are dropped."""
    x = np.frombuffer(raw, dtype="<i2")
    return level(x[: len(x) // ch * ch : ch].astype(np.float32)[int(rate * TRIM_START):], rate)


def level(x, rate):
    """float32 mono samples (changed in place) -> a levelled 48 kHz mono 16-bit WAV (a format Sonos and
    Cast both play), or None if silent/short. Works in place and in blocks, so the peak is the samples
    plus a copy of the speech frames rather than half a dozen full-size temporaries."""
    if len(x) < rate * 0.4:
        return None
    x -= x.mean()
    # Level on how loud the SPEECH is, not on the loudest sample: 20 ms frames well above the room's
    # noise floor count as speech. Normal talking at arm's length peaks near 4600 of 32767 (measured
    # 2026-11-22), so the mic is not clipping; levelling on a percentile and hard-clipping the rest
    # chopped the loudest syllables and made every clip a different loudness.
    n = rate // 50
    frames = x[: len(x) // n * n].reshape(-1, n)
    frms = np.empty(len(frames), dtype=np.float32)
    sq = np.empty((min(len(frames), 250), n), dtype=np.float32)   # 5 s of squares at a time
    for i in range(0, len(frames), len(sq)):
        part = sq[: len(frames[i:i + len(sq)])]
        np.square(frames[i:i + len(sq)], out=part)
        frms[i:i + len(part)] = part.mean(axis=1)
    del sq, part
    np.sqrt(frms, out=frms)
    floor = float(np.percentile(frms, 20))
    keep = frms > max(3 * floor, 30)
    if not keep.any():                # no pauses at all (a steady sound): use everything above silence
        keep = frms > 30
    if not keep.any():
        return None
    speech = frames[keep]             # a copy of the speech frames, squared in place
    np.square(speech, out=speech)
    rms = float(np.sqrt(speech.mean()))
    del speech, frames, keep
    gain = min(MAX_GAIN, TARGET_RMS / rms)
    note(f"clip level: speech rms {rms:.0f}, noise {floor:.0f}, gain {gain:.1f}x")
    # Soft limiter: below ~half scale it is nearly linear; louder peaks are rounded off, never chopped.
    np.multiply(x, gain, out=x)
    np.divide(x, LIMIT, out=x)
    np.tanh(x, out=x)
    np.multiply(x, LIMIT, out=x)
    pcm = x.astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)
    del pcm
    return out.getvalue()


def serve_once(data, ha_url):
    """Serve `data` at a random path on this device's LAN address for SERVE_SECS. Returns the URL."""
    host = urllib.parse.urlparse(ha_url).hostname
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 443))   # no packet is sent; this only picks the address the LAN sees us on
        ip = s.getsockname()[0]
    finally:
        s.close()
    name = f"/hc-{secrets.token_hex(8)}.wav"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != name:
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *a):
            pass

        def log_request(self, code="-", size="-"):   # one line per speaker that fetched the clip
            if str(code) == "200":
                note(f"clip fetched by {self.client_address[0]}")

    srv = http.server.ThreadingHTTPServer((ip, 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Timer(SERVE_SECS, srv.shutdown).start()
    return f"http://{ip}:{srv.server_address[1]}{name}"


# ---- the card -----------------------------------------------------------------------------------------
class Announce:
    def __init__(self, app):
        self.app = app
        self.rec = Recorder()
        self.mode = "pick"          # pick | recording | confirm | sending | alarm_hold | alarm_confirm | alarm_on
        self.clip, self.secs = None, 0.0
        self.alarm_down = None      # when the alarm key went down
        self.alarm_asked = 0.0
        self.ate_esc = False           # an ESC press used here; its release must not also leave the card
        self.send_started = 0.0
        self.job, self.worker = 0, None   # the clip being levelled off the UI thread

    @property
    def busy(self):
        """In the middle of something (recording, confirming, the alarm): every key is this card's."""
        return self.mode != "pick"

    # selection, kept in the settings file; everything selected the first time
    def targets(self, dev):
        return dev.get("targets", [])[:9]

    def selected(self, dev):
        sel = self.app.settings.get("announce_selected", {}).get(dev["id"])
        ents = [t["entity"] for t in self.targets(dev)]
        return [e for e in ents if e in sel] if sel is not None else ents

    def set_selected(self, dev, ents):
        self.app.settings.setdefault("announce_selected", {})[dev["id"]] = ents
        self.app.save_soon()
        self.app.dirty = True

    def volume(self, dev):
        return int(self.app.settings.get("announce_volume", {}).get(dev["id"], VOL_DEFAULT))

    def set_volume(self, dev, v):
        self.app.settings.setdefault("announce_volume", {})[dev["id"]] = max(10, min(100, v))
        self.app.save_soon()
        self.app.say(f"Volume {self.volume(dev)}%", 1.5)
        self.app.dirty = True

    # soft keys on this card: 4 Devices, 5/6 device, 7 All, 8 None
    def soft_state(self):
        many = len(self.app.devices) > 1
        idle = self.mode == "pick"
        return {"4": ("Devices", idle), "5": ("◀ Dev", many and idle), "6": ("Dev ▶", many and idle),
                "7": ("All", idle), "8": ("None", idle)}

    def soft(self, k):
        dev = self.app.dev
        if k == "7":
            self.set_selected(dev, [t["entity"] for t in self.targets(dev)])
        elif k == "8":
            self.set_selected(dev, [])

    # ---- keys
    def key(self, code, down):
        """Returns True when the key was used here."""
        app, dev = self.app, self.app.dev
        letter = next((c for c, n in hw.LETTER.items() if n == code), None)
        now = time.monotonic()
        if self.mode == "pick":
            if code == SPACE and down:
                url, why = ha_config()
                if not url:
                    app.say(why, 3); return True
                if self.worker and self.worker.is_alive():   # the last clip is still being read: it uses the file
                    app.say("One moment", 1); return True
                if not self.selected(dev):
                    app.say("Pick a speaker first", 2); return True
                self.rec.start(); self.mode = "recording"; app.dirty = True
                return True
            if letter == ALARM_KEY and down:
                self.alarm_down = now; self.mode = "alarm_hold"; app.dirty = True
                return True
            if (letter in (VOL_DOWN, VOL_UP) or code in VOL_CODES) and down:
                self.set_volume(dev, self.volume(dev) + VOL_CODES.get(code, 10 if letter == VOL_UP else -10))
                return True
            if letter and down:
                i = "qwertyuio".find(letter)
                ts = self.targets(dev)
                if 0 <= i < len(ts):
                    sel = self.selected(dev)
                    e = ts[i]["entity"]
                    self.set_selected(dev, [x for x in sel if x != e] if e in sel else sel + [e])
                return True
            return False
        if self.mode == "recording":
            if code == hw.ESC and down:
                self.rec.cancel(); self.mode = "pick"; app.say("Cancelled"); return True
            if code == SPACE and not down:
                # Stopping pw-record and levelling take a moment: on a worker, with "Sending…" shown
                self.mode, self.send_started, self.job = "sending", now, self.job + 1
                self.worker = threading.Thread(target=self._finish, args=(self.job,), daemon=True)
                self.worker.start()
                app.dirty = True
            return True
        if self.mode == "confirm":
            if down and code == hw.ENTER:
                self.mode = "sending"; app.dirty = True
                self.send_started = time.monotonic()
                threading.Thread(target=self._send_clip, args=(dev, self.clip), daemon=True).start()
            elif down and code == hw.ESC:
                self.clip = None; self.mode = "pick"; app.say("Discarded")
            elif down and code == SPACE:
                self.clip = None; self.rec.start(); self.mode = "recording"; app.dirty = True
            return True
        if self.mode == "alarm_hold":
            if letter == ALARM_KEY and not down:   # let go too early: nothing happens
                self.mode = "pick"; self.alarm_down = None; app.say("Alarm: hold 3 s", 2)
            return True
        if self.mode == "alarm_confirm":
            if down and code == hw.ENTER:
                self.mode = "alarm_on"; app.dirty = True
                threading.Thread(target=self._alarm, args=(dev,), daemon=True).start()
            elif down:
                self.mode = "pick"; app.say("Alarm called off", 2)
            return True
        if self.mode == "alarm_on":
            if down and code in (hw.ESC, hw.ENTER, SPACE):
                threading.Thread(target=self._stop_all, args=(dev,), daemon=True).start()
                self.mode = "pick"; app.say("Stopping", 2)
            return True
        if self.mode == "sending" and down and code == hw.ESC:   # never stuck on "Sending"
            note("ESC while sending: gave up waiting")
            self.mode = "pick"; app.say("Gave up waiting", 2)
        return True   # sending: ignore other keys

    def _finish(self, job):
        clip, secs = self.rec.finish()
        if job != self.job or self.mode != "sending":   # given up on meanwhile (ESC)
            return
        self.clip, self.secs = clip, secs
        if not clip:
            note(f"clip too short or silent ({secs:.1f}s)")
            self.mode = "pick"; self.app.say("Too short or silent", 2)
        else:
            self.mode = "confirm"
        self.app.dirty = True

    def leave(self):
        """Leaving the card (or the app): a recording in progress is thrown away and the microphone closed."""
        if self.mode == "recording":
            self.rec.cancel()
            self.mode = "pick"

    def next_due(self, now):
        """When tick() next has something to do (None: nothing). 20 times a second only while a counter
        runs on screen (recording, alarm hold)."""
        if self.mode in ("recording", "alarm_hold"):
            return now + 0.05
        if self.mode == "alarm_confirm":
            return self.alarm_asked + ALARM_CONFIRM + 0.001
        if self.mode == "sending":
            return self.send_started + SEND_WAIT + 0.001
        return None

    def tick(self):
        """Timers: the alarm hold, the alarm confirm window, the recording limit."""
        now = time.monotonic()
        app = self.app
        if self.mode == "alarm_hold" and self.alarm_down is not None:
            if now - self.alarm_down >= ALARM_HOLD:
                self.mode, self.alarm_asked, self.alarm_down = "alarm_confirm", now, None
            app.dirty = True
        elif self.mode == "alarm_confirm" and now - self.alarm_asked > ALARM_CONFIRM:
            self.mode = "pick"; app.say("Alarm called off", 2)
        elif self.mode == "sending" and now - self.send_started > SEND_WAIT:
            note(f"no result after {SEND_WAIT}s")
            self.mode = "pick"; app.say("No answer from HA", 3)
        elif self.mode == "recording":
            if now - self.rec.started > MAX_SECS:
                self.key(SPACE, False)   # as if SPACE were let go
            app.dirty = True             # the seconds counter

    # ---- Home Assistant actions (background threads)
    def _send_clip(self, dev, clip):
        app = self.app
        try:
            url, _ = ha_config()
            media = "http://cardputer.invalid/hc-fake.wav" if FAKE else serve_once(clip, url)
            sel = self.selected(dev)
            ts = {t["entity"]: t for t in self.targets(dev)}
            ann = [e for e in sel if ts[e].get("announce")]
            plain = [e for e in sel if not ts[e].get("announce")]
            # The card's own volume picker; Sonos announce applies it to the clip only. HA's Sonos docs give
            # it as a percent (`extra: volume: 20`); a fraction like 0.2 played at the default loudness.
            vol = self.volume(dev)
            if ann:
                body = {"entity_id": ann, "media_content_id": media, "media_content_type": "music", "announce": True}
                if vol is not None:
                    body["extra"] = {"volume": vol}
                ha_call("/api/services/media_player/play_media", body)
            if plain:
                ha_call("/api/services/media_player/play_media",
                        {"entity_id": plain, "media_content_id": media, "media_content_type": "music"})
            note(f"sent {self.secs:.1f}s to {', '.join(e.split('.', 1)[1] for e in sel)} at {vol}%")
            app.say(f"Sent to {len(sel)} ✓", 3)
        except Exception as e:
            ha_error(f"announce", e)
            app.say("Send failed", 3)
        self.clip = None
        if self.mode == "sending":
            self.mode = "pick"
        app.dirty = True

    def _alarm(self, dev):
        app = self.app
        try:
            ents = [t["entity"] for t in self.targets(dev)]
            ha_call("/api/services/media_player/play_media",
                    {"entity_id": ents, "media_content_id": dev["alarm"]["url"], "media_content_type": "music"})
            app.say("ALARM ON - ESC stops", 30)
        except Exception as e:
            ha_error(f"alarm", e)
            self.mode = "pick"; app.say("Alarm failed", 3)
        app.dirty = True

    def _stop_all(self, dev):
        try:
            ha_call("/api/services/media_player/media_stop", {"entity_id": [t["entity"] for t in self.targets(dev)]})
            self.app.say("Stopped", 2)
        except Exception as e:
            ha_error(f"stop", e)
            self.app.say("Stop failed", 3)

    # ---- drawing
    def draw(self, d):
        app, dev = self.app, self.app.dev
        from .app import F_BIG, F_SMALL, F_TITLE, PANEL, TXT, MUTED, BG
        url, why = ha_config()
        hint = {"pick": "SPACE hold: talk" if url else why}.get(self.mode, "")
        app.header(d, dev["name"], hint, dev.get("icon"))
        sel = self.selected(dev)
        cw, ch, gap = 33, 58, 2
        ts = self.targets(dev)
        for i, letter in enumerate("qwertyuio"):
            btn = None
            if i < len(ts):
                on = ts[i]["entity"] in sel
                btn = {"id": ts[i]["entity"], "label": ts[i]["label"], "symbol": "◉" if on else "○",
                       "group": "fan" if on else "mode"}
            app.key_cell(d, 2 + i * (cw + gap), 25, cw, ch, letter, btn)
            if btn and ts[i]["entity"] in sel:   # selected: a bright outline
                d.rounded_rectangle((2 + i * (cw + gap), 25, 2 + i * (cw + gap) + cw, 25 + ch), radius=5,
                                    outline=(40, 190, 170), width=2)
        i = "asdfghjkl".index(ALARM_KEY)   # only the alarm key is drawn on the A row; the rest is a status line
        app.key_cell(d, 2 + i * (cw + gap), 25 + ch + 3, cw, ch, ALARM_KEY,
                     {"id": "alarm", "label": "ALARM", "symbol": "⚠", "group": "pwr"})
        y = 25 + ch + 3
        vol = self.volume(dev)
        app.key_cell(d, 2, y, cw, ch, VOL_DOWN, {"id": "vol_down", "label": "VOL", "symbol": "\u25c0", "group": "heat"})
        app.key_cell(d, 2 + cw + gap, y, cw, ch, VOL_UP, {"id": "vol_up", "label": "VOL", "symbol": "\u25b6", "group": "heat"})
        x = 2 + 2 * (cw + gap) + 6
        d.text((x, y + 4), f"Volume {vol}%", font=F_TITLE, fill=TXT)
        bw = 2 + 6 * (cw + gap) - x - 8   # a bar in tenths
        for k in range(10):
            bx = x + k * bw / 10
            d.rectangle((bx, y + 22, bx + bw / 10 - 2, y + 30), fill=(240, 140, 40) if k < vol // 10 else (40, 44, 54))
        d.text((x, y + 34), f"{len(sel)} of {len(ts)} speakers.  Hold SPACE to talk.", font=F_SMALL, fill=MUTED)
        d.text((x, y + 46), "Hold L 3 s + ENTER: alarm.", font=F_SMALL, fill=MUTED)
        # overlays
        box = (16, 34, 304, 136)
        if self.mode == "recording":
            secs = time.monotonic() - self.rec.started
            self._panel(d, box, (200, 40, 40), f"●  Recording  {secs:4.1f} s",
                        "Let go of SPACE to stop.  ESC cancels.")
        elif self.mode == "confirm":
            self._panel(d, box, (40, 150, 90), f"Send {self.secs:.1f} s to {len(sel)} speaker{'s' if len(sel) != 1 else ''}?",
                        "ENTER send   ESC discard   SPACE redo")
        elif self.mode == "sending":
            self._panel(d, box, (70, 150, 240), "Sending…", "")
        elif self.mode == "alarm_hold":
            left = max(0.0, ALARM_HOLD - (time.monotonic() - (self.alarm_down or time.monotonic())))
            self._panel(d, box, (220, 60, 60), f"Keep holding L  {left:3.1f}", "Let go to call it off.")
        elif self.mode == "alarm_confirm":
            left = max(0.0, ALARM_CONFIRM - (time.monotonic() - self.alarm_asked))
            self._panel(d, box, (220, 60, 60), "Sound the ALARM everywhere?",
                        f"ENTER yes   any other key no   ({left:.0f} s)")
        elif self.mode == "alarm_on":
            self._panel(d, box, (220, 60, 60), "ALARM ON", "ESC, ENTER or SPACE stops it")

    def _panel(self, d, box, colour, title, sub):
        from .app import F_TITLE, F_SMALL, TXT, BG
        d.rounded_rectangle(box, radius=8, fill=BG, outline=colour, width=3)
        x0, y0, x1, y1 = box
        d.text(((x0 + x1 - d.textlength(title, font=F_TITLE)) / 2, y0 + 28), title, font=F_TITLE, fill=colour)
        if sub:
            d.text(((x0 + x1 - d.textlength(sub, font=F_SMALL)) / 2, y0 + 56), sub, font=F_SMALL, fill=TXT)
