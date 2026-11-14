"""
SSGrab - clipboard video grabber for Windows (SSvid-style).

Watches the clipboard. When a YouTube / Facebook / Vimeo / Instagram video URL
is copied while Chrome (or another configured browser) is the active window,
a small popup offers to download it as .mp4. Everything else is ignored.

Requires:  pip install yt-dlp pystray pillow
Also needs ffmpeg on PATH (winget install Gyan.FFmpeg) to merge HD video+audio,
and Deno on PATH (winget install DenoLand.Deno) for full YouTube support.

YouTube bot checks: on first start SSGrab downloads the bgutil PO-token generator
(bgutil-pot.exe, Rust build) and its yt-dlp plugin into %LOCALAPPDATA%\\SSGrab\\pot,
Nothing runs in the background: yt-dlp starts bgutil-pot.exe only when a YouTube
video needs a token, and it exits as soon as it has produced one.
Use "Update helpers" in the main window when YouTube starts failing again.
Set "use_token_helper": false in %APPDATA%\\SSGrab\\config.json to turn it off.
"""

import ctypes
import ctypes.wintypes as wt
import importlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("SSGrab", "yt-dlp is not installed.\n\nRun:  pip install yt-dlp")
    sys.exit(1)

APP = "SSGrab"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
CONFIG_FILE = CONFIG_DIR / "config.json"
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
TEMP_DIR = WORK_DIR / "tmp"
CACHE_DIR = WORK_DIR / "cache"

DEFAULTS = {
    "enabled": True,
    "download_dir": str(Path.home() / "Downloads"),
    "browsers": ["chrome.exe"],          # add "msedge.exe", "firefox.exe" if wanted
    "quality": "1080",                   # best | 2160 | 1440 | 1080 | 720 | 480 | audio
    "cookies_from_browser": "",          # e.g. "firefox"  (Chrome cookies are locked on Windows, see README)
    "cookie_file": "",                   # path to a Netscape cookies.txt export
    "popup_timeout_sec": 20,
    "use_token_helper": True,            # local bgutil PO-token generator for YouTube bot checks
}

# --- Which URLs we react to -------------------------------------------------
PATTERNS = {
    "YouTube": re.compile(
        r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?.*v=|shorts/|live/|embed/)"
        r"|youtu\.be/)[\w-]{6,}", re.I),
    "Facebook": re.compile(
        r"^https?://(?:www\.|m\.|web\.)?(?:facebook\.com/(?:.+/videos/|watch/?\?v=|reel/|share/[vr]/)"
        r"|fb\.watch/)", re.I),
    "Vimeo": re.compile(r"^https?://(?:www\.|player\.)?vimeo\.com/(?:video/)?(?:.*/)?\d{5,}", re.I),
    "Instagram": re.compile(r"^https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/[\w-]+", re.I),
}


def classify(text: str):
    text = text.strip()
    if "\n" in text or len(text) > 2000:
        return None
    for site, rx in PATTERNS.items():
        if rx.match(text):
            return site
    return None


# --- Windows helpers ---------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def clipboard_seq() -> int:
    return user32.GetClipboardSequenceNumber()


def foreground_exe() -> str:
    """Return the lowercase exe name of the foreground window's process."""
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


# --- Config ------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --- yt-dlp ------------------------------------------------------------------
def format_for(quality: str, have_ffmpeg: bool) -> str:
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio"
    h = "" if quality == "best" else f"[height<={quality}]"
    if have_ffmpeg:
        # Separate streams merged into mp4 (needed for anything above ~720p on YouTube)
        return (f"bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}[ext=mp4]/b{h}/b")
    # No ffmpeg: single pre-muxed file only
    return f"b{h}[ext=mp4]/b{h}/b"


def ydl_base_opts(cfg):
    return {"quiet": True, "no_warnings": True, "noplaylist": True,
            "cachedir": str(CACHE_DIR),
            "paths": {"temp": str(TEMP_DIR)}}


