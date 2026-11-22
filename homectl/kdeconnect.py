"""A small KDE Connect client: pair once with an Android device, then send it remote-input packets.

Python standard library plus the `openssl` command (for the one-time certificate). The S6's arrows and
buttons go over KDE Connect (Wi-Fi) rather than ADB or Bluetooth HID. Only what the card needs:
discovery by a UDP hello, TLS, protocol 8 identities, pairing, and `kdeconnect.mousepad.request`.

Protocol notes (kdeconnect-kde, kdeconnect-android, pykdeconnect):
- Packets are JSON lines: {"id": <ms>, "type": ..., "body": {...}}.
- We send our identity by UDP to port 1716; the phone opens TCP to our tcpPort and sends its identity
  in plain text. The side that OPENED the TCP connection is the TLS server, so here we are the client.
- Protocol 8: after TLS both sides send their identity again.
- Pairing: kdeconnect.pair {pair: true, timestamp}; the phone asks its user and answers {pair: true}.
  We keep the phone's device id and certificate fingerprint and refuse any other certificate later.
  That is trust on first use, so before asking, the card shows the name, address and key of the
  device that answered and waits for ENTER on the Cardputer (ESC: no).
- Every line read from the network has a length limit, and nothing waits on an unpaired peer past
  its deadline, so a stranger on the LAN cannot make the app allocate or hang.
- Android's MouseReceiver handles dx/dy (move), singleclick, doubleclick (recents), middleclick (home),
  backclick (back), singlehold/singlerelease (drag), scroll with dy (one step per 500).
"""
import hashlib, json, os, queue, re, select, socket, ssl, struct, subprocess, threading, time, uuid

CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "home-control", "kdeconnect")
PEERS = os.path.join(CONF, "peers.json")
PORT, PROTO = 1716, 8
CAPS_OUT = ["kdeconnect.mousepad.request", "kdeconnect.mpris.request", "kdeconnect.ping",
            "kdeconnect.share.request", "kdeconnect.battery.request"]
CAPS_IN = ["kdeconnect.ping", "kdeconnect.mpris", "kdeconnect.battery"]
FAKE = os.environ.get("HC_KDC_FAKE") == "1"   # tests: log packets instead of using the network
BT_SERVICE = "185f3df4-3268-4e3f-9fca-d4d5059915bd"            # KDE Connect's RFCOMM service
BT_MAIN = uuid.UUID("a0d0aaf4-1072-4d81-aa35-902a954b1266").bytes   # the multiplexer's default channel
BT_BUF = 4096
LAN_WAIT, BT_WAIT = 20, 40   # seconds to keep trying over Wi-Fi; over Bluetooth
HELLO_WAIT = 10              # seconds for the TLS handshake and the identity that follows it
CONFIRM_WAIT = 60            # seconds the card asks "pair with this device?" before it gives up
SEND_TIMEOUT = 2.0           # seconds a send may wait for the phone; longer, and the link counts as lost
HELLO_MAX = 8192             # longest identity or pair packet taken from a peer, in bytes
PACKET_MAX = 65536           # longest packet once paired (a now-playing update with long titles fits easily)
# TCP keepalive (idle s, probe interval s, probes) and TCP_USER_TIMEOUT (ms): a phone that vanishes
# (out of range, flat battery) is noticed in about 25 s, idle or not. Linux names; skipped where absent.
TCP_OPTS = (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 25000))


def _packet(typ, body):
    return (json.dumps({"id": int(time.time() * 1000), "type": typ, "body": body}) + "\n").encode()


class ProtocolError(OSError):
    """The peer broke the protocol (e.g. a line longer than allowed): the link is dropped."""


class PeersDamaged(Exception):
    """peers.json is there but cannot be read: never taken as "no phone paired yet"."""


