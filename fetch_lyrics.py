import sys, csv, json, re, time, hashlib
from pathlib import Path
import requests
import syncedlyrics

CACHE = Path.home() / ".lyrics_overlay_cache"
CACHE.mkdir(exist_ok=True)
UA = {"User-Agent": "LyricsOverlay/0.1 (your-contact-here)"}

lrclib_fails = 0
LRCLIB_GIVE_UP = 3

def clean(title):
    t = re.sub(r"\s*[-–]\s*(remaster(ed)?|live|radio edit|single version).*$", "", title, flags=re.I)
    t = re.sub(r"\s*\((feat|with)\.?[^)]*\)", "", t, flags=re.I)
    return t.strip()

def key(artist, title):
    s = re.sub(r"\s+", " ", f"{artist} - {title}".lower().strip())
    return CACHE / (hashlib.md5(s.encode()).hexdigest() + ".json")

def lrclib(artist, title):
    """lrc string, '' for a real miss, None if unreachable."""
    global lrclib_fails
    if lrclib_fails >= LRCLIB_GIVE_UP:
        return None
    try:
        r = requests.get("https://lrclib.net/api/get",
                         params={"artist_name": artist, "track_name": title},
                         headers=UA, timeout=5)
        if r.status_code == 404:
            lrclib_fails = 0
            return ""
        r.raise_for_status()
        lrclib_fails = 0
        return r.json().get("syncedLyrics") or ""
    except requests.RequestException:
        lrclib_fails += 1
        return None

def lrclib_search(artist, title, duration=None):
    if lrclib_fails >= LRCLIB_GIVE_UP:
        return None
    try:
        r = requests.get("https://lrclib.net/api/search",
                         params={"track_name": title, "artist_name": artist},
                         headers=UA, timeout=5)
        r.raise_for_status()
        hits = [x for x in r.json() if x.get("syncedLyrics")]
        if duration:
            hits = [x for x in hits if abs((x.get("duration") or 0) - duration) <= 5]
        return hits[0]["syncedLyrics"] if hits else ""
    except requests.RequestException:
        return None

def netease(artist, title):
    try:
        return syncedlyrics.search(f"{artist} - {title}", synced_only=True,
                                   providers=["NetEase"]) or ""
    except Exception:
        return None

def plausible(lrc, duration):
    """Reject lyrics whose timing doesn't fit the track length."""
    if not duration:
        return True
    stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", lrc)
    if not stamps:
        return False
    last = max(int(m) * 60 + float(s) for m, s in stamps)
    return duration * 0.5 <= last <= duration + 5

def fetch(artist, title, duration=None):
    t = clean(title)
    lrc = lrclib(artist, t)
    if lrc == "":
        lrc = lrclib_search(artist, t, duration)
    if lrc:
        return lrc, "lrclib"
    n = netease(artist, t)
    if n and not plausible(n, duration):
        n = ""  # looks like the wrong song
    if n:
        return n, "netease"
    if lrc == "" and n == "":
        return "", "miss"
    return None, "error"

def read_songs(path):
    p = Path(path)
    if p.suffix.lower() == ".csv":  # Exportify format
        with p.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                artist = row["Artist Name(s)"].split(";")[0].strip()
                dur = float(row["Duration (ms)"]) / 1000 if row.get("Duration (ms)") else None
                yield artist, row["Track Name"].strip(), dur
    else:  # "Artist - Title" per line
        for line in p.read_text(encoding="utf-8").splitlines():
            if " - " in line:
                a, t = line.split(" - ", 1)
                yield a.strip(), t.strip(), None

def main(path):
    songs = list(read_songs(path))
    stats = {"lrclib": 0, "netease": 0, "miss": 0, "error": 0, "cached": 0}
    for i, (artist, title, dur) in enumerate(songs, 1):
        f = key(artist, clean(title))
        if f.exists():
            stats["cached"] += 1
            continue
        lrc, src = fetch(artist, title, dur)
        stats[src] += 1
        if src in ("lrclib", "netease"):
            f.write_text(json.dumps({"lrc": lrc}), encoding="utf-8")
        elif src == "miss":
            f.write_text(json.dumps({"lrc": None, "missed_at": time.time()}), encoding="utf-8")
        print(f"[{i}/{len(songs)}] {src:8} {artist} - {title}")
        time.sleep(0.3)
    print("\nDone:", stats)

if __name__ == "__main__":
    main(sys.argv[1])