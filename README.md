# Random Image API

可长期运行的随机图片服务。根据客户端 `User-Agent` 自动判断 Desktop / Mobile，优先返回横屏或竖屏图片，也支持 `type=desktop` / `type=mobile` 显式指定。

项目按单机、可迁移、可备份恢复的目标设计：一个 FastAPI 进程 + SQLite 元数据 + 本地图片目录 + Docker Compose。

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

## 技术架构

```text
Client
  -> Docker Compose (api)
    -> Uvicorn + FastAPI
      -> 内存图片目录缓存
      -> SQLite (data/database/images.db)
      -> 图片文件 (data/images/)
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
│   ├── ua.py              # User-Agent 识别
│   └── config.py          # 环境变量配置
├── tests/
├── data/
│   ├── images/
│   │   ├── desktop/
│   │   └── mobile/
│   ├── database/
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

## Docker Compose 部署

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
| `APP_PORT` | 宿主机映射端口 | `8080` |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_BIND_PORT` | 容器内端口，保持 `8080` | `8080` |
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

`.env` 不进 Git。不要把真实 Token 写进 README 或镜像。

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
curl -X POST -H "X-Admin-Token: <token>" http://127.0.0.1:8080/admin/rescan
```

分类规则：

- `width > height` → desktop / landscape
- `height > width` → mobile / portrait
- `width == height` → square，默认 desktop 和 mobile 都可返回

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

常见错误：

| 情况 | HTTP |
| --- | --- |
| `type=test` 等非法参数 | `400` |
| 没有任何可用图片 | `404` |
| 数据库不可用 | `503` |

### `POST /admin/rescan`

仅当 `ADMIN_TOKEN` 非空时启用。请求头：`X-Admin-Token`。

## Curl 示例

```bash
curl -i http://127.0.0.1:8080/health

curl -D - -o /tmp/random.bin http://127.0.0.1:8080/random

curl -D - -o /tmp/win.jpg \
  -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36' \
  http://127.0.0.1:8080/random

curl -D - -o /tmp/android.webp \
  -H 'User-Agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36' \
  http://127.0.0.1:8080/random

curl -D - -o /tmp/iphone.bin \
  -H 'User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' \
  http://127.0.0.1:8080/random

curl -D - -o /tmp/force-desktop.jpg \
  -H 'User-Agent: Mozilla/5.0 (Linux; Android 10; K) Mobile Safari/537.36' \
  'http://127.0.0.1:8080/random?type=desktop'

curl -i 'http://127.0.0.1:8080/random?type=test'
```

## 数据目录

必须持久化、必须随项目一起迁移：

- `data/images/`：生产图片
- `data/database/images.db`：SQLite 元数据；WAL 模式下还可能出现 `images.db-wal` / `images.db-shm`
- `data/logs/`：访问日志，可按需清理，不作为核心数据

不要只把图片打进 Docker Image。容器删除后，只要 `./data` 还在，重建即可恢复。

## Backup

```bash
./scripts/backup.sh
```

生成：

```text
backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
```

备份内容：

- 全部图片
- 使用 SQLite `VACUUM INTO` 得到的一致性数据库快照
- `.env.example`
- 脱敏后的环境变量副本（`ADMIN_TOKEN` 等敏感值会被清空）

不会覆盖历史备份，也不会打包 cache、venv、日志热文件以外的无意义目录。

SQLite 不要在服务写入时直接 `cp images.db`。本脚本使用官方推荐的 `VACUUM INTO`，生成独立一致快照，不依赖复制 `-wal` / `-shm`。

## Restore

```bash
# 先预览，不会改数据
./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz

# 确认后执行
RESTORE_CONFIRM=YES ./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz

docker compose up -d
docker compose ps
curl -i http://127.0.0.1:8080/health
curl -D - -o /tmp/restored.bin http://127.0.0.1:8080/random
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
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```
