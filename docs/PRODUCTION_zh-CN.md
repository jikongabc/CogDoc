# CogDoc 备份与恢复说明

本文档记录本地知识库状态如何备份、恢复，以及哪些索引变化需要重建。

## 备份与恢复

需要备份的状态：

- `data/kb/`：知识库 registry、物化来源文件、generation state、入库 journal。
- `data/chroma_db/`：向量集合。
- `data/bm25_db/`：BM25 registry 与 native index bytes。
- `data/manifests/`：manifest 与索引契约快照。
- `data/source-artifacts/`：连接器来源的不可变原始版本，以及可恢复的 `.trash`。
- `data/state.db`：sessions、index jobs、连接/同步 checkpoint 与 health、AES-GCM 凭据 envelope、OAuth 会话、来源目录、审计导出作业账本，以及开启账号鉴权后的用户、带盐密码哈希、工作区、成员关系、登录会话/邀请摘要和资源 ACL。
- `data/audit/events.jsonl` 与 `data/audit/exports/`：租户哈希链原始日志和仍在保留期内的合规导出制品；两者必须与 `state.db` 取同一静默恢复点。格式、校验与外部 WORM 归档建议见[审计导出指南](AUDIT_EXPORTS_zh-CN.md)。
- `data/feedback/`：feedback 与 bad cases。
- `logs/traces/`：请求 trace，可按保留策略裁剪。
- 数据目录外的密钥材料：完整 vault keyring、连接器/OIDC client secret、OIDC flow key、SCIM Bearer、API key 与被 `secret_env` 引用的上游密钥。它们应由密钥管理系统单独备份和恢复。

恢复顺序：

1. 停止 API/前端进程。
2. 恢复 `data/` 目录和需要保留的 `logs/traces/`，但不要立即接入流量。
3. 从外部密钥系统注入该恢复点需要的完整 vault keyring、连接器/OIDC client secret、OIDC flow key、SCIM Bearer、API key 和连接器环境密钥。keyring 必须包含 `state.db` 中所有 credential `key_version` 对应的旧 key；OIDC flow key 必须能解密该恢复点仍保留的短时登录 flow。
4. 运行 `make check` 确认 native extension 符号匹配。
5. 运行 `make smoke-api` 验证 API 骨架可用。
6. 启动服务后检查 `/readyz` 与 `/v1/auth/config`；若已开启鉴权，先登录，再检查 `/v1/knowledge-bases`、目标 KB 的 sources/chunks、connection health、凭据 metadata、一个原始版本的下载 SHA-256，以及代表性的 ACL 允许/拒绝查询。切流前对每类生产连接至少完成一次同步。

没有做过恢复演练的备份不能视为有效备份。每次索引格式或 chunk identity 变化后，都应执行一次小规模 restore drill。

本地备份命令：

```bash
make backup
```

默认会把 `data/` 和 `logs/traces/` 打成 `backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz`，**不会**包含 `.env`。归档内版本化的 `backup_manifest.json` 会记录每个文件的相对路径、字节数、SHA-256、备份创建时间，以及不含密钥的源根目录配置元数据。为保持兼容，备份命令默认仍输出人类可读文本；自动化场景显式传入 `--json` 才输出单个 JSON 对象。

`v2` 归档执行逐文件完整性校验。恢复工具也兼容旧 `v1` 归档，并检查安全路径、成员类型、声明根目录、汇总大小及已有的顶层文件哈希。由于 `v1` 没有目录内逐文件哈希，结果会明确标记为 `verification_level: "degraded"` 并包含警告，不能将其视为全部恢复内容的密码学完整性证明。

如需同时备份 `.env`：

```bash
python scripts/backup_state.py --include-env
```

`.env` 可能包含 API key、vault 主密钥、OAuth client secret 和上游 token，只应保存到受控、加密且访问可审计的位置，不要提交或共享；优先从密钥管理系统独立恢复密钥。只有数据归档而没有对应 vault keyring 的备份无法解密连接凭据。

仅校验归档、不修改运行状态：

```bash
python scripts/restore_state.py backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz --verify-only
```

先恢复到空的演练目录，再检查其中的 `data/` 与 trace 根目录：

```bash
mkdir -p /srv/cogdoc-restore-drill
python scripts/restore_state.py backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz \
  --target /srv/cogdoc-restore-drill
```

