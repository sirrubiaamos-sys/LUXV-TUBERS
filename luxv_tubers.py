#!/usr/bin/env python3
"""Luxv Tubers — Your Videos. Your Quality. Your Speed.

Run with: python3 luxv_tubers.py

Dependencies (install separately; this app never invokes sudo or installs software):
  Kali/Debian: sudo apt install python3-tk ffmpeg; install yt-dlp into .python-packages
  Windows:     py -m pip install --target .python-packages yt-dlp; install FFmpeg
  macOS:       python3 -m pip install --target .python-packages yt-dlp; install FFmpeg

Only download videos that you are authorized to save. This application does not
bypass DRM, authentication, paywalls, or other access restrictions.
"""
from __future__ import annotations

import json
import base64
from datetime import datetime
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

APP_NAME = "Luxv Tubers"
VERSION = "1.0.0"
ACCENT = "#35a8ff"
CYAN = "#48e0dc"
BG = "#0b1019"
PANEL = "#111a27"
CARD = "#172334"
TEXT = "#edf4ff"
MUTED = "#96a9bf"
ERROR = "#ff717c"
SUCCESS = "#53d69b"
QUALITY_TIERS = ((240, "240p"), (360, "360p"), (420, "420p"), (480, "480p"), (720, "720p"),
                 (1080, "1080p"), (1440, "1440p"), (2160, "4K"))
VIDEO_BITRATES_KBPS = {240: 250, 360: 400, 420: 550, 480: 700, 720: 1400,
                       1080: 2500, 1440: 4500, 2160: 8000}

def app_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    target = root / "LuxvTubers"
    target.mkdir(parents=True, exist_ok=True)
    return target


CONFIG_DIR = app_dir()
CONFIG_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = CONFIG_DIR / "luxv_tubers.log"
HISTORY_FILE = CONFIG_DIR / "download_history.json"
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# A private per-application package directory lets users install yt-dlp locally
# without changing their system Python or requiring administrator privileges.
LOCAL_PACKAGES = Path(__file__).resolve().parent / ".python-packages"
if LOCAL_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PACKAGES))

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
except ImportError:
    yt_dlp = None
    class DownloadCancelled(Exception):
        pass


def default_download_dir() -> Path:
    return Path.home() / "Downloads"


DEFAULTS: dict[str, Any] = {
    "directory": str(default_download_dir()), "quality": "best", "container": "mp4",
    "max_concurrent": 2, "fragment_concurrency": 4, "retries": 5,
    "timeout": 30, "open_folder": False, "confirm_overwrite": True, "theme": "dark",
}


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Settings must be an object")
        result = DEFAULTS | data
        result["directory"] = str(Path(result["directory"]).expanduser())
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULTS.copy()


def save_settings(settings: dict[str, Any]) -> None:
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temp.replace(CONFIG_FILE)


def safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:180] or "video")


def human_size(size: int | float | None) -> str:
    if not size:
        return "Size unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "Size unknown"


def duration_text(seconds: int | float | None) -> str:
    if seconds is None:
        return "Duration unavailable"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def checked_url(value: str) -> str:
    value = value.strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a public HTTP or HTTPS video URL without embedded credentials.")
    hostname = parsed.hostname.lower().strip("[]")
    if hostname in {"localhost", "::1", "::"} or hostname.endswith(".local"):
        raise ValueError("Local network addresses are not supported.")
    if re.match(r"^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", hostname):
        raise ValueError("Local network addresses are not supported.")
    return value


def fmt_quality(fmt: dict[str, Any]) -> tuple[int, int, int]:
    return (int(fmt.get("height") or 0), int(fmt.get("tbr") or 0), int(fmt.get("filesize") or fmt.get("filesize_approx") or 0))


def available_video_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    candidates = [f for f in formats if f.get("format_id") and f.get("vcodec") not in (None, "none")]
    if not candidates and info.get("format_id"):
        candidates = [info]
    # Prefer one choice per resolution. Keep the best-reported format at each height.
    by_height: dict[int, dict[str, Any]] = {}
    for fmt in candidates:
        height = int(fmt.get("height") or 0)
        if height not in by_height or fmt_quality(fmt) > fmt_quality(by_height[height]):
            by_height[height] = fmt
    return sorted(by_height.values(), key=fmt_quality)


@dataclass
class Job:
    url: str
    title: str = "Waiting for analysis"
    state: str = "Queued"
    percent: float = 0.0
    speed: str = ""
    eta: str = ""
    size: str = ""
    error: str = ""
    job_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    info: dict[str, Any] | None = None
    format_value: str = "bestvideo+bestaudio/best"
    output_path: str = ""
    destination: str = ""
    container: str = "mp4"


