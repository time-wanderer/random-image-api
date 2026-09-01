# Random Image API V2 实施报告

## 1. 报告范围

本报告记录 Random Image API V2 文档与当前本地实现的状态。V2 是 V1 的向后兼容扩展：保留 `/random`、`?type=`、本地图库、WebDAV Hybrid、默认 90% 远程优先、缓存、归档 importer、Backup / Restore，并新增 tags、多对多主题、主题接口和管理 UI。

Docker Hub 状态必须区分：`qinlingmonkey/random-image-api:v1` 已发布并继续保留；V2 镜像仅计划发布，当前尚未发布。

## 2. 信息来源与调研说明

本次**未进行网络调研**，不声称查阅了外部网站或在线文档。文档事实来自本地材料：

- 提交 `8235e19` 中的 V1 `README.md`；
- 当前工作树的 V2 源码；
- 当前测试代码；
- `docker-compose.yml`、`.env.example`、Backup / Restore 脚本。

因此，Docker Hub V2 发布状态按任务给定事实记录为“计划中、未发布”，未通过网络核验仓库页面。

## 3. 技术选择

继续采用单服务 FastAPI + SQLite + bind mount + Docker Compose：

- 单机随机读图不需要 Redis、Celery、消息队列、PostgreSQL 或 Kubernetes；
- SQLite 通过幂等 schema migration 增加标签关系，兼顾 V1 数据和低运维成本；
- 本地永久图库和 SQLite 位于 `./data`，容器删除或重建不会丢失；
- WebDAV 仍是可选扩展，索引轻量同步、图片按需缓存；
- 缓存可重建，不进入备份；永久图库和 SQLite 使用脚本备份。

## 4. V2 实现方案

### 4.1 标签与多对多

新增 `tags`、`image_tags`、`webdav_object_tags`。同一张本地图片或一个 WebDAV 对象可以属于多个主题，不复制原图。标签具有 slug、显示名称和启用状态。

V1 数据库连接后会幂等迁移到 schema version 2，补充本地图片与 WebDAV 对象字段和索引。旧图片保持未打标签状态，仍可由 `/random` 返回。

### 4.2 主题 API 与严格错误语义

新增：

- `GET /random/{slug}`
- `GET /random?tag={slug}`

保留：

- `GET /random`
- `GET /random?type=desktop|mobile`
- `GET /health`
- V1 管理 Token 接口

路径标签与查询标签冲突时返回 `400`。未知、禁用、非法或没有候选的标签请求返回 `404`。数据库不可用，或 WebDAV 故障且没有本地/缓存候选可安全降级时返回 `503`。

### 4.3 WebDAV 第一层主题与缓存

V2 可把 `desktop/`、`mobile/` 下第一层目录作为主题提示并写入远端对象标签关系。管理员手工添加的标签在同步后保留；管理员禁用的远端对象不会被同步重新启用。

保留 V1 默认 90% 远程优先、远端失败降级、ETag / Last-Modified 条件刷新、LRU 容量淘汰和随机缓存轮换。

### 4.4 管理 UI 与安全

管理 UI 支持：

- 图片分页、方向/来源/缓存/启用状态/标签筛选；
- 标签创建、编辑、启停和合并；
- 图片多标签批量关联；
- 多文件上传、真实格式/方向识别、像素和大小限制、哈希去重；
- 本地图片删除，要求明确输入大写 `DELETE`；
- WebDAV 对象启停、标签移除；
- WebDAV 缓存清空；
- ZIP / TAR.GZ / TGZ 归档 preview-confirm。

安全措施包括登录限速、签名会话、HttpOnly / SameSite=Strict Cookie、CSRF、输出转义、上传与归档资源限制。preview 与会话绑定并有 TTL；登出、过期或进程重启后不能继续确认。

### 4.5 Backup / Restore 与迁移

Backup 保存本地永久图库、SQLite 一致性副本、公开模板和脱敏配置，不保存 WebDAV 缓存与真实 Secret。Restore 覆盖前先保存当前图库和数据库，并清理 SQLite WAL/SHM 残留。

