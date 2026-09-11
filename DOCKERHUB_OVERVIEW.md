> V3.0.0 的持久化可恢复分片上传已完成提交、GitHub 同步、远程隔离验收并发布到 Docker Hub。镜像标签为 `qinlingmonkey/random-image-api:v3`；已有 `v1`、`v2` 均继续保留。

# Random Image API：Docker Hub 与 V3.0.0 部署说明

Random Image API V3.0.0 在 V1/V2 能力基础上提供持久化可恢复网页分片上传，并整合 tags、多对多主题、主题随机接口、安全管理 UI、本地/Hybrid、WebDAV、缓存、importer 和 Backup / Restore。V3 保持既有 `/random` 与 `?type=` 接口兼容，并进一步完善批量图片管理和归档筛选。

## 发布状态（请先阅读）

- **当前工作区源码版本**：V3.0.0（已发布）
- **当前发布镜像版本**：V3.0.0，已完成远程隔离验收与 Registry 回读
- **当前发布镜像**：`qinlingmonkey/random-image-api:v3`
- **V3.0.0 Manifest digest**：`sha256:a8951d587fd88e81af9ba25b4ab912a76cb29238ce8b72d406e7a80b4b34e6cd`
- **V3.0.0 源码提交**：`69e1f0b27cfc241b86f3555f905faa25483e1b74`
- **V3.0.0 验收状态**：完整测试为 `139 passed`，Node 上传校验测试通过；容器 `healthy`、版本 `3.0.0`、PID 1 UID `1000`，管理总览与归档预览的用户文案、结构化摘要、标签提示和内部信息隔离均通过 HTTP/HTML 合同检查
- **平台**：`linux/amd64`
- **V3.0.0 Manifest / Registry 摘要**：`sha256:a8951d587fd88e81af9ba25b4ab912a76cb29238ce8b72d406e7a80b4b34e6cd`
- **镜像结构**：12 层；Entrypoint `/usr/local/bin/docker-entrypoint.sh`
- **V3.0.0 源码功能提交**：`0a9b07bd55955d5df7489e1cee5f96577797c24e`（已同步 GitHub `main`）
- **V2.2.3 历史摘要**：Registry `sha256:4b752bb391020f90fbf15c5e902d2f767b7b62d702e5731ab21ee80a13ccf82a`；Config `sha256:bb31ae8011517dfffd685fc8063717793e630211da46844224dad52738128fff`
- **V2.2.2 历史摘要**：Registry `sha256:7ca9a5958242417a0b1f9b5b5609a437f8158db55ab5323e989adcd098d0ae2f`；Config `sha256:0c1a92c610d5af76bb115f8ceab8d4b70f10be778ee5cef6b0843b7b83267ef3`
- **V2.2.1 历史摘要**：Registry `sha256:bd1dc2e6fa5b0882cb9fb3d6bf8816d2f175e7bd05624b3140544d9efefbbda8`；Config `sha256:1088deae369cf6e5a3e370dc4beaf7399b672cb6988ec5686cd9d3b316956633`
- **V2.2.0 历史摘要**：Registry `sha256:268860bd1cb046f9a7f9f34dfc6ec6396e5748993f67d91cd202032f2189e490`；Config `sha256:faa555405f7b82bd824ee741791fbd8adfa3823b47b5289bddc825483e2cdd32`
- **兼容回滚版本**：`qinlingmonkey/random-image-api:v1`，继续保留且不会被 V2 覆盖
- **Docker Hub 线上 Overview**：本文件为同步来源；发布收尾时通过 API 回读正文并校验一致性

V3 是 V1/V2 的兼容升级：旧的 `/random`、`?type=`、本地图库和 WebDAV Hybrid 部署可以继续使用；升级前仍应先备份 SQLite 与永久图片。

V3.0.0 将管理页面与内部技术说明明确分层：页面仅展示支持格式、当前配置上限、标签作用范围、上传状态和可执行错误提示；服务端临时目录、进程状态、代理实现及固定故障样例不再出现在操作页面。归档 preview 使用结构化中文摘要，确认前仍可调整本次全部图片的标签。正式发布后已独立回读并确认上述镜像元数据。

## 1. 使用 V3 镜像快速部署

创建项目目录，并下载仓库提供的完整环境配置模板：

