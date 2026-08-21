# CogDoc 分布式控制面与不可变索引部署

本章描述 `cogdoc[ha]` 提供的 PostgreSQL、分布式任务/调度、不可变索引 generation、S3 兼容对象存储、transactional outbox 和滚动迁移能力。首要不变量是：**任何不完整、损坏、由过期 worker 写出的索引，都不能成为在线 current generation。**

> 当前边界：`cogdoc-ha` 调度、索引发布和 outbox worker 可以横向扩展；主 API 仍包含本地会话、ACL、Chroma/BM25 缓存和文件 mutation 状态，因此仍必须持有单实例锁。不要用 `COGDOC_ALLOW_MULTI=1` 把多个主 API 写实例放到同一数据目录。完成全部 API 状态的 PostgreSQL 化以前，支持的拓扑是一个主 API 写实例加多个 `cogdoc-ha` worker，而不是多个主 API writer。这个限制是 fail-closed 的部署边界，不应通过共享 NFS 绕过。

## 索引不会写坏的提交协议

每次构建有独立 `generation_id`、数据库 fencing token、随机 lease token 和固定 base generation。发布顺序固定如下：

1. worker 在 generation 专属前缀写入所有文件；文件路径、大小和 SHA-256 必须与规范化 manifest 一致。
2. 本地仓库逐文件 `fsync`，再原子 rename generation 目录并 `fsync` 父目录。S3 先上传所有对象，multipart 每个 part 带 SHA-256，最后以 `If-None-Match: *` 写 manifest commit marker。
3. 发布前重新验证 manifest、对象数量、对象大小和 hash metadata。额外对象、缺失对象、半成品 multipart、symlink 或不安全路径全部拒绝。
4. 同一数据库事务锁定 head，检查 lease 未过期、fencing token 仍为最新、current 仍等于构建时冻结的 base、head revision CAS 成功。
5. 事务内切换 `current_generation_id`、把 generation 标为 `published`，同时 append `index.published` outbox 事件。任一步失败会整体回滚。
6. reader 使用 `resolve_current()`，在读取 authority 后验证对象，并在验证完成后再次确认 current 没变化。旧 generation 延迟 GC；GC 查询和删除都排除 current。

因此各类失败结果如下：

| 故障 | 可见结果 |
|---|---|
| 文件/part 只写一半 | 没有 commit marker，不可发布 |
| marker 已写、DB 前崩溃 | current 保持旧代；租约过期后复用原候选继续发布 |
| 旧 worker 晚到 | lease/fencing 拒绝，不能改 head |
| 两实例同时 publish | base + revision CAS 只有一个成功 |
| outbox append 失败 | head 与 generation 状态一并回滚 |
| current 对象缺失、大小或 hash metadata 漂移 | `resolve_current` 拒绝服务该代并使 readiness/worker 告警 |
| GC 与新发布并发 | current 永远不在 collectable 集合内；marker 先删使半清理代立即不可验证 |

## 生产依赖

安装：

```bash
python -m pip install -e '.[ha]'
```

生产多 worker 必须同时满足：

- PostgreSQL，应用账号可使用或创建专属 schema；连接池、statement timeout 和 lock timeout 均有界。
- S3 或兼容实现支持 bucket versioning、`PutObject If-None-Match`、`CompleteMultipartUpload If-None-Match`、multipart SHA-256 和强一致的 HEAD/LIST。
- bucket policy 只允许 CogDoc runtime role 操作配置的 prefix，并要求 TLS、versioning 与 conditional write。禁止其他主体覆盖该 prefix。
- bucket lifecycle 应在保留窗口后回收 noncurrent versions，并清理长时间未完成的 multipart upload。不能在 DB generation 保留期之前删除 current 或候选对象。
- 数据库与 bucket 使用同一地域，时钟由 NTP 同步。lease 正确性依赖数据库记录的 epoch 时间，不接受手工倒拨系统时钟。

基础配置：

