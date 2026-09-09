# Random Image API V2.2 迁移手册

本文覆盖三类迁移：V1→V2 原地升级、V2.0→V2.2 兼容升级，以及把 V2 连同永久数据迁移到新 VPS。V2.2 不改变 API 和 SQLite schema，重点增加响应式图片管理、独立 `square/` 目录、图片移动归档和更完整的标签编辑。

> 当前源码与 Docker Hub `qinlingmonkey/random-image-api:v2` 均为 V2.2.4，继续兼容 V2.2.3 的 API、SQLite schema 和数据目录。本补丁只调整管理端用户文案、错误提示和归档预览呈现，不迁移业务数据。升级会重启管理会话，管理员需要重新登录；尚未确认的归档 preview 需要重新上传并预览。发布镜像平台为 `linux/amd64`，Manifest/Registry 摘要为 `sha256:d00a6f75f028a913cb33062f80d9e3de68da6f82b4e88fb402b7cf64194257da`。

## 1. 数据边界

必须保留：

- `data/images/`：本地永久图库；
- `data/database/images.db`：图片元数据、tags、多对多关系、WebDAV 索引；
- 私下保存的部署配置和真实 Secret。

可重建、通常不迁移：

- `data/cache/webdav/`；
- `data/logs/`；
- Python 缓存、测试缓存、构建归档。

官方备份脚本包含图库、SQLite 一致性副本和脱敏配置，不包含缓存与真实 Secret。

## 2. V1→V2 原地升级

### 2.1 升级前检查

```bash
docker compose ps
curl -fsS http://127.0.0.1:10086/health
./scripts/backup.sh
```

把输出的备份文件复制到机器外部。另行安全保存真实配置；不要把 Secret 写进 Git 或迁移文档。

### 2.2 停止并切换到 V2

```bash
docker compose down
# 使用镜像部署时，把 Compose 中的 image 改为：
# image: qinlingmonkey/random-image-api:v2
# 从源码部署时，切换/复制经过审核的 V2 源码
cp .env.example .env.example.v2-reference
```

不要用新模板覆盖现有私密配置。对照 V2 配置补充管理 UI 变量，至少生成独立的管理令牌和会话密钥：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

分别写入 `ADMIN_TOKEN` 与 `ADMIN_SESSION_SECRET`。仅使用生成命令，不在文档、聊天或日志里粘贴真实值。

### 2.3 启动并触发原地迁移

```bash
docker compose pull       # Docker Hub 镜像部署
# docker compose build --pull  # 仅源码构建时使用
docker compose up -d
docker compose ps
docker compose logs --tail=100 api
curl -fsS http://127.0.0.1:10086/health
```

V2 首次连接旧 SQLite 时会幂等执行：

- 新建 `tags`、`image_tags`、`webdav_object_tags`；
- 为本地图片和 WebDAV 对象补充 V2 字段及索引；
- 保留旧图片和远端索引；
- 把 schema `user_version` 更新为 2。

旧图片默认仍是未打标签状态：`GET /random` 行为不变；只有关联标签后才会进入 `/random/{slug}` 或 `?tag=` 的主题结果。重复启动不会重复破坏数据。

### 2.4 V2.0→V2.2 兼容升级

V2.2 沿用 schema version 2，不需要单独执行数据库迁移。升级前仍应运行 `./scripts/backup.sh`，然后拉取通过验收的新镜像并重建服务。

V2.2 启动时会自动创建：

```text
data/images/square/
```

既有正方形图片即使位于根目录、`desktop/` 或 `mobile/` 仍能继续使用，不会被自动迁移。新上传或新导入的正方形图片进入 `square/`；需要整理旧文件时，可在管理网页中逐张移动。移动只改变物理归档目录，图片真实方向仍由像素宽高决定。

升级后重点验收：

- 管理页以瀑布流显示图片，预览只能在登录会话访问；
- 本地图片可移动至 `desktop`、`mobile`、`square`；
- 删除使用按钮二次确认，不再手写 `DELETE`；
- 本地图片和 WebDAV 对象均可添加或移除标签；
- WebDAV 未缓存对象只显示占位，不因打开管理页而下载原图。

### 2.5 验证兼容接口与 V2

```bash
curl -D - -o /dev/null http://127.0.0.1:10086/random
curl -D - -o /dev/null 'http://127.0.0.1:10086/random?type=mobile'
curl -i http://127.0.0.1:10086/random/not-created
```

