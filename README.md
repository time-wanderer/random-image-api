# Random Image API V3.0.0

> **V3.0.0 发布状态**：持久化可恢复网页分片上传已完成验收并发布。Docker Hub 镜像为 `qinlingmonkey/random-image-api:v3`；已发布的 `v1`、`v2` 镜像继续保留。完整使用与运维说明见：[网页归档分片上传](docs/CHUNKED_UPLOAD.md)。

Random Image API V3.0.0 在既有 V1/V2 能力上新增持久化可恢复网页分片上传，同时保留 `GET /random`、`?type=`、本地图库、WebDAV Hybrid、缓存、归档 importer、Backup / Restore、主题标签和安全管理 UI。

> 当前源码和 Docker Hub `v3` 镜像均已发布。V3 镜像使用 `linux/amd64` 构建；旧镜像的历史验收信息保留在实施记录中。

V1 快照见 [docs/V1.md](docs/V1.md)，V1 原地升级和 VPS 迁移见 [MIGRATION.md](MIGRATION.md)。

## 1. V3.0.0 能力概览

- `tags` 主题模型：一张本地图片或一个 WebDAV 对象可关联多个标签，多对多关系不会复制图片文件。
- `GET /random/{slug}` 与 `GET /random?tag={slug}`：按主题随机返回图片。
- 严格主题语义：未知、禁用或非法标签返回 `404`；标签存在但没有可用图片也返回 `404`；数据库不可用或无法安全降级的远端故障返回 `503`。
- WebDAV 第一层主题：在 `desktop/`、`mobile/` 下的第一层子目录名可作为标签提示，例如 `desktop/anime/a.jpg` 对应 `anime`。
- 浏览器管理 UI：响应式统计卡片、图片瀑布流、受保护预览、标签管理、多图上传、移动归档、友好删除确认、WebDAV 对象启停与标签维护、缓存清理、归档 preview-confirm。
- V3.0.0 独立使用 `data/images/square/` 保存正方形图片；`SQUARE_POLICY` 只决定正方形图片进入哪些随机池，不再决定物理存放目录。
- SQLite V1/V2→V3 幂等原地迁移：启动时创建标签关系表并补充新字段，原有未打标签图片仍可由 `GET /random` 使用。

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

## 3. 部署 V3.0.0

要求：Linux、Docker Engine、Docker Compose Plugin。

### 3.1 使用 Docker Hub 镜像

创建项目目录，并下载仓库提供的完整配置模板：

```bash
mkdir -p random-image-api
cd random-image-api
curl -fsSLO https://raw.githubusercontent.com/time-wanderer/random-image-api/main/.env.example
cp .env.example .env
```

创建 `compose.yml`：

```yaml
services:
  api:
    image: qinlingmonkey/random-image-api:v3
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

`.env.example` 是完整配置模板，已经预先写入端口、管理页面、Local/WebDAV、缓存、扫描、归档限制和所有管理参数。默认 `STORAGE_MODE=local`，因此不填写 WebDAV 账号也可以直接启动。

只需生成两个不同的管理 Secret 并写入 `.env`：

```bash
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=v3/" .env
sed -i "s/^IMAGE_NAME=.*/IMAGE_NAME=qinlingmonkey\\/random-image-api/" .env
sed -i "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^ADMIN_SESSION_SECRET=.*/ADMIN_SESSION_SECRET=$(openssl rand -hex 32)/" .env
chmod 600 .env

mkdir -p data/images/{desktop,mobile,square} data/database data/cache/webdav data/logs data/tmp/admin
sudo chown -R 1000:1000 data

docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

直接通过 HTTP 访问时使用 `ADMIN_COOKIE_SECURE=false`；生产环境放在 HTTPS 反向代理后时改为 `true`。默认管理入口是 `http://服务器地址:10086/manage-images/login`。自定义路径不能替代 Token、Session Cookie 和 CSRF 防护。

### 3.1.1 启用 WebDAV

不需要重新编写配置文件，只需编辑服务器上的 `.env`，切换模式并填写 WebDAV 必要信息：

```bash
sed -i 's/^STORAGE_MODE=.*/STORAGE_MODE=hybrid/' .env
vi .env
```

至少填写以下字段：

```env
STORAGE_MODE=hybrid
WEBDAV_BASE_URL=https://dav.example.com/remote.php/dav/files/user/
WEBDAV_ALLOWED_HOSTS=dav.example.com
WEBDAV_USERNAME=your-webdav-user
WEBDAV_PASSWORD=your-webdav-password
```

`WEBDAV_DESKTOP_ROOT` 和 `WEBDAV_MOBILE_ROOT` 已有 `/desktop/`、`/mobile/` 默认值，通常无需修改。保存后执行：

```bash
docker compose up -d --force-recreate
curl -fsS http://127.0.0.1:10086/health
```

