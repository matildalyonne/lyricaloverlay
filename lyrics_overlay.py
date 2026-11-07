"""Spotify synced-lyrics overlay (Windows, PyQt6).

Ctrl+Alt+L  toggle edit mode (drag to move, drag edges to resize, colour/size/opacity).
            Locked = fully click-through, display only.
Ctrl+Alt+Q  quit (or use the tray icon menu)

Runs in the background: the overlay appears when Spotify starts playing and hides
after AUTO_HIDE_SECONDS without playback. Launching it again just brings it up.

pip install PyQt6 spotipy pynput requests syncedlyrics
pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
"""
import bisect
import ctypes
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import spotipy
import syncedlyrics
from pynput import keyboard
from PyQt6.QtCore import QObject, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QCheckBox, QLabel, QMenu, QPushButton,
                             QSlider, QSystemTrayIcon, QWidget)
from spotipy.oauth2 import SpotifyOAuth

# ---------------- config ----------------
CLIENT_ID = ("YOUR_CLIENT_ID")
CLIENT_SECRET = ("YOUR_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"

HOTKEY_EDIT = "<ctrl>+<alt>+l"
HOTKEY_QUIT = "<ctrl>+<alt>+q"
LYRIC_OFFSET = 0.0        # seconds; positive = lyrics show earlier, negative = later
AUTO_LOCK_SECONDS = 10    # edit mode locks itself after this long without interaction
FONT_FAMILY = "Segoe UI"
BG_ALPHA = 130            # 0-255 background opacity when locked
CONFIG = Path(__file__).with_name("overlay_config.json")
TIMEOUT = (10, 25)        # (connect, read) seconds for LRCLIB requests
CACHE_DIR = Path.home() / ".lyrics_overlay_cache"   # same folder fetch_lyrics.py fills
MISS_RETRY_DAYS = 7       # retry "no lyrics found" results after this long
LRCLIB_COOLDOWN = 300     # seconds to skip LRCLIB after it fails
PREFETCH_COUNT = 8        # upcoming queue tracks to cache ahead of time
PREFETCH_EVERY = 30       # seconds between queue checks
AUTO_HIDE_SECONDS = 15    # overlay hides after this long with no playback
SCROLL_SMOOTHING = 0.12   # seconds; smaller = snappier line slide
DEFAULT_COLOR = "#ffffff"
SWATCHES = ["#ffffff", "#ffe066", "#66e0ff", "#7dffa0", "#ff8fd0", "#ffb066", "#111111"]
SERVER_NAME = "spotify-lyrics-overlay"   # lets a 2nd launch find the running copy
ACTIVE = threading.Event()   # set while the overlay is up; pauses queue prefetch otherwise

TEXT_FLAGS = Qt.AlignmentFlag.AlignHCenter.value | Qt.TextFlag.TextWordWrap.value
TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

E = Qt.Edge
CURSORS = {
    E.LeftEdge: Qt.CursorShape.SizeHorCursor,
    E.RightEdge: Qt.CursorShape.SizeHorCursor,
    E.TopEdge: Qt.CursorShape.SizeVerCursor,
    E.BottomEdge: Qt.CursorShape.SizeVerCursor,
    E.LeftEdge | E.TopEdge: Qt.CursorShape.SizeFDiagCursor,
    E.RightEdge | E.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
    E.RightEdge | E.TopEdge: Qt.CursorShape.SizeBDiagCursor,
    E.LeftEdge | E.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
}


# ---------------- lyrics ----------------
def parse_lrc(text):
    out = []
    for line in text.splitlines():
        stamps = TS.findall(line)
        if not stamps:
            continue
        lyric = TS.sub("", line).strip()
        for m, s in stamps:
            out.append((int(m) * 60 + float(s), lyric))
    out.sort(key=lambda x: x[0])
    return out


def clean(title):
    """Strip remaster/live/feat tags so cache keys and searches match more often."""
    t = re.sub(r"\s*[-\u2013]\s*(remaster(ed)?|live|radio edit|single version).*$", "", title, flags=re.I)
    t = re.sub(r"\s*\((feat|with)\.?[^)]*\)", "", t, flags=re.I)
    return t.strip()


def cache_file(artist, title):
    s = re.sub(r"\s+", " ", f"{artist} - {clean(title)}".lower().strip())
    return CACHE_DIR / (hashlib.md5(s.encode()).hexdigest() + ".json")


def cache_read(artist, title):
    """LRC text if cached, '' for a recent confirmed miss, None if unknown."""
    primary = re.split(r"\s*[;,&]\s*", artist)[0]   # local players may list several artists
    miss = False
    for a in dict.fromkeys([artist, primary]):
        try:
            d = json.loads(cache_file(a, title).read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("lrc"):
            return d["lrc"]
        if time.time() - d.get("missed_at", 0) < MISS_RETRY_DAYS * 86400:
            miss = True
    return "" if miss else None


def cache_write(artist, title, lrc):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"lrc": lrc} if lrc else {"lrc": None, "missed_at": time.time()}
        cache_file(artist, title).write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        print("Cache write failed:", e)


def lrclib_lyrics(title, search_title, artist, album, duration):
    """Synced LRC text from LRCLIB, '' if it has none.
    Raises on network/server errors."""
    hdr = {"User-Agent": "spotify-lyrics-overlay/0.1"}
    r = requests.get(
        "https://lrclib.net/api/get", headers=hdr, timeout=TIMEOUT,
        params={"track_name": title, "artist_name": artist,
                "album_name": album, "duration": int(duration)},
    )
    if r.status_code >= 500:
        r.raise_for_status()
    data = r.json() if r.ok else None
    if not data or not data.get("syncedLyrics"):
        r = requests.get(
            "https://lrclib.net/api/search", headers=hdr, timeout=TIMEOUT,
            params={"track_name": search_title, "artist_name": artist},
        )
        r.raise_for_status()
        results = [d for d in r.json() if d.get("syncedLyrics")]
        data = min(results, key=lambda d: abs((d.get("duration") or 0) - duration),
                   default=None)
    return data["syncedLyrics"] if data else ""


def plausible(lrc, duration):
    """Reject lyrics whose timing doesn't fit the track length (wrong song)."""
    if not duration:
        return True
    stamps = TS.findall(lrc)
    if not stamps:
        return False
    last = max(int(m) * 60 + float(s) for m, s in stamps)
    return duration * 0.5 <= last <= duration + 5


_lrclib_skip_until = 0.0


def fetch_online(title, artist, album, duration):
    """LRCLIB, then NetEase. Returns LRC text, '' if both confirmed nothing,
    or None if a source couldn't be reached (so the result isn't trusted/cached)."""
    global _lrclib_skip_until
    t = clean(title)
    lrclib_ok = False
    if time.time() >= _lrclib_skip_until:
        try:
            lrc = lrclib_lyrics(title, t, artist, album, duration)
            if lrc:
                return lrc
            lrclib_ok = True
        except Exception as e:
            print("LRCLIB error:", e)
            _lrclib_skip_until = time.time() + LRCLIB_COOLDOWN
    netease_ok = False
    try:
        n = syncedlyrics.search(f"{artist} - {t}", synced_only=True,
                                providers=["NetEase"]) or ""
        netease_ok = True
        if n and plausible(n, duration):
            return n
    except Exception as e:
        print("NetEase error:", e)
    return "" if (lrclib_ok and netease_ok) else None


def lookup(title, artist, album, duration):
    """cache -> LRCLIB -> NetEase. Same return values as fetch_online."""
    hit = cache_read(artist, title)
    if hit is not None:
        return hit
    lrc = fetch_online(title, artist, album, duration)
    if lrc is not None:
        cache_write(artist, title, lrc)
    return lrc


# ---------------- plumbing ----------------
class Bridge(QObject):
    playback = pyqtSignal(dict)
    lyrics = pyqtSignal(str, object)
    toggle_edit = pyqtSignal()
    quit_requested = pyqtSignal()


_sp = None
_sp_lock = threading.Lock()


def get_sp():
    """One shared Spotify client. The lock stops two threads opening two login prompts."""
    global _sp
    with _sp_lock:
        if _sp is None:
            sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI,
                scope="user-read-playback-state user-read-currently-playing",
                cache_path=str(CONFIG.with_name(".spotify_cache")),
                requests_timeout=15,
            ), requests_timeout=15, retries=2)
            sp.current_playback()   # triggers login/token refresh while the lock is held
            _sp = sp
        return _sp


