# 通用来源与知识源运维控制面

CogDoc 把每份内容表示为 `SourceDocument`、不可变 `SourceVersion` 和格式中立的 `SourceLocation`。旧 PDF 与 `[文件:P页码]` 引用保持兼容；幻灯片、工作表单元格、文本行、图片和章节使用可验证的位置，并在公开引用账本中固定 `source_version_id`。

运维控制面把“连接如何取数”“同步是否健康”“当前来源及历史版本是什么”“谁能读取物化文档”串成同一条可审计链。连接器密钥可以来自 AES-GCM 加密凭据库或兼容的环境变量引用，API 响应、连接配置和同步错误都不会返回密钥值。

## 支持范围

直接上传和连接器同步均支持：

- PDF、Markdown、纯文本与 HTML；
- DOCX、PPTX 与 XLSX（包括表格文本）；
- PNG、JPEG、TIFF、BMP 与 WebP。图片文字依赖本机 Tesseract；OCR 未安装或失败时按现有降级策略处理。

内置连接器包括本地目录、Git 工作树、固定 URL、Zotero、Notion、Confluence、SharePoint 和 S3。同步任务具备持久 checkpoint、租约、取消、指数退避、字节/页数/文档数预算、周期调度和崩溃恢复。任务只有在文件物化、权限映射、原始版本归档和索引 generation 均完成后才进入 `succeeded`。

## 权限边界

所有连接、凭据、来源和任务都按 `tenant_id` 与知识库物理 ID 隔离。API 还会验证当前工作区与知识库资源策略：

- 具备 `read` 的主体可以列出连接、同步任务和连接健康状态；
- 创建、暂停、删除、立即同步连接，取消/重放任务，管理凭据，以及查看包含上游标识和本地来源信息的运维来源目录，均要求 `manage_access`；
- OAuth 回调是供应商浏览器跳转需要的公开路由，但只接受服务端保存、短时、一次性的随机 `state`。租户、知识库、连接和发起人都从服务端会话恢复，查询参数不能改写授权边界；
- 普通读者浏览内容应使用 `/sources` 和 `/sources/{source}/chunks`，不要授予其运维目录权限。

账号模式下，`manage_access` 通常由 owner/admin 持有；最终判定以 `GET /v1/tenant` 返回的 permissions 和知识库实时 ACL 为准。

## 配置 AES-GCM 凭据库

凭据库默认关闭；不配置主密钥时，凭据与 OAuth 接口返回 `503 CREDENTIAL_UNAVAILABLE`，已有 `secret_env` 连接仍可继续同步。生成 32 字节随机主密钥：

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

把输出放进版本化 JSON keyring，并设置当前写入版本：

```dotenv
COGDOC_CREDENTIAL_MASTER_KEYS={"v1":"<base64url-32-byte-key>"}
COGDOC_CREDENTIAL_ACTIVE_KEY_VERSION=v1
```

升级自早期预览配置时也兼容别名 `COGDOC_CONNECTOR_VAULT_KEYS` 与 `COGDOC_CONNECTOR_VAULT_ACTIVE_KEY_ID`。同一含义只设置一组名称，避免部署工具产生冲突值。生产中应由密钥管理系统注入 keyring，不要把真实值提交到仓库、镜像、命令行参数或日志。

每个凭据 revision 使用随机数据加密密钥加密 JSON 载荷，再由 active master key 包装；两层均使用 AES-256-GCM。附加认证数据绑定 credential、tenant、KB、可选 connection、provider 和 kind，因此复制 SQLite 密文到另一授权域无法通过认证。SQLite 只公开 label、provider、scope、secret field 名、key version、有效期和 revision；仅同步/刷新路径能在完整作用域校验后解密，读取动作也写入凭据审计事件。

### 主密钥轮换

轮换时不能直接替换或删除旧 key：

1. 停止配置写入，完成 `data/` 与外部 keyring 的同一恢复点备份。
2. 在 JSON 中同时保留旧、新 key，将 active ID 改为新版本，例如 `{"v1":"...","v2":"..."}` 与 `v2`，然后重启。
3. 新建凭据会使用 `v2`。对每条旧凭据调用 `PATCH .../connector-credentials/{credential_id}`；可以提交新 `secret_values`，也可以只带 `expected_revision`，后者会把现值重新包装到 active key。遇到 `409 CREDENTIAL_REVISION_CONFLICT` 时刷新 metadata 后重试，不能覆盖并发轮换。
4. 确认凭据列表中的 `key_version` 全部为 `v2`，OAuth 短时会话已结束，并对每个连接执行一次成功同步。
5. 只有在旧 key 不再被任何凭据或回滚备份引用、且回滚窗口结束后，才从在线 keyring 删除 `v1`。归档备份仍需要对应旧 key；应按备份保留期保存它。

