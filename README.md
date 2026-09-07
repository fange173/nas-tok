# NasTok

[English](README.md) | [中文](README.zh-CN.md)

> A self-hosted, TikTok-style browser for videos and photos on your NAS.

NasTok turns folders of clips and pictures into an immersive feed: shuffle or newest-first, favorites, tags, albums, search, and one-click public share links. The backend is a single FastAPI + SQLite process. The frontend is a zero-build native ES module SPA. One container is enough.

---

## Features

- **Immersive feed** — random or newest order, stable pagination (`seed` keeps the shuffle sequence), favorites, tagged-only, recently watched.
- **Search and library filter** — substring match on title/path; chip/select to stay inside one library.
- **Media modes** — video, photos, or mixed; photo albums load their images on demand.
- **Streaming + download** — HTTP `Range` / `206` and `If-Range` for Safari/iOS; separate download action.
- **Favorites and tags** — per-user; default tag `稍后再看` (“watch later”); `PUT` replaces the tag set atomically.
- **Public shares** — video, photo, or album at `/s/{token}`; optional expiry, password, and max opens; revoking then sharing again issues a **new** token. Any signed-in user can share media they are allowed to see.
- **Library ACL** — first-level folders under the media root become libraries; new users get none until a sysadmin assigns them. The built-in `admin` account always has every library.
- **Admin** — users, libraries, audit log, share records; SQLite online backup **and restore** (sysadmin).
- **Player extras** — resume position (local), playback speed, sidecar `.vtt`/`.srt` subtitles, Media Session keys.
- **PWA** — manifest + service worker (shell stale-while-revalidate; `/api/**` and media never cached). PNG icons for iOS/Android home screen.
- **Security baseline** — CSP `script-src 'self'`, HttpOnly JWT cookie, login lockout, optional `Secure` cookie, `/docs` off by default.
- **Schema migrations** — `PRAGMA user_version` (currently **v3**), idempotent.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11 · FastAPI 0.115 · SQLAlchemy 2.0 · SQLite (WAL) |
| Auth | PyJWT (HttpOnly cookie) · PBKDF2-SHA256 (260k iterations) |
| Frontend | Native ES modules (no bundler) · `style.css` · PWA |
| Deploy | Docker Hub `fange173/nastok` (linux/amd64) · docker compose |
| CI | GitHub Actions |

## Quick start

### Docker Compose (recommended)

```bash
git clone https://github.com/fange173/nas-tok.git && cd nas-tok
# Mount NAS folders (optional, as many as you need):
# edit docker-compose.yml volumes, e.g.
#   - /srv/nas/movies:/media/videos/movies
docker compose pull
docker compose up -d
```

Or pull the image directly:

```bash
docker pull fange173/nastok:latest
```

To build locally instead of pulling: `docker compose up -d --build`.

Open `http://<NAS-IP>:8080` and sign in with the **built-in system administrator**:

| | |
|---|---|
| Username | `admin` |
| Password | `admin123` |
| Role | `sysadmin` |

> Change this password under **Change password** right after the first login. If you get locked out, set `RESET_ADMIN_PASSWORD=true` in `docker-compose.yml`, restart once, sign in, then set it back to `false`.

Set `SECRET_KEY` to a long random string before exposing the app beyond your LAN.

### Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export MEDIA_ROOT=./media/videos
export DATABASE_URL=sqlite:///./data/nasTok.db

uvicorn app.main:app --reload --port 8080
```

Set `ENABLE_DOCS=true` for `/docs`.

## Configuration

Environment variables (see comments in `docker-compose.yml` and `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | `nastok-dev-secret-change-me` | JWT signing key. **Must** be a long random value in production (startup warns, does not block). |
| `MEDIA_ROOT` | `/media/videos` | Media root. First-level subfolders become libraries. |
| `DATABASE_URL` | `sqlite:////app/data/nasTok.db` | SQLAlchemy URL |
| `TZ` | `Asia/Shanghai` | Timezone |
| `SCAN_CACHE_TTL` | `300` | Filesystem scan cache (seconds) |
| `MEDIA_ACCESS_CACHE_TTL` | `45` | Path ACL cache (seconds) |
| `LOG_RETENTION_DAYS` | `90` | Audit log + share-view retention |
| `LOGIN_MAX_FAILURES` | `8` | Failed logins per IP before lock |
| `LOGIN_LOCK_SECONDS` | `300` | Lock duration |
| `SHARE_VIEW_THROTTLE_SECONDS` | `60` | Share view log throttle per IP+token |
| `TRUST_PROXY_HEADERS` | `false` | Trust `X-Forwarded-For` only behind a reverse proxy |
| `COOKIE_SECURE` | `false` | Set `true` on HTTPS so the session cookie is Secure |
| `ENABLE_DOCS` | `false` | Enable `/docs` `/redoc` `/openapi.json` |
| `RESET_ADMIN_PASSWORD` | `false` | One-shot reset of built-in `admin` to `admin123` |

