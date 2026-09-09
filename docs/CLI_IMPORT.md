# 归档导入 CLI 使用手册

本文说明如何通过 Docker Compose 使用 Random Image API 的安全归档导入器。适用于 ZIP、TAR.GZ 和 TGZ 图片归档，涵盖预检、正式导入、多标签、磁盘空间、服务重扫及常见错误。

> 推荐流程：**将归档放入 `data/imports/` → 执行 `--dry-run` → 检查统计 → 正式导入 → 重扫服务 → 验证标签接口 → 按需删除源归档。**

## 1. 工作方式与数据边界

Importer 会：

- 拒绝路径穿越、绝对路径、符号链接及特殊归档成员；
- 限制归档大小、成员数量、单成员大小、总解压大小、压缩比和图片像素数；
- 用 Pillow 检查文件是否为有效 JPG/JPEG、PNG 或 WebP 图片；
- 按图片经过 EXIF 方向修正后的真实宽高分类；
- 将横屏、竖屏和方形图片分别保存到 `desktop/`、`mobile/`、`square/`；
- 使用 SHA-256 内容哈希命名和去重；
- 可将一个或多个公共标签写入 SQLite。

Importer 只处理归档文件，不直接接受普通目录。标签关系保存在 SQLite，不写入或修改图片元数据。

CLI 的 `--tag` 会应用于**本次归档涉及的全部有效图片**。CLI 不会根据归档父目录自动创建不同标签；需要检查和调整目录标签映射时，应使用管理网页的归档 preview-confirm 流程。

## 2. 执行前准备

### 2.1 进入项目根目录

命令应在包含 Compose 文件的项目根目录执行：

```bash
cd /path/to/random-image-api
```

先确认配置可解析：

```bash
docker compose config --quiet
```

### 2.2 准备导入目录

推荐把归档放入：

```text
data/imports/
```

创建目录：

```bash
mkdir -p data/imports
```

项目 Compose 已将宿主机 `./data` 挂载到容器 `/app/data`，因此：

```text
宿主机：data/imports/archive.zip
容器内：/app/data/imports/archive.zip
```

归档位于 `data/imports/` 时，不需要再增加单文件 `-v` 挂载。

### 2.3 路径必须正确引用

文件名或路径含空格、中文、括号或其他特殊字符时，应使用双引号包住完整路径：

```bash
"/app/data/imports/my archive.zip"
```

否则 Shell 会把路径拆成多个参数，Docker Compose 可能把文件名的一部分误认为 service，并报告：

```text
no such service: ...
```

自动化场景建议使用 ASCII、无空格文件名，例如：

```text
wallpaper-batch-001.zip
```

## 3. 第一步：只做安全预检

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --dry-run
```

`--dry-run` 不写入图片，也不修改数据库。

典型输出：

```text
DRY-RUN complete: imported=100 duplicates=3 skipped=2 desktop=60 mobile=35 square=5; square policy=both (both uses canonical desktop storage)
```

| 字段 | 含义 |
| --- | --- |
| `imported` | 正式执行时可新增的图片数量 |
| `duplicates` | 归档内重复或图库中已存在的图片数量 |
| `skipped` | 不是支持图片或无法识别的普通文件数量 |
| `desktop` | 内容识别为横屏的图片数量 |
| `mobile` | 内容识别为竖屏的图片数量 |
| `square` | 内容识别为方形的图片数量 |

`skipped` 不一定表示整个归档失败。若命令最终返回 `DRY-RUN complete`，说明归档整体通过安全检查；可根据需要检查被跳过的内容。

## 4. 第二步：正式导入

确认预检结果后，删除 `--dry-run`：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images
```

成功时会输出：

```text
IMPORT complete: ...
```

当命令输出 `IMPORT complete` 并返回 Shell 提示符时，本次 importer 进程已经结束。

## 5. 导入时添加标签

### 5.1 添加一个标签

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --database-path /app/data/database/images.db \
  --tag nature
```

### 5.2 一次添加多个标签

每个标签重复写一次 `--tag`：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --database-path /app/data/database/images.db \
  --tag nature \
  --tag wallpaper \
  --tag featured
```

使用 `--tag` 时必须同时提供：

```text
--database-path /app/data/database/images.db
```

不存在的标签会创建，已有标签会复用。重复传入同一标签不会创建重复关系。

标签 slug 应使用 1–63 位小写英文字母、数字和单连字符，例如：

```text
nature
mobile-wallpaper
collection-2026
```

不要使用空格、中文、大写字母或下划线作为 slug。中文名称可在导入后通过管理网页修改标签的显示名称，API slug 不受影响。

### 5.3 带标签预检

可以用正式导入相同的标签参数做预检：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --database-path /app/data/database/images.db \
  --tag nature \
  --tag featured \
  --dry-run
```

只要存在 `--dry-run`，图片和数据库都不会改变。

## 6. 导入文件夹

CLI 不直接接受目录。先把目录打包：

```bash
tar -czf data/imports/photos.tar.gz -C /path/to photos
```

然后预检或导入：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  /app/data/imports/photos.tar.gz \
  --images-dir /app/data/images \
  --dry-run
```

如果不需要归档安全导入，也可以把已确认安全的图片直接复制到 `data/images/desktop/`、`data/images/mobile/` 或 `data/images/square/`，再触发重扫；这种方式不会自动添加标签。

## 7. 大归档和资源限制

CLI 默认限制：

| 限制 | 默认值 |
| --- | ---: |
| 归档文件大小 | 512 MiB |
| 成员数量 | 10,000 |
| 单成员解压大小 | 100 MiB |
| 总解压大小 | 1 GiB |
| 最大压缩比 | 200 |
| 单张图片像素数 | 100,000,000 |

