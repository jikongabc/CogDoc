# 持久服务账号与 API Token

CogDoc 的工作区 owner/admin 可以创建持久服务账号，为 CI、入库机器人、只读检索客户端和运维自动化签发可到期、可轮换、可立即撤销的 API Token。它替代需要重启才能变更的 `COGDOC_API_PRINCIPALS`，同时保留静态 key 作为迁移与应急兼容入口。

服务账号是非人类主体：没有密码、浏览器会话、个人工作区或 OIDC 身份。每次请求都会从 `state.db` 实时读取账号的工作区、角色、启用状态、token 到期/撤销状态，因此角色降低、停用和撤销会作用于下一次请求。

## 安全边界

- 原始 token 使用 256 bit 随机值和 `cog_svc_` 前缀，只在创建响应与当前 Streamlit rerun 中显示一次；数据库、列表、审计、日志和后续 API 永不返回原文。
- `state.db` 只保存 SHA-256、非敏感末四位 hint、标签、到期时间和节流更新的最后使用时间。高熵 token 的 hash 仍应按敏感 metadata 保护，不能替代文件权限、TLS 和加密备份。
- 每个账号固定到一个工作区，角色只能是 `admin`、`editor`、`reviewer`、`viewer`，不能成为 owner。每个 token 还冻结一个非空权限子集，实际权限始终是“当前账号角色 ∩ token 作用域”：角色降低立即收窄旧 token，角色升级不会让旧 token 静默扩权。服务 token 带另一个工作区 header/path 时返回不透明 404。
- 服务账号即使是 admin，也不能调用 `/v1/auth/*`、`/v1/workspaces/*` 或 `/v1/principals/*` 管理真人身份、OIDC/SCIM、成员、邀请或继续铸造 token。服务账号生命周期必须由实时 owner/admin 真人会话管理。
- 停用账号会在同一事务中永久撤销其全部现有 token；重新启用后必须生成新 token。删除是软删除账号并撤销全部 token，不会让旧 token 因重名账号而复活。
- 账号与 token 更新使用 revision/CAS。并发轮换、停用或撤销只有一个旧 revision 能成功，其他请求返回 409。
- 每工作区最多 100 个活动服务账号，每账号最多 10 个未撤销且未到期 token；默认 token 有效 90 天，API 可设 1–365 天。显式 `expires_in_days=null` 才创建永久 token，不建议生产使用。

上述上限是兼容默认值。owner/admin 可通过工作区安全策略收紧为 1–500 个账号、每账号 1–50 个活动 token、1–365 天最长 TTL、禁止永久 token，并限制整个工作区允许签发的权限集合。策略变更使用 revision CAS；权限集合变更实时与现有 token 取交集，数量和 TTL 上限则约束后续签发，不会擅自删除已有凭据。

## 创建与使用

在 Streamlit 侧栏以 owner/admin 登录，打开“服务账号与 API Token”：

1. 创建账号，填写用途并选择满足任务的最低角色；只读检索通常使用 `viewer`，写入文档/连接配置按现有 RBAC 选择 `editor` 或 `admin`。
2. 在账号内生成 token，再从该角色权限中只勾选任务需要的作用域；复制一次性明文并立即存入 CI/密钥管理系统。
3. 使用普通 Bearer 或 `X-API-Key` 发送：

```bash
curl -H "Authorization: Bearer $COGDOC_SERVICE_TOKEN" \
  -H "X-CogDoc-Workspace: wsp_example" \
  https://api.example.com/v1/tenant
```

Bearer 与 `X-API-Key` 同时存在时仍由 Bearer 优先。不要把 token 放入 URL、query、命令历史、镜像层、仓库、Trace 或普通 `.env` 归档。

## API

以下接口全部要求目标工作区的真人 owner/admin 会话：