Session length: 24 hours, or 90 days if “Remember me” is checked. Changing the password invalidates other devices.

## Roles

| Role | Browse / favorite / tag / share | Rename / delete | Users & libraries | DB backup / restore |
|---|:---:|:---:|:---:|:---:|
| `user` | ✅ (assigned libraries only) | ❌ | ❌ | ❌ |
| `admin` | ✅ | ✅ | limited | ❌ |
| `sysadmin` | ✅ | ✅ | ✅ | ✅ |

- Built-in `admin` is always `sysadmin` and is granted every library on startup.
- New users start with **no** libraries.
- Share / rename / delete still require library ACL on the file path.
- Backup and restore are **sysadmin only** (`POST /api/admin/backup`, `POST /api/admin/restore`).

## Layout

```
app/
├── main.py            # User APIs, SPA, health
├── admin.py           # /api/admin/* users, libraries, logs, backup/restore
├── share.py           # /api/share/* and /api/shares
├── auth.py            # JWT cookie, login lockout, roles
├── database.py        # Models, migrations (v3), init_db
├── video_handler.py   # Scan, ACL, Range streaming, rename/delete
├── slog.py            # One-line JSON logs
└── static/            # index.html, style.css, PWA, js/*
tests/
scripts/               # frontend smoke + icon rasterizer
docker-compose.yml · Dockerfile · .github/workflows/ci.yml
```

## API (overview)

| Method | Path | Notes |
|---|---|---|
| `POST` `GET` | `/api/auth/login` `/logout` `/change-password` `/me` | Auth |
| `GET` | `/api/videos/list` | sort, paging, favorites, tags, `q`, `library_id`, `recent` |
| `GET` | `/api/libraries` | Libraries the current user can see |
| `GET` | `/api/videos/stream/{path}` | Range / 206 / `?download=1` |
| `GET` | `/api/videos/subtitles/{path}` | Sibling `.vtt` / `.srt` |
| `POST` | `/api/videos/view` `/refresh` | View log · rescan (cooldown for non-staff) |
| `PUT` `DELETE` | `/api/videos/edit` `/delete` | Staff + ACL |
| `GET` `POST` `DELETE` | `/api/favorites` | Favorites |
| `*` | `/api/tags` `/api/videos/tags` | Tag CRUD + assign |
| `GET` `PUT` | `/api/albums/images` `/rename` | Album images / rename |
| `POST` `DELETE` | `/api/shares` | Create / revoke share |
| `GET` `POST` | `/api/share/{token}` `/auth` `/stream` | Public page, password unlock, stream |
| `POST` | `/api/admin/backup` `/restore` | Sysadmin snapshot download / restore |
| `GET` | `/api/health` | `{ok, db, media}` — 503 if DB or media root is down |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
node scripts/smoke-modules.mjs
```

## Backup

Sysadmin only. Cookie contains a JWT — keep it private:

```bash
curl -sS -c ~/.nastok-cookies -X POST -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<password>"}' http://127.0.0.1:8080/api/auth/login
chmod 600 ~/.nastok-cookies
curl -sS -b ~/.nastok-cookies -X POST -OJ http://127.0.0.1:8080/api/admin/backup
```

Or inside the container (no `sqlite3` CLI in the image):

```bash
docker exec nas-tok python -c "import sqlite3; sqlite3.connect('/app/data/nasTok.db').backup(sqlite3.connect('/app/data/manual.db'))"
```

Restore: sysadmin UI on the Libraries panel, or `POST /api/admin/restore` with the snapshot file.

## Security notes

- Change `SECRET_KEY` and the `admin` password on every real deploy.
- Do not commit `data/*.db*` (password hashes, share tokens, audit logs).
- Do not ship a database inside the image.
- Direct HTTP: `TRUST_PROXY_HEADERS=false`, `COOKIE_SECURE=false`. HTTPS proxy: both `true`.
- Keep `ENABLE_DOCS=false` in production.
- See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
