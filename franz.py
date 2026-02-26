import subprocess
import sys

# Always run the region selector script in a separate process to update config.json
selector_process = subprocess.run(['python', 'region_selector.py'])
if selector_process.returncode != 0:
    print("Region selector failed, exiting.")
    sys.exit(1)

import asyncio
import base64
import ctypes
import ctypes.wintypes as W
import http.client
import json
import logging
import struct
import time
import urllib.parse
import webbrowser
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pipeline

HERE: Final[Path] = Path(__file__).resolve().parent
CONFIG_PATH: Final[Path] = HERE / "config.json"
PANEL_HTML: Final[Path] = HERE / "panel.html"
CONFIG_HTML: Final[Path] = HERE / "config.html"
PIPELINE_PY: Final[Path] = HERE / "pipeline.py"
NORM: Final[int] = 1000
SRCCOPY: Final[int] = 0x00CC0020
CAPTUREBLT: Final[int] = 0x40000000
HALFTONE: Final[int] = 4
LDN: Final[int] = 0x0002
LUP: Final[int] = 0x0004
RDN: Final[int] = 0x0008
RUP: Final[int] = 0x0010
WHEEL: Final[int] = 0x0800
WHEEL_DELTA: Final[int] = 120
KEYEVENTF_KEYUP: Final[int] = 0x0002
KEYEVENTF_EXTENDEDKEY: Final[int] = 0x0001
EXTENDED_VKS: Final[frozenset[int]] = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})

log: Final[logging.Logger] = logging.getLogger("franz")

_CFG: dict[str, Any] = json.loads(CONFIG_PATH.read_text("utf-8"))


def cfg(name: str, default: Any = None) -> Any:
    return _CFG.get(name, default)


def clamp(v: int, lo: int = 0, hi: int = NORM) -> int:
    return max(lo, min(hi, v))


VK_MAP: Final[dict[str, int]] = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "space": 0x20,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
    "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
    "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
    "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
    "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}


def setup_logging(run_dir: Path) -> None:
    level: int = getattr(logging, str(cfg("log_level", "INFO")).upper(), logging.INFO)
    fmt: logging.Formatter = logging.Formatter(
        "[%(name)s][%(asctime)s.%(msecs)03d][%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )
    root: logging.Logger = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    sh: logging.StreamHandler[Any] = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg("log_to_file", True):
        fh: logging.FileHandler = logging.FileHandler(run_dir / "main.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def make_run_dir() -> Path:
    base: Path = HERE / str(cfg("runs_dir", "runs"))
    base.mkdir(exist_ok=True)
    n: int = sum(1 for d in base.iterdir() if d.is_dir() and d.name.startswith("run_"))
    rd: Path = base / f"run_{n + 1:04d}"
    rd.mkdir(parents=True, exist_ok=True)
    return rd


@dataclass
class Ghost:
    bbox_2d: list[int]
    turn: int
    image_b64: str
    label: str = ""


@dataclass
class State:
    phase: str = "init"
    error: str | None = None
    turn: int = 0
    run_dir: Path | None = None
    annotated_b64: str = ""
    raw_b64: str = ""
    raw_seq: int = 0
    vlm_json: str = ""
    observation: str = ""
    ghosts_data: list[dict[str, Any]] = field(default_factory=list)
    actions_data: list[dict[str, Any]] = field(default_factory=list)
    heat_data: list[dict[str, Any]] = field(default_factory=list)
    raw_display: dict[str, Any] = field(default_factory=dict)
    ghosts_overlay: list[dict[str, Any]] = field(default_factory=list)
    msg_id: int = 0
    pending_seq: int = 0
    annotated_seq: int = -1
    annotated_event: asyncio.Event = field(default_factory=asyncio.Event)
    next_vlm: str | None = None
    next_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


S: State
STOP: asyncio.Event
GHOST_RING: deque[Ghost] = deque()


def set_phase(p: str, err: str | None = None) -> None:
    S.phase, S.error = p, err
    log.info("phase=%s err=%s", p, err)


ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)

_u32: ctypes.WinDLL = ctypes.WinDLL("user32", use_last_error=True)
_g32: ctypes.WinDLL = ctypes.WinDLL("gdi32", use_last_error=True)


def _s(dll: ctypes.WinDLL, nm: str, at: list[Any], rt: Any) -> None:
    f: Any = getattr(dll, nm)
    f.argtypes = at
    f.restype = rt


_s(_u32, "GetDC", [W.HWND], W.HDC)
_s(_u32, "ReleaseDC", [W.HWND, W.HDC], ctypes.c_int)
_s(_u32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int)
_s(_g32, "CreateCompatibleDC", [W.HDC], W.HDC)
_s(_g32, "CreateDIBSection", [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP)
_s(_g32, "SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
_s(_g32, "BitBlt", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL)
_s(_g32, "StretchBlt", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL)
_s(_g32, "SetStretchBltMode", [W.HDC, ctypes.c_int], ctypes.c_int)
_s(_g32, "SetBrushOrgEx", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], W.BOOL)
_s(_g32, "DeleteObject", [W.HGDIOBJ], W.BOOL)
_s(_g32, "DeleteDC", [W.HDC], W.BOOL)
_s(_u32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], W.BOOL)
_s(_u32, "mouse_event", [W.DWORD, W.DWORD, W.DWORD, ctypes.c_long, ctypes.c_ulong], None)
_s(_u32, "keybd_event", [W.BYTE, W.BYTE, W.DWORD, ctypes.POINTER(ctypes.c_ulong)], None)


class _BIH(ctypes.Structure):
    _fields_ = [
        ("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
        ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
        ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG),
        ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BIH), ("bmiColors", W.DWORD * 3)]


