# Soundora Audio Resolver

Small backend that searches YouTube with `yt-dlp` and returns a direct
full-length audio URL so the Flutter app can stream/download the whole track
(no more 30s Deezer previews for mainstream music).

## Endpoints

- `GET /health` → `{"status": "ok"}`
- `GET /resolve?title=<title>&artist=<artist>` →
  ```json
  {
    "videoId": "...",
    "title": "...",
    "uploader": "...",
    "duration": 248,
    "url": "https://rr1---sn...googlevideo.com/...",
    "headers": { "User-Agent": "...", "Referer": "..." }
  }
  ```
  The `url` is a direct, signed audio stream. 404 if nothing usable is found.
- `GET /stream?url=<signed googlevideo url>` →
  Relays the audio through this server. googlevideo URLs are signed for this
  server's IP, so devices with different egress (e.g. the Android emulator)
  get a 403 when streaming them directly. Serving the audio from here makes
  the request originate from the signing IP and keeps Range support for
  seeking. Only `googlevideo.com` hosts are accepted.

## Run locally

```sh
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# try it:
curl "http://localhost:8000/resolve?title=الف+ليلة+وليلة&artist=ام+كلثوم"
```

## Deploy (free, no credit card — Koyeb)

1. Create a GitHub repo with these files (`main.py`, `requirements.txt`,
   `Dockerfile`).
2. Sign up at https://app.koyeb.com (no credit card needed).
3. **Create Web Service** → **GitHub** → pick the repo.
4. Region: **Frankfurt** or **Washington D.C.** — Instance: **Free**.
5. Deploy, then copy the URL: `https://<app>-<org>.koyeb.app`
   (verify `GET /health` returns `{"status":"ok"}`).
6. (Optional) Keep it awake: create a free UptimeRobot monitor on `/health`
   every 5 minutes, since the free instance sleeps after 1h of idle traffic.

Then point the Flutter app at it at build time:

```sh
flutter build apk --release --dart-define=RESOLVER_URL=https://<app>-<org>.koyeb.app
```

## Notes

- Best audio (opus/m4a) is selected automatically; ExoPlayer plays it directly.
- Age-restricted/region-locked videos may fail; that's a YouTube policy issue.
- Runs a full yt-dlp extraction per `/resolve` call (~2–6s). Fine for one song
  at a time; don't batch many requests per second.
- Datacenter IPs can occasionally be treated with suspicion by YouTube; the
  local tunnel (`start-backend-tunnel.bat`, residential IP) remains a reliable
  fallback.
