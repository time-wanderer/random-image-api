# V3.0.0 持久化分片上传实施记录（未发布）

## 范围与状态

本轮直接在项目工作区实现网页大归档分片上传，已完成候选镜像构建、完整自动化测试和远程隔离运行态验收，但尚未提交、推送或发布正式镜像，也未部署到生产环境。计划镜像标签为 `v3`，已有 `v1`、`v2` 继续保留；当前已发布镜像仍为 V2.2.4，以下内容只描述 V3.0.0 未发布源码。

## 技术选型与实现

- 保留普通 multipart 小归档和无 JavaScript fallback；大归档使用浏览器 `File.slice()`、顺序 offset 和服务端能力协商。
- SQLite 新增 `upload_tasks`，schema 版本升至 3；任务不绑定所有管理员共用的 `ADMIN_TOKEN`，而是绑定独立随机、签名、HttpOnly 上传所有者 Cookie 的不可逆派生值。所有接口仍要求管理 Session，写请求仍要求 CSRF。
- 每个任务仅保存一个 `upload.bin`。`PATCH` 在读取请求体前执行同任务与单进程全局 in-flight admission：同任务并发稳定返回 `409 upload_in_progress`，全局超额返回 `429 too_many_inflight_chunks`，拒绝请求不会改变 offset。获准请求直接按 ASGI 数据块写入文件，不再使用 `list + join` 保留完整分片及其副本；失败截断回滚，`flush/fsync` 后再事务推进 offset。
- 完成阶段重新读取全文件，核对总长度并计算 SHA-256，随后复用 importer dry-run、归档成员检查、预览和确认导入。
- 上传任务创建、分片写入与普通 multipart staging 使用 `UPLOAD_TMP_DIR` 所在文件系统的空间；确认导入使用 `IMAGES_DIR` 所在文件系统。图库容量预算同时扣除其他 `preview_ready` 任务的预计导入字节和其他 `receiving` 任务尚承诺的剩余上传字节，支持临时目录与图库位于不同挂载点。
- 普通 multipart 优先原子移动可信的服务端 Starlette spool；无法同分区移动时，先按 spool 与受控副本短时并存的最坏峰值检查容量，再逐块复制。实现不访问客户端路径，复制、空间不足和后续归档校验异常均会清理受控目标。
- 使用应用总上限、活动任务数、剩余字节预留、24 小时 TTL、随机任务目录、路径 containment、`O_NOFOLLOW`、跨进程 `flock`、过期与孤儿清理保护磁盘和一致性。启动、管理请求以及随应用启动和关闭的轻量单实例周期任务都会执行 TTL 清理，不引入 Celery 或 Redis。
- 上传所有者 Cookie 在临近过期时沿用同一 owner ID 滚动续签，并保持 HttpOnly、SameSite、Path 和 Secure 判定语义；任务恢复身份不会因续签而变化。
- 默认保留 256 MiB 空闲空间。这一默认值优先兼顾资源有限的小型 VPS；数据库、日志、上传临时目录或图库共用分区时建议按部署容量提高。
- 标准备份不包含活动临时归档。Backup 与 Restore 均先 fail-closed 查询当前 Compose `api`：明确运行时不可覆盖，Docker/Compose 或状态查询不可用时只有显式离线确认变量才允许继续。完整归档强制同时存在 `data/images/` 与 `data/database/images.db`；Manifest 使用可迁移的项目标识，不记录 VPS 绝对项目路径。
- Restore 在展开前拒绝规范化后的重复成员、链接、特殊文件与路径逃逸，并限制成员数、单成员和总展开量；默认按 `2 × 归档展开量 + 旧 live 数据量 + 256 MiB` 估算峰值空间，展开倍率最低为 2 且可提高。Restore 先在暂存数据库中清空 `upload_tasks` 并执行 `PRAGMA integrity_check`；旧图库、整个旧数据库目录和旧 `data/tmp/admin/chunked/` 一并进入安全副本，切换失败时保持三者一致回滚，成功时才最终删除分片临时目录。

## 独立审查与修复

