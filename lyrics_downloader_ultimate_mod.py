import os
import re
import sys
import json
import time
import difflib
import threading
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
import queue
from pathlib import Path

# Fix Playwright driver path when frozen by PyInstaller
# Fix Playwright driver path when frozen by PyInstaller
if getattr(sys, 'frozen', False):
    import playwright._impl._driver as _pw_driver
    _pw_driver.compute_driver_executable = lambda: (
        Path(sys._MEIPASS) / "playwright" / "driver" / "node.exe",
        Path(sys._MEIPASS) / "playwright" / "driver" / "package" / "cli.js"
    )

# ------------------ Tag Recognition (Mutagen) ------------------
try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# ------------------ Metal Archives Provider ------------------
try:
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup
    MA_AVAILABLE = True
except ImportError:
    MA_AVAILABLE = False

MA_BASE = "https://www.metal-archives.com"

def get_tags(file_path):
    """Extracts Title, Artist, and Album from file tags."""
    if not MUTAGEN_AVAILABLE:
        return os.path.splitext(os.path.basename(file_path))[0], "Unknown Artist", "Unknown Album"
    try:
        if file_path.lower().endswith(".mp3"):
            try:
                audio = EasyID3(file_path)
            except Exception:
                audio = MutagenFile(file_path, easy=True)
        else:
            audio = MutagenFile(file_path, easy=True)

        title = audio.get('title', [None])[0] or os.path.splitext(os.path.basename(file_path))[0]
        artist = audio.get('artist', [None])[0] or "Unknown Artist"
        album = audio.get('album', [None])[0] or "Unknown Album"
        return title, artist, album
    except Exception:
        return os.path.splitext(os.path.basename(file_path))[0], "Unknown Artist", "Unknown Album"


def strip_parens(title):
    """Remove all (...) groups and surrounding whitespace from a title (in memory only)."""
    cleaned = re.sub(r'\s*\([^)]*\)', '', title)
    return cleaned.strip()


def is_lrc_synced(lrc_path):
    """Return True if the .lrc file at lrc_path contains synced timestamps like [mm:ss.xx]."""
    try:
        with open(lrc_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(1024)
        return bool(re.search(r'\[\d{2}:', content))
    except Exception:
        return False

# ------------------ Edge Browser Detection ------------------
def is_edge_installed():
    """Check if Microsoft Edge is installed on the system."""
    # Common install paths
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return True
    # Fallback: check registry
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Edge",
            0, winreg.KEY_READ
        )
        winreg.CloseKey(key)
        return True
    except Exception:
        pass

    return False

EDGE_AVAILABLE = is_edge_installed()
MA_AVAILABLE = MA_AVAILABLE and EDGE_AVAILABLE

