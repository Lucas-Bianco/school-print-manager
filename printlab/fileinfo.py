"""Read print time, filament, printer model and thumbnail from sliced files.

Supports:
- .bgcode  (Prusa binary G-code, PrusaSlicer's default for MK4 / CORE One)
- .gcode   (PrusaSlicer, Bambu Studio / OrcaSlicer, Cura text G-code)
- .3mf     (Bambu Studio "Export plate sliced file", i.e. .gcode.3mf)
"""
import base64
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


class FileError(ValueError):
    """Shown to the student, so keep messages plain."""


@dataclass
class FileInfo:
    kind: str                       # bgcode | gcode | 3mf
    est_seconds: int
    printer_model: str = ""
    filament_g: float | None = None
    thumbnail_png: bytes | None = None
    plate: int | None = None


_DUR = re.compile(r"(\d+)\s*([dhms])")


def parse_duration(text: str) -> int | None:
    parts = _DUR.findall(text)
    if not parts:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def _first_float(text: str) -> float | None:
    m = re.search(r"[-+]?\d*\.?\d+", text)
    return float(m.group()) if m else None


def _from_keyvalues(meta: dict) -> tuple[int | None, float | None, str]:
    est = None
    for key in ("estimated printing time (normal mode)", "estimated printing time"):
        if key in meta:
            est = parse_duration(meta[key])
            if est:
                break
    grams = None
    for key in ("filament used [g]", "total filament used [g]"):
        if key in meta:
            # multi-material files list one value per extruder: "1.2, 3.4"
            values = [float(v) for v in re.findall(r"\d*\.?\d+", meta[key])]
            if values:
                grams = round(sum(values), 1)
                break
    model = meta.get("printer_model", "").strip()
    return est, grams, model


# ---------------------------------------------------------------- text G-code
_KV = re.compile(r"^;\s*([^=\n]+?)\s*=\s*(.*?)\s*$", re.M)
_THUMB = re.compile(r"; thumbnail begin (\d+)x(\d+) \d+\s*\n(.*?); thumbnail end", re.S)


def parse_gcode_text(text: str) -> FileInfo:
    meta = {k.strip(): v for k, v in _KV.findall(text)}
    est, grams, model = _from_keyvalues(meta)

    if est is None:  # Bambu Studio / OrcaSlicer text G-code
        m = re.search(r";\s*total estimated time:\s*([^;\n]+)", text)
        if m:
            est = parse_duration(m.group(1))
    if est is None:  # Cura
        m = re.search(r"^;TIME:(\d+)", text, re.M)
        if m:
            est = int(m.group(1))
    if grams is None:
        m = re.search(r";\s*total filament weight \[g\]\s*:\s*([\d.]+)", text)
        if m:
            grams = float(m.group(1))

    thumb = None
    best = 0
    for w, h, body in _THUMB.findall(text):
        size = int(w) * int(h)
        if size > best:
            b64 = "".join(line.lstrip("; ").strip() for line in body.splitlines())
            try:
                thumb = base64.b64decode(b64)
                best = size
            except ValueError:
                pass

    if not est:
        raise FileError("No print-time estimate found in this file. Re-slice it and upload the new file.")
    return FileInfo("gcode", est, model, grams, thumb)


# -------------------------------------------------------------- binary G-code
def parse_bgcode(data: bytes) -> FileInfo:
    if data[:4] != b"GCDE":
        raise FileError("This .bgcode file is damaged. Re-slice it and upload again.")
    _version, checksum_type = struct.unpack_from("<IH", data, 4)
    pos = 10
    meta: dict[str, str] = {}
    thumbs: list[tuple[int, bytes]] = []
    while pos + 8 <= len(data):
        btype, comp, usize = struct.unpack_from("<HHI", data, pos)
        pos += 8
        csize = usize
        if comp != 0:
            (csize,) = struct.unpack_from("<I", data, pos)
            pos += 4
        param_size = 6 if btype == 5 else 2
        params = data[pos:pos + param_size]
        pos += param_size
        payload = data[pos:pos + csize]
        pos += csize
        if checksum_type == 1:
            pos += 4
        if btype == 1:  # first G-code block: all metadata comes before it
            break
        if comp == 0:
            raw = payload
        elif comp == 1:
            raw = zlib.decompress(payload)
        else:  # heatshrink: not used for metadata by PrusaSlicer's defaults
            continue
        if btype in (0, 2, 3, 4):
            for line in raw.decode("utf-8", "ignore").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    meta.setdefault(k.strip(), v.strip())
        elif btype == 5:
            fmt, w, h = struct.unpack("<HHH", params)
            if fmt == 0:  # PNG
                thumbs.append((w * h, raw))

    est, grams, model = _from_keyvalues(meta)
    if not est:
        raise FileError("No print-time estimate found in this file. Re-slice it and upload the new file.")
    thumb = max(thumbs)[1] if thumbs else None
    return FileInfo("bgcode", est, model, grams, thumb)


# ------------------------------------------------------------- Bambu .3mf
def parse_3mf(path: Path) -> FileInfo:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise FileError("This .3mf file is damaged.")
    with zf:
        names = set(zf.namelist())
        plates = sorted(
            int(m.group(1)) for n in names
            if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n))
        )
        if not plates:
            raise FileError(
                "This .3mf isn't sliced. In Bambu Studio, slice the plate and use "
                "File > Export > Export plate sliced file, then upload that .gcode.3mf."
            )
        plate = plates[0]
        est = grams = None
        model = ""
        if "Metadata/slice_info.config" in names:
            root = ElementTree.fromstring(zf.read("Metadata/slice_info.config"))
            for p in root.iter("plate"):
                md = {m.get("key"): m.get("value") for m in p.iter("metadata")}
                if md.get("index", "1") == str(plate) or est is None:
                    if md.get("prediction"):
                        est = int(float(md["prediction"]))
                    if md.get("weight"):
                        grams = round(float(md["weight"]), 1)
                    model = md.get("printer_model_id", model) or model
        gcode_name = f"Metadata/plate_{plate}.gcode"
        if not est:
            with zf.open(gcode_name) as fh:
                head = fh.read(300_000).decode("utf-8", "ignore")
            est = parse_gcode_text(head).est_seconds
        thumb = None
        for cand in (f"Metadata/plate_{plate}.png", f"Metadata/plate_{plate}_small.png"):
            if cand in names:
                thumb = zf.read(cand)
                break
    return FileInfo("3mf", est, model, grams, thumb, plate)


# ------------------------------------------------------------------- entry
def kind_for(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".bgcode"):
        return "bgcode"
    if name.endswith(".gcode"):
        return "gcode"
    if name.endswith(".3mf"):
        return "3mf"
    raise FileError("Upload a sliced file: .bgcode or .gcode (Prusa) or .gcode.3mf (Bambu).")


def read_info(path: Path, filename: str) -> FileInfo:
    kind = kind_for(filename)
    if kind == "3mf":
        return parse_3mf(path)
    with open(path, "rb") as fh:
        if kind == "bgcode":
            return parse_bgcode(fh.read(8 * 1024 * 1024))
        # Text G-code: metadata sits at the start (thumbnails) and end (config block).
        head = fh.read(512 * 1024)
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 512 * 1024))
        tail = fh.read()
    return parse_gcode_text(head.decode("utf-8", "ignore") + "\n" + tail.decode("utf-8", "ignore"))