class LuxvTubers:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.jobs: dict[str, Job] = {}
        self.history: list[dict[str, Any]] = self._load_history()
        self.waiting: queue.Queue[str] = queue.Queue()
        self.active_count = 0
        self.active_lock = threading.Lock()
        self.current_info: dict[str, Any] | None = None
        self.current_formats: list[dict[str, Any]] = []
        self.selected_job: str | None = None
        self._closing = False
        self._thumb_ref = None
        self._configure_window()
        self._configure_style()
        self._build_ui()
        self._dependency_status()
        self.root.after(80, self._drain_events)
        self.root.after(350, self._start_waiting)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} · {VERSION}")
        self.root.geometry("1160x790")
        self.root.minsize(850, 640)
        self.root.configure(bg=BG)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=31, borderwidth=0, font=("TkDefaultFont", 9))
        style.configure("Treeview.Heading", background=CARD, foreground=MUTED,
                        relief="flat", font=("TkDefaultFont", 9, "bold"))
        style.map("Treeview", background=[("selected", "#173a5a")], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor="#223145", background=ACCENT,
                        bordercolor="#223145", lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TCombobox", fieldbackground=PANEL, background=CARD, foreground=TEXT,
                        arrowcolor=CYAN, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])

    def _label(self, parent: tk.Widget, text: str, *, size: int = 10, color: str = TEXT,
               bold: bool = False, **kwargs: Any) -> tk.Label:
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("TkDefaultFont", size, "bold" if bold else "normal"), **kwargs)

    def _button(self, parent: tk.Widget, text: str, command: Any, *, primary: bool = False,
                width: int | None = None) -> tk.Button:
        bg = ACCENT if primary else CARD
        fg = "#06111c" if primary else TEXT
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=CYAN,
                         activeforeground="#06111c", relief="flat", bd=0, padx=14, pady=8,
                         cursor="hand2", font=("TkDefaultFont", 9, "bold"), width=width)

    def _panel(self, parent: tk.Widget, **kwargs: Any) -> tk.Frame:
        return tk.Frame(parent, bg=PANEL, highlightbackground="#223044", highlightthickness=1, **kwargs)

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=22, pady=18)
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 17))
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        mark = tk.Canvas(brand, width=42, height=42, bg=BG, highlightthickness=0)
        mark.pack(side="left", padx=(0, 10))
        mark.create_oval(2, 2, 40, 40, fill="#143456", outline=ACCENT, width=2)
        mark.create_polygon(17, 11, 17, 31, 32, 21, fill=CYAN, outline="")
        titlebox = tk.Frame(brand, bg=BG)
        titlebox.pack(side="left")
        self._label(titlebox, "LUXV TUBERS", size=16, bold=True).pack(anchor="w")
        self._label(titlebox, "YOUR VIDEOS. YOUR QUALITY. YOUR SPEED.", size=8, color=MUTED).pack(anchor="w", pady=(3, 0))
        self.status_label = self._label(header, "Checking dependencies…", size=9, color=MUTED)
        self.status_label.pack(side="right", padx=8)
        self._button(header, "Settings", self.open_settings).pack(side="right", padx=(6, 0))
        self._button(header, "History", self.open_history).pack(side="right", padx=6)
        self._button(header, "About", self.open_about).pack(side="right", padx=6)

        link_panel = self._panel(outer)
        link_panel.pack(fill="x", pady=(0, 12))
        top = tk.Frame(link_panel, bg=PANEL)
        top.pack(fill="x", padx=16, pady=(13, 9))
        self._label(top, "Paste a video link", size=12, bold=True).pack(side="left")
        self._label(top, "Public media supported by your installed yt-dlp", size=9, color=MUTED).pack(side="right")
        row = tk.Frame(link_panel, bg=PANEL)
        row.pack(fill="x", padx=16, pady=(0, 14))
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(row, textvariable=self.url_var, bg=BG, fg=TEXT, insertbackground=CYAN,
                                  relief="flat", highlightthickness=1, highlightbackground="#29394e",
                                  highlightcolor=ACCENT, font=("TkDefaultFont", 11))
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self.url_entry.bind("<Return>", lambda _e: self.analyze_url())
        self._button(row, "Paste link", self.paste_link).pack(side="left", padx=3)
        self._button(row, "Analyze", self.analyze_url, primary=True).pack(side="left", padx=(6, 0))

        info_panel = self._panel(outer)
        info_panel.pack(fill="x", pady=(0, 12))
        info_head = tk.Frame(info_panel, bg=PANEL)
        info_head.pack(fill="x", padx=16, pady=(13, 10))
        self._label(info_head, "VIDEO INFORMATION", size=9, color=CYAN, bold=True).pack(side="left")
        self.media_summary = self._label(info_head, "Analyze a URL to inspect its real formats.", size=9, color=MUTED)
        self.media_summary.pack(side="right")
        detail = tk.Frame(info_panel, bg=PANEL)
        detail.pack(fill="x", padx=16, pady=(0, 14))
        self.thumb_label = tk.Label(detail, text="▶", bg=CARD, fg=ACCENT, width=17, height=5,
                                    font=("TkDefaultFont", 25, "bold"))
        self.thumb_label.pack(side="left", padx=(0, 14))
        self.details_text = tk.Text(detail, height=4, bg=PANEL, fg=TEXT, relief="flat", wrap="word",
                                    font=("TkDefaultFont", 10), borderwidth=0, state="disabled")
        self.details_text.pack(side="left", fill="both", expand=True)
        controls = tk.Frame(info_panel, bg=PANEL)
        controls.pack(fill="x", padx=16, pady=(0, 15))
        self._label(controls, "VIDEO QUALITY", size=8, color=MUTED, bold=True).pack(side="left", padx=(0, 8))
        self.quality_var = tk.StringVar(value="Analyze a URL first")
        self.quality_box = ttk.Combobox(controls, textvariable=self.quality_var, state="readonly", width=35, values=[])
        self.quality_box.pack(side="left", padx=(0, 14))
        self._label(controls, "CONTAINER", size=8, color=MUTED, bold=True).pack(side="left", padx=(0, 8))
        self.container_var = tk.StringVar(value=self.settings["container"])
        self.container_box = ttk.Combobox(controls, textvariable=self.container_var,
                                          state="readonly", width=10, values=("mp4", "mkv", "webm"))
        self.container_box.pack(side="left", padx=(0, 14))
        self._label(controls, "SAVE TO", size=8, color=MUTED, bold=True).pack(side="left", padx=(0, 8))
        self.dest_var = tk.StringVar(value=self.settings["directory"])
        self.dest_entry = tk.Entry(controls, textvariable=self.dest_var, bg=BG, fg=TEXT,
                                   insertbackground=CYAN, relief="flat", width=36,
                                   highlightthickness=1, highlightbackground="#29394e")
        self.dest_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._button(controls, "Browse", self.choose_folder).pack(side="left", padx=(7, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(0, 10))
        self.download_btn = self._button(actions, "Download Video", self.enqueue_video, primary=True)
        self.download_btn.pack(side="left")
        self._button(actions, "Add URL batch", self.add_batch).pack(side="left", padx=8)
        self._button(actions, "Open Download Folder", self.open_download_folder).pack(side="right")

        queue_panel = self._panel(outer)
        queue_panel.pack(fill="both", expand=True)
        queue_head = tk.Frame(queue_panel, bg=PANEL)
        queue_head.pack(fill="x", padx=14, pady=(11, 8))
        self._label(queue_head, "DOWNLOAD QUEUE", size=9, color=CYAN, bold=True).pack(side="left")
        self.queue_count_label = self._label(queue_head, "0 jobs", size=9, color=MUTED)
        self.queue_count_label.pack(side="right")
        cols = ("video", "status", "quality", "progress", "speed", "eta")
        self.tree = ttk.Treeview(queue_panel, columns=cols, show="headings", selectmode="browse", height=8)
        labels = {"video":"Video", "status":"Status", "quality":"Quality", "progress":"Progress", "speed":"Speed", "eta":"Time left"}
        widths = {"video":355, "status":90, "quality":145, "progress":90, "speed":100, "eta":85}
        for col in cols:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], minwidth=55, stretch=col == "video", anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 9))
        self.tree.bind("<<TreeviewSelect>>", self.on_select_job)
        job_actions = tk.Frame(queue_panel, bg=PANEL)
        job_actions.pack(fill="x", padx=12, pady=(0, 11))
        self._button(job_actions, "Cancel selected", self.cancel_selected).pack(side="left")
        self._button(job_actions, "Retry failed", self.retry_selected).pack(side="left", padx=7)
        self._button(job_actions, "Remove selected", self.remove_selected).pack(side="left")
        self._button(job_actions, "Clear finished", self.clear_finished).pack(side="right")

        self.progress_label = self._label(outer, "Ready · Active, completed, and failed jobs are shown in the queue.", size=9, color=MUTED, anchor="w")
        self.progress_label.pack(fill="x", pady=(8, 2))
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100, style="Horizontal.TProgressbar")
        self.progress.pack(fill="x")

    def _dependency_status(self) -> None:
        if yt_dlp is None:
            self.status_label.config(text="yt-dlp missing", fg=ERROR)
            self.download_btn.config(state="disabled")
            self._set_info("yt-dlp is required. Open About for installation instructions.")
            return
        ffmpeg = shutil.which("ffmpeg")
        self.status_label.config(text=f"yt-dlp {yt_dlp.version.__version__}  ·  FFmpeg {'ready' if ffmpeg else 'required'}",
                                 fg=SUCCESS if ffmpeg else ERROR)
        if not ffmpeg:
            self.download_btn.config(state="disabled")

    def _set_info(self, text: str) -> None:
        self.details_text.config(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", text)
        self.details_text.config(state="disabled")

    def paste_link(self) -> None:
        try:
            self.url_var.set(self.root.clipboard_get().strip())
            self.url_entry.focus_set()
        except tk.TclError:
            messagebox.showinfo(APP_NAME, "The clipboard does not contain text.", parent=self.root)

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()), parent=self.root)
        if chosen:
            self.dest_var.set(chosen)

    def analyze_url(self, url: str | None = None) -> None:
        if yt_dlp is None:
            self.open_about()
            return
        raw = url if url is not None else self.url_var.get()
        try:
            video_url = checked_url(raw)
        except ValueError as exc:
            messagebox.showerror("Invalid URL", str(exc), parent=self.root)
            return
        self.download_btn.config(state="disabled", text="Analyzing…")
        self.media_summary.config(text="Retrieving video metadata…", fg=CYAN)
        self._set_info("Connecting to the source. This may take a few seconds.")
        threading.Thread(target=self._analyze_worker, args=(video_url,), daemon=True).start()

    def _ydl_options(self) -> dict[str, Any]:
        return {"quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": int(self.settings["timeout"]), "cachedir": False,
                "logger": _YtdlpLogger()}

    def _analyze_worker(self, url: str) -> None:
        try:
            with yt_dlp.YoutubeDL(self._ydl_options()) as ydl:
                info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise RuntimeError("The source did not return usable video information.")
            formats = available_video_formats(info)
            self.events.put(("analysis_ok", (info, formats)))
        except Exception as exc:
            logging.exception("Video analysis failed")
            self.events.put(("analysis_error", self._friendly_error(exc)))

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc).strip()
        if not text:
            return "The source could not be analyzed. Check the URL and connection."
        low = text.lower()
        if "unsupported url" in low or "no suitable extractor" in low:
            return "This website or URL is not supported by the installed yt-dlp version."
        if "timed out" in low or "timeout" in low:
            return "The source took too long to respond. Check your connection and try again."
        if "private" in low or "login" in low or "authentication" in low:
            return "This content is not publicly available to this app. It will not bypass access restrictions."
        return text[:600]

    @staticmethod
    def _format_title(fmt: dict[str, Any]) -> str:
        height = fmt.get("height")
        resolution = f"{height}p" if height else fmt.get("resolution") or "Video"
        ext = fmt.get("ext") or "media"
        size = human_size(fmt.get("filesize") or fmt.get("filesize_approx"))
        note = fmt.get("format_note")
        return f"{resolution} · {ext} · {size}" + (f" · {note}" if note and str(note).lower() not in str(resolution).lower() else "")

    @staticmethod
    def _estimated_size(fmt: dict[str, Any], duration: int | float | None) -> str:
        size = fmt.get("filesize")
        approximate = False
        if not size:
            size = fmt.get("filesize_approx")
            approximate = bool(size)
        if not size and duration and fmt.get("tbr"):
            try:
                size = float(fmt["tbr"]) * 1000 / 8 * float(duration)
                approximate = True
            except (TypeError, ValueError):
                size = None
        label = human_size(size)
        return f"~{label}" if approximate and size else label

    def _tier_title(self, height: int, label: str, formats: list[dict[str, Any]], duration: int | float | None) -> str:
        bitrate = VIDEO_BITRATES_KBPS[height] + 96  # Include estimated AAC audio.
        size = float(duration) * bitrate * 1000 / 8 if duration else None
        return f"{label} · ~{human_size(size)}"

    def _show_analysis(self, info: dict[str, Any], formats: list[dict[str, Any]]) -> None:
        self.current_info, self.current_formats = info, formats
        title = safe_filename(str(info.get("title") or "Untitled video"))
        uploader = str(info.get("uploader") or info.get("channel") or "Uploader unavailable")[:120]
        source = str(info.get("extractor_key") or info.get("extractor") or "Source unavailable")
        text = (f"{title}\nUploader: {uploader}\nDuration: {duration_text(info.get('duration'))}\nSource: {source}\n"
                "Quality sizes are estimates; merged audio can increase the final size.")
        self._set_info(text)
        self.media_summary.config(text=f"{len(formats)} video format(s) available", fg=SUCCESS)
        values = ["Best available quality", "Lowest available quality"] + [
            self._tier_title(height, label, formats, info.get("duration"))
            for height, label in QUALITY_TIERS
        ]
        self.quality_box.configure(values=values)
        default = self._quality_label_for_setting(formats)
        self.quality_var.set(default if default in values else values[0])
        self.download_btn.config(state="normal" if shutil.which("ffmpeg") else "disabled",
                                 text="Download Video")
        thumb = info.get("thumbnail")
        if thumb:
            threading.Thread(target=self._thumbnail_worker, args=(thumb,), daemon=True).start()
        if not formats:
            self.media_summary.config(text="No video formats were reported", fg=ERROR)
            self.download_btn.config(state="disabled")

    def _quality_label_for_setting(self, formats: list[dict[str, Any]]) -> str:
        configured = self.settings.get("quality", "best")
        if configured == "best":
            return "Best available quality"
        if configured == "worst":
            return "Lowest available quality"
        try:
            target = int(configured)
        except (TypeError, ValueError):
            return "Best available quality"
        tier = next(((height, label) for height, label in QUALITY_TIERS if height >= target), QUALITY_TIERS[-1])
        return self._tier_title(*tier, formats, self.current_info.get("duration") if self.current_info else None)

    def _thumbnail_worker(self, url: str) -> None:
        try:
            # Pillow is optional. No new runtime dependency is required for the application.
            import urllib.request
            from PIL import Image  # type: ignore
            request = urllib.request.Request(url, headers={"User-Agent": "LuxvTubers/1.0"})
            with urllib.request.urlopen(request, timeout=8) as response:
                image_data = response.read(3 * 1024 * 1024)
            import io
            image = Image.open(io.BytesIO(image_data))
            image.thumbnail((210, 95))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.events.put(("thumbnail", base64.b64encode(buffer.getvalue()).decode("ascii")))
        except Exception:
            return

    def _selected_format(self) -> tuple[str, str]:
        if not self.current_formats:
            return "bestvideo+bestaudio/best", "Best available quality"
        label = self.quality_var.get()
        if label == "Best available quality":
            return "bestvideo+bestaudio/best", label
        if label == "Lowest available quality":
            return "worstvideo+bestaudio/worst", label
        for height, tier_label in QUALITY_TIERS:
            if label.startswith(f"{tier_label} ·"):
                options = [f for f in self.current_formats if 0 < int(f.get("height") or 0) <= height]
                if not options:
                    fmt = self.current_formats[0]
                    fmt_id = str(fmt.get("format_id"))
                    fallback = f"{fmt_id}+bestaudio/best" if fmt.get("acodec") in (None, "none") else fmt_id
                    return fallback, label
                fmt = max(options, key=fmt_quality)
                fmt_id = str(fmt.get("format_id"))
                if fmt.get("acodec") in (None, "none"):
                    return f"{fmt_id}+bestaudio/best", label
                return fmt_id, label
        return "bestvideo+bestaudio/best", "Best available quality"

    @staticmethod
    def _height_for_selection(label: str, formats: list[dict[str, Any]]) -> int:
        for height, tier_label in QUALITY_TIERS:
            if label.startswith(f"{tier_label} ·"):
                return height
        available = [int(f.get("height") or 0) for f in formats if f.get("height")]
        if label == "Lowest available quality" and available:
            return min(available)
        return min(max(available, default=1080), 2160)

    def enqueue_video(self) -> None:
        if yt_dlp is None:
            self.open_about()
            return
        if not self.current_info:
            messagebox.showinfo(APP_NAME, "Analyze a video URL before adding a download.", parent=self.root)
            return
        try:
            url = checked_url(self.url_var.get())
            folder = Path(self.dest_var.get()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            if not os.access(folder, os.W_OK):
                raise PermissionError("The selected folder is not writable.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Download location", str(exc), parent=self.root)
            return
        if any(job.url == url and job.state in {"Queued", "Downloading"} for job in self.jobs.values()):
            messagebox.showinfo(APP_NAME, "This URL is already in the active queue.", parent=self.root)
            return
        fmt, quality = self._selected_format()
        job_id = f"{time.time_ns()}"
        job = Job(url=url, title=safe_filename(self.current_info.get("title") or "Video"),
                  info=self.current_info, format_value=fmt, job_id=job_id,
                  destination=str(folder), container=self.container_var.get())
        job.info = dict(job.info or {})
        job.info["selected_quality"] = quality
        job.info["target_height"] = self._height_for_selection(quality, self.current_formats)
        self.jobs[job_id] = job
        self.tree.insert("", "end", iid=job_id, values=(job.title, "Queued", quality, "0%", "—", "—"))
        self.waiting.put(job_id)
        self._update_queue_count()
        self._start_waiting()

    def add_batch(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Add video URLs")
        window.configure(bg=BG)
        window.geometry("550x380")
        tk.Label(window, text="Paste one video URL per line. Each URL is analyzed before download.",
                 bg=BG, fg=MUTED, wraplength=500).pack(anchor="w", padx=16, pady=14)
        text = tk.Text(window, bg=PANEL, fg=TEXT, insertbackground=CYAN, relief="flat", height=12)
        text.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        def add_urls() -> None:
            urls = [line.strip() for line in text.get("1.0", "end").splitlines() if line.strip()]
            valid: list[str] = []
            for item in urls:
                try:
                    valid.append(checked_url(item))
                except ValueError:
                    continue
            if not valid:
                messagebox.showerror("No valid URLs", "Add one or more public HTTP(S) video URLs.", parent=window)
                return
            window.destroy()
            for item in valid:
                job = Job(url=item, job_id=f"{time.time_ns()}-{len(self.jobs)}", title="Analyzing URL",
                          destination=self.dest_var.get(), container=self.container_var.get())
                self.jobs[job.job_id] = job
                self.tree.insert("", "end", iid=job.job_id, values=(job.title, "Analyzing", "Default quality", "0%", "—", "—"))
                threading.Thread(target=self._batch_analyze_worker, args=(job.job_id,), daemon=True).start()
            self._update_queue_count()
        self._button(window, "Add valid URLs", add_urls, primary=True).pack(anchor="e", padx=16, pady=(0, 16))

    def _batch_analyze_worker(self, job_id: str) -> None:
        job = self.jobs[job_id]
        try:
            with yt_dlp.YoutubeDL(self._ydl_options()) as ydl:
                info = ydl.extract_info(job.url, download=False)
            if not isinstance(info, dict) or not available_video_formats(info):
                raise RuntimeError("No video formats were reported for this URL.")
            self.events.put(("batch_ok", (job_id, info)))
        except Exception as exc:
            logging.exception("Batch URL analysis failed")
            self.events.put(("batch_error", (job_id, self._friendly_error(exc))))

    def _start_waiting(self) -> None:
        if self._closing:
            return
        max_jobs = max(1, min(4, int(self.settings.get("max_concurrent", 2))))
        while self.active_count < max_jobs and not self.waiting.empty():
            try:
                job_id = self.waiting.get_nowait()
            except queue.Empty:
                break
            job = self.jobs.get(job_id)
            if not job or job.state != "Queued":
                continue
            self.active_count += 1
            job.state = "Downloading"
            self._update_job(job)
            threading.Thread(target=self._download_worker, args=(job_id,), daemon=True).start()

    def _download_worker(self, job_id: str) -> None:
        job = self.jobs[job_id]
        folder = Path(job.destination or self.settings["directory"]).expanduser()
        template = str(folder / "%(title).180B [%(id)s].%(ext)s")
        container = job.container or str(self.settings.get("container", "mp4"))

        def progress_hook(data: dict[str, Any]) -> None:
            if job.cancel.is_set():
                raise DownloadCancelled("Cancelled by user")
            if data.get("status") == "downloading":
                downloaded = data.get("downloaded_bytes") or 0
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                percent = float(downloaded) * 100 / total if total else 0.0
                speed = data.get("speed")
                eta = data.get("eta")
                self.events.put(("progress", (job_id, percent, human_size(speed) + "/s" if speed else "—",
                                                duration_text(eta) if eta is not None else "—",
                                                f"{human_size(downloaded)} / {human_size(total)}" if total else human_size(downloaded))))
            elif data.get("status") == "finished":
                self.events.put(("job_state", (job_id, "Processing", "Merging streams if needed…")))

        def postprocessor_hook(data: dict[str, Any]) -> None:
            details = data.get("info_dict") or {}
            path = details.get("filepath") or details.get("_filename")
            if path:
                job.output_path = str(path)

        options = self._ydl_options() | {
            "format": job.format_value,
            "outtmpl": template,
            "merge_output_format": container,
            "retries": int(self.settings.get("retries", 5)),
            "fragment_retries": int(self.settings.get("retries", 5)),
            "socket_timeout": int(self.settings.get("timeout", 30)),
            "concurrent_fragment_downloads": int(self.settings.get("fragment_concurrency", 4)),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "continuedl": True,
            "nopart": False,
            "overwrites": not bool(self.settings.get("confirm_overwrite", True)),
            "noplaylist": True,
        }
        if container == "mp4":
            options["format_sort"] = ["res", "ext:mp4:m4a"]
            options["postprocessor_args"] = {"Merger+ffmpeg_o": ["-movflags", "+faststart"]}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([job.url])
            target_height = int((job.info or {}).get("target_height") or 0)
            if target_height:
                if not shutil.which("ffmpeg"):
                    raise RuntimeError("FFmpeg is required to resize/compress downloads. Install FFmpeg and restart the app.")
                source = Path(job.output_path) if job.output_path else None
                if not source or not source.is_file():
                    video_id = str((job.info or {}).get("id") or "")
                    matches = [p for p in folder.iterdir() if p.is_file() and video_id and f"[{video_id}]" in p.name]
                    if matches:
                        source = max(matches, key=lambda p: p.stat().st_mtime)
                if not source or not source.is_file():
                    raise RuntimeError("Download finished, but the output file could not be located for compression.")
                self.events.put(("job_state", (job_id, "Processing", f"Compressing to {target_height}p…")))
                output = source if source.suffix.lower() == f".{container}" else source.with_suffix(f".{container}")
                temporary = output.with_name(f"{output.stem}.compressed.tmp{output.suffix}")
                if output != source and output.exists():
                    raise FileExistsError(f"Compressed output already exists: {output.name}")
                video_bitrate = VIDEO_BITRATES_KBPS.get(target_height, 2500)
                if container == "webm":
                    codecs = ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4",
                              "-c:a", "libopus", "-b:a", "96k"]
                    movflags: list[str] = []
                else:
                    codecs = ["-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-b:a", "96k"]
                    movflags = ["-movflags", "+faststart"] if container == "mp4" else []
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                           "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale=-2:min(ih\\,{target_height})",
                           "-b:v", f"{video_bitrate}k", *codecs, *movflags, str(temporary)]
                try:
                    subprocess.run(command, check=True, capture_output=True, text=True)
                    os.replace(temporary, output)
                    if output != source:
                        source.unlink(missing_ok=True)
                    job.output_path = str(output)
                except subprocess.CalledProcessError as exc:
                    temporary.unlink(missing_ok=True)
                    detail = (exc.stderr or "").strip()[-500:]
                    raise RuntimeError(f"FFmpeg compression failed: {detail or 'unsupported input or codec'}") from exc
            self.events.put(("job_done", (job_id, "Completed", "Download complete")))
        except DownloadCancelled:
            self.events.put(("job_done", (job_id, "Cancelled", "Download cancelled")))
        except Exception as exc:
            logging.exception("Download failed")
            self.events.put(("job_failed", (job_id, self._friendly_error(exc))))
        finally:
            self.events.put(("worker_released", job_id))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "analysis_ok":
                    self._show_analysis(*data)
                elif kind == "analysis_error":
                    self.media_summary.config(text=data, fg=ERROR)
                    self._set_info(data)
                    self.download_btn.config(state="normal", text="Download Video")
                elif kind == "thumbnail":
                    try:
                        self._thumb_ref = tk.PhotoImage(data=data)
                        self.thumb_label.config(image=self._thumb_ref, text="")
                    except tk.TclError:
                        pass
                elif kind == "progress":
                    job_id, percent, speed, eta, size = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.percent, job.speed, job.eta, job.size = percent, speed, eta, size
                        self._update_job(job)
                        self.progress.configure(value=percent)
                        self.progress_label.config(text=f"{job.title} · {percent:.1f}% · {speed} · {size} · ETA {eta}")
                elif kind == "job_state":
                    job_id, state, note = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.state = state
                        self.progress_label.config(text=f"{job.title} · {note}")
                        self._update_job(job)
                elif kind == "job_done":
                    job_id, state, note = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.state = state
                        job.percent = 100 if state == "Completed" else job.percent
                        if state == "Completed":
                            self._record_history(job)
                        self.progress.configure(value=job.percent)
                        self.progress_label.config(text=f"{job.title} · {note}", fg=SUCCESS if state == "Completed" else MUTED)
                        self._update_job(job)
                        if state == "Completed" and self.settings.get("open_folder"):
                            self.root.after(50, self.open_download_folder)
                elif kind == "job_failed":
                    job_id, error = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.state, job.error = "Failed", error
                        self.progress_label.config(text=f"{job.title} · {error}", fg=ERROR)
                        self._update_job(job)
                elif kind == "worker_released":
                    self.active_count = max(0, self.active_count - 1)
                    self._start_waiting()
                elif kind == "batch_ok":
                    job_id, info = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.info = info
                        job.title = safe_filename(info.get("title") or "Video")
                        job.format_value, _ = self._format_for_info(info)
                        choice = self.settings.get("quality", "best")
                        if choice == "worst":
                            target_label = "Lowest available quality"
                        elif choice == "best":
                            target_label = "Best available quality"
                        else:
                            try:
                                target = int(choice)
                            except (TypeError, ValueError):
                                target = 2160
                            tier = next(((h, name) for h, name in QUALITY_TIERS if h >= target), QUALITY_TIERS[-1])
                            target_label = f"{tier[1]} ·"
                        job.info = dict(info)
                        job.info["target_height"] = self._height_for_selection(target_label, available_video_formats(info))
                        job.state = "Queued"
                        self.tree.item(job_id, values=(job.title, "Queued", "Default quality", "0%", "—", "—"))
                        self.waiting.put(job_id)
                        self._start_waiting()
                elif kind == "batch_error":
                    job_id, error = data
                    job = self.jobs.get(job_id)
                    if job:
                        job.state, job.error = "Failed", error
                        self._update_job(job)
                self._update_queue_count()
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(100, self._drain_events)

    def _format_for_info(self, info: dict[str, Any]) -> tuple[str, str]:
        formats = available_video_formats(info)
        if not formats:
            return "bestvideo+bestaudio/best", "Best available quality"
        choice = self.settings.get("quality", "best")
        if choice == "worst":
            fmt = formats[0]
        elif choice == "best":
            fmt = formats[-1]
        else:
            try:
                cap = int(choice)
                options = [f for f in formats if int(f.get("height") or 0) <= cap]
                fmt = max(options, key=fmt_quality) if options else formats[0]
            except (ValueError, TypeError):
                fmt = formats[-1]
        if fmt.get("acodec") in (None, "none"):
            return f"{fmt.get('format_id')}+bestaudio/best", self._format_title(fmt)
        return str(fmt.get("format_id")), self._format_title(fmt)

    def _update_job(self, job: Job) -> None:
        if not job.job_id or not self.tree.exists(job.job_id):
            return
        quality = (job.info or {}).get("selected_quality") or (job.info or {}).get("format") or "Selected video"
        self.tree.item(job.job_id, values=(job.title, job.state, str(quality), f"{job.percent:.1f}%",
                                           job.speed or "—", job.eta or "—"))

    def _update_queue_count(self) -> None:
        active = sum(job.state in {"Queued", "Analyzing", "Downloading", "Processing"} for job in self.jobs.values())
        self.queue_count_label.config(text=f"{len(self.jobs)} jobs · {active} active")

    @staticmethod
    def _load_history() -> list[dict[str, Any]]:
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return data[:200] if isinstance(data, list) else []
        except (OSError, ValueError, TypeError):
            return []

    def _record_history(self, job: Job) -> None:
        # Keep titles and local file location only; source URLs can contain private query tokens.
        record = {"title": job.title, "state": "Completed", "when": datetime.now().astimezone().isoformat(timespec="minutes"),
                  "folder": job.destination or self.settings["directory"]}
        self.history.insert(0, record)
        self.history = self.history[:200]
        try:
            temp = HISTORY_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(HISTORY_FILE)
        except OSError:
            logging.exception("Could not save download history")

    def open_history(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Download history")
        win.geometry("620x470")
        win.configure(bg=BG)
        tk.Label(win, text="Completed downloads", bg=BG, fg=TEXT,
                 font=("TkDefaultFont", 15, "bold")).pack(anchor="w", padx=18, pady=16)
        listbox = tk.Listbox(win, bg=PANEL, fg=TEXT, selectbackground="#173a5a", relief="flat",
                             font=("TkDefaultFont", 10), height=15)
        listbox.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        if self.history:
            for row in self.history:
                listbox.insert("end", f"{row.get('when', '')}   ·   {row.get('title', 'Video')}")
        else:
            listbox.insert("end", "No completed downloads have been recorded yet.")
        buttons = tk.Frame(win, bg=BG)
        buttons.pack(fill="x", padx=18, pady=(0, 16))
        def open_folder() -> None:
            selected = listbox.curselection()
            if not selected or selected[0] >= len(self.history):
                return
            folder = self.history[selected[0]].get("folder")
            if folder:
                self.dest_var.set(str(folder))
                self.open_download_folder()
        self._button(buttons, "Open selected folder", open_folder).pack(side="left")
        def clear() -> None:
            if messagebox.askyesno("Clear history", "Clear the saved list of completed downloads?", parent=win):
                self.history.clear()
                try:
                    HISTORY_FILE.unlink(missing_ok=True)
                except OSError:
                    logging.exception("Could not clear download history")
                win.destroy()
        self._button(buttons, "Clear history", clear).pack(side="left", padx=8)
        self._button(buttons, "Close", win.destroy).pack(side="right")

    def on_select_job(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        self.selected_job = selection[0] if selection else None

    def cancel_selected(self) -> None:
        job = self.jobs.get(self.selected_job or "")
        if not job:
            return
        if job.state == "Queued":
            job.state = "Cancelled"
            job.cancel.set()
            self._update_job(job)
        elif job.state == "Downloading":
            job.cancel.set()
            job.state = "Cancelling"
            self._update_job(job)
        elif job.state == "Processing":
            messagebox.showinfo(APP_NAME, "The media is being merged. Cancellation is not reliable during this final processing step.", parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, "Only queued or active downloads can be cancelled.", parent=self.root)

    def retry_selected(self) -> None:
        job = self.jobs.get(self.selected_job or "")
        if not job or job.state != "Failed":
            messagebox.showinfo(APP_NAME, "Select a failed job to retry.", parent=self.root)
            return
        job.state, job.percent, job.error = "Queued", 0.0, ""
        job.cancel = threading.Event()
        self.waiting.put(job.job_id)
        self._update_job(job)
        self._start_waiting()

    def remove_selected(self) -> None:
        job_id = self.selected_job
        job = self.jobs.get(job_id or "")
        if not job:
            return
        if job.state in {"Downloading", "Processing", "Cancelling"}:
            messagebox.showinfo(APP_NAME, "Cancel the active download before removing it.", parent=self.root)
            return
        self.jobs.pop(job.job_id, None)
        if self.tree.exists(job.job_id):
            self.tree.delete(job.job_id)
        self.selected_job = None

    def clear_finished(self) -> None:
        for job_id, job in list(self.jobs.items()):
            if job.state in {"Completed", "Failed", "Cancelled"}:
                self.jobs.pop(job_id, None)
                if self.tree.exists(job_id):
                    self.tree.delete(job_id)
        self.selected_job = None
        self._update_queue_count()

    def open_download_folder(self) -> None:
        folder = Path(self.dest_var.get() or self.settings["directory"]).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)], close_fds=True)
            else:
                opener = shutil.which("xdg-open")
                if opener:
                    subprocess.Popen([opener, str(folder)], close_fds=True)
                else:
                    raise OSError("xdg-open is not available")
        except OSError as exc:
            messagebox.showerror("Open folder", str(exc), parent=self.root)

    def open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Luxv Tubers Settings")
        win.geometry("540x500")
        win.resizable(False, False)
        win.configure(bg=BG)
        tk.Label(win, text="Download and performance settings", bg=BG, fg=TEXT,
                 font=("TkDefaultFont", 15, "bold")).pack(anchor="w", padx=20, pady=(18, 14))
        form = tk.Frame(win, bg=BG)
        form.pack(fill="both", expand=True, padx=20)
        variables: dict[str, tk.Variable] = {}
        def row(label: str, key: str, choices: tuple[str, ...] | None = None) -> None:
            r = tk.Frame(form, bg=BG)
            r.pack(fill="x", pady=6)
            tk.Label(r, text=label, bg=BG, fg=MUTED, width=25, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(self.settings[key]))
            variables[key] = var
            if choices:
                ttk.Combobox(r, textvariable=var, values=choices, state="readonly", width=22).pack(side="right")
            else:
                tk.Entry(r, textvariable=var, bg=PANEL, fg=TEXT, insertbackground=CYAN,
                         relief="flat", width=25).pack(side="right", ipady=5)
        row("Default download directory", "directory")
        browse = tk.Button(form, text="Browse for folder…", command=lambda: self._settings_browse(variables),
                           bg=CARD, fg=TEXT, relief="flat", padx=10, pady=6)
        browse.pack(anchor="e", pady=(0, 3))
        row("Default quality", "quality", ("best", "worst", "240", "360", "480", "720", "1080", "1440", "2160"))
        row("Preferred container", "container", ("mp4", "mkv", "webm"))
        row("Maximum simultaneous downloads (1–4)", "max_concurrent")
        row("Fragment concurrency (1–16)", "fragment_concurrency")
        row("Retry count", "retries")
        row("Connection timeout (seconds)", "timeout")
        for label, key in (("Open destination after download", "open_folder"), ("Avoid overwriting existing files", "confirm_overwrite")):
            var = tk.BooleanVar(value=bool(self.settings[key]))
            variables[key] = var
            tk.Checkbutton(form, text=label, variable=var, bg=BG, fg=TEXT, selectcolor=PANEL,
                           activebackground=BG, activeforeground=TEXT).pack(anchor="w", pady=3)
        buttons = tk.Frame(win, bg=BG)
        buttons.pack(fill="x", padx=20, pady=17)
        def save() -> None:
            try:
                candidate = self.settings.copy()
                candidate["directory"] = str(Path(str(variables["directory"].get())).expanduser())
                candidate["quality"] = str(variables["quality"].get())
                candidate["container"] = str(variables["container"].get())
                for key, lo, hi in (("max_concurrent", 1, 4), ("fragment_concurrency", 1, 16), ("retries", 0, 30), ("timeout", 5, 180)):
                    candidate[key] = max(lo, min(hi, int(str(variables[key].get()))))
                candidate["open_folder"] = bool(variables["open_folder"].get())
                candidate["confirm_overwrite"] = bool(variables["confirm_overwrite"].get())
                self.settings = candidate
                save_settings(candidate)
                self.dest_var.set(candidate["directory"])
                self.container_var.set(candidate["container"])
                win.destroy()
            except (ValueError, OSError) as exc:
                messagebox.showerror("Invalid settings", str(exc), parent=win)
        self._button(buttons, "Cancel", win.destroy).pack(side="right", padx=(7, 0))
        self._button(buttons, "Save settings", save, primary=True).pack(side="right")

    def _settings_browse(self, variables: dict[str, tk.Variable]) -> None:
        chosen = filedialog.askdirectory(initialdir=str(variables["directory"].get()), parent=self.root)
        if chosen:
            variables["directory"].set(chosen)

    def open_about(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("About Luxv Tubers")
        win.configure(bg=BG)
        win.geometry("560x440")
        win.resizable(False, False)
        text = ("Luxv Tubers · LuxvK Labs\nYour Videos. Your Quality. Your Speed.\n\n"
                f"Version {VERSION}\n\n"
                "Install yt-dlp (required):\n"
                "  Kali/Debian: sudo apt install yt-dlp python3-tk\n"
                "  Windows:     py -m pip install yt-dlp\n"
                "  macOS:       python3 -m pip install --user yt-dlp\n\n"
                "FFmpeg is required to resize and compress output to the selected resolution.\n"
                "Kali/Debian: sudo apt install ffmpeg\n"
                "Windows/macOS: install a trusted FFmpeg build for your platform.\n\n"
                "Optional thumbnail display: python3 -m pip install pillow\n\n"
                "The app does not install software, use cookies, bypass access controls,\n"
                "or send URLs/files to a Luxv server. Download only content you may save.\n\n"
                f"Log file: {LOG_FILE}")
        tk.Label(win, text=text, bg=BG, fg=TEXT, justify="left", anchor="nw",
                 font=("TkFixedFont", 10), padx=20, pady=20).pack(fill="both", expand=True)
        self._button(win, "Close", win.destroy, primary=True).pack(anchor="e", padx=20, pady=(0, 16))

    def on_close(self) -> None:
        active = [job for job in self.jobs.values() if job.state in {"Downloading", "Processing", "Cancelling"}]
        if active and not messagebox.askyesno(APP_NAME, "Downloads are active. Cancel them and close?", parent=self.root):
            return
        self._closing = True
        for job in active:
            job.cancel.set()
        try:
            self.settings["directory"] = str(Path(self.dest_var.get()).expanduser())
            self.settings["container"] = self.container_var.get()
            save_settings(self.settings)
        except OSError:
            logging.exception("Could not save preferences during shutdown")
        self.root.after(250, self.root.destroy)


class _YtdlpLogger:
    """Send yt-dlp diagnostics to the user log without echoing URLs in the GUI."""
    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            logging.debug("yt-dlp %s", msg[:500])
    def warning(self, msg: str) -> None:
        logging.warning("yt-dlp %s", str(msg)[:500])
    def error(self, msg: str) -> None:
        logging.error("yt-dlp %s", str(msg)[:500])


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not start the Tk interface: {exc}", file=sys.stderr)
        print("Install Tkinter for your Python distribution and try again.", file=sys.stderr)
        return 1
    LuxvTubers(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