原地恢复前必须停止所有写入进程，再使用 `--target . --force`。非空目标在没有 `--force` 时会被拒绝；强制恢复只替换归档声明的顶层路径，不影响项目中的其他文件。恢复程序会拒绝路径穿越和非普通成员，先在目标同级临时目录解包并全量校验 manifest，成功后才以原子移动提交；提交失败会回滚原路径。

### Docker 备份与离线恢复

镜像以非 root 的 `cogdoc` 用户运行，并设置 `COGDOC_BACKUP_DIR=/app/data/backups`，因此默认备份目录可写且随数据卷持久化。备份会主动排除这个输出子树，连续执行不会把旧归档递归打进新归档。以下示例使用命名卷；为了得到静默备份，先停 API，再用同一镜像执行一次性 helper：

```bash
docker stop cogdoc-api
docker run --rm \
  --mount type=volume,src=cogdoc-data,dst=/app/data \
  cogdoc:0.1.0 \
  python /app/scripts/backup_state.py --json
docker run --rm \
  --mount type=volume,src=cogdoc-data,dst=/app/data,readonly \
  cogdoc:0.1.0 \
  python /app/scripts/restore_state.py \
    /app/data/backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz --verify-only
```

禁止覆盖仍在使用的挂载卷。恢复时创建全新的目标卷，以 root helper 在容器临时目录完成完整校验与恢复，再复制已验证的 `data/` 并把所有权交还运行用户。下面的目标为空检查不能删除；每次演练或恢复都使用新的卷名：

```bash
docker volume create cogdoc-data-restored
docker run --rm --user 0 \
  --mount type=volume,src=cogdoc-data,dst=/source,readonly \
  --mount type=volume,src=cogdoc-data-restored,dst=/restored \
  --entrypoint /bin/bash cogdoc:0.1.0 -lc '
    set -euo pipefail
    test -z "$(find /restored -mindepth 1 -print -quit)"
    python /app/scripts/restore_state.py \
      /source/backups/cogdoc-backup-YYYYMMDD-HHMMSS.tar.gz \
      --target /tmp/cogdoc-restore
    cp -a /tmp/cogdoc-restore/data/. /restored/
    chown -R cogdoc:cogdoc /restored
  '
```

用 `cogdoc-data-restored` 启动临时 API，等待镜像的 `/readyz` 健康检查通过，再验证账号登录、KB/source 数量以及代表性的授权/拒绝检索，确认后才能切流量；回滚窗口结束前保留旧卷。bind mount 部署也应在同一文件系统使用“新目标再切换”的模式；不要在普通非 root 应用容器里执行 `restore_state.py --target /app --force`。

每个发布版本以及每次索引契约变化后至少执行一次恢复演练，并记录归档大小、校验耗时、恢复耗时、`/readyz`、KB/source 数量和代表性检索结果。本地归档是文件级崩溃一致副本，不是跨存储协调快照；要求零丢失恢复点时必须先停止写入。因此可实现的 RPO 等于距离最近一次已完成、静默备份的时间，之后的变更无法恢复。RTO 包括归档传输、全量 SHA-256 校验、解包、native/index 兼容性检查，以及必要时的索引重建；大型 Chroma/BM25 状态通常主导恢复时间。只有使用生产规模数据完成演练后，才能承诺具体 RPO/RTO。

## 账号鉴权与 ACL 上线

`COGDOC_ACCOUNT_AUTH_ENABLED=false` 会有意保持旧版本地/API key 行为；个人或团队生产部署应显式开启。模块级服务随后会在 `COGDOC_DATA_DIR/state.db` 中创建身份与资源 ACL 表，已配置的静态 API 主体仍可用于服务自动化。真人登录 token 和邀请 token 只返回给调用者，数据库仅保存 SHA-256 摘要；密码使用带随机盐和版本的 scrypt 哈希，最少 12 个字符，并带有界失败锁定以及可配置的登录会话/邀请 TTL。

全新部署按以下顺序上线：

1. 把服务置于 TLS 后方，停止全部写入进程并创建已验证备份。
2. 设置 `COGDOC_ACCOUNT_AUTH_ENABLED=true`，初始化阶段暂时保留 `COGDOC_SELF_REGISTRATION_ENABLED=true`。
3. 启动 API，确认 `GET /v1/auth/config`，注册首位 owner，把返回的 Bearer token 放进密钥存储；禁止放入 URL、命令历史、应用日志或 Trace。
4. 创建所需工作区，以最小权限角色邀请成员，通过独立受保护通道交付每个一次性邀请 token，并撤销未使用邀请。
5. 企业部署确认 bootstrap owner 可正常使用后，设置 `COGDOC_SELF_REGISTRATION_ENABLED=false` 并重启。必须保留至少一条验证过的 owner 恢复路径；尚无 owner 时先关注册会导致账号初始化锁死。
6. 对外开放前，实测登录、工作区切换、移除成员、撤销会话、知识库/文档 ACL、一次被拒查询和一次获准 Research。