```dotenv
COGDOC_HA_ENABLED=true
COGDOC_HA_DATABASE_URL=postgresql://cogdoc:***@postgres.internal:5432/cogdoc
COGDOC_HA_DATABASE_SCHEMA=cogdoc
COGDOC_HA_OBJECT_STORE=s3
COGDOC_HA_S3_BUCKET=cogdoc-production
COGDOC_HA_S3_PREFIX=cluster-a
COGDOC_HA_S3_REGION=ap-southeast-1
COGDOC_HA_S3_REQUIRE_VERSIONING=true
COGDOC_HA_WORKER_ID=worker-a-01
COGDOC_HA_SCHEDULER_ENABLED=true
COGDOC_HA_OUTBOX_ENABLED=true
COGDOC_HA_MAINTENANCE_ENABLED=true
COGDOC_HA_MAINTENANCE_INTERVAL_SECONDS=30
COGDOC_HA_RETENTION_SECONDS=604800
COGDOC_HA_SCRUB_INTERVAL_SECONDS=3600
COGDOC_HA_MAINTENANCE_BATCH_SIZE=100
COGDOC_HA_INDEX_WORKER_ENABLED=true
COGDOC_HA_INDEX_WORKER_COUNT=2
COGDOC_HA_INDEX_WORKER_POLL_SECONDS=0.5
COGDOC_HA_INDEX_WORKER_LEASE_SECONDS=300
COGDOC_HA_RELEASE_ID=2026.08.22
COGDOC_HA_MINIMUM_SCHEMA_VERSION=1
COGDOC_HA_MAXIMUM_SCHEMA_VERSION=1
COGDOC_HA_VERSION_HEARTBEAT_INTERVAL_SECONDS=30
COGDOC_HA_VERSION_HEARTBEAT_TTL_SECONDS=90
COGDOC_HA_INDEX_READS_ENABLED=true
COGDOC_HA_INDEX_REPLICA_CACHE_ROOT=/var/lib/cogdoc/ha-index-cache
```

每个同时在线的进程必须有唯一且稳定的 `COGDOC_HA_WORKER_ID`。发布版本必须显式设置
`COGDOC_HA_RELEASE_ID`；schema 最小/最大版本声明必须与该二进制实际兼容范围一致。
版本心跳会阻止仍有旧进程在线时执行不兼容 contract 迁移，不能通过缩短 TTL 或删除心跳行
替代正常滚动下线。

AWS S3 推荐用 bucket policy 的 `s3:if-none-match` condition 拒绝没有条件头的 `PutObject` 和 `CompleteMultipartUpload`。MinIO/其他兼容服务必须先通过 staging 的 multipart 冲突与 versioning 故障注入，不能只以“API 名称兼容”判定可用。

## 运维命令

```bash
# PostgreSQL、bucket 可达性和 versioning 检查
cogdoc-ha doctor

# 常驻的单线程持久调度器与 outbox dispatcher；可运行多个副本
cogdoc-ha serve

# 单步排查
cogdoc-ha scheduler-once
cogdoc-ha outbox-once

# 先执行 expand/backfill/validate；contract 必须单独确认兼容下限
cogdoc-ha migrate --batch-size 1000 --max-batches 100
cogdoc-ha migrate --allow-contract --minimum-compatible-version 1

# 发布已经构建完成的不可变索引目录
cogdoc-ha publish-index \
  --tenant tenant-a --kb docs --build-id deploy-20260821-01 \
  --directory /srv/cogdoc/build/docs \
  --chunk-version v7 --embedding-model model-name --dimensions 1024

# 先落不可变对象与 prepared generation，再交给分布式 worker 发布
cogdoc-ha enqueue-index \
  --tenant tenant-a --kb docs --build-id deploy-20260821-01 \
  --directory /srv/cogdoc/build/docs \
  --chunk-version v7 --embedding-model model-name --dimensions 1024

# 流式校验 current generation 的每一个对象和完整 SHA-256
cogdoc-ha scrub-index --limit 100

# 以操作员提供的幂等 key 重放一条 dead-letter 作业
cogdoc-ha replay-job --job-id haj-... --replay-key incident-20260822-01

# 只清理超过七天且绝非 current 的旧 generation
cogdoc-ha gc-index --retention-seconds 604800 --limit 100
```

`build-id` 是幂等键。同一个 tenant/KB/build-id 只能对应一份 generation；活动租约属于另一 worker 时调用会失败，租约过期后接管会轮换 token。不要用随机新 build-id 重试同一业务命令，否则会失去端到端幂等性。