密钥丢失时密文不可恢复；切换 active ID 本身不会批量重加密历史记录。

## 手工凭据与连接

凭据接口为：

```text
GET    /v1/knowledge-bases/{kb}/connector-credentials
POST   /v1/knowledge-bases/{kb}/connector-credentials
PATCH  /v1/knowledge-bases/{kb}/connector-credentials/{credential_id}
DELETE /v1/knowledge-bases/{kb}/connector-credentials/{credential_id}
GET    /v1/knowledge-bases/{kb}/connector-credentials/audit/events
```

创建一个 Confluence 静态凭据；`secret_values` 只用于这次写入，不会出现在响应中：

```json
{
  "provider": "confluence",
  "credential_kind": "static",
  "label": "团队手册只读 token",
  "secret_values": {"token": "<secret>"}
}
```

各连接器接受的字段为：

| 连接器 | provider | 必需 secret fields | 可选 |
|---|---|---|---|
| `zotero` | `zotero` | `api_key` | — |
| `notion` | `notion` | `token`（OAuth 的 `access_token` 也可） | — |
| `confluence` | `confluence` 或 `atlassian` | `token`（OAuth 的 `access_token` 也可） | — |
| `sharepoint` | `sharepoint` 或 `microsoft` | `token`（OAuth 的 `access_token` 也可） | — |
| `s3` | `s3` 或 `aws` | `access_key`, `secret_key` | `session_token` |

随后在连接创建请求中只传 `credential_id`：

```json
{
  "connector_type": "confluence",
  "name": "团队手册",
  "config": {
    "base_url": "https://team.atlassian.net",
    "include_acl": true,
    "schedule_seconds": 300
  },
  "credential_id": "cred-...",
  "workspace_visible": false
}
```

API 会校验 provider 和 secret field 是否满足连接器契约。已被连接引用的凭据不能删除；轮换和删除可用 `expected_revision` 做乐观并发保护。`GET .../audit/events` 返回 create/use/rotate/delete 的操作者、revision、key version 和时间，不返回密文或明文。

## OAuth：Notion、Atlassian 与 Microsoft

OAuth 依赖已启用的凭据库和 API 的公开 HTTPS origin：

```dotenv
COGDOC_CONNECTOR_OAUTH_PUBLIC_BASE_URL=https://cogdoc.example.com
COGDOC_CONNECTOR_OAUTH_SESSION_TTL_SECONDS=600
COGDOC_CONNECTOR_OAUTH_TIMEOUT_SECONDS=15
COGDOC_CONNECTOR_INDEX_TIMEOUT_SECONDS=30

COGDOC_NOTION_OAUTH_CLIENT_ID=
COGDOC_NOTION_OAUTH_CLIENT_SECRET=
COGDOC_ATLASSIAN_OAUTH_CLIENT_ID=
COGDOC_ATLASSIAN_OAUTH_CLIENT_SECRET=
COGDOC_MICROSOFT_OAUTH_CLIENT_ID=
COGDOC_MICROSOFT_OAUTH_CLIENT_SECRET=
COGDOC_MICROSOFT_OAUTH_TENANT=common
```

只配置需要的 provider。Notion 和 Atlassian 需要 client secret；Microsoft client secret 可留空用于 public client。把供应商应用的回调 URI 精确配置为：

```text
https://cogdoc.example.com/v1/auth/connector-oauth/callback/notion
https://cogdoc.example.com/v1/auth/connector-oauth/callback/atlassian
https://cogdoc.example.com/v1/auth/connector-oauth/callback/microsoft
```

生产 origin 必须是 HTTPS；仅 localhost 开发允许 HTTP。发起授权：

```text
POST /v1/knowledge-bases/{kb}/connector-oauth/authorize
```

请求体的 `provider` 为 `notion`、`atlassian` 或 `microsoft`，可同时传 `connection_id` 与 label。响应中的 `authorization_url` 交给浏览器打开。回调成功后 token 作为 `credential_kind=oauth` 写入凭据库；Atlassian 凭据用于 Confluence，Microsoft 凭据用于 SharePoint。Microsoft 使用 S256 PKCE；所有 provider 都使用高熵、服务端绑定、只可消费一次且默认 600 秒过期的 state 会话，PKCE verifier 自身也作为短时加密凭据保存。

需要刷新时调用：

```text
POST /v1/knowledge-bases/{kb}/connector-credentials/{credential_id}/refresh
```

