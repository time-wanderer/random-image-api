# VPS 迁移手册

目标：半年以后换一台全新 Linux VPS，只安装 Docker Engine 和 Docker Compose Plugin，复制项目与数据、配置 `.env`，即可用 `docker compose up -d` 恢复服务。

本项目不依赖当前机器的 IP、域名、用户名或绝对路径。

## 必须迁移

| 路径 | 原因 |
| --- | --- |
| 整个项目源码目录 | Dockerfile、compose、脚本、应用代码 |
| `data/images/` | 生产图片 |
| `data/database/images.db` | SQLite 元数据 |
| `data/database/images.db-wal` / `images.db-shm` | 若存在，表示 WAL 未完全 checkpoint；建议先停服务再拷 |
| `.env` | 运行配置；到新机器后按端口和 Token 再检查一遍 |
| `backups/` | 历史备份，建议一并带走 |

## 不需要迁移

| 路径 | 原因 |
| --- | --- |
| `__pycache__/`、`.pytest_cache/`、`.venv/` | 可重建 |
| `data/logs/` | 日志，不是业务数据 |
| Docker 容器、镜像、匿名卷 | 新机器重新 `docker compose build` |
| 当前 VPS 的 `/root`、`/etc`、系统软件包 | 新机器只装 Docker |
| 宿主机上的 Python / SQLite | 运行时在容器内 |

本项目未使用 PostgreSQL。没有 `pg_dump` 步骤。

## 旧 VPS 操作

### 1. 检查服务

```bash
cd /path/to/random-image-api
docker compose ps
docker compose logs --tail=100
curl -sS http://127.0.0.1:${APP_PORT:-10086}/health
```

确认 `status` 为 `ok`，记下图片数量。

### 2. 停止写入

```bash
docker compose down
```

不要使用 `docker compose down -v`。

停服务后再备份，WAL 会更干净。`backup.sh` 即使在运行中也会用 `VACUUM INTO` 做一致快照，但迁移前停写更稳妥。

### 3. 做 Backup

```bash
./scripts/backup.sh
ls -lh backups/backup-*.tar.gz
```

得到：

```text
backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
```

其中包含：

- 图片
- `VACUUM INTO` 生成的 SQLite 快照（不需要再单独复制 `-wal`/`-shm`）
- 脱敏配置

### 4. 打包项目（可选但推荐）

```bash
cd ..
tar --exclude='random-image-api/.venv' \
    --exclude='random-image-api/.pytest_cache' \
    --exclude='random-image-api/data/logs/*' \
    --exclude='random-image-api/.a0proj' \
    -czf random-image-api-migrate.tar.gz random-image-api
```

也可以只复制：

- 源码与脚本
- `data/images`
- `data/database`
- `.env`
- `backups/backup-*.tar.gz`

### 5. `.env` 处理

把旧 `.env` 带到新机器。到新环境后检查：

- `APP_PORT` 是否与防火墙 / 反向代理一致
- `ADMIN_TOKEN` 是否仍需要
- 不要把旧机器绝对路径写进去

备份包里的 `config/env.sanitized` 会清空 Token，不能替代你自己保管的 `.env`。

## 新 VPS 操作

### 1. 安装 Docker

按官方文档安装 Docker Engine 和 Docker Compose Plugin，例如 Debian / Ubuntu：

- https://docs.docker.com/engine/install/debian/
- https://docs.docker.com/engine/install/ubuntu/

验证：

```bash
docker version
docker compose version
```

不需要安装 Python、pip、SQLite、PostgreSQL。

### 2. 复制项目

把 `random-image-api` 目录或迁移包放到任意路径，例如：

```text
/opt/random-image-api
```

路径可以变，不要写死到应用代码里。

### 3. 配置 `.env`

```bash
cd /opt/random-image-api
cp .env.example .env
# 或使用从旧机器带来的 .env
```

Compose 会覆盖容器内数据路径为 `/app/data`，并挂载当前目录的 `./data`。因此换目录部署时，只要相对结构不变即可。

### 4. Restore

如果新机器上还没有图片/数据库，用备份恢复：

```bash
RESTORE_CONFIRM=YES ./scripts/restore.sh backups/backup-YYYY-MM-DD-HHMMSS.tar.gz
```

如果已经完整复制了整个 `data/` 目录，可以跳过 restore，直接启动。

Restore 会：

1. 校验归档结构
2. 把现有 `data/images`、`data/database` 复制到 `backups/pre-restore-<时间>/`
3. 写入图片和 SQLite 快照
4. 删除恢复后的 `-wal` / `-shm`，避免旧 WAL 与新快照混用

### 5. 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

健康状态应为 `healthy`（取决于 Docker healthcheck 启动宽限期）。

### 6. 验证

```bash
curl -sS http://127.0.0.1:${APP_PORT:-10086}/health
curl -D - -o /tmp/mig.bin http://127.0.0.1:${APP_PORT:-10086}/random
file /tmp/mig.bin
```

判断成功的标准：

- `docker compose ps` 中服务为 running
- `/health` 返回 `status=ok`、`database=ok`
- 图片数量与旧环境一致或符合预期
- `/random` 返回 `200` 且 `Content-Type` 为图片
- `data/images` 下文件仍在

## SQLite 迁移说明

推荐方式 A：先 `docker compose down`，再复制整个 `data/database/`。

推荐方式 B：使用 `./scripts/backup.sh` 的 `VACUUM INTO` 快照，再在新机器 `restore.sh`。

不要在数据库正在写入时只复制 `images.db` 而丢掉不一致的 WAL。若服务还在跑，至少把 `images.db`、`images.db-wal`、`images.db-shm` 一起拷，并在新机器启动前确保三者来自同一次拷贝；更稳妥的做法仍是停服务或使用 `VACUUM INTO`。

恢复后若只放入快照文件，应删除旧的 `-wal`/`-shm`。`restore.sh` 已处理这一点。

## PostgreSQL

当前版本不使用 PostgreSQL，无 `pg_dump` / `pg_restore` 步骤。如果未来升级为 PostgreSQL，必须同时改：

- `docker-compose.yml` 增加数据库服务和 volume
- healthcheck / `depends_on: condition: service_healthy`
- backup / restore 改为 `docker compose exec` + `pg_dump` / `pg_restore`
- 本文件补上数据库密码仅存在 `.env` 的说明

## 如何回滚

1. `docker compose down`
2. 把 `backups/pre-restore-<时间>/data/images` 和 `database` 拷回 `data/`
3. 或对更早的 `backup-*.tar.gz` 再执行一次 restore
4. `docker compose up -d`
5. 再测 `/health` 和 `/random`

## 最小记忆清单

1. 旧机器：`docker compose down` → `./scripts/backup.sh`
2. 带走：源码 + `data/` + `.env` + `backups/`
3. 新机器：安装 Docker Compose Plugin
4. 配 `.env` → `RESTORE_CONFIRM=YES ./scripts/restore.sh <归档>`（若未整目录复制 data）
5. `docker compose up -d --build`
6. 验证 `/health` 和 `/random`
