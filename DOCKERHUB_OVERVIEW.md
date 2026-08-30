# Random Image API

一个轻量、可长期运行的随机图片 API，基于 FastAPI、Uvicorn、Pillow 和 SQLite。

## 功能

- `GET /random` 返回随机图片
- 根据 User-Agent 自动识别 Desktop / Mobile
- Desktop 优先返回横屏图片，Mobile 优先返回竖屏图片
- 支持 `?type=desktop` 和 `?type=mobile` 显式覆盖
- 支持 JPG、JPEG、PNG 和 WebP
- 自动读取图片宽高并维护 SQLite 元数据
- 支持分类缺图 fallback、文件缺失及损坏图片处理
- 默认使用本地永久图库；可选 WebDAV Hybrid 远程扩展图库
- Hybrid 默认 90% 优先 WebDAV，远程 401/403、超时或 5xx 时降级到本地图片与有效缓存
- WebDAV 只同步目录索引，图片按需下载到有容量上限、可刷新轮换的独立缓存
- 支持安全导入 ZIP / TAR.GZ 混合图片包并按真实方向分类、去重
- `GET /health` 提供服务、数据库、图片、WebDAV 和缓存状态
- 图片、SQLite 数据库和日志通过 `/app/data` 持久化
- 以非 root 用户运行
- 提供 Docker Compose、Backup、Restore 和 VPS 迁移方案

## 镜像标签

| 标签 | 用途 |
| --- | --- |
| `v1` | 当前稳定的主版本标签 |

当前镜像平台：`linux/amd64`。

## Docker Compose 安装

创建项目目录和持久化数据目录：

```bash
mkdir -p random-image-api/data/images/desktop \
  random-image-api/data/images/mobile \
  random-image-api/data/database \
  random-image-api/data/logs
cd random-image-api
```

创建 `compose.yml`：

```yaml
services:
  api:
    image: qinlingmonkey/random-image-api:v1
    restart: unless-stopped
    ports:
      - "10086:10086"
    environment:
      LOG_LEVEL: INFO
      FALLBACK_ENABLED: "true"
      SQUARE_POLICY: both
      SCAN_ON_STARTUP: "true"
      SCAN_INTERVAL_SECONDS: "300"
      MAX_PICK_RETRIES: "8"
      TRUSTED_PROXY_HEADERS: "true"
    volumes:
      - ./data:/app/data
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10086/health', timeout=4)"
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 15s
```

拉取并启动：

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f
```

检查服务：

```bash
curl -i http://127.0.0.1:10086/health
curl -D - -o random-image.bin http://127.0.0.1:10086/random
```

更新镜像：

```bash
docker compose pull
docker compose up -d
docker image prune -f
```

停止服务：

```bash
docker compose down
```

`docker compose down` 不会删除绑定挂载的 `./data` 目录。

## 添加图片

将图片放入持久化目录：

```text
data/images/desktop/
data/images/mobile/
```

支持格式：

```text
.jpg  .jpeg  .png  .webp
```

程序会读取图片实际宽高进行分类：

- `width > height`：Desktop / Landscape
- `height > width`：Mobile / Portrait
- 正方形图片默认同时参与 Desktop 和 Mobile 选择

新增、删除或替换图片后，应用会按配置定期扫描；也可配置管理令牌后调用 `POST /admin/rescan`。

## API

### `GET /health`

返回服务状态、图片数量和数据库状态。

```bash
curl http://127.0.0.1:10086/health
```

### `GET /random`

根据 User-Agent 自动选择 Desktop 或 Mobile 图片：

```bash
curl -D - -o random-image.bin http://127.0.0.1:10086/random
```

显式指定 Desktop：

```bash
curl -D - -o desktop-image.bin \
  "http://127.0.0.1:10086/random?type=desktop"
```

显式指定 Mobile：

```bash
curl -D - -o mobile-image.bin \
  "http://127.0.0.1:10086/random?type=mobile"