class SmtcReader:
    """Reads what the Spotify desktop app is playing straight from Windows
    (System Media Transport Controls). Local only, works with no internet.
    Create it and use it from one thread only."""

    def __init__(self):
        import asyncio
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
        except ImportError:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
        self.Status = Status
        self.loop = asyncio.new_event_loop()
        self.mgr = self.loop.run_until_complete(Manager.request_async())

    def read(self):
        """Playback dict, {} if Spotify has no track loaded, None if no Spotify session."""
        return self.loop.run_until_complete(self._read())

    async def _read(self):
        sess = next((x for x in self.mgr.get_sessions()
                     if "spotify" in x.source_app_user_model_id.lower()), None)
        if sess is None:
            return None
        props = await sess.try_get_media_properties_async()
        if not props.title:
            return {}
        tl = sess.get_timeline_properties()
        playing = sess.get_playback_info().playback_status == self.Status.PLAYING
        dur = (tl.end_time - tl.start_time).total_seconds()
        pos = tl.position.total_seconds()
        if playing:   # Windows reports a snapshot; add the time since it was taken
            lu = tl.last_updated_time
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            pos += (datetime.now(timezone.utc) - lu).total_seconds()
        return {
            "id": f"{props.artist} - {props.title}",
            "title": props.title,
            "artist": props.artist,
            "album": props.album_title,
            "duration": dur,
            "progress": max(0.0, min(pos, dur)) if dur else max(0.0, pos),
            "playing": playing,
        }


