# Random Image API V2.1 实施报告

## 1. 报告范围

本报告记录 Random Image API V2.1 文档与当前本地实现的状态。V2.1 保留 V1/V2 API、WebDAV Hybrid、缓存、归档 importer、Backup / Restore、tags 和主题接口，重点优化管理 UI、图片预览、物理目录整理与标签编辑。

V2.1.0 已通过本地与远程隔离验收，最终完整测试为 `79 passed`。在本次发布完成前，Docker Hub `qinlingmonkey/random-image-api:v2` 仍指向已发布的 V2.0.0（Registry 摘要 `sha256:22097fbcb95a953c99a4c32a4c0381bfdc817bc25e6e2825272694a1d9d126cb`）；`v1` 继续保留。GitHub 与 Docker Hub 的最终发布结果将在完成实际推送和 Registry 回读后补充。

## 2. 信息来源与调研说明

实现与部署说明主要来自以下已实际核对的材料和环境：

- 提交 `8235e19` 中的 V1 `README.md`；
- 当前工作树的 V2 源码；
- 当前测试代码；
- `docker-compose.yml`、`.env.example`、Backup / Restore 脚本。
- 测试 VPS 上的隔离 Docker Compose 构建、运行、管理 UI、归档和 Backup / Restore 验收；
- Docker Hub Registry API 对 `v2` manifest、配置 Blob、摘要与平台的发布后回读。

本次未为技术选型重新进行广泛网络调研；FastAPI、SQLite、WebDAV 与 Docker Compose 的选型延续已交付 V1。Docker Hub 发布状态则已通过 Registry API 实际核验，不依赖页面展示推断。

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
- 本地图片使用按钮二次确认删除，服务端校验 CSRF 与明确确认字段，并兼容旧 `DELETE` 字段；
- WebDAV 对象启停、标签添加与移除，但不提供远程原图删除；
- WebDAV 缓存清空；
- ZIP / TAR.GZ / TGZ 归档 preview-confirm。

安全措施包括登录限速、签名会话、HttpOnly / SameSite=Strict Cookie、CSRF、输出转义、上传与归档资源限制。preview 与会话绑定并有 TTL；登出、过期或进程重启后不能继续确认。

### 4.5 Backup / Restore 与迁移

Backup 保存本地永久图库、SQLite 一致性副本、公开模板和脱敏配置，不保存 WebDAV 缓存与真实 Secret。Restore 覆盖前先保存当前图库和数据库，并清理 SQLite WAL/SHM 残留。

V1→V2 原地升级采用“先备份、停服务、替换源码、补充配置、Compose 重建、启动自动迁移、验收”的流程。跨 VPS 迁移采用源码 + 备份归档 + 私下保存配置三部分传输。

### 4.6 V2.1 管理体验与目录整理

V2.1 新增或完善：

- 响应式统计卡片、筛选表单和图片瀑布流；
- 仅管理员签名会话可访问的本地图片与 WebDAV 缓存预览；
- WebDAV 未缓存对象使用占位卡片，打开管理页不会批量下载远端原图；
- 本地图片在 `desktop`、`mobile`、`square` 之间安全移动，数据库 ID、标签和启停状态保持；
- 上传与 importer 将正方形图片保存到独立 `data/images/square/`，`SQUARE_POLICY` 只控制随机池；
- 删除改为按钮二次确认，服务端仍校验 CSRF 与明确确认字段；
- 单张及批量本地图片标签添加/移除，以及 WebDAV 对象标签添加/移除；
- 来源、真实方向、存放目录、启用、缓存、标签和文件名/HREF 筛选。

预览路径只由数据库 ID 或精确 HREF 解析，执行登录校验、参数化查询、路径 containment 与符号链接拒绝。移动使用同文件系统 `os.replace`，数据库失败时回移；删除先把文件原子移动至隔离名称，数据库事务失败时恢复。WebDAV 不提供远程删除。

## 5. 本次文档工作

本次只修改或创建以下受允许文档：

| 文件 | 变更 |
| --- | --- |
| `README.md` | 重写为 V2 完整教程，说明兼容、API、WebDAV、管理 UI、配置、升级和备份 |
| `MIGRATION.md` | 增加 V1→V2 原地迁移、回滚和跨 VPS 迁移步骤 |
| `REPORT.md` | 真实记录本地来源、方案、测试状态、风险与待执行验收 |
| `DOCKERHUB_OVERVIEW.md` | 以 V2 为主教程，提供镜像部署、管理 UI、主题 API、WebDAV、备份恢复，并保留 V1 回滚说明 |
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

### 6.4 V2.1 本地与浏览器验证

主线程已实际执行：

- 管理端与 importer 专项测试：`24 passed`；
- 最终完整测试：`pytest -q`，`79 passed`，仅有 Starlette TestClient 的 `httpx2` 迁移弃用警告；
- Python `compileall`、Shell 语法、`git diff --check`、Markdown 围栏、可提交文件 Secret 模式和运行数据跟踪检查均通过；
- 新增回归覆盖管理员预览鉴权、图片瀑布流标记、预览 Content-Type 与缓存头、符号链接拒绝、Square 上传与 Restore 目录、移动时同名冲突、真实方向与存放目录分离、按钮删除确认、单图标签添加/移除、Catalog 立即刷新、WebDAV 标签添加/移除、已缓存预览和未缓存占位，以及 Docker ENTRYPOINT 可执行权限。

V2.1 浏览器视觉检查使用 `/tmp` 全新数据目录、5 张隔离测试图和无头 Chromium 149 实际执行：管理员登录成功；桌面端按 CSS Columns 显示 4 列瀑布流；390×844 手机视口显示 1 列；本地图片预览、筛选区、真实方向、存放目录、Square 卡片和操作控件均渲染正常；两种视口都没有横向溢出。临时 Uvicorn、截图和 `/tmp` 数据已全部清理。

