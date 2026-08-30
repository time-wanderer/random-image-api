# Random Image API

一个基于 FastAPI、Uvicorn、Pillow 和 SQLite 的轻量随机图片 API。默认使用本地永久图库，也可以启用 WebDAV Hybrid：约 90% 请求优先远程图库，远程故障时自动降级到有效缓存与本地图片。

## 主要功能

- `GET /random` 返回随机图片
- 根据 User-Agent 自动识别 Desktop / Mobile
- Desktop 优先横屏，Mobile 优先竖屏
- 支持 `?type=desktop`、`?type=mobile` 显式指定
- 支持 JPG、JPEG、PNG、WebP
- 正方形图片默认可用于 Desktop 和 Mobile
- 默认本地永久图库，容器重建不会删除图片
- 可选 WebDAV `desktop/`、`mobile/` 目录扩展图库
- Hybrid 默认 90% 优先 WebDAV
- WebDAV 只同步轻量索引，不批量下载完整图库
- 远程图片按需缓存，支持 ETag / Last-Modified 更新、随机轮换和近似 LRU 淘汰
- WebDAV 401/403、超时或 5xx 时降级到有效缓存与本地图片
- 安全导入 ZIP、TAR.GZ、TGZ 混合图片包，按真实方向分类和去重
- SQLite 元数据、健康检查、访问日志、Backup / Restore
- 应用进程以 UID 1000 非 Root 用户运行

## 镜像信息

| 项目 | 值 |
| --- | --- |
| 镜像 | `qinlingmonkey/random-image-api` |
| 稳定标签 | `v1` |
| 平台 | `linux/amd64` |
| 容器端口 | `10086` |
| 数据目录 | `/app/data` |

快速拉取：

```bash
docker pull qinlingmonkey/random-image-api:v1
```

生产环境推荐使用下面的 Docker Compose，而不是单独 `docker run`。

## 一、最快启动：本地图库模式

### 1. 创建目录

```bash
mkdir -p random-image-api/data/images/desktop \
  random-image-api/data/images/mobile \
  random-image-api/data/database \
  random-image-api/data/cache/webdav \
  random-image-api/data/logs
cd random-image-api
```

容器入口会把挂载的数据目录调整给 UID/GID 1000。若宿主机安全策略阻止容器修改权限，可提前执行：

```bash
sudo chown -R 1000:1000 data
```

### 2. 创建 `compose.yml`

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

如果宿主机的 `10086` 已占用，只改端口左侧，例如：

```yaml
ports:
  - "18086:10086"
```

此时宿主机访问端口变为 `18086`，容器内部仍是 `10086`。

### 3. 创建 `.env`

生成随机管理令牌：

```bash
printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
chmod 600 .env
```

最小 `.env` 只需要 `ADMIN_TOKEN`。镜像的其他默认值为：

```env
STORAGE_MODE=local
FALLBACK_ENABLED=true
SQUARE_POLICY=both
SCAN_ON_STARTUP=true
SCAN_INTERVAL_SECONDS=300
LOG_LEVEL=INFO
```

不要把 `.env` 上传到 GitHub，不要把 Token 或 WebDAV 密码写入 Compose、镜像、截图或日志。

### 4. 启动并检查

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 api
curl -fsS http://127.0.0.1:10086/health
```

首次图库为空时，`/health` 正常返回，但 `/random` 会返回 HTTP 404。这不是服务故障，需要先添加图片。

## 二、添加本地永久图片

将图片复制到：

```text
data/images/desktop/
data/images/mobile/
```

支持格式：

```text
.jpg  .jpeg  .png  .webp
```

目录名称方便人工管理，最终分类以 Pillow 读取的真实视觉宽高为准：

- `width > height`：Desktop / Landscape
- `height > width`：Mobile / Portrait
- `width == height`：Square，默认两边都可返回

例如：

```bash
cp /path/to/wallpaper.jpg data/images/desktop/
cp /path/to/phone.webp data/images/mobile/
```

等待最多 `SCAN_INTERVAL_SECONDS`，或立即重扫：

```bash
set -a
. ./.env
set +a
curl -fsS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/rescan
unset ADMIN_TOKEN
```

验证：

```bash
curl -D - -o random-image.bin http://127.0.0.1:10086/random
curl -D - -o desktop-image.bin \
  'http://127.0.0.1:10086/random?type=desktop'
curl -D - -o mobile-image.bin \
  'http://127.0.0.1:10086/random?type=mobile'
