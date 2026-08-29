# 项目实施报告

## 1. 项目目标

在项目根目录交付一个可长期运行的随机图片 API：

- `GET /random` 按 User-Agent 或 `type` 返回横屏 / 竖屏图片
- `GET /health` 报告服务、图片数量和数据库状态
- 图片与元数据持久化
- Docker Compose 正式部署
- Backup / Restore / VPS 迁移文档齐备
- 经过实际测试，而不是只生成示例代码

## 2. 网络调研结果

编码前实际访问了官方文档和公开资料（`curl` HTTP 200），而不是只依赖既有记忆。

### 调研了什么

1. 随机图片 API 的常见实现
2. FastAPI 返回图片文件的方法
3. Python Web 服务处理静态图片的方法
4. User-Agent 判断 Desktop / Mobile
5. Windows / macOS / Android / iPhone UA 形态
6. 图片横竖屏判断与 Pillow 读取宽高
7. 随机选择策略与大规模文件扫描性能
8. Docker Compose 部署 FastAPI、healthcheck、数据持久化
9. SQLite 是否适合、何时应上 PostgreSQL
10. SQLite / PostgreSQL Backup Restore
11. VPS 迁移方式
12. API 异常处理

### 得到的结论

| 主题 | 结论 |
| --- | --- |
| 随机图片 API | 常见做法是自建静态图库随机选文件，或代理 Picsum / Unsplash。本项目要可控、可迁移、可备份，应自建本地图库。 |
| FastAPI 返回图片 | 官方推荐 `FileResponse(path)`，可设 `media_type`，并自动补 `Content-Length` / `ETag`。不要把大图读进内存再 `Response(content=bytes)`。 |
| 静态图片 | 本服务不是目录浏览，而是按条件选一张图后以文件流返回。 |
| User-Agent | MDN 明确 UA sniffing 不可靠，但本需求只做粗分类。Android / iPhone 仍带 `Mobile` / `iPhone` / `Android`；无 UA 或 curl 应回退 Desktop。 |
| 横竖屏 | Pillow `Image.size` 得到 `(width, height)`。`width > height` 横屏，`height > width` 竖屏，相等单独处理。 |
| 随机策略 | 元数据进 SQLite + 内存 ID 列表，`random.choice`。不要每次请求 `os.walk`。损坏 / 缺失文件从缓存剔除后重试。 |
| Docker Compose | 单服务即可。`restart: unless-stopped`、healthcheck、`.env`、bind mount `./data`。无数据库从服务时不需要 `depends_on`。 |
| 持久化 | Docker 文档：需要同时从宿主机访问文件时用 bind mount。图片和 SQLite 必须挂出容器。 |
| SQLite | 官方 *Appropriate Uses For SQLite*：低到中流量网站、本地应用文件格式、无需专职 DBA 的场景适合。本项目单实例、读多写少。 |
| PostgreSQL | 适合多实例、高并发写、多服务共享。当前没有这些需求，引入只会增加 Compose、备份和迁移成本。 |
| SQLite 备份 | 不要热拷贝正在写入的 `*.db`。应使用 Online Backup API 或 `VACUUM INTO`。本项目采用 `VACUUM INTO`，得到紧凑一致快照，恢复时丢弃旧 WAL。 |
| PostgreSQL 备份 | 若未来使用，应 `docker compose exec` + `pg_dump` / `pg_restore`，不在宿主机装客户端。 |
| 异常处理 | FastAPI `HTTPException`：非法参数 400，无图 404，数据库不可用 503。损坏文件跳过，不让服务崩溃。 |
| 性能 | 启动扫描 + 周期扫描 + 内存缓存。图片量到十万级仍主要是磁盘随机读，不必上 Redis。 |

### 最终采用的方案

**FastAPI + Uvicorn + Pillow + SQLite + 本地 `data/images` + Docker Compose 单服务。**

图片分类采用方案 C 的轻量版：目录可按 desktop/mobile 整理，但分类以宽高为准，元数据写入 SQLite，运行时用内存列表随机选取。