def cookie_opts(cfg):
    if cfg.get("cookie_file") and Path(cfg["cookie_file"]).exists():
        return {"cookiefile": cfg["cookie_file"]}
    if cfg.get("cookies_from_browser"):
        return {"cookiesfrombrowser": (cfg["cookies_from_browser"],)}
    return {}


def is_bot_check(err) -> bool:
    s = str(err).lower()
    return ("not a bot" in s or "sign in to confirm" in s or "http error 403" in s
            or "po token" in s or "only images are available" in s)


def attempt_plans(cfg, site, pot_ok):
    """Ordered list of extra yt-dlp options to try. Token helper first, cookies last
    (a PO token and logged-in cookies don't mix, so they are never sent together)."""
    plans = []
    if site == "YouTube":
        if pot_ok:
            # On-demand: yt-dlp runs bgutil-pot.exe only when a token is needed; it exits afterwards
            cli = {"youtubepot-bgutilcli": {"cli_path": [str(POT_EXE)]}}
            plans.append(("token helper", {"extractor_args": cli}))
            plans.append(("token helper, mweb client",
                          {"extractor_args": {**cli, "youtube": {"player_client": ["mweb"],
                                                                 "fetch_pot": ["always"]}}}))
        else:
            plans.append(("plain", {}))
    else:
        plans.append(("plain", {}))
    ck = cookie_opts(cfg)
    if ck:
        plans.append(("cookies", ck))
    return plans


def run_with_fallback(cfg, site, pot_ok, opts, url, download, log=None):
    last = None
    for label, extra in attempt_plans(cfg, site, pot_ok):
        try:
            with yt_dlp.YoutubeDL({**opts, **extra}) as y:
                return y, y.extract_info(url, download=download)
        except Exception as e:
            last = e
            if log:
                log(f"   attempt '{label}' failed: {str(e)[:120]}")
            if not is_bot_check(e):
                break          # a real error (removed video, etc.) - retrying won't help
    raise last


# --- YouTube PO-token helper (bgutil, Rust build) ----------------------------
POT_DIR = WORK_DIR / "pot"
POT_EXE = POT_DIR / "bgutil-pot.exe"
PLUGIN_DIR = POT_DIR / "plugins"
POT_REL = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/"
POT_EXE_URL = POT_REL + "bgutil-pot-windows-x86_64.exe"
POT_PLUGIN_URL = POT_REL + "bgutil-ytdlp-pot-provider-rs.zip"


class PotHelper:
    """Downloads the token generator and its yt-dlp plugin, and points yt-dlp at the plugin.
    Nothing runs in the background: yt-dlp launches bgutil-pot.exe per token and it exits."""

    def __init__(self, log):
        self.log = log
        self.ok = False
        self.ready = threading.Event()

    def _download(self, url, dest):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "SSGrab"})
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)

    def _install(self, force=False):
        import zipfile
        POT_DIR.mkdir(parents=True, exist_ok=True)
        if force or not POT_EXE.exists():
            self.log("Downloading YouTube token helper…")
            self._download(POT_EXE_URL, POT_EXE)
        target = PLUGIN_DIR / "bgutil"
        if force or not any(target.rglob("getpot_bgutil*.py")):
            shutil.rmtree(PLUGIN_DIR, ignore_errors=True)
            zpath = POT_DIR / "plugin.zip"
            self._download(POT_PLUGIN_URL, zpath)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(PLUGIN_DIR / "_x")
            zpath.unlink(missing_ok=True)
            # yt-dlp wants <plugin dir>/<package>/yt_dlp_plugins/...; normalise whatever the zip held
            found = next((p.parent for p in (PLUGIN_DIR / "_x").rglob("yt_dlp_plugins") if p.is_dir()), None)
            if not found:
                raise RuntimeError("plugin zip has no yt_dlp_plugins folder")
            shutil.move(str(found), str(target))
            shutil.rmtree(PLUGIN_DIR / "_x", ignore_errors=True)
            # We only use on-demand (CLI) mode; drop the server-mode provider so it never probes a port
            for p in target.rglob("getpot_bgutil_http.py"):
                p.unlink()

    def _load_plugins(self):
        from yt_dlp import globals as ygl, plugins as ypl
        ygl.plugin_dirs.value = [str(PLUGIN_DIR)]
        for name in [m for m in sys.modules if m.startswith("yt_dlp_plugins")]:
            del sys.modules[name]          # drop any stale (empty) namespace from an earlier load
        importlib.invalidate_caches()
        ypl.load_all_plugins()

    def start(self, force=False):
        def work():
            self.ready.clear()
            try:
                self._install(force)
                self._load_plugins()
                self.ok = True
                self.log("✔ YouTube token helper ready (runs only when a video needs it)")
            except Exception as e:
                self.ok = False
                self.log(f"⚠ YouTube token helper unavailable ({str(e)[:120]}) - "
                         f"YouTube will fall back to cookies if set")
            finally:
                self.ready.set()
        threading.Thread(target=work, daemon=True).start()

    def disable(self):
        from yt_dlp import globals as ygl
        ygl.plugin_dirs.value = []
        self.ok = False
        self.ready.set()


