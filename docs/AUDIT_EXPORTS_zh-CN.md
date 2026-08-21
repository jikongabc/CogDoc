# 审计与合规导出

CogDoc 的在线审计日志按租户维护连续 SHA-256 哈希链。审计导出模块从已验证的时间点快照生成不可变 NDJSON 制品，适合合规取证、离线归档和送入 SIEM。

## 安全边界

- 只有当前租户的 owner/admin（`manage_access`）可以创建、查看、下载或删除导出。
- 导出调用 `AuditStore.snapshot()`，不会绕过校验直接读取日志文件；日志损坏时任务失败关闭。
- 请求正文、Cookie、令牌和凭据不会进入审计事件，导出也不会扩大字段集合。
- 每个租户最多同时运行两个任务。保留时间为 5 分钟到 7 天，默认 1 天。
- 下载前重新流式校验大小与 SHA-256，响应使用 `no-store` 和 `nosniff`。
- 删除使用 revision CAS；过期清理和手工删除都会移除磁盘制品。

## 文件格式

文件扩展名为 `.ndjson`。第一行是清单，后续每行是一个原始审计事件，按租户序号升序排列：

```json
{"schema_version":"v1","record_type":"manifest","tenant_id":"tenant-a","event_count":42,"first_sequence":10,"last_sequence":51,"source_chain_head":"...","filters":{}}
```

`source_chain_head` 是过滤前、所选序号窗口最后一条源事件的哈希，因此即使动作过滤省略了中间事件，仍能记录导出所依据的已验证源快照锚点。作业元数据另外给出整个文件的 `artifact_sha256` 与 `byte_size`。离线归档应同时保存清单、文件摘要和外部可信时间戳；本地哈希链不是数字签名或 WORM 存储的替代品。

## API

- `POST /v1/audit-events/exports`：创建异步任务。可传 `from_sequence`、`to_sequence`、`actions`、`statuses` 和 `retention_seconds`。
- `GET /v1/audit-events/exports`：列出当前租户最近任务。
- `GET /v1/audit-events/exports/{job_id}`：轮询状态。
- `GET /v1/audit-events/exports/{job_id}/content`：下载成功任务的 NDJSON。
- `DELETE /v1/audit-events/exports/{job_id}?expected_revision=N`：删除终态任务。

状态为 `pending`、`running`、`succeeded`、`failed`、`expired` 或 `deleted`。进程重启会恢复 pending/running 作业；临时文件只有在 fsync 完成后才原子发布。

## 备份与恢复

元数据位于主 `state.db` 的 `audit_export_jobs` 表，制品位于 `COGDOC_DATA_DIR/audit/exports/`。恢复点必须同时包含 `state.db`、`audit/events.jsonl` 与该目录，否则下载完整性检查会拒绝不匹配或缺失的制品。

建议把下载后的文件写入对象锁定存储，并由外部系统再次计算 SHA-256。不要延长应用内临时保留期来替代正式归档策略。