刷新要求 OAuth 凭据包含 `refresh_token`，并可用 `?expected_revision=<revision>` 防止覆盖并发更新。同步解析凭据时若 access token 已过期或将在 60 秒内过期，也会先执行带 revision 保护的刷新；并发 worker 已完成刷新时会重新读取最新 revision。供应商拒绝授权、state 不匹配/过期/重放或 token 响应异常都会 fail closed；callback query 中的错误不会写入凭据值。

## 环境变量兼容路径

旧连接仍可用 `secret_env`，其值是环境变量名而不是密钥：

```json
{
  "connector_type": "confluence",
  "name": "团队手册",
  "config": {"base_url": "https://team.atlassian.net"},
  "secret_env": {"token": "COGDOC_CONFLUENCE_TOKEN"},
  "workspace_visible": false
}
```

同步开始时服务才解析环境变量；变量缺失会安全失败。常用变量见 `.env.example`：`COGDOC_NOTION_TOKEN`、`COGDOC_CONFLUENCE_TOKEN`、`COGDOC_SHAREPOINT_TOKEN`、`COGDOC_ZOTERO_API_KEY`，以及 S3 可引用的 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_SESSION_TOKEN`。

同一个连接不能同时设置 `credential_id` 与 `secret_env`。建议按连接逐个迁移：先创建 vault 凭据，再创建或更新使用该 `credential_id` 的连接，成功同步后才从运行环境移除旧密钥；回滚窗口内保留受控的旧部署配置。没有要求集中管理/OAuth 的本地部署可以继续只用环境引用。

## 连接配置

连接 API 为：

```text
GET    /v1/knowledge-bases/{kb}/connections
POST   /v1/knowledge-bases/{kb}/connections
PATCH  /v1/knowledge-bases/{kb}/connections/{connection_id}
DELETE /v1/knowledge-bases/{kb}/connections/{connection_id}
POST   /v1/knowledge-bases/{kb}/connections/{connection_id}/sync
```

删除连接采用可重试的两阶段收尾：先持久设置 `disabled + deleting` fence、撤销周期调度并排空尚未提交的同步；若已有任务进入 `committing`，接口返回 409，连接保持 fenced，待该提交完成后重试。随后在短时 KB 写锁内，仅按持久 catalog/manifest/contract 所证明的归属删除该连接的顶层物化文件和 contract/current/work 状态、tombstone catalog，并为旧文档 ACL 写入 durable retirement fence；retiring 文档不能被重新分享或授权。释放 KB 锁和全局凭据引用锁后，构建并等待不再含这些来源的新索引 generation（超时后保留并复用同一持久 job ID，不会盲目重复提交）；成功后再次短时加锁，重验 KB incarnation、实时管理权限、连接 fence 和并发 retirement 状态，再原子删除文档 ACL/fence、同步 checkpoint/health 和连接定义。任一步失败均返回错误并保留 fenced 定义作为重试句柄，不会误删其他连接或人工上传文件；历史文件名只有在多份 ownership ledger 与内容 hash 一致时才兼容清理。catalog 的不可变版本记录和 raw artifact 默认保留，可通过 `include_deleted=true` 继续审计；其物理清理由版本保留与显式 trash 流程负责。

各类型的非密钥配置如下：

| 类型 | 必填 | 可选 |
|---|---|---|
| `local-directory` | `root` | `follow_symlinks`, `schedule_seconds` |
| `git` | `repository` | `ref`, `subpath`, `schedule_seconds` |
| `url` | `urls` | `schedule_seconds` |
| `zotero` | `library_type`, `library_id` | `schedule_seconds` |
| `notion` | — | `schedule_seconds` |
| `confluence` | `base_url` | `include_acl`, `schedule_seconds` |
| `sharepoint` | `site_id`, `drive_id` | `include_acl`, `schedule_seconds` |
| `s3` | `bucket`, `region` | `prefix`, `endpoint`, `schedule_seconds` |

周期范围是 60 秒到 365 天。URL/云连接只允许 HTTPS、显式允许的主机和公网 DNS 结果；TCP 固定到本次验证过的 IP，重定向逐跳重新做主机与 DNS 校验，跨 origin 时移除 Authorization/Cookie，响应大小受限。本地与 Git 连接读取服务主机文件，因此只能把经审计的根目录/仓库交给管理员配置。`COGDOC_LOCAL_CONNECTOR_ALLOWED_ROOTS` 与 `COGDOC_GIT_CONNECTOR_ALLOWED_ROOTS` 是进程级授权：列入任一根目录等于授权给**所有租户的 KB 管理员**选择该目录及子目录，不能把多租户私有数据的共同父目录加入；需要租户专属根时应运行独立实例或在外层实施租户到根目录的强制映射。

## 同步健康、死信与重放

任务与健康接口为：

```text
GET  /v1/knowledge-bases/{kb}/sync-jobs
GET  /v1/knowledge-bases/{kb}/sync-jobs/{job_id}
POST /v1/knowledge-bases/{kb}/sync-jobs/{job_id}/cancel
POST /v1/knowledge-bases/{kb}/sync-jobs/{job_id}/replay
GET  /v1/knowledge-bases/{kb}/connection-health
GET  /v1/knowledge-bases/{kb}/connections/{connection_id}/health
```

任务状态包括 `pending`、`running`、`committing`、`retry_wait`、`succeeded`、`failed`、`dead_letter` 和 `cancelled`。健康快照同时给出最近任务/错误/耗时、连续失败数、下次调度时间与当前 backlog。

可重试错误默认最多尝试 5 次，退避从 5 秒开始指数增长；耗尽后进入 `dead_letter` 并暂停该连接的周期调度。非重试错误直接进入 `failed`。重放只接受死信任务，要求连接仍启用且没有活动任务；它从最近一次成功 checkpoint 新建任务，使用 `replay_of` 指向原任务，绝不会重写原死信记录。修复凭据、ACL、网络或上游配置后再重放，不要把 replay 当成跳过失败原因的强制成功开关。

`GET /metrics` 暴露以下低基数 Prometheus 指标：

- `cogdoc_connector_sync_events_total{connector_type,outcome}`；
- `cogdoc_connector_sync_duration_seconds{connector_type,outcome}`；
- `cogdoc_connector_sync_backlog{connector_type}`；
- `cogdoc_connector_sync_documents_total{connector_type,outcome}`。

指标 label 不包含 tenant、KB、connection 或 job ID，避免高基数。配置 `COGDOC_WEBHOOK_URL` 后，`retry`、`succeeded`、`failed` 和 `dead_letter` 会异步投递 `connector.sync.<outcome>`；payload 包含作用域 ID、连接器类型、attempt、耗时、backlog、有界 counters、错误码和 retry 时间，不含凭据。`COGDOC_WEBHOOK_SECRET` 会原样放入 `X-CogDoc-Webhook-Secret`，接收端应使用 TLS、恒定时间比较并自行做 event ID 幂等；投递失败只记日志，不改变任务终态。

## 来源目录、版本与 ACL

运维来源目录提供连接维度过滤、健康诊断、不可变版本、原始内容下载和差异比较：

```text
GET /v1/knowledge-bases/{kb}/source-catalog
GET /v1/knowledge-bases/{kb}/source-catalog/{source_id}
GET /v1/knowledge-bases/{kb}/source-catalog/{source_id}/versions
GET /v1/knowledge-bases/{kb}/source-catalog/{source_id}/versions/{version_id}/content
GET /v1/knowledge-bases/{kb}/source-catalog/{source_id}/diff?from_version_id=...&to_version_id=...
GET /v1/knowledge-bases/{kb}/source-artifacts/usage
DELETE /v1/knowledge-bases/{kb}/source-artifacts/trash?older_than=<epoch>&limit=100
```

列表支持 `connection_id`、`health_status` 与 `include_deleted`；source health filter 的闭集为 `unknown`、`syncing`、`healthy`、`degraded`、`stale`、`error`。每条来源返回上游 external ID、origin、当前 version/SHA-256、大小、抓取/更新时间、连接和健康状态；若已物化为文档，还会投影 `document_id`、访问策略与 `acl_epoch`。修改权限仍使用 `/documents/{document_id}/access` 和 grants 接口，来源目录不会绕过文档 ACL。

上游完整快照不再包含某来源时，catalog 会把它标成 `deleted_at`/`stale`，默认列表隐藏；`include_deleted=true` 可用于排障。上游再次出现同一稳定来源时，正常同步会重新 upsert。这里的逻辑删除是目录状态，不会暗中把旧版本提升为当前在线内容。

原始 artifact 位于 `COGDOC_DATA_DIR/source-artifacts/`，与当前物化目录和 index generation 分离。下载前重新校验 SHA-256，并返回 `X-CogDoc-Content-SHA256`。文本版本返回 unified diff；二进制版本只返回两端 metadata。diff 输入/输出默认最多处理 256 KiB 和 5000 行，超过时返回 `truncated=true`，不能把截断结果当作完整审计。

默认每来源保留最新 10 个活动原始版本，单文件最多 100 MiB；可分别用 `COGDOC_SOURCE_ARTIFACT_MAX_VERSIONS`（2–100）与 `COGDOC_SOURCE_ARTIFACT_MAX_FILE_MB`（1–2048）调整。每个租户的活动区加 trash 默认还有 512 MiB 硬上限，可用 `COGDOC_SOURCE_ARTIFACT_MAX_TENANT_MB`（1–2048）调整；批量同步会在发布前原子预留容量，因此一个租户不能抢占另一租户已预留的空间。store 另保留 2 GiB 进程级紧急物理上限，trash 同样计入；接近上限时应先备份、验证并执行受控清理。版本目录不可变，相同 ID 但内容或 metadata 不同会冲突失败。

历史原始版本的手工软删除与恢复接口为：

```text
DELETE /v1/knowledge-bases/{kb}/source-catalog/{source_id}/versions/{version_id}/artifact
POST   /v1/knowledge-bases/{kb}/source-artifacts/{recovery_token}/restore
```

当前在线 version 不能删除。删除把完整 artifact 原子移动到 store-local `.trash` 并返回作用域绑定的 `recovery_token`；恢复前会检查 tenant/KB、hash、版本数限制和目标冲突。trash 仍计入磁盘使用，且 catalog 的不可变版本 metadata 不会随 raw artifact 删除；版本列表的 `artifact_available=false` 表示不能下载/diff。

永久清理必须显式调用 `DELETE .../source-artifacts/trash`：`older_than` 是非负 Unix epoch 秒，只删除当前 tenant/KB 下删除时间严格早于该边界的项目，`limit` 范围 1–1000、默认 100。该操作不会返回恢复令牌且不可撤销；先完成停止写入的一致备份、验证恢复演练和业务保留期审批，再分批清理并复查 usage。不要用未来时间配合大 limit 当作日常“清空”操作。

## 外部权限同步

SharePoint 和 Confluence 会随内容抓取外部权限快照。外部用户通过当前工作区成员身份映射为文档授权；未知用户和尚不支持的组不会扩大权限。上游权限不完整、权限接口失败或身份服务失败时，文档进入私有隔离状态，并撤销该连接器此前管理的授权。人工添加的授权不会被连接器撤销或覆盖。

Notion 等不能提供完整 ACL 的接口默认返回不完整权限；在账号鉴权模式下会按 fail-closed 处理。对于受信任且本来面向全工作区的来源，可在连接上显式设置 `workspace_visible=true`。不要通过关闭 `include_acl` 绕过受限来源的权限同步。

## 备份、恢复与版本回滚

连接器控制面的同一恢复点至少包含：

- `data/state.db`：连接、加密凭据 envelope、OAuth 会话、同步任务/checkpoint/health、来源目录和 ACL 状态；
- `data/source-artifacts/`：活动原始版本与 `.trash`；
- `data/kb/`、`data/chroma_db/`、`data/bm25_db/` 和 `data/manifests/`：物化来源与同代索引；
- 独立保管的完整 vault keyring、OAuth client secrets 和环境引用密钥。默认 `make backup` 不包含 `.env`。

先停止 API/worker 和所有写入，再运行 `make backup`；用 `scripts/restore_state.py ... --verify-only` 验证归档。恢复时先把 `data/` 恢复到空目录，再注入能够解密该恢复点全部 `key_version` 的 keyring，最后启动并验证 `/readyz`、凭据 metadata、连接 health、一个 raw version 下载/hash、代表性 ACL 允许/拒绝查询，以及每类连接的一次同步。没有旧 key 时，即使 SQLite 和 artifact 完整也不能恢复 vault 凭据。

应用版本回滚也必须恢复同一时间点的 `state.db`、source artifacts、物化目录和索引 generation；不能只回滚数据库或只切旧镜像。控制面上线后的旧应用不认识 vault connection 时，应保留升级前环境引用配置和密钥直到回滚窗口结束。不要手工编辑 SQLite、复制单个 credential row 或把旧 version 直接覆盖成当前文件。

解析器与来源契约属于索引构建版本的一部分。需要 v7 迁移时执行：

```bash
python scripts/migrate_v7_indexes.py scan
python scripts/migrate_v7_indexes.py run
```

逐库验证 PDF 页码引用、至少一种非 PDF 定位引用、连接权限、版本下载和同步任务后，再执行 `finalize <run_id>`。验收失败时使用 `rollback <run_id>` 回切保留的上一代索引，并恢复同一时间点的完整控制面备份。数据库表和字段只做向前兼容的增量创建；回滚不能依赖“删新列”。