```

非法 `type` 返回 HTTP `400`；没有可用图片时返回 HTTP `404`。

## 配置

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `ADMIN_TOKEN` | 空 | 可选管理令牌；为空时关闭管理扫描接口 |
| `FALLBACK_ENABLED` | `true` | 目标分类缺图时是否回退 |
| `SQUARE_POLICY` | `both` | 正方形归属：`both`、`desktop` 或 `mobile` |
| `SCAN_ON_STARTUP` | `true` | 启动时扫描图片 |
| `SCAN_INTERVAL_SECONDS` | `300` | 后台扫描间隔（秒） |
| `MAX_PICK_RETRIES` | `8` | 遇到损坏或缺失文件时的最大重试次数 |
| `TRUSTED_PROXY_HEADERS` | `true` | 是否信任反向代理来源请求头 |

Secret 应通过环境变量或 `.env` 注入，不要写进镜像或公开文档。

### WebDAV Hybrid（可选）

默认 `STORAGE_MODE=local`，本地图片仍放在 `/app/data/images` 且永不被缓存维护删除。启用 Hybrid 时，WebDAV 远程目录约定为：

```text
<WEBDAV_BASE_URL>/desktop/
<WEBDAV_BASE_URL>/mobile/
```

Compose 的 `environment` 可增加：

```yaml
      STORAGE_MODE: hybrid
      HYBRID_REMOTE_PROBABILITY: "0.9"
      WEBDAV_BASE_URL: https://dav.example.com/random-image-api
      WEBDAV_DESKTOP_ROOT: /desktop/
      WEBDAV_MOBILE_ROOT: /mobile/
      WEBDAV_ALLOWED_HOSTS: dav.example.com
      WEBDAV_USERNAME: ${WEBDAV_USERNAME}
      WEBDAV_PASSWORD: ${WEBDAV_PASSWORD}
      CACHE_MAX_BYTES: "1073741824"
      CACHE_MAX_FILES: "2000"
      CACHE_REFRESH_AFTER_SECONDS: "3600"
      CACHE_ROTATE_PERCENT: "10"
```

真实账号和密码放在同目录未公开的 `.env`。应用仅同步远程索引，图片被选中时才按需下载；缓存位于 `/app/data/cache/webdav`，支持 ETag / Last-Modified 条件刷新、随机轮换和近似 LRU 淘汰。远程 401/403、超时或 5xx 时会使用有效缓存与永久本地图降级；远程故障不会清空缓存。

混合压缩包可在源码部署目录使用容器安全导入：

```bash
mkdir -p data/imports
cp /path/to/gallery.zip data/imports/
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip --output-dir /app/data/images --dry-run
docker compose run --rm api python -m app.importer \
  /app/data/imports/gallery.zip --output-dir /app/data/images
```

支持 ZIP、TAR.GZ、TGZ；按真实视觉方向分类，并拒绝路径穿越、链接、设备文件和解压炸弹。

## 数据持久化

必须将宿主机数据目录挂载到容器内 `/app/data`。该目录包含：

- `/app/data/images`：图片文件
- `/app/data/database/images.db`：SQLite 元数据
- `/app/data/logs`：访问日志

镜像本身不包含私人图片、生产数据库、日志、备份、`.env` 或访问凭据。删除或重建容器不会删除宿主机挂载的数据。

## Backup / Restore

源码仓库提供：

```text
scripts/backup.sh
scripts/restore.sh
```

Backup 会保存图片及 SQLite 一致性快照；Restore 默认先预览，并要求显式确认后才会替换数据。

## 技术栈

- Python
- FastAPI
- Uvicorn
- Pillow
- SQLite
- Docker / Docker Compose

## 安全建议

- 生产部署使用当前主版本标签 `v1`，升级前先查看镜像发布说明
- 不要把 `.env`、管理令牌、图片、数据库、日志或备份打进镜像
- 通过反向代理提供 TLS
- 限制管理接口令牌并定期轮换
- 定期执行 Backup，并实际验证 Restore

## License

MIT