```

本地图片属于永久数据：会进入项目 Backup，不会被 WebDAV 缓存维护删除。

## 三、启用 WebDAV Hybrid

### 1. 准备远程目录

第一版采用远程目录分类：

```text
<WEBDAV_BASE_URL>/desktop/   横屏图片
<WEBDAV_BASE_URL>/mobile/    竖屏图片
```

示例：

```text
https://dav.example.com/random-image-api/desktop/
https://dav.example.com/random-image-api/mobile/
```

要求：

- 使用 HTTPS
- 使用专用只读 WebDAV 账号
- 账号只授予图库目录读取权限
- `WEBDAV_ALLOWED_HOSTS` 明确填写允许访问的主机
- 不要把凭据写进远程 URL

### 2. 修改 `.env`

```env
ADMIN_TOKEN=使用-openssl-rand-hex-32-生成的值

STORAGE_MODE=hybrid
HYBRID_REMOTE_PROBABILITY=0.9

WEBDAV_BASE_URL=https://dav.example.com/random-image-api
WEBDAV_DESKTOP_ROOT=/desktop/
WEBDAV_MOBILE_ROOT=/mobile/
WEBDAV_ALLOWED_HOSTS=dav.example.com
WEBDAV_USERNAME=专用只读账号
WEBDAV_PASSWORD=应用密码

WEBDAV_TIMEOUT_SECONDS=10
WEBDAV_SYNC_INTERVAL_SECONDS=300
WEBDAV_MAX_XML_BYTES=2097152
WEBDAV_MAX_OBJECTS=10000
WEBDAV_MAX_DOWNLOAD_BYTES=26214400

CACHE_MAX_BYTES=1073741824
CACHE_MAX_FILES=2000
CACHE_REFRESH_AFTER_SECONDS=3600
CACHE_ROTATE_PERCENT=10
```

配置变化后必须重新创建容器；单独 `restart` 不会重新读取 `.env`：

```bash
docker compose up -d --force-recreate
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

### 3. 手动同步 WebDAV 索引

```bash
set -a
. ./.env
set +a
curl -fsS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/webdav/sync
unset ADMIN_TOKEN
```

应用通过 `PROPFIND` 只保存路径、大小、ETag、修改时间等轻量索引，不会预先把整个远程图库下载到 VPS。

### 4. Hybrid 选择与故障降级

默认行为：

1. 每个请求有 90% 概率优先 WebDAV，10% 概率优先本地永久图库。
2. 本地分支为空时仍会继续尝试 WebDAV。
3. WebDAV 缓存命中时直接返回缓存。
4. 未缓存或需要更新时，从 WebDAV 受限下载并校验图片。
5. WebDAV 401/403、超时、5xx、非法内容或下载失败时，从同方向的“有效 WebDAV 缓存 + 本地永久图片”联合池选择。
6. 同方向为空且 `FALLBACK_ENABLED=true` 时，再尝试另一方向。

可通过响应头判断来源：

```text
X-Image-Source: local | webdav-live | webdav-cache
X-Remote-Fallback-Used: true | false
```

## 四、缓存如何更新

WebDAV 缓存位于：

```text
data/cache/webdav/
```

它与 `data/images/` 的永久本地图严格分离。

更新机制：

- 超过 `CACHE_REFRESH_AFTER_SECONDS` 后，下次命中使用 ETag / Last-Modified 条件请求
- 远端返回 HTTP 304 时保留原文件，只更新状态
- 远端内容变化时先写临时文件，验证后原子替换
- 后台每轮随机标记 `CACHE_ROTATE_PERCENT` 的缓存，使冷门项也能重新验证
- 超过 `CACHE_MAX_BYTES` 或 `CACHE_MAX_FILES` 时按近似 LRU 淘汰
- WebDAV 故障不会全量清空现有缓存
- 缓存默认不进入 Backup，删除后可从 WebDAV 重建

手动执行缓存维护：

```bash
set -a
. ./.env
set +a
curl -fsS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/cache/maintain
unset ADMIN_TOKEN
```

## 五、导入目录混乱的压缩包

支持：

```text
ZIP  TAR.GZ  TGZ
```

归档中的目录名和图片名称可以不统一。导入器会：

- 使用 Pillow 解码并应用 EXIF Orientation
- 按真实视觉宽高放入 `desktop/` 或 `mobile/`
- 使用图片内容 SHA-256 安全命名
- 自动跳过重复内容
- 拒绝绝对路径、`..`、链接、设备文件、超限成员、解压炸弹和像素炸弹

先把归档放进持久化目录：

```bash
mkdir -p data/imports
cp /path/to/gallery.zip data/imports/
```

先预览，不写文件：

```bash
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip \
  --output-dir /app/data/images \
  --dry-run
```

确认摘要后正式导入：

```bash
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip \
  --output-dir /app/data/images
```

然后触发本地扫描，或等待自动扫描。确认导入结果和 `/random` 正常后，再自行删除原压缩包。