class MetalArchivesSession:
    """
    Manages a single Playwright browser session for the duration of a download
    batch.  Call .start() before the loop and .stop() when done.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self.page = None
        # Cache: band_name (lower) -> { band_url, albums: [{title, url, year}] }
        self._band_cache = {}
        # Cache: album_url -> { title: track_id | None }
        self._album_cache = {}

    def start(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(channel="msedge", headless=True)
        context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        self.page = context.new_page()
        # Warm up session to avoid bot detection on first real request.
        # Metal Archives uses long-polling connections so "networkidle" never
        # fires — use "domcontentloaded" instead and treat timeout as non-fatal.
        try:
            self.page.goto(MA_BASE, timeout=20000,
                           wait_until="domcontentloaded")
        except Exception as e:
            print(f"[MA] Warmup navigation warning (non-fatal): {e}")
        time.sleep(2)

    def stop(self):
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self.page = None

    # ---------- Internal helpers ----------

    def _search_band(self, name):
        """
        Return a list of (band_url, found_name) for all results matching *name*.
        The caller is responsible for picking the right one.
        """
        import urllib.parse
        encoded = urllib.parse.quote(name)
        url = (
            f"{MA_BASE}/search/ajax-band-search/"
            f"?field=name&query={encoded}"
            f"&sEcho=1&iColumns=3&iDisplayStart=0&iDisplayLength=10"
        )
        print(f"[MA] Searching band: '{name}' -> {url}")
        self.page.goto(url)
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            data = json.loads(self.page.inner_text("body"))
        except Exception as e:
            print(f"[MA] ERROR: Could not parse band search JSON: {e}")
            return []
        rows = data.get("aaData", [])
        print(f"[MA] Band search returned {len(rows)} result(s):")
        candidates = []
        for i, row in enumerate(rows):
            m = re.search(r'href="([^"]+)".*?>([^<]+)<', row[0])
            if m:
                print(f"[MA]   [{i}] '{m.group(2)}' -> {m.group(1)}")
                candidates.append((m.group(1), m.group(2)))
            else:
                print(f"[MA]   [{i}] (unparseable cell) {row[0][:120]}")
        if not candidates:
            print(f"[MA] No bands found for query '{name}'.")
        return candidates

    def _get_discography(self, band_url):
        """Return list of {title, url, year} for all full-length / EP entries."""
        band_id = band_url.rstrip("/").split("/")[-1]
        url = f"{MA_BASE}/band/discography/id/{band_id}/tab/main"
        print(f"[MA] Fetching discography: {url}")
        self.page.goto(url)
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        soup = BeautifulSoup(self.page.content(), "html.parser")
        albums = []
        for row in soup.select("tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                a = cols[0].find("a")
                if a and "/albums/" in a.get("href", ""):
                    albums.append({
                        "title": a.text.strip(),
                        "url": a["href"],
                        "year": cols[1].text.strip(),
                    })
        print(f"[MA] Found {len(albums)} album(s) in discography:")
        for alb in albums:
            print(f"[MA]   '{alb['title']}' ({alb['year']}) -> {alb['url']}")
        if not albums:
            print(f"[MA] WARNING: Discography page returned no albums. "
                  f"Page may have failed to load or band has no releases.")
        return albums

    def _get_album_tracks(self, album_url):
        """Return list of (raw_title, track_id_or_None) as scraped from MA."""
        print(f"[MA] Fetching track list: {album_url}")
        self.page.goto(album_url)
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        soup = BeautifulSoup(self.page.content(), "html.parser")
        tracks = []
        for row in soup.select("tr.even, tr.odd"):
            cols = row.find_all("td")
            if len(cols) >= 4:
                title = cols[1].text.strip()
                btn = row.find("a", id=re.compile(r"lyricsButton\d+"))
                track_id = None
                if btn:
                    m = re.search(r"\d+", btn["id"])
                    if m:
                        track_id = m.group()
                tracks.append((title, track_id))
        print(f"[MA] Found {len(tracks)} track(s):")
        for t_title, t_id in tracks:
            id_str = t_id if t_id else "no lyrics button"
            print(f"[MA]   '{t_title}' (id={id_str})")
        if not tracks:
            print("[MA] WARNING: No tracks found — album page may have failed to load.")
        return tracks

    @staticmethod
    def _best_track_match(query, track_list, threshold=0.75):
        """
        Find the best-matching (raw_title, track_id) pair from *track_list*
        for the given *query* string.

        Matching strategy (each step strips parenthetical suffixes first):
          1. Exact match after lowercasing both sides.
          2. Best difflib ratio match above *threshold*.

        Returns (raw_title, track_id) or (None, None).
        """
        def normalise(s):
            # Strip (...) groups — same logic as the app-wide strip_parens()
            return re.sub(r'\s*\([^)]*\)', '', s).strip().lower()

        query_norm = normalise(query)
        print(f"[MA] Matching query '{query}' (normalised: '{query_norm}') "
              f"against {len(track_list)} track(s) (threshold={threshold}):")

        best_ratio = 0.0
        best_entry = (None, None)

        for raw_title, track_id in track_list:
            candidate_norm = normalise(raw_title)

            # Step 1: exact match on normalised strings
            if query_norm == candidate_norm:
                print(f"[MA]   EXACT match: '{raw_title}'")
                return raw_title, track_id

            # Step 2: similarity ratio
            ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
            print(f"[MA]   ratio={ratio:.3f}  '{raw_title}' (normalised: '{candidate_norm}')")
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = (raw_title, track_id)

        if best_ratio >= threshold:
            print(f"[MA]   FUZZY match (ratio={best_ratio:.3f}): '{best_entry[0]}'")
            return best_entry

        print(f"[MA]   NO match found (best ratio={best_ratio:.3f}, threshold={threshold}). "
              f"Consider lowering threshold if the correct track is listed above.")
        return None, None

    def _fetch_lyrics_by_id(self, track_id):
        """Fetch raw lyrics text for a track_id; return None if unavailable."""
        url = f"{MA_BASE}/release/ajax-view-lyrics/id/{track_id}"
        print(f"[MA] Fetching lyrics for track id={track_id}: {url}")
        self.page.goto(url)
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        text = self.page.inner_text("body").strip()
        if not text or "lyrics not available" in text.lower():
            print(f"[MA] Lyrics not available for id={track_id}.")
            return None
        print(f"[MA] Got lyrics ({len(text)} chars).")
        return text

    # ---------- Public fetch method ----------

    def fetch_lyrics(self, artist, album, title):
        """
        Search Metal Archives for *artist*, navigate to the album that best
        matches *album*, and return the plain-text lyrics for *title*.
        When multiple bands share the same name, the correct one is identified
        by checking which band's discography contains an album matching *album*.
        Returns the lyrics string, or None if not found.
        """
        print(f"\n[MA] ===== fetch_lyrics | artist='{artist}' | album='{album}' | title='{title}' =====")
        if not self.page:
            print("[MA] ERROR: No active browser page.")
            return None

        # --- Band lookup (cached per artist name) ---
        # Cache stores a list of confirmed (band_url, found_name, discography)
        # dicts — one per distinct band that matched the search query.
        cache_key = artist.lower()
        if cache_key in self._band_cache:
            print(f"[MA] Band '{artist}' found in cache "
                  f"({len(self._band_cache[cache_key])} candidate(s)).")
        else:
            candidates = self._search_band(artist)
            if not candidates:
                print(f"[MA] Band '{artist}' not found on Metal Archives.")
                self._band_cache[cache_key] = []
                return None
            # Fetch discography for every candidate up front so we can compare
            entries = []
            for band_url, found_name in candidates:
                discography = self._get_discography(band_url)
                entries.append({
                    "band_url": band_url,
                    "found_name": found_name,
                    "albums": discography,
                })
            self._band_cache[cache_key] = entries

        all_band_entries = self._band_cache[cache_key]
        if not all_band_entries:
            return None

        # --- Select the band whose discography best matches *album* ---
        album_lower = album.lower()

        def album_match_score(entry):
            """Return the best album-name similarity score for this band entry."""
            best = 0.0
            for alb in entry["albums"]:
                ma_title = alb["title"].lower()
                # Substring containment counts as a strong match
                if album_lower in ma_title or ma_title in album_lower:
                    return 1.0
                ratio = difflib.SequenceMatcher(None, album_lower, ma_title).ratio()
                if ratio > best:
                    best = ratio
            return best

        scored = [(album_match_score(e), e) for e in all_band_entries]
        scored.sort(key=lambda x: x[0], reverse=True)

        print(f"[MA] Band candidates ranked by album match for '{album}':")
        for score, entry in scored:
            print(f"[MA]   score={score:.3f}  '{entry['found_name']}' "
                  f"({len(entry['albums'])} albums) -> {entry['band_url']}")

        # Require at least a modest album match to avoid using the wrong band
        BAND_SCORE_THRESHOLD = 0.5
        best_score, best_entry = scored[0]
        if best_score < BAND_SCORE_THRESHOLD:
            print(f"[MA] WARNING: Best band score {best_score:.3f} is below threshold "
                  f"{BAND_SCORE_THRESHOLD}. No band confidently matched album '{album}'.")
            # Still attempt with the top-scored band rather than giving up entirely
        else:
            print(f"[MA] Selected band: '{best_entry['found_name']}' (score={best_score:.3f})")

        discography = best_entry["albums"]

        # --- Find the album(s) to search for the track ---
        matched_album_urls = []
        for alb in discography:
            if album_lower in alb["title"].lower() or alb["title"].lower() in album_lower:
                matched_album_urls.append(alb["url"])
                print(f"[MA] Album match: '{alb['title']}' matched query '{album}'")

        if not matched_album_urls:
            print(f"[MA] No direct album match for '{album}' — will search all "
                  f"{len(discography)} album(s) as fallback.")
        search_urls = matched_album_urls if matched_album_urls else [a["url"] for a in discography]

        for alb_url in search_urls:
            if alb_url in self._album_cache:
                print(f"[MA] Track list for {alb_url} found in cache.")
            else:
                self._album_cache[alb_url] = self._get_album_tracks(alb_url)
            track_list = self._album_cache[alb_url]

            matched_title, track_id = self._best_track_match(title, track_list)
            if matched_title is not None:
                if track_id is None:
                    print(f"[MA] Track '{matched_title}' found but has no lyrics button.")
                    return None
                time.sleep(1)   # Be polite to the server
                return self._fetch_lyrics_by_id(track_id)

        print(f"[MA] Track '{title}' not found in any searched album.")
        return None


def lyrics_to_plain_lrc(text):
    """
    Wrap plain-text lyrics so they can be stored as a .lrc file without
    timestamps.  Players that support plain .lrc will display them; the
    existing is_lrc_synced() check will correctly classify this as unsynced.
    """
    lines = text.splitlines()
    return "\n".join(lines)

# ------------------ Config & Constants ------------------
APP_NAME = "Synced Lyrics Downloader MOD"
ALL_PROVIDERS = ["Lrclib", "Musixmatch", "Megalobiz", "NetEase", "Genius", "Metal Archives"]
# Providers that never return synced lyrics — skip them when upgrading unsynced -> synced
PLAIN_ONLY_PROVIDERS = {"Genius", "Metal Archives"}
AUDIO_EXTENSIONS = (".mp3", ".flac", ".ogg", ".aac", ".m4a", ".opus")

# ------------------ Global State ------------------
library_data = {}  # { Artist: { Album: [ {path, title, artist, album} ] } }
current_folder = None  # Tracks the last successfully loaded music folder
ui_q = queue.Queue()
stop_download_event = threading.Event()
only_missing_var = None  # Will be initialized in the UI section
album_index_map = []  # list of (artist, album) parallel to album_list widget rows
track_data_list = []  # list of track dicts parallel to track_list widget rows

def ui_call(fn, *args): ui_q.put((fn, args))


def pump_ui_queue():
    try:
        while True:
            fn, args = ui_q.get_nowait()
            fn(*args)
    except queue.Empty:
        pass
    root.after(50, pump_ui_queue)


# ------------------ Core Logic ------------------

N_SCAN = 5  # Update the status bar every N folders during scanning

def scan_library(root_dir):
    """Deep scan folder and extract tags. No disk-caching as requested."""
    new_library = {}
    folder_count = 0
    ui_call(set_status, "Scanning tags...")
    for root_path, _, files in os.walk(root_dir):
        audio_files = [f for f in files if f.lower().endswith(AUDIO_EXTENSIONS)]
        if audio_files:
            folder_count += 1
            if folder_count % N_SCAN == 0:
                display_path = root_path.replace('/','\\')
                ui_call(set_status, f"Scanning [{folder_count}]: {display_path}")
        for f in audio_files:
                full_path = os.path.join(root_path, f)
                title, artist, album = get_tags(full_path)
                title = strip_parens(title)
                if artist not in new_library: new_library[artist] = {}
                if album not in new_library[artist]: new_library[artist][album] = []
                # Store album in the track dict so Metal Archives can use it
                new_library[artist][album].append({
                    "path": full_path,
                    "title": title,
                    "artist": artist,
                    "album": album,
                })
    return new_library


# ------------------ UI Refresh ------------------

def toggle_all_albums():
    """Selects or deselects all albums in the list."""
    if album_list.size() == 0:
        return
    if album_list.curselection():
        album_list.selection_clear(0, tk.END)
    else:
        album_list.selection_set(0, tk.END)
    on_album_select()

def select_all_artists():
    """Selects all artists and loads all their albums into the middle panel."""
    if artist_list.size() == 0:
        return
    artist_list.selection_set(0, tk.END)
    on_artist_select()

def refresh_artist_list():
    artist_list.delete(0, tk.END)
    for artist in sorted(library_data.keys()):
        artist_list.insert(tk.END, "👤 " + artist)


def on_artist_select(event=None):
    global album_index_map
    sel = artist_list.curselection()
    if not sel: return
    album_list.delete(0, tk.END)
    track_list.delete(0, tk.END)
    album_index_map = []
    seen = set()
    for artist_idx in sel:
        artist = artist_list.get(artist_idx)[2:]  # Strip icon
        for alb in sorted(library_data.get(artist, {}).keys()):
            key = (artist, alb)
            if key not in seen:
                seen.add(key)
                album_index_map.append(key)
                album_list.insert(tk.END, "💿 " + alb)


def on_album_select(event=None):
    global track_data_list
    sel_alb = album_list.curselection()
    if not sel_alb: return

    track_list.delete(0, tk.END)
    track_data_list = []

    for alb_idx in sel_alb:
        if alb_idx >= len(album_index_map):
            continue
        artist, album = album_index_map[alb_idx]
        for t in library_data.get(artist, {}).get(album, []):
            track_data_list.append(t)

    for idx, t in enumerate(track_data_list):
        lrc_path = os.path.splitext(t["path"])[0] + ".lrc"
        icon = "❌ "
        is_synced = False
        file_exists = os.path.exists(lrc_path)

        if file_exists:
            try:
                is_synced = is_lrc_synced(lrc_path)
                icon = "✅ "
            except Exception:
                icon = "✅ "

        track_list.insert(tk.END, icon + t["title"])

        if file_exists:
            if is_synced:
                track_list.itemconfig(idx, {'bg': '#9dffb0', 'fg': '#000000'})
            else:
                track_list.itemconfig(idx, {'bg': '#f0f0f0', 'fg': '#000000'})
        else:
            track_list.itemconfig(idx, {'bg': '#ffffff', 'fg': '#000000'})

# ------------------ Advanced Single-Track Download Window ------------------

def open_advanced_window():
    """Open a window for manually entering Artist + Title to download lyrics for a single track."""

    # --- Resolve pre-fill values from the single selected track (if any) ---
    prefill_artist = ""
    prefill_title  = ""
    prefill_path   = ""

    sel_tracks = track_list.curselection()
    if len(sel_tracks) == 1:
        t = track_data_list[sel_tracks[0]]
        prefill_artist = t.get("artist", "")
        prefill_title  = t.get("title",  "")
        prefill_path   = os.path.normpath(os.path.splitext(t["path"])[0] + ".lrc")

    # --- Build the window ---
    win = tk.Toplevel(root)
    win.title("Advanced – Manual Lyrics Download")
    root.update_idletasks()
    win.update_idletasks()

    x = root.winfo_x() + (root.winfo_width() // 2) - 250
    y = root.winfo_y() + (root.winfo_height() // 2) - 150
    win.geometry(f"+{x}+{y}")
    win.resizable(True, True)
    win.grab_set()   # modal

    pad = dict(padx=10, pady=4)

    # Artist row
    tk.Label(win, text="Artist:", anchor="w", width=10).grid(row=0, column=0, sticky="w", **pad)
    artist_var = tk.StringVar(value=prefill_artist)
    artist_entry = tk.Entry(win, textvariable=artist_var, width=40)
    artist_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

    # Title row
    tk.Label(win, text="Title:", anchor="w", width=10).grid(row=1, column=0, sticky="w", **pad)
    title_var = tk.StringVar(value=prefill_title)
    title_entry = tk.Entry(win, textvariable=title_var, width=40)
    title_entry.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)

    # Save-path row
    tk.Label(win, text="Save as:", anchor="w", width=10).grid(row=2, column=0, sticky="w", **pad)
    path_var = tk.StringVar(value=prefill_path)
    path_entry = tk.Entry(win, textvariable=path_var, width=34)
    path_entry.grid(row=2, column=1, sticky="ew", **pad)

    def browse_path():
        initial = path_var.get() or os.path.expanduser("~")
        chosen = filedialog.asksaveasfilename(
            parent=win,
            initialfile=os.path.basename(initial) if initial else "lyrics.lrc",
            initialdir=os.path.dirname(initial) if initial else os.path.expanduser("~"),
            defaultextension=".lrc",
            filetypes=[("LRC files", "*.lrc"), ("All files", "*.*")],
            title="Choose where to save the .lrc file",
        )
        if chosen:
            #path_var.set(chosen.replace("/", "\\"))
            path_var.set(os.path.normpath(chosen))

    tk.Button(win, text="…", command=browse_path, width=3).grid(row=2, column=2, padx=(0, 10))

    # Providers row (mirrors main-window provider_vars)
    prov_frame_adv = tk.LabelFrame(win, text="Providers", padx=5, pady=5)
    prov_frame_adv.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=4)
    adv_provider_vars = {}
    for p in ALL_PROVIDERS:
        var = tk.BooleanVar(value=provider_vars[p].get())
        adv_provider_vars[p] = var
        cb = tk.Checkbutton(prov_frame_adv, text=p, variable=var)
        cb.pack(side="left")
        if p == "Metal Archives" and not MA_AVAILABLE:
            cb.config(fg="gray")
            var.set(False)

    # Button row
    btn_frame_adv = tk.Frame(win)
    btn_frame_adv.grid(row=4, column=0, columnspan=3, pady=6)
    dl_btn = tk.Button(btn_frame_adv, text="Download Lyrics", width=16)
    dl_btn.pack(side="left", padx=8)
    cancel_btn_adv = tk.Button(btn_frame_adv, text="Cancel", state="disabled", width=10)
    cancel_btn_adv.pack(side="left", padx=4)
    tk.Button(btn_frame_adv, text="Close", command=win.destroy, width=10).pack(side="left", padx=4)

    win.columnconfigure(1, weight=1)

    # --- Liveness flag (for buttons only — window may close while worker runs) ---
    win_alive = [True]

    def _on_close():
        win_alive[0] = False
        adv_stop_event.set()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)

    # --- Thread-safe helpers ---
    # All log messages go to the main window's Download Log via log()
    adv_log = log

    def _adv_set_buttons(downloading):
        if not win_alive[0]:
            return
        dl_btn.config(state="disabled" if downloading else "normal")
        cancel_btn_adv.config(state="normal" if downloading else "disabled")

    def adv_set_buttons(downloading):
        ui_call(_adv_set_buttons, downloading)

    # --- Download worker ---
    adv_stop_event = threading.Event()

    def adv_download():
        artist   = artist_var.get().strip()
        title    = title_var.get().strip()
        lrc_path = path_var.get().strip()

        if not artist or not title:
            messagebox.showwarning("Missing fields", "Please fill in both Artist and Title.", parent=win)
            return
        if not lrc_path:
            messagebox.showwarning("Missing path", "Please choose a save location.", parent=win)
            return

        active = [p for p in ALL_PROVIDERS if adv_provider_vars[p].get()]
        if not active:
            messagebox.showwarning("Providers", "Please select at least one provider.", parent=win)
            return

        if "Metal Archives" in active and not MA_AVAILABLE:
            active = [p for p in active if p != "Metal Archives"]
            if not active:
                messagebox.showwarning("Providers", "No usable providers selected.", parent=win)
                return

        adv_stop_event.clear()
        adv_set_buttons(True)
        adv_log(f"--- Downloading: {artist} – {title}")

        def worker():
            tmp_path     = lrc_path + ".tmp"
            best_lrc_path = None
            success      = False
            ma_session   = None

            if "Metal Archives" in active and MA_AVAILABLE:
                adv_log("--- Launching Metal Archives browser session...")
                try:
                    ma_session = MetalArchivesSession()
                    ma_session.start()
                    adv_log("--- Metal Archives browser ready.")
                except Exception as e:
                    adv_log(f"  ✘ Metal Archives: could not start browser: {e}")
                    ma_session = None

            try:
                for p in active:
                    if adv_stop_event.is_set():
                        break

                    adv_log(f"  → Trying {p}…")

                    # ---- Metal Archives ----
                    if p == "Metal Archives":
                        if ma_session is None:
                            adv_log(f"  ✘ Metal Archives: browser not available, skipping.")
                            continue
                        try:
                            lyrics_text = ma_session.fetch_lyrics(
                                artist=artist, album="", title=title)
                        except Exception as e:
                            adv_log(f"  ✘ Metal Archives error: {e}")
                            continue
                        if not lyrics_text:
                            adv_log(f"  ✘ Metal Archives: not found.")
                            continue
                        plain_lrc = lyrics_to_plain_lrc(lyrics_text)
                        try:
                            with open(tmp_path, "w", encoding="utf-8") as f:
                                f.write(plain_lrc)
                        except Exception as e:
                            adv_log(f"  ✘ Metal Archives: could not write file ({e})")
                            continue
                        if best_lrc_path is None:
                            os.replace(tmp_path, lrc_path)
                            best_lrc_path = lrc_path
                            success = True
                            adv_log(f"  ~ Metal Archives: saved unsynced lyrics, continuing search for synced...")
                        else:
                            os.remove(tmp_path)
                        continue

                    # ---- Standard syncedlyrics providers ----
                    query = f"{artist} - {title}"
                    cmd   = ["syncedlyrics", query, "-p", p.lower(), "-o", tmp_path]
                    try:
                        subprocess.run(cmd, capture_output=True,
                                       shell=sys.platform == "win32", timeout=30)
                    except subprocess.TimeoutExpired:
                        adv_log(f"  ✘ {p}: timed out.")
                        continue

                    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 10:
                        adv_log(f"  ✘ {p}: no result.")
                        continue

                    if is_lrc_synced(tmp_path):
                        os.replace(tmp_path, lrc_path)
                        best_lrc_path = lrc_path
                        success = True
                        adv_log(f"  ✔ {p}: synced lyrics found!")
                        break
                    else:
                        adv_log(f"  ~ {p}: unsynced lyrics, continuing search for synced...")
                        if best_lrc_path is None:
                            os.replace(tmp_path, lrc_path)
                            best_lrc_path = lrc_path
                            success = True
                        else:
                            os.remove(tmp_path)

            finally:
                if ma_session:
                    ma_session.stop()
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            # --- Final status ---
            if adv_stop_event.is_set():
                adv_log("--- Cancelled by user.")
            elif success:
                synced = is_lrc_synced(lrc_path) if best_lrc_path else False
                label  = "synced" if synced else "unsynced"
                adv_log(f"--- ✔ Done. Saved {label} lyrics → {lrc_path}")
            else:
                adv_log("--- ✖ No lyrics found with the selected providers.")

            adv_set_buttons(False)
            ui_call(on_album_select)   # refresh main window track icons

        threading.Thread(target=worker, daemon=True).start()

    def adv_cancel():
        adv_stop_event.set()
        adv_log("!!! Cancellation requested…")

    dl_btn.config(command=adv_download)
    cancel_btn_adv.config(command=adv_cancel)
    artist_entry.focus_set()


# ------------------ Download Logic ------------------

def start_download():
    targets = []

    sel_tracks = track_list.curselection()
    if sel_tracks:
        # Specific tracks chosen — look them up directly in track_data_list
        for i in sel_tracks:
            if i < len(track_data_list):
                targets.append(track_data_list[i])
    else:
        # No track selection — download all tracks from every selected album
        for i in album_list.curselection():
            if i < len(album_index_map):
                artist, album = album_index_map[i]
                targets.extend(library_data.get(artist, {}).get(album, []))

    if not targets:
        messagebox.showinfo("Select", "Please select at least one album or track.")
        return

    active_providers = [p for p in ALL_PROVIDERS if provider_vars[p].get()]
    if not active_providers:
        messagebox.showwarning("Providers", "Please select at least one provider.")
        return

    # Warn if Metal Archives is selected but Playwright/BS4 are not installed
    if "Metal Archives" in active_providers and not MA_AVAILABLE:
        messagebox.showwarning(
            "Metal Archives",
            "Metal Archives requires:\n"
            "Edge browser installed"
            "Python libraries 'playwright' and 'beautifulsoup4'.\n"
            "Install them with:\n\n"
            "  pip install playwright beautifulsoup4\n"
            "Metal Archives will be skipped for this session."
        )
        active_providers = [p for p in active_providers if p != "Metal Archives"]
        if not active_providers:
            return

    def worker():
        stop_download_event.clear()

        # Tracks are split into three states:
        #   - no .lrc at all        -> needs a fresh download
        #   - .lrc exists, unsynced -> attempt to upgrade to synced
        #   - .lrc exists, synced   -> skip entirely
        final_targets = []
        if only_missing_var.get():
            for t in targets:
                lrc_path = os.path.splitext(t['path'])[0] + ".lrc"
                if not os.path.exists(lrc_path) or os.path.getsize(lrc_path) < 10:
                    final_targets.append(dict(t, _upgrade=False))
                elif not is_lrc_synced(lrc_path):
                    final_targets.append(dict(t, _upgrade=True))

            if not final_targets:
                log("--- All selected tracks already have synced lyrics. Nothing to do.")
                ui_call(set_status, "Ready.", "normal")
                return
        else:
            final_targets = [dict(t, _upgrade=False) for t in targets]

        # --- Start a Metal Archives browser session if needed ---
        ma_session = None
        if "Metal Archives" in active_providers and MA_AVAILABLE:
            log("--- Launching Metal Archives browser session...")
            ui_call(set_status, "Starting Metal Archives browser...")
            try:
                ma_session = MetalArchivesSession()
                ma_session.start()
                log("--- Metal Archives browser ready.")
            except Exception as e:
                import traceback
                log(f"  ✘ Metal Archives: Could not start browser:\n{traceback.format_exc()}")

        try:
            for idx, track in enumerate(final_targets, 1):
                if stop_download_event.is_set():
                    log("--- Download Cancelled by user.")
                    break

                is_upgrade = track.get('_upgrade', False)
                if is_upgrade:
                    log(f"[{idx}/{len(final_targets)}] Upgrading (unsynced->synced): {track['title']}")
                else:
                    log(f"[{idx}/{len(final_targets)}] Downloading: {track['title']}")
                success = False

                lrc_path = os.path.splitext(track['path'])[0] + ".lrc"
                tmp_path = lrc_path + ".tmp"
                best_lrc_path = None

                for p in active_providers:
                    if stop_download_event.is_set():
                        log("--- Download Cancelled by user.")
                        ui_call(set_status, "Cancelled.", "error")
                        ui_call(on_album_select)
                        return

                    if is_upgrade and p in PLAIN_ONLY_PROVIDERS:
                        log(f"  – SKIP: {p} never provides synced lyrics (upgrade mode).")
                        continue

                    # When re-downloading everything (only_missing unchecked),
                    # skip plain-only providers for tracks that already have a
                    # synced .lrc — they can't improve on what's already there.
                    if not is_upgrade and p in PLAIN_ONLY_PROVIDERS:
                        if os.path.exists(lrc_path) and is_lrc_synced(lrc_path):
                            log(f"  – SKIP: {p} is plain-only and track already has synced lyrics.")
                            continue

                    ui_call(set_status, f"Downloading {track['title']} ({p})...")

                    # ---- Metal Archives: custom browser-based fetch ----
                    if p == "Metal Archives":
                        if ma_session is None:
                            continue  # Browser failed to start earlier
                        try:
                            lyrics_text = ma_session.fetch_lyrics(
                                artist=track.get("artist", ""),
                                album=track.get("album", ""),
                                title=track["title"],
                            )
                        except Exception as e:
                            log(f"  ✘ FAILED: Metal Archives — {e}")
                            continue

                        if not lyrics_text:
                            log(f"  ✘ FAILED: Metal Archives — not found.")
                            continue

                        # MA only provides plain (unsynced) lyrics
                        plain_lrc = lyrics_to_plain_lrc(lyrics_text)
                        try:
                            with open(tmp_path, "w", encoding="utf-8") as f:
                                f.write(plain_lrc)
                        except Exception as e:
                            log(f"  ✘ FAILED: Metal Archives — could not write file ({e})")
                            continue

                        # Plain lyrics -> treat the same as any other unsynced result
                        log(f"  ~ PLAIN: Metal Archives returned unsynced lyrics, trying next provider...")
                        if is_upgrade:
                            # Upgrading: don't overwrite existing unsynced with another unsynced
                            os.remove(tmp_path)
                        elif best_lrc_path is None:
                            os.replace(tmp_path, lrc_path)
                            best_lrc_path = lrc_path
                            success = True
                            ui_call(on_album_select)
                        else:
                            os.remove(tmp_path)

                        # Keep searching for a synced version from other providers
                        continue

                    # ---- Standard syncedlyrics providers ----
                    query = f"{track['artist']} - {track['title']}"
                    cmd = ["syncedlyrics", query, "-p", p.lower(), "-o", tmp_path]
                    try:
                        subprocess.run(
                            cmd,
                            capture_output=True,
                            shell=sys.platform == "win32",
                            timeout=30
                        )
                    except subprocess.TimeoutExpired:
                        log(f"  ✘ TIMEOUT: {p} took too long, skipping.")
                        continue

                    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 10:
                        log(f"  ✘ FAILED: {p} — no file returned.")
                        continue

                    if is_lrc_synced(tmp_path):
                        log(f"  ✔ SYNCED: Found synced lyrics on {p}")
                        os.replace(tmp_path, lrc_path)
                        best_lrc_path = lrc_path
                        success = True
                        ui_call(on_album_select)
                        break  # Synced is ideal - no need to try further providers
                    else:
                        log(f"  ~ PLAIN: {p} returned unsynced lyrics, trying next provider...")
                        if is_upgrade:
                            os.remove(tmp_path)
                        elif best_lrc_path is None:
                            os.replace(tmp_path, lrc_path)
                            best_lrc_path = lrc_path
                            success = True
                            ui_call(on_album_select)
                        else:
                            os.remove(tmp_path)

                # Clean up temp file if something went wrong mid-loop
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

                if not success:
                    if is_upgrade:
                        log(f"  ℹ No synced version found for '{track['title']}' - keeping existing unsynced lyrics.")
                    else:
                        log(f"  ✖ No lyrics found for '{track['title']}' after trying all selected providers.")
                elif best_lrc_path and not is_lrc_synced(best_lrc_path):
                    log(f"  ℹ Saved unsynced lyrics for '{track['title']}' (no synced version found).")

        finally:
            # Always shut the browser down, even if an exception occurred
            if ma_session is not None:
                log("--- Closing Metal Archives browser session.")
                ma_session.stop()

        ui_call(set_status, "All downloads complete.")
        ui_call(on_album_select)

    threading.Thread(target=worker, daemon=True).start()


# ------------------ UI Helpers ------------------
def cancel_download():
    """Signals the background worker to stop downloading."""
    if not stop_download_event.is_set():
        stop_download_event.set()
        log("!!! Cancellation requested. Stopping after current track...")
        ui_call(set_status, "Cancelling...")

def set_status(text, color="normal"):
    status_var.set(text)


def log(msg):
    ui_call(lambda m: [log_box.insert(tk.END, m + "\n"), log_box.see(tk.END)], msg)


def open_folder():
    """Opens the folder of the currently selected album, falling back to the root music folder."""
    folder = None

    sel_alb = album_list.curselection()
    if sel_alb and sel_alb[0] < len(album_index_map):
        artist, album = album_index_map[sel_alb[0]]
        tracks = library_data.get(artist, {}).get(album, [])
        if tracks:
            folder = os.path.dirname(tracks[0]["path"])

    if not folder:
        if not current_folder:
            messagebox.showinfo("No Folder", "No music folder is loaded yet.")
            return
        folder = current_folder

    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.run(["open", folder])
    else:
        subprocess.run(["xdg-open", folder])


def open_selected_track_lyrics(event=None):
    """Open the double-clicked track's .lrc file in the default text editor."""
    if not track_data_list:
        return

    if event is not None:
        track_idx = track_list.nearest(event.y)
        if track_idx < 0 or track_idx >= len(track_data_list):
            return
        track_list.selection_clear(0, tk.END)
        track_list.selection_set(track_idx)
        track_list.activate(track_idx)
    else:
        sel_tracks = track_list.curselection()
        if not sel_tracks:
            return
        track_idx = sel_tracks[0]

    track = track_data_list[track_idx]
    lrc_path = os.path.normpath(os.path.splitext(track["path"])[0] + ".lrc")

    if not os.path.exists(lrc_path):
        messagebox.showinfo("Lyrics Not Found", f"No lyrics file exists yet for:\n\n{track['title']}")
        return

    if sys.platform == "win32":
        os.startfile(lrc_path)
    elif sys.platform == "darwin":
        subprocess.run(["open", lrc_path])
    else:
        subprocess.run(["xdg-open", lrc_path])



