# CogDoc 分布式控制面与不可变索引部署

本章描述 `cogdoc[ha]` 提供的 PostgreSQL、分布式任务/调度、不可变索引 generation、S3 兼容对象存储、transactional outbox 和滚动迁移能力。首要不变量是：**任何不完整、损坏、由过期 worker 写出的索引，都不能成为在线 current generation。**

> 当前边界：`cogdoc-ha` API worker 可横向扩展文档、connector、账号身份与资源授权、Chat/摘要/对比/检索、反馈闭环，以及 Research 研究任务控制面。KB identity/lifecycle/epoch、账号/登录会话、聊天记忆与执行租约、OIDC/SCIM/service account、资源 ACL/外部 ACL checkpoint、连接、同步 job/health/schedule、加密凭据/OAuth state、Research job/dispatch、反馈/分析/检索调优/评测草稿/派生知识 ledger、source/index head 均使用 PostgreSQL；来源、提交快照、核心索引代和派生知识索引代使用 versioned S3。HA Chat 可读取共享检索调优与经过摘要/epoch 校验的派生知识索引；HA Research 仍显式禁用这些辅助通道，直到研究 attempt provenance 同时冻结它们的 revision。禁止用共享 NFS 或单独设置 `COGDOC_ALLOW_MULTI=1` 绕过此边界。

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
COGDOC_HA_RESEARCH_WORKER_POLL_SECONDS=0.5
COGDOC_HA_RESEARCH_WORKER_LEASE_SECONDS=120
COGDOC_HA_RELEASE_ID=2026.08.22
COGDOC_HA_MINIMUM_SCHEMA_VERSION=1
COGDOC_HA_MAXIMUM_SCHEMA_VERSION=9
COGDOC_HA_IDENTITY_CONFIG_VERSION=1
COGDOC_HA_VERSION_HEARTBEAT_INTERVAL_SECONDS=30
COGDOC_HA_VERSION_HEARTBEAT_TTL_SECONDS=90
COGDOC_HA_INDEX_READS_ENABLED=true
COGDOC_HA_INDEX_REPLICA_CACHE_ROOT=/var/lib/cogdoc/ha-index-cache
COGDOC_HA_CHAT_SESSION_LEASE_SECONDS=300
COGDOC_HA_CHAT_INDEX_READER_LEASE_SECONDS=600
COGDOC_HA_CHAT_MAX_SESSIONS_PER_SCOPE=1024
COGDOC_HA_CHAT_SESSION_TTL_SECONDS=604800
COGDOC_HA_CHAT_MAX_DISPLAY_MESSAGES=2000
COGDOC_HA_CHAT_MAX_SESSION_BYTES=4194304
COGDOC_HA_API_MULTI_WRITER_ENABLED=true
COGDOC_HA_MUTATION_LEASE_SECONDS=300
COGDOC_HA_SOURCE_CACHE_ROOT=/var/lib/cogdoc/ha-source-cache
COGDOC_HA_SOURCE_MAX_FILES=100000
COGDOC_HA_SOURCE_MAX_TOTAL_BYTES=10737418240
COGDOC_HA_SOURCE_ARTIFACT_MAX_TOTAL_BYTES=10737418240
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

多 writer 模式下，每条 KB mutation 还持有 PostgreSQL lease token、单调 fencing token 与
冻结的 KB epoch。来源目录先写入 generation 专属对象前缀，所有文件完成后再写 canonical
manifest 和 `COMMITTED` marker；HA portable index 也完成对象校验后，source head 与 index
head 才在同一个 PostgreSQL 事务内一起 CAS 切换并追加 outbox。旧节点即使在超时后完成构建，
也会在本地 `switch_active` 前的最后一道 fence 被拒绝；source/index 任一 hook 或 outbox 失败
都会回滚两个 head。API 节点不共享本地来源或索引目录，它们只是可丢弃 cache。