已有静态主体的部署必须分阶段迁移，不能假设主体配置会自动变成真人用户。系统不会把 `COGDOC_API_PRINCIPALS` 自动转换为密码账号，新注册账号的工作区 ID 也是新生成的。改开关前先导出全部 tenant/KB ID 清单，并在迁移期间保留旧服务主体；ACL 开始执行后，无策略的 KB 会按设计从普通列表中消失。每个已知旧知识库都必须由同租户 owner/admin 服务 key 调用 `PATCH /v1/knowledge-bases/{kb}/access` 初始化，否则缺失 ACL 记录会对管理员也拒绝访问。先用 `GET /v1/knowledge-bases/{kb}/documents` 获取稳定 `document_id`，再增加逐文档策略或 grant。若要把旧库迁入新生成的账号工作区，应在目标工作区建库，并通过受支持 API/CLI 重新入库原始 PDF；禁止直接在 SQLite 中改写 registry 或 ACL 的 tenant ID。确认账号工作区、策略、成员、配额、Trace 与 Research 产物都正确后，才移除旧 key。

本版本新增 `document_id = source-name-v1` 元数据，并把 chunk 身份契约提升为 `source_sha256_name_page_span_local_v6_document_acl_parent_child_section_index_cs600_ov60_min30_ctx160`。这属于强制重建变化：ACL 上线完成前，必须通过正常入库流程重建所有受影响 PDF 的向量/BM25 generation。`SUBSET` 权限会在向量和 BM25 的 top-k 选择前下推，并在融合后再次过滤；后台 Research 会持久化创建者和冻结 allowlist，在召回前后复验当前成员/ACL，权限被撤销或后端无法执行子集过滤时 fail-closed。

本地身份实现面向共享同一受保护数据目录的单个可写 CogDoc 服务实例。不能把各自持有独立 `state.db` 的实例直接放到负载均衡器后，并期待会话、邀请、成员或 ACL epoch 自动收敛。严格限制文件权限并加密备份，因为 `state.db` 含密码哈希、OIDC 身份映射/加密在途 flow、SCIM 用户/组/revision 和活动 token 摘要；获得仍有效的原始 Bearer 或邀请 token 依然足以执行对应操作。本版本提供单 provider 企业 OIDC 登录、显式账号绑定与工作区级 SCIM 目录预配，完整上线、密钥轮换和故障排查见 [OIDC](OIDC_zh-CN.md) 与 [SCIM](SCIM_zh-CN.md) 部署指南。邮件投递、邮箱密码重置与 CogDoc 自身强制 MFA 仍不提供；MFA/条件访问应由 IdP 强制，不能绕过本地会话和 ACL 校验。

长期自动化优先使用[持久服务账号](SERVICE_ACCOUNTS_zh-CN.md)，为每个用途签发独立、有限期、最小角色 token；静态 `COGDOC_API_PRINCIPALS` 只保留迁移和受控 break-glass。服务 token 原文不在备份中，恢复后必须从外部密钥系统重新注入客户端，无法确认的 token应撤销重发。

共享 Streamlit 部署禁止设置单数的 `COGDOC_API_KEY`。它是前端向外发请求的客户端凭据；一旦存在，每个尚无真人会话的浏览器都会按设计跳过登录页，并以该服务主体操作。多用户前端必须留空它，API 端服务身份只用 `COGDOC_API_KEYS` 或 `COGDOC_API_PRINCIPALS` 配置。单数变量仅适合受信的单用户控制台或专用自动化前端。

反向代理必须保留 `X-CogDoc-Workspace`。它是非敏感的工作区选择器，不是独立权限；API 每次请求都会把它与已认证会话的实时成员关系绑定验证。新客户端用它使并发标签页固定在预期工作区；不带它的旧客户端继续使用 session 活动工作区。上游网关应拒绝或覆盖伪造的不同值，且不得仅按此 header 分路/缓存响应而忽略认证身份分区。

回滚是运维恢复，不是删表：停止写入，恢复同一时间点的已验证 `state.db`、KB 与索引备份，恢复旧配置后重启。仅把账号鉴权改回 false 只会让程序忽略账号/ACL 表，不会把账号工作区 ID 映射成旧 tenant，不能当作数据迁移捷径。整个回滚窗口都要保留上线前归档与静态服务凭据。

