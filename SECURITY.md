# Security

## Default credentials

A fresh install creates:

| Field | Value |
|---|---|
| Username | `admin` |
| Password | `admin123` |
| Role | `sysadmin` |

Change this password immediately after first login. Do not expose port 8080 to the public internet with the default password.

Forgotten password: set `RESET_ADMIN_PASSWORD=true`, restart once, log in, change the password, then set it back to `false`.

## Production checklist

- Set `SECRET_KEY` to a long random string (32+ characters). The process prints a warning and still starts if you leave the default.
- Keep `ENABLE_DOCS=false` unless you need `/docs` on a private network.
- Direct HTTP on a LAN: `TRUST_PROXY_HEADERS=false`, `COOKIE_SECURE=false`.
- HTTPS reverse proxy: `TRUST_PROXY_HEADERS=true`, `COOKIE_SECURE=true`.
- Never commit `data/*.db*` — the file contains password hashes, share tokens, and audit logs.
- Do not bake a database into the Docker image.

## Git history

Older commits in this repository still contain a copy of `data/nasTok.db` (default `admin` / `admin123` hash only). The file is no longer tracked. If you publish a **new** GitHub repo, prefer a clean history (orphan branch or `git filter-repo`) so that blob is not reachable. If this history was ever public, treat `admin123` as compromised and rotate the password anyway.

Commit author emails in `git log` are also part of history. Use a public-facing address for future commits if you do not want a personal mailbox on GitHub.

## Reporting

Please open a private GitHub security advisory, or open an issue without including secrets, hashes, or personal media paths.