def write_atomic(path, text, mode=0o600):
    """Write a small file so that a power cut leaves the old or the new one, never a torn one."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # pointer moves must not wait to be bundled
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in TCP_OPTS:
        opt = getattr(socket, name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass


def _cut(link):
    """Wake every thread blocked on the link (each gets an error) without closing the file descriptor
    under them; the connection's own thread closes it once they are out."""
    try:
        if isinstance(link, socket.socket):
            socket.socket.shutdown(link, socket.SHUT_RDWR)   # the plain socket's, so the TLS state stays intact
        else:
            link.close()                                     # BtLink: shuts its socket down
    except OSError:
        pass


class Lines:
    """Packets (JSON lines) from a socket, each at most `limit` bytes. With a deadline a read gives up
    then (TimeoutError). Without one it waits as long as the phone stays quiet: it blocks in select(),
    so the short send timeout on the same socket never ends an idle link; only the keepalive, a failed
    send or close() does."""

    def __init__(self, sock, limit=HELLO_MAX):
        self.sock, self.limit, self.buf = sock, limit, bytearray()

    def readline(self, deadline=None):
        """One line without its newline, or None once the peer has closed."""
        while True:
            i = self.buf.find(b"\n")
            if i > self.limit or (i < 0 and len(self.buf) > self.limit):
                raise ProtocolError(f"packet longer than {self.limit} bytes")
            if i >= 0:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("no packet in time")
                self.sock.settimeout(left)
            elif not getattr(self.sock, "pending", lambda: 0)():
                select.select([self.sock], [], [])
            try:
                data = self.sock.recv(4096)
            except TimeoutError:
                if deadline is not None:
                    raise
                continue          # only part of a TLS record has come: wait for the rest
            if not data:
                return None
            self.buf += data

    def __iter__(self):
        while (line := self.readline()) is not None:
            yield line


class BtLink:
    """KDE Connect over Bluetooth (for when the two are not on one Wi-Fi; the Android side is beta). One RFCOMM connection carries KDE Connect's multiplexer (kdeconnect-kde,
    core/backends/bluetooth/Multiplexing protocol.md). Frame: type (1 byte), data length (2, big-endian),
    channel UUID (16), data. Types: 0 version, 1 open, 2 close, 3 READ (the sender will take that many more
    bytes), 4 WRITE. Only the default channel is used here; frames for other channels are dropped.
    No TLS on this link: the Bluetooth bond encrypts it. Each identity carries a certificate, but nothing
    here proves the sender holds its key (see _run_bt)."""

    def __init__(self, sock):
        self.sock = sock
        self.credit = 0                       # bytes the phone has said it will take
        self.cv = threading.Condition()
        self.rx = queue.Queue()               # default-channel bytes; None = link closed
        self.wlock = threading.Lock()
        self._frame(0, struct.pack(">HH", 1, 1))
        self._frame(3, struct.pack(">H", BT_BUF))
        threading.Thread(target=self._read, daemon=True).start()

    def _frame(self, typ, data=b""):
        with self.wlock:
            self.sock.sendall(struct.pack(">BH", typ, len(data)) + BT_MAIN + data)

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            d = self.sock.recv(n - len(buf))
            if not d:
                raise OSError("closed")
            buf += d
        return buf

    def _read(self):
        try:
            while True:
                typ, length = struct.unpack(">BH", self._exact(3))
                chan, data = self._exact(16), self._exact(length)
                if typ == 0:
                    lo, hi = struct.unpack(">HH", data[:4])
                    if not lo <= 1 <= hi:
                        raise OSError("multiplexer version")
                elif chan != BT_MAIN:
                    pass
                elif typ == 3:
                    with self.cv:
                        self.credit += struct.unpack(">H", data[:2])[0]
                        self.cv.notify_all()
                elif typ == 4:
                    self.rx.put(data)
                    self._frame(3, struct.pack(">H", len(data)))   # taken at once, so the phone may send as much again
                elif typ == 2:
                    raise OSError("closed by the phone")
        except (OSError, struct.error):
            pass
        self.rx.put(None)
        with self.cv:
            self.cv.notify_all()

    def lines(self):
        """Packets (one JSON line each) from the phone, until the link closes."""
        buf = b""
        while True:
            d = self.rx.get()
            if d is None:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line
            if len(buf) > PACKET_MAX:
                return

    def sendall(self, data):
        while data:
            with self.cv:
                if not self.cv.wait_for(lambda: self.credit > 0, timeout=3):
                    raise OSError("the phone is not taking data")
                n = min(self.credit, BT_BUF, len(data))
                self.credit -= n
            self._frame(4, data[:n])
            data = data[n:]

    def close(self):
        for end in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):   # shutdown wakes the reader
            try:
                end()
            except OSError:
                pass