### 为什么不采用其他方案

- 不代理 Picsum / Unsplash：依赖外网、不可备份、不可离线迁移。
- 不上 Flask：任务优先评估 FastAPI，调研后没有更换理由。
- 不上 PostgreSQL / Redis / Celery / Kafka / K8s：没有多实例、任务队列或共享状态需求。
- 不把图片只按目录名分类：用户可能放错目录；宽高更可靠。
- 不用每次请求全盘扫描：图片增多后延迟会线性变差。

## 3. 主要参考来源

实际访问并提取过正文的来源：

1. FastAPI Custom Response / FileResponse
   https://fastapi.tiangolo.com/advanced/custom-response/
2. FastAPI Handling Errors / HTTPException
   https://fastapi.tiangolo.com/tutorial/handling-errors/
3. Pillow `Image.size` / `Image.open`
   https://pillow.readthedocs.io/en/stable/reference/Image.html
4. SQLite Online Backup API
   https://www.sqlite.org/backup.html
5. SQLite `VACUUM INTO`
   https://www.sqlite.org/lang_vacuum.html
6. Appropriate Uses For SQLite
   https://www.sqlite.org/whentouse.html
7. SQLite WAL
   https://www.sqlite.org/wal.html
8. MDN User-Agent / Browser detection
   https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/User-Agent
   https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Browser_detection_using_the_user_agent
9. Docker volumes / bind mounts
   https://docs.docker.com/engine/storage/volumes/
10. Docker Compose startup order / healthcheck
    https://docs.docker.com/compose/how-tos/startup-order/
11. Docker Engine install (Debian)
    https://docs.docker.com/engine/install/debian/
12. Lorem Picsum（对照“随机图 API”常见外部实现）
    https://picsum.photos/

内置 `search_engine` 在本环境多次返回空结果；`document_query` 访问 FastAPI 文档时出现 `524`。最终以 `curl` 拉取上述页面原文完成调研。

## 4. 技术方案比较

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 纯文件系统，无数据库 | 最简单 | 每次请求扫描或难以记录损坏文件 | 不采用 |
| 仅按 desktop/mobile 目录 | 人工直观 | 放错目录会分错，正方形难处理 | 仅作可选整理方式 |
| FastAPI + SQLite + 缓存 | 简单、可备份、可迁移 | 单机写入有锁 | **采用** |
| FastAPI + PostgreSQL | 多实例更强 | 多一个有状态服务，备份更重 | 当前不采用 |
| 外链随机图 API | 无本地存储 | 不能离线、不能备份自己的图库 | 不采用 |

## 5. 最终技术架构

```text
Client
  -> (可选反向代理)
    -> docker compose service `api`
      -> uvicorn app.main:app :8080
        -> Catalog 内存缓存
        -> SQLite WAL: data/database/images.db
        -> files: data/images/**
```

行为要点：

- `type` 参数优先于 User-Agent
- Desktop → 横屏优先；Mobile → 竖屏优先
- 正方形默认两边都可返回（`SQUARE_POLICY=both`）
- 一侧为空则 fallback 到另一侧
- 损坏、缺失、不支持格式被跳过
- 全空返回 404，不 500

## 6. 数据库选型

**使用 SQLite，不使用 PostgreSQL。**

需要数据库的原因：

- 缓存 width / height / orientation / content-type / mtime
- 避免每次请求读完整张图片或遍历全部文件
- health 需要稳定计数
- Backup / Restore 需要一份可校验的元数据快照

不需要 PostgreSQL 的原因：

- 单进程、读多写少
- 没有多用户账号体系
- 没有多实例同时写库
- 没有复杂查询或高并发统计

当前优点：零额外容器、备份就是一个文件、迁移复制 `data/` 即可。
当前缺点：不适合多副本同时写；WAL 迁移时要比纯“复制一个文件”稍小心。

