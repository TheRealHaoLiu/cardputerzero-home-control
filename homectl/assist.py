"""The Assistant card: hold SPACE and ask, and Home Assistant's Assist answers on this device's speaker.

One turn is one run of an Assist pipeline (Settings > Voice assistants in Home Assistant): the clip goes
to Home Assistant, which turns it into words, hands them to the pipeline's conversation agent, and
speaks the answer. All the speech work and the thinking are Home Assistant's; this device only records,
shows the words both ways, and plays the answer. Which pipeline is the card's choice (key 7), so the
same card works with the built-in agent (commands for the house) and with a language model.

Assist pipelines are only reachable over Home Assistant's WebSocket API, so this file carries a small
WebSocket client (the standard library has none). One connection per turn, closed afterwards.

Needs the same two files as the Announce card (ha.json, ha-token; see announce.py).

Tests: HC_HA_FAKE=1 answers every turn from here, with a line in the test log and no network or
sound; HC_FAKE_MIC=1 records a tone instead of the microphone.
"""
import base64, io, json, os, signal, socket, ssl, struct, subprocess, sys, threading, time, urllib.parse, wave

import numpy as np

from . import hw
from .announce import FAKE, Recorder, _dies_with, _LIBC, ha_config, ha_error, log

SPACE, BACKSPACE = 57, 14
UP, DOWN, LEFT, RIGHT = 103, 108, 105, 106
VOL_KEYS = {LEFT: -10, RIGHT: 10, 114: -10, 115: 10}   # the keyboard's own left / right and volume keys
SCROLL_KEYS = {UP: 1, DOWN: -1, 33: 1, 45: -1}         # ... its up / down, and F / X (the Zero's arrows)
MAX_SECS = 30            # longest question
RATE = 16000             # what an Assist pipeline takes: 16 kHz, 16-bit, mono
CHUNK = 8192             # bytes of the clip per WebSocket message
TURN_WAIT = 90           # seconds on "Thinking" before the card gives up and says so
WS_WAIT = 60             # seconds without a word from Home Assistant before a turn is called dead
MAX_MSG = 4 << 20        # bytes: no message from Home Assistant in a turn comes near this
MAX_AUDIO = 8 << 20      # bytes: a spoken answer larger than this is not fetched
KEEP_TURNS = 30          # questions and answers kept on screen
VOL_DEFAULT = 70
REPLY = os.path.join(hw.RUN, "hc-assist-reply")


def note(line):
    print(f"{time.strftime('%H:%M:%S')} assist: {line}", file=sys.stderr, flush=True)


# ---- WebSocket ----------------------------------------------------------------------------------------
class Ws:
    """A WebSocket client, as much of RFC 6455 as Home Assistant's API uses: text and binary messages
    both ways, pings answered, no extensions."""

    def __init__(self, url, timeout=WS_WAIT):
        u = urllib.parse.urlsplit(url)
        tls = u.scheme == "https"
        port = u.port or (443 if tls else 80)
        s = socket.create_connection((u.hostname, port), timeout=10)
        try:
            if tls:
                s = ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f"GET {u.path.rstrip('/')}/api/websocket HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                part = s.recv(4096)
                if not part or len(head) > 16384:
                    raise OSError("no WebSocket answer")
                head += part
            head, self.buf = head.split(b"\r\n\r\n", 1)
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise OSError("WebSocket refused: " + head.split(b"\r\n", 1)[0].decode("latin-1")[:60])
            s.settimeout(timeout)
        except BaseException:
            s.close()
            raise
        self.s = s

    def _read(self, n):
        while len(self.buf) < n:
            part = self.s.recv(65536)
            if not part:
                raise ConnectionError("Home Assistant closed the connection")
            self.buf += part
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, op, data):
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     b"\xfe" + struct.pack(">H", n) if n < 65536 else b"\xff" + struct.pack(">Q", n))
        mask = os.urandom(4)   # a client's messages are always masked
        body = (np.frombuffer(data, dtype=np.uint8) ^ np.resize(np.frombuffer(mask, dtype=np.uint8), n)).tobytes() if n else b""
        self.s.sendall(head + mask + body)

    def send(self, obj):
        self._send(1, json.dumps(obj).encode())

    def send_bytes(self, data):
        self._send(2, data)

    def recv(self):
        """The next message, parsed (binary ones are skipped: Home Assistant sends none here)."""
        data = b""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 15, b1 & 127
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n + len(data) > MAX_MSG:
                raise ConnectionError("message too large")
            mask = self._read(4) if b1 & 128 else None
            part = self._read(n)
            if mask:
                part = (np.frombuffer(part, dtype=np.uint8) ^ np.resize(np.frombuffer(mask, dtype=np.uint8), n)).tobytes()
            if op == 9:
                self._send(10, part); continue      # ping: answer it
            if op == 10:
                continue
            if op == 8:
                raise ConnectionError("Home Assistant closed the connection")
            data += part
            if b0 & 128:
                if op == 2:
                    data = b""; continue
                return json.loads(data)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def connect():
    """A logged-in connection to Home Assistant's WebSocket API. Raises RuntimeError with words for the screen."""
    url, token = ha_config()
    if not url:
        raise RuntimeError(token)
    ws = Ws(url)
    try:
        if ws.recv().get("type") != "auth_required":
            raise RuntimeError("Not Home Assistant?")
        ws.send({"type": "auth", "access_token": token})
        if ws.recv().get("type") != "auth_ok":
            raise RuntimeError("Token refused")
    except BaseException:
        ws.close()
        raise
    return ws