- 修复确认导入异常回滚的数据丢失竞态：不再以“全图库前后差集”推断本次文件，只删除 importer 明确返回且仍位于图库根目录内的 `created_paths`，不会误删并发操作产生的文件。
- confirm 在跨进程全局导入锁内重新执行安全 dry-run，以当前图库状态刷新预计新增字节；同 digest 已被其他任务导入后按 0 新增量检查容量，不重复扣除已上传 archive。
- 修复归档成员名与 chunked_create 文件名的真实 NUL 校验，避免把字面 `\x00` 错当 NUL。
- 持久 preview 的确认导入、任务消费和临时目录删除在同一文件锁内完成；确认失败保留任务供重试，成功后并发重复确认无法再次导入。
- 修复浏览器取消逻辑：只有服务端返回 `204`、`404` 或 `410` 才清除本地恢复索引；`401` 转登录，其他错误保留任务并提示重试。
- 服务端强制执行协商最小分片，只有最后一片允许小于最小值。
- 修复 Backup/Restore 在线执行的跨资源一致性风险，并为 Restore 增加完整归档要求、资源配额、安全展开、空间检查，以及覆盖图库、整个数据库目录与分片目录的一致失败回滚。
- 已复核 `UploadStore.locked()`：打开或 `fdopen` 异常会关闭原始 fd，nonblocking `flock` 的 `BlockingIOError` 会经过 `finally` 关闭 handle；清理遇到已持锁任务会跳过，不删除活跃任务。
- 定位远程合法 ZIP 在 chunked complete 返回 `400 invalid_archive` 的真实根因：分片任务统一保存为 `upload.bin`，而 importer 和成员摘要读取此前只按磁盘路径扩展名识别格式。现在从受信任任务行读取 `.zip`、`.tar.gz` 或 `.tgz`，在 complete 的 dry-run、成员摘要以及后续 confirm 的再次 dry-run/正式导入中显式传递格式；没有放宽归档安全校验。
- complete 在进入鉴权前将 `owner_key` 初始化为空，仅在成功取得所有者后记录任务错误；底层异常使用任务 ID、异常类型和固定错误码结构化记录并保留服务端 traceback，客户端仍只收到不泄漏内部异常的固定中文错误。
- 稳定请求错误契约：PATCH 声明或实际分片超过专用上限统一返回 `chunk_too_large`；初始化 JSON 请求过大仍返回 `request_too_large`；合法 JSON 但顶层不是 object 返回 `invalid_request`，仅语法或编码损坏返回 `invalid_json`。
- 管理总览继续引用外部 `admin-upload.js`，未重新内联。Python 页面测试只验证 script src、可缓存脚本响应和上传 data attributes；上传、降档、续传与取消行为由 `tests/test_admin_upload.js` 验证。
- Restore 测试改为运行脚本并验证恢复结果，不再匹配旧硬编码字面；集成测试发现缺失方向目录后，脚本通过变量化 live 图片路径确保 `desktop/`、`mobile/`、`square/` 均存在。
- `requirements-dev.txt` 固定增加 `pytest-asyncio==1.3.0`，为现有 `pytest.mark.asyncio` 测试提供明确、可复现的插件依赖。

## 创建和修改文件

- 新增：`app/chunked_upload.py`、`tests/test_chunked_upload.py`、`docs/CHUNKED_UPLOAD.md`。
- 修改：`app/admin.py`、`app/admin_upload.js`、`app/config.py`、`app/db.py`、`app/__init__.py`、`.env.example`、相关测试及公开文档。
- 本轮 Backup/Restore 安全增量明确修改 `scripts/backup.sh`、`scripts/restore.sh`、`README.md`、`MIGRATION.md`、`REPORT.md`，并新增 `tests/test_backup_restore_scripts.py`；未覆盖或改写其他功能文件。

## 实际执行的检查