未来应升级 PostgreSQL 的信号：多实例水平扩展、多服务共享同一元数据、持续高并发写入、需要细粒度权限或复杂报表。

## 7. Docker Compose 架构

`docker-compose.yml` 只有一个 `api` 服务：

- `build: .`
- `env_file: .env`
- 容器内强制 `DATA_DIR=/app/data` 等路径，避免把宿主机相对路径带进容器
- `./data:/app/data` bind mount
- `restart: unless-stopped`
- healthcheck 访问 `http://127.0.0.1:8080/health`
- 无 `privileged`、无 docker.sock、无 `network_mode: host`

## 8. 数据持久化设计

| 数据 | 位置 | 是否进镜像 |
| --- | --- | --- |
| 图片 | `./data/images` | 否 |
| SQLite | `./data/database/images.db` | 否 |
| 日志 | `./data/logs` | 否 |
| 配置 | `.env` | 否 |

容器删除后，只要项目目录和 `./data` 还在，`docker compose up -d` 即可恢复。

## 9. 项目目录

见 README。核心新增代码在 `app/main.py`、`app/catalog.py`、`app/db.py`，测试在 `tests/`，运维脚本在 `scripts/`。

## 10. 已实现功能

- `GET /health`
- `GET /random` 与 `?type=desktop|mobile`
- Windows / macOS / Android / iPhone / 无 UA / curl UA 识别
- jpg / jpeg / png / webp
- 宽高自动分类 + 正方形策略
- fallback、损坏文件跳过、缺失文件剔除
- 访问日志（含反向代理 IP）
- 可选 `POST /admin/rescan`
- Dockerfile / docker-compose.yml / .env.example / .gitignore
- `scripts/backup.sh`（`VACUUM INTO`）与 `scripts/restore.sh`（确认码 + 预恢复副本）
- README / MIGRATION / REPORT

## 11. 创建和修改的文件

创建：

- `app/main.py`
- `app/catalog.py`
- `app/db.py`
- `tests/conftest.py`
- `tests/test_api.py`
- `tests/test_ua.py`
- `scripts/backup.sh`
- `scripts/restore.sh`
- `scripts/generate_samples.py`
- `scripts/build-image.sh`
- `Dockerfile`
- `docker-entrypoint.sh`
- `docker-compose.yml`
- `LICENSE`
- `.dockerignore`
- `pytest.ini`
- `requirements-dev.txt`
- `README.md`
- `MIGRATION.md`
- `REPORT.md`

修改：

- `app/config.py`（`lru_cache`）
- `app/ua.py`（沿用既有实现）
- `.env` / `.env.example`
- `.gitignore`
- `requirements.txt`

运行期生成（不提交）：

- `data/images/**` 示例图
- `data/database/images.db`
- `data/logs/access.log`
- `backups/backup-2026-08-16-185133.tar.gz`
- `dist/random-image-api-verified.tar` 及 SHA-256 校验文件

## 12. 自动测试过程

实际执行：

```bash
python3 scripts/generate_samples.py
python3 -m pytest tests -q
```

覆盖：

- `/health`
- `/random`
- Windows / macOS / Android / iPhone UA
- `type=desktop` / `type=mobile` / 非法 type
- 无 UA、curl UA
- 空目录、目录不存在
- Desktop 无图 fallback、Mobile 无图 fallback
- 损坏图片、文件被删、不支持 gif
- 管理接口 token

## 13. 自动测试结果

**21 passed。**

输出摘要：

```text
.....................                                                    [100%]
```

另有 Starlette/httpx `TestClient` 弃用警告，不影响结果。未伪造。

## 14. Docker Compose Build 结果

**已在独立远程 Debian 13 测试环境实际执行成功。**

测试环境提供 Docker Engine 28.5.2 和 Docker Compose v2.40.3。为避免影响服务器已有业务，测试使用全新隔离目录、独立 Compose project、独立镜像名称与标签、未占用的高位端口以及独立容器网络。

