# NasTok

[English](README.md) | [中文](README.zh-CN.md)

> NAS 轻量级短视频浏览器 —— 自托管、多用户、按库授权、可分享的私人媒体库。

NasTok 把 NAS 上散落的视频和图片目录变成类似短视频 App 的浏览体验：沉浸式信息流、收藏、标记、合集、搜索，以及一键生成公开分享链接。后端是单进程 FastAPI + SQLite，前端是零构建的原生 ES Module SPA，一个容器即可跑起来。

---

## 功能特性

- **沉浸式信息流** —— 随机 / 最新排序，稳定分页（`seed` 固定随机序列），仅收藏、仅已打标、最近观看。
- **搜索与库过滤** —— 按标题 / 路径子串搜索；按存储库筛选。
- **多种媒体模式** —— 视频、图片、混合；图片合集按需加载图列。
- **流式播放 + 下载** —— HTTP `Range` / `206` 与 `If-Range`，Safari/iOS 友好；单独下载入口。
- **收藏与标记** —— 每用户独立；默认标记「稍后再看」；`PUT` 全量替换、幂等。
- **公开分享** —— 视频 / 图片 / 合集生成 `/s/{token}`；可设过期、访问密码、打开次数上限；停用后再分享会 **换新 token**。任意登录用户可分享自己有权访问的媒体。
- **按库授权** —— 媒体根下一级子目录自动成为存储库；新建用户默认无库。内置 `admin` 始终拥有全部库。
- **多角色后台** —— 用户 / 存储库 / 审计日志 / 分享记录；SQLite 在线备份 **与恢复**（仅系统管理员）。
- **播放增强** —— 本地续看进度、倍速、同目录 `.vtt`/`.srt` 字幕、Media Session。
- **PWA** —— manifest + Service Worker（壳资源 stale-while-revalidate，`/api/**` 与媒体流不缓存）。iOS/Android 主屏 PNG 图标。
- **安全基线** —— CSP `script-src 'self'`、HttpOnly JWT Cookie、登录失败限流、可选 `Secure` Cookie、接口文档默认关闭。
- **轻量迁移** —— `PRAGMA user_version`（当前 **v3**），幂等可重放。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 · FastAPI 0.115 · SQLAlchemy 2.0 · SQLite（WAL） |
| 鉴权 | PyJWT（HttpOnly Cookie）· PBKDF2-SHA256（26 万次迭代） |
| 前端 | 原生 ES Module SPA（零构建）· `style.css` · PWA |
| 部署 | Docker Hub `fange173/nastok`（linux/amd64）· docker compose |
| CI | GitHub Actions |

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/fange173/nas-tok.git && cd nas-tok
# 把 NAS 上的视频目录挂进来（可选多个）
# 编辑 docker-compose.yml 的 volumes，例如：
#   - /srv/nas/movies:/media/videos/movies
docker compose pull
docker compose up -d
```

或直接拉取镜像：

```bash
docker pull fange173/nastok:latest
```

若要在本地构建而不是拉取：`docker compose up -d --build`。

访问 `http://<NAS-IP>:8080`，使用 **内置系统管理员** 登录：

| | |
|---|---|
| 用户名 | `admin` |
| 密码 | `admin123` |
| 角色 | `sysadmin` |

> **首次登录后请立即在「修改密码」里改掉默认密码。** 若忘记密码，在 `docker-compose.yml` 设 `RESET_ADMIN_PASSWORD=true` 重启一次，登录成功后再改回 `false`。

对局域网以外暴露前，请把 `SECRET_KEY` 改成足够长的随机串。

### 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export MEDIA_ROOT=./media/videos
export DATABASE_URL=sqlite:///./data/nasTok.db

uvicorn app.main:app --reload --port 8080
```

需要在线接口文档时加 `ENABLE_DOCS=true`，访问 `/docs`。

## 配置项

环境变量（见 `docker-compose.yml` 注释与 `.env.example`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SECRET_KEY` | `nastok-dev-secret-change-me` | JWT 签名密钥，**生产必须改为随机长串**（启动会告警，不阻塞） |
| `MEDIA_ROOT` | `/media/videos` | 媒体根目录，一级子目录自动登记为存储库 |
| `DATABASE_URL` | `sqlite:////app/data/nasTok.db` | SQLAlchemy URL |
| `TZ` | `Asia/Shanghai` | 时区 |
| `SCAN_CACHE_TTL` | `300` | 扫描缓存秒数 |
| `MEDIA_ACCESS_CACHE_TTL` | `45` | 路径 ACL 缓存秒数 |
| `LOG_RETENTION_DAYS` | `90` | 审计日志与分享访问明细保留天数 |
| `LOGIN_MAX_FAILURES` | `8` | 同 IP 连续登录失败上限 |
| `LOGIN_LOCK_SECONDS` | `300` | 锁定秒数 |
| `SHARE_VIEW_THROTTLE_SECONDS` | `60` | 分享访问同 IP+token 节流秒数 |
| `TRUST_PROXY_HEADERS` | `false` | 仅在反代后设 `true`（信任 XFF） |
| `COOKIE_SECURE` | `false` | HTTPS 反代后设 `true` |
| `ENABLE_DOCS` | `false` | `true` 时开启 `/docs` `/redoc` `/openapi.json` |
| `RESET_ADMIN_PASSWORD` | `false` | `true` 时重启把内置 admin 密码重置为 `admin123`（用完改回） |