- `node tests/test_admin_upload.js`：通过。
- `node --check app/admin_upload.js` 与 `node --check tests/test_admin_upload.js`：通过。
- `/opt/venv/bin/python -m compileall -q app tests`：通过。
- `python -m py_compile ...`：通过。
- `bash -n scripts/backup.sh scripts/restore.sh scripts/build-image.sh` 与 `sh -n docker-entrypoint.sh`：通过。
- `git diff --check`：通过。
- `/opt/venv/bin/python -m unittest -v tests.test_backup_restore_scripts`：8 项隔离集成测试通过；使用临时项目与 `PATH` fake docker，未调用真实 Docker。覆盖 Backup/Restore 在线拒绝、Compose 状态不可确认时 Backup 默认拒绝及显式 override、缺少图库、缺少数据库、超过总展开量、危险空间倍率、重复成员、符号链接、路径逃逸、脚本生成备份再恢复、成功恢复后清空 `upload_tasks` 和分片临时目录，以及最终删除 chunked 失败时图库、旧数据库与旧任务文件一致回滚。
- `/opt/venv/bin/python -m py_compile tests/test_backup_restore_scripts.py`：通过。
- 最终使用 `set -Eeuo pipefail` 严格执行 10 阶段检查：`compileall`、目标 `py_compile`、Node 行为测试、Node 语法、Shell 语法、8 项脚本集成测试、脚本 `0755` 权限、版本/schema/NUL 源码守卫、公开内容与 UTF-8 扫描、`git diff --check`；全部通过，末尾输出 `STRICT_FINAL_CHECKS_OK`。
- `/opt/venv-a0/bin/python` 实际导入 importer 并调用成员名校验，真实 NUL 文件名被 `ArchiveSecurityError` 拒绝；输出 `REAL_NUL_REJECTED_OK`。`/opt/venv/bin/python` 缺少 Pillow，因此未将该运行时的导入失败误报为功能失败。
- 公开 UI/文档扫描未发现用户域名、真实归档名/数量、服务器绝对路径、固定事故大小或代理品牌；UTF-8 replacement character 扫描无命中。
- 使用 `/tmp` 隔离目录和标准库实际验证 schema v3、任务创建、顺序写入、非末片最小值拒绝、末片例外、offset 持久化、nonblocking 文件锁、活跃任务清理跳过、TTL 删除：通过。
- 在 VPS 临时隔离源码副本中使用候选镜像作为一次性测试运行时；未挂载生产数据、未连接生产 Compose。目标测试 `tests/test_chunked_upload.py tests/test_admin.py tests/test_api.py` 共 80 项，全部通过。
- 同一隔离环境执行全量 `pytest -q -p no:cacheprovider`：精确统计为 `134 passed`、`0 failed`、`0 errors`。首次试跑错误地保留了 `CACHE_DIR`，只导致配置派生测试失败；改为复制源码至容器临时可写目录，并清除 `DATA_DIR`、`IMAGES_DIR`、`DATABASE_PATH`、`LOG_DIR`、`CACHE_DIR`、`UPLOAD_TMP_DIR` 后全量通过，确认这是测试环境覆盖项而非产品逻辑缺陷。
- 使用最新源码重建候选镜像并执行真实隔离运行态验收：`/health` 返回 V3.0.0 和 `ok`，Uvicorn PID 1 UID 为 `1000`；约 3.09 MiB 的测试 ZIP 以 1 MiB 分片上传，首片提交后重建 API 容器，服务器仍返回正确 offset，随后从该位置续传至完整文件。归档 complete、preview 和 confirm 均通过，确认后临时任务目录立即删除且图片成功进入隔离图库。
- 同一运行态验收确认：取消部分上传后立即回收 `upload.bin`；将隔离任务置为过期后，后台周期清理在没有额外管理请求的情况下删除任务目录和 SQLite 记录；最终临时上传目录占用为 0 字节、`upload_tasks` 为 0 行。验收前后 VPS 原有容器稳定字段快照完全一致，未挂载、读取、停止或修改生产业务数据与容器。
- 新增回归覆盖：PATCH 正文读取前的同任务 `409` 与全局 `429` admission、拒绝请求 offset 不变、流式失败回滚、上传临时分区与图库分区分别检查、图库预算扣除其他 receiving 任务承诺、multipart spool 移动或受控复制及异常清理、无请求 TTL 周期回收、应用关闭停止后台任务、owner Cookie 临期滚动续签与任务身份保持、真实 NUL 拒绝与字面转义文件名不误拒。
- 本轮使用 `/opt/venv-a0/bin/python` 在临时目录真实构造合法 ZIP，将其写入无扩展名的 chunked `upload.bin`，依次验证显式 suffix dry-run、`summary_json`/`entries_json`/`import_required_bytes` 持久化、正式导入与任务删除；输出 `extensionless ZIP preview/schema/confirm passed`。
- 本轮重新执行 `node --check app/admin_upload.js` 与 `node tests/test_admin_upload.js`：通过，后者输出 `admin_upload.js validation tests passed`。
- 本轮第一次运行 8 项 Backup/Restore 集成测试时，成功恢复用例真实发现恢复包缺少空的 `mobile/`、`square/` 后脚本未补建目录；修复脚本后重跑 `python -m unittest -v tests.test_backup_restore_scripts`，结果 `Ran 8 tests ... OK`。编辑工具保存脚本后曾导致执行位丢失，已恢复并核验 `scripts/backup.sh`、`scripts/restore.sh`、`docker-entrypoint.sh` 均为 `0755`。
- 本轮 `compileall`、`bash -n scripts/backup.sh scripts/restore.sh docker-entrypoint.sh` 和 `git diff --check` 均实际通过。
- 远程测试容器必须清除镜像继承的生产目录环境变量后再运行 pytest，避免 `CACHE_DIR=/app/data/cache/webdav` 等生产默认值污染临时目录配置测试。推荐命令：`env -u DATA_DIR -u IMAGES_DIR -u DATABASE_PATH -u LOG_DIR -u CACHE_DIR -u UPLOAD_TMP_DIR python -m pytest -q`。这属于测试进程环境隔离，不是产品逻辑修复。

## 已解决问题

单请求归档不再是大归档网页上传的唯一选择；普通 multipart 上限与分片应用总上限分离；上传位置可持久恢复；服务端 offset 成为进度权威；分片 `413` 可按服务端能力自动降为 4/2/1 MiB；临时数据不需要 part 合并；确认导入成功后及时清理。页面刷新后不会自动取得本地文件，用户需在同一浏览器重新选择同一文件；管理 Session 过期后可重新登录续传，有效 owner Cookie 临近过期时会保持同一 owner 身份滚动续签。换浏览器、清除 owner Cookie、Cookie 已失效或轮换 `ADMIN_SESSION_SECRET` 后不能接管旧任务。

## 未解决问题与已知风险

- 全量自动化测试和真实隔离 HTTP 运行态验收均已通过，但尚未执行真实浏览器视觉回归与长时间真实网络中断恢复验收；当前不据 HTTP 验收声称视觉验收通过。
- in-flight admission 是单进程内门控；跨进程同任务仍由 nonblocking `flock` 快速拒绝，但全局并发计数不跨进程聚合。当前 Docker Compose 默认单实例部署符合该边界。
- SQLite + 本地 bind mount 面向单实例或共享本地文件系统，不支持多副本跨主机并行写同一任务。
- 应用无法自定义请求到达应用前被上游链路拒绝时的响应，但默认 8 MiB 分片和自动降档可降低单请求体大小。
- 标准备份不恢复活动上传；跨主机迁移或 Restore 前需完成或取消任务，并在 API 停止写入时执行备份。

