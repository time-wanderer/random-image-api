# Random Image API V2.1

Random Image API V2 是 V1 的**向后兼容扩展**：保留 V1 的 `GET /random`、`?type=`、本地图库、WebDAV Hybrid、默认 90% 远程优先、缓存、归档 importer、Backup / Restore，并新增主题标签和安全管理 UI。V2.1 在不改变 API 和数据库 schema 的前提下，重点优化管理网页与本地图片整理体验。

> 当前发布状态：V2.1.0 已通过本地和远程隔离验收，完整测试为 `79 passed`。GitHub `main` 已包含提交 `d637c4b6d8f444516a6084e571748a9ba3c692e1`；Docker Hub `qinlingmonkey/random-image-api:v2` 已更新为 V2.1.0（`linux/amd64`，Registry 摘要 `sha256:d35f9c129d3c2119552b2b18877201125b224c4f918b0caee66ba1692d987a0e`）。`qinlingmonkey/random-image-api:v1` 继续保留用于旧部署与回滚。

V1 快照见 [docs/V1.md](docs/V1.md)，V1 原地升级和 VPS 迁移见 [MIGRATION.md](MIGRATION.md)。

## 1. V2 与 V2.1 能力

- `tags` 主题模型：一张本地图片或一个 WebDAV 对象可关联多个标签，多对多关系不会复制图片文件。
- `GET /random/{slug}` 与 `GET /random?tag={slug}`：按主题随机返回图片。
- 严格主题语义：未知、禁用或非法标签返回 `404`；标签存在但没有可用图片也返回 `404`；数据库不可用或无法安全降级的远端故障返回 `503`。
- WebDAV 第一层主题：在 `desktop/`、`mobile/` 下的第一层子目录名可作为标签提示，例如 `desktop/anime/a.jpg` 对应 `anime`。
- 浏览器管理 UI：响应式统计卡片、图片瀑布流、受保护预览、标签管理、多图上传、移动归档、友好删除确认、WebDAV 对象启停与标签维护、缓存清理、归档 preview-confirm。
- V2.1 独立使用 `data/images/square/` 保存正方形图片；`SQUARE_POLICY` 只决定正方形图片进入哪些随机池，不再决定物理存放目录。
- SQLite V1→V2 幂等原地迁移：启动时创建标签关系表并补充新字段，原有未打标签图片仍可由 `GET /random` 使用。

## 2. 架构与数据

```text
Client
  -> Docker Compose / FastAPI
    -> data/images/                 本地永久图库
    -> data/database/images.db      SQLite 元数据、标签关系、远端索引
    -> 可选 WebDAV                  远端扩展图库
    -> data/cache/webdav/           可重建的按需缓存
    -> 管理 UI                      会话、CSRF、上传与归档操作
```

支持 `.jpg`、`.jpeg`、`.png`、`.webp`。Desktop 优先横图，Mobile 优先竖图；`SQUARE_POLICY` 控制正方形归属，`FALLBACK_ENABLED` 控制方向回退。

标签 slug 只允许 1–63 位小写字母、数字和单连字符，不能使用保留路由名。显示名称与 slug 分离。`images ↔ tags`、`webdav_objects ↔ tags` 都是多对多关系。

## 3. 部署 V2

要求：Linux、Docker Engine、Docker Compose Plugin。

### 3.1 使用 Docker Hub 镜像

创建项目目录和持久化目录：

```bash
mkdir -p random-image-api/data/{images/desktop,images/mobile,images/square,database,logs,cache/webdav,tmp/admin}
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

创建 `.env`。两个 Secret 必须分别随机生成：

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

直接通过 HTTP 访问时可暂用 `ADMIN_COOKIE_SECURE=false`；生产环境应放在 HTTPS 反向代理后并改为 `true`。默认管理入口是 `http://服务器地址:10086/manage-images/login`。自定义路径不能替代 Token、Session Cookie 和 CSRF 防护。

### 3.2 从源码构建

