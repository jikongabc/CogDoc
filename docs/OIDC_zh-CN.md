# 企业 OIDC 单点登录

CogDoc 可把一个企业 OpenID Connect 身份提供方接入现有账号、工作区、会话与 ACL 模型。OIDC 登录成功后签发的仍是普通 CogDoc Bearer 会话；后续权限判断继续使用实时成员关系、角色和资源 ACL，而不是长期信任 ID token。

当前实现面向单个服务端配置的 OIDC provider，支持 Authorization Code、S256 PKCE、nonce、一次性 state、RS256/JWKS 验签、显式账号绑定、可选 JIT 建号、工作区域名准入与 IdP 组到工作区角色映射。企业目录生命周期可另行接入 [SCIM 2.0](SCIM_zh-CN.md)；邮件投递、密码重置和由 CogDoc 强制执行的 IdP MFA仍不提供，MFA、设备与条件访问应在 IdP 侧配置。

## 安全模型

- discovery、token 与 JWKS 端点必须是默认 443 端口的 HTTPS URL，并且 hostname 在服务端 allowlist 中。传输会固定一次解析得到的公网 IP、保留原 hostname 的 TLS SNI/证书校验、禁止 redirect，并对请求总时限、DNS 地址数和响应大小设上限；
- callback 只接受服务端生成、短时、一次性的高熵 `state`。nonce 与 PKCE verifier 使用 AES-256-GCM 加密后保存在 SQLite，数据库不保存 state、handoff code 或 CogDoc Bearer 明文；
- ID token 必须使用 RS256，严格校验签名、issuer、audience/azp、exp、iat、可选 nbf、nonce、subject 和显式 `email_verified=true`。同 `kid` 密钥轮换时会强制刷新一次 JWKS；
- callback URL 只携带短时一次性 `oidc_code`，不会携带 CogDoc Bearer。前端立即用 POST 交换，随后从浏览器 URL 删除该参数；
- 已存在的邮箱默认不会因“邮箱相同”自动绑定。用户必须先用现有方式登录，再从账号面板发起显式绑定。只有明确设置 `COGDOC_OIDC_ALLOW_VERIFIED_EMAIL_LINK=true` 才允许受信 provider 的 verified email 自动链接；
- 账号绑定在写入身份的同一事务中复验发起会话。会话在 provider 往返期间被撤销或过期时，callback fail closed；
- 组声明必须是有界字符串列表，组名经 NFKC、空白折叠与大小写归一化后做精确匹配；不支持通配、子串或 `owner` 映射。一次 token 最多接受 200 个组和 32 KiB 字符串列表声明；
- 只有由 OIDC JIT 创建并记录为 OIDC-managed 的成员角色会随组声明重算。管理员手工改角色会解除 OIDC 对该成员的角色管理；SCIM 一旦接管，同一成员始终以 SCIM 角色为准；
- 回调 query 必须通过网关透传但不得进入 access log、WAF、APM 或分析平台。CogDoc 会脱敏 Uvicorn access log，外层代理仍需只记录 callback path。

## 配置

先开启持久账号鉴权，并在 IdP 注册下面这个精确 callback：

```text
https://api.example.com/v1/auth/oidc/callback
```

生成独立的 32 字节 flow 加密密钥：

```bash
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

生产环境示例：

```dotenv
COGDOC_ACCOUNT_AUTH_ENABLED=true
COGDOC_SELF_REGISTRATION_ENABLED=false

COGDOC_OIDC_ENABLED=true
COGDOC_OIDC_ISSUER=https://id.example.com
COGDOC_OIDC_CLIENT_ID=cogdoc-production
COGDOC_OIDC_CLIENT_SECRET=replace-from-secret-manager
COGDOC_OIDC_REDIRECT_URI=https://api.example.com/v1/auth/oidc/callback
COGDOC_OIDC_FLOW_KEY=replace-with-base64url-32-byte-key
COGDOC_OIDC_DISPLAY_NAME=Company SSO
COGDOC_OIDC_SCOPES=openid,email,profile
COGDOC_OIDC_ALLOWED_ENDPOINT_HOSTS=id.example.com
COGDOC_OIDC_ALLOWED_RETURN_URLS=https://app.example.com/