主 API 的本地索引提交会在执行任何破坏性 ACL 收尾前，同步导出 portable index、校验
对象并切换 HA authority。镜像失败时本地索引任务会以 `HA_MIRROR_FAILED` 失败，旧 HA
generation 继续服务，待撤销的 ACL 保持私有/retirement fence，不能为了恢复可用性手工清掉
fence。后台 mirror reconciler 会补偿“本地 generation 已提交、HA pointer 尚未切换”的崩溃
窗口。reader 只认数据库 current；下载、完整校验、安装后还会再次确认 head，若发布在验证期
发生变化则丢弃候选并重试，绝不回退到某个本地旧索引。
副本缓存同样不具权威性：发现本地 generation 缺文件、额外文件或 hash 不符时，会在
目标专属的跨进程锁内先原子隔离损坏目录，再从不可变对象重新物化并 `fsync` 后切换；
进程崩溃遗留的临时/隔离目录按目标哈希命名空间回收，不会因短 ID 前缀误删其他 generation。

portable generation 使用严格 SQLite/二进制格式，不包含 pickle：BM25 文本与元数据为规范化
JSON，向量为 little-endian float32，每行有 checksum。安装器先在 generation 专属 collection
安装向量和 BM25，再核对 chunk identity 集，最后原子写 installation marker。部分安装或维度、
embedding contract 不一致不会变成可查询索引。

S3 发布时由请求 checksum、逐 part checksum、不可覆盖 key 和 manifest metadata 共同校验写入；正常读取 `iter_bytes()` 还会重新计算完整 SHA-256。`doctor`/热路径的 HEAD 验证不会每次下载整个大型索引，因此生产还应启用 S3 原生 checksum/完整性能力，并运行低频全量 scrub；不要把 HEAD metadata 当作对存储介质 bit-rot 的完整扫描。

## 任务、调度与 outbox

- `ha_jobs` 采用 `FOR UPDATE SKIP LOCKED` 领取，lease token 是完成/失败/heartbeat 的 fencing capability；过期 token 永远不能提交结果。
- `(queue, tenant, idempotency_key)` 唯一。相同 key 的不同 payload 直接冲突，不会静默复用。
- 重试次数耗尽或最后一次 lease 过期进入 dead letter，不能被普通 claim 绕过。
- 调度器先在数据库中生成唯一 fire ledger，再把 fire 幂等投递为 job；进程在两步之间退出不会漏任务，也不会产生两个业务 job。
- outbox 必须在业务事务内 `append`。dispatcher 是 at-least-once；webhook body、`Idempotency-Key` 和 `X-CogDoc-Event-Id` 使用同一个持久 event ID，接收方必须按它去重。
- 同 topic/aggregate 的事件按 revision 投递。已送达 payload 可清理，但独立 dedup 墓碑继续阻止历史 idempotency key 重新发送。

维护线程会有界地回收过期 lease、清理已送达 fire/outbox、保留幂等墓碑、回收超过保留期且
绝非 current 的 generation，并按低频周期流式 scrub current。作业和事件列表、清理批次都有
硬上限；不要通过直接 SQL 删除 `ha_job_keys` 或 outbox dedup 行，否则历史业务命令可能再次
执行。每个维护阶段和每个 generation 都隔离失败：单个损坏对象不会饿死后续租户的 scrub，
也不会阻断同轮 lease/outbox/fire 回收；本轮仍会聚合报错并保持 readiness 失败。