当前 `COGDOC_HA_API_MULTI_WRITER_ENABLED=1` 是严格的“文档、connector、身份/ACL、Chat、Research 与索引 writer”节点角色，不是把
仍使用本地 SQLite 的整套 API 直接横向扩容。节点只开放健康/指标、HA 控制面、KB 创建与读取、
文档读写、索引任务读取，以及共享来源 catalog/raw artifact 的查询、下载、软删、恢复与
purge，并开放连接/凭据/OAuth/sync job/health 控制面；这些来源 mutation 会取得同一 PostgreSQL
KB fencing lease。连接定义、周期调度、同步 checkpoint/health、credential envelope/audit、OAuth
one-shot state 与 callback binding journal 均在 PostgreSQL 中跨节点 CAS。凭据引用更新另持有带
heartbeat 的集群 lease，避免一个节点绑定连接时另一节点删除同一 credential。

账号、登录 session、邀请、workspace membership、OIDC identity/policy、SCIM directory、service
account/token/session policy 与资源 ACL epoch/tombstone/retirement fence 同样使用 PostgreSQL。session
撤销、成员 incarnation tombstone 与 ACL epoch 在下一请求立即跨节点可见；删除成员时先提交全 workspace
grant 撤销和 membership tombstone，再删除 membership，因此任一步崩溃都只会少授权，不会让旧 grant
在重新邀请后复活。OIDC browser flow 保持一次性 CAS，所有节点还会注册 flow encryption key 指纹；同一
集群出现不同 key 会启动失败。外部 provider ACL checkpoint 也在共享库中，避免另一 connector worker
用旧 checkpoint 跳过撤权。
身份安全参数、OIDC trust contract 与 SCIM token fingerprint 另有集群一致性指纹。普通滚动发布保持
`COGDOC_HA_IDENTITY_CONFIG_VERSION` 不变；确实要变更这些配置时只递增 1，新版本首次启动会 CAS
推进指纹，此后旧配置节点无法重新加入。禁止跳版本或复用同一版本号承载不同配置。
版本推进后，仍持旧指纹的在线节点会立即在 readiness 中变为 `not_ready`，必须让负载均衡停止
向它发送新请求并完成滚动下线。

Research job 的完整 JSON 状态、紧凑列表投影和 execution/revision CAS 存在 PostgreSQL；
`ha_research_dispatches` 只负责为同一 durable attempt 选举一个执行节点。领取使用
`FOR UPDATE SKIP LOCKED` 与带过期时间的随机 lease token，节点接管时还会在 research job
内部轮换 execution lease，并把旧 worker 已领取但未完成的 section 恢复为 pending。旧节点
晚到后不能提交 evidence、失败状态或 report。节点关闭会先停止领取并释放自己持有的 dispatch；
进程崩溃则由 lease expiry 接管。启动和低频 reconciliation 都会分页补投 active job，避免
“job 状态已提交、dispatch enqueue 前崩溃”造成永久孤儿；超过 HA retention 的终态 dispatch
由有界批次清理，Research 正文与审计记录不会随 queue envelope 一起删除。

每次 evidence attempt 会冻结当前 KB epoch、ACL epoch、HA index generation/build contract 与
source version 集。写入 evidence/report/review/publication 前，同一 PostgreSQL 事务锁定 KB
incarnation、index head、ACL epoch，以及用户 session/membership（若适用），再写 research row。
索引在检索期间发布新代、KB 被删除重建、ACL 收窄、session 撤销或 membership incarnation
变化，都会使本次提交 fail-closed；不会把两个索引代的证据拼进一份报告。Research worker 只读
已校验的 HA current portable index。派生知识与 retrieval feedback/tuning 已迁入共享 authority，
但 HA Research 当前仍明确禁用这些辅助通道：研究 attempt 的 provenance 尚未冻结派生索引 generation
和调优 revision，不能让长任务混用两个辅助状态版本。HA Chat 的请求边界会逐次验证这些共享 revision，
因此可安全启用；单机模式保持原行为。