超过默认限制时，应根据可信归档的实际情况和服务器可用磁盘谨慎提高。例如：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/large-archive.zip" \
  --images-dir /app/data/images \
  --max-archive-bytes 3221225472 \
  --max-total-bytes 8589934592 \
  --dry-run
```

可调整参数：

| 参数 | 作用 |
| --- | --- |
| `--max-archive-bytes` | 归档文件最大字节数 |
| `--max-members` | 最大归档成员数 |
| `--max-member-bytes` | 单成员最大解压字节数 |
| `--max-total-bytes` | 所有成员最大解压字节数 |
| `--max-compression-ratio` | 最大压缩比 |
| `--max-image-pixels` | 单张图片最大像素数 |

不要为未知来源归档取消或盲目放大安全限制。正式导入前还应确认磁盘剩余空间：

```bash
df -h .
du -sh data/imports data/images data/database 2>/dev/null
```

## 8. 磁盘空间说明

正式导入后通常会同时保留：

1. `data/imports/` 中的源归档；
2. `data/images/` 中解压并按内容保存的永久图片；
3. SQLite 元数据和标签关系；
4. 可选的备份文件。

因此导入后磁盘占用增加是正常现象。标签关系本身占用很小，主要空间来自源归档和导入后的图片。

确认图片、标签和随机接口均正常后，可以删除不再需要的源归档：

```bash
rm -- "data/imports/archive.zip"
```

路径中的 `--` 用于阻止以连字符开头的文件名被当作命令参数。删除源归档不会删除已经导入的图片或 SQLite 标签关系。

检查 importer staging 是否残留：

```bash
find data/images -maxdepth 1 -type d -name '.import-staging-*' -print
```

正常成功或失败清理后应无输出。

## 9. 导入后让运行服务立即识别

正式导入后，可等待周期扫描，也可以调用管理重扫接口：

```bash
set -a
. ./.env
set +a

curl -fsS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/rescan
```

检查健康状态：

```bash
curl -fsS http://127.0.0.1:10086/health
```

也可以重启 API 触发扫描：

```bash
docker compose restart api
```

优先推荐 `/admin/rescan`，因为无需中断服务。

## 10. 验证标签和随机接口

假设导入时使用了：

```text
--tag nature
```

验证主题随机接口：

```bash
curl -fsS -D - -o /dev/null \
  "http://127.0.0.1:10086/random?tag=nature&type=desktop"
```

成功时应返回 `200`，响应头通常包括：

```text
X-Image-Tag: nature
X-Image-Source: local
```

也可以登录管理网页，在图片库中按“本地图片、方向、标签”组合筛选。

## 11. JSON 输出与退出码

自动化脚本可使用 `--json`：

```bash
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  /app/data/imports/archive.zip \
  --images-dir /app/data/images \
  --dry-run \
  --json
```

成功退出码为 `0`；可预期的导入、校验、文件或 SQLite 错误退出码为 `2`。

## 12. 常见问题

### `no such service: 文件名的一部分`

原因通常是路径含空格但未加引号。引用完整路径，或把归档重命名为 ASCII、无空格文件名。

### `archive file size limit exceeded`

归档超过 `--max-archive-bytes`。确认归档可信、磁盘充足后，显式提高该限制并重新执行 `--dry-run`。

### `archive expanded size limit exceeded`

归档解压总量超过 `--max-total-bytes`。不要只看压缩包大小，应为解压后的图片预留空间。

### `--database-path is required when --tag is used`

使用 `--tag` 时补充：

```text
--database-path /app/data/database/images.db
```

### 再次导入同一归档显示大量 `duplicates`

这是内容哈希去重的正常结果，不会重复复制图片。再次执行带标签的命令可为相同图片补充指定公共标签。

### `IMPORT complete` 后网页暂时看不到图片

等待自动扫描，或调用 `/admin/rescan`。必要时使用 `docker compose restart api`。

## 13. 推荐完整流程

```bash
cd /path/to/random-image-api

# 1. 检查 Compose 配置和磁盘
docker compose config --quiet
df -h .

# 2. 备份现有数据
./scripts/backup.sh

# 3. 预检
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --database-path /app/data/database/images.db \
  --tag nature \
  --tag featured \
  --dry-run

# 4. 正式导入：确认上一步后删除 --dry-run
docker compose run --rm --no-deps \
  --entrypoint python \
  api -m app.importer \
  "/app/data/imports/archive.zip" \
  --images-dir /app/data/images \
  --database-path /app/data/database/images.db \
  --tag nature \
  --tag featured

# 5. 重扫
set -a
. ./.env
set +a
curl -fsS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:10086/admin/rescan

# 6. 验证
curl -fsS http://127.0.0.1:10086/health
curl -fsS -D - -o /dev/null \
  "http://127.0.0.1:10086/random?tag=nature&type=desktop"
```

## 14. 参数速查

| 参数 | 是否必需 | 说明 |
| --- | --- | --- |
| `archive` | 是 | ZIP、TAR.GZ 或 TGZ 的容器内路径 |
| `--images-dir` / `--output-dir` | 是 | 图片持久化目录，两个名称等价 |
| `--database-path` | 使用标签时必需 | SQLite 数据库路径 |
| `--tag SLUG` | 否 | 整批公共标签，可重复多次 |
| `--dry-run` | 否 | 只验证和统计，不写入 |
| `--json` | 否 | 输出机器可读 JSON |
| `--square-policy` | 否 | 方形图片随机池策略：`both`、`desktop` 或 `mobile`；物理文件仍写入 `square/` |
| 各 `--max-*` 参数 | 否 | 覆盖默认安全和资源限制 |