def web_read(sp):
    pb = sp.current_playback()
    item = pb and pb.get("item")
    if item and pb.get("currently_playing_type") == "track":
        return {
            "id": item.get("id") or item["name"],
            "title": item["name"],
            "artist": item["artists"][0]["name"],
            "album": item["album"]["name"],
            "duration": item["duration_ms"] / 1000,
            "progress": (pb.get("progress_ms") or 0) / 1000,
            "playing": pb["is_playing"],
        }
    return {}


def playback_loop(bridge):
    """Prefers the local Windows source (no internet). Falls back to the Spotify
    web API only when the desktop app isn't the one playing (e.g. phone)."""
    smtc = None
    try:
        smtc = SmtcReader()
    except Exception as e:
        print("Local tracking unavailable, using Spotify API only:", e)
    while True:
        try:
            d, local = None, False
            if smtc:
                try:
                    d = smtc.read()
                except Exception as e:
                    print("Local tracking error:", e)
            if d is None:
                d = web_read(get_sp())
            else:
                local = True
            bridge.playback.emit(d)
            # local reads are free; the web API is polled gently while idle
            time.sleep(1 if (local or d.get("playing")) else 5)
        except Exception as e:
            print("Spotify error:", e)
            time.sleep(3)


def prefetch_loop():
    """While online, cache lyrics for the next few queued tracks so they're
    already on disk if the connection drops."""
    seen = set()
    while True:
        ACTIVE.wait()   # nothing to prefetch for while the overlay is hidden
        try:
            q = get_sp().queue()
            for it in (q.get("queue") or [])[:PREFETCH_COUNT]:
                if not it or it.get("type") != "track" or it["id"] in seen:
                    continue
                res = lookup(it["name"], it["artists"][0]["name"],
                             it["album"]["name"], it["duration_ms"] / 1000)
                if res is not None:   # None = couldn't reach sources, retry next round
                    seen.add(it["id"])
        except Exception as e:
            print("Prefetch error:", e)
        time.sleep(PREFETCH_EVERY)


def set_click_through(widget, enable):
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = int(widget.winId())
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
    style = (style | WS_EX_TRANSPARENT) if enable else (style & ~WS_EX_TRANSPARENT)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


