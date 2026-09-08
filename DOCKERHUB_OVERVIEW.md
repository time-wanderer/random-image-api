# Random Image API：Docker Hub 与 V2.2 部署说明

Random Image API V2 是 V1 的向后兼容扩展，增加 tags、多对多主题、主题随机接口和安全管理 UI，同时保留 V1 的 `/random`、本地/Hybrid、WebDAV 默认 90% 远程优先、缓存、importer 和 Backup / Restore。V2.2 继续兼容 V2.0 的 API 与数据库，重点改善管理网页、图片预览、目录整理和标签编辑体验。

## 发布状态（请先阅读）

- **当前候选源码版本**：V2.2.3，已完成远程隔离构建、93 项 Python 测试与运行态验收，等待本轮发布
- **当前发布镜像版本**：V2.2.2（本轮发布前）
- **当前发布镜像**：`qinlingmonkey/random-image-api:v2`
- **V2.2.3 候选验收状态**：完整测试为 `93 passed`，Node 上传校验测试通过；容器 `healthy`、版本 `2.2.3`、未登录管理 HTML `GET` 的 `303` 登录回退、PID 1 UID `1000`、上传进度与多标签页面合同和原有 10 个容器稳定字段比对均通过
- **平台**：`linux/amd64`
- **V2.2.2 Manifest / Registry 摘要**：`sha256:7ca9a5958242417a0b1f9b5b5609a437f8158db55ab5323e989adcd098d0ae2f`
- **V2.2.2 Config 摘要**：`sha256:0c1a92c610d5af76bb115f8ceab8d4b70f10be778ee5cef6b0843b7b83267ef3`
- **镜像结构**：12 层；Entrypoint `/usr/local/bin/docker-entrypoint.sh`
- **V2.2.2 源码功能提交**：`d8752a0321404d8ad7ec2cfa3e2c8d06bf9bbd2b`（已同步 GitHub `main`）
- **V2.2.1 历史摘要**：Registry `sha256:bd1dc2e6fa5b0882cb9fb3d6bf8816d2f175e7bd05624b3140544d9efefbbda8`；Config `sha256:1088deae369cf6e5a3e370dc4beaf7399b672cb6988ec5686cd9d3b316956633`
- **V2.2.0 历史摘要**：Registry `sha256:268860bd1cb046f9a7f9f34dfc6ec6396e5748993f67d91cd202032f2189e490`；Config `sha256:faa555405f7b82bd824ee741791fbd8adfa3823b47b5289bddc825483e2cdd32`
- **兼容回滚版本**：`qinlingmonkey/random-image-api:v1`，继续保留且不会被 V2 覆盖
- **Docker Hub 线上 Overview**：本文件为同步来源；发布收尾时通过 API 回读正文并校验一致性

V2 是 V1 的扩展：旧的 `/random`、`?type=`、本地图库和 WebDAV Hybrid 部署可以继续使用；升级前仍应先备份 SQLite 与永久图片。

正式发布镜像的发布前验收使用一次性测试容器另行提供开发测试依赖；生产镜像按精简设计只安装运行依赖，不内置 `pytest`。生产镜像显式设置 `CACHE_DIR=/app/data/cache/webdav`，因此测试进程必须使用 `env -u CACHE_DIR`，以验证未显式配置缓存目录时会随临时 `DATA_DIR` 派生缓存路径；该操作仅隔离测试环境变量，不改变正式容器配置。本轮验证了版本 `2.2.1`、健康状态、UID `1000`、未登录 `401`、登录/CSRF、真实 PNG 上传、无标签筛选与标签关系变化、SQLite 关系、正式数据布局下的备份内容与脱敏。验收仅使用真实 HTTP/HTML 管理流程，未执行浏览器视觉验收；隔离容器、候选镜像和临时目录已清理，原有容器快照前后一致。正式发布后已独立回读并确认上述镜像元数据。

## 1. 使用 V2 镜像快速部署

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

`.env.example` 是完整配置模板，已经包含端口、管理页面、Local/WebDAV、缓存、扫描、归档限制和管理参数。默认 `STORAGE_MODE=local`，不填写 WebDAV 账号也可以启动。

只需把模板设置为镜像部署，并生成两个不同的管理 Secret：

```bash
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=v2/" .env
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
data/images/square/
```

服务按真实宽高判断方向。正方形图片从 V2.2 起保存到 `square/`；`SQUARE_POLICY` 只控制随机池归属。旧位置中的正方形图片继续兼容，不会自动迁移。V2 管理 UI 默认为：

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

V2 管理 UI 推荐使用两阶段流程：

1. **preview**：上传归档，先完整校验路径穿越、成员数、单文件/总大小、压缩比和真实图片格式，执行 dry-run，不写入永久图库；上传前可选择多个已有启用标签，空表示不加标签；
2. **confirm**：核对统计、上传前标签及归档成员父目录产生的目录标签映射后，才正式导入；预览页仍可复核修改。上传前选择的标签应用于本次所有图片。

待确认归档保存在服务端 `UPLOAD_TMP_DIR`，preview 索引只在进程内存；TTL、登出或进程重启会清理或使其失效。浏览器或 Cloudflare 上传链路可能另用本机或边缘临时存储，这些均不是永久图库。

默认网页归档上限仍为 512 MiB，没有提高。若经过 Cloudflare，网页实际上限取应用限制、计划限制和站点 **Network → Maximum Upload Size** 设置中的较小值。Cloudflare 官方当前列出的计划边界为 Free/Pro 100 MB、Business 200 MB、Enterprise 默认 500 MB（Enterprise 可在 Network 页面自助调整至 5 GB）；超过或把站点设置调低到请求大小以下均会返回 `413`。2.56 GiB 不适合网页路径，应使用 CLI。

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
- 归档上传返回 `413`：比较 `ADMIN_MAX_ARCHIVE_BYTES`、Cloudflare 计划上限及 Network → Maximum Upload Size，网页有效值取其中最小者；大归档改用 CLI。
- `.env` 修改未生效：执行 `docker compose up -d --force-recreate`。
- Permission denied：确保挂载目录可由镜像内非 Root 用户写入。
- WebDAV 主题未出现：主题必须是方向根目录下第一层、且名称可转换为合法 slug；然后重新同步。

完整源码、V1 快照和迁移文档见：`https://github.com/time-wanderer/random-image-api`。
