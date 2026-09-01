# Random Image API：Docker Hub 与 V2 部署说明

Random Image API V2 是 V1 的向后兼容扩展，增加 tags、多对多主题、主题随机接口和安全管理 UI，同时保留 V1 的 `/random`、本地/Hybrid、WebDAV 默认 90% 远程优先、缓存、importer 和 Backup / Restore。

## 发布状态（请先阅读）

- **当前推荐版本**：`qinlingmonkey/random-image-api:v2`
- **平台**：`linux/amd64`
- **Registry 摘要**：`sha256:22097fbcb95a953c99a4c32a4c0381bfdc817bc25e6e2825272694a1d9d126cb`
- **兼容回滚版本**：`qinlingmonkey/random-image-api:v1`，继续保留且不会被 V2 覆盖

V2 是 V1 的扩展：旧的 `/random`、`?type=`、本地图库和 WebDAV Hybrid 部署可以继续使用；升级前仍应先备份 SQLite 与永久图片。

## 1. 使用 V2 镜像快速部署

创建持久化目录：

```bash
mkdir -p random-image-api/data/{images/desktop,images/mobile,database,cache/webdav,logs,tmp/admin}
cd random-image-api
sudo chown -R 1000:1000 data
```

创建 `compose.yml`：

```yaml
services:
  api:
    image: qinlingmonkey/random-image-api:v2
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

创建 `.env`，管理 Token 与会话密钥必须分别随机生成：

```bash
cat > .env <<EOF
APP_PORT=10086
ADMIN_PATH=/manage-images
ADMIN_TOKEN=$(openssl rand -hex 32)
ADMIN_SESSION_SECRET=$(openssl rand -hex 32)
ADMIN_COOKIE_SECURE=false
STORAGE_MODE=local
EOF
chmod 600 .env

docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

默认管理登录地址：

```text
http://<主机>:10086/manage-images/login
```

直接通过 HTTP 测试时可使用 `ADMIN_COOKIE_SECURE=false`；生产环境应配置 HTTPS 反向代理并改为 `true`。仅修改 `ADMIN_PATH` 不能替代 Token、Session Cookie 与 CSRF 防护。

## 2. 可选：从源码构建 V2