```bash
cp .env.example .env
# 生成管理令牌与管理会话密钥，分别写入 .env 对应字段
openssl rand -hex 32
openssl rand -hex 32
chmod 600 .env

docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

请把第二个生成值写入 `ADMIN_SESSION_SECRET`。`.env.example` 已提供该变量名；不要把真实值提交到 Git。Compose 将 `./data` 挂载到 `/app/data`，所有重要数据保留在宿主机。

常用操作：

```bash
docker compose logs -f api
docker compose restart api
docker compose up -d --build
docker compose down
```

## 4. API 教程

### 4.1 健康检查

```bash
curl -fsS http://127.0.0.1:10086/health
```

`GET /health` 返回版本、数据库、本地图片数量、最后扫描状态、WebDAV 和缓存摘要。

### 4.2 V1 兼容随机接口

```bash
curl -D headers.txt -o image.bin http://127.0.0.1:10086/random
curl -D headers.txt -o image.bin 'http://127.0.0.1:10086/random?type=mobile'
```

未传 `type` 时按 User-Agent 识别；显式 `desktop` / `mobile` 优先。响应头包含方向、尺寸、来源、方向回退、远端降级和标签等信息。

### 4.3 V2 主题接口

以下两种写法等价：

```bash
curl -D headers.txt -o image.bin http://127.0.0.1:10086/random/anime
curl -D headers.txt -o image.bin 'http://127.0.0.1:10086/random?tag=anime&type=desktop'
```

若路径和查询同时给出标签，两者必须一致，否则返回 `400`。成功响应包含：

- `X-Image-Tag`：标签 slug；无标签筛选时为 `untagged`。
- `X-Image-Tag-Name`：主题显示名称的 UTF-8 百分号编码（主题请求时）；客户端可使用 URL decode 还原中文。
- `X-Image-Source`：`local`、`webdav-cache` 等来源。
- `X-Fallback-Used`、`X-Remote-Fallback-Used`：方向或远端降级状态。

严格状态码：

| 状态 | 含义 |
| --- | --- |
| `200` | 找到并返回图片 |
| `400` | `type` 非法，或路径标签与查询标签冲突 |
| `404` | 标签未知/禁用/非法，或该筛选下确认没有可用图片 |
| `503` | 数据库不可用，或 WebDAV 故障且没有本地/缓存候选可安全降级 |

## 5. 本地图片与 importer

可把图片放入：

```text
data/images/desktop/
data/images/mobile/
data/images/square/
```

程序始终以图片真实宽高分类；`desktop/`、`mobile/`、`square/` 是物理归档目录，便于人工整理，不会覆盖真实方向。V2.1 上传和归档 importer 会把正方形图片保存到 `square/`。旧版本中位于根目录、`desktop/` 或 `mobile/` 的正方形图片仍兼容，不会被强制移动。等待周期扫描，或配置 `ADMIN_TOKEN` 后触发：

```bash
curl -fsS -X POST \
  -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/rescan
```

命令行 importer 继续支持 ZIP、TAR.GZ、TGZ：

```bash
python -m app.importer /path/to/images.zip --images-dir ./data/images --dry-run
python -m app.importer /path/to/images.zip --images-dir ./data/images
```

它会先验证归档成员、路径、大小和压缩比，再识别真实图片格式、方向并按内容去重。V2 管理 UI 提供更安全易用的 preview-confirm 流程，见下文。

## 6. WebDAV Hybrid

在私密配置中设置：

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

建议远端结构：

```text
desktop/
├── anime/
│   └── wide.webp
└── landscape/
    └── lake.jpg
mobile/
└── anime/
    └── tall.png
```

V2 读取 `desktop/`、`mobile/` 下**第一层子目录**作为主题提示；更深层目录不会产生额外层级标签。远端同步只更新轻量索引，图片在命中时按需下载。

`HYBRID_REMOTE_PROBABILITY=0.9` 保留 V1 的 90% 远程优先策略。远端失败时按当前方向尝试缓存和本地图库，必要时再做方向回退；没有候选且远端状态不可靠时返回 `503`。

缓存支持 ETag / Last-Modified 条件刷新、容量与文件数上限、LRU 淘汰和随机轮换。维护接口仍兼容：

```bash
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/webdav/sync
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/cache/maintain
```

## 7. 管理 UI

默认入口为 `http://<主机>:<端口>/manage-images`，可用 `ADMIN_PATH` 改为其他非保留路径。

### 必要配置

```dotenv
ADMIN_TOKEN=<使用 openssl rand -hex 32 生成>
ADMIN_SESSION_SECRET=<使用 openssl rand -hex 32 生成>
ADMIN_PATH=/manage-images
ADMIN_COOKIE_NAME=ria_admin_session
ADMIN_SESSION_TTL_SECONDS=1800
ADMIN_PREVIEW_TTL_SECONDS=600
ADMIN_LOGIN_WINDOW_SECONDS=60
ADMIN_LOGIN_MAX_ATTEMPTS=5
ADMIN_MAX_UPLOAD_BYTES=26214400
ADMIN_PAGE_SIZE=20
```

归档限制还可通过 `ADMIN_MAX_ARCHIVE_BYTES`、`ADMIN_MAX_ARCHIVE_MEMBERS`、`ADMIN_MAX_ARCHIVE_MEMBER_BYTES`、`ADMIN_MAX_ARCHIVE_TOTAL_BYTES`、`ADMIN_MAX_ARCHIVE_COMPRESSION_RATIO` 调整；图片像素上限使用 `ADMIN_MAX_IMAGE_PIXELS`，临时目录使用 `UPLOAD_TMP_DIR`，归档默认标签可用 `ADMIN_IMPORT_DEFAULT_TAG` 设置。

### 安全边界