Chat 的短期/中期/长期记忆、展示历史、turn 幂等键与 session execution lease 同样存入
PostgreSQL。相同 session 在集群内一次只允许一个活跃执行者；节点失联后必须等租约过期接管，
旧执行者的晚到答案会被 capability token 拒绝。每次 `/chat`、流式 Chat、`/summary`、
`/compare` 与 `/retrieve` 都先取得一个 index reader lease，并在整个执行期间固定同一不可变
generation；即使 head 中途切换也不会把两个代的证据混入同一答案。LangGraph 换工作线程却未
传播执行上下文时，provider 只在唯一活跃 generation 可确定时继续，否则 fail-closed。

最终写入聊天记忆的数据库事务会同时锁定并复核 KB lifecycle/epoch、ACL epoch，以及用户登录
session + membership incarnation 或 service-account token。删库、撤权、降权、session/token
撤销和同 slug 重建后，旧节点均不能向新 incarnation 写入历史。展示记录保存回答所用的
`index_generation_id`，便于审计。终态 session 受 TTL、每 scope 数量、单 session 字节与展示消息
上限约束；后台维护有界清理过期 session、execution lease 与 index reader lease。

反馈、分析、retrieval feedback/tuning、评测草稿和派生知识使用共享 append-only/CAS ledger；每个
mutation 在 PostgreSQL 写事务内复核 KB epoch、登录或 service token、ACL epoch 和所需权限。知识
review/revise/delete 还冻结最新 event sequence，防止路由检查后条目被并发改绑到另一个文档。派生知识
索引使用独立、epoch-scoped 的 immutable generation 和对象前缀，不会改写核心文档 index head。
每次 ledger mutation 在同一事务写 durable refresh outbox；跨节点租约去重，进程重启和周期维护会
恢复漏掉的刷新。读路径在查询前后比较当前 approved snapshot digest、KB epoch 与 generation，任何
stale/missing 状态都丢弃向量结果并回退共享词法 ledger，绝不返回旧 incarnation 的缓存。

同步抓取完成后，私有 connection snapshot 会在 `connector_sync_jobs` 进入 `committing` 前写入
versioned S3，并把 canonical manifest/hash/phase/index job ID 写入 PostgreSQL。worker 进程损失后，
另一节点从该证据逐文件校验恢复 staging，幂等重放 catalog/artifact/ACL/materialization；索引线程
继承而不是重新获取同步任务持有的 KB lease。新 source generation 与 portable index generation
只有在相同 KB epoch/fencing token 下才会同事务推进，任一对象、head CAS 或 outbox 失败都保持旧
source/index head。`local-directory` 与本地 `git` 连接在多 writer 角色中 fail-closed 禁用，避免节点
本地目录差异生成非确定快照；应改用 S3、HTTPS URL 或 SaaS connector。

KB 删除使用可恢复的
`fenced → cleaned → deleted` saga：先在 PostgreSQL 中提升 KB epoch 并冻结 source/index head，
再清理共享 catalog/raw artifact，最后在同一事务撤销两个 current head、写 KB tombstone 与
`kb.deleted` outbox。任何中途失败都保持 `deleting`，启动恢复会继续执行；同 slug 重建只有在
旧 saga 完成后才以更高 epoch 原子激活，旧 generation 只进入 retention GC，绝不会直接成为新
实例的 current。删除 saga 也清理共享反馈、分析、检索调优、评测草稿、派生知识与其待刷新任务；
后台 connector recovery 会在每个节点启动，但共享 active-job CAS、job lease 与 KB lease 保证只有一个 worker
取得发布权。不要通过反向代理绕过该保护。现有 tenant quota
在该角色会自动切到 PostgreSQL 权威 ledger：按 tenant 行锁串行 admission，实际文档/字节用量
来自 current source-generation head，in-flight reservation 由节点 heartbeat 续租，节点崩溃后
才会过期释放。因此 `COGDOC_TENANT_MAX_KNOWLEDGE_BASES`、`COGDOC_TENANT_MAX_DOCUMENTS`、
`COGDOC_TENANT_MAX_STORAGE_MB` 是集群硬限制，而不是每个 pod 各自一份额度。