## 后续建议

发布后可继续补充真实浏览器视觉回归和长时间真实断网恢复验收，并根据实际 CDN、网关和 VPS 空间调整分片及磁盘安全余量。当前隔离验收未接触生产数据；GitHub 与 Docker Hub 的实际发布状态以本报告后续发布记录为准。

---

# Random Image API V2.2 实施报告

## 1. 报告范围

本报告记录 Random Image API V2.2 文档、当前实现与发布状态。源码与 Docker Hub `qinlingmonkey/random-image-api:v2` 均已发布为 V2.2.4；远程隔离构建、完整测试、运行态验收和发布后 Registry 回读均已完成。V2.2 保留 V1/V2 API、WebDAV Hybrid、缓存、归档 importer、Backup / Restore、tags 和主题接口，重点优化管理 UI、图片预览、物理目录整理与标签编辑。

### V2.2.4 用户文案与内部说明分层（已发布）

#### 问题与设计结论

V2.2.3 为解释超大归档上传问题，在管理页面直接展示了服务端临时目录、进程内状态、TTL、上游临时存储及固定大小故障样例等实现说明。这些信息适合维护文档和实施报告，不适合作为普通管理员执行上传操作时的页面文案。V2.2.4 将两类信息分开：管理页面仅保留当前操作所需的支持格式、动态上限、标签作用范围、上传/校验状态和可执行错误恢复建议；内部存储生命周期、代理边界和故障分析继续记录在技术文档中。

#### 实现方案

- 重写普通图片与归档上传区说明，删除服务端路径、进程状态、代理品牌、固定文件大小和故障案例等内部细节。
- 上传错误统一为简短、可行动的中文提示，不向用户暴露 multipart、临时目录或上游状态实现。
- 归档 preview 不再输出 Python 字典或内部字段，改为“归档内容、检查图片、可导入、重复、跳过、横屏、竖屏、方形”的结构化中文摘要。
- 明确上传页所选标签作用于本次全部图片，并在确认页保留且允许调整；空标签时提供创建标签或稍后处理的正常路径。
- 保留 V2.2.3 的流式 multipart、磁盘 spool、请求与归档限额、CSRF、会话绑定、安全解压、哈希校验和失败清理等服务端安全边界，仅调整对用户的呈现。

#### 创建和修改的文件

- 功能与版本：`app/__init__.py`、`app/admin.py`、`app/admin_upload.js`。
- 回归测试：`tests/test_admin.py`、`tests/test_admin_upload.js`、`tests/test_api.py`。
- 发布文档：`README.md`、`MIGRATION.md`、`REPORT.md`、`DOCKERHUB_OVERVIEW.md`；新增独立的 `docs/CLI_IMPORT.md`，并从 README 的 importer 章节提供直达链接。CLI 手册将首次使用中容易遇到的路径引用、Compose 挂载、预检、多标签、大归档限制、磁盘空间、重扫与验证问题整理为通用说明，不记录任何单次导入的文件名、图片数量、域名、用户名或服务器绝对路径。

#### 实际测试与发布结果

- `node tests/test_admin_upload.js`：通过；`node --check app/admin_upload.js`：通过。
- Python `compileall`、Shell 语法、Markdown/Secret 检查和 `git diff --check`：通过。
- 测试 VPS 隔离构建成功，完整 Python 测试为 `93 passed`。
- 真实隔离容器为 `healthy`，`/health` 返回版本 `2.2.4`，Uvicorn PID 1 UID 为 `1000`。
- 管理总览与归档 preview 的 HTTP/HTML 合同验收通过：必要操作文案和结构化摘要存在，内部路径、内部字段、原始字典、代理品牌与固定故障样例不出现在页面中。
- 原有 10 个 VPS 容器前后稳定字段一致；测试容器、候选镜像、临时源码、临时登录配置和发布 Token 副本均已清理。
- 功能提交 `8fdc465972f6cb9f53cd4eb58b9d22ddb35c7ade` 已同步 GitHub `main`。
- Docker Hub `qinlingmonkey/random-image-api:v2` 已发布为 `linux/amd64`；Registry 回读确认 Manifest 摘要为 `sha256:d00a6f75f028a913cb33062f80d9e3de68da6f82b4e88fb402b7cf64194257da`，Config 摘要为 `sha256:483a5c6f60e276eaf1343f444158700ef89cfe2f1dba50267b27c82cefef64e9`，共 12 层。

#### 已知风险与后续建议

- 本轮没有改变应用、反向代理或 CDN 的实际上传上限；大文件仍受整条上传链路中最小限制约束。
- 本轮执行了真实 HTTP/HTML 验收，但未通过公网域名重新上传多 GiB 归档，也未执行浏览器视觉回归。
- 若后续需要支持多 GiB 网页上传，应单独设计可续传/分片协议和持久任务状态，而不是继续扩大单次 multipart 请求。