def _bmi(w: int, h: int) -> _BMI:
    b: _BMI = _BMI()
    hd: _BIH = b.bmiHeader
    hd.biSize = ctypes.sizeof(_BIH)
    hd.biWidth = w
    hd.biHeight = -h
    hd.biPlanes = 1
    hd.biBitCount = 32
    hd.biCompression = 0
    return b


def _screen() -> tuple[int, int]:
    return int(_u32.GetSystemMetrics(0)), int(_u32.GetSystemMetrics(1))


def _crop_px(bw: int, bh: int) -> tuple[int, int, int, int]:
    c: dict[str, Any] = cfg("capture_crop", {"x1": 0, "y1": 0, "x2": NORM, "y2": NORM})
    x1: int = clamp(int(c.get("x1", 0)))
    y1: int = clamp(int(c.get("y1", 0)))
    x2: int = clamp(int(c.get("x2", NORM)))
    y2: int = clamp(int(c.get("y2", NORM)))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (
        clamp((x1 * bw + NORM // 2) // NORM, 0, bw),
        clamp((y1 * bh + NORM // 2) // NORM, 0, bh),
        clamp((x2 * bw + NORM // 2) // NORM, 0, bw),
        clamp((y2 * bh + NORM // 2) // NORM, 0, bh),
    )


def _n2s(nx: int, ny: int) -> tuple[int, int]:
    sw, sh = _screen()
    x1, y1, x2, y2 = _crop_px(sw, sh)
    cw: int = max(1, x2 - x1)
    ch: int = max(1, y2 - y1)
    px: int = x1 + (clamp(nx) * (cw - 1) + NORM // 2) // NORM if cw > 1 else x1
    py: int = y1 + (clamp(ny) * (ch - 1) + NORM // 2) // NORM if ch > 1 else y1
    return px, py


def _dib(dc: Any, w: int, h: int) -> tuple[Any, int]:
    bits: ctypes.c_void_p = ctypes.c_void_p()
    hbmp: Any = _g32.CreateDIBSection(dc, ctypes.byref(_bmi(w, h)), 0, ctypes.byref(bits), None, 0)
    return (hbmp, int(bits.value)) if hbmp and bits.value else (None, 0)


def _capture_full() -> tuple[bytes, int, int] | None:
    sw, sh = _screen()
    sdc: Any = _u32.GetDC(0)
    if not sdc:
        return None
    mdc: Any = _g32.CreateCompatibleDC(sdc)
    if not mdc:
        _u32.ReleaseDC(0, sdc)
        return None
    hb, bits = _dib(sdc, sw, sh)
    if not hb:
        _g32.DeleteDC(mdc)
        _u32.ReleaseDC(0, sdc)
        return None
    old: Any = _g32.SelectObject(mdc, hb)
    _g32.BitBlt(mdc, 0, 0, sw, sh, sdc, 0, 0, SRCCOPY | CAPTUREBLT)
    raw: bytes = bytes((ctypes.c_ubyte * (sw * sh * 4)).from_address(bits))
    _g32.SelectObject(mdc, old)
    _g32.DeleteObject(hb)
    _g32.DeleteDC(mdc)
    _u32.ReleaseDC(0, sdc)
    return raw, sw, sh


def _crop_bgra(bgra: bytes, sw: int, sh: int, x1: int, y1: int, x2: int, y2: int) -> tuple[bytes, int, int]:
    cw: int = x2 - x1
    ch: int = y2 - y1
    if cw <= 0 or ch <= 0:
        return bgra, sw, sh
    src: memoryview = memoryview(bgra)
    out: bytearray = bytearray(cw * ch * 4)
    ss: int = sw * 4
    ds: int = cw * 4
    for y in range(ch):
        so: int = (y1 + y) * ss + x1 * 4
        do: int = y * ds
        out[do:do + ds] = src[so:so + ds]
    return bytes(out), cw, ch


def _stretch(bgra: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
    sdc: Any = _u32.GetDC(0)
    if not sdc:
        return None
    sdc2: Any = _g32.CreateCompatibleDC(sdc)
    ddc: Any = _g32.CreateCompatibleDC(sdc)
    if not sdc2 or not ddc:
        if sdc2:
            _g32.DeleteDC(sdc2)
        if ddc:
            _g32.DeleteDC(ddc)
        _u32.ReleaseDC(0, sdc)
        return None
    sb, sbi = _dib(sdc, sw, sh)
    if not sb:
        _g32.DeleteDC(sdc2)
        _g32.DeleteDC(ddc)
        _u32.ReleaseDC(0, sdc)
        return None
    ctypes.memmove(sbi, bgra, sw * sh * 4)
    os: Any = _g32.SelectObject(sdc2, sb)
    db, dbi = _dib(sdc, dw, dh)
    if not db:
        _g32.SelectObject(sdc2, os)
        _g32.DeleteObject(sb)
        _g32.DeleteDC(sdc2)
        _g32.DeleteDC(ddc)
        _u32.ReleaseDC(0, sdc)
        return None
    od: Any = _g32.SelectObject(ddc, db)
    _g32.SetStretchBltMode(ddc, HALFTONE)
    _g32.SetBrushOrgEx(ddc, 0, 0, None)
    _g32.StretchBlt(ddc, 0, 0, dw, dh, sdc2, 0, 0, sw, sh, SRCCOPY)
    result: bytes = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dbi))
    _g32.SelectObject(ddc, od)
    _g32.SelectObject(sdc2, os)
    _g32.DeleteObject(db)
    _g32.DeleteObject(sb)
    _g32.DeleteDC(ddc)
    _g32.DeleteDC(sdc2)
    _u32.ReleaseDC(0, sdc)
    return result


def _to_png(bgra: bytes, w: int, h: int) -> bytes:
    stride: int = w * 4
    src: memoryview = memoryview(bgra)
    rows: bytearray = bytearray()
    for y in range(h):
        rows.append(0)
        row: memoryview = src[y * stride:(y + 1) * stride]
        for i in range(0, len(row), 4):
            rows.extend((row[i + 2], row[i + 1], row[i], 255))

    def ck(t: bytes, b: bytes) -> bytes:
        c: bytes = t + b
        return struct.pack(">I", len(b)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + ck(b"IDAT", zlib.compress(bytes(rows), 6))
        + ck(b"IEND", b"")
    )


def _bbox_crop_b64(bgra: bytes, img_w: int, img_h: int, bbox: list[int]) -> str:
    x1: int = clamp(bbox[0] * img_w // NORM, 0, img_w)
    y1: int = clamp(bbox[1] * img_h // NORM, 0, img_h)
    x2: int = clamp(bbox[2] * img_w // NORM, 0, img_w)
    y2: int = clamp(bbox[3] * img_h // NORM, 0, img_h)
    cw: int = x2 - x1
    ch: int = y2 - y1
    if cw <= 0 or ch <= 0:
        return ""
    cropped, cw2, ch2 = _crop_bgra(bgra, img_w, img_h, x1, y1, x2, y2)
    return base64.b64encode(_to_png(cropped, cw2, ch2)).decode("ascii")


def capture() -> tuple[str, int, int, bytes]:
    d: float = float(cfg("capture_delay", 0.0))
    if d > 0:
        time.sleep(d)
    cap: tuple[bytes, int, int] | None = _capture_full()
    if not cap:
        return "", 0, 0, b""
    bgra, w, h = cap
    cr: Any = cfg("capture_crop")
    if isinstance(cr, dict) and all(k in cr for k in ("x1", "y1", "x2", "y2")):
        bgra, w, h = _crop_bgra(bgra, w, h, *_crop_px(w, h))
    ow: int = int(cfg("capture_width", 0))
    oh: int = int(cfg("capture_height", 0))
    dw: int = 0
    dh: int = 0
    if ow > 0 and oh > 0:
        dw, dh = ow, oh
    else:
        p: int = int(cfg("capture_scale_percent", 100))
        if 0 < p != 100:
            dw, dh = max(1, (w * p + 50) // 100), max(1, (h * p + 50) // 100)
    if dw > 0 and dh > 0 and (w, h) != (dw, dh):
        s: bytes | None = _stretch(bgra, w, h, dw, dh)
        if s:
            bgra, w, h = s, dw, dh
    b64: str = base64.b64encode(_to_png(bgra, w, h)).decode("ascii")
    log.info("capture %dx%d b64=%d", w, h, len(b64))
    return b64, w, h, bgra


def _build_ghosts(ghost_regions: list[dict[str, Any]], raw_bgra: bytes, img_w: int, img_h: int, turn: int) -> None:
    max_ghosts: int = int(cfg("ghost_max", 12))
    for g in ghost_regions:
        bbox: list[int] = g["bbox_2d"]
        crop_b64: str = _bbox_crop_b64(raw_bgra, img_w, img_h, bbox)
        if not crop_b64:
            continue
        GHOST_RING.append(Ghost(
            bbox_2d=list(bbox), turn=turn, image_b64=crop_b64, label=g.get("label", ""),
        ))
    while len(GHOST_RING) > max_ghosts:
        GHOST_RING.popleft()


def _ghosts_for_overlay(current_turn: int) -> list[dict[str, Any]]:
    max_age: int = int(cfg("ghost_max_age", 6))
    out: list[dict[str, Any]] = []
    for g in GHOST_RING:
        age: int = current_turn - g.turn
        if age > max_age:
            continue
        out.append({
            "bbox_2d": g.bbox_2d, "turn": g.turn, "age": age,
            "image_b64": g.image_b64, "label": g.label,
        })
    return out


def _ghosts_summary(ghosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"bbox_2d": g["bbox_2d"], "turn": g["turn"], "age": g["age"], "label": g["label"]}
        for g in ghosts
    ]


def _bbox_center(bbox: list[int]) -> tuple[int, int]:
    return (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2


def _jl(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")


def _save_artifact(rd: Path, turn: int, suffix: str, b64: str, extra: dict[str, Any]) -> None:
    nm: str = f"turn_{turn:04d}_{suffix}.png"
    if b64:
        (rd / nm).write_bytes(base64.b64decode(b64))
    _jl(rd / "turns.jsonl", {"turn": turn, "stage": suffix, **extra, f"{suffix}_png": nm})


def _mto(x: int, y: int) -> None:
    _u32.SetCursorPos(x, y)


def _mev(f: int, data: int = 0) -> None:
    _u32.mouse_event(f, 0, 0, data, 0)


def _kev(vk: int, up: bool = False) -> None:
    flags: int = 0
    if up:
        flags |= KEYEVENTF_KEYUP
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    _u32.keybd_event(vk, 0, flags, None)


def _type_text(text: str) -> None:
    for ch in text:
        vk_scan: int = ctypes.windll.user32.VkKeyScanW(ord(ch))
        if vk_scan == -1:
            continue
        vk: int = vk_scan & 0xFF
        shift: bool = bool(vk_scan & 0x100)
        ctrl_mod: bool = bool(vk_scan & 0x200)
        alt_mod: bool = bool(vk_scan & 0x400)
        if ctrl_mod:
            _kev(0x11)
        if alt_mod:
            _kev(0x12)
        if shift:
            _kev(0x10)
        _kev(vk)
        time.sleep(0.01)
        _kev(vk, True)
        if shift:
            _kev(0x10, True)
        if alt_mod:
            _kev(0x12, True)
        if ctrl_mod:
            _kev(0x11, True)
        time.sleep(0.02)


def _press_hotkey(keys_str: str) -> None:
    keys: list[str] = [k.strip().lower() for k in keys_str.split()]
    vks: list[int] = []
    for k in keys:
        vk: int | None = VK_MAP.get(k)
        if vk is not None:
            vks.append(vk)
        elif len(k) == 1:
            vk_scan: int = ctypes.windll.user32.VkKeyScanW(ord(k))
            if vk_scan != -1:
                vks.append(vk_scan & 0xFF)
    for vk_code in vks:
        _kev(vk_code)
        time.sleep(0.02)
    for vk_code in reversed(vks):
        _kev(vk_code, True)
        time.sleep(0.02)


def execute(actions: list[dict[str, Any]]) -> None:
    if not cfg("physical_execution", True):
        log.info("exec skip %d", len(actions))
        return
    ad: float = float(cfg("action_delay_seconds", 0.05))
    ds: int = max(1, int(cfg("drag_duration_steps", 20)))
    dd: float = float(cfg("drag_step_delay", 0.01))
    drag_start: tuple[int, int] | None = None

    for a in actions:
        atype: str = a.get("type", "")
        cx, cy = _bbox_center(a["bbox_2d"])
        sx, sy = _n2s(cx, cy)

        match atype:
            case "click":
                log.info("exec click (%d,%d)", sx, sy)
                _mto(sx, sy)
                time.sleep(0.03)
                _mev(LDN)
                time.sleep(0.03)
                _mev(LUP)
            case "double_click":
                log.info("exec double_click (%d,%d)", sx, sy)
                _mto(sx, sy)
                time.sleep(0.03)
                _mev(LDN)
                time.sleep(0.03)
                _mev(LUP)
                time.sleep(0.05)
                _mev(LDN)
                time.sleep(0.03)
                _mev(LUP)
            case "right_click":
                log.info("exec right_click (%d,%d)", sx, sy)
                _mto(sx, sy)
                time.sleep(0.03)
                _mev(RDN)
                time.sleep(0.03)
                _mev(RUP)
            case "drag_start":
                drag_start = (sx, sy)
                log.info("exec drag_start (%d,%d)", sx, sy)
            case "drag_end":
                if drag_start is None:
                    drag_start = (sx, sy)
                ex, ey = sx, sy
                bx, by = drag_start
                log.info("exec drag (%d,%d)->(%d,%d)", bx, by, ex, ey)
                _mto(bx, by)
                time.sleep(0.03)
                _mev(LDN)
                time.sleep(0.03)
                for i in range(1, ds + 1):
                    _mto(bx + (ex - bx) * i // ds, by + (ey - by) * i // ds)
                    time.sleep(dd)
                time.sleep(0.03)
                _mev(LUP)
                drag_start = None
            case "scroll_up":
                clicks: int = 3
                params: str = a.get("params", "")
                if params.strip().isdigit():
                    clicks = int(params.strip())
                log.info("exec scroll_up %d at (%d,%d)", clicks, sx, sy)
                _mto(sx, sy)
                time.sleep(0.03)
                for _ in range(clicks):
                    _mev(WHEEL, WHEEL_DELTA)
                    time.sleep(0.03)
            case "scroll_down":
                clicks = 3
                params = a.get("params", "")
                if params.strip().isdigit():
                    clicks = int(params.strip())
                log.info("exec scroll_down %d at (%d,%d)", clicks, sx, sy)
                _mto(sx, sy)
                time.sleep(0.03)
                for _ in range(clicks):
                    _mev(WHEEL, -WHEEL_DELTA)
                    time.sleep(0.03)
            case "type":
                text: str = a.get("params", "")
                log.info("exec type '%s'", text)
                _type_text(text)
            case "hotkey":
                keys_str_val: str = a.get("params", "")
                log.info("exec hotkey '%s'", keys_str_val)
                _press_hotkey(keys_str_val)
            case "key":
                key_name: str = a.get("params", "").strip().lower()
                vk_val: int | None = VK_MAP.get(key_name)
                if vk_val is not None:
                    log.info("exec key '%s' vk=0x%02X", key_name, vk_val)
                    _kev(vk_val)
                    time.sleep(0.03)
                    _kev(vk_val, True)
            case _:
                log.warning("exec unknown action type: '%s'", atype)
        time.sleep(ad)


def call_vlm(obs: str, ann_b64: str) -> tuple[str, dict[str, Any], str | None]:
    url: str = str(cfg("api_url", ""))
    u: urllib.parse.ParseResult = urllib.parse.urlparse(url)
    host: str = u.hostname or "127.0.0.1"
    port: int = u.port or 80
    path: str = u.path or "/v1/chat/completions"
    body: bytes = json.dumps({
        "model": str(cfg("model", "")),
        "temperature": float(cfg("temperature", 0.7)),
        "top_p": float(cfg("top_p", 0.9)),
        "max_tokens": int(cfg("max_tokens", 1000)),
        "messages": [
            {"role": "system", "content": str(cfg("system_prompt", ""))},
            {"role": "user", "content": [
                {"type": "text", "text": obs or "(no prior observation)"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ann_b64}"}},
            ]},
        ],
    }).encode("utf-8")
    log.info("vlm POST %s:%d%s obs=%d ann=%d", host, port, path, len(obs), len(ann_b64))
    try:
        conn: http.client.HTTPConnection = http.client.HTTPConnection(host, port)
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
        })
        resp: http.client.HTTPResponse = conn.getresponse()
        data: bytes = resp.read()
        conn.close()
        if not 200 <= resp.status < 300:
            return "", {}, f"HTTP {resp.status}"
        obj: Any = json.loads(data.decode("utf-8", "replace"))
        return obj["choices"][0]["message"]["content"], obj.get("usage") or {}, None
    except Exception as e:
        log.error("vlm: %s", e)
        return "", {}, str(e)


async def engine_loop(rd: Path) -> None:
    S.run_dir = rd
    bt: bool = bool(cfg("boot_enabled", True))
    bv: str = str(cfg("boot_vlm_output", ""))
    if bt and bv.strip():
        async with S.lock:
            S.next_vlm = bv
            S.next_event.set()
        set_phase("boot")
    else:
        set_phase("waiting_inject")

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    raw_bgra_buf: bytes = b""
    raw_w: int = 0
    raw_h: int = 0

    while not STOP.is_set():
        try:
            await asyncio.wait_for(S.next_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

        async with S.lock:
            vlm_raw: str = S.next_vlm or ""
            S.next_vlm = None
            S.next_event.clear()

        if not vlm_raw.strip():
            continue

        async with S.lock:
            S.turn += 1
            turn: int = S.turn
        log.info("=== TURN %d ===", turn)
        set_phase("running")

        result: pipeline.PipelineResult = pipeline.process(vlm_raw)
        log.info("pipeline ghosts=%d actions=%d heat=%d next=%d",
                 len(result.ghosts), len(result.actions), len(result.heat), len(result.next_turn))

        if raw_bgra_buf and result.ghosts:
            _build_ghosts(result.ghosts, raw_bgra_buf, raw_w, raw_h, turn)

        async with S.lock:
            S.vlm_json = vlm_raw
            S.observation = result.next_turn
            S.ghosts_data = result.ghosts
            S.actions_data = result.actions
            S.heat_data = result.heat
            S.raw_display = result.raw_display
            S.msg_id += 1
            S.ghosts_overlay = _ghosts_for_overlay(turn)

        set_phase("executing")
        await loop.run_in_executor(None, execute, result.actions)

        set_phase("capturing")
        raw_b64, w, h, raw_bgra = await loop.run_in_executor(None, capture)
        if not raw_b64:
            log.error("capture failed")
            set_phase("error", "capture failed")
            safe: str = json.dumps({"observation": "Capture failed. Retrying.", "regions": [], "actions": []})
            async with S.lock:
                S.next_vlm = safe
                S.next_event.set()
            continue

        raw_bgra_buf, raw_w, raw_h = raw_bgra, w, h
        async with S.lock:
            S.raw_b64 = raw_b64
            S.raw_seq += 1
            ghosts_snapshot: list[dict[str, Any]] = list(S.ghosts_overlay)
            actions_snapshot: list[dict[str, Any]] = list(S.actions_data)

        await loop.run_in_executor(
            None, _save_artifact, rd, turn, "raw", raw_b64,
            {"observation": result.next_turn, "ghosts": result.ghosts,
             "actions": actions_snapshot, "ghosts_visible": _ghosts_summary(ghosts_snapshot)},
        )

        async with S.lock:
            S.pending_seq = turn
            S.annotated_seq = -1
            S.annotated_b64 = ""
            S.annotated_event.clear()

        set_phase("waiting_annotated")
        await S.annotated_event.wait()

        async with S.lock:
            ann_b64: str = S.annotated_b64

        await loop.run_in_executor(
            None, _save_artifact, rd, turn, "ann", ann_b64,
            {"ghosts_rendered": _ghosts_summary(ghosts_snapshot),
             "actions_rendered": actions_snapshot,
             "ghost_count": len(ghosts_snapshot)},
        )

        set_phase("calling_vlm")
        txt, usage, err = await loop.run_in_executor(None, call_vlm, result.next_turn, ann_b64)

        if err:
            log.error("vlm err t=%d: %s", turn, err)
            set_phase("vlm_error", err)
            safe = json.dumps({"observation": f"VLM error: {err}. Retrying.", "regions": [], "actions": []})
            async with S.lock:
                S.next_vlm = safe
                S.next_event.set()
            continue

        log.info("vlm ok t=%d len=%d", turn, len(txt))
        async with S.lock:
            S.next_vlm = txt
            S.next_event.set()
        set_phase("running")


class Server:
    def __init__(self, host: str, port: int) -> None:
        self._h: str = host
        self._p: int = port
        self._srv: asyncio.Server | None = None

    async def start(self) -> None:
        self._srv = await asyncio.start_server(self._conn, self._h, self._p)
        log.info("http://%s:%d", self._h, self._p)

    async def stop(self) -> None:
        if self._srv:
            self._srv.close()
            await self._srv.wait_closed()

    async def _conn(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await self._proc(r, w)
        except Exception:
            pass
        finally:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass

    async def _proc(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        rl: bytes = await r.readline()
        if not rl:
            return
        parts: list[str] = rl.decode("utf-8", "replace").strip().split(" ")
        if len(parts) < 2:
            return
        method: str = parts[0]
        path: str = parts[1].split("?", 1)[0]
        hd: dict[str, str] = {}
        while True:
            hl: bytes = await r.readline()
            if not hl or hl in (b"\r\n", b"\n"):
                break
            d: str = hl.decode("utf-8", "replace").strip()
            if ":" in d:
                k, v = d.split(":", 1)
                hd[k.strip().lower()] = v.strip()
        body: bytes = b""
        cl: int = int(hd.get("content-length", "0"))
        if cl > 0:
            body = await r.readexactly(cl)
        match method:
            case "GET":
                await self._get(path, w)
            case "POST":
                await self._post(path, body, w)
            case "OPTIONS":
                await self._json(w, {})
            case _:
                await self._err(w, 405)

    async def _get(self, path: str, w: asyncio.StreamWriter) -> None:
        match path:
            case "/" | "/index.html":
                await self._raw(w, 200, "text/html; charset=utf-8", PANEL_HTML.read_bytes())
            case "/config.html":
                await self._raw(w, 200, "text/html; charset=utf-8", CONFIG_HTML.read_bytes())
            case "/config":
                await self._json(w, {
                    "ui": cfg("ui", {}),
                    "capture_width": int(cfg("capture_width", 512)),
                    "capture_height": int(cfg("capture_height", 288)),
                })
            case "/config_full":
                await self._json(w, _CFG)
            case "/pipeline_source":
                await self._json(w, {"source": PIPELINE_PY.read_text("utf-8")})
            case "/state":
                async with S.lock:
                    await self._json(w, {
                        "phase": S.phase, "error": S.error, "turn": S.turn, "msg_id": S.msg_id,
                        "pending_seq": S.pending_seq, "annotated_seq": S.annotated_seq, "raw_seq": S.raw_seq,
                        "actions": S.actions_data, "heat": S.heat_data,
                        "observation": S.observation,
                        "raw_display": S.raw_display,
                        "ghost_count": len(S.ghosts_overlay),
                    })
            case "/frame":
                async with S.lock:
                    await self._json(w, {"seq": S.raw_seq, "raw_b64": S.raw_b64})
            case "/ghosts":
                async with S.lock:
                    await self._json(w, {"turn": S.turn, "ghosts": S.ghosts_overlay})
            case _:
                await self._err(w, 404)

    async def _post(self, path: str, body: bytes, w: asyncio.StreamWriter) -> None:
        match path:
            case "/annotated":
                try:
                    obj: Any = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._json(w, {"ok": False, "err": "bad json"}, 400)
                    return
                seq: Any = obj.get("seq")
                img: Any = obj.get("image_b64", "")
                async with S.lock:
                    exp: int = S.pending_seq
                if seq != exp:
                    await self._json(w, {"ok": False, "err": f"seq {seq}!={exp}"}, 409)
                    return
                if not isinstance(img, str) or len(img) < 100:
                    await self._json(w, {"ok": False, "err": "img short"}, 400)
                    return
                async with S.lock:
                    S.annotated_b64 = img
                    S.annotated_seq = seq
                    S.annotated_event.set()
                await self._json(w, {"ok": True, "seq": seq})
            case "/inject":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._json(w, {"ok": False, "err": "bad json"}, 400)
                    return
                txt: Any = obj.get("vlm_text", "")
                if not isinstance(txt, str) or not txt.strip():
                    await self._json(w, {"ok": False, "err": "empty"}, 400)
                    return
                async with S.lock:
                    S.next_vlm = txt
                    S.next_event.set()
                await self._json(w, {"ok": True})
            case "/save_config":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._json(w, {"ok": False, "err": "bad json"}, 400)
                    return
                CONFIG_PATH.write_text(json.dumps(obj, indent=2, ensure_ascii=False), "utf-8")
                global _CFG
                _CFG = obj
                log.info("config saved")
                await self._json(w, {"ok": True})
            case "/save_pipeline":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._json(w, {"ok": False, "err": "bad json"}, 400)
                    return
                source: str = obj.get("source", "")
                PIPELINE_PY.write_text(source, "utf-8")
                log.info("pipeline.py saved")
                await self._json(w, {"ok": True})
            case _:
                await self._err(w, 404)

    async def _raw(self, w: asyncio.StreamWriter, code: int, ct: str, data: bytes) -> None:
        status_map: dict[int, str] = {
            200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
        }
        st: str = status_map.get(code, "OK")
        headers: str = (
            f"HTTP/1.1 {code} {st}\r\n"
            f"Content-Type: {ct}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET,POST,OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Connection: close\r\n\r\n"
        )
        w.write(headers.encode() + data)
        await w.drain()

    async def _json(self, w: asyncio.StreamWriter, obj: Any, code: int = 200) -> None:
        await self._raw(w, code, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    async def _err(self, w: asyncio.StreamWriter, code: int) -> None:
        await self._json(w, {"error": code}, code)


async def async_main() -> None:
    global S, STOP
    S, STOP = State(), asyncio.Event()
    rd: Path = make_run_dir()
    setup_logging(rd)
    log.info("Franz start rd=%s", rd)
    srv: Server = Server(str(cfg("host", "127.0.0.1")), int(cfg("port", 1234)))
    await srv.start()
    webbrowser.open(f"http://{cfg('host', '127.0.0.1')}:{cfg('port', 1234)}")
    task: asyncio.Task[None] = asyncio.create_task(engine_loop(rd))
    try:
        await STOP.wait()
    except KeyboardInterrupt:
        STOP.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await srv.stop()
    log.info("Franz stopped")


def main() -> None:
    time.sleep(5)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