### 6.5 V2.1 远程隔离 Docker 验收

主线程在测试 VPS 的全新 `/tmp` 目录、独立 Compose 项目、独立镜像标签和未占用高位端口中完成验收。测试前记录服务器原有容器快照；清理后原有 10 个容器的名称、镜像和状态与测试前一致，未停止、替换或修改任何既有业务容器、目录或数据。

- 最终候选源码归档经清单与路径审计，共 42 个文件，不含 `.env`、Git 元数据、业务图片、数据库、日志、缓存、备份包或镜像归档；
- Compose 配置渲染、Docker 构建和最终镜像内完整测试通过，最终镜像 ID 为 `sha256:036bd4609853103fd179c8f17c7cacf897d915a5b644851a4663c7cabfdfe827`；
- 镜像内完整测试为 `79 passed`，生产镜像内容在一次性测试容器运行前后保持不变；
- Compose 容器达到 `healthy`，`/health` 返回版本 `2.1.0`，Uvicorn PID 1 以 UID `1000` 运行，SQLite `PRAGMA integrity_check=ok`；
- 真实 HTTP 管理流程通过：未登录预览拒绝、登录与 CSRF、Square 上传到 `square/`、瀑布流卡片、受保护 PNG 预览、单图标签添加/移除、`desktop↔square` 移动且真实方向保持 Square、主题随机接口、缺少确认的删除拒绝以及 `confirm=1` 友好删除；
- Backup/Restore 通过：备份包含 Square 原图和 SQLite 标签关系，不包含 WebDAV 缓存或测试 Secret；停止服务后恢复成功，恢复前后图片 ID、Square 路径、真实方向和主题关系一致，恢复后的主题接口返回可解码 PNG；
- 本轮隔离容器、Compose 网络、源码目录与临时归档均已删除；仅保留经过验收的独立镜像用于发布，发布后再删除。

远程验收实际发现并修复两个发布阻断问题：

1. Git 索引记录 `docker-entrypoint.sh` 为可执行，但工作区归档曾保存为 `0600`，导致生产容器启动时报 `permission denied`。现由 Dockerfile 使用 `COPY --chmod=0755` 显式保证镜像内权限，并增加回归测试。
2. Restore 脚本重建图片目录时遗漏空的 `square/`。现显式创建 `desktop/`、`mobile/`、`square/`，并增加脚本契约测试。

### 6.6 V2.0 已发布版本的历史验收

以下记录属于已发布 V2.0.0，作为 V2.1 验收前的历史基线：

主线程已实际执行并通过：

- `timeout 300 pytest -q`：`74 passed`，仅有 FastAPI `TestClient` 的 `httpx2` 迁移弃用警告，退出码 `0`；
- `PYTHONPYCACHEPREFIX=/tmp/ria-pycache python -m compileall -q app tests`：返回 `0`；
- `bash -n scripts/backup.sh scripts/restore.sh scripts/build-image.sh` 与 `sh -n docker-entrypoint.sh`：返回 `0`；
- `git diff --check`：返回 `0`；
- 对可提交源码、测试、脚本、文档和公开配置进行高置信 Secret / 私钥模式扫描：无命中；
- Restore 预检使用 `/tmp` 隔离归档验证：安全归档接受，`../escape` 恶意成员拒绝。

### 6.7 V2.0 远程隔离 Docker 验收

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

GitHub `main` 已同步 V2 源码提交 `304d5ff48dd6f82904066dd54aa436d651a997d5`。同一远程验收镜像已发布为 `qinlingmonkey/random-image-api:v2`；发布后通过 Docker Registry API 独立回读 manifest 和配置 Blob，确认摘要为 `sha256:22097fbcb95a953c99a4c32a4c0381bfdc817bc25e6e2825272694a1d9d126cb`、配置摘要为 `sha256:911fd314b95b6227a24f2100246d63cf0d1cdb29a7e46128fdab3a8c61e73357`、平台为 `linux/amd64`、共 10 层。

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

- 当前 `v2` 仅发布 `linux/amd64`；ARM64 主机需要自行从源码构建，或等待后续多架构镜像。
- 管理会话与待确认 preview 存于单进程内存；容器重启会退出登录并使 preview 失效。这符合当前单实例设计，但不适用于多副本共享会话。
- 管理 UI 应部署在 HTTPS 反向代理后；仅改变 `ADMIN_PATH` 不是访问控制。
- `TRUSTED_PROXY_HEADERS=true` 只适合可信代理边界，直连公网时可能造成客户端 IP 日志被伪造。
- SQLite 适合当前单机部署；不应并发挂载给多个写实例。
- WebDAV 第一层目录必须能转换为合法 slug；更深层目录不形成层级主题。
- 文件系统与 SQLite 无法形成真正的跨资源原子事务；实现通过预校验、原子落位和失败补偿降低风险，极端掉电窗口仍可能需要下一次 scan 修复状态。

## 9. 后续建议

1. 保留 V1 镜像与 `docs/V1.md`，为回滚和旧部署维护提供基线。
2. 后续评估构建 `linux/arm64` 多架构镜像；发布前不要把当前 `v2` 描述为多架构。
3. 生产部署应使用 HTTPS 反向代理、随机独立 Secret、WebDAV 只读应用账号，并定期把 Backup 复制到机器外。
4. 关注 Starlette TestClient 的 `httpx2` 迁移弃用警告，在上游兼容窗口内更新测试依赖。