## 六、API 与管理接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 服务、SQLite、本地图片、WebDAV 和缓存状态 |
| `GET /random` | 按 User-Agent 返回随机图片 |
| `GET /random?type=desktop` | 强制 Desktop |
| `GET /random?type=mobile` | 强制 Mobile |
| `POST /admin/rescan` | 重扫本地永久图库 |
| `POST /admin/webdav/sync` | 同步 WebDAV 轻量索引 |
| `POST /admin/cache/maintain` | 缓存轮换标记和容量淘汰 |

管理接口仅在 `ADMIN_TOKEN` 非空时启用，并要求请求头：

```text
X-Admin-Token: <ADMIN_TOKEN>
```

Token 为空时管理接口返回 404，错误时返回 401。

## 七、数据持久化

必须保留宿主机的 `./data`：

```text
data/
├── images/          本地永久图片，必须备份和迁移
├── database/        SQLite 本地与远程索引，必须备份和迁移
├── cache/webdav/    可重建远程缓存，默认不备份
├── logs/            访问日志
└── imports/         用户临时放入的待导入归档
```

镜像不包含私人图片、生产数据库、日志、备份、`.env` 或访问凭据。执行 `docker compose down` 不会删除 bind mount 中的 `./data`。

不要把 `docker compose down -v` 作为日常停止命令。

## 八、Backup / Restore

完整的 Backup / Restore 脚本位于 GitHub 源码仓库：

```text
https://github.com/time-wanderer/random-image-api
```

克隆源码并使用仓库自带的 Compose 时：

```bash
./scripts/backup.sh
```

备份包含：

- `data/images/` 本地永久图片
- SQLite `VACUUM INTO` 一致性快照
- `.env.example`
- 清空 Token、用户名和密码后的脱敏配置

不包含可重建的 `data/cache/`。WebDAV 远程图库本体需要使用 WebDAV 服务商的快照、版本或备份能力保护。

恢复前先停止 API：

```bash
docker compose down
./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
RESTORE_CONFIRM=YES ./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
docker compose up -d
curl -fsS http://127.0.0.1:10086/health
```

Restore 会先把现有图片和数据库保存到带时间戳的 `pre-restore-*` 目录。

## 九、更新、停止与日志

更新稳定标签：

```bash
docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

查看日志：

```bash
docker compose logs --tail=200 api
docker compose logs -f api
```

重启：

```bash
docker compose restart api
```

停止但保留数据：

```bash
docker compose down
```

确认新镜像正常后再按需清理无引用旧镜像：

```bash
docker image prune
```

## 十、常见问题

### `/health` 正常，但 `/random` 返回 404

图库为空，或图片全部损坏/格式不支持。检查：

```bash
curl -fsS http://127.0.0.1:10086/health
docker compose logs --tail=200 api
find data/images -type f
```

添加图片并等待扫描，或调用 `/admin/rescan`。

### `/random` 返回 503

Hybrid 模式下远程故障，且没有可用的同方向缓存或本地图片。检查 WebDAV URL、账号权限、允许主机、TLS、远程目录及 `/health` 中的 WebDAV 状态。

### WebDAV 返回 403

确认专用账号对 `desktop/`、`mobile/` 目录具有读取和 `PROPFIND` 权限。服务会尝试使用有效缓存与本地永久图片降级，但没有 fallback 图片时仍无法返回图片。

### 修改 `.env` 后没有生效

`docker compose restart` 不会重新读取 Compose 环境。执行：

```bash
docker compose up -d --force-recreate
```

### SQLite 或日志提示 Permission denied

确认挂载不是只读，并检查目录权限：

```bash
sudo chown -R 1000:1000 data
docker compose up -d --force-recreate
```

### 宿主机端口冲突

将映射左侧改为其他空闲端口：

```yaml
ports:
  - "18086:10086"
```

随后访问 `http://127.0.0.1:18086`。

### Android 拿到横图

确认请求没有显式携带 `?type=desktop`。显式 `type` 始终优先于 User-Agent。

## 安全建议

- 使用 HTTPS 反向代理对外提供 API
- 管理接口只允许可信网络访问
- `ADMIN_TOKEN` 使用 `openssl rand -hex 32` 生成并定期轮换
- WebDAV 使用最小权限只读账号
- `.env` 权限设置为 `600`
- 不上传图片、数据库、日志、备份和 Secret 到 GitHub
- 定期 Backup，并在隔离目录实际验证 Restore
- 升级前先备份 `data/images/` 和 SQLite

## 源码与完整文档

GitHub：

```text
https://github.com/time-wanderer/random-image-api
```

仓库包含完整 README、迁移指南、实现报告、Dockerfile、Docker Compose、Backup / Restore 脚本和自动测试。

## License

MIT
