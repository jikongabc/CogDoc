# 企业 SCIM 2.0 目录同步

CogDoc 提供面向企业目录的 SCIM 2.0 `/Users` 与 `/Groups` 接口，用于把 IdP 中的人员状态、工作区成员关系和组角色同步到现有账号、OIDC 与 ACL 模型。SCIM 只负责目录生命周期；用户登录仍由 CogDoc 密码或企业 OIDC 完成，查询与写入权限仍在每次请求时按实时成员关系、角色和资源 ACL 判断。

当前实现支持多个工作区各自使用独立 Bearer token、用户预配/更新/停用/软删除、组成员同步、服务端组名到 CogDoc 角色映射、分页、精确 `eq` filter、PATCH 和弱 ETag 并发保护。Bulk、sort、密码同步、自定义 schema 和 SCIM 自动发现租户均不支持。

## 安全模型

- SCIM 使用与普通 API key、浏览器会话完全分离的 Bearer token。每个 token 只绑定一个工作区，只能访问 `/scim/v2`；它不能调用 `/v1` API，也不能跨工作区读取资源。
- 原始 token 只从受保护的部署配置读取。应用状态只保留 SHA-256 fingerprint，审计事件只记录工作区、标签、方法和结果，不记录 token 或请求体。
- 所有写操作在 SQLite `BEGIN IMMEDIATE` 事务中执行，并用资源 revision/ETag 做 optimistic CAS。两个目录请求用同一旧版本竞争时，只会有一个成功。
- SCIM 永远不能授予 `owner`。默认角色及组映射只允许 `admin`、`editor`、`reviewer`、`viewer`；多个组命中时取最高非 owner 角色。
- token 是工作区能力，不是全局账号管理权。修改一个工作区的 SCIM `userName` 或 `displayName` 不会改写共享用户的全局账号邮箱/显示名，也不会修改其他工作区的 SCIM 记录。
- `active=false` 或删除用户会立即移除该工作区成员关系并撤销该工作区会话。只有当同一账号的全部 SCIM 资源都不再活动时，账号级密码/OIDC 登录与全部会话才一并禁用；任一其他工作区仍活动时，不会错误停用全局账号。
- 组角色配置在每次启动时与持久记录对账。删除或降低映射会立即降权已有成员，不需要等待 IdP 再发送一次 PATCH。

## 配置

先完成账号模式和 [OIDC 单点登录](OIDC_zh-CN.md)，创建目标工作区并记录其 `wsp_...` ID。为每个目录连接生成独立随机 token：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

生产配置示例：

```dotenv
COGDOC_ACCOUNT_AUTH_ENABLED=true
COGDOC_SELF_REGISTRATION_ENABLED=false
COGDOC_OIDC_ENABLED=true

COGDOC_SCIM_ENABLED=true
COGDOC_SCIM_BEARER_TOKENS=[{"token":"replace-with-random-secret","workspace_id":"wsp_example","label":"Entra Production"}]
COGDOC_SCIM_DEFAULT_ROLE=viewer
COGDOC_SCIM_GROUP_ROLE_MAP={"CogDoc Admins":"admin","CogDoc Editors":"editor","CogDoc Reviewers":"reviewer"}
```

`COGDOC_SCIM_BEARER_TOKENS` 是单行 JSON 数组，最多 100 项。每项包含 32–4096 字符的 `token`、目标 `workspace_id` 和可选的非敏感 `label`。可以给同一工作区配置多个 token 以完成无停机轮换，但它们必须共享同一 issuer、默认角色和组映射策略。

组名按去空格后的 Unicode case-insensitive 精确值匹配，不做前缀、正则或模糊判断。未映射组仍保存成员关系，但不提升角色。默认从 `viewer` 开始；确认资源 ACL 后再逐组开放更高角色。

SCIM 使用 OIDC 配置的精确 issuer 绑定预配身份。生产入口会拒绝“开启 SCIM 但未开启账号鉴权/OIDC”的配置。服务必须位于 TLS 反向代理后，网关应只允许 IdP 出口访问 `/scim/v2`，并保留 `Authorization` header；禁止把该 header 写入日志、Trace、APM 或错误页。

## IdP 接入

在 Microsoft Entra、Okta 或其他 RFC 7644 客户端中使用：

| IdP 字段 | 值 |
| --- | --- |
| Tenant/Base URL | `https://api.example.com/scim/v2` |
| Secret token | 对应工作区的独立 SCIM Bearer |
| User identifier | 稳定 `externalId`，`userName` 使用已验证企业邮箱 |
| Provisioning actions | Create、Update、Deactivate/Delete Users；Create、Update、Delete Groups |

建议顺序：

