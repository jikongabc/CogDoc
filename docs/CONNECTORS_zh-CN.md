# 通用来源与持续同步

CogDoc 的来源层不再只表示“某个 PDF”。每份内容都以 `SourceDocument`、不可变 `SourceVersion` 和格式中立的 `SourceLocation` 进入索引。旧 PDF 与 `[文件:P页码]` 引用保持兼容；新格式会使用可验证的位置，例如幻灯片、工作表单元格、文本行、图片或章节，并在公开引用账本中固定 `source_version_id`。

## 支持范围

直接上传和连接器同步均支持：

- PDF、Markdown、纯文本与 HTML；
- DOCX、PPTX 与 XLSX（包括表格文本）；
- PNG、JPEG、TIFF、BMP 与 WebP。图片文字依赖本机 Tesseract；OCR 未安装或失败时按现有降级策略处理。

内置连接器包括本地目录、Git 工作树、固定 URL、Zotero、Notion、Confluence、SharePoint 和 S3。同步任务具备持久 checkpoint、租约、取消、指数退避、字节/页数/文档数预算以及崩溃恢复。任务只有在文件物化、权限映射和索引 generation 均完成后才进入 `succeeded`。

## 创建连接

网页侧栏的“来源连接”面板可以创建、暂停和立即同步连接。也可调用：

```text
GET    /v1/knowledge-bases/{kb}/connections
POST   /v1/knowledge-bases/{kb}/connections
PATCH  /v1/knowledge-bases/{kb}/connections/{connection_id}
DELETE /v1/knowledge-bases/{kb}/connections/{connection_id}
POST   /v1/knowledge-bases/{kb}/connections/{connection_id}/sync
GET    /v1/knowledge-bases/{kb}/sync-jobs
GET    /v1/knowledge-bases/{kb}/sync-jobs/{job_id}
POST   /v1/knowledge-bases/{kb}/sync-jobs/{job_id}/cancel
```

连接管理属于管理员能力（`manage_access`）。普通读者只能查看连接和同步状态，不能创建连接、触发同步或改变连接状态。

创建请求示例：

```json
{
  "connector_type": "confluence",
  "name": "团队手册",
  "config": {
    "base_url": "https://team.atlassian.net",
    "include_acl": true,
    "schedule_seconds": 300
  },
  "secret_env": {
    "token": "COGDOC_CONFLUENCE_TOKEN"
  },
  "workspace_visible": false
}
```

`secret_env` 只保存环境变量名，API、SQLite 和任务错误都不会保存密钥值。明文 `token`、`password`、`secret_key` 等不能放进 `config`。服务进程必须在同步开始前获得所引用的环境变量；缺失密钥会使任务安全失败。

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

周期最短 60 秒。URL/云连接只允许 HTTPS、显式允许的主机、公网 DNS 结果且不跟随重定向；响应大小受限。本地与 Git 连接读取服务主机文件，因此仅管理员可配置。

## 权限同步

SharePoint 和 Confluence 会随内容抓取外部权限快照。外部用户通过当前工作区成员身份映射为文档授权；未知用户和尚不支持的组不会扩大权限。上游权限不完整、权限接口失败或身份服务失败时，文档进入私有隔离状态，并撤销该连接器此前管理的授权。人工添加的授权不会被连接器撤销或覆盖。

Notion 等不能提供完整 ACL 的接口默认返回不完整权限；在账号鉴权模式下会按 fail-closed 处理。对于受信任且本就面向全工作区的来源，可在连接上显式设置 `workspace_visible=true`。

## 升级与回滚

数据库表和 `managed_by` 字段在启动时执行向前兼容的增量创建。旧 manifest 会在读取/下一次真实索引提交时投影为通用来源契约，不需要直接编辑 JSON 或 SQLite。

解析器与来源契约属于索引构建版本的一部分。生产升级应停止写入并备份 `state.db`、知识库源目录和索引 generation，然后执行：

```bash
python scripts/migrate_v7_indexes.py scan
python scripts/migrate_v7_indexes.py run
```

逐库验证 PDF 页码引用、至少一种非 PDF 定位引用、连接权限和同步任务后，再执行 `finalize <run_id>`。验收失败时使用 `rollback <run_id>` 回切保留的上一代索引，并恢复同一时间点的 `state.db` 与源目录备份。不要只回滚数据库或只回滚索引；连接任务、物化文件、ACL 与 generation 必须来自同一恢复点。