def choose_folder():
    folder = filedialog.askdirectory()
    if folder:
        def run_scan():
            global library_data, current_folder
            library_data = scan_library(folder)
            current_folder = folder
            ui_call(refresh_artist_list)
            ui_call(set_status, f"Loaded {len(library_data)} artists.")
            ui_call(auto_select_first)

        threading.Thread(target=run_scan, daemon=True).start()


# ------------------ Main UI ------------------
root = tk.Tk()
root.title(APP_NAME)
root.geometry("1000x800")

# Top Controls
top_frame = tk.Frame(root, padx=10, pady=5)
top_frame.pack(fill="x")
artist_ctrl_frame = tk.Frame(root, padx=10, pady=5)
artist_ctrl_frame.pack(fill='x')

tk.Button(top_frame, text="Select Folder", command=choose_folder).pack(side="left", padx=(10, 0))
tk.Button(top_frame, text="Browse Folder", command=open_folder).pack(side="left", padx=(20, 0))
tk.Button(artist_ctrl_frame, text="Select All Artists", command=select_all_artists).pack(side="left", padx=(10, 0))
tk.Button(artist_ctrl_frame, text="Select All Albums", command=toggle_all_albums).pack(side="left", padx=(160, 0))

# Provider selection checkboxes
prov_frame = tk.LabelFrame(top_frame, padx=5, pady=0)
prov_frame.pack(side="left", padx=(100, 10))
provider_vars = {}
for p in ALL_PROVIDERS:
    var = tk.BooleanVar(value=True)
    provider_vars[p] = var
    cb = tk.Checkbutton(prov_frame, text=p, variable=var)
    cb.pack(side="left")
    # Dim the Metal Archives checkbox if dependencies are missing
    if p == "Metal Archives" and not MA_AVAILABLE:
        cb.config(fg="gray", state="normal")
        var.set(False)
        if not EDGE_AVAILABLE:
            cb.config(text="Metal Archives (Edge not found)")