## 统一 SQLite 状态迁移

默认后端仍为 `COGDOC_STATE_BACKEND=jsonl`。迁移完成并通过校验前不要切换后端。先停止 API、worker，以及所有可能写入 sessions、jobs、research plans、feedback、analysis、derived knowledge 或 retrieval feedback 的进程，再针对同一实例依次执行：

```bash
python scripts/migrate_state.py
python scripts/migrate_state.py --apply
python scripts/migrate_state.py --verify-only
```

第一条命令是 dry-run，不应修改任何状态。`--apply` 会获取同一实例的迁移锁，复制现有 JSONL 状态并保留 sessions/jobs，在临时库中构建统一 SQLite，完成全部 canonical record 对比后才原子替换 `state.db`。`--verify-only` 会独立比较已提交 SQLite 与 canonical 源记录。三步全部成功后，才设置：

```bash
COGDOC_STATE_BACKEND=sqlite
```

随后启动服务，检查 `/readyz`、会话历史、未完成/已完成索引任务、反馈数量、派生知识，以及一条代表性的检索反馈查询。在整个回滚窗口内保留 `state.db.pre-unified-*.bak` 和原始 JSONL；它们是恢复工件，不能在迁移后立即清理。

Research 证据执行以章节为恢复粒度。若服务在任务处于 `running` 时退出，启动过程会把执行中的章节重置为 `pending`，并把任务协调到 `paused`，必须由运维或用户显式恢复。报告生成会把每个原子需求重新送入闭集 Evidence Unit 校验，只有 `supported` 的 grounding ID 能进入章节生成；生成后的声明只依据本章精确证据接受审计，独立的需求覆盖审计还要求每个原子需求都由已支持且有引用的声明回答。声明与覆盖失败共享最多一次有界修复，修复后必须重新通过引用、声明和覆盖三道门。无证据、冲突、遗漏需求、校验失败、语义审计失败和生成失败都会成为报告中的显式缺口。若服务在 `generating` 时退出，任务会回到 `evidence_ready` 等待显式重试，并保留选择性重生成范围。状态库只保存有界证据预览、定位、公开引用账本、声明/覆盖审计摘要和渲染后的 Markdown 报告，不保存完整来源 chunk 或模型声明文本。

每次证据/报告 attempt 都持久化 attempt ID、可轮换 lease、阶段截止时间，以及检索查询、候选文档、模型调用和模型输入累计字符的原子预算。恢复执行必定轮换 lease，因此正在排空或迟到的 worker 不能继续预扣资源，也不能提交旧输出。已准入的排队/运行总量受 `COGDOC_RESEARCH_MAX_PENDING` 限制；超过上限的启动/生成请求返回带 `Retry-After` 的 `503`。暂停和取消会原子作废证据与报告 lease、通知活动 worker，并取消尚未开始的 future。截止时间或预算耗尽会持久化并 fail-closed。

自动规划的来源读取与模型工作使用独立的有界 daemon executor（`COGDOC_RESEARCH_PLANNING_WORKERS` / `COGDOC_RESEARCH_PLANNING_MAX_PENDING`），不占用共享 API offload 池；前后的短状态库操作仍使用共享池。绝对截止时间覆盖排队、来源读取和模型执行。进入 lifespan 关闭后，服务会通知所有已注册规划控制器、取消排队任务；若不透明的进程内来源读取仍未排空，则延后关闭 runtime 和释放进程锁。`make serve` 还通过 `UVICORN_GRACEFUL_SHUTDOWN_SECONDS`（默认 `15`）设置 Uvicorn 活动请求的优雅关闭上限；使用其他启动器时必须配置等价的有限上限，否则 Uvicorn 可能在进入 lifespan 关闭前无限等待活动 HTTP handler。原始 socket 断开不是所有 ASGI 服务器都会转成 handler 取消信号，因此这种情况下仍以专用容量和规划绝对截止时间作为外层边界。

