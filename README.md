# SSGrab

A small Windows tray app that works like SSvid: copy a video link in Chrome and a
popup offers to download it as an MP4.

- **Sites:** YouTube (videos, Shorts, live), Facebook (videos, reels, fb.watch),
  Vimeo, Instagram (posts, reels).
- **Everything else is ignored.** SSGrab only reacts when the link is copied while
  Chrome is the active window (other browsers can be added in Settings).
- **Downloads happen locally** with [yt-dlp](https://github.com/yt-dlp/yt-dlp).
  No third-party download server is involved.

## Requirements

- Windows 10 or 11
- Python 3.10+ on PATH (`python --version`)
- [ffmpeg](https://ffmpeg.org/) – merges separate HD video and audio streams into one MP4.
  Without it, downloads are limited to pre-merged files (about 720p at most).
- [Deno](https://deno.com/) – yt-dlp needs a JavaScript runtime to decode YouTube.

`install.bat` installs all of these (ffmpeg and Deno through `winget`).

## Install

1. Put `ssgrab.py`, `install.bat` and `update-ytdlp.bat` in one folder.
   If you downloaded them in a zip, right-click the zip → **Properties** → **Unblock**
   before extracting.
2. Run `install.bat`. It:
   - installs the Python packages (`yt-dlp`, `pystray`, `pillow`, `pyinstaller`)
   - installs ffmpeg and Deno
   - builds `SSGrab.exe` into the same folder and deletes the build leftovers
   - adds a Startup shortcut so SSGrab starts minimized to the tray at sign-in
3. Open a new terminal (or sign out and back in) so ffmpeg and Deno are on PATH,
   then start `SSGrab.exe`.

## Using it

1. Copy a video link in Chrome (address bar, right-click → Copy link, Share → Copy).
2. A popup appears in the bottom-right corner with the title and length.
3. Pick a quality and click **Download MP4**, or ignore it – the popup closes
   itself after 20 seconds.
4. When it finishes, **Show in folder** opens the file in Explorer.

The main window (double-click the tray icon) shows a log of detected links and
downloads, and has buttons for **Open folder**, **Update helpers** and **Settings…**.

## Where things are saved

| What | Where |
|---|---|
| Videos | `%USERPROFILE%\Downloads` by default, named `Title [video-id].mp4` |
| Settings | `%APPDATA%\SSGrab\config.json` |
| Temp files, yt-dlp cache, YouTube token helper | `%LOCALAPPDATA%\SSGrab\` |

## Settings

| Setting | Meaning |
|---|---|
| Download folder | Where videos are saved |
| Browsers | Exe names to watch, comma-separated, e.g. `chrome.exe, msedge.exe` |
| Default quality | `best`, `2160`, `1440`, `1080`, `720`, `480` or `audio` |
| Cookies from browser | e.g. `firefox` – only needed for private or members-only videos |
| Cookie file | A Netscape-format `cookies.txt` export, as an alternative to the above |
| Popup timeout | Seconds before an unused popup closes |

`use_token_helper` (see below) can only be changed by editing `config.json`.

## YouTube bot checks

YouTube sometimes answers yt-dlp with *"Sign in to confirm you're not a bot."*
It wants a Proof-of-Origin token, which a browser generates automatically.

SSGrab handles this with the
[bgutil PO-token provider (Rust build)](https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs):

- On first start it downloads `bgutil-pot.exe` and its yt-dlp plugin (about 50 MB)
  into `%LOCALAPPDATA%\SSGrab\pot`.
- **Nothing runs in the background.** yt-dlp starts `bgutil-pot.exe` only when a
  YouTube video needs a token, and it exits as soon as it has produced one.
- **Update helpers** in the main window downloads fresh copies.
- To turn it off, set `"use_token_helper": false` in `config.json`.

For each YouTube link SSGrab tries, in order, stopping at the first that works:

1. normal request, with the token helper available
2. YouTube's mobile-web client with a token on every request
3. your cookie source from Settings, if one is set

Cookies are never sent together with a token, because YouTube rejects that
combination. Each failed attempt is listed in the main window's log.

**About cookies:** Chrome encrypts its cookies on Windows so other programs can't
read them. Use `firefox` as the cookie source, or export a `cookies.txt` from Chrome
with an extension such as *Get cookies.txt LOCALLY*. A spare Google account is
safer than your main one.

## When downloads start failing

Sites change often and yt-dlp catches up within days. Run `update-ytdlp.bat`: it
updates yt-dlp, rebuilds `SSGrab.exe` in place, cleans up and restarts SSGrab.
If YouTube still fails, also click **Update helpers**.

## Troubleshooting

- **`Permission denied: C:\Windows\System32\...tmp`** – fixed in current versions
  (SSGrab uses its own temp folder). With an older build, set the Startup
  shortcut's **Start in** field to the folder that holds `SSGrab.exe`.
- **HD downloads stop at 720p** – ffmpeg isn't on PATH. Run
  `winget install Gyan.FFmpeg`, then restart SSGrab from a new session.
- **Popup never appears** – check **Monitor clipboard** is ticked, the link was
  copied in a browser listed in Settings, and the link is a video page (not a
  channel, profile or search page).

## Note

Downloading may break a site's terms of service. Use it for content you have the
right to save.