```bash
mkdir -p random-image-api
cd random-image-api
curl -fsSLO https://raw.githubusercontent.com/time-wanderer/random-image-api/main/.env.example
cp .env.example .env
```

创建仅使用预构建镜像的 `compose.yml`：

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

`.env.example` 是完整配置模板，已经包含端口、管理页面、Local/WebDAV、缓存、扫描、归档限制和管理参数。默认 `STORAGE_MODE=local`，不填写 WebDAV 账号也可以启动。

只需把模板设置为镜像部署，并生成两个不同的管理 Secret：

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

默认管理登录地址：

```text
http://<主机>:10086/manage-images/login
```

直接通过 HTTP 测试时可使用 `ADMIN_COOKIE_SECURE=false`；生产环境应配置 HTTPS 反向代理并改为 `true`。仅修改 `ADMIN_PATH` 不能替代 Token、Session Cookie 与 CSRF 防护。

### 1.1 按需启用 WebDAV

完整 `.env.example` 已预先包含 WebDAV 配置。默认 `STORAGE_MODE=local`，不会连接 WebDAV；需要时只需切换模式并填写服务信息：

```bash
sed -i 's/^STORAGE_MODE=.*/STORAGE_MODE=hybrid/' .env
vi .env
```

至少设置：

```env
STORAGE_MODE=hybrid
WEBDAV_BASE_URL=https://dav.example.com/remote.php/dav/files/user/
WEBDAV_ALLOWED_HOSTS=dav.example.com
WEBDAV_USERNAME=your-webdav-user
WEBDAV_PASSWORD=your-webdav-password
```

`WEBDAV_DESKTOP_ROOT` 和 `WEBDAV_MOBILE_ROOT` 默认分别为 `/desktop/`、`/mobile/`，通常无需修改。保存后重建容器：

```bash
docker compose up -d --force-recreate
curl -fsS http://127.0.0.1:10086/health
```

WebDAV 密码只保存在服务器 `.env`，不要提交到 GitHub、备份或 Docker Hub。

## 2. 可选：从源码构建 V3

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

## 3. V3 API

V1 接口完全保留：

```bash
curl -D - -o image.bin http://127.0.0.1:10086/random
curl -D - -o image.bin 'http://127.0.0.1:10086/random?type=desktop'
```

V3 主题接口：

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
data/images/square/
```

服务按真实宽高判断方向。正方形图片从 V3.0.0 起保存到 `square/`；`SQUARE_POLICY` 只控制随机池归属。旧位置中的正方形图片继续兼容，不会自动迁移。V3 管理 UI 默认为：

```text
http://<主机>:10086/manage-images
```

可配置 `ADMIN_PATH`。管理 UI 支持：

- 响应式统计卡片和本地图片瀑布流；
- 仅管理员会话可访问的懒加载图片预览；
- 创建、编辑、启停、合并标签；
- 给单张或多张本地图片添加/移除标签；
- 给 WebDAV 对象添加/移除标签；
- 图片标签筛选提供“全部标签”“无标签”和用户创建标签；“无标签”表示不存在任何标签关系，关联停用标签的图片不算无标签，并可与来源、方向、目录、状态、搜索、排序和分页组合；
- 多文件上传、JPG/JPEG/PNG/WebP 类型与每文件大小预检、哈希去重；
- ZIP/TAR.GZ/TGZ 归档上传前校验、真实上传进度、代理/网络错误恢复和无 JS 普通表单 fallback；
- 把本地图片移动到 `desktop`、`mobile` 或 `square`，且不改变真实方向；
- 点击按钮后二次确认删除本地原图，不再手写 `DELETE`；
- 按当前筛选结果跨分页批量添加/移除标签和删除本地图片；删除时 SQLite `image_tags` 关系自动级联清理，标签定义保留；
- 归档预览支持搜索和逐项排除，排除项仍安全校验但不进入导入、容量统计或本次标签。
- WebDAV 对象启停、缓存维护和清空；未缓存对象只显示占位，不自动下载。

管理 UI 使用签名会话、HttpOnly / SameSite=Strict Cookie、登录限速与 CSRF。生产环境应置于 HTTPS 反向代理后，并设置独立随机的 `ADMIN_TOKEN` 与 `ADMIN_SESSION_SECRET`。

## 5. 归档 preview-confirm 与 importer

命令行 importer 支持 ZIP、TAR.GZ、TGZ、多次 `--tag` 和 `--database-path`，只处理归档，不直接接受目录：

```bash
python -m app.importer /path/to/images.zip --output-dir ./data/images --dry-run
python -m app.importer /path/to/images.tar.gz \
  --output-dir ./data/images \
  --database-path ./data/database/images.db \
  --tag nature --tag featured