自动规划及证据/报告生成中的标准工厂 `ChatOpenAI` 调用，会在全新的 spawn 子进程中重建，并关闭传输层重试。监督器会用规划或持久阶段的剩余截止时间收紧单次调用时限；子进程存活时轮询进程内停止信号与单调截止时间，在准入前和子进程回收后执行权威持久检查，并始终 join 和回收子进程。超时、暂停、取消或关闭会先发送 terminate，超过 `COGDOC_RESEARCH_PROVIDER_KILL_GRACE_SECONDS` 后升级为 kill。应按后台 Research attempt 可用的 provider 容量设置 `COGDOC_RESEARCH_PROVIDER_WORKERS` 与 `COGDOC_RESEARCH_PROVIDER_MAX_PENDING`，让 `COGDOC_RESEARCH_PROVIDER_CALL_TIMEOUT_SECONDS` 小于上游负载均衡器超时，并把 `COGDOC_RESEARCH_PROVIDER_IPC_MAX_BYTES` 视为 fail-closed 的响应信封上限。若已识别的 `ChatOpenAI` 客户端无法转换为安全子进程调用，在 `COGDOC_RESEARCH_LLM_PROCESS_ISOLATION_ENABLED=true` 时会 fail-closed；不透明或非标准客户端仍走有界 daemon 兼容路径，只能在检查点协作式停止。优雅关闭会在结束应用 lifespan 前作废全部活动 lease，兼容路径的迟到结果无法提交。

超时计时从 spawn 前开始，包含 provider 槽位等待与子进程生命周期。Python 本地 spawn bootstrap 和有界 IPC 信封解码属于可信的准入/序列化边界：其耗时会计入 deadline，但解释器无法异步抢占这些短同步操作本身。工厂调用会先转成有大小上限的纯字节配方，再进入 spawn，以保持该边界可预测。

这层隔离只终止本地 HTTP 客户端进程。已经收到请求的远端 API 或 Ollama 服务可能继续计算和计费，因此仍需 provider 侧请求 ID、预算及账单告警。检索、重排、嵌入、Hugging Face 模型加载、Torch kernel 与 native/Rust 调用仍在进程内；Research 控制器会在这些调用前后检查截止时间，但无法强制抢占阻塞中的调用。不得把本版本描述为任意 provider 沙箱或全流水线隔离。

集合视图应轮询 `GET /v1/research-jobs/summaries`，而不是兼容保留的完整列表接口。摘要接口使用有界 keyset 分页（`limit` 与 opaque `cursor`），返回 ETag、支持 `If-None-Match` 命中后的 `304`，且不包含章节、证据、报告和历史正文。只有用户显式选中后才获取一个任务详情及其报告。应监控 `cogdoc_research_lifecycle_total`、`cogdoc_research_background_total`、`cogdoc_research_background_in_progress`、`cogdoc_research_terminations_total`、章节候选/证据直方图以及覆盖/声明审计计数器；指标标签均为低基数闭集，任务 ID 只进入结构化日志字段。

声明核验应按粘性桶渐进发布。先设置 `CLAIM_VERIFICATION_MODE=shadow`，依次把 `CLAIM_VERIFICATION_ROLLOUT_PERCENT` 从 `5` 提升到 `25`、`100`；人工基线发布门禁通过后再切换 `enforce`，重复同一百分比阶梯。配置为 `shadow` 时未命中桶回退 `off`，配置为 `enforce` 时未命中桶回退 `shadow`，因此任何阶段都可把 mode 改为 `off` 作为全局停用。百分比提升期间不得修改 `CLAIM_VERIFICATION_ROLLOUT_SEED`，否则现有会话会重新分桶。最终响应和 trace 中的 `policy_id`、配置/实际 mode、bucket 不含原始会话身份，可用于复现；监控 `cogdoc_claim_verification_rollouts_total` 和 `cogdoc_claim_verification_cohorts_total`，确认实际分布、`would_block`、修复率和误放人工抽检结果后再扩大流量。

生产模块会把每个终态 rollout 的最小化元数据写入统一 `state.db`，按 `CLAIM_VERIFICATION_OBSERVATION_RETENTION_DAYS` 和 `CLAIM_VERIFICATION_OBSERVATION_MAX_PER_TENANT` 双重淘汰。记录不含问题、答案、证据、文档、会话或原始分桶 key；Reviewer/Owner 通过 `GET /v1/claim-verification/observations/summary` 获取当前工作区的时间窗聚合。未显式传入 `policy_id` 时，接口只统计当前策略版本；历史策略只用于追溯，不得与当前样本混合判定就绪。`operational_readiness` 仅以非 off 已执行样本数和 verifier error rate 判断运行稳定性，必须继续执行 `make eval-claim-verification-gate` 才能判断语义质量。写入失败只损失旁路观测且会记录结构化错误，不得反向阻断用户回答；读取失败返回 `503`，运维应修复 `state.db` 可写性或锁竞争后再扩大灰度。

