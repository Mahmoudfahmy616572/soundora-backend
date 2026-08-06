"""Soundora Audio Resolver backend.

Search YouTube via yt-dlp and return a direct full-length audio URL plus the
headers needed to stream it. Used as the full-song fallback in the Flutter app.
"""

import asyncio
import base64
import hashlib
import os
import re
import shutil
import tempfile
import time

import anyio
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse

app = FastAPI(title="Soundora Audio Resolver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 15,
    "retries": 2,
    "format": "bestaudio/best",
    "remote_components": "ejs:github",
}

_COOKIE_MASTER: str | None = None
_COOKIE_FILE = os.environ.get("YT_COOKIES", "").strip()
if not _COOKIE_FILE:
    for _candidate in ("cookies.txt", os.path.join(os.path.dirname(__file__), "cookies.txt")):
        if os.path.exists(_candidate):
            _COOKIE_FILE = _candidate
            break
if _COOKIE_FILE:
    # yt-dlp writes back to the cookiefile it reads, which can shrink/corrupt
    # the master file. Keep a pristine master and let yt-dlp mutate a scratch
    # copy in /tmp instead.
    _COOKIE_MASTER = _COOKIE_FILE
    _scratch = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    try:
        shutil.copyfile(_COOKIE_MASTER, _scratch)
        _COOKIE_FILE = _scratch
    except OSError:
        _COOKIE_MASTER = None
    _BASE_OPTS["cookiefile"] = _COOKIE_FILE


def _refresh_cookie_scratch() -> None:
    """Sync the working cookie copy from the master before each yt-dlp call.

    Successful yt-dlp runs refresh the session in the master file; the scratch
    copy used by the server would otherwise go stale and get 403s.
    """
    if _COOKIE_MASTER and _COOKIE_FILE:
        try:
            shutil.copyfile(_COOKIE_MASTER, _COOKIE_FILE)
        except OSError:
            pass

# yt-dlp needs a JS runtime (deno) to solve YouTube signature/n challenges on
# datacenter IPs. Make sure a user-level deno install is visible no matter how
# uvicorn was started (e.g. Codespaces postStart).
for _deno_dir in (
    os.path.join(os.path.expanduser("~"), ".deno", "bin"),
    "/usr/local/bin",
):
    if os.path.isfile(os.path.join(_deno_dir, "deno")):
        if _deno_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = _deno_dir + os.pathsep + os.environ.get("PATH", "")
        break

_ID_RE = re.compile(r"^[\w-]{11}$")


def _search(query: str, limit: int = 8) -> list:
    opts = {
        **_BASE_OPTS,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }
    _refresh_cookie_scratch()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        return info.get("entries") or []


def _clean(text: str) -> str:
    """Normalise a title/artist for searching: drop separators and junk tokens."""
    t = re.sub(r"\s*-\s*", " ", text)
    t = re.sub(r"\b(RoL|FM|HD|Official|Lyrics|Audio|Video|Song)\b", " ", t,
               flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


def _query_variants(title: str, artist: str) -> list:
    raw = f"{title} {artist}".strip()
    clean_title = _clean(title)
    clean_artist = _clean(artist)
    variants = [
        raw,
        title,
        clean_title,
        f"{clean_title} أغنية",
        f"{clean_title} song",
    ]
    if clean_artist:
        variants.insert(3, f"{clean_title} {clean_artist}")
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _pick_entry(candidates: list) -> dict | None:
    for entry in candidates:
        if not entry:
            continue
        duration = entry.get("duration") or 0
        if duration and (duration < 45 or duration > 3600):
            continue
        if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        return entry
    return None


def _resolve(video_id: str) -> dict:
    opts = {**_BASE_OPTS, "skip_download": True}
    opts.pop("format", None)
    _refresh_cookie_scratch()
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_id, download=False)


def _pick_audio_url(info: dict) -> str | None:
    def is_direct(u: str | None) -> bool:
        return bool(u) and "manifest.googlevideo.com" not in u and not u.endswith(".m3u8")

    url = info.get("url")
    if is_direct(url):
        return url
    formats = info.get("formats") or []
    best_combined = best_audio = None
    for f in formats:
        acodec = f.get("acodec") or ""
        fu = f.get("url") or ""
        proto = f.get("protocol") or ""
        if acodec == "none" or not is_direct(fu) or "m3u8" in proto:
            continue
        bitrate = f.get("tbr") or f.get("abr") or 0
        combined = (f.get("vcodec") or "") != "none"
        slot = best_combined if combined else best_audio
        if slot is None or bitrate > slot[0]:
            slot = (bitrate, fu)
            if combined:
                best_combined = slot
            else:
                best_audio = slot
    winner = best_combined or best_audio
    return winner[1] if winner else None
    return None