WebDAV 密码只保存在服务器 `.env`，不要提交到 GitHub、备份或 Docker Hub。

### 3.2 从源码构建

```bash
git clone https://github.com/time-wanderer/random-image-api.git
cd random-image-api
cp .env.example .env
sed -i "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^ADMIN_SESSION_SECRET=.*/ADMIN_SESSION_SECRET=$(openssl rand -hex 32)/" .env
chmod 600 .env

docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

GitHub 只提供模板，不保存真实 Secret。Compose 将 `./data` 挂载到 `/app/data`，所有重要数据保留在宿主机。

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

### 4.3 V3 主题接口

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

> 完整的 Docker Compose 命令行导入流程、路径引用、多标签、大归档限制、磁盘空间和常见错误说明，请直接查看：[归档导入 CLI 使用手册](docs/CLI_IMPORT.md)。首次使用建议先阅读该手册并执行 `--dry-run`。

可把图片放入：

```text
data/images/desktop/
data/images/mobile/
data/images/square/
```

程序始终以图片真实宽高分类；`desktop/`、`mobile/`、`square/` 是物理归档目录，便于人工整理，不会覆盖真实方向。V3.0.0 上传和归档 importer 会把正方形图片保存到 `square/`。旧版本中位于根目录、`desktop/` 或 `mobile/` 的正方形图片仍兼容，不会被强制移动。等待周期扫描，或配置 `ADMIN_TOKEN` 后触发：

```bash
curl -fsS -X POST \
  -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/rescan
```

命令行 importer 继续支持 ZIP、TAR.GZ、TGZ：

```bash
python -m app.importer /path/to/images.zip --images-dir ./data/images --dry-run
python -m app.importer /path/to/images.zip --images-dir ./data/images
python -m app.importer /path/to/images.zip \
  --images-dir ./data/images \
  --database-path ./data/database/images.db \
  --tag nature \
  --tag featured
```

CLI 已支持 ZIP、TAR.GZ、TGZ、多次 `--tag` 和 `--database-path`，但只处理归档，不直接接受目录。`--tag` 可重复传入，一次为本次归档涉及的所有图片关联多个标签；使用 `--tag` 时必须同时提供 `--database-path`。标签及图片关系保存在 SQLite 的 `tags`、`image_tags` 和 `webdav_object_tags` 表中，不写入图片文件。本地图片以内容哈希识别，WebDAV 对象以标准化 HREF 关联，因此不会依赖文件显示顺序关联标签。

文件夹请先打包后再运行 CLI：

```bash
tar -czf /tmp/photos.tar.gz -C /path/to photos
python -m app.importer /tmp/photos.tar.gz \
  --output-dir ./data/images \
  --database-path ./data/database/images.db \
  --tag nature --tag featured