def bt_channel(addr):
    """The RFCOMM channel KDE Connect listens on (Android picks a new one each time it starts)."""
    try:
        out = subprocess.run(["sdptool", "browse", addr], capture_output=True, text=True, timeout=25).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for rec in re.split(r"\n(?=Service (?:Name|RecHandle):)", out):
        m = re.search(r"Channel: (\d+)", rec)
        if BT_SERVICE in rec.lower() and m:
            return int(m.group(1))
    return None


class Client:
    """One phone. connect() runs in the background; state is shown on the card."""

    def __init__(self, name="Cardputer", log=None, on_change=None):
        self.name, self.log, self.on_change = name, log, on_change
        self._state = "idle"         # idle | connecting | connected | pairing | not paired | no device | error | bad peers
        self.via = ""                # "wifi" or "bt" while connected
        self.transport = "wifi"      # which one to use; the user picks, nothing switches by itself
        self.tls = None              # the connected link, for sending
        self.conn = None             # the link being set up or served, so close() can cut it short
        self.lock = threading.Lock()     # guards the two references above; never held while sending
        self.wlock = threading.Lock()    # one sender at a time
        self.run_lock = threading.Lock() # start() against the end of a connection attempt
        self.busy = False            # a connection attempt (thread) is running
        self.stop = threading.Event()
        self.thread = None
        self.again = None            # pair flag for a connect asked for while the last one was still closing
        self.battery = None          # (percent, charging) as the phone last reported
        self.players = []            # media players on the phone (mpris)
        self.now = {}                # player -> its last state (title, artist, isPlaying, volume)
        self.asking = None           # while state is "confirm": {"name", "ip", "key"} of the device that answered
        self.answer = threading.Event()
        self.accepted = False

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):          # the card redraws on every change, whichever thread made it
        changed, self._state = value != self._state, value
        if changed and self.on_change:
            self.on_change()

    # ---- identity and storage
    def _setup(self):
        os.makedirs(CONF, mode=0o700, exist_ok=True)
        idf = os.path.join(CONF, "device_id")
        if not os.path.exists(idf):
            write_atomic(idf, uuid.uuid4().hex)       # once, on the first connect
        self.dev_id = open(idf).read().strip()
        self.cert, self.key = os.path.join(CONF, "cert.pem"), os.path.join(CONF, "key.pem")
        if not os.path.exists(self.cert):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-keyout", self.key, "-out", self.cert, "-days", "3650",
                            "-subj", f"/O=KDE/OU=Kde connect/CN={self.dev_id}"], check=True, capture_output=True)
            os.chmod(self.key, 0o600)

    def _peers(self):
        """The paired phones; {} when there is no file yet. A file that is there but does not parse raises
        PeersDamaged: taking it as "nothing paired" would quietly let the next device that answers pair."""
        try:
            with open(PEERS) as f:
                peers = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise PeersDamaged(f"{PEERS}: {e}") from None
        if not isinstance(peers, dict) or not all(isinstance(p, dict) for p in peers.values()):
            raise PeersDamaged(f"{PEERS}: not a map of devices")
        return peers

    def _save_peers(self, peers):
        write_atomic(PEERS, json.dumps(peers))

    def _identity(self):
        return _packet("kdeconnect.identity", {"deviceId": self.dev_id, "deviceName": self.name, "deviceType": "desktop",
                                               "protocolVersion": PROTO, "incomingCapabilities": CAPS_IN,
                                               "outgoingCapabilities": CAPS_OUT, "tcpPort": PORT})

    # ---- connection
    def start(self, pair=False):
        """Connect in the background (pair=True also asks the phone to pair if it is new). A no-op while an
        attempt is still running: a second listener on port 1716 would fail ("Address already in use")."""
        with self.run_lock:
            if self.busy:
                if self.stop.is_set():   # closing (e.g. Wi-Fi <-> Bluetooth): connect again once it has ended
                    self.again = pair
                    self.state = "connecting"
                return
            self.busy = True
            self.stop.clear()
            self.again = None
            self.thread = threading.Thread(target=self._run, args=(pair,), daemon=True)
            self.thread.start()

    def _run(self, pair):
        try:
            if FAKE:
                self.state = "connected"; return
            self._setup()
            try:
                peers = self._peers()
            except PeersDamaged as e:
                self._say(f"kdeconnect: pairing file damaged ({e}); delete it to pair again")
                self.state = "bad peers"; return
            if not peers and not pair:
                self.state = "not paired"; return
            self.state = "connecting"
            if self.transport == "bt":
                if any(p.get("bt") for p in peers.values()):
                    self._run_bt(peers)
                else:
                    self._say("kdeconnect: no Bluetooth address in peers.json"); self.state = "error"
                return
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PORT)); srv.listen(1); srv.settimeout(1)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            targets = [p["ip"] for p in peers.values() if p.get("ip")] + ["255.255.255.255"]
            conn, deadline = None, time.monotonic() + LAN_WAIT
            ignored = {}                 # who answered but is not the paired phone -> times (logged once each)
            try:
                while conn is None and not self.stop.is_set() and time.monotonic() < deadline:
                    for ip in targets:
                        try:
                            udp.sendto(self._identity(), (ip, PORT))
                        except OSError:
                            pass
                    try:
                        conn, addr = srv.accept()
                    except socket.timeout:
                        continue
                    try:                 # their identity, in plain text: bounded in size and in time
                        theirs = json.loads(Lines(conn, HELLO_MAX).readline(deadline) or b"null")["body"]
                        theirs = theirs if isinstance(theirs, dict) else {}
                    except (OSError, ValueError, KeyError, TypeError):
                        theirs = {}
                    # The hello is also broadcast, so any KDE Connect device in the house may answer. Once a
                    # phone is paired, only that phone is let in: never ask a stranger to pair.
                    if not theirs.get("deviceId") or (peers and theirs["deviceId"] not in peers):
                        who = (theirs.get("deviceName"), addr[0])
                        if who not in ignored:
                            self._say(f"kdeconnect: ignoring {who[0]!r} at {who[1]} (not the paired device)")
                        ignored[who] = ignored.get(who, 0) + 1
                        conn.close(); conn = None
            finally:
                srv.close(); udp.close()
                for (name, ip), n in ignored.items():
                    if n > 1:
                        self._say(f"kdeconnect: ignored {name!r} at {ip} {n} times in all")
            if conn is None:
                self.state = "no device"; return
            with self.lock:
                self.conn = conn
            _tune(conn)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # self-signed by design; checked against the pinned fingerprint below
            ctx.load_cert_chain(self.cert, self.key)
            conn.settimeout(HELLO_WAIT)
            if self.stop.is_set():
                return
            tls = ctx.wrap_socket(conn, server_side=False)
            with self.lock:
                self.conn = tls
            if self.stop.is_set():       # close() came while the handshake ran
                return
            fp = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            known = peers.get(theirs.get("deviceId"))
            if known and known.get("cert_sha256") != fp:
                self.state = "error"; self._say("certificate changed: refusing"); return
            lines = Lines(tls, HELLO_MAX)
            if theirs.get("protocolVersion", 7) >= 8:
                tls.sendall(self._identity())
                lines.readline(time.monotonic() + HELLO_WAIT)
            if not known:
                if not pair:
                    self.state = "not paired"; return
                # Trust on first use: whoever is pinned now is trusted from then on, and anything on the LAN
                # can answer the broadcast. So the user says yes on the Cardputer first, seeing what answered.
                self.accepted = False
                self.answer.clear()
                self.asking = {"name": str(theirs.get("deviceName") or "?")[:32], "ip": addr[0],
                               "key": ":".join(fp[i:i + 2] for i in range(0, 8, 2)).upper()}
                self.state = "confirm"
                self.answer.wait(CONFIRM_WAIT)
                self.asking = None
                if self.stop.is_set():
                    return
                if not self.accepted:
                    self._say(f"kdeconnect: pairing with {theirs.get('deviceName')!r} at {addr[0]} declined on the Cardputer")
                    self.state = "not paired"; return
                self.state = "pairing"
                tls.sendall(_packet("kdeconnect.pair", {"pair": True, "timestamp": int(time.time())}))
                answer_by = time.monotonic() + 60
                while True:
                    line = lines.readline(answer_by)
                    if line is None:
                        raise OSError("closed while pairing")
                    p = json.loads(line)
                    if p["type"] == "kdeconnect.pair":
                        if not p["body"].get("pair"):
                            self.state = "not paired"; return
                        break
            entry = dict(known or {"name": theirs.get("deviceName"), "cert_sha256": fp})
            entry["ip"] = addr[0]                              # remember where it was, for the next hello
            if entry != known:                                 # the SD card is written only when this changed
                peers[theirs["deviceId"]] = entry
                self._save_peers(peers)
            tls.settimeout(SEND_TIMEOUT)   # for sends; the reader waits in select() as long as the phone is quiet
            lines.limit = PACKET_MAX
            self._serve(tls, lines, "wifi")
        except Exception as e:
            self._say(f"kdeconnect: {e}")
            self.state = "error"
        finally:
            with self.lock:
                link, self.conn, self.tls = self.conn, None, None
            if link is not None:
                _cut(link)
                with self.wlock:          # no sender is inside it any more: now the descriptor can go
                    try:
                        link.close()
                    except OSError:
                        pass
            with self.run_lock:
                if self.stop.is_set() and self.again is None:
                    self.state = "idle"
                if self.again is not None:
                    pair, self.again = self.again, None
                    self.stop.clear()
                    self.state = "connecting"
                    self.thread = threading.Thread(target=self._run, args=(pair,), daemon=True)
                    self.thread.start()
                else:
                    self.busy = False
            if self.on_change:
                self.on_change()

    def _serve(self, link, lines, via):
        """Connected: ask for the players and the battery, then take packets until the phone goes away."""
        with self.lock:
            self.tls = self.conn = link
        if self.stop.is_set():            # close() came before the link was registered: it could not cut it
            self.state = "idle"; return
        self.via, self.state = via, "connected"
        self.packet("kdeconnect.mpris.request", {"requestPlayerList": True})
        self.packet("kdeconnect.battery.request", {"request": True})
        try:
            for line in lines:
                if self.stop.is_set():
                    break
                try:
                    self._incoming(json.loads(line))
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
        except OSError as e:              # the keepalive gave up, a send failed, or a packet was too long
            if not self.stop.is_set():
                self._say(f"kdeconnect: link lost ({e})")
        self.state = "idle" if self.stop.is_set() else "no device"

    def _run_bt(self, peers):
        """Bluetooth, for a phone already paired over Wi-Fi whose Bluetooth address is in peers.json ("bt").
        The two must be bonded in Bluetooth first, and the phone's KDE Connect needs "Enable bluetooth"."""
        dev_id, peer = next((k, p) for k, p in peers.items() if p.get("bt"))
        t0, sock, chan = time.time(), None, peer.get("bt_channel")
        while sock is None and not self.stop.is_set() and time.time() - t0 < BT_WAIT:
            chan = chan or bt_channel(peer["bt"])     # the S6 often does not answer the first few calls
            if not chan:
                self._say("kdeconnect bluetooth: no answer to the service lookup")
                time.sleep(1); continue
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            s.settimeout(12)
            try:
                s.connect((peer["bt"], chan)); sock = s
            except OSError as e:
                s.close()
                self._say(f"kdeconnect bluetooth: channel {chan}: {e.strerror or e}")
                if e.errno in (103, 111):             # refused or aborted: the channel number is stale
                    chan = None
                time.sleep(1)
        if sock is None:
            self.state = "no device"; return
        sock.settimeout(None)
        link = BtLink(sock)
        lines = link.lines()
        try:
            theirs = json.loads(next(lines))["body"]
            der = ssl.PEM_cert_to_DER_cert(theirs.get("certificate", ""))
            # Only a match against the pinned id and fingerprint: with no TLS handshake on this link nothing
            # proves the sender holds that certificate's key, so this catches the wrong phone, not an
            # impostor. What keeps others out is the Bluetooth bond (pairing and link encryption).
            if theirs.get("deviceId") != dev_id or hashlib.sha256(der).hexdigest() != peer.get("cert_sha256"):
                self._say("kdeconnect: Bluetooth peer is not the paired device: refusing")
                self.state = "error"; return
            me = json.loads(self._identity())
            me["body"]["certificate"] = open(self.cert).read()
            link.sendall((json.dumps(me) + "\n").encode())
            if chan != peer.get("bt_channel"):
                peer["bt_channel"] = chan
                self._save_peers(peers)
            self._serve(link, lines, "bt")
        except (StopIteration, ValueError, KeyError, OSError) as e:
            self._say(f"kdeconnect bluetooth: {e!r}")
            self.state = "error"
        finally:
            link.close()

    def _incoming(self, p):
        body = p.get("body", {})
        if p["type"] == "kdeconnect.battery":
            self.battery = (body.get("currentCharge"), body.get("isCharging"))
        elif p["type"] == "kdeconnect.mpris":
            if "playerList" in body:
                if body["playerList"] != self.players:
                    self._say(f"kdeconnect: players {body['playerList']}")
                self.players = body["playerList"]
                for pl in self.players:
                    self.packet("kdeconnect.mpris.request", {"player": pl, "requestNowPlaying": True, "requestVolume": True})
            if body.get("player"):
                self.now.setdefault(body["player"], {}).update(body)
                if "volume" not in body:   # the phone only tells its volume when asked; keep the card's number true
                    self.packet("kdeconnect.mpris.request", {"player": body["player"], "requestVolume": True})
        if self.on_change:
            self.on_change()

    def has_bt(self):
        """True when a paired phone has a Bluetooth address on file."""
        try:
            return any(p.get("bt") for p in self._peers().values())
        except PeersDamaged:
            return False

    def player(self):
        """The player to control: one that is playing, else the first the phone listed."""
        playing = [pl for pl in self.players if self.now.get(pl, {}).get("isPlaying")]
        return (playing or self.players or [None])[0]

    def media(self, action):
        """PlayPause / Next / Previous / Stop on the phone's current player."""
        pl = self.player()
        if not pl:
            self.packet("kdeconnect.mpris.request", {"requestPlayerList": True})
            return False
        return self.packet("kdeconnect.mpris.request", {"player": pl, "action": action})

    def volume(self, delta):
        pl = self.player()
        if not pl:
            return False
        st = self.now.setdefault(pl, {})
        st["volume"] = max(0, min(100, int(st.get("volume", 50)) + delta))
        return self.packet("kdeconnect.mpris.request", {"player": pl, "setVolume": st["volume"]})

    def share_url(self, url):
        """The phone opens the link (KDE Connect's share plugin)."""
        return self.packet("kdeconnect.share.request", {"url": url})

    def _say(self, msg):
        if self.log:
            self.log(msg)

    def send(self, body):
        """Send one kdeconnect.mousepad.request (pointer, clicks, keys). False if not connected."""
        return self.packet("kdeconnect.mousepad.request", body)

    def packet(self, typ, body):
        if FAKE:
            if self.log:
                self.log(f"kdc {typ.split('.')[-2] if typ.count('.') > 1 else typ} " + json.dumps(body, sort_keys=True))
            return True
        link = self.tls
        if not link:
            return False
        # A send waits at most SEND_TIMEOUT for the phone, and for another sender (the gyro at 100 Hz) at
        # most as long. A send that times out drops the link: part of a TLS record may be gone.
        if not self.wlock.acquire(timeout=SEND_TIMEOUT):
            return False
        try:
            if self.tls is not link:
                return False
            link.sendall(_packet(typ, body))
            return True
        except OSError as e:
            _cut(link)                    # inside the lock, so the next sender fails at once instead of waiting
            with self.lock:
                lost, self.tls = self.tls is link, (None if self.tls is link else self.tls)
            if lost:
                self._say(f"kdeconnect: send failed ({e})")
                self.state = "no device"
            return False
        finally:
            self.wlock.release()

    def confirm(self, ok):
        """The user's answer to "pair with this device?" (ENTER: True, ESC: False)."""
        self.accepted = bool(ok)
        self.answer.set()

    def close(self):
        """Drop the link now, without waiting for a sender stuck on a dead Wi-Fi link: the socket is shut
        down, which wakes that sender and the reader with an error; the connection's thread closes it."""
        self.stop.set()
        self.answer.set()
        with self.lock:
            link, self.tls = self.conn, None
        if link is not None:
            _cut(link)
        if not FAKE:
            self.state = "idle"


