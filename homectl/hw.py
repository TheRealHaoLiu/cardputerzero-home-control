"""The device: screen, keyboard, infrared. No desktop in between, like the launcher's own apps.
Screen and keys follow cardputerzero-herdr-voice-client's hw.py so both apps behave the same way."""
import fcntl, mmap, os, select, struct, subprocess, tempfile

import numpy as np

W, H = 320, 170
FBDEV = os.environ.get("HC_FBDEV", "/dev/fb0")
KEYDEV = os.environ.get("HC_KEYDEV", "/dev/input/event3")
TEST_KEYS = os.environ.get("HC_TEST_KEYS")    # a FIFO of "<code> <1|0>" lines, for driving the app in tests
IRDEV = os.environ.get("HC_IRDEV", "/dev/lirc0")  # gpio-ir-tx; /dev/lirc1 is the receiver
IR_LOG = os.environ.get("HC_IR_LOG")           # tests: write what would be sent here instead of sending


def _run_dir():
    """Where recordings, IR timings and screenshots go: RAM, never the SD card. The launcher sets
    XDG_RUNTIME_DIR; without it, the user's /run/user/<uid>, else /dev/shm, else the temp dir."""
    for d in (os.environ.get("HC_RUNDIR"), os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}", "/dev/shm"):
        if d and os.path.isdir(d):
            return d
    return tempfile.gettempdir()


RUN = _run_dir()
FBIOBLANK = 0x4611

ESC, ENTER = 1, 28
DIGIT = {str(d % 10): 1 + d for d in range(1, 11)}  # "1" -> 2 ... "9" -> 10, "0" -> 11
LETTER = {c: 16 + i for i, c in enumerate("qwertyuiop")}
LETTER.update({c: 30 + i for i, c in enumerate("asdfghjkl")})
LETTER.update({c: 44 + i for i, c in enumerate("zxcvbnm")})
EVENT = struct.Struct("llHHi")  # struct input_event on a 64-bit kernel
EV_KEY = 1


class Screen:
    def __init__(self):
        self.fd = os.open(FBDEV, os.O_RDWR)
        self.map = mmap.mmap(self.fd, W * H * 2)
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, 0)  # wake the panel if the launcher blanked it
        except OSError:
            pass

    def show(self, rgb):
        """rgb: an H x W x 3 uint8 array."""
        a = rgb.astype(np.uint16)
        rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        self.map.seek(0)
        self.map.write(rgb565.astype("<u2").tobytes())

    def close(self):
        self.map.close()
        os.close(self.fd)


class Keys:
    """Key presses and releases as (code, is_down) pairs. Auto-repeat is dropped. wake() makes a read()
    that is waiting return at once, so the app can sleep until a key or a background thread needs it."""

    def __init__(self):
        self.fds = []
        try:
            self.fds.append(os.open(KEYDEV, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            if not TEST_KEYS:
                raise
        self.test = None
        if TEST_KEYS:
            self.test = os.open(TEST_KEYS, os.O_RDWR | os.O_NONBLOCK)  # read-write: no end-of-file between writers
            self.fds.append(self.test)
        self.wake_r, self.wake_w = os.pipe()          # a self-pipe: works on any kernel, unlike eventfd
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.fds.append(self.wake_r)
        self.buf = b""

    def wake(self):
        try:
            os.write(self.wake_w, b"!")
        except BlockingIOError:
            pass                                      # a wake-up is already waiting

    def read(self, timeout):
        out = []
        ready, _, _ = select.select(self.fds, [], [], timeout)
        for fd in ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            if fd == self.wake_r:
                continue
            if fd == self.test:
                self.buf += data
                *lines, self.buf = self.buf.split(b"\n")
                for line in lines:
                    try:
                        code, down = line.split()
                        out.append((int(code), down == b"1"))
                    except ValueError:
                        pass
                continue
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, i)
                if typ == EV_KEY and value in (0, 1):
                    out.append((code, value == 1))
        return out


def ir_send(signal, label=""):
    """Send one raw signal ({"carrier", "duty", "raw"}) through ir-ctl. Returns None, or an error string."""
    raw = signal["raw"]
    lines = [f"{'pulse' if i % 2 == 0 else 'space'} {us}" for i, us in enumerate(raw)]
    if IR_LOG:
        with open(IR_LOG, "a") as f:
            f.write(f"{label} carrier={signal['carrier']} duty={signal['duty']} n={len(raw)}\n")
        return None
    with tempfile.NamedTemporaryFile("w", dir=RUN, prefix="hc-ir-", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        path = f.name
    try:
        r = subprocess.run(["ir-ctl", "-d", IRDEV, "-c", str(signal["carrier"]), "-D", str(signal["duty"]),
                            "--send", path], capture_output=True, text=True, timeout=10)
        return None if r.returncode == 0 else (r.stderr.strip() or f"ir-ctl exit {r.returncode}")
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    finally:
        os.unlink(path)


def wol_send(mac, broadcast="255.255.255.255", port=9):
    """Send a Wake-on-LAN magic packet. Returns None, or an error string."""
    import socket
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        if len(mac_bytes) != 6:
            return "bad MAC"
        packet = b"\xff" * 6 + mac_bytes * 16
        if IR_LOG:   # tests: log instead of sending
            with open(IR_LOG, "a") as f:
                f.write(f"wol {mac} -> {broadcast}:{port}\n")
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(3):   # a magic packet is fire-and-forget; send a few
                s.sendto(packet, (broadcast, port))
        return None
    except (OSError, ValueError) as e:
        return str(e)


def tcp_up(host, port, timeout=1.5):
    """True when something accepts a TCP connection at host:port (used to see a woken machine come up)."""
    import socket
    if IR_LOG:
        return True
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