### V2.2.3 聚焦修复（已发布）

#### 网络调研与限制结论

本轮检索并核对了上游网关的 HTTP `413` 与请求体限制资料。结论是网页有效上限取应用限制与链路中所有上游请求体限制的最小值；上游可在请求到达应用前拒绝。文档因此采用通用代理说明，不绑定具体厂商或套餐。

#### 实现方案

- 普通图片选择与拖拽仅接受扩展名和 MIME 对应的 JPG/JPEG、PNG、WebP，并按每文件 `ADMIN_MAX_UPLOAD_BYTES` 上传前拒绝；非法拖拽不会写入 input，也不会提交，错误区使用中文 `role=alert`。
- 归档选择与拖拽仅接受 ZIP、TAR.GZ、TGZ，按服务端注入的 `ADMIN_MAX_ARCHIVE_BYTES` 预检并展示文件名、大小和网页上限；超限提示改用 CLI。
- 归档表单保留 method/action/enctype 的无 JS fallback；有 JS 时使用 `XMLHttpRequest` + `FormData`，展示真实 upload progress、已传 MiB和上传完成后的服务端安全校验状态。认证失效转登录；413、常见上游网关错误、网络中断、超时和中断均显示中文错误并恢复按钮。
- 上传前可选择多个已有启用标签，空表示不加标签；preview 将字段语义和选择保存到 `_Preview`，确认页默认保留并允许修改。preview 与 confirm 两阶段均从 SQLite 验证标签存在且启用，拒绝未知/禁用标签并清理临时归档。
- 待确认归档保存于服务端 `UPLOAD_TMP_DIR`，索引只存在进程内存；TTL、登出或重启会清理或使其失效。浏览器或上游网关可能另用本机或边缘临时存储。
- CLI 文档明确其已支持 ZIP/TAR.GZ/TGZ、多 `--tag` 和 `--database-path`，但不直接接受目录；文件夹应先打包，或复制到 `images/{desktop,mobile,square}` 后 rescan。目录标签映射来自归档成员父目录，预览页可复核；上传前标签应用于本次所有图片。

#### 创建和修改的文件

- 修改：`app/__init__.py`、`app/admin.py`、`tests/test_api.py`、`tests/test_admin.py`、`README.md`、`DOCKERHUB_OVERVIEW.md`、`REPORT.md`。
- 新增：`app/admin_upload.js`、`tests/test_admin_upload.js`。
- 未修改 `.env`、业务图片、SQLite、日志、缓存或备份包；远程操作仅使用隔离目录、候选镜像和临时容器，验收及发布后均已清理。

#### 测试、结果与风险

- Node 纯函数测试已实际执行：`node tests/test_admin_upload.js`，通过；覆盖图片扩展/MIME 对应、逐文件超限、归档扩展与归档超限。
- Python `compileall`、Node `--check`、Shell 语法和 `git diff --check` 均通过。
- 测试 VPS 上成功构建 V2.2.3 生产候选镜像；使用不写回候选镜像的一次性测试容器安装开发依赖后，完整 Python 测试为 `93 passed`。
- 运行态验收确认容器 `healthy`、版本 `2.2.3`、OOM 为 false、重启次数为 0，Uvicorn PID 1 的 UID 为 `1000`；未登录管理页面 `303` 回退登录页，空标签引导、创建测试标签后的普通图片/归档多标签控件及内联上传进度逻辑通过真实 HTTP/HTML 合同检查。
- 原有 10 个容器的 ID、镜像和名称等稳定字段验收前后一致；本轮测试容器、候选镜像和远程隔离目录均已清理，长期 VPS SSH 密钥按约定保留。
- V2.2.3 源码提交 `9e2282783b331e2c80b3aa83f8c1fecaf57b520f` 已同步 GitHub `main`；Docker Hub `v2` 已发布为 `linux/amd64`，发布后 Registry 回读确认 Manifest 摘要为 `sha256:4b752bb391020f90fbf15c5e902d2f767b7b62d702e5731ab21ee80a13ccf82a`、Config 摘要为 `sha256:bb31ae8011517dfffd685fc8063717793e630211da46844224dad52738128fff`，共 12 层。镜像版本由 `app/__init__.py` 提供，隔离容器 `/health` 已实际核验为 `2.2.3`。
- 已知风险：前端校验只改善体验，不能成为信任边界；服务端 CSRF、实际图片解码、请求/图片/归档限额和两阶段标签验证继续承担安全约束。上游网关在应用前拒绝的请求无法由应用返回自定义页面，只能由前端根据状态码给出提示。本轮未实际通过公网代理上传多 GiB 归档，也未执行浏览器视觉验收。