拥有租户 `manage_access` 权限的管理员可使用以下 HTTP 控制面；所有 mutation 在存储层再次
带 `tenant_id` 条件，不依赖先读后写的路由判断：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/v1/ha/jobs` | 分页查看本租户作业，可按 queue/status 过滤 |
| `GET` | `/v1/ha/jobs/{job_id}` | 查看安全投影，不返回 payload/result/lease token |
| `POST` | `/v1/ha/jobs/{job_id}/cancel` | 请求取消；运行中 worker 仍须在提交前检查 fence |
| `POST` | `/v1/ha/jobs/{job_id}/replay` | 仅重放 dead-letter，`replay_key` 幂等 |
| `GET` | `/v1/ha/schedules` | 分页查看本租户调度，不返回原始 payload |
| `PATCH` | `/v1/ha/schedules/{schedule_id}` | 以 `expected_revision` CAS 启停 |
| `GET` | `/v1/ha/index-generations` | 查看 generation 摘要；物理 KB ID、对象路径和 lease 不返回 |

分页游标必须把响应的 `before_created_at` 与对应 `before_*_id` 一起传回。HA 未启用时这些
端点返回 503；viewer/editor 不会因通用 GET/POST 权限映射而绕过显式 `manage_access` 检查。

Webhook 只接受 HTTPS。配置 secret 时发送 `X-CogDoc-Signature: sha256=<HMAC>`；接收方应对原始请求体计算 HMAC、常量时间比较，并在写入本地幂等表后才返回 2xx。3xx 不会跟随。

## 零停机数据库迁移

迁移固定分四阶段：

1. `expand`：只增加旧版本可忽略的新表/列/索引；DDL 在事务内执行。
2. `backfill`：有界批次和持久 cursor；每批必须幂等，因为进程可在业务写成功、cursor 更新前退出。
3. `validate`：完整性检查不通过则停止，不能进入 contract。
4. `contract`：显式传入 `minimum_compatible_version`，确认最老在线应用已理解新 schema 后才删除旧结构。

迁移版本、名称与 SHA-256 checksum 一经注册不可改。PostgreSQL runner 同时持 session advisory lock 和可观测 lease 行；SQLite 本地模式使用 `BEGIN IMMEDIATE` 与 lease。发布顺序应为：先部署兼容 expand 的版本 → 完成 backfill/validate → 滚动升级全部 worker → 核对最老版本 → 单独执行 contract。禁止把 expand 与破坏性 contract 放在同一次自动启动中。

## 监控、备份与恢复

`/health/ready` 在启用 HA runtime 时增加 required 的 `ha_control_plane`，会检查数据库和对象存储。`cogdoc-ha doctor` 额外报告 backend、object store 与 `multi_instance_safe` 判定。

至少告警：

- running/delivering lease 过期率、retry/dead-letter 数量；
- schedule pending fire 年龄；
- outbox 最老 pending 年龄和投递失败率；
- building/prepared generation 年龄、fencing 冲突、publish CAS 冲突；
- S3 incomplete multipart 数量、noncurrent version bytes、4xx/5xx；
- PostgreSQL pool wait、statement timeout、deadlock/serialization retry；
- current generation 验证失败（最高优先级，停止切流）。

`/metrics` 额外导出固定低基数的 HA 指标：`cogdoc_ha_snapshot_up`、
`cogdoc_ha_jobs{status}`、`cogdoc_ha_outbox{status}`、
`cogdoc_ha_generations{status}`、`cogdoc_ha_expired_job_leases`、
`cogdoc_ha_enabled_schedules`、`cogdoc_ha_due_schedules`、
`cogdoc_ha_current_generations`、`cogdoc_ha_live_instances` 与
`cogdoc_ha_maintenance_failures`。标签不会包含 tenant、KB、queue、job 或 generation ID，
避免基数和身份泄漏。`cogdoc_ha_snapshot_up=0` 表示本次抓取失败，不能继续使用上一份快照
冒充健康。

`/health/ready` 同时要求 HA 数据库、对象存储、scheduler、outbox、maintenance、index
worker、版本心跳与 API mirror 健康。探测在有界线程池中 single-flight 并短暂缓存，不在
ASGI 事件循环里做对象 hash 或文件系统写入。readiness 失败时停止新流量，但保留进程供
正在持 lease 的操作完成或被 fencing 接管。

备份必须在同一恢复点保留 PostgreSQL snapshot 和 S3 versioned objects。只恢复数据库而没有对应 object versions，会使 current 验证失败；只恢复 bucket 不恢复数据库，不会自动选择某个 generation。恢复演练应：恢复 DB → 恢复/保留所引用的 object versions → `cogdoc-ha doctor` → 对每个 current 执行 manifest 验证 → 再开放 reader。不要通过手改 `ha_index_heads.current_generation_id` 修复；应重新构建或使用经过审计的发布流程。

## 上线清单

1. staging 运行全部 `tests/test_ha_*.py`，并设置 `COGDOC_TEST_POSTGRES_DSN` 运行真实 PostgreSQL `SKIP LOCKED`、schema bootstrap、publish + outbox 回滚用例。
2. 验证 bucket versioning、conditional multipart、拒绝覆盖、incomplete upload lifecycle。
3. 运行旧 worker 晚到、marker 后崩溃、outbox 回滚、八调度实例并发、current 损坏和 GC 故障注入。
4. 先启动一个 `cogdoc-ha serve`，观察一个完整调度和 outbox 周期，再扩容 worker。
5. 保持主 API 单 writer；确认 `COGDOC_ALLOW_MULTI` 未被用于绕过其本地状态边界。
6. 完成 PostgreSQL + S3 同恢复点备份与恢复演练后才宣布生产就绪。