def ask(ws, msg):
    """Send one command and wait for its result."""
    ws.send(msg)
    while True:
        r = ws.recv()
        if r.get("id") == msg["id"] and r.get("type") == "result":
            if not r.get("success"):
                raise RuntimeError((r.get("error") or {}).get("message") or "Home Assistant said no")
            return r.get("result")


def pipelines():
    """([{"id", "name"}, ...], id of Home Assistant's preferred one)."""
    if FAKE:
        log("assist pipelines")
        return [{"id": "p-home", "name": "Home Assistant"}, {"id": "p-model", "name": "Test Model"}], "p-home"
    ws = connect()
    try:
        r = ask(ws, {"id": 1, "type": "assist_pipeline/pipeline/list"})
    finally:
        ws.close()
    return [{"id": p["id"], "name": p.get("name") or p["id"]} for p in r.get("pipelines", [])], r.get("preferred_pipeline")


def converse(on, pipeline=None, conversation=None, pcm=None, text=None, speak=True, hold=None):
    """One turn. `pcm` (16 kHz mono 16-bit samples) or `text` is the question. on(kind, value) is called
    as things arrive: "heard" (the question as words), "partial" (the answer so far, from agents that
    write it out as they go), "reply" (the whole answer). `hold` receives the connection, so the caller
    can close it to give up. Returns {"conversation", "audio" (a URL path, or None), "more"}."""
    if FAKE:
        log(f"assist run {'clip ' + str(len(pcm)) + ' bytes' if pcm is not None else 'text ' + json.dumps(text)}"
            f" pipeline={pipeline} conversation={conversation} speak={speak}")
        time.sleep(0.4)
        if pcm is not None:
            on("heard", "turn on the test light")
        time.sleep(0.4)
        on("reply", "Turned on the test light." if not conversation else "It was already on.")
        return {"conversation": "c-1", "audio": "/fake.wav" if speak else None, "more": False}
    ws = connect()
    if hold is not None:
        hold.append(ws)
    out = {"conversation": conversation, "audio": None, "more": False}
    try:
        msg = {"id": 1, "type": "assist_pipeline/run", "start_stage": "stt" if pcm is not None else "intent",
               "end_stage": "tts" if speak else "intent", "timeout": TURN_WAIT,
               # no_vad: the key says when the question ends, so nothing is cut at a pause
               "input": {"sample_rate": RATE, "no_vad": True} if pcm is not None else {"text": text}}
        if pipeline:
            msg["pipeline"] = pipeline
        if conversation:
            msg["conversation_id"] = conversation
        ws.send(msg)
        partial = ""
        while True:
            r = ws.recv()
            if r.get("id") != 1:
                continue
            if r.get("type") == "result":
                if not r.get("success"):
                    raise RuntimeError((r.get("error") or {}).get("message") or "Assist said no")
                continue
            ev = r.get("event") or {}
            kind, data = ev.get("type"), ev.get("data") or {}
            if kind == "run-start" and pcm is not None:
                tag = bytes([data["runner_data"]["stt_binary_handler_id"]])
                for i in range(0, len(pcm), CHUNK):
                    ws.send_bytes(tag + pcm[i:i + CHUNK])
                ws.send_bytes(tag)             # an empty piece: the end of the clip
            elif kind == "stt-end":
                on("heard", (data.get("stt_output") or {}).get("text", "").strip())
            elif kind == "intent-progress":
                piece = (data.get("chat_log_delta") or {}).get("content")
                if isinstance(piece, str) and piece:
                    partial += piece
                    on("partial", partial)
            elif kind == "intent-end":
                io_ = data.get("intent_output") or {}
                speech = ((io_.get("response") or {}).get("speech") or {}).get("plain") or {}
                on("reply", (speech.get("speech") or "").strip())
                out["conversation"] = io_.get("conversation_id") or conversation
                out["more"] = bool(io_.get("continue_conversation"))
            elif kind == "tts-end":
                out["audio"] = (data.get("tts_output") or {}).get("url")
            elif kind == "error":
                raise RuntimeError(data.get("message") or data.get("code") or "Assist failed")
            elif kind == "run-end":
                return out
    finally:
        ws.close()


