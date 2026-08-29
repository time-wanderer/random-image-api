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
- `GET /health` 提供服务、数据库和图片统计状态
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
