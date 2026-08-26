# Account Manager 多服务器 OSS 云归档

## 1. 功能范围

Account Manager 在调度任务结束后，将任务最终产物和完整数据库快照异步归档到阿里云 OSS。该机制同时覆盖本地 GPU 任务和第三方 API 任务，不依赖 `scheduler_assets` 是否存在。

每台服务器继续使用自己的 `scheduler.sqlite3` 和 `history.sqlite3`。任务通过 `prompt_id` 关联，通过固定 `cloud_server_id` 区分来源服务器。当前约定：

- 杭州服务器：`goumei`
- 5090 双卡服务器：`seetacloud-5090x2`

媒体对象写入公开前缀：

```text
Goumee-ComfyUI-Server-Data/public/servers/<server_id>/YYYY/MM/DD/<prompt_id>/<ordinal>-<filename>
```

每个任务唯一的 gzip JSON 清单写入私有前缀：

```text
Goumee-ComfyUI-Server-Data/private/jobs/<prompt_id>/manifest.json
```

manifest 对象使用 `Content-Type: application/json`、`Content-Encoding: gzip`、`Cache-Control: no-store` 和 private object ACL。数据库只永久保存 `oss://bucket/key`，不保存会过期的签名 URL。

## 2. 安全前置条件

曾经出现在聊天、工单、日志或截图里的 AccessKey 必须先在阿里云控制台轮换并禁用。不要把旧密钥继续用于部署。

新的 RAM 身份应只具备以下最小权限：

- 对 `Goumee-ComfyUI-Server-Data/public/*` 和 `private/*` 写入对象；
- 对断点续传所需的 multipart 操作授权；
- 如插件不需要删除云对象，不授予 `DeleteObject`；
- 不授予 Bucket 管理、Bucket Policy 修改或其他 Bucket 权限；
- private 前缀只允许授权身份读取；
- 匿名读取策略只覆盖 `Goumee-ComfyUI-Server-Data/public/*`。

完整 manifest 会原样保存数据库中与 prompt 可关联的 payload、API 请求/响应、错误和日志。它可能含 Prompt、Base64 输入、供应商 URL、Cookie 或其他隐私数据，因此 `private/*` 匿名 GET 和匿名 List 都必须拒绝。

## 3. 安装依赖

在运行 ComfyUI 的同一个 Conda 环境中安装：

```bash
python -m pip install "alibabacloud-oss-v2>=1.3.0"
```

插件使用官方 OSS Python SDK V2、V4 签名、SDK CRC 校验和 checkpoint 断点续传。

## 4. 凭据文件

管理脚本会在每个 tmux Worker 启动前自动加载与脚本同目录的 `oss.env`。凭据只能使用环境变量：

```bash
OSS_ACCESS_KEY_ID='替换为轮换后的RAM AccessKey ID'
OSS_ACCESS_KEY_SECRET='替换为轮换后的RAM AccessKey Secret'
export OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET
```

设置严格权限：

```bash
chmod 600 /mnt/Comfyui-admin/oss.env
chmod 600 /root/autodl-tmp/Comfyui-admin/oss.env
```

不要把 `oss.env` 放进插件 Git 仓库，也不要把 AccessKey 写入 `config.json`、manifest 或日志。

## 5. config.json 配置

两台服务器只需让 `cloud_server_id` 不同，其余 OSS 配置保持一致：

```json
{
  "cloud_archive_enabled": true,
  "cloud_server_id": "seetacloud-5090x2",
  "cloud_oss_region": "cn-hangzhou",
  "cloud_oss_endpoint": "oss-cn-hangzhou.aliyuncs.com",
  "cloud_oss_bucket": "goumee-coze",
  "cloud_oss_prefix": "Goumee-ComfyUI-Server-Data",
  "cloud_public_base_url": "https://goumee-coze.oss-cn-hangzhou.aliyuncs.com",
  "cloud_max_attempts": 3,
  "cloud_upload_concurrency": 2,
  "cloud_remote_max_bytes": 21474836480,
  "cloud_manifest_max_bytes": 2147483648
}
```

杭州服务器把 `cloud_server_id` 改成 `goumei`。插件将全服务器对象上传并发硬限制为最多 2；配置更大的数值也不会突破 2。

启用后，每个 ComfyUI 进程都有一个后台领取线程，但 SQLite claim、lease 和全局活动计数会保证任务不重复上传。上传 lease 每 60 秒续期，进程崩溃后其他 Worker 可接管过期 lease。

## 6. 任务结束行为

任务结束回调只进行产物发现和 SQLite 登记，不计算大文件哈希、不下载远程媒体、不压缩 manifest，也不进行 OSS 网络请求。

后台上传器只遍历 `history.outputs`：

- 支持 `filename + subfolder + type`；
- 支持 `fullpath` 或 `file_path`；
- 支持最终输出中的绝对 HTTP/HTTPS URL；
- 不遍历输入 prompt；
- 不从 API Log 猜测最终结果；
- 不把 API 请求 Base64、参考图、输入视频或轮询中间 URL 当成独立产物。