会话时长：普通 24 小时；勾选「记住登录」为 90 天。改密会使其他设备上的旧会话立即失效。

## 角色与权限

| 角色 | 浏览 / 收藏 / 标记 / 分享 | 改名 / 删除 | 用户与存储库 | 数据库备份 / 恢复 |
|---|:---:|:---:|:---:|:---:|
| `user`（普通） | ✅（仅已分配的库） | ❌ | ❌ | ❌ |
| `admin`（管理员） | ✅ | ✅ | 有限 | ❌ |
| `sysadmin`（系统管理员） | ✅ | ✅ | ✅ | ✅ |

- 内置 `admin` 固定为 `sysadmin`，启动时自动拥有全部存储库。
- 新建用户默认无任何存储库。
- 分享 / 改名 / 删除仍校验库 ACL。
- 备份与恢复 **仅 sysadmin**（`POST /api/admin/backup`、`POST /api/admin/restore`）。

## 项目结构

```
app/
├── main.py            # 用户向 API、SPA、健康检查
├── admin.py           # /api/admin/* 用户、库、日志、备份/恢复
├── share.py           # /api/share/* 与 /api/shares
├── auth.py            # JWT Cookie、登录限流、角色
├── database.py        # 模型、迁移（v3）、init_db
├── video_handler.py   # 扫描、ACL、Range 流、改名/删除
├── slog.py            # 一行 JSON 日志
└── static/            # index.html、style.css、PWA、js/*
tests/
scripts/               # 前端冒烟、图标栅格化
docker-compose.yml · Dockerfile · .github/workflows/ci.yml
```

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` `GET` | `/api/auth/login` `/logout` `/change-password` `/me` | 鉴权 |
| `GET` | `/api/videos/list` | 排序、分页、收藏、标记、搜索、库、最近观看 |
| `GET` | `/api/libraries` | 当前用户可见存储库 |
| `GET` | `/api/videos/stream/{path}` | Range / 206 / `?download=1` |
| `GET` | `/api/videos/subtitles/{path}` | 同目录 `.vtt` / `.srt` |
| `POST` | `/api/videos/view` `/refresh` | 观看记录 · 手动刷新（非 staff 有冷却） |
| `PUT` `DELETE` | `/api/videos/edit` `/delete` | staff + ACL |
| `GET` `POST` `DELETE` | `/api/favorites` | 收藏 |
| `*` | `/api/tags` `/api/videos/tags` | 标记 CRUD + 打标 |
| `GET` `PUT` | `/api/albums/images` `/rename` | 合集图片 / 重命名 |
| `POST` `DELETE` | `/api/shares` | 创建 / 取消分享 |
| `GET` `POST` | `/api/share/{token}` `/auth` `/stream` | 公开页、密码解锁、拉流 |
| `POST` | `/api/admin/backup` `/restore` | 系统管理员快照下载 / 恢复 |
| `GET` | `/api/health` | `{ok, db, media}`，库或媒体根异常时 503 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
node scripts/smoke-modules.mjs
```

## 备份

仅系统管理员。Cookie 含 JWT，放私有目录并收紧权限：

```bash
curl -sS -c ~/.nastok-cookies -X POST -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<密码>"}' http://127.0.0.1:8080/api/auth/login
chmod 600 ~/.nastok-cookies
curl -sS -b ~/.nastok-cookies -X POST -OJ http://127.0.0.1:8080/api/admin/backup
```

或容器内用 Python 标准库（镜像无 `sqlite3` CLI）：

```bash
docker exec nas-tok python -c "import sqlite3; sqlite3.connect('/app/data/nasTok.db').backup(sqlite3.connect('/app/data/manual.db'))"
```

恢复：后台「存储库」面板，或 `POST /api/admin/restore` 上传快照文件。

## 安全说明

- 每次真实部署都要改 `SECRET_KEY` 和 `admin` 密码。
- 不要提交 `data/*.db*`（含密码哈希、分享 token、审计日志）。
- 不要把数据库打进镜像。
- 直连 HTTP：`TRUST_PROXY_HEADERS=false`、`COOKIE_SECURE=false`；HTTPS 反代后两者都设 `true`。
- 生产保持 `ENABLE_DOCS=false`。
- 详见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
