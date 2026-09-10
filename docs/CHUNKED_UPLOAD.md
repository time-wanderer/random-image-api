# V3.0.0 网页归档分片上传（未发布源码）

本功能仅存在于当前未发布工作区。计划发布镜像标签为 `v3`；已有 `v1`、`v2` 镜像继续保留，不覆盖、不删除。

管理网页同时保留两条归档上传路径：

- 小归档可继续通过普通 `multipart/form-data` 上传，默认单请求上限为 512 MiB；禁用 JavaScript 时也使用这条兼容路径。
- 大于服务端建议分片大小的归档由浏览器使用 `File.slice()` 顺序分片上传。默认建议每片 8 MiB，若链路返回 `413`，浏览器会在服务端允许范围内自动尝试 4、2、1 MiB。

分片上传不会绕过应用总上限。默认应用总上限为 8 GiB，归档完成后仍会执行与普通上传相同的安全校验、dry-run 预览和人工确认导入。

## 使用方式

1. 登录管理页面并选择一个 ZIP、TAR.GZ 或 TGZ 归档。
2. 页面按服务端能力选择分片大小并显示总体进度。进度来自服务端已经确认的 offset，不使用浏览器仅发送到网络链路的字节数。
3. 网络中断、页面刷新或容器重启后，重新选择同一文件即可从服务端确认的位置继续。
4. 上传完成后进入预览页，检查摘要、标签与目录标签映射，再确认导入。
5. 导入完成后，临时归档会立即删除。

任务默认保留 24 小时。应用启动、管理请求及单实例后台周期任务都会清理过期任务；即使没有新的 HTTP 请求，过期任务也会被回收。缺失文件对应的数据库记录和受控临时目录中的孤儿任务也会清理，取消任务则立即删除临时文件。后台任务随应用启动并在应用关闭时停止，不依赖 Celery、Redis 或其他常驻服务。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `ADMIN_MULTIPART_ARCHIVE_MAX_BYTES` | 536870912 | 普通单请求归档上限 |
| `ADMIN_MAX_ARCHIVE_BYTES` | 8589934592 | importer 安全校验的归档总上限 |
| `ADMIN_CHUNKED_UPLOAD_ENABLED` | true | 是否启用网页分片上传 |
| `ADMIN_CHUNK_RECOMMENDED_BYTES` | 8388608 | 服务端建议分片大小 |
| `ADMIN_CHUNK_MIN_BYTES` | 1048576 | 可接受的最小协商分片 |
| `ADMIN_CHUNK_MAX_BYTES` | 16777216 | 服务端接受的单片最大值 |
| `ADMIN_CHUNKED_MAX_UPLOAD_BYTES` | 8589934592 | 分片上传应用总上限 |
| `ADMIN_CHUNKED_UPLOAD_TTL_SECONDS` | 86400 | 活动任务有效期 |
| `ADMIN_CHUNKED_CLEANUP_INTERVAL_SECONDS` | 30 | 单实例后台 TTL 清理周期（秒） |
| `ADMIN_UPLOAD_OWNER_TTL_SECONDS` | 2592000 | 签名上传所有者 Cookie 有效期，不能短于任务 TTL；临近过期时滚动续签 |
| `ADMIN_CHUNKED_MAX_ACTIVE_TASKS` | 2 | 全局最多活动任务数 |
| `ADMIN_CHUNKED_MAX_INFLIGHT_PATCHES` | 2 | 单进程同时读取的 PATCH 数；同任务并发另行快速拒绝 |
| `ADMIN_CHUNKED_MIN_FREE_BYTES` | 268435456 | 上传临时分区和图库分区各自容量检查必须保留的空闲空间 |

默认最小剩余空间选择 256 MiB，以便小型 VPS 可以使用，同时避免上传直接耗尽文件系统。上传任务创建、分片写入和普通 multipart staging 使用 `UPLOAD_TMP_DIR` 所在文件系统的可用空间；确认导入使用 `IMAGES_DIR` 所在文件系统。两者位于不同挂载点时不会混用 `disk_usage` 结果。图库容量预算还会扣除其他 `receiving` 任务尚未提交的承诺上传字节，防止一次确认导入占用已接受任务的预留。数据库、日志或其他服务与上述目录共用分区时，建议提高保留值。修改 min/recommended/max 时必须满足 `min <= recommended <= max`。

