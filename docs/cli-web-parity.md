# CLI 与 Web 功能对齐约定

CogDoc Web 和 `cogdoc` CLI 都是 FastAPI `/v1` 的客户端。账号、Workspace、角色、ACL、知识库、任务状态和审计结果以服务端为唯一事实来源；任何产品功能不得只通过 CLI 直接修改本地数据库。

## 入口

```bash
# API 服务先运行
make serve

# 真人账号登录；令牌以 0600 权限保存在 ~/.config/cogdoc/cli.json
cogdoc login owner@example.com
cogdoc status

# 无参数进入交互模式；普通文本直接向当前知识库提问
cogdoc
```

CI 和自动化应使用最小权限服务账号令牌，通过 `COGDOC_API_KEY` 注入，不要把令牌放进命令历史：

```bash
COGDOC_API_KEY='cog_svc_...' cogdoc --workspace wsp_xxx kb list
```

`cogdoc --local-storage`（或 `cogdoc-local`）是显式的离线维护入口。它使用 `default` 本地租户、会获取单实例写锁，不代表 Web 账号数据，也不能用于正常多租户操作。

## 能力映射

| 产品能力 | CLI 命令 | Web 区域 |
| --- | --- | --- |
| 登录、账号会话与 OIDC | `login`、`logout`、`status`、`auth ...` | 登录页、账号菜单 |
| Workspace、成员、角色、邀请 | `workspace ...` | 工作区管理、成员与角色 |
| 知识库及角色范围 | `kb ...`、`kb access` | 知识库、访问权限 |
| 批量文档与 Embedding | `document upload ... --embedding ... --roles ...` | 上传区 |
| 文档策略与主体 ACL | `document access`、`acl ...` | 访问权限 |
| 索引、同步、系统任务 | `jobs` | 任务中心 |
| 流式问答和会话 | `chat`、`sessions` | 对话工作台 |
| 派生知识及批量审核 | `knowledge ...` | 派生知识 |
| Research | `research ...` | 研究 |
| 连接器与同步 | `integration ...` | 外部集成 |
| Trace、检索诊断 | `trace ...`、`diagnose` | Trace 调试、检索诊断 |
| RAG 评测、声明核验 | `evaluation ...` | RAG 评测 |
| 索引代际 | `migration ...` | 索引代际 |
| 审计导出、安全策略与 SCIM | `audit ...`、`security ...` | 管理设置 |
| 服务账号和令牌 | `service-account ...` | 管理设置 |

新发布且尚未增加快捷命令的 API 仍可通过受限入口调用：

```bash
cogdoc api GET /v1/audit-events
cogdoc api GET /v1/workspaces/wsp_xxx/service-accounts
```

该入口只接受 `GET/POST/PUT/PATCH/DELETE` 和 `/v1/` 路径，仍携带当前 Workspace 与 Bearer 权限，不会绕过后端鉴权。

## 对齐规则

1. Web 与 CLI 必须复用 `CogDocClient` 或同一个已版本化 API 契约，禁止从 CLI 新增数据库直写产品路径。
2. CLI 切换 Workspace 后必须清空当前知识库选择，防止跨租户复用上下文。
3. 创建知识库、上传文档和修改访问范围时，CLI 必须传递与 Web 相同的 `role_ids`。
4. 文档上传必须走批量异步索引端点，并返回可在 Web 任务中心看到的 `job_id`。
5. Chat 必须消费 `/v1/chat/stream`，在终端逐 token 输出，并保留最终引用、Trace 和 Session 标识。
6. 破坏性命令要求交互确认；自动化必须显式传 `--yes`。

## 契约验证

`tests/test_api_cli.py` 检查登录态隔离、交互参数继承、Workspace 头、ACL 失败关闭、任务部分降级、批量上传、反馈/证据绑定和 `/v1` 路径约束；`tests/test_frontend_client.py` 检查 Python 客户端与后端路由形状。前端 TypeScript 客户端继续由 Web typecheck、lint、build 和 E2E 覆盖。
