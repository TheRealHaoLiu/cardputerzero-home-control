"""Build a device file (devices/<id>.json) from Flipper-IRDB .ir files. Runs on a computer, not the device.

usage: python3 tools/flipper2device.py <spec.json> <out.json>

The spec names the device, its key layout, and one or more code sets. Each code set is a Flipper
file plus a map from our button ids to the button names used in that file. Raw entries are copied;
parsed entries (NEC, NECext, Samsung32) are turned into timings by remoteprotocols
(https://github.com/ianchi/remoteprotocols, `pip install remoteprotocols pyyaml`; set REMOTEPROTOCOLS
to its command if it is not on PATH).
"""
import json, os, subprocess, sys

ENCODER = os.environ.get("REMOTEPROTOCOLS", "remoteprotocols")


def le(hexbytes, n):
    """Flipper stores numbers as little-endian hex bytes: "00 BF 00 00" -> 0xBF00 (first n bytes)."""
    b = [int(x, 16) for x in hexbytes.split()][:n]
    return sum(v << (8 * i) for i, v in enumerate(b))


def encode(protocol, address, command):
    """Timings for a parsed entry, through remoteprotocols."""
    if protocol == "NEC":
        cmd = f"nec:{le(address, 1):#x}:{le(command, 1):#x}"
    elif protocol == "NECext":
        cmd = f"nec:{le(address, 2):#x}:{le(command, 2):#x}"
    elif protocol == "Samsung32":
        cmd = f"samsung:{le(address, 1) | le(address, 1) << 8:#x}:{le(command, 1):#x}"
    else:
        raise SystemExit(f"protocol {protocol} is not supported yet")
    out = subprocess.run([ENCODER, "encode", cmd], capture_output=True, text=True, check=True).stdout.strip()
    body, freq = out.split(":")[1:3] if out.startswith("duration:") else (None, None)
    if body is None:
        raise SystemExit(f"remoteprotocols gave no timings for {cmd}: {out}")
    raw = [abs(int(x)) for x in body.split(",")]
    return {"carrier": int(freq), "duty": 33, "raw": raw, "from": cmd}


def read_flipper(path):
    """Return {name: {"carrier", "duty", "raw"}} for the entries of a Flipper .ir file."""
    out, cur = {}, None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, val = (s.strip() for s in line.split(":", 1))
            if key == "name":
                cur = {"name": val}
            elif cur is None:
                continue
            elif key == "type":
                cur["type"] = val
            elif key == "frequency":
                cur["carrier"] = int(val)
            elif key == "duty_cycle":
                cur["duty"] = round(float(val) * 100)
            elif key in ("protocol", "address", "command"):
                cur[key] = val
                if key == "command":
                    out[cur["name"]] = encode(cur["protocol"], cur["address"], val)
                    cur = None
            elif key == "data":
                cur["raw"] = [int(x) for x in val.split()]
                raw = cur["raw"]
                if len(raw) % 2 == 0:  # a transmission must end on a pulse
                    raw.pop()
                out[cur["name"]] = {"carrier": cur.get("carrier", 38000), "duty": cur.get("duty", 33), "raw": raw}
                cur = None
    return out


def main():
    spec_path, out_path = sys.argv[1], sys.argv[2]
    spec = json.load(open(spec_path))
    base = os.path.dirname(os.path.abspath(spec_path))
    # A card with "modes" (e.g. Bluetooth and Wi-Fi) has its pages inside each mode; the code sets are shared.
    faces = list(spec["modes"].values()) if spec.get("modes") else [spec]
    pages = [p for f in faces for p in (f.get("pages") or [f["layout"]])]
    fixed = spec.get("fixed", {})   # shown on every page (e.g. home, back, arrows)
    buttons = {b["id"] for page in pages + [f.get("fixed", {}) for f in faces] for b in page.values()}
    sets = {}
    for name, cs in spec["code_sets"].items():
        entries = read_flipper(os.path.join(base, cs["file"]))
        # A map value "<other file>:<name>" borrows a button from another remote, e.g. the TV's volume
        # on the Apple TV card, for a sound bar that answers the TV's remote.
        for b, ref in list(cs["map"].items()):
            if ":" in ref:
                other, name = ref.rsplit(":", 1)
                got = read_flipper(os.path.join(base, os.path.dirname(cs["file"]), other))
                if name in got:
                    entries[ref] = got[name]
        missing = sorted(b for b in buttons if cs["map"].get(b) not in entries)
        if len(missing) == len(buttons):
            raise SystemExit(f"code set {name}: no signals at all")
        if missing:   # a remote family without that button: drawn greyed out on this code set
            print(f"code set {name}: no signal for {', '.join(missing)}")
        sets[name] = {"source": cs["source"],
                      "signals": {b: entries[cs["map"][b]] for b in sorted(buttons) if b not in missing}}
    device = {k: spec[k] for k in ("id", "name", "icon", "backend") if k in spec}
    if spec.get("modes"):
        device["modes"] = spec["modes"]
    else:
        device["pages"] = pages
    if fixed:
        device["fixed"] = fixed
    if spec.get("model"):
        device["model"] = spec["model"]
    if spec.get("keyboard"):
        device["keyboard"] = spec["keyboard"]
    if spec.get("hint"):
        device["hint"] = spec["hint"]
    if spec.get("todo"):
        device["todo"] = spec["todo"]
    device["code_sets"] = sets
    with open(out_path, "w") as f:
        json.dump(device, f, indent=1)
        f.write("\n")
    print(f"{out_path}: {len(buttons)} buttons, code sets {', '.join(sets)}")


if __name__ == "__main__":
    main()