```bash
git clone https://github.com/time-wanderer/random-image-api.git
cd random-image-api
cp .env.example .env
# 分别生成并写入 ADMIN_TOKEN、ADMIN_SESSION_SECRET
openssl rand -hex 32
openssl rand -hex 32
chmod 600 .env

docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

项目 Compose 使用本地 Dockerfile 构建，`./data:/app/data` 保存永久数据。当前 Docker Hub `v2` 仅提供 `linux/amd64`；ARM64 主机应从源码构建。

常用维护：

```bash
docker compose logs -f api
docker compose restart api
docker compose up -d --build
docker compose down
```

## 3. V2 API

V1 接口完全保留：

```bash
curl -D - -o image.bin http://127.0.0.1:10086/random
curl -D - -o image.bin 'http://127.0.0.1:10086/random?type=desktop'
```

V2 主题接口：

```bash
curl -D - -o image.bin http://127.0.0.1:10086/random/anime
curl -D - -o image.bin 'http://127.0.0.1:10086/random?tag=anime&type=mobile'
```

同一图片可关联多个 tags，无需复制文件。路径标签与查询标签同时存在时必须一致，否则 `400`。

| 状态码 | 说明 |
| --- | --- |
| `200` | 返回图片 |
| `404` | 标签未知/禁用/非法，或筛选后没有可用图片 |
| `503` | 数据库不可用，或 WebDAV 故障且没有本地/缓存候选可降级 |

## 4. 本地图库、上传与删除

手工图片目录：

```text
data/images/desktop/
data/images/mobile/
```

服务按真实宽高判断方向。V2 管理 UI 默认为：

```text
http://<主机>:10086/manage-images
```

可配置 `ADMIN_PATH`。管理 UI 支持：

- 创建、编辑、启停、合并标签；
- 给一张图片关联多个标签；
- 多文件上传、格式/像素/大小验证、哈希去重；
- 删除本地原图，必须输入大写 `DELETE`；
- WebDAV 对象启停与标签维护；
- 缓存维护和清空。

管理 UI 使用签名会话、HttpOnly / SameSite=Strict Cookie、登录限速与 CSRF。生产环境应置于 HTTPS 反向代理后，并设置独立随机的 `ADMIN_TOKEN` 与 `ADMIN_SESSION_SECRET`。

## 5. 归档 preview-confirm 与 importer

V1 命令行 importer 仍保留，支持 ZIP、TAR.GZ、TGZ：

```bash
python -m app.importer /path/to/images.zip --images-dir ./data/images --dry-run
python -m app.importer /path/to/images.zip --images-dir ./data/images
```

V2 管理 UI 推荐使用两阶段流程：

1. **preview**：上传归档，先完整校验路径穿越、成员数、单文件/总大小、压缩比和真实图片格式，执行 dry-run，不写入永久图库；
2. **confirm**：核对统计、默认标签和第一层目录标签映射后，才正式导入。

preview 绑定当前登录会话并有 TTL；登出、过期或容器重启后必须重新 preview。

## 6. WebDAV Hybrid 与第一层主题

私密配置示意只使用占位符：

```dotenv
STORAGE_MODE=hybrid
HYBRID_REMOTE_PROBABILITY=0.9
WEBDAV_BASE_URL=<HTTPS WebDAV 根地址>
WEBDAV_ALLOWED_HOSTS=<允许的主机名>
WEBDAV_USERNAME=<用户名>
WEBDAV_PASSWORD=<密码>
WEBDAV_DESKTOP_ROOT=/desktop/
WEBDAV_MOBILE_ROOT=/mobile/
```

主题目录结构：

```text
desktop/anime/wide.webp
mobile/anime/tall.webp
desktop/landscape/lake.jpg
```

V2 把 `desktop/` 或 `mobile/` 下第一层目录视为 tag 提示。同步只保存索引；图片命中后才下载到缓存。管理员手工禁用的远端对象不会因后续同步被重新启用。

默认 `HYBRID_REMOTE_PROBABILITY=0.9`，即保留 V1 的 90% WebDAV 优先策略。远端失败时使用当前方向的缓存/本地候选，再按配置做方向回退。缓存继续支持条件刷新、LRU 容量淘汰和随机轮换。

```bash
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/webdav/sync
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/cache/maintain
```

## 7. V2 关键环境变量

| 用途 | 变量 |
| --- | --- |
| 端口与路径 | `APP_PORT`、`DATA_DIR`、`IMAGES_DIR`、`DATABASE_PATH`、`LOG_DIR` |
| 选图 | `FALLBACK_ENABLED`、`SQUARE_POLICY`、`SCAN_INTERVAL_SECONDS` |
| 管理 UI | `ADMIN_TOKEN`、`ADMIN_SESSION_SECRET`、`ADMIN_PATH`、`ADMIN_COOKIE_NAME`、各 `ADMIN_*` 限制 |
| WebDAV | `STORAGE_MODE`、`HYBRID_REMOTE_PROBABILITY`、`WEBDAV_*` |
| 缓存 | `CACHE_DIR`、`CACHE_MAX_BYTES`、`CACHE_MAX_FILES`、`CACHE_REFRESH_AFTER_SECONDS`、`CACHE_ROTATE_PERCENT` |

使用以下命令生成 Secret，不要复制文档中的固定示例值：

```bash
openssl rand -hex 32
```

`.env` 必须保留在部署机并限制权限，不得提交到 Git、打入镜像或出现在日志中。

## 8. V1→V2 原地升级

```bash
./scripts/backup.sh
docker compose down
# 把 Compose 镜像切换为 qinlingmonkey/random-image-api:v2，
# 并补充 ADMIN_SESSION_SECRET 等 V2 环境变量
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:10086/health
```

V2 启动时幂等迁移 V1 SQLite：新增 tags、多对多关系和 V2 字段，不删除旧图片。未打标签图片继续服务 `/random`，但在关联标签前不会进入主题接口。完整升级与回滚步骤见 [MIGRATION.md](MIGRATION.md)。

## 9. Backup / Restore 与 VPS 迁移

```bash
./scripts/backup.sh
docker compose down
RESTORE_CONFIRM=YES ./scripts/restore.sh /path/to/backup.tar.gz
docker compose up -d
```

备份包含永久图库、SQLite 一致性副本和脱敏配置；不包含可重建缓存与真实 Secret。迁移到新 VPS 时复制 Compose 配置、备份归档和私下保存的配置，在新机恢复后拉取 `v2`；也可复制源码后自行构建。详见 GitHub 仓库中的 `MIGRATION.md`。

## 10. 故障排查

- `/random` 为 `404`：确认图库有可用图片；主题请求还需确认标签存在、启用并已关联候选。
- `/random` 为 `503`：检查数据库、WebDAV 连通性、允许主机、凭据、缓存和本地降级候选。
- 管理 UI 无法登录：确认 `ADMIN_TOKEN`、会话密钥、入口路径和反向代理设置。
- `.env` 修改未生效：执行 `docker compose up -d --force-recreate`。
- Permission denied：确保挂载目录可由镜像内非 Root 用户写入。
- WebDAV 主题未出现：主题必须是方向根目录下第一层、且名称可转换为合法 slug；然后重新同步。

完整源码、V1 快照和迁移文档见：`https://github.com/time-wanderer/random-image-api`。
