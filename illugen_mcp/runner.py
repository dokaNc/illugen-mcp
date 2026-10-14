"""Headless-style control of the real IlluGen application, with no mouse, focus or visible window.

How it works (each point verified against IlluGen 1.2.1):
- Every job runs a private copy: a hard link of IlluGen.exe in <state>/runtime/<job>/ with junctions to the
  resource folders and a copy of config.ini. IlluGen resolves config.ini, the recovery file and resources
  relative to its exe, so a GUI IlluGen the user has open is never affected (only logs/GL cache in HOME
  are shared). The folder is deleted afterwards (junctions are unlinked first, never followed).
- IlluGen opens the first argv entry that ends in ".illugen" and doesn't start with "-", but only when
  no recovery file exists next to the exe; the private folder starts without one.
- The app is started with SW_HIDE, then turned into a "ghost" window: layered with alpha 0, click-through,
  never activated, no taskbar entry. It stays invisible to the user but is still composited, so
  PrintWindow can take screenshots.
- Commands are sent with PostMessage(WM_KEYDOWN/WM_KEYUP) straight to the SDL window. Focus and cursor
  are not needed. Key bindings come from the [Shortcuts] section of config.ini (command name -> key).
- Progress comes from IlluGen's log (%LOCALAPPDATA%/JangaFX/illugen/logs/YYYY-MM-DD.log):
  "project.odin ... load project", "icons.odin ... Total icons" (UI ready),
  "export.odin:243] Write:" (one per exported item), "main.odin ... Exiting".
- UI-only modals ("Issues when loading project", "Export Failed") are detected on the screenshot: purple
  primary button at (+166, +67) and red/yellow icon at (-197, -17) from the window center.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .paths import DEFAULT_EXE, STATE_DIR

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "JangaFX" / "illugen" / "logs"

# Fallback bindings (IlluGen defaults) when config.ini can't be read.
DEFAULT_SHORTCUTS = {"export-all": "F9", "project-save": "Ctrl+S", "force-quit": "F8"}

if sys.platform == "win32":  # project editing stays importable elsewhere; the engine needs Windows
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x2, 0x4, 0x10
PW_RENDERFULLCONTENT = 2

VK_NAMES = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "return": 0x0D, "enter": 0x0D, "escape": 0x1B, "space": 0x20,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, ",": 0xBC, ".": 0xBE, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, "`": 0xC0, "keypad_add": 0x6B, "keypad_subtract": 0x6D,
}
VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK_NAMES.update({f"keypad_{i}": 0x60 + i for i in range(10)})


class IlluGenError(RuntimeError):
    """Engine-side failure (IlluGen missing, did not start, timed out...)."""

    HINT = ("\nIf this persists, check the IlluGen log in %LOCALAPPDATA%\\JangaFX\\illugen\\logs and report it at "
            "https://github.com/dokaNc/illugen-mcp/issues")

    def __str__(self) -> str:
        return super().__str__() + self.HINT


class ExportFailed(IlluGenError):
    HINT = ""  # usually a graph problem (shown in the screenshot), not an MCP defect


def _png(img) -> bytes:
    import io

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def parse_key_combo(combo: str) -> list[int]:
    """'Ctrl+Shift+S' -> [VK_CONTROL, VK_SHIFT, 'S']. Handles keys that are themselves '+'-free names."""
    vks = []
    for part in combo.split("+"):
        p = part.strip().lower()
        if p in VK_NAMES:
            vks.append(VK_NAMES[p])
        elif len(p) == 1 and p.isalnum():
            vks.append(ord(p.upper()))
        else:
            raise IlluGenError(f"unsupported key name {part!r} in shortcut {combo!r}")
    return vks


def read_shortcuts(config_path: Path) -> dict[str, str]:
    """command -> key combo, from config.ini [Shortcuts] (lines are 'Key+Combo = command')."""
    result = dict(DEFAULT_SHORTCUTS)
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    in_section, bound = False, {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_section = s == "[Shortcuts]"
            continue
        if in_section and " = " in s:
            key, cmd = s.split(" = ", 1)
            bound.setdefault(cmd.strip(), key.strip())
    if bound:
        result = {**{k: v for k, v in DEFAULT_SHORTCUTS.items() if k not in bound}, **bound}
    return result


class LogTail:
    """Reads IlluGen's daily log from a byte offset, following the midnight rollover.

    IlluGen names the file after the UTC date, which differs from the local date around midnight
    (e.g. 00:00-02:00 in UTC+2), so both candidate names are followed."""

    def __init__(self) -> None:
        self.offsets: dict[Path, int] = {p: (p.stat().st_size if p.exists() else 0) for p in self._today()}
        self.lines: list[str] = []

    @staticmethod
    def _today() -> list[Path]:
        days = dict.fromkeys((_dt.datetime.now(_dt.timezone.utc).date(), _dt.date.today()))
        return [LOG_DIR / f"{d:%Y-%m-%d}.log" for d in days]

    def poll(self) -> list[str]:
        for p in self._today():
            self.offsets.setdefault(p, 0)  # rollover: a new day's file is read from its start
        new: list[str] = []
        for p, offset in self.offsets.items():
            if not p.exists():
                continue
            with open(p, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            cut = chunk.rfind(b"\n") + 1  # keep incomplete last line for next poll
            if cut:
                self.offsets[p] = offset + cut
                new += chunk[:cut].decode("utf-8", errors="replace").splitlines()
        self.lines += new
        return new

    def count(self, needle: str) -> int:
        self.poll()
        return sum(needle in ln for ln in self.lines)

    def wait_for(self, needle: str, timeout: float, alive=None, min_count: int = 1) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.count(needle) >= min_count:
                return True
            if alive is not None and not alive():
                return False
            time.sleep(0.25)
        return False

    def issues(self) -> list[str]:
        self.poll()
        keep = ("serialization", "project.odin", "export.odin", "FATAL", "Fatal", "panic")
        return [ln for ln in self.lines if ("ERROR" in ln or "WARN" in ln) and any(k in ln for k in keep)]


def _illugen_pids() -> list[int]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq IlluGen.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("illugen") and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def _find_window(pid: int) -> int | None:
    found: list[int] = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "SDL_app":
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


RESOURCE_DIRS = ("hdri", "textures", "presets_1_0_0", "node_presets")


def _is_junction(p: Path) -> bool:
    try:
        return bool(os.lstat(p).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _remove_runtime(d: Path) -> None:
    """Deletes a private runtime dir. Junctions are unlinked first (os.rmdir removes the link only), so
    the real install folders they point to can never be touched."""
    if _is_junction(d):
        return
    for entry in os.scandir(d):
        if entry.is_dir(follow_symlinks=False) or _is_junction(Path(entry.path)):
            if _is_junction(Path(entry.path)):
                try:
                    os.rmdir(entry.path)
                except OSError:
                    return  # keep the dir rather than risk recursing into the install
    if any(_is_junction(Path(e.path)) for e in os.scandir(d)):
        return
    shutil.rmtree(d, ignore_errors=True)


def _make_runtime(install: Path) -> Path:
    """Private IlluGen 'install' for one job. IlluGen resolves config.ini, the recovery file and resources
    relative to its exe, so a hard link of IlluGen.exe in its own folder runs
    fully isolated from a GUI IlluGen the user may have open. Only HOME (logs, GL cache) stays shared."""
    root = STATE_DIR / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    for old in root.iterdir():  # leftovers of jobs that died: only our own "<pid>_<hex>" dirs, never links
        if (re.fullmatch(r"\d+_[0-9a-f]+", old.name) and not os.path.islink(old) and not _is_junction(old)
                and old.is_dir() and time.time() - old.stat().st_mtime > 3600):
            _remove_runtime(old)
    d = root / f"{os.getpid()}_{time.time_ns():x}"
    d.mkdir()
    try:
        os.link(install / "IlluGen.exe", d / "IlluGen.exe")
    except OSError:
        shutil.copy2(install / "IlluGen.exe", d / "IlluGen.exe")
    import _winapi

    for sub in RESOURCE_DIRS:
        if (install / sub).is_dir():
            _winapi.CreateJunction(str(install / sub), str(d / sub))
    if (install / "config.ini").exists():
        shutil.copy2(install / "config.ini", d / "config.ini")  # user's shortcuts and preferences
    return d


class _JobLock:
    """Serializes engine jobs across processes (several MCP clients may run this server)."""

    def __init__(self, timeout: float = 900):
        self.timeout = timeout
        self.f = None

    def __enter__(self):
        import msvcrt

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(STATE_DIR / "job.lock", "a+b")
        end = time.monotonic() + self.timeout
        while True:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() > end:
                    self.f.close()
                    raise IlluGenError("another IlluGen job is still running") from None
                time.sleep(0.5)

    def __exit__(self, *exc):
        import msvcrt

        try:
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


class IlluGenSession:
    """One managed, invisible IlluGen instance with one project loaded, in a private runtime folder."""

    _lock = threading.Lock()

    def __init__(self, project: Path, exe: Path = DEFAULT_EXE, width: int = 1600, height: int = 900):
        self.project = Path(project).resolve()
        self.install = Path(exe).parent
        self.runtime: Path | None = None
        self.size = (width, height)
        self.proc: subprocess.Popen | None = None
        self.hwnd: int | None = None
        self.log: LogTail | None = None
        self.shortcuts = read_shortcuts(self.install / "config.ini")
        self.notes: list[str] = []
        self.load_issue_png: bytes | None = None
        self.failure_png: bytes | None = None
        self._job: _JobLock | None = None

    # ---- lifecycle ----
    def __enter__(self) -> IlluGenSession:
        if not IlluGenSession._lock.acquire(timeout=900):
            raise IlluGenError("another IlluGen job is still running")
        try:
            self._job = _JobLock().__enter__()
            self.start()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.stop()
        finally:
            if self._job:
                self._job.__exit__()
                self._job = None
            IlluGenSession._lock.release()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 90) -> None:
        if not (self.install / "IlluGen.exe").exists():
            raise IlluGenError(f"IlluGen not found in {self.install} (set ILLUGEN_EXE)")
        if not self.project.exists():
            raise IlluGenError(f"project not found: {self.project}")
        if self.project.suffix.lower() != ".illugen":
            raise IlluGenError("IlluGen only opens files ending in .illugen from the command line")
        self.runtime = _make_runtime(self.install)
        self.log = LogTail()
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        self.proc = subprocess.Popen([str(self.runtime / "IlluGen.exe"), "--nosplash", str(self.project)],
                                     cwd=str(self.runtime), startupinfo=si)
        if not self.log.wait_for("Total icons", timeout, alive=self.alive):
            raise IlluGenError("IlluGen did not finish starting:\n" + "\n".join(self.log.lines[-15:]))
        if self.log.count("load project") == 0:
            raise IlluGenError("IlluGen started but did not load the project")
        end = time.monotonic() + 10
        while self.hwnd is None and time.monotonic() < end:
            self.hwnd = _find_window(self.proc.pid)
            time.sleep(0.2)
        if self.hwnd is None:
            raise IlluGenError("IlluGen window not found")
        self._ghost()
        time.sleep(1.5)
        # "Issues when loading project" (unknown node types...) is UI-only, never logged: keep its picture
        img = self.capture()
        if self.dialog(img):
            self.load_issue_png = _png(img)
            self.notes.append("IlluGen reported issues while loading the project (see screenshot)")
            self.key("Return")
            time.sleep(0.5)

    def _ghost(self) -> None:
        h = self.hwnd
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        user32.SetWindowLongW(h, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetLayeredWindowAttributes(h, 0, 0, 2)  # LWA_ALPHA, fully transparent
        user32.ShowWindow(h, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(h, None, 0, 0, self.size[0], self.size[1], SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)

    def stop(self, timeout: float = 15) -> None:
        if self.alive() and self.hwnd:
            try:
                if self.dialog():  # a modal dialog swallows shortcuts
                    self.key("Return")
                    time.sleep(0.5)
                self.command("force-quit")
                self.proc.wait(timeout)
            except Exception:
                pass
        if self.alive():
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=0x08000000)
            try:
                self.proc.wait(10)
            except Exception:
                pass
            self.notes.append("IlluGen did not quit cleanly and was terminated")
        self.proc = None
        if self.runtime is not None:
            time.sleep(0.5)
            _remove_runtime(self.runtime)  # private config.ini / recovery file go with it
            self.runtime = None

    # ---- input ----
    def key(self, combo: str) -> None:
        vks = parse_key_combo(combo)
        for vk in vks:
            sc = user32.MapVirtualKeyW(vk, 0)
            user32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, 1 | (sc << 16))
            time.sleep(0.04)
        for vk in reversed(vks):
            sc = user32.MapVirtualKeyW(vk, 0)
            user32.PostMessageW(self.hwnd, WM_KEYUP, vk, 1 | (sc << 16) | 0xC0000000)
            time.sleep(0.04)

    def command(self, name: str) -> None:
        combo = self.shortcuts.get(name)
        if not combo:
            raise IlluGenError(f"command {name!r} has no keyboard shortcut in config.ini [Shortcuts]")
        self.key(combo)

    # ---- operations ----
    def save(self, timeout: float = 30) -> None:
        before = self.project.stat().st_mtime_ns
        self.command("project-save")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.project.stat().st_mtime_ns != before and self.log.count("time to save project"):
                time.sleep(0.3)
                return
            time.sleep(0.25)
        raise IlluGenError("project save timed out")

    def export_all(self, timeout: float = 300, settle: float = 2.5) -> int:
        """Runs File > Export. Returns the number of items written according to the log."""
        needle = "export.odin:243"
        start = self.log.count(needle)
        self.command("export-all")
        end = time.monotonic() + timeout
        last, last_change, last_check = start, time.monotonic(), 0.0
        while time.monotonic() < end:
            n = self.log.count(needle)
            if n != last:
                last, last_change = n, time.monotonic()
            if n > start and time.monotonic() - last_change >= settle:
                return n - start
            if not self.alive():
                raise IlluGenError("IlluGen exited during export")
            # "Export Failed" (graph warnings, write errors) is only shown in the UI
            if time.monotonic() - last_check > 1.0:
                last_check = time.monotonic()
                img = self.capture()
                if self.dialog(img) == "error":
                    self.failure_png = _png(img)
                    self.key("Return")
                    raise ExportFailed("IlluGen refused to export: 'Nodes failed to export' (usually node "
                                       "warnings in the graph, e.g. a pin type mismatch). See screenshot.")
            time.sleep(0.25)
        if last > start:
            return last - start
        raise IlluGenError("export produced nothing before the timeout (are export nodes enabled and connected?)")

    def screenshot_png(self) -> bytes:
        """PNG of the (invisible) IlluGen window."""
        import io

        out = io.BytesIO()
        self.capture().save(out, "PNG")
        return out.getvalue()

    def dialog(self, img=None) -> str | None:
        """Classifies a modal IlluGen dialog from its icon (dialogs are centered; the icon sits at a
        fixed offset left of center): 'error' (red circle), 'warning' (yellow triangle) or None."""
        img = img or self.capture()
        w, h = img.size
        # every IlluGen modal (Export Failed, Issues when loading, Recovery) has its purple primary button
        # at (+166, +67) from the window center; require it so red/yellow viewport content can't match
        bx, by = w // 2 + 166, h // 2 + 67
        purple = 0
        for x in range(bx - 30, bx + 31, 3):
            for y in range(by - 6, by + 7, 3):
                r, g, b = img.getpixel((x, y))
                if abs(r - 119) < 30 and abs(g - 107) < 30 and abs(b - 247) < 30:
                    purple += 1
        if purple < 60:  # 21 x 5 samples
            return None
        cx, cy = w // 2 - 197, h // 2 - 17
        red = yellow = 0
        for x in range(cx - 24, cx + 25, 2):
            for y in range(cy - 24, cy + 25, 2):
                r, g, b = img.getpixel((x, y))
                if r > 230 and g < 120 and b < 120:
                    red += 1
                elif r > 220 and 150 < g < 225 and b < 110:
                    yellow += 1
        if red > 40:
            return "error"
        if yellow > 40:
            return "warning"
        return None

    def capture(self):
        """PIL image of the (invisible) IlluGen window."""
        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        old = gdi32.SelectObject(mem, bmp)
        try:
            user32.PrintWindow(self.hwnd, mem, PW_RENDERFULLCONTENT)

            class BIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

            bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        from PIL import Image

        return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