class Gyro:
    """Air mouse: while running, turns the BMI270's angular rate into pointer moves (hold Space to steer)."""
    DEV = os.environ.get("HC_GYRO_DEV", "/sys/bus/iio/devices/iio:device0")

    def __init__(self, send, cfg=None):
        cfg = cfg or {}
        self.send = send
        self.axes = (cfg.get("x_axis", "z"), cfg.get("y_axis", "x"))
        self.signs = (cfg.get("x_sign", -1), cfg.get("y_sign", -1))
        self.gain = cfg.get("gain", 900.0)        # pixels per radian
        self.dead = cfg.get("deadzone", 0.03)     # rad/s below which the hand counts as still
        self.hz = cfg.get("hz", 100)
        self.run = threading.Event()     # steering (shown on the card)
        self.done = None                 # the running loop's own off switch

    def start(self):
        if self.run.is_set():
            return
        self.run.set()
        # Each loop has its own switch: a loop told to stop always ends, even if start() comes again
        # within its tick, so two loops never send at once.
        self.done = threading.Event()
        threading.Thread(target=self._loop, args=(self.done,), daemon=True).start()

    def stop(self):
        self.run.clear()
        if self.done:
            self.done.set()

    def _loop(self, done):
        try:
            scale = float(open(os.path.join(self.DEV, "in_anglvel_scale")).read())
            files = [open(os.path.join(self.DEV, f"in_anglvel_{a}_raw")) for a in self.axes]
        except OSError:
            return
        fx = fy = 0.0
        period = 1.0 / self.hz
        while not done.is_set():
            t = time.monotonic()
            rates = []
            for f in files:
                f.seek(0)
                rates.append(int(f.read()) * scale)
            vx, vy = (s * r if abs(r) > self.dead else 0.0 for s, r in zip(self.signs, rates))
            fx += vx * self.gain * period
            fy += vy * self.gain * period
            dx, dy = int(fx), int(fy)
            if dx or dy:
                fx -= dx; fy -= dy
                self.send({"dx": dx, "dy": dy})
            done.wait(max(0.0, period - (time.monotonic() - t)))
        for f in files:
            f.close()