@app.get("/health")
def health():
    return {"status": "ok"}


_STREAM_CACHE: dict[str, tuple[str, float]] = {}
_STREAM_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_TTL = 15 * 60


def _download_to_file(url: str) -> str:
    """Download a signed googlevideo URL fully to a local temp file.

    Streaming relays get cut mid-transfer by the CDN; a bounded download via
    yt-dlp (which handles cookies + n-sig at request time) completes reliably.
    The app then gets a stable local file with full Range support.
    """
    h = hashlib.sha256(url.encode()).hexdigest()[:20]
    out = os.path.join(tempfile.gettempdir(), f"yt_{h}.mp4")
    opts = {
        **_BASE_OPTS,
        "outtmpl": out,
        "noprogress": True,
        "overwrites": True,
    }
    opts.pop("format", None)  # direct googlevideo URLs break with format select
    _refresh_cookie_scratch()
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if not os.path.isfile(out):
        raise RuntimeError("download produced no file")
    return out


@app.get("/stream")
async def stream(request: Request, url: str | None = Query(default=None),
                 u: str | None = Query(default=None)):
    """Download the signed googlevideo stream and serve it as a local file.

    The googlevideo URLs returned by /resolve are signed for this backend's
    IP, so a device whose egress differs (e.g. the Android emulator) gets a 403
    when streaming them directly. Serving the audio from here makes the request
    originate from the signing IP, and downloading before serving avoids the
    mid-transfer drops the raw relay experienced.

    Pass the stream URL via ``url`` or, when a reverse proxy might inspect the
    query (e.g. Codespaces tunnels), via ``u`` as base64url-encoded.
    """
    if u is not None:
        try:
            padding = "=" * (-len(u) % 4)
            url = base64.urlsafe_b64decode(u + padding).decode("utf-8")
        except Exception:
            raise HTTPException(status_code=400, detail="bad encoding")
    if not url:
        raise HTTPException(status_code=400, detail="missing url")

    host = httpx.URL(url).host or ""
    if "googlevideo.com" not in host:
        raise HTTPException(status_code=400, detail="unsupported host")

    h = hashlib.sha256(url.encode()).hexdigest()[:20]
    lock = _STREAM_LOCKS.setdefault(h, asyncio.Lock())

    def _fresh(entry) -> bool:
        return (
            entry is not None
            and os.path.exists(entry[0])
            and time.time() - entry[1] < _CACHE_TTL
        )

    entry = _STREAM_CACHE.get(h)
    if not _fresh(entry):
        async with lock:
            entry = _STREAM_CACHE.get(h)
            if not _fresh(entry):
                try:
                    path = await anyio.to_thread.run_sync(_download_to_file, url)
                except Exception as exc:
                    raise HTTPException(
                        status_code=502, detail=f"download failed: {exc}"
                    )
                entry = (path, time.time())
                _STREAM_CACHE[h] = entry
    return FileResponse(entry[0], media_type="video/mp4")


@app.get("/resolve")
def resolve(title: str = Query(...), artist: str = ""):
    query = f"{title} {artist}".strip()
    if not query:
        raise HTTPException(status_code=400, detail="empty query")

    candidates = []
    for variant in _query_variants(title, artist):
        try:
            for entry in _search(variant):
                if entry and entry not in candidates:
                    candidates.append(entry)
        except Exception:
            continue
        if len(candidates) >= 6:
            break
    if not candidates:
        raise HTTPException(status_code=404, detail="no result")

    last_error = None
    for entry in candidates:
        if _pick_entry([entry]) is None:
            continue
        video_id = entry.get("id")
        if not video_id or not _ID_RE.match(video_id):
            continue
        try:
            info = _resolve(video_id)
        except Exception as e:
            last_error = str(e)
            continue
        url = _pick_audio_url(info)
        if not url:
            continue
        headers = info.get("http_headers") or {}
        return {
            "videoId": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "url": url,
            "headers": headers,
        }
    raise HTTPException(status_code=500, detail=f"no audio url ({last_error})")