# Browser Panes
panes = tk.PanedWindow(root, orient="horizontal", sashwidth=4)
panes.pack(fill="both", expand=True, padx=10)

# Artist Panel
artist_frame = tk.Frame(panes)
artist_list = tk.Listbox(artist_frame, exportselection=0, selectmode="extended")
artist_scroll = tk.Scrollbar(artist_frame, orient="vertical", command=artist_list.yview)
artist_list.config(yscrollcommand=artist_scroll.set)
artist_scroll.pack(side="right", fill="y")
artist_list.pack(side="left", fill="both", expand=True)
artist_list.bind("<<ListboxSelect>>", on_artist_select)
panes.add(artist_frame, width=250)

# Album Panel
album_frame = tk.Frame(panes)
album_list = tk.Listbox(album_frame, exportselection=0)
album_scroll = tk.Scrollbar(album_frame, orient="vertical", command=album_list.yview)
album_list.config(yscrollcommand=album_scroll.set)
album_scroll.pack(side="right", fill="y")
album_list.pack(side="left", fill="both", expand=True)
album_list.bind("<<ListboxSelect>>", on_album_select)
panes.add(album_frame, width=330)

# Track Panel
track_frame = tk.Frame(panes)
track_list = tk.Listbox(track_frame, exportselection=0, selectmode="extended")
track_scroll = tk.Scrollbar(track_frame, orient="vertical", command=track_list.yview)
track_list.config(yscrollcommand=track_scroll.set)
track_scroll.pack(side="right", fill="y")
track_list.pack(side="left", fill="both", expand=True)
track_list.bind("<Double-Button-1>", open_selected_track_lyrics)
panes.add(track_frame)

