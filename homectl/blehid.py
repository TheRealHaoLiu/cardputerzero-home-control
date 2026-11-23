"""The CardputerZero as a Bluetooth keyboard, gamepad and mouse for one device (HID over GATT).

Android takes all of it natively, with nothing installed on its side: the D-pad moves focus, Enter
opens, the consumer keys are Home / Back / Recents / volume / media, the mouse is a pointer. BlueZ
does the radio work; this module registers a GATT application, an advertisement and a pairing agent
over D-Bus (python3-dbus and python3-gi, both on the stock image) in one background thread, on its
own bus connection, so closing that connection takes all three away again.

What was learned on a RoamView S6 (Android 15), 2026-11-22:
- The adapter must be LE only (`ControllerMode = le` in /etc/bluetooth/main.conf). With classic
  Bluetooth also on, Android pairs, records the device as dual mode and never opens the HID service.
- The launcher opens an app on a real Enter key, not on gamepad A, and leaves a widget only on a real
  Escape, so there is a keyboard report next to the gamepad. Recents is consumer usage 0x29F.
- One host only. Another bonded device (a phone paired for something else) connects as soon as it
  sees a HID service; it is refused at the first read, so keys never reach it. Never `block` such a
  device in BlueZ instead: after a blocked device's connection ends, the kernel re-enables the
  advertising as non-connectable and nothing can connect any more.
- BlueZ will not redo a Pair-button pairing with a device it already knows (JustWorksRepairing =
  never), so the agent compares a code (DisplayYesNo) and the card asks ENTER / ESC.
"""
import json, os, struct, threading, time

from .kdeconnect import write_atomic

CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "home-control")
HOST = os.path.join(CONF, "blehid.json")          # {"addr": ..., "name": ...}: the one device let in
FAKE = os.environ.get("HC_HID_FAKE") == "1"       # tests: log reports instead of using Bluetooth
BASE = "/org/cardputerzero/homecontrol/hid"
ADAPTER = "/org/bluez/" + os.environ.get("HC_HID_ADAPTER", "hci0")   # e.g. hci1: a second controller for this alone
PROPS = "org.freedesktop.DBus.Properties"
CHRC, DESC, DEVICE, ADAPTER1 = ("org.bluez.GattCharacteristic1", "org.bluez.GattDescriptor1",
                                "org.bluez.Device1", "org.bluez.Adapter1")
CONFIRM_WAIT = 20            # seconds the card asks "pair with this device?": a pairing, and a read held
                             # back meanwhile, both give up at 25 to 30

REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01, 0x85, 0x01,   # report 1: gamepad
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02,  # 16 buttons
    0x05, 0x01, 0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x42,                # hat: 0..7 clockwise from up, 8 = centred
    0x65, 0x00, 0x75, 0x04, 0x95, 0x01, 0x81, 0x03,
    0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04,
    0x81, 0x02, 0xC0,                                  # four axes, held centred
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02,   # report 2: consumer control, one 16-bit usage
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0,
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x03,   # report 3: keyboard, modifiers + six keys
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x75, 0x08, 0x95, 0x01, 0x81, 0x01,
    0x19, 0x00, 0x29, 0x65, 0x15, 0x00, 0x25, 0x65, 0x75, 0x08, 0x95, 0x06, 0x81, 0x00, 0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x04, 0x09, 0x01, 0xA1, 0x00,   # report 4: mouse
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x03, 0x81, 0x02,
    0x75, 0x05, 0x95, 0x01, 0x81, 0x03,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03,
    0x81, 0x06, 0xC0, 0xC0,                            # dx, dy, wheel
])
IDLE = {"gamepad": struct.pack("<HB4B", 0, 8, 128, 128, 128, 128), "consumer": b"\0\0",
        "keyboard": bytes(8), "mouse": bytes(4)}

# What a card's key can send ("hid": <name>):
HAT = {"up": 0, "right": 2, "down": 4, "left": 6}
BUTTON = {"a": 0, "b": 1, "x": 3, "y": 4, "l1": 6, "r1": 7, "select": 10, "start": 11}
CONSUMER = {"home": 0x223, "back": 0x224, "recents": 0x29F, "vol_up": 0xE9, "vol_down": 0xEA, "mute": 0xE2,
            "play": 0xCD, "next": 0xB5, "prev": 0xB6, "menu": 0x40}