def to_pcm(wav):
    """The recorder's levelled mono WAV -> 16 kHz 16-bit samples (each group of samples averaged: a rough
    low-pass, enough for speech)."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    step = max(1, rate // RATE)
    return x[: len(x) // step * step].reshape(-1, step).mean(axis=1).astype("<i2").tobytes()


def fetch(path):
    """The spoken answer, from the address Home Assistant gave for it (no token: the address is the key)."""
    import urllib.request
    url, _ = ha_config()
    with urllib.request.urlopen(urllib.parse.urljoin(url + "/", path), timeout=20) as r:
        data = r.read(MAX_AUDIO + 1)
    if len(data) > MAX_AUDIO:
        raise RuntimeError("Answer too long to play")
    return data


def words(e):
    """An error as words for the screen."""
    if isinstance(e, RuntimeError):
        return str(e)[:120] or "Assist failed"
    if isinstance(e, TimeoutError):
        return "No answer from Home Assistant"
    return "Can't reach Home Assistant"


# ---- the card -----------------------------------------------------------------------------------------
class Assist:
    def __init__(self, app):
        self.app = app
        self.rec = Recorder()
        self.mode = "idle"          # idle | recording | thinking | speaking
        self.stage = ""             # what "thinking" is waiting for, for the header
        self.ate_esc = False        # an ESC press used here; its release must not also leave the card
        self.job, self.worker = 0, None   # the turn in progress; a newer job number orphans an older turn
        self.started = 0.0          # when the turn in progress was sent
        self.turns = []             # [who, text]: "you" | "ha" | "ha…" (still being written) | "err"
        self.scroll = 0             # lines scrolled back from the newest
        self.conversation = None    # Home Assistant's id for this conversation, so a follow-up has context
        self.pipes, self.preferred = None, None   # None: not asked yet; "busy"; else the list
        self.ws = []                # the connection of the turn in progress, to close when giving up
        self.player = None
        self._wrapped = {}

    @property
    def busy(self):
        return self.mode != "idle"

    # ---- choices, kept in the settings file
    def _get(self, key, dev, default):
        return self.app.settings.get(key, {}).get(dev["id"], default)

    def _set(self, key, dev, value):
        self.app.settings.setdefault(key, {})[dev["id"]] = value
        self.app.save_soon()
        self.app.dirty = True

    def volume(self, dev):
        return int(self._get("assist_volume", dev, VOL_DEFAULT))

    def sound(self, dev):
        return bool(self._get("assist_sound", dev, True))

    def chosen(self, dev):
        """(id, name) of the pipeline this card uses: the one picked here, else the one the card's file
        names, else Home Assistant's preferred one. (None, "") until the list is known: Home Assistant
        then uses its preferred one."""
        pipes = self.pipes if isinstance(self.pipes, list) else []
        by_id = {p["id"]: p for p in pipes}
        want = self._get("assist_pipeline", dev, None)
        if want not in by_id:
            want = next((p["id"] for p in pipes if p["name"] == dev.get("pipeline")), self.preferred)
        p = by_id.get(want)
        return (p["id"], p["name"]) if p else (None, "")

    def load(self):
        """Ask Home Assistant which pipelines there are (once, in the background)."""
        if self.pipes == "busy":
            return
        self.pipes = "busy"

        def run():
            try:
                self.pipes, self.preferred = pipelines()
            except Exception as e:
                ha_error("assist pipelines", e)
                self.pipes = []
            self.app.dirty = True
        threading.Thread(target=run, daemon=True).start()

    # soft keys on this card: 4 Devices, 5/6 device, 7 the agent, 8 spoken answers on or off
    def soft_state(self):
        many = len(self.app.devices) > 1
        idle = self.mode == "idle"
        pipes = self.pipes if isinstance(self.pipes, list) else None
        return {"4": ("Devices", True), "5": ("◀ Dev", many), "6": ("Dev ▶", many),
                "7": ("Agent", idle and pipes is not None and len(pipes) != 1),
                "8": ("Spoken" if self.sound(self.app.dev) else "Silent", idle)}

    def soft(self, k):
        app, dev = self.app, self.app.dev
        if k == "7":
            if not self.pipes:          # the list never arrived: ask again
                self.load(); app.say("Asking Home Assistant…", 2); return
            ids = [p["id"] for p in self.pipes]
            cur = self.chosen(dev)[0]
            self._set("assist_pipeline", dev, ids[(ids.index(cur) + 1) % len(ids) if cur in ids else 0])
            self.conversation = None    # another agent knows nothing of this conversation
            app.say(self.chosen(dev)[1], 2)
        elif k == "8":
            self._set("assist_sound", dev, not self.sound(dev))
            app.say("Answers are spoken" if self.sound(dev) else "Answers on screen only", 2)

    # ---- keys
    def key(self, code, down):
        """Returns True when the key was used here."""
        app, dev = self.app, self.app.dev
        if self.mode == "recording":
            if code == hw.ESC and down:
                self.rec.cancel(); self.mode = "idle"; app.say("Cancelled")
            elif code == SPACE and not down:
                self._send_clip()
            return True
        if code == SPACE:
            if down and self.mode != "thinking":      # while it speaks, SPACE cuts in with the next question
                url, why = ha_config()
                if not url:
                    app.say(why, 3)
                elif self.worker and self.worker.is_alive() and self.mode == "idle":
                    app.say("One moment", 1)          # the last clip is still being read: it uses the file
                else:
                    if self.mode == "speaking":
                        self.give_up()                # the answer being spoken is cut off and its turn is over
                    self.rec.start(); self.mode, self.scroll = "recording", 0; app.dirty = True
            return True
        if code == hw.ESC:
            if down and self.mode == "thinking":
                note("ESC while thinking: gave up waiting")
                self.give_up(); app.say("Gave up waiting", 2)
            elif down:
                self.hush()
            return True
        if not down:
            return False
        if code in VOL_KEYS:
            self._set("assist_volume", dev, max(10, min(100, self.volume(dev) + VOL_KEYS[code])))
            app.say(f"Volume {self.volume(dev)}%", 1.5)
            return True
        if code in SCROLL_KEYS:
            self.scroll = max(0, self.scroll + 3 * SCROLL_KEYS[code]); app.dirty = True
            return True
        if code == BACKSPACE and self.mode == "idle":
            self.turns, self.conversation, self.scroll = [], None, 0
            app.say("New conversation", 1.5)
            return True
        return False

    def _send_clip(self):
        self.job += 1
        self.mode, self.stage, self.started = "thinking", "Sending…", time.monotonic()
        dev = self.app.dev

        def run(job):
            clip, secs = self.rec.finish()        # stopping pw-record and levelling take a moment
            if job != self.job:
                return
            if not clip:
                note(f"clip too short or silent ({secs:.1f}s)")
                self.mode = "idle"; self.app.say("Too short or silent", 2)
                return
            self._turn(job, dev, pcm=to_pcm(clip))
        self.worker = threading.Thread(target=run, args=(self.job,), daemon=True)
        self.worker.start()
        self.app.dirty = True

    def ask(self, text):
        """A typed question (keyboard mode)."""
        self.give_up()
        self.mode, self.stage, self.started, self.scroll = "thinking", "Thinking…", time.monotonic(), 0
        self._say("you", text)
        threading.Thread(target=self._turn, args=(self.job, self.app.dev), kwargs={"text": text}, daemon=True).start()
        self.app.dirty = True

    def _say(self, who, text):
        self.turns.append([who, text])
        del self.turns[:-2 * KEEP_TURNS]
        if len(self._wrapped) > 8 * KEEP_TURNS:
            self._wrapped.clear()

    # ---- one turn (a background thread)
    def _turn(self, job, dev, pcm=None, text=None):
        app, t0 = self.app, time.monotonic()
        times = {}

        def on(kind, value):
            if job != self.job:
                return
            times.setdefault(kind, time.monotonic() - t0)
            if kind == "heard":
                self._say("you", value or "(nothing heard)")
                self.stage = "Thinking…"
            elif self.turns and self.turns[-1][0] == "ha…":
                self.turns[-1][:] = ["ha…" if kind == "partial" else "ha", value or self.turns[-1][1]]
            else:
                self._say("ha…" if kind == "partial" else "ha", value or "(no answer)")
            app.dirty = True

        if pcm is not None:
            self.stage = "Hearing…"; app.dirty = True
        del self.ws[:]
        try:
            out = converse(on, pipeline=self.chosen(dev)[0], conversation=self.conversation, pcm=pcm, text=text,
                           speak=self.sound(dev), hold=self.ws)
        except Exception as e:
            if job != self.job:      # given up on meanwhile: the closed connection is why it failed
                return
            ha_error("assist", e)
            if self.turns and self.turns[-1][0] == "ha…":
                self.turns[-1][0] = "ha"
            self._say("err", words(e))
            self.mode = "idle"; app.dirty = True
            return
        if job != self.job:
            return
        self.conversation = out["conversation"]
        note("turn: " + ", ".join(f"{k} after {v:.1f}s" for k, v in times.items() if k != "partial")
             + f", done after {time.monotonic() - t0:.1f}s")
        if out["audio"] and self.sound(dev):
            self._speak(job, dev, out["audio"])
        if job == self.job:
            self.mode = "idle"; app.dirty = True

    def _speak(self, job, dev, path):
        if FAKE:
            log(f"assist play {path} volume={self.volume(dev)}")
            self.mode = "speaking"; self.app.dirty = True
            time.sleep(0.8)
            return
        ext = os.path.splitext(urllib.parse.urlsplit(path).path)[1].lower()
        file = REPLY + (ext if ext in (".mp3", ".wav", ".flac", ".ogg") else ".mp3")
        try:
            data = fetch(path)
            if job != self.job:
                return
            with open(file, "wb") as f:
                f.write(data)
            self.player = subprocess.Popen(["pw-play", "--volume", f"{self.volume(dev) / 100:.2f}", file],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.mode = "speaking"; self.app.dirty = True
            self.player.wait()
        except Exception as e:
            ha_error("assist speech", e)
            if job == self.job:
                self.app.say("Could not play the answer", 2)
        finally:
            self.player = None
            try:
                os.unlink(file)
            except OSError:
                pass

    def hush(self):
        """Stop the spoken answer, if one is playing."""
        p = self.player
        if p:
            try:
                p.terminate()
            except OSError:
                pass

    def give_up(self):
        """Orphan the turn in progress: whatever it still brings is dropped."""
        self.job += 1
        for ws in self.ws:
            ws.close()
        self.hush()
        if self.turns and self.turns[-1][0] == "ha…":
            self.turns[-1][0] = "ha"
        self.mode = "idle"; self.app.dirty = True

    def leave(self):
        """Leaving the card (or the app): the microphone is closed, the speaker stops, a turn in progress
        is dropped. What was said stays on the card until the app closes."""
        if self.mode == "recording":
            self.rec.cancel()
        if self.mode != "idle":
            self.give_up()

    def next_due(self, now):
        if self.mode == "recording":
            return now + 0.05                      # the seconds counter
        if self.mode == "thinking":
            return self.started + TURN_WAIT + 5.001
        return None

    def tick(self):
        now = time.monotonic()
        if self.mode == "recording":
            if now - self.rec.started > MAX_SECS:
                self.key(SPACE, False)             # as if SPACE were let go
            self.app.dirty = True
        elif self.mode == "thinking" and now - self.started > TURN_WAIT + 5:
            note(f"no result after {TURN_WAIT + 5}s")
            self.give_up(); self._say("err", "No answer from Home Assistant")

    # ---- drawing
    def _lines(self, d, font, width):
        """The conversation as wrapped lines: (text, colour, starts a turn)."""
        from .app import TXT
        out = []
        for who, text in self.turns:
            key = (who, text)
            if key not in self._wrapped:
                lead = "› " if who == "you" else ""
                rows, row = [], lead
                for word in (text + ("…" if who == "ha…" else "")).split():
                    while d.textlength(word, font=font) > width:      # a word wider than the screen: cut it
                        n = next(i for i in range(len(word), 0, -1) if d.textlength(word[:i], font=font) <= width)
                        if row.strip():
                            rows.append(row.rstrip()); row = ""
                        rows.append(word[:n]); word = word[n:]
                    if row.strip() and d.textlength(row + word, font=font) > width:
                        rows.append(row.rstrip()); row = ""
                    row += word + " "
                if row.strip():
                    rows.append(row.rstrip())
                self._wrapped[key] = rows or [lead]
            col = {"you": (120, 190, 255), "err": (240, 130, 110)}.get(who, TXT)
            out += [(r, col, i == 0) for i, r in enumerate(self._wrapped[key])]
        return out

    def draw(self, d):
        from .app import F_SMALL, F_TEXT, F_TITLE, MUTED, TXT, draw_icon
        app, dev = self.app, self.app.dev
        if self.pipes is None:
            self.load()
        url, why = ha_config()
        name = self.chosen(dev)[1]
        right = {"idle": name if url else why, "recording": "", "thinking": self.stage,
                 "speaking": "SPACE: next   ESC: quiet"}[self.mode]
        app.header(d, dev["name"], right, dev.get("icon"))
        top, bottom, lh = 25, hw.H - 24, 15
        if not self.turns and self.mode in ("idle", "recording"):
            draw_icon(d, "mic", (hw.W // 2 - 16, 32, hw.W // 2 + 16, 80), colour=MUTED)
            for y, text, font, col in ((88, "Hold SPACE and ask", F_TITLE, TXT),
                                       (108, "Fn+ENTER: type the question instead", F_SMALL, MUTED),
                                       (121, "◀ ▶ volume    BACKSPACE new conversation", F_SMALL, MUTED)):
                d.text(((hw.W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=col)
        else:
            lines = self._lines(d, F_TEXT, hw.W - 16)
            if self.mode == "thinking" and not (self.turns and self.turns[-1][0] == "ha…"):
                lines.append((self.stage, MUTED, True))
            fit = (bottom - top) // lh
            self.scroll = min(self.scroll, max(0, len(lines) - fit))
            end = len(lines) - self.scroll
            shown = lines[max(0, end - fit):end]
            y = top if len(lines) <= fit else bottom - len(shown) * lh
            for text, col, _ in shown:
                d.text((8, y), text, font=F_TEXT, fill=col)
                y += lh
            if end > fit:                          # more above / below: small marks at the right edge
                d.text((hw.W - 9, top), "▴", font=F_SMALL, fill=MUTED)
            if self.scroll:
                d.text((hw.W - 9, bottom - 12), "▾", font=F_SMALL, fill=MUTED)
        if self.mode == "recording":
            secs = time.monotonic() - self.rec.started
            app.ann._panel(d, (16, 34, 304, 136), (200, 40, 40), f"●  Listening  {secs:4.1f} s",
                           "Let go of SPACE to send.  ESC cancels.")