COGDOC_FRONTEND_PUBLIC_URL=https://app.example.com/
COGDOC_OIDC_JIT_PROVISIONING_ENABLED=false
COGDOC_OIDC_ALLOW_VERIFIED_EMAIL_LINK=false
```

`COGDOC_OIDC_ISSUER` 必须与 discovery 和 ID token 的 `iss` 完全一致。若 discovery 返回另一个 token/JWKS hostname，需要把它显式加入 `COGDOC_OIDC_ALLOWED_ENDPOINT_HOSTS`；仅列 hostname，不含 scheme、port 或 path。`COGDOC_OIDC_ALLOWED_RETURN_URLS` 是逗号分隔的精确 HTTPS URL 列表，必须包含 Streamlit 使用的 `COGDOC_FRONTEND_PUBLIC_URL`。

public client 可以留空 client secret；confidential client 应从密钥管理系统注入。不要把 client secret、flow key 或 Bearer 写入仓库、镜像、URL、命令历史和日志。

## 上线顺序

1. 备份并校验 `state.db`，通过现有密码或服务主体保留一条 owner 恢复路径。
2. 在 IdP 建应用，配置精确 callback、允许 `openid email profile`，并要求已验证邮箱。建议在 IdP 侧强制 MFA/条件访问。
3. 注入 OIDC 配置，先保持 JIT 与 verified-email 自动链接关闭，启动服务并确认 `/readyz` 的 `oidc_flow_store` 为 `ready`。
4. owner 登录 CogDoc，在账号面板显式绑定自己的企业身份；用隐私窗口验证登录、callback、一次性交换、登出和会话撤销。
5. 对需要自动加入的团队工作区，由 owner/admin 设置 OIDC policy：允许的精确邮箱域、默认成员角色和 enabled。可额外配置 IdP 组 claim（默认 `groups`）及 `group=role` 映射；建议打开“仅允许命中组映射”，先映射 `viewer`，验证 ACL 后再提升。
6. 需要自动建号时才开启 `COGDOC_OIDC_JIT_PROVISIONING_ENABLED=true`。若同一 issuer+domain 同时匹配多个工作区而请求未固定 workspace，登录会以冲突拒绝，不会猜测目标。
7. 检查 Uvicorn、反向代理、WAF 与 APM 日志，确认 callback 的 `state`、`code`、`error` 和前端 `oidc_code` 均未出现。

JIT 用户会得到一个 OIDC-only 账号和个人工作区；随机不可达密码只用于保持表结构一致，密码能力在同一事务中关闭。不能删除 OIDC-only 用户的最后一种登录方式。工作区 policy 只负责 OIDC-managed 成员的准入与角色，不会绕过手工成员关系、SCIM 或资源 ACL，也不会自动移除后来不再匹配域名的现有成员；离职与强制会话撤销仍应走成员/SCIM 生命周期流程。

policy 的 `group_claim` 是 ID token 中的精确 claim 名，`group_role_map` 是规范化组名到 `viewer|reviewer|editor|admin` 的映射。一个用户命中多个组时取权限最高的非 owner 角色。`require_mapped_group=false` 时没有命中会回退 `default_role`；设为 `true` 时没有命中会拒绝 JIT 加入和后续 OIDC-managed 工作区登录。policy 更新使用 `expected_revision` 乐观并发控制，并进入普通管理 API 审计链。

## API

| 端点 | 作用 |
| --- | --- |
| `POST /v1/auth/oidc/authorize` | 创建登录 flow，返回 provider authorization URL |
| `GET /v1/auth/oidc/callback` | IdP 公开回调；返回到 allowlisted 前端 URL |
| `POST /v1/auth/oidc/exchange` | 一次性交换浏览器 handoff code，返回普通 CogDoc 会话或绑定结果 |
| `POST /v1/auth/oidc/link/authorize` | 已登录用户发起显式身份绑定 |
| `GET /v1/auth/oidc/identities` | 查看当前账号已绑定的联邦身份 metadata |
| `DELETE /v1/auth/oidc/identities/{identity_id}` | 解绑；不能移除唯一认证方式 |
| `GET/PUT /v1/workspaces/{workspace_id}/oidc-policy` | owner/admin 查看或 revision-safe 更新域名准入策略 |
| `GET /v1/workspaces/{workspace_id}/scim-status` | owner/admin 查看不含凭据的目录同步摘要 |

前端返回 URL 可以带已有 query；allowlist 比较是规范化后的完整 URL 精确匹配。callback 会追加 `oidc_code` 或通用 `oidc_error=authorization_failed`，不会把 provider 原始错误或 token 回显给浏览器。

## 备份、轮换与恢复

OIDC flow、身份映射、工作区 policy 和 CogDoc 会话都在 `state.db`；client secret 与 flow key 必须独立备份。恢复时数据库与密钥必须来自同一恢复点。缺失 flow key 不会暴露 flow 内容，但会使仍在途登录无法完成。

flow 最长 30 分钟、handoff 最长 5 分钟；过期行在启动时有界清理。轮换 flow key 前停止新登录并等待最长 flow TTL + handoff TTL，或接受所有在途 flow 失效，然后在一次受控重启中替换 key。当前 flow store 不维护多版本 keyring，不能热轮换仍在使用的旧 key。

回滚到不支持 OIDC 的版本前应停止写入并恢复同一时点的完整 `state.db` 备份；不要手工删除 v2 身份表或只修改 schema version。关闭 `COGDOC_OIDC_ENABLED` 只会关闭新 OIDC 流程，已签发的普通 CogDoc 会话仍按原 TTL/撤销状态有效，如需立即收口应同时撤销相关会话。

## 故障排查

- `/v1/auth/config` 中 `oidc_enabled=false`：检查账号鉴权与 OIDC 开关，确认必填配置非空；
- `/readyz` 返回 503 且 `oidc_flow_store=not_ready`：检查 `state.db` 权限、schema 与 flow key，先恢复一致备份，禁止删表绕过；
- provider 页面前失败：核对 issuer discovery、endpoint hostname allowlist、DNS/egress 和 TLS；非 443 endpoint 会被拒绝；
- callback 后回到 `oidc_error=authorization_failed`：检查 redirect URI 精确一致、授权码未重放、系统时间、JWKS、audience/azp、nonce 和 verified email；服务端不会把具体 provider 错误放进浏览器 URL；
- 新用户被拒：检查 JIT 开关、workspace policy issuer/域名/enabled、是否发生多 policy 歧义，以及邮箱是否已被本地账号占用；
- 已有邮箱冲突：先用密码/现有 OIDC 登录，然后从账号面板显式绑定。除非已完成 IdP 信任评估，不要打开 verified-email 自动链接。