普通 multipart 由 Starlette 先写入服务端 spool。应用仅接管该服务端对象，不把客户端文件名当作路径：若 spool 有可信普通文件路径且与受控临时目录同一文件系统，则原子移动；否则先按“spool 与受控副本短时并存”的最坏峰值检查临时分区，再逐块复制。复制、空间不足或格式校验异常都会清理受控目标。

## 持久化与恢复边界

任务状态保存在 SQLite `upload_tasks` 表，临时目录中每个任务只保存一个顺序增长的 `upload.bin`，不会保存全部 part 后再合并。`PATCH` 在读取请求体前取得同任务和全局 in-flight admission：同任务并发快速返回稳定 `409 upload_in_progress`，全局超额返回 `429 too_many_inflight_chunks`，拒绝请求不会推进 offset。获准请求直接把 ASGI 数据块顺序写入文件，不再用 `list + join` 保留完整分片及其副本。每片写入完成后执行 `flush` 和 `fsync`，再事务更新 offset；失败会把本片截断到写入前位置。

`data/` 的 Compose bind mount 使任务可以跨容器重启恢复。任务所有权不使用全局固定的 `ADMIN_TOKEN`：首次成功登录会获得独立、随机、签名且 HttpOnly 的上传所有者 Cookie，数据库只保存其不可逆派生值。管理 Session 过期或服务重启后，同一浏览器保留该 Cookie 并重新登录，即可在任务 TTL 内重新选择同一文件续传；有效 Cookie 临近过期时会沿用同一 owner ID 滚动续签，并保持 HttpOnly、SameSite=Strict、管理路径与 Secure 判定语义。换浏览器、清除 Cookie、Cookie 已过期或轮换 `ADMIN_SESSION_SECRET` 后，旧任务不可再由页面恢复，只能等待 TTL 清理；这避免所有使用同一管理员 Token 的会话共享任务。

标准备份必须先执行 `docker compose stop api`，以保证图库与 SQLite 来自同一停止写入时点；脚本检测到 Compose API 仍运行时会拒绝继续。备份会创建 SQLite 一致性副本，因此副本中可能存在 `upload_tasks` 表和当时的任务行；但它故意不复制活动 `upload.bin`。Restore 脚本会显式清空恢复数据库中的 `upload_tasks`，并删除默认分片临时目录，使这些不完整任务一致失效，而不是伪装成可恢复任务。因此跨主机迁移或执行标准 Restore 前，应先完成或取消活动上传。仅在原机保留同一 `data/` bind mount 的容器重建/重启场景，任务才能连同临时文件恢复。

## HTTP 协议摘要

所有接口都要求有效管理 Session；所有写请求还要求 `X-CSRF-Token`。任务 ID 不可枚举，并绑定到签名上传所有者 Cookie 派生的身份，而不是绑定到所有管理员共用的 Token。

- `GET /manage-images/archives/uploads/capabilities`：读取服务端分片能力和上限。
- `POST /manage-images/archives/uploads`：初始化任务并返回能力参数。
- `HEAD /manage-images/archives/uploads/{id}`：读取服务端确认 offset、总长度和状态。
- `PATCH /manage-images/archives/uploads/{id}`：发送 `application/offset+octet-stream` 顺序分片。
- `POST /manage-images/archives/uploads/{id}/complete`：校验总长度、SHA-256 并执行 dry-run。
- `GET /manage-images/archives/uploads/{id}/preview`：查看导入预览。
- `DELETE /manage-images/archives/uploads/{id}`：取消任务。
- `POST /manage-images/archives/confirm`：沿用现有确认导入流程。

错误响应包含稳定的 `error.code` 与可操作中文 `error.message`。offset 冲突返回 `409`，并在 `Upload-Offset` 响应头提供权威位置。`PATCH` 必须同时携带合法十进制 `Content-Length`、`Upload-Offset`、正确 Content-Type 和 CSRF；不接受 chunked transfer 绕过声明大小。完成阶段读取全文件、核对总长度、计算 SHA-256 并执行 importer dry-run。校验失败会保留已上传任务供重复检查或取消；确认导入失败也保留持久任务，只有确认成功后才删除临时文件。
