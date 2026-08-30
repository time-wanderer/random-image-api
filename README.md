# Random Image API

可长期运行的随机图片服务。根据客户端 `User-Agent` 自动判断 Desktop / Mobile，优先返回横屏或竖屏图片，也支持 `type=desktop` / `type=mobile` 显式指定。

项目按单机、可迁移、可备份恢复的目标设计：FastAPI + SQLite + 本地永久图库 + 可选 WebDAV 扩展图库 + Docker Compose。

## 快速导航

| 目标 | 建议阅读 |
| --- | --- |
| 直接使用已发布镜像 | [Docker Hub 镜像快速部署](#docker-hub-镜像快速部署) |
| 从 GitHub 源码构建 | [从源码部署](#从源码部署) |
| 手工添加本地图片 | [图片添加方式](#图片添加方式) |
| 使用 WebDAV 扩展图库 | [WebDAV Hybrid 与混合压缩包](#webdav-hybrid-与混合压缩包) |
| 导入目录混乱的压缩包 | [导入目录混乱的压缩包](#导入目录混乱的压缩包) |
| 备份或恢复 | [Backup](#backup) / [Restore](#restore) |
| 迁移到新 VPS | [MIGRATION.md](MIGRATION.md) |

## 功能列表

- `GET /random`：返回一张随机图片
- 根据 User-Agent 识别 Windows / macOS / Desktop Linux / Android / iPhone 等客户端
- Desktop 优先横屏，Mobile 优先竖屏
- 显式参数 `?type=desktop` / `?type=mobile` 优先于 User-Agent
- 支持 `.jpg` / `.jpeg` / `.png` / `.webp`
- 按图片宽高自动分类，正方形图片默认两边都可用
- Desktop 或 Mobile 缺失时自动 fallback 到另一侧
- `GET /health`：服务、数据库、图片数量
- 访问日志：时间、路径、客户端类型、状态码、返回图片、耗时、代理感知 IP
- SQLite 元数据缓存，避免每次请求全盘扫描
- Docker Compose 部署、bind mount 持久化、Backup / Restore
- 默认本地模式；可选 WebDAV Hybrid，默认 90% 优先远程并支持故障降级
- WebDAV 只同步轻量索引，远程图片按需缓存、条件刷新、随机轮换和 LRU 淘汰
- 安全导入 ZIP / TAR.GZ / TGZ 混合图片包，按真实方向分类并去重

## 技术架构

```text
Client
  -> Docker Compose (api)
    -> Uvicorn + FastAPI
      -> 本地永久图库 (data/images/)
      -> SQLite 本地与远程索引 (data/database/images.db)
      -> 可选 WebDAV desktop/ + mobile/
      -> 有界远程缓存 (data/cache/webdav/)
```

- Web：Python 3.12、FastAPI、Uvicorn
- 图片尺寸：Pillow `Image.size`
- 元数据：SQLite WAL
- 部署：单服务 Docker Compose
- 持久化：`./data` bind mount，不把生产图片打进镜像

不引入 Redis、Celery、PostgreSQL、Kubernetes 或多实例共享存储。当前业务是单机随机读图，SQLite 足够。

## 项目目录

```text
random-image-api/
├── app/
│   ├── main.py            # FastAPI 入口
│   ├── catalog.py         # 扫描、分类、随机选择、fallback
│   ├── db.py              # SQLite
│   ├── webdav.py          # WebDAV 索引、下载、缓存与降级
│   ├── importer.py        # ZIP / TAR.GZ 安全导入
│   ├── ua.py              # User-Agent 识别
│   └── config.py          # 环境变量配置
├── tests/
├── data/
│   ├── images/
│   │   ├── desktop/
│   │   └── mobile/
│   ├── database/
│   ├── cache/webdav/
│   └── logs/
├── backups/
├── scripts/
│   ├── backup.sh
│   ├── restore.sh
│   └── generate_samples.py
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── MIGRATION.md
└── REPORT.md
```

## 环境要求

正式部署只需要：

- Linux VPS
- Docker Engine
- Docker Compose Plugin

开发 / 本地验证还可以使用 Python 3.12+。

不要在宿主机单独安装业务数据库。不要依赖当前机器的绝对路径、固定 IP 或固定域名。

## Docker Hub 镜像快速部署

适合只想运行服务、不需要修改源码的用户。镜像为 `linux/amd64`：

```bash
mkdir -p random-image-api/data/images/desktop \
  random-image-api/data/images/mobile \
  random-image-api/data/database \
  random-image-api/data/cache/webdav \
  random-image-api/data/logs
cd random-image-api
# 镜像以 UID/GID 1000 非 Root 运行，数据目录必须可写
sudo chown -R 1000:1000 data
```

创建 `compose.yml`：

```yaml
services:
  api:
    image: qinlingmonkey/random-image-api:v1
    restart: unless-stopped
    ports:
      - "10086:10086"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10086/health', timeout=4)"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 15s
```

创建最小 `.env`，并生成不可预测的管理令牌：

```bash
printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
chmod 600 .env
docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

然后把图片复制到 `data/images/desktop/` 或 `data/images/mobile/`。程序最终仍以图片真实宽高分类；目录名称主要方便人工管理。等待自动扫描，或读取 `.env` 后调用管理接口：

```bash
set -a; . ./.env; set +a
curl -fsS -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/rescan
unset ADMIN_TOKEN
curl -D - -o random-image.bin http://127.0.0.1:10086/random
```

完整的镜像部署、WebDAV 和故障排查教程见 [DOCKERHUB_OVERVIEW.md](DOCKERHUB_OVERVIEW.md)。

## 从源码部署

适合需要修改代码、运行测试或自行构建镜像的用户：

```bash
cp .env.example .env
# 按需修改 APP_PORT、LOG_LEVEL、ADMIN_TOKEN
python3 scripts/generate_samples.py   # 可选：生成演示图片
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f
```

浏览器或 curl 访问：

```text
http://<主机>:<APP_PORT>/health
http://<主机>:<APP_PORT>/random
```

`docker-compose.yml` 会把容器内路径固定为 `/app/data`，并把宿主机 `./data` 挂进去。因此 `.env` 里即使写 `./data`，容器内仍使用 `/app/data`。

## 构建完整 Docker 镜像文件

GitHub 仓库保存用于维护和重新构建的**源码底本**。Docker 镜像归档体积较大，不建议提交到 Git。在安装了 Docker Engine 的机器上执行：

```bash
./scripts/build-image.sh
```

默认生成：

```text
dist/random-image-api-local.tar
dist/random-image-api-local.tar.sha256
```

也可以指定镜像名称、标签和输出路径：

```bash
IMAGE_NAME=random-image-api \
IMAGE_TAG=v1.0.0 \
OUTPUT="$PWD/dist/random-image-api-v1.0.0.tar" \
./scripts/build-image.sh
```

在另一台服务器加载：

```bash
sha256sum -c dist/random-image-api-v1.0.0.tar.sha256
docker load -i dist/random-image-api-v1.0.0.tar
```

镜像只包含应用代码和 Python 依赖，**不包含** `.env`、真实图片、SQLite 数据库、日志或备份。生产数据仍通过 `./data:/app/data` 持久化。

## .env 配置

参考 `.env.example`：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `IMAGE_NAME` | Docker 镜像名称 | `random-image-api` |
| `IMAGE_TAG` | Docker 镜像标签 | `local` |
| `APP_PORT` | 宿主机映射端口 | `10086` |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_BIND_PORT` | 容器内端口，保持 `10086` | `10086` |
| `DATA_DIR` | 数据根目录 | `./data` |
| `IMAGES_DIR` | 图片目录 | `./data/images` |
| `DATABASE_PATH` | SQLite 文件 | `./data/database/images.db` |
| `LOG_DIR` | 日志目录 | `./data/logs` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `ADMIN_TOKEN` | 可选管理令牌，空则关闭 `/admin/rescan` | 空 |
| `FALLBACK_ENABLED` | 一侧无图时是否回退到另一侧 | `true` |
| `SQUARE_POLICY` | 正方形图片归属：`both` / `desktop` / `mobile` | `both` |
| `SCAN_ON_STARTUP` | 启动时扫描图片 | `true` |
| `SCAN_INTERVAL_SECONDS` | 后台扫描间隔 | `300` |
| `MAX_PICK_RETRIES` | 损坏/缺失文件重试次数 | `8` |
| `TRUSTED_PROXY_HEADERS` | 是否信任 `X-Forwarded-For` / `X-Real-IP` | `true` |
| `STORAGE_MODE` | 存储模式：`local` / `hybrid` | `local` |
| `HYBRID_REMOTE_PROBABILITY` | Hybrid 优先选择 WebDAV 的概率 | `0.9` |
| `WEBDAV_BASE_URL` | HTTPS WebDAV 根 URL | 空 |
| `WEBDAV_DESKTOP_ROOT` | WebDAV 横图目录 | `/desktop/` |
| `WEBDAV_MOBILE_ROOT` | WebDAV 竖图目录 | `/mobile/` |
| `WEBDAV_ALLOWED_HOSTS` | 允许访问的 WebDAV 主机，逗号分隔 | 空 |
| `WEBDAV_SYNC_INTERVAL_SECONDS` | 远程轻量索引同步周期 | `300` |
| `CACHE_MAX_BYTES` | WebDAV 缓存容量上限 | `1073741824` |
| `CACHE_MAX_FILES` | WebDAV 缓存文件数上限 | `2000` |
| `CACHE_REFRESH_AFTER_SECONDS` | 缓存条件刷新间隔 | `3600` |
| `CACHE_ROTATE_PERCENT` | 每轮随机轮换比例 | `10` |

`.env` 不进 Git。不要把真实 Token、WebDAV 用户名或密码写进 README、镜像、数据库或日志。

## GitHub 与隐私安全

公开或私有 GitHub 仓库均只提交源码、Dockerfile、Compose 配置、脚本和文档。以下内容已由 `.gitignore` 排除，不应使用 `git add -f` 强制添加：

- `.env`、`.a0proj/` 和编辑器配置；
- `data/images/` 中的真实图片；
- SQLite 数据库、WAL / SHM 文件；
- 访问日志与 Backup；
- `dist/` 下的 Docker 镜像 tar 和校验文件。

首次推送前执行：

```bash
git status
git ls-files
git grep -nI -E 'password|secret|api[_-]?key|BEGIN .*PRIVATE' -- . ':!.git'
```

建议在 GitHub 设置中启用邮箱隐私，并使用 GitHub 提供的 `users.noreply.github.com` 邮箱作为 Git 提交邮箱。不要在仓库、Issue 或日志中发布 VPS IP、SSH 用户名、密码、Token、私人照片或完整访问日志。

## 第一次启动

1. 复制项目到目标机器。
2. `cp .env.example .env` 并检查端口。
3. 把图片放到 `data/images/`，可按 desktop/mobile 分子目录，也可以平铺，程序会按宽高分类。
4. `docker compose up -d --build`
5. 访问 `/health`，确认 `status=ok` 且图片数量正确。

## 停止、重启、状态、日志、更新

```bash
docker compose down          # 停止，不删除 ./data
docker compose restart
docker compose ps
docker compose logs
docker compose logs -f
docker compose up -d --build           # 更新代码后重建
docker compose up -d --force-recreate  # 配置变化后重建容器
```

不要把 `docker compose down -v` 当作日常命令。本项目用 bind mount，数据主要在 `./data`，但 `-v` 仍可能误删其他匿名卷。

## 图片添加方式

1. 把 `.jpg` / `.jpeg` / `.png` / `.webp` 放到 `data/images/`。
2. 可选分子目录 `desktop/`、`mobile/`，仅方便人工管理，最终分类以宽高为准。
3. 等待最多 `SCAN_INTERVAL_SECONDS`，或在设置了 `ADMIN_TOKEN` 后调用：

```bash
curl -X POST -H "X-Admin-Token: <token>" http://127.0.0.1:10086/admin/rescan
```

分类规则：

- `width > height` → desktop / landscape
- `height > width` → mobile / portrait
- `width == height` → square，默认 desktop 和 mobile 都可返回

## WebDAV Hybrid 与混合压缩包

默认 `STORAGE_MODE=local`，现有本地图片方式不变：用户放入 `data/images/` 的图片属于永久图库，不会被缓存维护删除。进阶模式使用 WebDAV 扩展图库：

```text
<WEBDAV_BASE_URL>/desktop/   横屏图片
<WEBDAV_BASE_URL>/mobile/    竖屏图片
```

应用通过 `PROPFIND` 只同步路径、大小、ETag 和修改时间等轻量索引，不会预先下载整个远程图库。图片被选中时才下载到独立的 `data/cache/webdav/`；下载后会用 Pillow 校验真实格式、EXIF 方向和宽高。

### 启用 WebDAV

在 `.env` 中配置：

```env
STORAGE_MODE=hybrid
HYBRID_REMOTE_PROBABILITY=0.9
WEBDAV_BASE_URL=https://dav.example.com/random-image-api
WEBDAV_DESKTOP_ROOT=/desktop/
WEBDAV_MOBILE_ROOT=/mobile/
WEBDAV_ALLOWED_HOSTS=dav.example.com
WEBDAV_USERNAME=专用只读账号
WEBDAV_PASSWORD=应用密码
```

要求使用 HTTPS，并建议账号仅有图库目录读取权限。真实凭据只能放在未跟踪的 `.env`，不能写入仓库、日志或文档。配置变化后执行：

```bash
docker compose up -d --force-recreate
curl -sS http://127.0.0.1:10086/health
```

### 90% 远程优先与降级

- 默认每次请求有 90% 概率优先 WebDAV、10% 概率优先本地永久图库；本地为空时仍会继续尝试远程。
- WebDAV 缓存命中时直接返回缓存；未命中或需要更新时进行受限下载。
- WebDAV 返回 401/403、连接或读取超时、5xx、非法内容或下载失败时，立即从同方向的“有效 WebDAV 缓存 + 本地永久图片”联合池降级选择。
- 同方向为空且 `FALLBACK_ENABLED=true` 时，再尝试另一方向联合池。
- 远程正常但图库和本地均为空时返回 404；远程故障且没有任何可降级图片时返回 503。

响应头 `X-Image-Source` 为 `local`、`webdav-live` 或 `webdav-cache`；`X-Remote-Fallback-Used: true` 表示发生了远程故障降级。

### 缓存更新机制

缓存不会永久固定：

1. 超过 `CACHE_REFRESH_AFTER_SECONDS` 后，下次命中使用 ETag / Last-Modified 条件请求；远端未变时以 304 更新状态。
2. 后台每轮维护随机标记 `CACHE_ROTATE_PERCENT` 的缓存，使冷门项也能在后续访问时重新验证。
3. 超过 `CACHE_MAX_BYTES` 或 `CACHE_MAX_FILES` 时按近似 LRU 淘汰旧缓存。
4. 下载先写临时文件，校验后原子替换，不会提供半文件。
5. WebDAV 故障不会全清缓存；`data/images/` 永久本地图永不参与缓存淘汰。

设置 `ADMIN_TOKEN` 后可手动操作：

```bash
curl -sS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:10086/admin/webdav/sync

curl -sS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:10086/admin/cache/maintain
```

`/health` 会增加脱敏后的 `webdav` 和 `cache` 状态。

### 导入目录混乱的压缩包

支持 ZIP、TAR.GZ 和 TGZ。归档里的目录与文件名可以不统一；导入器按图片真实视觉宽高分类，并以内容 SHA-256 安全命名和去重。

```bash
mkdir -p data/imports
cp /path/to/gallery.zip data/imports/

# 仅预览，不写文件
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip --output-dir /app/data/images --dry-run

# 正式导入
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip --output-dir /app/data/images

# 让常驻服务立即重扫
docker compose up -d
```

结果进入 `data/images/desktop/` 和 `data/images/mobile/`。导入器拒绝绝对路径、`..`、链接、设备文件、解压炸弹、超限成员及 Pillow 像素炸弹；重复图片跳过，不会修改 SQLite 或调用 Restore。确认成功后再自行删除原压缩包。

## API 文档

### `GET /health`

```json
{
  "status": "ok",
  "service": "random-image-api",
  "version": "1.0.0",
  "images": {
    "total": 5,
    "desktop": 2,
    "mobile": 2,
    "square": 1
  },
  "database": "ok",
  "fallback_enabled": true,
  "square_policy": "both",
  "last_scan_at": "2026-08-16T10:49:59+00:00",
  "last_scan_error": null
}
```

数据库异常时 `status` 为 `degraded`，`database` 为 `error`。

### `GET /random`

成功时直接返回图片二进制：

- `Content-Type`: `image/jpeg` / `image/png` / `image/webp`
- `X-Client-Type`: 本次使用的目标类型
- `X-Image-File`: 相对 `IMAGES_DIR` 的路径
- `X-Image-Orientation`: `desktop` / `mobile` / `square`
- `X-Image-Width` / `X-Image-Height`
- `X-Fallback-Used`: `true` / `false`
- `X-Image-Source`: `local` / `webdav-live` / `webdav-cache`
- `X-Remote-Fallback-Used`: WebDAV 故障时是否使用缓存/本地联合池降级

常见错误：

| 情况 | HTTP |
| --- | --- |
| `type=test` 等非法参数 | `400` |
| 没有任何可用图片 | `404` |
| 数据库不可用 | `503` |

### 管理接口

仅当 `ADMIN_TOKEN` 非空时启用，均要求请求头 `X-Admin-Token`：

| 接口 | 用途 |
| --- | --- |
| `POST /admin/rescan` | 立即重扫本地永久图库 |
| `POST /admin/webdav/sync` | 立即同步 WebDAV 轻量索引 |
| `POST /admin/cache/maintain` | 立即执行缓存轮换标记与容量淘汰 |

`ADMIN_TOKEN` 为空时管理接口返回 404；令牌错误时返回 401。修改 `.env` 后使用 `docker compose up -d --force-recreate`，仅执行 `restart` 不会重新载入环境变量。

## Curl 示例

```bash
curl -i http://127.0.0.1:10086/health

curl -D - -o /tmp/random.bin http://127.0.0.1:10086/random

curl -D - -o /tmp/win.jpg \
  -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36' \
  http://127.0.0.1:10086/random

curl -D - -o /tmp/android.webp \
  -H 'User-Agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36' \
  http://127.0.0.1:10086/random

curl -D - -o /tmp/iphone.bin \
  -H 'User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' \
  http://127.0.0.1:10086/random

curl -D - -o /tmp/force-desktop.jpg \
  -H 'User-Agent: Mozilla/5.0 (Linux; Android 10; K) Mobile Safari/537.36' \
  'http://127.0.0.1:10086/random?type=desktop'

curl -i 'http://127.0.0.1:10086/random?type=test'
```

## 数据目录

必须持久化、必须随项目一起迁移：

- `data/images/`：用户手工添加或压缩包导入的本地永久图片，必须备份和迁移
- `data/database/images.db`：本地与 WebDAV 索引元数据；WAL 模式下还可能出现 `images.db-wal` / `images.db-shm`
- `data/cache/webdav/`：按需下载的 WebDAV 可重建缓存，有容量上限，默认不备份
- `data/logs/`：访问日志，可按需清理，不作为核心数据

不要只把图片打进 Docker Image。容器删除后，只要 `./data` 还在，重建即可恢复。本地永久图片和 WebDAV 缓存严格分离，缓存维护绝不会删除 `data/images/`。

## Backup

```bash
./scripts/backup.sh
```

生成：

```text
backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
```

备份内容：

- `data/images/` 中的全部本地永久图片
- 使用 SQLite `VACUUM INTO` 得到的一致性数据库快照（含本地与远程索引）
- `.env.example`
- 脱敏后的环境变量副本（`ADMIN_TOKEN`、WebDAV 用户名和密码等敏感值会被清空）

不会覆盖历史备份；明确排除可从 WebDAV 重建的 `data/cache/`、venv、热日志和无意义目录。WebDAV 远程图库本体必须使用 WebDAV 服务商的快照、版本或备份功能另行保护。

SQLite 不要在服务写入时直接 `cp images.db`。本脚本使用官方推荐的 `VACUUM INTO`，生成独立一致快照，不依赖复制 `-wal` / `-shm`。

## Restore

```bash
# 先预览，不会改数据
./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz

# 确认后执行
RESTORE_CONFIRM=YES ./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz

docker compose up -d
docker compose ps
curl -i http://127.0.0.1:10086/health
curl -D - -o /tmp/restored.bin http://127.0.0.1:10086/random
```

恢复前会把现有 `data/images` 和 `data/database` 复制到 `backups/pre-restore-<时间>/`。

## 日常维护

- 加图：复制到 `data/images/`，等待扫描或调用 `/admin/rescan`
- 看状态：`docker compose ps` + `curl /health`
- 看日志：`docker compose logs -f` 或 `data/logs/access.log`
- 定期 `./scripts/backup.sh`，把 `backups/*.tar.gz` 拷到另一台机器或对象存储
- 更新：`git pull` 后 `docker compose up -d --build`

## 常见问题

**`/random` 返回 404**
图片目录为空，或文件都损坏 / 格式不受支持。先看 `/health` 的 `images.total`。

**Android 访问却拿到横图**
检查是否带了 `?type=desktop`。显式 type 一定优先。

**改完图片数量不变**
等扫描间隔，或配置 `ADMIN_TOKEN` 后手动 rescan。

**容器重建后图片没了**
确认启动时使用项目目录里的 `docker compose`，并且 `./data` 没有被删。不要用一次性 `docker run` 且不挂卷。

**如何迁移到新 VPS**
见 [MIGRATION.md](MIGRATION.md)。

## 本地开发

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_samples.py
python3 -m pytest tests -q
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 10086
```