V2.2.2 修复命令行归档导入无法打标签、网页压缩包上传内存与临时文件生命周期、多标签归档关联，以及管理会话失效后 HTML 页面不返回登录页的问题。最终候选镜像完整测试为 `88 passed`，远程健康检查、未登录 HTML `GET` 的 `303` 登录回退和 Uvicorn PID 1 UID `1000` 均通过；发布前后原有 10 个容器快照一致，隔离容器、候选镜像、临时认证和目录均已清理。Docker Hub `qinlingmonkey/random-image-api:v2` 已发布为 V2.2.2、平台 `linux/amd64`；独立回读确认 Manifest/Registry 摘要为 `sha256:7ca9a5958242417a0b1f9b5b5609a437f8158db55ab5323e989adcd098d0ae2f`，Config 摘要为 `sha256:0c1a92c610d5af76bb115f8ceab8d4b70f10be778ee5cef6b0843b7b83267ef3`，共 12 层，Entrypoint 为 `/usr/local/bin/docker-entrypoint.sh`。源码功能提交 `d8752a0321404d8ad7ec2cfa3e2c8d06bf9bbd2b` 已同步 GitHub `main`；长期复用 SSH 密钥按约定保留。

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

- 图片分页、方向/来源/缓存/启用状态/标签筛选；标签筛选提供“全部标签”“无标签”和用户创建标签；
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

### 4.6 V2.2 管理体验与目录整理

V2.2 新增或完善：

- 响应式统计卡片、筛选表单和图片瀑布流；
- 仅管理员签名会话可访问的本地图片与 WebDAV 缓存预览；
- WebDAV 未缓存对象使用占位卡片，打开管理页不会批量下载远端原图；
- 本地图片在 `desktop`、`mobile`、`square` 之间安全移动，数据库 ID、标签和启停状态保持；
- 上传与 importer 将正方形图片保存到独立 `data/images/square/`，`SQUARE_POLICY` 只控制随机池；
- 删除改为按钮二次确认，服务端仍校验 CSRF 与明确确认字段；
- 单张及批量本地图片标签添加/移除，以及 WebDAV 对象标签添加/移除；
- 来源、真实方向、存放目录、启用、缓存、标签和文件名/HREF 筛选；“无标签”表示不存在任何标签关系，关联停用标签的图片不算无标签，并可与其他筛选、排序和分页组合。

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
| `app/__init__.py` | 将补丁版本更新为 `2.2.1` |
| `app/admin.py` | 增加本地与 WebDAV 管理图片页的“无标签”筛选及状态说明 |
| `tests/test_api.py`、`tests/test_admin.py` | 增加版本与无标签筛选回归覆盖 |

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

### 6.4 V2.2 本地与浏览器验证

主线程已实际执行：

- 管理端与 importer 专项测试：`24 passed`；
- 最终完整测试：`pytest -q`，`81 passed`，仅有 Starlette TestClient 的 `httpx2` 迁移弃用警告；
- Python `compileall`、Shell 语法、`git diff --check`、Markdown 围栏、可提交文件 Secret 模式和运行数据跟踪检查均通过；
- 新增回归覆盖管理员预览鉴权、图片瀑布流标记、预览 Content-Type 与缓存头、符号链接拒绝、Square 上传与 Restore 目录、移动时同名冲突、真实方向与存放目录分离、按钮删除确认、单图标签添加/移除、Catalog 立即刷新、WebDAV 标签添加/移除、已缓存预览和未缓存占位，以及 Docker ENTRYPOINT 可执行权限。

本轮未执行浏览器视觉检查：当前浏览器无法访问远程隔离端口，因此未伪造截图或视觉结论。本轮改用远端 curl/HTML 合同检查，确认登录、CSRF、详情抽屉、Lightbox、批量选择、搜索排序、拖拽上传区、Square 目录、快捷菜单和标签工作台相关标记存在。

### 6.4.1 V2.2.1 无标签筛选发布收尾

- 管理图片页新增稳定值 `__untagged__`，本地图片和 WebDAV 对象均通过参数化查询中的固定 `NOT EXISTS` 条件判断不存在任何标签关系；
- 关联停用标签的图片不算无标签；无标签可与来源、方向、存放目录、启用/缓存状态、文件名/HREF 搜索、排序和分页组合；
- 筛选项、刷新/分页选中状态、当前条件摘要、专属空状态及 SQL 注入式输入均有回归测试；
- 本地与远程一次性测试容器完整 `pytest -q` 均为 `83 passed`。生产镜像按精简设计只安装 `requirements.txt` 中的运行依赖，不内置 `pytest`；远程测试由一次性测试容器另行提供 `requirements-dev.txt` 中的开发测试依赖；
- 生产镜像显式设置 `CACHE_DIR=/app/data/cache/webdav`。运行完整测试时对 pytest 进程使用 `env -u CACHE_DIR`，是为了避免生产默认值覆盖配置测试的未配置场景，使其能够验证缓存目录随临时 `DATA_DIR` 派生；该操作只隔离测试进程环境，不改变正式发布镜像的运行配置；
- 正式发布镜像在发布前构建成功，应用版本为 `2.2.1`；隔离运行中 `/health` 正常，主进程 UID 为 `1000`，未登录访问返回 `401`，登录和 CSRF 流程通过；
- 真实 PNG 上传后可由“无标签”筛选命中；创建并关联标签后，该图片从无标签结果移除，SQLite 标签关系与页面结果一致；
- 按正式 `data` 布局执行的 Backup 内容检查与配置脱敏通过；原有容器快照前后一致，隔离容器、候选镜像和临时目录均已清理；
- 本轮仅执行真实 HTTP/HTML 管理流程验收，未执行浏览器视觉验收，不据此声称视觉验收通过；
- 远程测试复用既有长期 SSH 密钥并按约定保留，未因本轮清理撤销或删除；
- 发布后通过独立回读确认 `qinlingmonkey/random-image-api:v2` 为版本 `2.2.1`、平台 `linux/amd64`，Manifest/Registry 摘要为 `sha256:bd1dc2e6fa5b0882cb9fb3d6bf8816d2f175e7bd05624b3140544d9efefbbda8`，Config 摘要为 `sha256:1088deae369cf6e5a3e370dc4beaf7399b672cb6988ec5686cd9d3b316956633`，共 12 层，Entrypoint 为 `/usr/local/bin/docker-entrypoint.sh`；源码功能提交 `c32b1d1426b70c4b10b7435a8aa7a55d0fd6907b` 已同步 GitHub `main`；
- 最终文档收尾未修改代码或测试；发布文档已提交并推送，Docker Hub 线上 Overview 已同步并完成正文长度与 SHA-256 一致性校验。