人工标注闭环默认完全关闭（`CLAIM_VERIFICATION_REVIEW_SAMPLE_PERCENT=0`）。显式启用后，系统只从已执行的 `shadow`/`enforce` 审计中做确定性声明级抽样，并持久化声明正文、模型判定和该声明精确引用的有界证据快照；问题、完整答案、会话、trace ID 与原始分桶身份不会入库。Reviewer/Owner 可在网页“证据审核 → 声明核验”完成筛选、翻页、逐证据判卷、并发冲突处理和门禁集下载；对应 API 使用 `GET /v1/claim-verification/reviews/summary` 获取当前权限下的待审/已审、人机一致率与证据完整性指标，使用 `GET /v1/claim-verification/reviews` 分页取样，按需读取 `GET /v1/claim-verification/reviews/{review_id}`，再通过 `POST /v1/claim-verification/reviews/{review_id}/label` 提交带 revision 的乐观并发标注；`GET /v1/claim-verification/reviews/export` 以 `limit`/`cursor` 分页输出可直接供声明核验评测门禁使用的已审数据。所有汇总、读取、标注与导出除租户隔离外，还会按当前 KB 和证据 source ACL 重新鉴权；权限撤销后旧快照立即不可见，内部物理 KB ID 不进入响应。队列按 retention 与每租户上限双重淘汰，采样/写入失败仍不得影响用户回答。启用前必须完成私有语料审批，并按证据敏感度设置抽样率、文本上限和保留期。

Research 发布是独立的乐观并发状态转换：显式 `reviewer`、`admin` 或 `owner` 主体按 RBAC 放行并落盘其 `subject_id`；旧部署也可使用 `COGDOC_EVAL_REVIEW_API_KEYS`，此时只落盘非敏感 key 指纹身份。每次证据执行都会冻结索引 generation/build/chunk identity、来源 SHA-256、已批准派生知识版本、检索调权版本以及检索/校验契约版本；任何漂移都会把证据标为 stale，并阻止生成、审阅与发布。显式刷新会先归档旧报告，清空所有章节的证据与审计结果，再基于新快照执行全量检索。已生成章节必须标记为 `approved`，被阻断章节必须显式标记为 `accepted_gap` 并填写非空理由；任何 `changes_requested` 决定都必须附带修订要求，且只能通过同一检索与校验链路重新生成。系统最多归档十个完整报告版本，审阅历史最多保留 100 个事件。只有退回或旧版未审计章节会消耗检索、校验和生成资源；保留章节与新章节的局部账本会重新编号、换算偏移并合成为经过校验的全局账本。v2 artifact SHA-256 精确绑定 Markdown、严格引用账本、可追踪 provenance、有界聚合/逐章声明与需求覆盖审计、证据身份/文本哈希承诺、报告版本和生成时间；独立 publication SHA-256 再把该 artifact 与精确审阅历史、逐章决定、发布时间和审核者身份绑定。确定性 ZIP 包含 `report.md`、`citation-ledger.json`、`provenance.json`、`verification.json` 及逐文件哈希 manifest。旧版已发布 Markdown 仍可通过 `X-CogDoc-Integrity: legacy-unverified` 下载，但不能生成验证包；任何畸形或被篡改的 artifact 都不会返回正文。

dry-run、apply 或 verify 任一步失败时，保持服务停止且不要切换后端。保存命令输出的 JSON 错误，确认没有遗留迁移进程占用实例锁，检查数据目录的剩余空间和权限，并修复 malformed/duplicate canonical records 后重新从 dry-run 开始。禁止手工提升临时数据库。

SQLite 启动失败或迁移后检查失败时，按以下步骤回滚：

1. 停止 API 和所有状态写入进程。
2. 设置 `COGDOC_STATE_BACKEND=jsonl`，或删除 SQLite 覆盖配置。
3. 保留失败的 `state.db` 用于排障，不要覆盖留存的 JSONL。
4. 如果统一数据库替换了既有 `state.db`，仅在仍依赖旧数据库的组件需要时恢复对应的 `state.db.pre-unified-*.bak`。
5. 重启服务，从 JSONL 验证 sessions/jobs 和反馈状态；修复根因后重新从 dry-run 开始迁移。

迁移锁只能串行化同一实例中遵循该锁的迁移进程，不能保证在线应用写入安全，因此停止全部写入进程是强制运维前提。

## 索引格式与迁移