```

文件夹应先用 `tar -czf /tmp/photos.tar.gz -C /path/to photos` 打包后运行 CLI；也可复制图片到 `data/images/{desktop,mobile,square}/` 后调用 `POST /admin/rescan`。

V3 管理 UI 推荐使用两阶段流程：

1. **preview**：上传归档，先完整校验路径穿越、成员数、单文件/总大小、压缩比和真实图片格式，执行 dry-run，不写入永久图库；上传前可选择多个已有启用标签，空表示不加标签；
2. **confirm**：核对统计、上传前标签及归档成员父目录产生的目录标签映射后，才正式导入；预览页仍可复核修改。上传前选择的标签应用于本次所有图片。

待确认归档保存在服务端 `UPLOAD_TMP_DIR`，preview 索引只在进程内存；TTL、登出或进程重启会清理或使其失效。浏览器或上游网关可能另用本机或边缘临时存储，这些均不是永久图库。

普通 multipart 网页归档上限默认仍为 512 MiB。若经过反向代理或托管网关，实际上限取应用限制与链路中所有上游请求体限制的最小值；大归档优先使用可恢复分片上传或 CLI。

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

V3 把 `desktop/` 或 `mobile/` 下第一层目录视为 tag 提示。同步只保存索引；图片命中后才下载到缓存。管理员手工禁用的远端对象不会因后续同步被重新启用。

默认 `HYBRID_REMOTE_PROBABILITY=0.9`，即保留 V1 的 90% WebDAV 优先策略。远端失败时使用当前方向的缓存/本地候选，再按配置做方向回退。缓存继续支持条件刷新、LRU 容量淘汰和随机轮换。

```bash
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/webdav/sync
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/cache/maintain
```

## 7. V3 关键环境变量

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
docker compose stop api
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
docker compose stop api
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
- 归档上传返回 `413`：比较应用限制与所有上游请求体限制，网页有效值取其中最小者；大归档使用可恢复分片上传或 CLI。
- `.env` 修改未生效：执行 `docker compose up -d --force-recreate`。
- Permission denied：确保挂载目录可由镜像内非 Root 用户写入。
- WebDAV 主题未出现：主题必须是方向根目录下第一层、且名称可转换为合法 slug；然后重新同步。

完整源码、V1 快照和迁移文档见：`https://github.com/time-wanderer/random-image-api`。


## 源码 V3.0.0：网页大归档分片上传

V3 管理页保留普通 multipart 小归档上传，并为大归档提供服务端 capabilities、顺序 offset、断点恢复、默认 8 MiB 与 `413` 自适应降档、SQLite 任务持久化、取消操作和单 `upload.bin` 临时存储。`PATCH` 在读取 body 前执行同任务与进程级 in-flight admission，获准后直接逐块写入。进度只采用服务端确认 offset；刷新后需在同一浏览器重新选择同一文件。任务绑定独立签名上传所有者 Cookie，临近过期时保持 owner 身份滚动续签。完成上传后继续使用既有 importer dry-run、结构化预览和确认导入，确认成功后立即清理任务文件。

上传与普通 multipart staging 按 `UPLOAD_TMP_DIR` 所在文件系统检查容量，确认导入按 `IMAGES_DIR` 所在文件系统检查，并保留其他 receiving 任务尚承诺的字节；跨文件系统 multipart 复制会先检查 spool 与受控副本短时并存的峰值。过期任务由启动、管理请求和轻量单实例周期任务清理。标准 Backup 不复制临时 `upload.bin`，Restore 会清空快照内 `upload_tasks` 并删除默认分片临时目录。默认参数及完整恢复边界见仓库 `docs/CHUNKED_UPLOAD.md`。Docker Hub `v3` 已发布，不覆盖或删除 `v1`、`v2`；V3 镜像包含上述分片上传功能。