# Action Row
btn_row = tk.Frame(root, pady=5)
btn_row.pack(fill="x")
tk.Button(btn_row, text="Download Lyrics", command=start_download).pack(side="left", padx=10)
tk.Button(btn_row, text="Cancel", command=cancel_download).pack(side="left")
only_missing_var = tk.BooleanVar(value=True)
tk.Checkbutton(btn_row, text="Only Download Missing", variable=only_missing_var).pack(side="left", padx=20)
tk.Button(btn_row, text="Advanced", command=open_advanced_window).pack(side="left", padx=(0, 10))

# Right Side: The Legend
legend_subgroup = tk.Frame(btn_row)
legend_subgroup.pack(side="right", padx=10)


def add_legend_item(parent, icon, text, bg_color=None):
    item_container = tk.Frame(parent, padx=5)
    item_container.pack(side="left")

    lbl = tk.Label(
        item_container,
        text=f"{icon} {text}",
        font=("Arial", 8),
        padx=5,
        pady=2,
        fg="black"
    )
    if bg_color:
        lbl.configure(bg=bg_color)
    lbl.pack(side="left")

add_legend_item(legend_subgroup, "✅", "Synced", "#9dffb0")
add_legend_item(legend_subgroup, "✅", "Standard", "#f0f0f0")
add_legend_item(legend_subgroup, "❌", "Missing")