### 通用来源与连接器升级

本版本把 PDF 专用来源扩展为带不可变版本和格式中立位置的通用来源，并新增连接配置、加密凭据/OAuth、同步任务与 health、来源目录/原始版本和外部 ACL 状态。SQLite 结构在启动时只做增量建表/加列；旧 manifest 和 `secret_env` 连接保持兼容，但解析器/来源契约变化仍可能要求索引 generation 重建。完整配置与接口见[通用来源与知识源运维控制面](CONNECTORS_zh-CN.md)。

#### 凭据库与 OAuth 上线

连接密钥不得进入请求的 `config`、registry、日志或任务错误。两种受支持路径为：连接只保存环境变量名的 `secret_env`；或只保存 `credential_id`，由 vault 在 tenant/KB/connection 作用域校验后解密。两者不能在同一连接中混用。不配置 `COGDOC_CREDENTIAL_MASTER_KEYS` 时凭据/OAuth 接口返回 503，但环境引用连接继续工作，因此可逐连接迁移。

生产 vault keyring 是 key ID 到 base64url 32 字节 key 的 JSON，active ID 必须存在于其中。凭据采用每 revision 随机 DEK 的 AES-256-GCM 信封加密，认证数据绑定 credential、tenant、KB、可选 connection、provider 和 kind。数据库泄漏仍需要按敏感事件处理；加密不能抵消运行中主密钥和数据库同时失窃，也不能替代主机权限、日志脱敏和加密备份。

推荐上线顺序：

1. 停止写入并同时备份 `data/` 与部署密钥配置；生成 `v1` key，经密钥管理系统注入 `COGDOC_CREDENTIAL_MASTER_KEYS` 与 `COGDOC_CREDENTIAL_ACTIVE_KEY_VERSION=v1`。
2. 在 TLS 反向代理后设置 `COGDOC_CONNECTOR_OAUTH_PUBLIC_BASE_URL`；供应商控制台的 callback 必须精确为 `/v1/auth/connector-oauth/callback/{provider}`，其中 provider 分别为 `notion`、`atlassian`、`microsoft`。仅配置需要的 client；Notion/Atlassian 必须有 client secret，Microsoft public client 可留空 secret。
3. 用 owner/admin 身份创建手工凭据或发起 OAuth，把新凭据绑定连接，确认凭据 API 从不回显 `secret_values`，再执行一次同步和 ACL 拒绝测试。
4. 逐连接撤掉旧运行环境密钥。回滚窗口内仍应在受控位置保留旧部署配置，不得因新连接成功一次就立即销毁旧恢复路径。

轮换主密钥时先把新旧 key 同时放入 keyring，切换 active ID 后，逐条 PATCH 凭据使其在不改 secret 的情况下重新包装；用 metadata 的 `key_version` 和一次真实同步验收。只有所有持久凭据、短时 OAuth 会话和仍保留的备份都不再引用旧版本后，才能删除旧 key。active ID 切换不会自动批量重加密；遗失旧 key 会让对应密文永久不可用。OAuth access token 已过期或将在 60 秒内过期时，同步路径会先用 refresh token 执行 revision-safe 刷新；应对 provider 刷新失败和 refresh token 撤销单独告警。

凭据、OAuth 发起/刷新、source catalog、artifact 删除/恢复、同步取消和死信重放统一要求 KB 级 `manage_access`。连接/任务/health 摘要可由具备 `read` 的主体查看；包含上游 ID、本地 origin、失败诊断与投影 ACL 的运维 source catalog 不应开放给普通读者。OAuth callback 虽是公开路径，但只依赖短时、一次性、高熵 state 恢复服务端 tenant/KB/connection/user 绑定；网关必须完整透传 query，不能缓存 callback 响应。CogDoc 会从 Uvicorn access log 移除该 callback 的整段 query；反向代理、WAF、APM 与负载均衡器也必须只记录 callback path，禁止记录其中的 `state`、`code` 或 `error` query。

#### 来源版本与恢复

连接同步会把原始版本归档到 `COGDOC_DATA_DIR/source-artifacts/`，独立于当前物化目录与索引 generation。默认单版本上限 100 MiB、每来源保留 10 个活动版本、每租户（含 trash）硬上限 512 MiB；可用 `COGDOC_SOURCE_ARTIFACT_MAX_TENANT_MB` 调整租户隔离边界。批量同步在 authority transition 前原子预留 raw 版本容量，超限不会先发布物化快照。下载与文本 diff 会验证完整 SHA-256，diff 仅在内存保留 256 KiB/5000 行的有界前缀；二进制 diff 只返回两端 metadata。超过保留数的旧版本进入 store-local trash，trash 仍占用租户上限和 2 GiB 进程级紧急物理上限；监控 `GET .../source-artifacts/usage` 的 active/trash bytes 和 versions。