1. 保留一个不依赖 SCIM 的 bootstrap owner 恢复账号，并完成 `state.db` 与外部密钥备份。
2. 先配置一个测试工作区和 `viewer` 默认角色，只同步少量试点用户。
3. IdP 测试连接后，确认 `/readyz` 的 `scim_directory=ready`，再检查管理面板中的活动/停用用户和组计数。
4. 验证创建、组加入、组移除、`active=false`、重新激活及用户删除；停用后旧会话必须立即失败。
5. 再配置生产组映射并扩大同步范围。不要把 IdP 的普通全员组直接映射为 `admin`。

一个企业用户可被预配到多个 CogDoc 工作区。未指定登录目标且同一 OIDC 身份命中多个活动 SCIM 工作区时，CogDoc 会以冲突拒绝并要求显式选择，不会猜测工作区。

## 协议范围

| 端点 | 作用 |
| --- | --- |
| `GET /scim/v2/ServiceProviderConfig` | 返回 PATCH/filter/ETag 能力以及不支持 Bulk/sort/password |
| `GET /scim/v2/ResourceTypes[/{User|Group}]` | 返回资源类型发现信息 |
| `GET /scim/v2/Schemas[/{schema}]` | 返回 User/Group 核心 schema |
| `GET/POST /scim/v2/Users` | 分页、精确过滤或创建用户 |
| `GET/PUT/PATCH/DELETE /scim/v2/Users/{id}` | 读取、替换、局部更新、停用并软删除用户 |
| `GET/POST /scim/v2/Groups` | 分页、精确过滤或创建组 |
| `GET/PUT/PATCH/DELETE /scim/v2/Groups/{id}` | 更新组属性/成员或软删除组 |
| `GET /v1/workspaces/{workspace_id}/scim-status` | owner/admin 查看不含 token/fingerprint 的同步摘要 |

User filter 支持 `id`、`externalId`、`userName` 的精确 `eq`；Group 支持 `id`、`externalId`、`displayName`。分页参数为 `startIndex`（从 1 开始）和 `count`（0–200）。不支持的 filter、路径、schema、超过 1 MB 的 body 或非法分页会返回 `application/scim+json` 错误，而不是静默忽略。

写响应带 `ETag: W/"<revision>"`。客户端应把该值作为下一次 PUT/PATCH/DELETE 的 `If-Match`；旧 revision 返回 412。未发送 `If-Match` 时，服务仍会在本次读取到的 revision 上执行 CAS，以防请求内部发生丢更新。

Group PATCH 支持替换、增加和删除 `members`，以及 `members[value eq "<user-id>"]` 删除形式。成员必须是同工作区内仍存在的 SCIM User；跨工作区或已删除 ID 以 404/400 fail closed。

## 令牌轮换、审计与恢复

轮换时先在 `COGDOC_SCIM_BEARER_TOKENS` 中为同一工作区加入新 token，受控重启并验证新 token，再从配置删除旧 token并再次重启。应用不会把原始 token写入数据库，因此恢复 `state.db` 不会恢复 token；部署密钥系统必须独立备份 SCIM token配置。

SCIM 的用户、组、组成员、revision 与 OIDC 映射都在 `state.db`。恢复必须使用同一时间点的完整数据库，并恢复当时有效的 OIDC/SCIM 配置。恢复后先在隔离环境检查 `/readyz`、管理摘要、抽样用户/组 ETag和一次停用撤销，再允许 IdP 恢复写入。

所有已认证 SCIM 请求进入现有持久审计链，但请求正文不会进入审计。运维应对连续 401、409/412、503、停用量突增和组映射变化告警。不要把 SCIM 审计替代 IdP 自身的 provisioning log；两边时间线都应保留。

关闭 `COGDOC_SCIM_ENABLED` 只会关闭目录端点，不会自动恢复已停用成员或删除持久目录记录。需要退出 SCIM 管理时，应先在 IdP 停止任务、导出成员与角色、把需要保留的成员转成明确的人工管理策略，再受控关闭；禁止直接删表或把所有用户临时设为 owner。

## 故障排查

- IdP 测试连接返回 401：确认使用的是 SCIM 专用 token、没有前后空格、网关保留 `Authorization`，并确认服务已用包含该 token 的配置重启。
- 返回 404：确认 Base URL 精确为 `/scim/v2`，资源 ID 来自同一个工作区/token；跨工作区 ID 会有意表现为不存在。
- 返回 409：检查 `externalId`、`userName`、组名唯一性，或 owner/现有全局账号冲突。
- 返回 412：IdP 使用了旧 ETag；重新 GET 资源后基于最新 revision 重试，不要无条件覆盖。
- `/readyz` 中 `scim_directory=not_ready`：停止 IdP 写入，检查 `state.db` 权限/schema/完整性并从一致备份恢复，不能删 SCIM 表绕过。
- 用户已停用但仍能访问另一个工作区：这是多工作区预配的预期行为；检查另一个活动 SCIM 资源。目标已停用工作区的成员关系和会话仍必须被撤销。
- 修改组映射后角色未变化：确认完成受控重启并查看 `/v1/workspaces/{id}/scim-status`。启动对账失败会让 readiness 保持不可切流状态。