### 6.4.2 V2.2.2 上传、标签与会话修复

- 命令行 importer 新增可重复使用的 `--tag`，并要求同时传入 `--database-path`；多个标签在一个 SQLite 事务中写入，重复 slug 会去重，`--dry-run` 不创建数据库或图片文件；
- 标签实体与关系仅保存在 SQLite 的 `tags`、`image_tags` 和 `webdav_object_tags` 表中，不写入图片；本地图片按内容哈希关联，WebDAV 对象按标准化 HREF 关联；
- 网页归档确认支持默认标签、多个已存在标签和可选目录标签，并在同一 SQLite 事务中创建关系；
- 管理端改用固定版本 `python-multipart==0.0.22` 与 `starlette==1.6.0` 的流式 multipart 解析。上传文件通过 `SpooledTemporaryFile` 进入磁盘 spool，归档以 1 MiB 分块写入并计算 SHA-256，请求结束时确定性关闭表单中的上传文件；
- 请求总量、单图、归档大小、成员数量、单成员大小、解压总量、压缩比和图片像素限制继续生效；超限、损坏或空归档会返回安全错误并删除临时文件；
- HTML `GET` 在 Cookie 无效、签名错误、过期或服务端会话不存在时返回 `303` 到 `/manage-images/login`；`POST` 等写请求保持 `401`，避免重定向重放写操作；
- 独立审查发现并修复两项高优先级异常路径：multipart 临时文件未确定性关闭，以及归档在第二个 `os.replace()` 失败时可能留下第一个已提交文件。新增故障注入测试确认只撤销本次新建文件；
- 最终 V2.2.2 工作树重新构建后，远程候选镜像完整测试为 `88 passed`；`/health` 返回版本 `2.2.2`，管理登录回退和 PID 1 UID `1000` 通过；原有 10 个容器前后快照一致，隔离容器、镜像和目录已清理；
- 本轮没有执行浏览器视觉验收，不据 HTTP/HTML 合同检查声称视觉验收通过；
- Docker Hub `v2` 已发布为 V2.2.2；独立回读确认 Manifest/Registry 摘要为 `sha256:7ca9a5958242417a0b1f9b5b5609a437f8158db55ab5323e989adcd098d0ae2f`，Config 摘要为 `sha256:0c1a92c610d5af76bb115f8ceab8d4b70f10be778ee5cef6b0843b7b83267ef3`，平台为 `linux/amd64`，共 12 层，Entrypoint 为 `/usr/local/bin/docker-entrypoint.sh`；镜像内版本核验为 `2.2.2`，发布前后原有 10 个容器快照一致。Docker Hub 线上 Overview 将在本轮文档提交后同步并单独校验。

本轮创建或修改的文件包括 `app/__init__.py`、`app/admin.py`、`app/importer.py`、`requirements.txt`、`tests/test_api.py`、`tests/test_admin.py`、`tests/test_importer.py`、`README.md`、`MIGRATION.md`、`REPORT.md` 和 `DOCKERHUB_OVERVIEW.md`。未修改 `.env`、业务图片、数据库、日志、缓存、备份包或现有远程服务。

### 6.5 V2.2 远程隔离 Docker 验收

主线程在测试 VPS 的全新 `/tmp` 目录、独立 Compose 项目、独立镜像标签和未占用高位端口中完成验收。测试前记录服务器原有容器快照；清理后原有 10 个容器的名称、镜像和状态与测试前一致，未停止、替换或修改任何既有业务容器、目录或数据。

