# 工作区会话安全策略

CogDoc 的用户 Bearer 会话可以在同一账号有权访问的工作区之间切换。工作区 owner/admin 可为每个工作区独立设置会话上限；策略只约束当前活动工作区是该工作区的用户会话，不影响服务账号 Token，也不会改变其他工作区的策略。

## 策略字段

`GET/PUT /v1/workspaces/{workspace_id}/session-policy` 使用以下字段：

| 字段 | 范围 | 语义 |
| --- | --- | --- |
| `idle_timeout_minutes` | `null` 或 5–43200 | 距离持久化的最近活动超过该时间后撤销会话 |
| `absolute_timeout_hours` | `null` 或 1–8760 | 从会话创建起计算的绝对最长时长 |
| `max_active_sessions` | `null` 或 1–50 | 每个用户在该工作区最多保留的活动会话数 |
| `expected_revision` | 非负整数 | 乐观并发版本；首次保存使用 `0` |

三个约束默认均为 `null`，保持升级前的全局会话 TTL 行为。设置绝对时长只会缩短现有 token 的期限，后续放宽策略不会延长已经签发的 token。并发上限按用户隔离；新登录或切换进入工作区时保留当前会话并撤销该用户最旧的超限会话，不会让一个用户挤掉另一个用户。

## 生效与撤销

- 保存更严格策略时，CogDoc 在同一身份库事务中缩短现有过长期限，并撤销已经空闲、绝对过期或超出并发上限的会话；
- 每次认证仍会实时检查当前工作区策略。命中空闲或绝对超时的 token 会先持久撤销，再统一返回未认证；
- 从其他工作区切换时，会在切换提交前检查目标工作区策略与并发槽位；
- 活动时间最多每 60 秒持久化一次，因此空闲超时最短限制为 5 分钟，避免高频读取把 SQLite 写路径串行化；
- policy 的 GET/PUT 需要 `manage_access`，修改走普通 HTTP 审计链，且 revision 冲突返回 409。

策略不会禁用密码或 OIDC 登录，也不能映射/提升角色，因此即使误设较短时长，owner 仍可重新认证并修复策略。若需要企业 JIT、组角色或目录停用，请分别使用 [OIDC](OIDC_zh-CN.md) 与 [SCIM](SCIM_zh-CN.md)；若需要机器身份 TTL 与权限上限，请使用[服务账号策略](SERVICE_ACCOUNTS_zh-CN.md)。

## 会话盘点与事件响应

owner/admin 可通过 `GET /v1/workspaces/{workspace_id}/security-sessions` 分页查看当前活动工作区等于该工作区的会话。响应只包含 `session_id`、用户安全 metadata、角色、创建/最近活动/到期时间与状态；查询不会读取或返回 Bearer、token hash、密码、OIDC claim 或客户端网络标识。

- `limit` 为 1–100，默认 50；响应的 `next_before_session_id` 是工作区绑定的排他 keyset 游标；
- 默认只列活动会话；`include_inactive=true` 可用于事件复盘已撤销或自然过期的记录；
- `DELETE /v1/workspaces/{workspace_id}/security-sessions/{session_id}` 幂等撤销一个仍属于该工作区的会话；跨工作区 ID 与游标统一按不存在处理；
- admin 可以撤销普通成员与自己的会话，但不能撤销 owner；owner 可以撤销任意工作区会话。成员已移除但仍残留的工作区会话仍会显示为无角色，便于 owner/admin 收口；
- 前端账号页展示最近 50 个活动会话；更长历史使用 API 游标读取。

## 运维建议

1. 先设置绝对时长和较宽并发上限，确认管理端、长任务和反向代理不会缓存旧 401；
2. 再启用空闲超时，并验证浏览器多标签页、移动端休眠与工作区切换；
3. 保留至少两位 owner 的独立登录方式；策略收紧会立即撤销不合规会话，但不会删除账号或成员关系；
4. 备份 `state.db` 时必须包含 `auth_workspace_session_policies` 与 `auth_sessions` 的同一恢复点；不要只回滚策略表；
5. 事件响应时可继续用 `/v1/auth/logout-all` 或逐会话 DELETE 主动撤销，不必等待策略超时。