# --- GUI ---------------------------------------------------------------------
class Popup(tk.Toplevel):
    """Small always-on-top offer window in the bottom-right corner."""

    def __init__(self, app, url, site):
        super().__init__(app.root)
        self.app, self.url, self.site = app, url, site
        self.title(f"{APP} - {site}")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.remaining = app.cfg["popup_timeout_sec"]
        self.busy = False

        pad = {"padx": 10, "pady": 4}
        ttk.Label(self, text=f"{site} video detected", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        self.title_var = tk.StringVar(value="Fetching title…")
        ttk.Label(self, textvariable=self.title_var, wraplength=340).pack(anchor="w", **pad)

        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Quality:").pack(side="left")
        self.quality = ttk.Combobox(row, width=10, state="readonly",
                                    values=["best", "2160", "1440", "1080", "720", "480", "audio"])
        self.quality.set(app.cfg["quality"])
        self.quality.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, length=340, mode="determinate")
        self.progress.pack(**pad)
        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status).pack(anchor="w", **pad)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(4, 10))
        self.dl_btn = ttk.Button(btns, text="Download MP4", command=self.start)
        self.dl_btn.pack(side="left")
        self.ignore_btn = ttk.Button(btns, text="Ignore", command=self.destroy)
        self.ignore_btn.pack(side="right")

        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - w - 20}+{sh - h - 70}")

        threading.Thread(target=self._fetch_title, daemon=True).start()
        self.after(1000, self._tick)

    def _ui(self, fn, *a):
        self.app.q.put(lambda: self.winfo_exists() and fn(*a))

    def _tick(self):
        if not self.winfo_exists() or self.busy:
            return
        self.remaining -= 1
        if self.remaining <= 0:
            self.destroy()
            return
        self.ignore_btn.config(text=f"Ignore ({self.remaining})")
        self.after(1000, self._tick)

    def _fetch_title(self):
        try:
            pot_ok = self.app.pot_wait()
            _, info = run_with_fallback(self.app.cfg, self.site, pot_ok,
                                        {**ydl_base_opts(self.app.cfg), "skip_download": True},
                                        self.url, download=False)
            t = info.get("title") or self.url
            d = info.get("duration")
            if d:
                t += f"   ({int(d)//60}:{int(d)%60:02d})"
            self._ui(self.title_var.set, t)
        except Exception as e:
            self._ui(self.title_var.set, f"(Couldn't read details: {short_err(e)})")

    def start(self):
        self.busy = True
        self.dl_btn.config(state="disabled")
        self.quality.config(state="disabled")
        self.ignore_btn.config(text="Close")
        self.status.set("Starting…")
        threading.Thread(target=self._download, args=(self.quality.get(),), daemon=True).start()

    def _hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = done * 100 / total if total else 0
            spd = d.get("speed") or 0
            self._ui(self.progress.config, {"value": pct})
            self._ui(self.status.set, f"{pct:5.1f}%   {done/1e6:.1f} / {total/1e6:.1f} MB   {spd/1e6:.1f} MB/s")
        elif d["status"] == "finished":
            self._ui(self.status.set, "Processing (merging audio/video)…")

    def _download(self, quality):
        cfg = self.app.cfg
        have_ffmpeg = shutil.which("ffmpeg") is not None
        out_dir = Path(cfg["download_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            **ydl_base_opts(cfg),
            "format": format_for(quality, have_ffmpeg),
            "outtmpl": str(out_dir / "%(title).150B [%(id)s].%(ext)s"),
            "windowsfilenames": True,
            "progress_hooks": [self._hook],
        }
        if have_ffmpeg and quality != "audio":
            opts["merge_output_format"] = "mp4"
        try:
            pot_ok = self.app.pot_wait()
            y, info = run_with_fallback(cfg, self.site, pot_ok, opts, self.url,
                                        download=True, log=self.app.log)
            path = info.get("requested_downloads", [{}])[0].get("filepath") or y.prepare_filename(info)
            self._ui(self.progress.config, {"value": 100})
            self._ui(self.status.set, "Done ✔")
            self._ui(self._done, path)
            self.app.log(f"✔ {Path(path).name}")
        except Exception as e:
            msg = short_err(e)
            self._ui(self.status.set, f"Failed: {msg}")
            self.app.log(f"✖ {self.site}: {msg}")

    def _done(self, path):
        self.dl_btn.config(text="Show in folder", state="normal",
                           command=lambda: subprocess.Popen(["explorer", "/select,", os.path.normpath(path)]))


def short_err(e):
    s = str(e).replace("ERROR: ", "").strip()
    if is_bot_check(s):
        s = ("YouTube bot check not passed (token helper and cookies tried). "
             "Try 'Update helpers', or set a cookie source in Settings. " + s[:100])
    return s[:260]


class App:
    def __init__(self):
        self.cfg = load_config()
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.geometry("520x320")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.last_seq = clipboard_seq()
        self.last_url = ""
        self.tray = None
        self._build()
        self._setup_tray()
        self.root.after(400, self._poll)
        self.root.after(100, self._drain)
        self._check_deps()
        from yt_dlp import globals as ygl
        ygl.plugin_dirs.value = []         # no stray plugins; only ours, once it's installed
        self.pot = PotHelper(self.log)
        if self.cfg.get("use_token_helper", True):
            self.pot.start()
        else:
            self.pot.disable()

    def pot_wait(self):
        """Wait (up to 2 min, first run downloads ~50 MB) for the token helper; True if usable."""
        self.pot.ready.wait(120)
        return self.pot.ok

    def update_helpers(self):
        self.log("Updating YouTube token helper…")
        self.pot.start(force=True)

    # main window
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.enabled = tk.BooleanVar(value=self.cfg["enabled"])
        ttk.Checkbutton(top, text="Monitor clipboard", variable=self.enabled,
                        command=self._toggle).pack(side="left")
        ttk.Button(top, text="Settings…", command=self.settings).pack(side="right")
        ttk.Button(top, text="Update helpers", command=self.update_helpers).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Open folder",
                   command=lambda: os.startfile(self.cfg["download_dir"])).pack(side="right", padx=6)
        self.listbox = tk.Listbox(self.root, font=("Segoe UI", 9))
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log(f"Watching for YouTube / Facebook / Vimeo / Instagram links copied in "
                 f"{', '.join(self.cfg['browsers'])}")

    def log(self, msg):
        self.q.put(lambda: (self.listbox.insert("end", msg), self.listbox.see("end")))

    def _toggle(self):
        self.cfg["enabled"] = self.enabled.get()
        save_config(self.cfg)

    def _check_deps(self):
        if not shutil.which("ffmpeg"):
            self.log("⚠ ffmpeg not found - HD downloads limited to pre-merged (≈360-720p). "
                     "Install: winget install Gyan.FFmpeg")
        if not shutil.which("deno"):
            self.log("⚠ Deno not found - some YouTube videos may fail. Install: winget install DenoLand.Deno")

    # clipboard loop
    def _poll(self):
        try:
            seq = clipboard_seq()
            if seq != self.last_seq:
                self.last_seq = seq
                if self.cfg["enabled"] and foreground_exe() in [b.lower() for b in self.cfg["browsers"]]:
                    try:
                        text = self.root.clipboard_get()
                    except tk.TclError:
                        text = ""
                    site = classify(text)
                    if site and text.strip() != self.last_url:
                        self.last_url = text.strip()
                        self.log(f"→ {site}: {self.last_url}")
                        Popup(self, self.last_url, site)
        finally:
            self.root.after(400, self._poll)

    def _drain(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()()
            except Exception:
                pass
        self.root.after(100, self._drain)

    # settings dialog
    def settings(self):
        w = tk.Toplevel(self.root)
        w.title("Settings")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=12)
        f.pack()
        vars_ = {}

        def row(r, label, key, browse=None):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=", ".join(self.cfg[key]) if isinstance(self.cfg[key], list) else str(self.cfg[key]))
            ttk.Entry(f, textvariable=v, width=44).grid(row=r, column=1, pady=3)
            if browse:
                ttk.Button(f, text="…", width=3, command=lambda: v.set(browse() or v.get())).grid(row=r, column=2)
            vars_[key] = v

        row(0, "Download folder", "download_dir", filedialog.askdirectory)
        row(1, "Browsers (exe names)", "browsers")
        row(2, "Default quality", "quality")
        row(3, "Cookies from browser", "cookies_from_browser")
        row(4, "Cookie file (cookies.txt)", "cookie_file",
            lambda: filedialog.askopenfilename(filetypes=[("Cookie file", "*.txt"), ("All", "*.*")]))
        row(5, "Popup timeout (sec)", "popup_timeout_sec")
        ttk.Label(f, foreground="gray", wraplength=420, justify="left",
                  text="Quality: best, 2160, 1440, 1080, 720, 480, audio.\n"
                       "Cookies are only needed for private / age-restricted / login-only videos. "
                       "Use 'firefox' or a cookies.txt export (Chrome's own cookie store is "
                       "encrypted on Windows and can't be read by other programs).").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))

        def ok():
            for k, v in vars_.items():
                val = v.get().strip()
                if k == "browsers":
                    self.cfg[k] = [b.strip() for b in val.split(",") if b.strip()]
                elif k == "popup_timeout_sec":
                    self.cfg[k] = max(5, int(val or 20))
                else:
                    self.cfg[k] = val
            save_config(self.cfg)
            w.destroy()

        ttk.Button(f, text="Save", command=ok).grid(row=7, column=1, sticky="e", pady=6)

    # tray
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        img = Image.new("RGB", (64, 64), (200, 30, 30))
        ImageDraw.Draw(img).polygon([(22, 16), (22, 48), (48, 32)], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.q.put(self.show), default=True),
            pystray.MenuItem("Monitoring", lambda: self.q.put(self._flip),
                             checked=lambda _: self.cfg["enabled"]),
            pystray.MenuItem("Quit", lambda: self.q.put(self.quit)),
        )
        self.tray = pystray.Icon(APP, img, APP, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _flip(self):
        self.enabled.set(not self.enabled.get())
        self._toggle()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        if "--minimized" in sys.argv:
            self.root.after(50, self.hide)
        self.root.mainloop()


if __name__ == "__main__":
    # Single-instance guard
    mutex = kernel32.CreateMutexW(None, False, "Global\\SSGrabSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    # Never run from wherever Windows happened to start us (often C:\Windows\System32,
    # which is read-only). Use our own writable folder for the working dir and temp files.
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for old in TEMP_DIR.glob("*"):
        try:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
        except OSError:
            pass
    os.chdir(WORK_DIR)
    os.environ["TEMP"] = os.environ["TMP"] = str(TEMP_DIR)
    tempfile.tempdir = str(TEMP_DIR)
    App().run()