| 端点 | 用途 |
| --- | --- |
| `GET/POST /v1/workspaces/{workspace_id}/service-accounts` | 列表或创建账号 |
| `GET/PATCH /v1/workspaces/{workspace_id}/service-accounts/{id}` | 查看或 revision-safe 修改名称、角色、启用状态 |
| `DELETE .../service-accounts/{id}?expected_revision=N` | 软删除账号并撤销全部 token |
| `GET/POST .../service-accounts/{id}/tokens` | 查看 token metadata 或签发一次性原文 |
| `DELETE .../tokens/{token_id}?expected_revision=N` | 立即撤销单个 token |
| `GET/PUT /v1/workspaces/{workspace_id}/service-account-policy` | 查看或 revision-safe 更新工作区账号数、token 数、TTL、永久 token 与权限上限 |

创建 token 示例：

```json
{
  "label": "github-actions-production",
  "expires_in_days": 30,
  "permissions": ["read", "query"]
}
```

成功响应带 `Cache-Control: no-store`。其中顶层 `token` 只出现一次；`service_token` metadata 可安全用于记录 `token_id`、revision、hint 与到期时间。列表只返回 metadata。

## 从静态 API key 迁移

1. 保留现有 `COGDOC_API_PRINCIPALS`，用真人 owner 登录目标工作区。
2. 为每个自动化用途创建独立服务账号，不要多个系统共用一个 token；角色先与旧 principal 等价，再按实际调用降权。
3. 生成有期限的新 token，更新一个客户端并验证 `/v1/tenant`、代表性允许操作和一个应被拒绝的操作。
4. 从服务器配置移除对应静态 key，受控重启，确认旧 key 返回 401、新 token继续工作。
5. 全部迁移后只保留经过审批的 break-glass 静态主体。`COGDOC_API_KEY` 单数仍是 CLI/Streamlit 出站凭据，不是服务端账号库。

静态配置与持久服务账号使用相同 RBAC，但前者只有重启时读取，无法提供每 token 到期、最后使用和在线撤销。不要把“保留静态兼容”理解为两边会自动同步。

从 schema v4 升级的存量服务 token 没有冻结作用域，为保持兼容会继续跟随账号当前角色；升级后应逐个签发显式最小权限的新 token、切换客户端，再撤销旧 token。所有新 token（即使请求省略 `permissions`）都会冻结签发时角色权限，后续角色升级不会扩张它。

## 轮换、审计与恢复

轮换采用重叠窗口：先创建新 token、更新客户端并验证，再按 token revision 撤销旧 token。不要通过禁用后重新启用账号来轮换，因为禁用会有意永久撤销全部旧 token。

所有服务账号管理和使用请求进入现有工作区审计链，principal 记录稳定的 `service-account:<id>`，不会记录原始 token。应对即将到期、长期未使用、连续 401/403、admin token 使用和异常工作区访问告警。最后使用时间最多每 5 分钟落库一次，不能当作逐请求计费账本。

账号与 token metadata/hash 位于 `state.db`，但一次性原始 token 只在外部密钥系统。恢复数据库不会恢复丢失的原文；应撤销不可确认的旧 token并签发新 token。回滚到不支持持久服务账号的版本前，先把关键自动化迁回受保护的静态 principal，停止写入并恢复同一时间点备份，不能手工降低 schema version。

## 故障排查

- 401：检查 token 是否被撤销、到期、所属账号被停用/删除，或复制时是否多了空格；列表中的 hint 只能辅助定位，不能恢复原文。
- 403：账号角色不足，或正在访问身份/工作区/服务账号管理端点；应由真人管理员执行这些操作，不要提升自动化为 owner。
- 404：请求 path/header 指向另一个工作区，或资源不属于 token 工作区。服务不会向 token 泄露其他工作区是否存在。
- 409：刷新账号/token metadata 后使用最新 revision 重试；不要移除 CAS 或盲目覆盖赢家。
- 服务突然失败：检查账号是否刚被降权/停用、token 是否到期以及审计事件；角色和撤销是实时的，不要求重启。