最后一个请求预期为 `404`。登录管理 UI，创建标签并给图片关联后，再验证：

```bash
curl -D - -o /dev/null http://127.0.0.1:10086/random/<slug>
curl -D - -o /dev/null 'http://127.0.0.1:10086/random?tag=<slug>'
```

Hybrid 用户还应同步 WebDAV，并核对第一层主题目录映射、缓存与故障降级。

### 2.6 回滚到 V1

不要让 V1 长期直接写入已升级的 V2 数据库。安全回滚方式：

```bash
docker compose down
RESTORE_CONFIRM=YES ./scripts/restore.sh /path/to/升级前备份.tar.gz
# 切回 V1 源码或继续使用已保留的 :v1 镜像
docker compose up -d
```

恢复会先把当前图库和数据库保存到 `backups/pre-restore-*`。

## 3. 迁移到新 VPS

### 3.1 旧 VPS：冻结写入并备份

在维护窗口停止管理 UI 上传、删除、标签编辑和归档确认，然后：

```bash
docker compose ps
./scripts/backup.sh
docker compose down
sha256sum backups/backup-*.tar.gz
```

把以下内容通过受保护通道复制到新 VPS：

1. V2 项目源码（不含缓存、日志和构建产物）；
2. 最新 `backup-*.tar.gz` 及校验和；
3. 单独保存的真实部署配置。

### 3.2 新 VPS：准备项目

安装 Docker Engine 与 Docker Compose Plugin，然后：

```bash
cd /path/to/random-image-api
cp .env.example .env
chmod 600 .env
```

把真实配置安全写回 `.env`。检查端口、数据路径、管理会话密钥，以及 Hybrid 的 URL、允许主机、用户名和密码。不要依赖旧 VPS 的固定 IP、用户名或绝对路径。

### 3.3 恢复永久数据

```bash
sha256sum -c /path/to/backup.sha256
RESTORE_CONFIRM=YES ./scripts/restore.sh /path/to/backup.tar.gz
```

脚本恢复 `data/images/` 和 `data/database/`，清理 SQLite WAL/SHM 残留，并保留恢复前数据副本。WebDAV 缓存不恢复，服务会按需重建。

### 3.4 构建、启动和验收

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 api
curl -fsS http://127.0.0.1:10086/health
curl -D - -o /dev/null http://127.0.0.1:10086/random
```

主题验收：

- 已启用标签返回 `200`，响应头有 `X-Image-Tag`；
- 未知、禁用、非法或无候选标签返回 `404`；
- 数据库不可用，或远端失败且无法安全降级时返回 `503`；
- 同一图片可同时从多个标签接口返回；
- V1 `/random` 与 `?type=` 继续工作。

管理验收：登录、响应式瀑布流、受保护预览、标签增删改/合并、单图与批量标签、多图上传、三类目录移动、按钮删除确认、WebDAV 对象启停和标签维护、缓存维护、归档 preview-confirm。归档 preview 绑定会话且有 TTL，迁移或重启前必须确认完成或重新预览。

## 4. WebDAV 迁移注意事项

远端推荐布局：

```text
desktop/<tag-slug>/image.webp
mobile/<tag-slug>/image.webp
```

只有方向根目录下的第一层目录作为主题提示。新 VPS 必须能通过 HTTPS 访问配置的 WebDAV 主机，且主机在 `WEBDAV_ALLOWED_HOSTS` 中。迁移后执行：

```bash
curl -fsS -X POST -H 'X-Admin-Token: <ADMIN_TOKEN>' \
  http://127.0.0.1:10086/admin/webdav/sync
```

随后按主题请求图片，确认远端索引、缓存和 90% 远程优先策略符合预期。

## 5. 最小迁移清单

- [ ] 升级/迁移前健康检查正常。
- [ ] 已生成备份并复制到机器外。
- [ ] 已安全保存真实配置，未提交 Secret。
- [ ] 使用 `qinlingmonkey/random-image-api:v2` 拉取部署，或从已审核源码经 Docker Compose 构建。
- [ ] Restore 完成，图库和 SQLite 均存在。
- [ ] `/health`、V1 随机接口、主题接口已验证。
- [ ] 管理 UI 安全与写操作已抽查。
- [ ] Hybrid 用户已验证第一层主题、同步、缓存和降级。
- [ ] 回滚备份在验收结束前保留。