```

也可直接把图片复制到 `data/images/{desktop,mobile,square}/` 后触发 `POST /admin/rescan`。目录标签映射来自归档成员的父目录，网页预览页可复核修改；网页上传前选择的标签应用于本次所有图片。

Importer 会先验证归档成员、路径、大小和压缩比，再识别真实图片格式、方向并按内容去重。文件提交或 SQLite 标签事务失败时，会撤销本次新建文件；导入前已存在的重复图片不会被删除。V3 管理 UI 提供更安全易用的 preview-confirm 流程，见下文。

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

V3 读取 `desktop/`、`mobile/` 下**第一层子目录**作为主题提示；更深层目录不会产生额外层级标签。远端同步只更新轻量索引，图片在命中时按需下载。

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

归档限制还可通过 `ADMIN_MAX_ARCHIVE_BYTES`、`ADMIN_MAX_ARCHIVE_MEMBERS`、`ADMIN_MAX_ARCHIVE_MEMBER_BYTES`、`ADMIN_MAX_ARCHIVE_TOTAL_BYTES`、`ADMIN_MAX_ARCHIVE_COMPRESSION_RATIO` 调整；V3.0.0 源码默认 `ADMIN_MAX_ARCHIVE_BYTES=8589934592`（8 GiB），普通 multipart 单请求另由 `ADMIN_MULTIPART_ARCHIVE_MAX_BYTES` 控制，默认仍为 512 MiB。图片像素上限使用 `ADMIN_MAX_IMAGE_PIXELS`，临时目录使用 `UPLOAD_TMP_DIR`。

若站点经过反向代理或托管网关，网页实际上传上限取应用配置与所有上游请求体限制中的较小值。上游拒绝可能在请求到达应用前返回 `413`；应同步检查代理配置。多 GiB 归档优先使用可恢复分片上传或 CLI。

### 安全边界

- 未配置 `ADMIN_TOKEN` 时，旧管理 API 隐藏为 `404`；管理 UI 登录也依赖管理凭据。
- 登录有限速；会话 Cookie 为 HttpOnly、SameSite=Strict，并由会话密钥签名。
- 管理会话失效后，HTML `GET` 请求返回 `303` 并跳转到 `/manage-images/login`；`POST` 等写请求仍返回 `401`，不会通过重定向重放写操作。
- 所有写操作要求 CSRF token；页面输出进行 HTML 转义。
- 图片和归档使用 `python-multipart` 流式解析，文件部分通过服务端磁盘 spool 处理，并设置请求、单图、归档成员和归档总量限制；普通归档优先原子接管同分区 spool，否则在检查最坏临时空间峰值后逐块复制，异常会清理受控目标，请求结束时会确定性关闭 spool。客户端文件名不会被当作服务端路径。
- 普通 multipart 待确认归档及其 preview 索引仍绑定当前内存 Session，登出、TTL 到期或进程重启后失效；V3 分片任务及 preview 则保存在 SQLite 与单个 `upload.bin` 中，可在同一浏览器保留签名上传所有者 Cookie、重新登录并重新选择同一文件后恢复。有效 owner Cookie 临近过期时会保持原 owner ID 和安全属性滚动续签；换浏览器、清 Cookie 或轮换 `ADMIN_SESSION_SECRET` 后不能接管旧任务。
- 生产环境应在 HTTPS 反向代理后使用，不要公开 Token、Cookie 或 WebDAV 凭据。

### 常用操作

1. **瀑布流浏览**：本地图片以响应式卡片展示并使用浏览器懒加载；预览只能在管理员登录会话中访问，不暴露宿主机文件路径。当前直接传输原图供预览，不额外生成缩略图，超大原图较多时会增加浏览器流量。
2. **方向与目录**：卡片分别显示“真实方向”和“存放目录”。管理员可把本地原图移动到 `desktop`、`mobile` 或 `square`，移动只改变归档位置，不伪造图片方向；同名冲突会生成安全的新文件名。
3. **标签**：创建、编辑、启用/禁用、合并；每张本地图片和 WebDAV 对象均可直接添加或移除标签。本地图片支持按当前筛选结果跨分页批量添加/移除，不受每页数量限制。一张图可关联多个标签而不复制文件。
4. **上传**：支持单图和多图上传，验证格式、像素和大小，按内容哈希去重，并按真实方向保存；正方形图片进入 `square/`。
5. **删除**：点击“删除本地原图”后由浏览器二次确认，页面不再要求手写 `DELETE`；支持按当前筛选结果跨分页批量删除本地图片。删除图片记录时，SQLite 通过 `ON DELETE CASCADE` 清理 `image_tags` 关系，但保留标签定义；服务端仍要求明确确认字段和 CSRF。WebDAV 只允许禁用、维护标签或清理本地缓存，绝不远程删除原图。
6. **WebDAV 预览**：已有本地缓存的远端对象可以预览；未缓存对象显示占位卡片，打开管理页不会批量下载远端原图。
7. **筛选**：可按来源、真实方向、存放目录、启用状态、缓存状态、标签和文件名/HREF 筛选，并选择每页数量。标签筛选提供“全部标签”“无标签”和用户创建标签；“无标签”表示不存在任何标签关系，因此关联了停用标签的图片不算无标签，并可与其他筛选、排序和分页组合。
8. **缓存**：可运行维护或清空 WebDAV 缓存；缓存可重建，不是永久图库。
9. **归档**：只接受 ZIP、TAR.GZ、TGZ。先上传到 preview，系统执行完整安全校验和 dry-run；预览页支持搜索和逐项排除图片，排除项仍完成安全检查但不会导入、占用导入容量或获得本次标签；核对摘要、上传前选择的多标签及归档成员父目录映射后再 confirm。上传前标签应用于本次所有图片，空表示不加标签。普通 multipart preview 绑定当前 Session；持久分片 preview 绑定签名上传所有者 Cookie，并受 24 小时任务 TTL 约束。

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
docker compose stop api
./scripts/backup.sh
docker compose down
docker compose build --pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:10086/health
```

V2 首次连接 SQLite 时自动、幂等地迁移 schema，并设置数据库版本。原图片、WebDAV 索引与未打标签行为继续保留；未打标签图片仍由 `/random` 使用，但不会自动出现在主题接口中。升级细节和回滚见 [MIGRATION.md](MIGRATION.md)。

## 10. Backup / Restore

Backup 与 Restore 都必须在 `api` 已停止写入时执行。标准流程会停止服务，并用第二条命令确认没有运行中的 Compose `api` 容器；成功时该命令不输出容器 ID：