V1→V2 原地升级采用“先备份、停服务、替换源码、补充配置、Compose 重建、启动自动迁移、验收”的流程。跨 VPS 迁移采用源码 + 备份归档 + 私下保存配置三部分传输。

## 5. 本次文档工作

本次只修改或创建以下受允许文档：

| 文件 | 变更 |
| --- | --- |
| `README.md` | 重写为 V2 完整教程，说明兼容、API、WebDAV、管理 UI、配置、升级和备份 |
| `MIGRATION.md` | 增加 V1→V2 原地迁移、回滚和跨 VPS 迁移步骤 |
| `REPORT.md` | 真实记录本地来源、方案、测试状态、风险与待执行验收 |
| `DOCKERHUB_OVERVIEW.md` | 明确 V1 已发布保留、V2 未发布；区分 V1 镜像与 V2 源码部署 |
| `docs/V1.md` | 从提交 `8235e19` 提炼简洁 V1 快照，未长篇复制原文 |

本次已修改应用源码、测试、公开环境模板、部署文件、恢复脚本与文档；未读取或修改现有 `.env`、业务图片/数据库/日志、`backups`、`dist` 或 `.a0proj`。

## 6. 测试与验证状态

### 6.1 已有基线

- 基线测试记录：`57 passed`。

该数字作为任务给定的既有基线记录，不表示本次文档线程重新执行了完整基线。

### 6.2 当前已实际执行的专项命令

根据任务提供的当前执行记录：

- `tests/test_v2.py`：`4 passed`；
- 组合专项测试：`54 passed`。
- 首轮 V2 完整测试：`pytest -vv -s`，`69 passed, 1 warning in 18.84s`。
- 独立审查修复后的最终完整测试：`timeout 300 pytest -q`，`74 passed, 1 warning`，退出码 `0`。
- 最终 `compileall`、Shell 语法、`git diff --check` 与 Secret 扫描见最终验收记录。

这些结果均为实际执行记录；首轮为 `69 passed`，独立审查修复后的最终完整套件为 `74 passed`。

### 6.3 本文档线程实际执行

- 从本地 Git 提交读取 V1 README：`git show 8235e19:README.md`；
- 定向读取当前 V2 源码、测试、Compose、公开环境模板及 Backup / Restore 脚本；
- 文档完成后执行限定检查：四份已跟踪文档的 `git diff --check -- README.md MIGRATION.md REPORT.md DOCKERHUB_OVERVIEW.md` 返回 `0`；新建 `docs/V1.md` 使用 `git diff --no-index --check /dev/null docs/V1.md` 检查，无 whitespace 诊断（新文件存在差异时该命令按设计返回 `1`）。

### 6.4 最终验收

主线程已实际执行并通过：

- `timeout 300 pytest -q`：`74 passed`，仅有 FastAPI `TestClient` 的 `httpx2` 迁移弃用警告，退出码 `0`；
- `PYTHONPYCACHEPREFIX=/tmp/ria-pycache python -m compileall -q app tests`：返回 `0`；
- `bash -n scripts/backup.sh scripts/restore.sh scripts/build-image.sh` 与 `sh -n docker-entrypoint.sh`：返回 `0`；
- `git diff --check`：返回 `0`；
- 对可提交源码、测试、脚本、文档和公开配置进行高置信 Secret / 私钥模式扫描：无命中；
- Restore 预检使用 `/tmp` 隔离归档验证：安全归档接受，`../escape` 恶意成员拒绝。

### 6.5 远程隔离 Docker 验收

主线程在测试 VPS 的全新目录、独立 Compose 项目、独立镜像标签和随机空闲高位端口中完成验收。测试前后均核对服务器原有容器集合，未停止、替换或修改任何既有业务容器、网络、目录或数据。