来源 catalog 与 raw artifact 的权威元数据也已迁入 PostgreSQL。raw bytes 只写入内容寻址、
不可覆盖的对象 key；下载会交叉校验数据库 identity、对象版本、大小与完整 SHA-256。同步在
进入 `committing` 前取得集群级 batch reservation，物理总量、tenant 总量和每来源版本槽均
包含其他节点的在途写入；reservation 由 owner heartbeat 续租。软删只改变 PostgreSQL lifecycle，
恢复仍重新验证对象；显式 purge 才删除对象。上传后数据库提交前崩溃产生的不可见对象由 HA
maintenance 在确认不存在 active row 和 upload intent 后有界回收。

所有 API 节点必须配置相同的 `COGDOC_CREDENTIAL_MASTER_KEYS`。启动会在 PostgreSQL 登记每个
key version 的不可逆 fingerprint：同名不同 key 立即拒绝；数据库中仍有 credential 使用而本节点
缺少的版本也会拒绝。轮换采用两阶段发布：先把新 version 加到所有节点但保持旧 active version，
确认 readiness 全绿后再统一切换 active version；旧 version 只能在审计确认已无引用后移除。密钥
本身、明文 token、OAuth verifier 均不写入共享数据库日志或对象 manifest。

在线备份会拒绝存在 `ha_connector_commits` 的快照：该行表示同步正跨越 source/index authority
边界，其私有对象会在终态立即回收，不具备已发布 generation 的保留期。等待该次同步成功、失败
并完成 cleanup 后重试备份；不要直接删除 commit 行或对象，否则恢复任务会永久失去 staging。

`index.published` 与 `kb.source-generation.published` 除常规竞争消费外，还有每个 API 节点独立
的持久 invalidation cursor；所以每个节点都会清除自己的 portable engine/retriever cache。
handler 失败不会推进 cursor，重启会从 `(created_at,event_id)` 继续，不能把普通 outbox 单消费者
当成广播总线。

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
worker、Research job store/dispatch/dispatcher、版本心跳与 API mirror 健康。探测在有界线程池中 single-flight 并短暂缓存，不在
ASGI 事件循环里做对象 hash 或文件系统写入。readiness 失败时停止新流量，但保留进程供
正在持 lease 的操作完成或被 fencing 接管。

备份必须在同一恢复点保留 PostgreSQL snapshot 和 S3 versioned objects。只恢复数据库而没有对应 object versions，会使 current 验证失败；只恢复 bucket 不恢复数据库，不会自动选择某个 generation。`recovery-manifest` 不会替你运行 `pg_dump`，它只把已经完成的数据库备份标识、可选 dump 哈希与当前数据库 authority 引用的对象清单绑定起来。存在任何未完成 KB 删除 saga 时，清单和完整 backup 都会拒绝生成，避免恢复出“raw 已删但 head 尚未撤销”或相反的混合状态。标准顺序是：

推荐使用完整备份命令。它在一个保持打开的 PostgreSQL `REPEATABLE READ READ ONLY`
事务中调用 `pg_export_snapshot()`，让 `pg_dump --snapshot` 与 authority 清单读取同一个数据库
快照；DSN 只通过子进程环境传递，不出现在命令行参数中。dump、恢复清单与 bundle metadata
全部写入私有 staging 目录并 fsync，所有校验成功后才通过一次目录 rename 对外发布：

```bash
cogdoc-ha backup \
  --output-dir /secure-backups/cogdoc \
  --name recovery-20260822T120000Z \
  --timeout-seconds 7200
```

