# 🎵 Synced Lyrics Downloader MOD

A desktop GUI app for downloading synced (`.lrc`) lyrics for your local music library. Built with Python and tkinter.  
Fork of https://github.com/type0dev/synced-lyrics-downloader

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

## Important changes:
  - Track recognition based on tags (no folder structure needed)  
  - New lyrics supplier: Metal Archives
  
---
## Screenshot

![Synced Lyrics Downloader](https://raw.githubusercontent.com/magsho/synced-lyrics-downloader-mod/refs/heads/main/Screenshot.png)

---
## Features

- **Synced lyrics first** — always tries `.lrc` with timestamps before falling back to plain text
- **Everything saved as `.lrc`** — maximum compatibility with all media players
- **Multiple providers** — Lrclib, Musixmatch, Megalobiz, NetEase, Genius, Metal Archives
- **Smart scanning** — scan selection for missing lyrics, download only what's missing
- **Music files reconition** based on tags in files, your folder structure doesn't matter
- **Supported file types:** mp3, flac, ogg, aac, m4a, opus
---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/magsho/synced-lyrics-downloader-mod.git  
cd synced-lyrics-downloader-mod

# 2. Install the dependencies
pip install syncedlyrics
pip install mutagen
pip install playwright

# 3. Run
python lyrics_downloader_ultimate_mod.py
or with folder path as argument:
python lyrics_downloader_ultimate_mod.py "PATH"
```

---

## Quick Start

1. Click **Select Folder** and point it at your music library
2. Select an **artist** from the left panel
3. Select **albums** and/or **tracks** (or use Select All Albums)
4. Click **Download Lyrics**
5. Watch the log panel — done!

Things to consider:
Artist and Track recognition is based on tags in music files, without proper tags it will not work!  
Supported file types are: mp3, flac, ogg, aac, m4a, opus.  
Metal Archives downloading works by custom web scrapper inside Edge browser installed in your system thus it's Windows only.  

## Providers

| Provider | Synced | Plain | Notes |
|----------|--------|-------|-------|
| Lrclib | ✅ | ❌ | Best first choice, open source |
| Musixmatch | ✅ | ❌ | Good coverage |
| Megalobiz | ✅ | ❌ | Good for older tracks |
| NetEase | ✅ | ❌ | Large Asian library, may give non-English results |
| Genius | ❌ | ✅ | Plain text only |
| Metal Archives | ❌ | ✅ | Plain text only, has only metal lyrics often not available anywhere else |

---

## License

MIT — do whatever you want with it.