实际执行 `docker compose build --pull`，最终镜像构建成功。修复后的测试镜像 ID：

```text
sha256:9bb7afa1bd9d70bde960e59b6f7e386b3d2208c42b67b7a8279e38790051f76b
```

构建和运行过程未使用 `privileged`，未将 Docker socket 挂载到应用容器，也未停止、删除或重建服务器原有容器。

## 15. Docker Compose 启动结果

已实际执行独立项目的 `docker compose up -d`。最终状态：

```text
api-1   Up (healthy)   0.0.0.0:<测试端口>->8080/tcp
```

启动前记录了服务器原有 8 个容器名称；完整验收后再次比较，名称集合一致，证明测试没有停止、删除或替换原有容器。

## 16. Healthcheck 结果

Docker healthcheck 已实际观察到从 `starting` 变为 `healthy`。应用层 `/health` 返回 HTTP 200、`Content-Type: application/json`：

```json
{
  "status": "ok",
  "service": "random-image-api",
  "version": "1.0.0",
  "images": {"total": 5, "desktop": 2, "mobile": 2, "square": 1},
  "database": "ok",
  "fallback_enabled": true,
  "square_policy": "both",
  "last_scan_error": null
}
```

## 17. API 实际请求结果

对远程 Compose 服务进行了真实 curl，并在容器内用 Pillow 验证源图片：

| 请求 | HTTP | Content-Type | 结果 |
| --- | --- | --- | --- |
| `/health` | 200 | application/json | total=5，database=ok |
| Windows UA | 200 | image/png | 返回 Desktop 选择池图片 |
| Android UA | 200 | image/jpeg | 返回 Mobile 选择池图片 |
| iPhone UA | 200 | image/png | 返回 Mobile 选择池图片 |
| Android + `type=desktop` | 200 | image/png | 显式参数覆盖 UA |
| Windows + `type=mobile` | 200 | image/png | 显式参数覆盖 UA |
| `type=test` | 400 | application/json | 合理错误，不崩溃 |

所有成功图片响应体均大于 100 字节，`Content-Type` 为 `image/*`；容器内 Pillow 验证 5 张源图片全部有效。访问日志实际记录了时间、路径、HTTP 状态、客户端类型、图片、耗时和代理后的客户端地址。

## 18. Restart 验证

已实际执行：

```bash
docker compose restart
```

容器从 `starting` 恢复为 `healthy`，随后 `/health` 和 `/random` 均成功。

## 19. 数据持久化验证

- 已实际执行 `docker compose down`，确认专属容器和项目网络被移除；
- `./data/images` 的 5 张图片和 `./data/database/images.db` 仍存在；
- 再执行 `docker compose up -d` 后恢复为 `healthy`；
- 图片前后 SHA-256 清单一致；
- SQLite 逻辑检查仍为 5 行元数据；
- `/health` 返回 `total=5`、`database=ok`。

## 20. Backup 验证

在远程隔离项目中实际执行 `./scripts/backup.sh`，生成约 8.5K 的归档。检查确认归档包含 5 张图片、SQLite 数据库、`.env.example` 和脱敏配置。

归档使用 SQLite `VACUUM INTO`，未简单复制热库；`ADMIN_TOKEN` 等敏感字段不会保留实际值。

## 21. Restore 验证

已在停止隔离 Compose 项目后实际执行：

```bash
RESTORE_CONFIRM=YES ./scripts/restore.sh <backup-file>
docker compose up -d
```

Restore 会先把原数据保存到 `backups/pre-restore-*`。恢复后容器重新变为 `healthy`，`/health` 返回 5 张图片、`database=ok`，SQLite 元数据可用。

## 22. Migration 验证

完成了真实远程迁移式验证：

1. 将不含 `.env`、数据库、日志、备份和真实图片的源码包上传到全新隔离目录；
2. 生成专用 `.env`，使用独立镜像名、Compose project 和端口；
3. 构建镜像并启动服务；
4. 验证 healthcheck、API、图片和 SQLite；
5. 验证 restart、down/up、Backup/Restore；
6. 使用 `docker save` 导出完整镜像 tar；
7. 下载镜像归档并比对 SHA-256。