```bash
docker compose stop api
docker compose ps --status running -q api
./scripts/backup.sh
# Restore 仍保持 api 停止；首次不确认只显示风险并退出
RESTORE_CONFIRM=YES ./scripts/restore.sh /path/to/backup-YYYY-MM-DD-HHMMSS.tar.gz
docker compose up -d
```

两个脚本都会 fail-closed 查询当前项目的 Compose 状态：确认 `api` 正在运行时必定拒绝；Docker/Compose 不可用或状态查询失败时也默认拒绝。只有已经通过其他方式确认所有写入停止，才可显式覆盖状态探测失败：

```bash
BACKUP_OFFLINE_CONFIRMED=1 ./scripts/backup.sh
RESTORE_CONFIRM=YES RESTORE_OFFLINE_CONFIRMED=1 \
  ./scripts/restore.sh /path/to/backup.tar.gz
```

备份是完整数据集，必须同时包含 `data/images/` 和 `data/database/images.db`；缺少任一项的归档不能 Restore。备份包含永久图库、SQLite 一致性副本和脱敏配置，不包含 WebDAV 缓存、真实 Secret 或分片临时 `upload.bin`，Manifest 不记录当前 VPS 的绝对项目路径。SQLite 快照可能带有 `upload_tasks` 行，因此 Restore 会在暂存数据库中清空这些任务行，最终删除默认分片临时目录，避免出现只有元数据、没有归档文件的伪恢复任务；失败回滚同时恢复旧图库、整个旧数据库目录和旧 `data/tmp/admin/chunked/`。

Restore 在展开前拒绝重复成员、链接、特殊文件和路径逃逸，并检查成员数、单成员大小、总展开量与磁盘剩余空间。安全默认值分别为 100000 个成员、单成员 1 GiB、总展开量 20 GiB；峰值空间按 `2 × 归档展开量 + 旧 live 数据量 + 256 MiB` 估算，以覆盖暂存、新 live 副本和旧数据回滚副本。可按受信备份和目标磁盘容量调整 `RESTORE_MAX_MEMBERS`、`RESTORE_MAX_MEMBER_BYTES`、`RESTORE_MAX_TOTAL_BYTES`、`RESTORE_FREE_SPACE_MARGIN_BYTES`，也可用 `RESTORE_EXPANDED_SPACE_FACTOR` 提高展开量倍率，但不能低于 2。恢复前的旧数据保存在 `backups/pre-restore-*`。跨 VPS 迁移流程见 [MIGRATION.md](MIGRATION.md)。

## 11. 开发验证

```bash
python -m pytest
python -m compileall -q app tests
bash -n scripts/*.sh docker-entrypoint.sh
```

本次 V3.0.0 发布候选镜像的完整 Python 测试为 `139 passed`，Node 上传校验行为测试通过。本轮还执行了 Python `compileall`、Node `--check`、Shell 语法和 `git diff --check`，并完成远程隔离运行态验收：容器为 `healthy`、版本为 `3.0.0`、Uvicorn 主进程 UID 为 `1000`；管理页面登录、V3.0.0 标识、批量操作入口和归档入口均通过真实 HTTP/HTML 合同检查。隔离容器、候选镜像和临时目录均已清理。

Docker Hub `v3` 已发布为 V3.0.0；Registry digest、linux/amd64 平台和镜像元数据已独立核验。V2.2.4 的历史验收记录仍保留在 [REPORT.md](REPORT.md) 中。

当前 V3 实施与验证记录见 [REPORT.md](REPORT.md)。


## V3.0.0 可恢复网页分片上传

管理网页保留普通 multipart 小归档上传；超过服务端建议分片大小时使用顺序 offset 分片。浏览器先读取服务端 capabilities，默认建议 8 MiB，遇到 `413` 自动降为 4/2/1 MiB，总体进度只按服务端确认的 `Upload-Offset` 计算。任务状态持久化到 SQLite，单任务临时目录只含一个 `upload.bin`。页面刷新不会自动取得本地文件；同一浏览器重新登录后需要重新选择同一文件，页面才会按文件指纹和服务端 offset 继续。

应用总上传上限、普通单请求上限和建议分片大小是三个独立概念。默认应用总上限 8 GiB、普通单请求 512 MiB、建议分片 8 MiB；浏览器遇到 `413` 会在服务端范围内自适应降为 4/2/1 MiB。`PATCH` 在读取 body 前执行同任务与进程级 in-flight admission，并直接逐块写入，避免完整分片的 `list + join` 双份内存。上传和普通 staging 检查 `UPLOAD_TMP_DIR` 所在文件系统，确认导入检查 `IMAGES_DIR` 所在文件系统，并为其他 receiving 任务保留尚承诺的字节。活动任务默认最多 2 个、TTL 24 小时；启动、管理请求和随应用关闭而停止的轻量周期任务都会清理过期数据。详见 [网页归档分片上传](docs/CHUNKED_UPLOAD.md)。