# ---------------- overlay ----------------
def fade(d):
    """Opacity (0-255) for a line d lines away from the active one."""
    pts = [(0, 255), (1, 150), (2, 80), (3, 0)]
    if d >= 3:
        return 0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if d <= x1:
            return y0 + (y1 - y0) * (d - x0) / (x1 - x0)
    return 0


class Overlay(QWidget):
    PAD = 20
    MARGIN = 8
    EDIT_BOTTOM = 68   # room reserved for the controls in edit mode

    def __init__(self, bridge):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setMinimumSize(380, 150)
        self.bg_alpha = BG_ALPHA
        self.show_title = False
        self.color = QColor(DEFAULT_COLOR)
        self.lyric_scale = 1.0
        self.title_scale = 1.0
        self.load_geometry()

        self.edit = False
        self.track_id = None
        self.title = self.artist = ""
        self.lyrics = None      # None = loading, [] = not found, False = unreachable
        self.times = []
        self.display = []       # ["♪", line 1, line 2, ...]; the index is the active line
        self.playing = False
        self.base_pos = 0.0
        self.base_t = time.monotonic()
        self.scroll = 0.0       # animated position within self.display
        self._anim_t = time.monotonic()
        self.last_active = time.monotonic()
        self._last = None

        self.bridge = bridge
        bridge.playback.connect(self.on_playback)
        bridge.lyrics.connect(self.on_lyrics)
        bridge.toggle_edit.connect(lambda: self.set_edit(not self.edit))

        self.tick_timer = QTimer(self)      # runs only while the overlay is visible
        self.tick_timer.setInterval(16)
        self.tick_timer.timeout.connect(self.tick)

        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(AUTO_LOCK_SECONDS * 1000)
        self.idle_timer.timeout.connect(lambda: self.set_edit(False))

        # controls, only visible in edit mode
        style = "color: #9bbcff;"
        self.chk = QCheckBox("Title/artist", self)
        self.chk.setChecked(self.show_title)
        self.chk.setStyleSheet(style)
        self.chk.toggled.connect(self.on_title_toggled)

        self.lbl_color = QLabel("Color", self)
        self.swatches = []
        for hexcol in SWATCHES:
            b = QPushButton(self)
            b.setFixedSize(20, 20)
            b.setStyleSheet(f"background: {hexcol}; border: 1px solid #9bbcff; border-radius: 10px;")
            b.clicked.connect(lambda _=False, c=hexcol: self.set_color(c))
            self.swatches.append(b)

        def make_slider(label, lo, hi, value, slot):
            lbl = QLabel(label, self)
            sl = QSlider(Qt.Orientation.Horizontal, self)
            sl.setRange(lo, hi)
            sl.setValue(value)
            sl.valueChanged.connect(slot)
            return lbl, sl

        self.lbl_op, self.slider_op = make_slider("Opacity", 0, 255, self.bg_alpha, self.on_alpha_changed)
        self.lbl_ly, self.slider_ly = make_slider("Lyrics", 50, 250, round(self.lyric_scale * 100), self.on_lyric_scale)
        self.lbl_ti, self.slider_ti = make_slider("Title", 50, 250, round(self.title_scale * 100), self.on_title_scale)
        self.pairs = [(self.lbl_op, self.slider_op), (self.lbl_ly, self.slider_ly), (self.lbl_ti, self.slider_ti)]

        self.controls = [self.chk, self.lbl_color, *self.swatches]
        for lbl, sl in self.pairs:
            self.controls += [lbl, sl]
        for c in self.controls:
            if isinstance(c, QLabel):
                c.setStyleSheet(style)
            c.setCursor(Qt.CursorShape.ArrowCursor)
            c.hide()
        self.layout_controls()

    # ----- geometry persistence -----
    def load_geometry(self):
        try:
            g = json.loads(CONFIG.read_text())
            self.setGeometry(g["x"], g["y"], g["w"], g["h"])
            self.bg_alpha = g.get("alpha", BG_ALPHA)
            self.show_title = g.get("show_title", False)
            c = QColor(g.get("color", DEFAULT_COLOR))
            self.color = c if c.isValid() else QColor(DEFAULT_COLOR)
            self.lyric_scale = float(g.get("lyric_scale", 1.0))
            self.title_scale = float(g.get("title_scale", 1.0))
        except Exception:
            s = QApplication.primaryScreen().availableGeometry()
            w, h = 800, 200
            self.setGeometry(s.x() + (s.width() - w) // 2, s.bottom() - h - 60, w, h)

    def save_settings(self):
        g = self.geometry()
        try:
            CONFIG.write_text(json.dumps({
                "x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
                "alpha": self.bg_alpha, "show_title": self.show_title,
                "color": self.color.name(), "lyric_scale": self.lyric_scale,
                "title_scale": self.title_scale,
            }))
        except Exception as e:
            print("Couldn't save settings:", e)

    # ----- show / hide -----
    def show_overlay(self):
        if not self.isVisible():
            self.show()
            self.scroll = float(self.target_index())   # no slide on pop-up
        self._anim_t = time.monotonic()
        self.apply_click_through()
        self.tick_timer.start()
        ACTIVE.set()

    def hide_overlay(self):
        self.tick_timer.stop()
        self.hide()
        ACTIVE.clear()

    def pop_up(self):
        """Shortcut clicked / tray 'Show': show now, hide again after the usual idle time."""
        self.last_active = time.monotonic()
        self.show_overlay()

    # ----- modes -----
    def apply_click_through(self):
        set_click_through(self, not self.edit)

    def set_edit(self, on):
        self.edit = on
        self.last_active = time.monotonic()
        if on and not self.isVisible():
            self.show_overlay()
        self.apply_click_through()
        for c in self.controls:
            c.setVisible(on)
        if on:
            self.idle_timer.start()
        else:
            self.idle_timer.stop()
            self.unsetCursor()
            self.save_settings()
        self.update()

    def on_title_toggled(self, checked):
        self.show_title = checked
        self.idle_timer.start()
        self.update()

    def on_alpha_changed(self, value):
        self.bg_alpha = value
        self.idle_timer.start()
        self.update()

    def on_lyric_scale(self, value):
        self.lyric_scale = value / 100
        self.idle_timer.start()
        self.update()

    def on_title_scale(self, value):
        self.title_scale = value / 100
        self.idle_timer.start()
        self.update()

    def set_color(self, hexcol):
        self.color = QColor(hexcol)
        self.idle_timer.start()
        self.update()

    def layout_controls(self):
        W, H, pad, h = self.width(), self.height(), self.PAD, 24
        y1, y2 = H - 60, H - 32
        x = pad
        cw = self.chk.sizeHint().width()
        self.chk.setGeometry(x, y1, cw, h)
        x += cw + 16
        lw = self.lbl_color.sizeHint().width()
        self.lbl_color.setGeometry(x, y1, lw, h)
        x += lw + 6
        for b in self.swatches:
            b.move(x, y1 + 2)
            x += 24
        gap = 14
        col = (W - 2 * pad - gap * (len(self.pairs) - 1)) // len(self.pairs)
        for k, (lbl, sl) in enumerate(self.pairs):
            cx = pad + k * (col + gap)
            lw = lbl.sizeHint().width()
            lbl.setGeometry(cx, y2, lw, h)
            sl.setGeometry(cx + lw + 6, y2, max(30, col - lw - 6), h)

    def resizeEvent(self, ev):
        self.layout_controls()
        super().resizeEvent(ev)

    # ----- data -----
    def on_playback(self, d):
        if d and d["playing"]:
            self.last_active = time.monotonic()
            if not self.isVisible():
                self.show_overlay()
        if not d:
            self.track_id, self.lyrics, self.times, self.display = None, None, [], []
            self.playing = False
            return
        self.base_pos = d["progress"]
        self.base_t = time.monotonic()
        self.playing = d["playing"]
        if d["id"] != self.track_id:
            self.track_id = d["id"]
            self.title, self.artist = d["title"], d["artist"]
            self.lyrics, self.times, self.display = None, [], []
            threading.Thread(target=self._fetch, args=(d,), daemon=True).start()

    def _fetch(self, d):
        lines = False  # False = couldn't reach any lyrics source
        for delay in (0, 3, 8, 15):
            time.sleep(delay)
            if self.track_id != d["id"]:
                return  # skipped to another track meanwhile
            try:
                lrc = lookup(d["title"], d["artist"], d["album"], d["duration"])
            except Exception as e:
                print("Lyrics error:", e)
                continue
            if lrc is not None:
                lines = parse_lrc(lrc) if lrc else []
                break
        self.bridge.lyrics.emit(d["id"], lines)

    def on_lyrics(self, track_id, lines):
        if track_id == self.track_id:
            self.lyrics = lines
            self.times = [t for t, _ in lines] if lines else []
            self.display = (["♪"] + [text or "♪" for _, text in lines]) if lines else []
            self.scroll = float(self.target_index())   # jump straight to the right line

    def position(self):
        if self.playing:
            return self.base_pos + time.monotonic() - self.base_t
        return self.base_pos

    def status_text(self):
        """Message to show instead of lyrics, or None when lyrics are ready."""
        if self.track_id is None:
            return "Nothing playing"
        if self.lyrics is None:
            return "Loading lyrics…"
        if self.lyrics is False:
            return "Couldn't reach lyrics sources (check connection)"
        if not self.lyrics:
            return "No synced lyrics found"
        return None

    def target_index(self):
        """Index in self.display of the line that should be active right now."""
        if not self.times:
            return 0
        return bisect.bisect_right(self.times, self.position() + LYRIC_OFFSET)

    def tick(self):
        now = time.monotonic()
        if not self.edit and now - self.last_active > AUTO_HIDE_SECONDS:
            self.hide_overlay()
            return
        dt = min(now - self._anim_t, 0.1)
        self._anim_t = now
        target = float(self.target_index())
        diff = target - self.scroll
        if abs(diff) > 4:                      # seek or new song: don't scroll through it
            self.scroll = target
        else:
            self.scroll += diff * (1 - math.exp(-dt / SCROLL_SMOOTHING))
            if abs(target - self.scroll) < 0.002:
                self.scroll = target
        sig = (self.status_text(), round(self.scroll, 3), len(self.display), self.track_id)
        if sig != self._last:
            self._last = sig
            self.update()

    # ----- mouse (only ever fires in edit mode) -----
    def edges_at(self, p):
        m, e = self.MARGIN, Qt.Edge(0)
        if p.x() <= m:
            e |= Qt.Edge.LeftEdge
        elif p.x() >= self.width() - m:
            e |= Qt.Edge.RightEdge
        if p.y() <= m:
            e |= Qt.Edge.TopEdge
        elif p.y() >= self.height() - m:
            e |= Qt.Edge.BottomEdge
        return e

    def mouseMoveEvent(self, ev):
        if not self.edit:
            return
        self.idle_timer.start()
        edges = self.edges_at(ev.position().toPoint())
        self.setCursor(CURSORS.get(edges, Qt.CursorShape.SizeAllCursor))

    def mousePressEvent(self, ev):
        if not self.edit or ev.button() != Qt.MouseButton.LeftButton:
            return
        self.idle_timer.start()
        edges = self.edges_at(ev.position().toPoint())
        handle = self.windowHandle()
        if edges:
            handle.startSystemResize(edges)
        else:
            handle.startSystemMove()

    # ----- drawing -----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)

        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.edit:
            p.setBrush(QColor(20, 24, 40, 200))
            p.setPen(QPen(QColor(90, 160, 255), 2))
        else:
            p.setBrush(QColor(0, 0, 0, self.bg_alpha))
            p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 14, 14)

        w = self.width() - 2 * self.PAD
        base = max(12, int(self.height() / 8 * self.lyric_scale))   # scales with window height
        top = 24 if self.edit else 4
        bottom = self.height() - (self.EDIT_BOTTOM if self.edit else 4)
        # dark text gets a light shadow, light text a dark one
        shadow = QColor(0, 0, 0) if self.color.lightness() > 110 else QColor(255, 255, 255)

        def put(text, font, y, h, alpha):
            r = QRect(self.PAD, int(y), w, h)
            p.setFont(font)
            sc = QColor(shadow)
            sc.setAlpha(int(alpha))
            p.setPen(sc)
            p.drawText(r.translated(1, 1), TEXT_FLAGS, text)
            c = QColor(self.color)
            c.setAlpha(int(alpha))
            p.setPen(c)
            p.drawText(r, TEXT_FLAGS, text)

        def height_of(font, text):
            return QFontMetrics(font).boundingRect(QRect(0, 0, w, 10000), TEXT_FLAGS, text).height()

        if self.show_title and self.track_id is not None:
            tf = QFont(FONT_FAMILY)
            tf.setPixelSize(max(11, int(self.height() / 8 * 0.5 * self.title_scale)))
            fm = QFontMetrics(tf)
            label = fm.elidedText(f"{self.title} \u2014 {self.artist}",
                                  Qt.TextElideMode.ElideRight, w)
            put(label, tf, top, fm.height(), 220)
            top += fm.height() + 6

        status = self.status_text()
        if status is not None:
            f = QFont(FONT_FAMILY)
            f.setPixelSize(base)
            f.setBold(True)
            h = height_of(f, status)
            put(status, f, top + (bottom - top - h) / 2, h, 255)
        else:
            p.setClipRect(QRect(0, int(top), self.width(), max(0, int(bottom - top))))
            s = min(self.scroll, len(self.display) - 1)
            a = int(s)
            frac = s - a
            gap = base * 0.5
            items = []   # (index, text, font, height, alpha)
            for i in range(a - 3, a + 5):
                if not 0 <= i < len(self.display):
                    continue
                dist = abs(i - s)
                f = QFont(FONT_FAMILY)
                f.setPixelSize(max(8, round(base * (1 - 0.3 * min(dist, 1)))))
                f.setBold(dist < 0.5)
                text = self.display[i]
                items.append((i, text, f, height_of(f, text), fade(dist)))
            # stack the lines, then slide the stack so the point between the
            # current and next line (by frac) sits in the middle of the area
            y, tops = 0.0, {}
            for i, _t, _f, h, _a in items:
                tops[i] = (y, h)
                y += h + gap
            centre = lambda i: tops[i][0] + tops[i][1] / 2
            focus = centre(a) if a + 1 not in tops else centre(a) * (1 - frac) + centre(a + 1) * frac
            shift = (top + bottom) / 2 - focus
            for i, text, f, h, al in items:
                if al > 0:
                    put(text, f, tops[i][0] + shift, h, al)
            p.setClipping(False)

        if self.edit:
            p.setFont(QFont(FONT_FAMILY, 8))
            p.setPen(QColor(150, 190, 255))
            p.drawText(self.PAD, 16, "EDIT MODE: drag to move, drag edges to resize, Ctrl+Alt+L to lock")