# Large Status/Log Window
log_frame = tk.Frame(root, padx=10, pady=5)
log_frame.pack(fill="both", expand=True)
tk.Label(log_frame, text="Download Log:").pack(anchor="w")
log_box = tk.Text(log_frame, height=12, bg="#d3d3d3", fg="black", font=("Consolas", 10))
log_box.pack(fill="both", expand=True, side="left")
scroll = tk.Scrollbar(log_frame, command=log_box.yview)
scroll.pack(side="right", fill="y")
log_box.config(yscrollcommand=scroll.set)

status_var = tk.StringVar(value="Ready.")
tk.Label(root, textvariable=status_var, anchor="w", padx=10, relief="sunken", bg="#d3d3d3").pack(fill="x")


def auto_select_first():
    """Selects the first artist and album safely."""
    if artist_list.size() > 0:
        artist_list.selection_clear(0, tk.END)
        artist_list.selection_set(0)
        artist_list.activate(0)
        artist_list.see(0)
        on_artist_select()

        if album_list.size() > 0:
            album_list.selection_clear(0, tk.END)
            album_list.selection_set(0)
            album_list.activate(0)
            album_list.see(0)
            on_album_select()


def check_args():
    folder_path = None

    if len(sys.argv) > 1:
        folder_path = sys.argv[1].strip('"')
        log(f"Received path: {folder_path}")

    if folder_path and os.path.isdir(folder_path):
        def run_initial_load():
            global library_data, current_folder
            library_data = scan_library(folder_path)
            current_folder = folder_path
            ui_call(refresh_artist_list)
            ui_call(set_status, f"Auto-loaded: {len(library_data)} artists.")
            ui_call(auto_select_first)

        threading.Thread(target=run_initial_load, daemon=True).start()
    elif folder_path:
        log(f"Error: '{folder_path}' is not a valid directory.")


# Final Execution Block
pump_ui_queue()
root.after(100, check_args)
root.mainloop()