- 最终候选源码归档经清单与路径审计，共 42 个文件，不含 `.env`、Git 元数据、业务图片、数据库、日志、缓存、备份包或镜像归档；
- Compose 配置与 Docker 构建通过；V2.2.0 正式候选镜像 ID 为 `sha256:9cad79d94a1cd902a4ede2a08d19461079be821c95ad375f8a499de2595a09e8`。通过一次性 root 验证容器复制源码并安装开发依赖后，正式镜像上的完整测试通过。
- 本地完整测试为 `81 passed`；正式 V2.2.0 候选镜像内完整测试为 `81 passed`，并通过 compileall 与 Shell 语法检查；测试容器为一次性容器，未修改正式镜像内容。
- Compose 容器达到 `healthy`，`/health` 返回版本 `2.2.0`，Uvicorn PID 1 以 UID `1000` 运行，SQLite `PRAGMA integrity_check=ok`；
- 真实 HTTP 管理流程通过：未登录预览拒绝、登录与 CSRF、Square 上传到 `square/`、瀑布流卡片、受保护 PNG 预览、单图标签添加/移除、`desktop↔square` 移动且真实方向保持 Square、主题随机接口、缺少确认的删除拒绝以及 `confirm=1` 友好删除；
- Backup/Restore 通过：备份包含 Square 原图和 SQLite 标签关系，不包含 WebDAV 缓存或测试 Secret；停止服务后恢复成功，恢复前后图片 ID、Square 路径、真实方向和主题关系一致，恢复后的主题接口返回可解码 PNG；
- 本轮隔离容器、Compose 网络、源码目录、临时归档和临时凭据均已清理；远端原有容器快照前后 `cmp` 完全一致。

远程验收实际发现并修复两个发布阻断问题：

1. Git 索引记录 `docker-entrypoint.sh` 为可执行，但工作区归档曾保存为 `0600`，导致生产容器启动时报 `permission denied`。现由 Dockerfile 使用 `COPY --chmod=0755` 显式保证镜像内权限，并增加回归测试。
2. Restore 脚本重建图片目录时遗漏空的 `square/`。现显式创建 `desktop/`、`mobile/`、`square/`，并增加脚本契约测试。

### 6.6 V2.0 已发布版本的历史验收

以下记录属于已发布 V2.0.0，作为 V2.2 验收前的历史基线：

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

## 7. V2.2.0 历史发布与实际结果

- V2.2.0 本地最终 `pytest -q`：`81 passed`，退出码 `0`；`compileall`、Shell 语法和 `git diff --check` 均通过。
- 远程隔离 Compose 构建、healthy、SQLite、UID 1000、HTTP、登录/CSRF、上传、UI HTML 合同、Backup/Restore 均通过。
- 本轮远程无法使用浏览器访问隔离端口，因此未执行远程视觉验收；使用真实 HTTP 管理流程和 HTML 合同检查替代，未伪造截图结论。
- GitHub `main` 已同步 V2.2 功能与验收提交 `384a155290c69dbd65dda08d2d374dccc2f2a7e7`。
- 使用该提交的 Git 归档在测试 VPS 独立目录构建最终 `linux/amd64` 镜像，应用版本标签为 `2.2.0`，源码修订标签为 `384a155290c69dbd65dda08d2d374dccc2f2a7e7`。
- `qinlingmonkey/random-image-api:v2` 已推送成功；Docker Registry API 独立回读确认 manifest 摘要为 `sha256:268860bd1cb046f9a7f9f34dfc6ec6396e5748993f67d91cd202032f2189e490`、config 摘要为 `sha256:faa555405f7b82bd824ee741791fbd8adfa3823b47b5289bddc825483e2cdd32`、平台为 `linux/amd64`、共 12 层。
- 发布使用独立远程目录、专用镜像标签和临时 Docker 配置；清理后不保留 Docker Hub 登录配置、构建目录或候选镜像，服务器原有业务容器未被停止、重启或修改。

## 8. 已解决问题

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

## 9. 未解决问题与已知风险

- 当前 `v2` 仅发布 `linux/amd64`；ARM64 主机需要自行从源码构建，或等待后续多架构镜像。
- 管理会话与待确认 preview 存于单进程内存；容器重启会退出登录并使 preview 失效。这符合当前单实例设计，但不适用于多副本共享会话。
- 管理 UI 应部署在 HTTPS 反向代理后；仅改变 `ADMIN_PATH` 不是访问控制。
- `TRUSTED_PROXY_HEADERS=true` 只适合可信代理边界，直连公网时可能造成客户端 IP 日志被伪造。
- SQLite 适合当前单机部署；不应并发挂载给多个写实例。
- WebDAV 第一层目录必须能转换为合法 slug；更深层目录不形成层级主题。
- 文件系统与 SQLite 无法形成真正的跨资源原子事务；实现通过预校验、原子落位和失败补偿降低风险，极端掉电窗口仍可能需要下一次 scan 修复状态。

## 10. 后续建议

1. 保留 V1 镜像与 `docs/V1.md`，为回滚和旧部署维护提供基线。
2. 后续评估构建 `linux/arm64` 多架构镜像；发布前不要把当前 `v2` 描述为多架构。
3. 生产部署应使用 HTTPS 反向代理、随机独立 Secret、WebDAV 只读应用账号，并定期把 Backup 复制到机器外。
4. 关注 Starlette TestClient 的 `httpx2` 迁移弃用警告，在上游兼容窗口内更新测试依赖。