最终镜像归档约 186M，已保存到本项目被 Git 忽略的 `dist/`，并通过 `tar -tf` 与 SHA-256 校验。镜像包只含应用运行环境，不含生产图片、数据库、`.env`、日志或备份。

## 23. 已解决的问题

- 内置搜索工具空返回：改用 curl 拉取官方文档；
- 避免每次请求扫盘：采用 SQLite 元数据与内存缓存；
- SQLite 热备份风险：改用 `VACUUM INTO`；
- Restore 误删风险：要求 `RESTORE_CONFIRM=YES`，并先保存 `pre-restore-*`；
- 正方形计数不重复计入总数：健康检查按真实 orientation 计数，选择池按 policy 复用；
- 部分源码权限为 `0600`，导致非 root 容器用户无法读取：统一源码权限，并使用 `COPY --chown=appuser:appuser`；
- bind mount 目录由宿主机 root 创建，导致非 root 应用无法写日志和 SQLite：增加受控 entrypoint，仅处理 `/app/data` 权限后使用 `gosu` 降权运行应用；
- 固定 `container_name` 不利于并行与隔离部署：移除固定容器名，使用 Compose project 管理命名；
- 镜像名称原先固定：增加 `IMAGE_NAME` / `IMAGE_TAG` 环境变量；
- 缺少可分发镜像文件：增加 `scripts/build-image.sh`，执行 build、save、tar 检查和 SHA-256 生成。

## 24. 未解决的问题

1. `/proc/...` 模拟数据库失效的自动测试只断言 404 或 503，不是稳定的 503 专项环境。
2. 未进行公网压力测试；当前任务范围只验证了功能、部署和数据安全路径。

## 25. 已知风险

- User-Agent 分类是启发式，平板、折叠屏或桌面模式 Android 可能不准；可用 `type=` 覆盖；
- 信任 `X-Forwarded-For` 时，若直接暴露公网且无可信反向代理，客户端可伪造 IP；
- 大量超大图片会占用磁盘和带宽；服务不做实时缩放；
- 周期扫描对超大图库会产生 CPU / IO 尖峰，可加大 `SCAN_INTERVAL_SECONDS`；
- entrypoint 会递归调整当前项目挂载的 `/app/data` 所有权，大型图库首次启动可能较慢。

## 26. 性能问题

当前测试为 5 张图片。设计上：

- 未变化文件按 size + mtime 跳过解码；
- 请求不做全目录扫描；
- 随机选择使用内存池；
- 不引入 Redis。

图片数量达到数万、单张数 MB 时，瓶颈更可能是磁盘、首次权限扫描和网络带宽，而不是 SQLite。

## 27. 安全问题

- `.env` 已加入 `.gitignore`；
- 备份会清空 `TOKEN` / `PASSWORD` / `SECRET` / `API_KEY` 类字段；
- `ADMIN_TOKEN` 为空时管理接口返回 404；
- 无鉴权读图：服务按公开随机图 API 设计，不应放置私密照片；
- 当前未实现速率限制；
- `.a0proj/`、`dist/`、真实图片、数据库、日志和备份均不进入 Git；
- 远程测试报告不记录服务器地址、SSH 用户、密码或其他可识别信息；
- 应用进程最终以非 root 的 `appuser` 身份运行，entrypoint 仅在启动时处理挂载的 `/app/data`。

## 28. 后续优化建议

1. 若需要固定“同一客户端短时间不重复”，可加进程内 LRU，仍不必引入 Redis；
2. 可选增加按格式过滤的 query，例如 `?format=webp`；
3. 对超大目录可改用 inotify 或手动 rescan，并关闭短周期全量扫描；
4. 若未来多机部署，再评估对象存储与 PostgreSQL，而不是现在拆分微服务。