产物目录固定包含 `database.dump`、`recovery-manifest.json`、`bundle.json`。命令要求
PostgreSQL HA backend 与可执行的 `pg_dump`，默认逐对象流式校验内容；只有在另有独立 scrub
证据时才可显式使用 `--skip-content-verification`。DSN 所指用户必须有读取目标 schema 所有
HA 表及导出 snapshot 的权限。

若数据库 dump 由外部备份平台创建，也可使用拆分流程：

1. 确认 migration 1–6 都处于 `validated` 或 `contracted`，并暂停会推进 current head 或清理 noncurrent object version 的维护窗口。
2. 生成 PostgreSQL snapshot/dump，保存不可复用的备份 ID，并计算 dump 的 SHA-256。
3. 在对应 authority 仍有效、S3 lifecycle 尚未回收 noncurrent versions 时捕获恢复清单；生产备份应启用内容校验：

   ```bash
   cogdoc-ha recovery-manifest \
     --database-snapshot-id pgdump-20260822T120000Z \
     --database-sha256 "$PG_DUMP_SHA256" \
     --output /secure-backups/cogdoc/recovery-20260822.json \
     --verify-content
   ```

4. 将数据库备份、恢复清单和 S3 VersionId 保留策略作为一个恢复点归档。清单采用原子写入并包含自身 SHA-256，但仍应由备份系统做不可变保留、访问控制与异地复制。
5. 演练时先恢复 DB，再恢复或保留清单中引用的精确 object versions，然后运行：

   ```bash
   cogdoc-ha doctor
   cogdoc-ha verify-recovery-manifest \
     --manifest /secure-backups/cogdoc/recovery-20260822.json \
     --verify-content
   ```

完整 bundle 则直接执行：

```bash
cogdoc-ha verify-backup \
  --path /secure-backups/cogdoc/recovery-20260822T120000Z
```

完整 bundle 只携带数据库 dump 与对象 inventory，不复制 S3 payload；必须由 bucket versioning、
Object Lock/不可变备份或跨区域复制保留 inventory 中的精确 VersionId。只有输出
`status=verified` 后才能开放 reader。校验会拒绝 dump hash/size 漂移、bundle/manifest 校验和
错误、schema 不匹配、schema migration 未验证、current head 非 published/active、对象缺失、
hash/size/VersionId 漂移、raw artifact content-address 不一致。不要通过手改
`ha_index_heads.current_generation_id` 修复；应恢复对应对象版本、重新构建，或使用经过审计
的发布流程。

数据库时间点恢复也会把身份与撤权状态回退到该时间点。灾备切流前必须按事故时间窗批量撤销
恢复出的 login session、invite、service token 与未完成 OIDC flow，并轮换静态 API/SCIM token、
connector credential 及可能受影响的用户密码；随后从权威 IdP/SCIM 重放 membership 并核对 ACL
epoch。不能把“恢复清单对象全部通过 hash”误解成“备份之后发生的凭据撤销仍然存在”。

## 上线清单

1. staging 运行全部 `tests/test_ha_*.py`，并设置 `COGDOC_TEST_POSTGRES_DSN` 运行真实 PostgreSQL `SKIP LOCKED`、schema bootstrap、publish + outbox 回滚用例。
2. 验证 bucket versioning、conditional multipart、拒绝覆盖、incomplete upload lifecycle。
3. 运行旧 worker 晚到、marker 后崩溃、outbox 回滚、八调度实例并发、current 损坏和 GC 故障注入。
4. 先启动一个 `cogdoc-ha serve`，观察一个完整调度和 outbox 周期，再扩容 worker。
5. 对所有 API pod 核对相同的 identity config version/fingerprint；确认 Chat 跨节点历史、session 并发拒绝、固定 index generation、ACL/session 撤销拒写，以及 Research 跨节点创建、领取、租约过期接管均通过；未迁移的本地状态端点仍返回 503，且 `COGDOC_ALLOW_MULTI` 未被用于绕过边界。
6. 完成 PostgreSQL + S3 同恢复点备份与恢复演练后才宣布生产就绪。
