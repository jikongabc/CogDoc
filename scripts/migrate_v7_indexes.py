import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.api.ingest import KnowledgeBaseRegistry  # noqa: E402
from cogdoc.service.index_migration import IndexMigrationRunner  # noqa: E402
from cogdoc.state_runtime import default_state_runtime  # noqa: E402


def _progress(event):
    message = {
        "progress": f"{event['position']}/{event['total']}",
        "kb_id": event["kb_id"],
        "status": event["status"],
    }
    if event.get("error"):
        message["error"] = event["error"]
    print(json.dumps(message, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检测、迁移和回滚 v7 RAG 索引；迁移期间保留上一代索引。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="只检测需要迁移的知识库")
    run_parser = subparsers.add_parser("run", help="迁移全部过期知识库")
    run_parser.add_argument("--include-current", action="store_true")
    rollback_parser = subparsers.add_parser("rollback", help="回滚一次迁移")
    rollback_parser.add_argument("run_id")
    rollback_parser.add_argument("--storage-id", action="append", default=[])
    finalize_parser = subparsers.add_parser("finalize", help="验收后清理保留的旧代")
    finalize_parser.add_argument("run_id")
    args = parser.parse_args()

    registry = KnowledgeBaseRegistry()
    runtime = default_state_runtime()
    runner = IndexMigrationRunner(
        knowledge_store=runtime.knowledge_store,
        refresh_derived_knowledge=runtime.refresh_derived_knowledge_index,
    )
    if args.command == "scan":
        result = runner.plan(registry.list())
    elif args.command == "run":
        result = runner.run(
            registry.list(), include_current=args.include_current, progress=_progress
        )
    elif args.command == "rollback":
        result = runner.rollback(
            args.run_id, storage_ids=args.storage_id, progress=_progress
        )
    else:
        result = runner.finalize(args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "completed_with_failures" else 0


if __name__ == "__main__":
    raise SystemExit(main())