# ---------------- app ----------------
def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(30, 215, 96))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    f = QFont(FONT_FAMILY)
    f.setPixelSize(38)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(0, 0, 0))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


def signal_running_instance():
    """If another copy is already running, tell it to show itself. True if it did."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main():
    if sys.stdout is None or sys.stderr is None:   # pythonw has no console: log to a file
        log = open(Path(__file__).with_name("overlay.log"), "w", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # hiding the overlay must not end the program
    if signal_running_instance():
        return

    bridge = Bridge()
    overlay = Overlay(bridge)              # starts hidden; appears when playback starts
    bridge.quit_requested.connect(app.quit)
    app.aboutToQuit.connect(overlay.save_settings)

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), overlay.pop_up()))

    app.setWindowIcon(make_icon())
    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("Spotify lyrics overlay")
    menu = QMenu()
    menu.addAction("Show overlay").triggered.connect(overlay.pop_up)
    menu.addAction("Edit mode").triggered.connect(lambda: overlay.set_edit(True))
    menu.addAction("Quit").triggered.connect(bridge.quit_requested.emit)
    tray.setContextMenu(menu)
    tray.show()
    app.aboutToQuit.connect(tray.hide)

    threading.Thread(target=playback_loop, args=(bridge,), daemon=True).start()
    threading.Thread(target=prefetch_loop, daemon=True).start()
    hotkeys = keyboard.GlobalHotKeys({
        HOTKEY_EDIT: bridge.toggle_edit.emit,
        HOTKEY_QUIT: bridge.quit_requested.emit,
    })
    hotkeys.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()