- 未配置 `ADMIN_TOKEN` 时，旧管理 API 隐藏为 `404`；管理 UI 登录也依赖管理凭据。
- 登录有限速；会话 Cookie 为 HttpOnly、SameSite=Strict，并由会话密钥签名。
- 所有写操作要求 CSRF token；页面输出进行 HTML 转义。
- 会话和待确认归档 preview 保存在单进程内存中；重启会退出登录并使 preview 失效。
- 生产环境应在 HTTPS 反向代理后使用，不要公开 Token、Cookie 或 WebDAV 凭据。

### 常用操作

1. **瀑布流浏览**：本地图片以响应式卡片展示并使用浏览器懒加载；预览只能在管理员登录会话中访问，不暴露宿主机文件路径。当前直接传输原图供预览，不额外生成缩略图，超大原图较多时会增加浏览器流量。
2. **方向与目录**：卡片分别显示“真实方向”和“存放目录”。管理员可把本地原图移动到 `desktop`、`mobile` 或 `square`，移动只改变归档位置，不伪造图片方向；同名冲突会生成安全的新文件名。
3. **标签**：创建、编辑、启用/禁用、合并；每张本地图片和 WebDAV 对象均可直接添加或移除标签，本地图片还支持勾选后批量添加/移除。一张图可关联多个标签而不复制文件。
4. **上传**：支持单图和多图上传，验证格式、像素和大小，按内容哈希去重，并按真实方向保存；正方形图片进入 `square/`。
5. **删除**：点击“删除本地原图”后由浏览器二次确认，页面不再要求手写 `DELETE`；服务端仍要求明确确认字段和 CSRF。WebDAV 只允许禁用、维护标签或清理本地缓存，绝不远程删除原图。
6. **WebDAV 预览**：已有本地缓存的远端对象可以预览；未缓存对象显示占位卡片，打开管理页不会批量下载远端原图。
7. **筛选**：可按来源、真实方向、存放目录、启用状态、缓存状态、标签和文件名/HREF 筛选，并选择每页数量。
8. **缓存**：可运行维护或清空 WebDAV 缓存；缓存可重建，不是永久图库。
9. **归档**：先上传到 preview，系统执行完整安全校验和 dry-run；核对摘要、默认标签及第一层目录映射后再 confirm。preview 绑定当前会话、有 TTL，登出、过期或服务重启后不能确认。

## 8. 环境变量

| 分组 | 变量 |
| --- | --- |
| 服务 | `APP_HOST`、`APP_BIND_PORT`、`APP_PORT`、`LOG_LEVEL` |
| 数据 | `DATA_DIR`、`IMAGES_DIR`、`DATABASE_PATH`、`LOG_DIR` |
| 选图 | `FALLBACK_ENABLED`、`SQUARE_POLICY`、`SCAN_ON_STARTUP`、`SCAN_INTERVAL_SECONDS`、`MAX_PICK_RETRIES` |
| 管理 | `ADMIN_TOKEN`、`ADMIN_SESSION_SECRET`、`ADMIN_PATH`、`ADMIN_COOKIE_NAME`、各 `ADMIN_*` 限制、`UPLOAD_TMP_DIR` |
| WebDAV | `STORAGE_MODE`、`HYBRID_REMOTE_PROBABILITY`、`WEBDAV_BASE_URL`、根目录、允许主机、凭据、同步/下载限制 |
| 缓存 | `CACHE_DIR`、`CACHE_MAX_BYTES`、`CACHE_MAX_FILES`、`CACHE_REFRESH_AFTER_SECONDS`、`CACHE_ROTATE_PERCENT` |
| 代理 | `TRUSTED_PROXY_HEADERS` |

`.env` 不得提交。不要在源码、文档、命令历史或日志中保存真实密钥。若服务不在可信反向代理后，应评估关闭 `TRUSTED_PROXY_HEADERS`。

## 9. V1→V2 原地升级

先备份，再替换为 V2 源码并重建：

```bash
./scripts/backup.sh
docker compose down
docker compose build --pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

V2 首次连接 SQLite 时自动、幂等地迁移 schema，并设置数据库版本。原图片、WebDAV 索引与未打标签行为继续保留；未打标签图片仍由 `/random` 使用，但不会自动出现在主题接口中。升级细节和回滚见 [MIGRATION.md](MIGRATION.md)。

## 10. Backup / Restore

```bash
./scripts/backup.sh
# 先停服务；首次不确认只显示风险并退出
RESTORE_CONFIRM=YES ./scripts/restore.sh /path/to/backup-YYYY-MM-DD-HHMMSS.tar.gz
docker compose up -d
```

备份包含永久图库、SQLite 一致性副本和脱敏配置，不包含 WebDAV 缓存及真实 Secret。恢复前会把现有图库和数据库复制到时间戳安全目录。跨 VPS 迁移流程见 [MIGRATION.md](MIGRATION.md)。

## 11. 开发验证

```bash
python -m pytest
python -m compileall -q app tests
bash -n scripts/*.sh docker-entrypoint.sh
```

当前 V2 实施与验证记录见 [REPORT.md](REPORT.md)。