手工删除只允许非当前在线版本，返回绑定 tenant/KB 的 `recovery_token`；恢复会检查 hash、目标冲突和版本配额。catalog 的版本 metadata 不随 raw artifact 软删除，`artifact_available=false` 表示该版本不能下载/diff。永久清理接口 `DELETE .../source-artifacts/trash?older_than=<epoch>&limit=<n>` 只删除当前 tenant/KB 中早于边界的 trash，且不可恢复；调用前必须完成停止写入的一致备份、归档恢复演练和保留期审批，再以小批次清理并复查 usage。上游完整快照删除的来源会标为 `deleted_at`/`stale`，可用 `include_deleted=true` 排障；它与 raw artifact 的软删除是两套状态，不能互相代替。

#### 同步可观测与死信处理

可重试错误默认最多尝试 5 次并指数退避，耗尽后进入 `dead_letter`、停止该连接周期调度；非重试错误进入 `failed`。修复根因后，`POST .../sync-jobs/{job_id}/replay` 从最近成功 checkpoint 创建新任务，并以 `replay_of` 保留原死信关联；原任务不可变，且有活动任务或连接被禁用时拒绝重放。

告警应组合 `/connection-health` 的 `health_status`、`consecutive_failures`、`last_error_code`、`backlog` 与以下 Prometheus 指标：`cogdoc_connector_sync_events_total`、`cogdoc_connector_sync_duration_seconds`、`cogdoc_connector_sync_backlog`、`cogdoc_connector_sync_documents_total`。label 仅含闭集 connector type/outcome，不包含 tenant、KB、connection、job 等高基数 ID。配置 `COGDOC_WEBHOOK_URL` 后，retry/succeeded/failed/dead_letter 会异步发送 `connector.sync.<outcome>`；`COGDOC_WEBHOOK_SECRET` 是 `X-CogDoc-Webhook-Secret` 共享值而非签名，接收端必须使用 HTTPS、恒定时间比较、event ID 幂等和自己的重试/告警。投递失败只记日志，不回滚同步终态。

本地目录与 Git 连接可读取服务主机文件，必须把允许的根目录纳入部署审计。根目录 allowlist 是全实例共享能力，任一租户的 KB 管理员都能选择其中任一根或子目录；禁止把各租户私有目录的共同父目录列入，隔离要求更高时应拆分实例。URL 与云连接使用 HTTPS、服务端主机 allowlist、单次解析并固定公网 IP；重定向逐跳重验且跨 origin 剥离凭据，并受响应大小限制。网络层仍应拒绝到 loopback、RFC1918、link-local 和云 metadata 网段的出站流量，作为 DNS 与代理配置错误的纵深防御。

Confluence/SharePoint 权限读取失败、身份映射服务异常或 ACL 分页不完整时会将文档设为私有并撤销连接器托管授权。不要通过关闭 `include_acl` 绕过受限来源的权限同步；只有确认整个来源本来面向当前工作区时，才设置 `workspace_visible=true`。

#### 控制面回滚

升级前恢复点必须同时覆盖 `state.db`、`source-artifacts/`、KB 物化目录、Chroma/BM25/manifests、vault keyring、OAuth client 配置和环境引用密钥。回滚时停止所有写入，先在新目录/卷完整校验归档，再注入能够解密该时点所有 `key_version` 的 keyring 并启动旧版本。逐项验证 health、凭据 metadata、原始版本 hash、连接同步和 ACL 后再切流；不能只换旧镜像、只恢复数据库或手工删除新增表列。

以下变化必须视为索引契约变化：

- `CHUNK_IDENTITY_BASE_VERSION` 或 chunk 参数变化。
- `INDEX_BUILD_VERSION` 变化。
- parser、tokenizer、embedding model、BM25 artifact 格式变化。
- Chroma collection 命名或 generation layout 变化。

规则：

- 可复用变化：只改 API/前端/Prompt，不改 chunk/index/native artifact，可不强制重建。
- 强制重建变化：chunk identity、parser/tokenizer、embedding model、BM25 bytes 格式变化。
- 迁移说明必须写清楚：是否强制重建、是否兼容旧 generation、失败如何回滚。