远程 URL 下载会拒绝本机地址、内网地址和非 HTTP(S) 协议，重定向后的目标也重新校验，以避免后台下载器访问服务器内网。

每个媒体对象和 manifest 都只有 3 次外层 attempt：第一次立即、第二次 30 秒后、第三次 120 秒后。SDK 内部网络重试属于一次外层 attempt。

失败任务有部分有效输出时仍上传已有输出；找不到历史本地文件时对应产物记为 `source_missing`；没有任何媒体输出的任务仍上传私有 manifest，并将任务状态记为 `no_artifacts`。上传成功不会删除 ComfyUI 原始输出文件。

## 7. SQLite 状态查询

任务级状态：

```sql
SELECT
  prompt_id,
  server_id,
  generation_status,
  cloud_status,
  artifact_count,
  uploaded_count,
  failed_count,
  manifest_upload_status,
  manifest_attempt_count,
  manifest_oss_uri,
  manifest_sha256,
  last_error,
  updated_at
FROM scheduler_cloud_tasks
WHERE prompt_id = '替换为prompt_id';
```

产物级状态：

```sql
SELECT
  ordinal,
  source_kind,
  filename,
  upload_status,
  attempt_count,
  size_bytes,
  sha256,
  oss_uri,
  public_url,
  etag,
  oss_crc64,
  last_error
FROM scheduler_cloud_artifacts
WHERE prompt_id = '替换为prompt_id'
ORDER BY ordinal;
```

`cloud_status` 可能是 `pending`、`uploading`、`cloud_completed`、`cloud_failed` 或 `no_artifacts`。产物还可能显示 `source_missing`。完整 manifest 本身第三次上传失败时，任务一定落为 `cloud_failed`。

## 8. 历史回填

回填工具没有筛选条件时会拒绝运行。建议永远先预览：

```bash
./manage_comfyui.sh cloud-backfill --dry-run --prompt-id 83f2483d-2f09-4e45-85c5-7ab7330ee8b2
```

按时间和数量回填：

```bash
./manage_comfyui.sh cloud-backfill \
  --since 2026-08-25T00:00:00+08:00 \
  --until 2026-08-26T00:00:00+08:00 \
  --limit 20
```

重试已经最终失败的指定任务：

```bash
./manage_comfyui.sh cloud-backfill \
  --prompt-id 0849254c-7457-4d7b-bb1d-055aeb4d8b29 \
  --retry-cloud-failed
```

默认跳过已经存在云归档记录的 prompt。回填只写入 pending 记录，实际下载、哈希、上传和 manifest 生成仍由运行中的后台 Worker 异步处理。

## 9. manifest 内容与完整性

manifest 动态读取导出时真实的 SQLite schema，导出：

- `scheduler_jobs` 当前 prompt 行；
- 按 `sequence` 升序的全部 `scheduler_api_logs`；
- `scheduler_job_logs`；
- `history` 原始 `data` 字符串和便于阅读的 `parsed_data`；
- 通过 prompt_id 或 history Asset ID 关联的 `scheduler_assets`；
- 按任务 `worker_port` 找到的 Worker 导出时快照；
- `scheduler_cloud_tasks` 与 `scheduler_cloud_artifacts`；
- 产物对象键、永久 OSS URI、公开 URL、大小、SHA-256、ETag、CRC64、attempt 和错误。

SQLite BLOB 始终使用带原始类型、字节数、SHA-256 和 Base64 的 typed envelope，不会因为内容恰好是 UTF-8 就变成字符串。`sqlite_sequence` 不导出。

`integrity.manifest_content_sha256` 是把该字段置空后，对规范化 JSON 计算的 SHA-256。`scheduler_cloud_tasks.manifest_sha256` 则是最终 gzip 上传字节流的 SHA-256。

临时 gzip 位于：

```text
<plugin>/cloud_staging/<server_id>/<prompt_id>/manifest.json.gz.tmp
```

上传成功立即删除；进程崩溃或最终失败时保留以便人工诊断和重试。默认压缩后安全上限为 2 GiB，超过时不截断内容，而是记录 `manifest_size_limit_exceeded` 并按重试状态机处理。

## 10. 上线验收

上线前备份两台服务器的 `scheduler.sqlite3` 和 `history.sqlite3`。先在临时前缀验证以下项目：

1. public 媒体匿名 GET 返回 200；
2. private manifest 匿名 GET 返回 403；
3. 使用授权 SDK 或 `ossutil` 能读取 private manifest；
4. 根据 `Content-Encoding: gzip` 解压后可解析 JSON；
5. manifest 记录数、自身内容哈希与 SQLite 查询一致；
6. 模拟 OSS 不可达时生成任务本身不被阻塞，第三次失败后 SQLite 显示 `cloud_failed`；
7. 本地 GPU 成功、API 成功、失败但有部分输出、失败且空输出四类任务都有 private manifest；
8. 有媒体的任务在 `scheduler_cloud_artifacts.public_url` 中有逐一对应的公开链接。

Bucket Policy 和 RAM Policy 是 OSS 控制面的验收项，插件无法替代控制台权限配置。没有完成 private 403 验证前，不应把完整 manifest 功能视为正式上线。