- Compose 配置渲染和 Docker 镜像构建通过；
- 最终验收镜像 ID：`sha256:911fd314b95b6227a24f2100246d63cf0d1cdb29a7e46128fdab3a8c61e73357`，约 190 MB；
- 最终镜像内完整测试：`74 passed`，仅有 Starlette TestClient 的 `httpx2` 迁移弃用警告；
- Compose 容器达到 `healthy`，正式 Uvicorn 进程 UID 为 `1000`，持久化目录属主为 `1000:1000`；
- `/health`、V1 Desktop/Mobile `/random`、图片解码和 SQLite `PRAGMA integrity_check=ok` 通过；
- 自定义管理路径、错误登录拒绝、Session Cookie、CSRF 拒绝和管理页面控件通过；
- 中文主题、严格主题筛选、网页单图上传后主题立即可用通过；
- ZIP 归档 Preview→Confirm、真实宽高方向分类、默认标签和一级目录标签映射通过；
- 本地图片禁用后手动重扫不复活，容器重启后禁用状态仍持久化；
- Backup 使用一致性 SQLite 快照，包含永久图片和标签关系，排除 WebDAV 缓存并脱敏环境配置；停止 API 后 Restore 成功，恢复后的标签、禁用状态和主题 API 均通过。

远程验收过程中实际发现并修复两个发布阻断缺陷：

1. 中文标签显示名直接写入 HTTP Header 时，Starlette 的 Latin-1 编码会触发 `UnicodeEncodeError` 并返回 500。修复为保留 ASCII slug 的 `X-Image-Tag`，并将 `X-Image-Tag-Name` 按 UTF-8 百分号编码；新增中文回归测试。
2. 网页上传先扫描文件、后写标签，成功路径没有在标签事务提交后再次刷新 Catalog，导致 SQLite 已有标签但主题 API 暂时返回 404。修复为标签提交后重新扫描，并增加上传及归档后的内存主题索引回归断言。

Docker Hub `v2` 发布和发布后 Registry 摘要复验仍未执行，因此本报告当前不把镜像发布标记为完成。

## 7. 已解决问题

- 在不破坏 V1 `/random` 的前提下增加主题随机接口；
- 以多对多关系支持一图多主题；
- 区分“主题不存在/无候选”的 `404` 与基础设施不可用的 `503`；
- 将 WebDAV 第一层目录映射为主题提示；
- 提供带会话、CSRF 和限速的浏览器管理入口；
- 上传和归档导入增加资源限制、真实格式识别与内容去重；
- 归档采用 preview-confirm，避免未审阅即写入永久图库；
- V1 SQLite 可幂等原地升级，旧未标签图片继续服务；
- 文档不再误导用户认为 Docker Hub V2 已发布。
- 中文主题显示名不会再导致响应头编码异常；
- 网页上传完成后主题索引无需等待定时扫描即可立即使用；
- 远程 Compose、非 Root、持久化、管理 UI、归档导入及 Backup / Restore 已完成隔离验收。

## 8. 未解决问题与已知风险

- V2 Docker Hub 镜像尚未发布；当前只能从源码构建。发布完成后必须再更新本报告和使用教程。
- 管理会话与待确认 preview 存于单进程内存；容器重启会退出登录并使 preview 失效。这符合当前单实例设计，但不适用于多副本共享会话。
- 管理 UI 应部署在 HTTPS 反向代理后；仅改变 `ADMIN_PATH` 不是访问控制。
- `TRUSTED_PROXY_HEADERS=true` 只适合可信代理边界，直连公网时可能造成客户端 IP 日志被伪造。
- SQLite 适合当前单机部署；不应并发挂载给多个写实例。
- WebDAV 第一层目录必须能转换为合法 slug；更深层目录不形成层级主题。
- 文件系统与 SQLite 无法形成真正的跨资源原子事务；实现通过预校验、原子落位和失败补偿降低风险，极端掉电窗口仍可能需要下一次 scan 修复状态。

## 9. 后续建议

1. 将当前验收通过的源码先推送 GitHub，并核对远端 commit。
2. 将同一验收镜像发布为 `qinlingmonkey/random-image-api:v2`，再通过 Registry API 核对摘要与 `linux/amd64` 平台。
3. Docker Hub V2 真正发布后更新 README、MIGRATION、REPORT 和 Docker Hub Overview；发布前保持“未发布”表述。
4. 保留 V1 镜像与 `docs/V1.md`，为回滚和旧部署维护提供基线。