USAGE = {"enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C}
# Keyboard mode: the Cardputer's own keys (evdev codes, a US layout) as keyboard usages
EVKEY = {c: 0x04 + ord(ch) - 97 for row, first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44))
         for c, ch in enumerate(row, first)}
EVKEY.update({1 + d: 0x1D + d for d in range(1, 10)})
EVKEY.update({11: 0x27, 28: 0x28, 1: 0x29, 14: 0x2A, 15: 0x2B, 57: 0x2C, 12: 0x2D, 13: 0x2E, 26: 0x2F, 27: 0x30,
              43: 0x31, 39: 0x33, 40: 0x34, 41: 0x35, 51: 0x36, 52: 0x37, 53: 0x38, 58: 0x39, 111: 0x4C,
              106: 0x4F, 105: 0x50, 108: 0x51, 103: 0x52})
EVMOD = {29: 0x01, 42: 0x02, 56: 0x04, 125: 0x08, 97: 0x10, 54: 0x20, 100: 0x40}   # ctrl, shift, alt, meta


def _classes(dbus):
    """The D-Bus objects BlueZ talks to. Built on first use, so an app that never opens a Bluetooth card
    never loads dbus."""
    svc = dbus.service

    class Node(svc.Object):
        def __init__(self, bus, path, props, link=None, value=b""):
            super().__init__(bus, path)
            self.path, self.props, self.link, self.value, self.notifying = dbus.ObjectPath(path), props, link, value, False

        @svc.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return self.props

        @svc.method(PROPS, in_signature="ss", out_signature="v")
        def Get(self, iface, name):
            return self.props[name]

        @svc.signal(PROPS, signature="sa{sv}as")
        def PropertiesChanged(self, iface, changed, invalidated):
            pass

    class Char(Node):
        @svc.method(CHRC, in_signature="a{sv}", out_signature="ay", async_callbacks=("ok", "no"))
        def ReadValue(self, options, ok, no):                     # answered now, or once the user has
            self.link._read(str(options.get("device", "")), lambda: ok(dbus.Array(self.value, signature="y")), no)

        @svc.method(CHRC, in_signature="aya{sv}")
        def WriteValue(self, value, options):
            pass                                                  # control point, protocol mode: nothing to do

        @svc.method(CHRC)
        def StartNotify(self):
            self.notifying = True
            self.link._update()

        @svc.method(CHRC)
        def StopNotify(self):
            self.notifying = False
            self.link._update()

    class Desc(Node):
        @svc.method(DESC, in_signature="a{sv}", out_signature="ay")
        def ReadValue(self, options):
            return dbus.Array(self.value, signature="y")

    class App(svc.Object):
        def __init__(self, bus):
            super().__init__(bus, BASE)
            self.nodes = {}

        @svc.method("org.freedesktop.DBus.ObjectManager", out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            return self.nodes

    class Advert(svc.Object):
        def __init__(self, bus, name):
            super().__init__(bus, BASE + "/adv")
            self.name = name

        @svc.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return {"Type": "peripheral", "ServiceUUIDs": dbus.Array(["1812"], signature="s"),
                    "Appearance": dbus.UInt16(0x03C0), "LocalName": self.name, "Discoverable": dbus.Boolean(True)}

        @svc.method("org.bluez.LEAdvertisement1")
        def Release(self):
            pass

    class Agent(svc.Object):
        """Pairing: every request goes to the card, which shows the code and waits for ENTER or ESC."""

        def __init__(self, bus, link):
            super().__init__(bus, BASE + "/agent")
            self.link = link

        @svc.method("org.bluez.Agent1", in_signature="ou", async_callbacks=("ok", "no"))
        def RequestConfirmation(self, device, passkey, ok, no):
            self.link._ask(str(device), "%06d" % passkey, ok, no)

        @svc.method("org.bluez.Agent1", in_signature="o", async_callbacks=("ok", "no"))
        def RequestAuthorization(self, device, ok, no):
            self.link._ask(str(device), None, ok, no)

        @svc.method("org.bluez.Agent1", in_signature="os")
        def AuthorizeService(self, device, uuid):
            pass

        @svc.method("org.bluez.Agent1")
        def Cancel(self):
            self.link._asked(None)

        @svc.method("org.bluez.Agent1")
        def Release(self):
            pass

    return Char, Desc, Node, App, Advert, Agent


class Hid:
    """One host. start() advertises and lets it connect; state is shown on the card."""

    def __init__(self, name="CardputerZero", log=None, on_change=None):
        self.name, self.log, self.on_change = name, log, on_change
        self._state = "idle"     # idle | starting | waiting | connected | confirm | classic on | no bluetooth | error
        self.asking = None       # while state is "confirm": {"name", "addr", "code"} of the device asking
        self.lock = threading.Lock()     # the report state below, set from the app's thread and the gyro's
        self.run_lock = threading.Lock()
        self.thread, self.loop, self.glib, self.again, self.want = None, None, None, False, False
        self.reports = {}        # name -> its characteristic, while running
        self.buttons, self.held, self.cons, self.mods, self.keys, self.mouse = 0, [], [], 0, [], 0

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):      # the card redraws on every change, whichever thread made it
        changed, self._state = value != self._state, value
        if changed and self.on_change:
            self.on_change()

    def _say(self, msg):
        if self.log:
            self.log(f"hid: {msg}")

    # ---- the host on file
    @staticmethod
    def saved():
        try:
            with open(HOST) as f:
                host = json.load(f)
            return host if isinstance(host, dict) and host.get("addr") else None
        except (OSError, ValueError):
            return None

    # ---- life cycle
    def start(self):
        """Advertise and serve in the background. While a closing run is still ending, it starts again
        once that has ended (two runs would register the same objects twice)."""
        with self.run_lock:
            if self.thread and self.thread.is_alive():
                self.again = True
                return
            self.again, self.want = False, True
            self.state = "starting"
            self.thread = threading.Thread(target=self._thread, daemon=True)
            self.thread.start()

    def _thread(self):
        while True:
            try:
                self._run()
            except Exception as e:           # BlueZ not running, no adapter, a D-Bus error
                self._say(f"{type(e).__name__}: {e}")
                self.state = "no bluetooth" if "org.bluez" in str(e) or "ServiceUnknown" in str(e) else "error"
            with self.run_lock:
                if not self.again:
                    self.thread = None
                    return
                self.again, self.want = False, True
                self.state = "starting"

    def close(self, wait=0):
        """Stop: release every key, drop the host's link, take the service away. Returns at once, or
        (wait, in seconds: the app leaving) once that is done, so the host is not left on a dead link."""
        with self.run_lock:
            self.again, self.want = False, False
            loop, glib, thread = self.loop, self.glib, self.thread
        if loop is not None:
            glib.idle_add(self._stop)
        if wait and thread:
            thread.join(wait)
        if not FAKE:
            self.state = "idle"

    def _run(self):
        if FAKE:
            self.state = "connected"
            return
        import dbus, dbus.mainloop.glib, dbus.service
        from gi.repository import GLib
        Char, Desc, Node, App, Advert, Agent = _classes(dbus)
        dbus.mainloop.glib.threads_init()
        bus = dbus.SystemBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
        self.dbus, self.bus = dbus, bus
        self.host = self.saved()             # {"addr", "name"} or None: then the first device accepted becomes it
        self.host_path = f"{ADAPTER}/dev_{self.host['addr'].replace(':', '_')}" if self.host else None
        self.refused, self.pending, self.waiting = set(), None, []
        self.adapter = dbus.Interface(bus.get_object("org.bluez", ADAPTER), PROPS)
        if int(self.adapter.Get(ADAPTER1, "Class")):
            self._say("classic Bluetooth is on; Android needs LE only (ControllerMode = le in /etc/bluetooth/main.conf)")
            self.classic = True
        else:
            self.classic = False

        app = App(bus)
        self.nodes = [app, Advert(bus, self.name), Agent(bus, self)]
        def node(cls, path, iface, props, value=b""):
            n = cls(bus, path, props, self, value)
            app.nodes[n.path] = {iface: props}
            self.nodes.append(n)
            return n
        def char(name, uuid, flags, value=b"", ref=None):
            c = node(Char, f"{BASE}/s/{name}", CHRC, {"UUID": uuid, "Service": dbus.ObjectPath(BASE + "/s"),
                                                      "Flags": dbus.Array(flags, signature="s")}, value)
            if ref:                          # report reference: its id, and 1 = an input report
                node(Desc, f"{c.path}/ref", DESC, {"UUID": "2908", "Characteristic": c.path,
                                                   "Flags": dbus.Array(["read"], signature="s")}, bytes([ref, 1]))
            return c
        node(Node, BASE + "/s", "org.bluez.GattService1", {"UUID": "1812", "Primary": dbus.Boolean(True)})
        char("info", "2a4a", ["encrypt-read"], bytes([0x11, 0x01, 0x00, 0x03]))
        char("map", "2a4b", ["encrypt-read"], REPORT_MAP)
        char("control", "2a4c", ["write-without-response"])
        char("protocol", "2a4e", ["encrypt-read", "write-without-response"], b"\x01")
        self.reports = {name: char(name, "2a4d", ["encrypt-read", "encrypt-notify"], IDLE[name], ref)
                        for ref, name in enumerate(("gamepad", "consumer", "keyboard", "mouse"), 1)}

        # Pairing is open while the card is; every request still waits for ENTER on the Cardputer.
        self.was_pairable = bool(self.adapter.Get(ADAPTER1, "Pairable"))
        self.was_timeout = int(self.adapter.Get(ADAPTER1, "PairableTimeout"))
        self.adapter.Set(ADAPTER1, "PairableTimeout", dbus.UInt32(0))
        self.adapter.Set(ADAPTER1, "Pairable", dbus.Boolean(True))
        agents = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
        agents.RegisterAgent(BASE + "/agent", "DisplayYesNo")
        agents.RequestDefaultAgent(BASE + "/agent")
        loop = GLib.MainLoop()
        def failed(what):
            def cb(err):
                self._say(f"{what}: {err}")
                self.state = "error"
                loop.quit()
            return cb
        dbus.Interface(bus.get_object("org.bluez", ADAPTER), "org.bluez.GattManager1").RegisterApplication(
            BASE, {}, reply_handler=lambda: None, error_handler=failed("RegisterApplication"))
        self.adverts = dbus.Interface(bus.get_object("org.bluez", ADAPTER), "org.bluez.LEAdvertisingManager1")
        self.adverts.RegisterAdvertisement(BASE + "/adv", {}, reply_handler=lambda: None,
                                           error_handler=failed("RegisterAdvertisement"))
        self.kicks = 0
        bus.add_signal_receiver(self._device_changed, dbus_interface=PROPS, signal_name="PropertiesChanged",
                                bus_name="org.bluez", path_keyword="path")
        with self.run_lock:
            self.loop, self.glib = loop, GLib
            if not self.want:                # closed while this was still setting up
                GLib.idle_add(self._stop)
        if self.host_path and self._dev(self.host_path, "Connected") == "1":
            # Still connected from before (the app was killed): Android has given up on the service that
            # went away and looks again only on a new connection.
            self._say("host still connected from before; reconnecting it")
            self._hang_up(self.host_path)
        self._update()
        try:
            loop.run()
        finally:
            with self.run_lock:
                self.loop = None
            self.reports = {}
            for n in self.nodes:
                n.remove_from_connection()
            bus.close()                      # BlueZ drops the application, the advertisement and the agent with it
            if self.state not in ("error", "no bluetooth"):
                self.state = "idle"

    def _stop(self):
        """In the loop's thread: let go of everything held, hang up on the host, put pairing back."""
        for name, c in self.reports.items():
            self._emit(c, IDLE[name])
        self._asked(False)
        try:
            # The advertisement goes first. Android reconnects to a bonded keyboard within a second of
            # being hung up on; if it still finds us while the service is going away, it sees no HID
            # service, gives up, and then waits for someone to tap Connect in its settings.
            self.adverts.UnregisterAdvertisement(BASE + "/adv")
        except Exception as e:
            self._say(f"closing: {e}")
        try:
            if self.host_path:
                dev = self.dbus.Interface(self.bus.get_object("org.bluez", self.host_path), DEVICE)
                dev.Disconnect(reply_handler=lambda: None, error_handler=lambda e: None)
            self.adapter.Set(ADAPTER1, "PairableTimeout", self.dbus.UInt32(self.was_timeout))
            if not self.was_pairable:
                self.adapter.Set(ADAPTER1, "Pairable", self.dbus.Boolean(False))
        except Exception as e:
            self._say(f"closing: {e}")
        self.glib.timeout_add(150, self.loop.quit)   # after the idle reports and the disconnect have gone out
        return False

    # ---- who is let in (all in the loop's thread)
    def _dev(self, path, prop):
        try:
            return str(self.dbus.Interface(self.bus.get_object("org.bluez", path), PROPS).Get(DEVICE, prop))
        except Exception:
            return ""

    def _read(self, path, ok, no):
        """A read of the HID service: answered for the host, refused for anyone else. With no host on
        file, the first device to try is put to the user (ENTER / ESC on the card) and its reads wait
        for the answer: Android keeps an empty key map for a device whose first read was refused."""
        refuse = lambda: no(self.dbus.exceptions.DBusException("not this device", name="org.bluez.Error.NotAuthorized"))
        if path and path == self.host_path:
            return ok()
        if path and self.host and self._dev(path, "Address") == self.host["addr"]:
            self.host_path = path
            self._update()
            return ok()
        if path and not self.host and path not in self.refused and not self.pending:
            self._ask(path, None)
        if self.pending and self.pending[0] == path:
            self.waiting.append((ok, refuse))
        else:
            refuse()

    def _ask(self, path, code, ok=None, no=None):
        if self.pending or path in self.refused:
            if no:
                no(self.dbus.exceptions.DBusException("busy", name="org.bluez.Error.Rejected"))
            return
        self.pending = (path, ok, no, self.glib.timeout_add_seconds(CONFIRM_WAIT, self._asked, False, True))
        self.asking = {"name": self._dev(path, "Alias") or "a device", "addr": self._dev(path, "Address"), "code": code}
        self._say(f"asking about {self.asking['name']} ({self.asking['addr']})")
        self.state = "confirm"

    def confirm(self, yes):
        """The user's answer on the card (ENTER: True, ESC: False), from the app's thread."""
        if self.loop is not None:
            self.glib.idle_add(self._asked, bool(yes))

    def _asked(self, yes, timed_out=False):
        """yes: True / False from the user (or the wait running out), None when BlueZ cancelled."""
        if not self.pending:
            return False
        path, ok, no, timer = self.pending
        self.pending, self.asking = None, None
        waiting, self.waiting = self.waiting, []
        if not timed_out:
            self.glib.source_remove(timer)
        for answer in waiting:               # the reads held back while the question was open
            answer[0 if yes else 1]()
        if yes:
            self.host_path = path
            self.host = None                 # the device accepted replaces whatever was on file
            if ok:
                ok()                         # a pairing: its address is final once it has bonded (_device_changed)
            else:                            # already bonded: its address is known now
                self._keep(path)
        else:
            if not timed_out:                # only a "no" from the user lasts; an unanswered question comes back
                self.refused.add(path)
            self._say("refused" if not timed_out else "no answer")
            if no and yes is not None:
                no(self.dbus.exceptions.DBusException("refused", name="org.bluez.Error.Rejected"))
            self._hang_up(path)
        self._update()
        return False

    def _hang_up(self, path):
        try:
            self.dbus.Interface(self.bus.get_object("org.bluez", path), DEVICE).Disconnect(
                reply_handler=lambda: None, error_handler=lambda e: None)
        except Exception:
            pass

    def _keep(self, path):
        self.host = {"addr": self._dev(path, "Address"), "name": self._dev(path, "Alias")}
        try:
            os.makedirs(CONF, exist_ok=True)
            write_atomic(HOST, json.dumps(self.host))
        except OSError as e:
            self._say(f"could not save {HOST}: {e}")
        self._say(f"host is {self.host['name']} ({self.host['addr']})")

    def _device_changed(self, iface, changed, invalidated, path=None):
        if iface != DEVICE or path != self.host_path:
            return
        if (changed.get("Bonded") and not self.host) or (self.host and "Address" in changed):
            self._keep(path)                             # the pairing just accepted went through (or its address settled)
        if "Connected" in changed:
            self._update()
            if changed["Connected"] and self.kicks < 3:      # it should open the service within seconds
                self.glib.timeout_add_seconds(6, self._kick)

    def _kick(self):
        """The host is connected but has not opened the HID service (it gave up on an earlier try and
        only looks again on a new connection): hang up, so it connects afresh."""
        if self.loop is not None and self.host_path and self.state not in ("connected", "confirm") \
                and self._dev(self.host_path, "Connected") == "1":
            self.kicks += 1
            self._say(f"host connected without opening the service; reconnecting it ({self.kicks})")
            self._hang_up(self.host_path)
        return False

    def _update(self):
        if self.state in ("idle", "error", "no bluetooth") and self.loop is None:
            return
        if self.pending:
            self.state = "confirm"
        elif self.host_path and any(c.notifying for c in self.reports.values()) and self._dev(self.host_path, "Connected") == "1":
            self.state = "connected"
        else:
            self.state = "classic on" if self.classic else "waiting"

    # ---- reports (from any thread)
    def _emit(self, c, data):
        c.value = data
        if c.notifying:
            c.PropertiesChanged(CHRC, {"Value": self.dbus.Array(data, signature="y")}, [])
        return False

    def _send(self, name, data):
        if FAKE:
            self._say(f"{name} {data.hex()}")
            return True
        c, glib = self.reports.get(name), self.glib
        if c is None or self.state != "connected":
            return False
        glib.idle_add(self._emit, c, data)
        return True

    def key(self, name, down):
        """Press or release one named key (see HAT, BUTTON, CONSUMER, USAGE). False if it did not go out."""
        with self.lock:
            if name in HAT or name in BUTTON:
                if name in HAT:              # the newest direction held wins
                    self.held = [h for h in self.held if h != name] + ([name] if down else [])
                else:
                    self.buttons = self.buttons | 1 << BUTTON[name] if down else self.buttons & ~(1 << BUTTON[name])
                hat = HAT[self.held[-1]] if self.held else 8
                return self._send("gamepad", struct.pack("<HB4B", self.buttons, hat, 128, 128, 128, 128))
            if name in CONSUMER:
                self.cons = [c for c in self.cons if c != name] + ([name] if down else [])
                return self._send("consumer", struct.pack("<H", CONSUMER[self.cons[-1]] if self.cons else 0))
            if name in USAGE:
                return self._keyboard(USAGE[name], down)
        return False

    def evkey(self, code, down):
        """Keyboard mode: one of the Cardputer's own keys (an evdev code) as the same key on the host."""
        with self.lock:
            if code in EVMOD:
                self.mods = self.mods | EVMOD[code] if down else self.mods & ~EVMOD[code]
                return self._keyboard(None, down)
            return self._keyboard(EVKEY[code], down) if code in EVKEY else False

    def _keyboard(self, usage, down):
        if usage is not None:
            self.keys = [k for k in self.keys if k != usage] + ([usage] if down else [])
        return self._send("keyboard", bytes([self.mods, 0] + (self.keys[-6:] + [0] * 6)[:6]))

    def move(self, dx, dy, wheel=0):
        """Pointer movement in counts (and wheel clicks); long moves go out as several reports."""
        ok = True
        while dx or dy or wheel:
            step = [max(-127, min(127, v)) for v in (dx, dy, wheel)]
            dx, dy, wheel = dx - step[0], dy - step[1], wheel - step[2]
            ok = self._send("mouse", struct.pack("<B3b", self.mouse, *step)) and ok
        return ok

    def click(self, down, button=1):
        """Hold or let go of a mouse button (1 left, 2 right)."""
        with self.lock:
            self.mouse = self.mouse | button if down else self.mouse & ~button
            return self._send("mouse", struct.pack("<B3b", self.mouse, 0, 0, 0))

    def release(self):
        """Let go of everything held (leaving keyboard mode, losing the pointer)."""
        with self.lock:
            self.buttons, self.held, self.cons, self.mods, self.keys, self.mouse = 0, [], [], 0, [], 0
            for name in IDLE:
                self._send(name, IDLE[name])
