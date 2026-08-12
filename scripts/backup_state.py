import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cogdoc.config.settings import get_settings


DEFAULT_BACKUP_DIR = ROOT / "backups"
MANIFEST_NAME = "backup_manifest.json"
MANIFEST_VERSION = "v2"


def _default_backup_dir() -> Path:
    """Return the CLI default without changing the source-tree default.

    Container images set ``COGDOC_BACKUP_DIR`` to a directory on the persisted
    data volume.  Source checkouts that do not export it keep writing to the
    historical ``<repo>/backups`` directory.
    """

    configured = os.environ.get("COGDOC_BACKUP_DIR", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_BACKUP_DIR


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _created_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arcname(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(ROOT)
        value = relative.as_posix()
    except ValueError:
        value = path.name
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
        raise ValueError(f"无法生成安全的归档路径: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_paths(
    *, include_traces: bool, include_env: bool, extra_paths: Iterable[Path]
) -> list[Path]:
    settings = get_settings()
    paths = [Path(settings.cogdoc_data_dir)]
    if include_traces:
        paths.append(Path(settings.cogdoc_trace_dir))
    if include_env:
        paths.append(ROOT / ".env")
    paths.extend(extra_paths)

    resolved: list[Path] = []
    seen: set[Path] = set()
    env_path = (ROOT / ".env").resolve()
    for path in paths:
        candidate = path if path.is_absolute() else ROOT / path
        candidate = candidate.resolve()
        if candidate == env_path and not include_env:
            continue
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        resolved.append(candidate)
    return resolved


def _payload_entries(
    paths: list[Path],
    *,
    include_env: bool,
    excluded_paths: Iterable[Path] = (),
) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    env_path = (ROOT / ".env").resolve()
    exclusions = tuple(path.absolute() for path in excluded_paths)
    for root in paths:
        root_name = _arcname(root)
        candidates = [root] if root.is_file() else [root, *sorted(root.rglob("*"))]
        for source in candidates:
            absolute_source = source.absolute()
            if any(
                absolute_source == excluded or excluded in absolute_source.parents
                for excluded in exclusions
            ):
                continue
            if source.is_symlink():
                raise ValueError(f"备份不允许符号链接: {source}")
            if source.resolve() == env_path and not include_env:
                continue
            relative = source.relative_to(root).as_posix()
            archive_name = root_name if relative == "." else f"{root_name}/{relative}"
            if source.is_dir():
                if archive_name in files:
                    raise ValueError(f"归档路径冲突: {archive_name}")
                directories.add(archive_name)
            elif source.is_file():
                if archive_name in directories:
                    raise ValueError(f"归档路径冲突: {archive_name}")
                previous = files.setdefault(archive_name, source)
                if previous != source:
                    raise ValueError(f"归档路径冲突: {archive_name}")
            else:
                raise ValueError(f"不支持的状态文件类型: {source}")
    return files, directories


def _build_manifest(
    paths: list[Path],
    archive_name: str,
    include_env: bool,
    include_traces: bool,
    files: dict[str, Path],
    directories: set[str],
    created_at: str,
) -> dict:
    settings = get_settings()
    file_items = [
        {
            "path": name,
            "size_bytes": source.stat().st_size,
            "sha256": _sha256(source),
            "created_at": created_at,
        }
        for name, source in sorted(files.items())
    ]
    return {
        "schema_version": MANIFEST_VERSION,
        "created_at": created_at,
        "archive": archive_name,
        "source": {
            "project_root": str(ROOT.resolve()),
            "configuration": {
                "data_dir": str(Path(settings.cogdoc_data_dir)),
                "trace_dir": str(Path(settings.cogdoc_trace_dir)),
            },
            "roots": [
                {
                    "archive_path": _arcname(path),
                    "source_path": str(path),
                    "type": "directory" if path.is_dir() else "file",
                }
                for path in paths
            ],
        },
        "options": {
            "includes_env": include_env and any(name == ".env" for name in files),
            "includes_traces": include_traces,
        },
        "file_count": len(file_items),
        "total_size_bytes": sum(item["size_bytes"] for item in file_items),
        "files": file_items,
        "directories": sorted(directories),
    }


def build_manifest(paths: list[Path], archive_name: str, include_env: bool) -> dict:
    files, directories = _payload_entries(paths, include_env=include_env)
    return _build_manifest(
        paths,
        archive_name,
        include_env,
        False,
        files,
        directories,
        _created_at(),
    )


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o640
    info.mtime = mtime
    archive.addfile(info, io.BytesIO(data))


def create_backup(
    output_dir: Path,
    *,
    name: str | None,
    include_traces: bool,
    include_env: bool,
    extra_paths: list[Path],
) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_name = name or f"cogdoc-backup-{_timestamp()}.tar.gz"
    if not archive_name.endswith(".tar.gz"):
        archive_name = f"{archive_name}.tar.gz"
    archive_path = (output_dir / archive_name).resolve()
    if archive_path.exists():
        raise FileExistsError(f"备份文件已存在: {archive_path}")

    paths = collect_paths(
        include_traces=include_traces,
        include_env=include_env,
        extra_paths=extra_paths,
    )
    if not paths:
        raise FileNotFoundError("没有找到可备份的路径")
    if any(path.is_dir() and path.resolve() == output_dir for path in paths):
        raise ValueError("备份输出目录不能与状态根目录相同")
    nested_output = [
        output_dir
        for path in paths
        if path.is_dir() and output_dir.is_relative_to(path.resolve())
    ]
    source_files, directories = _payload_entries(
        paths,
        include_env=include_env,
        excluded_paths=nested_output,
    )
    created_at = _created_at()
    mtime = int(datetime.now(timezone.utc).timestamp())

    with tempfile.TemporaryDirectory(prefix=".cogdoc-backup-", dir=output_dir) as tmp:
        temporary = Path(tmp)
        payload = temporary / "payload"
        for directory in sorted(directories):
            (payload / directory).mkdir(parents=True, exist_ok=True)
        snapshot_files: dict[str, Path] = {}
        for archive_name_in_payload, source in sorted(source_files.items()):
            destination = payload / archive_name_in_payload
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            snapshot_files[archive_name_in_payload] = destination

        manifest = _build_manifest(
            paths,
            archive_path.name,
            include_env,
            include_traces,
            snapshot_files,
            directories,
            created_at,
        )
        partial = temporary / archive_path.name
        with tarfile.open(partial, "w:gz") as archive:
            _add_bytes(
                archive,
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                mtime,
            )
            for directory in sorted(directories):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o750
                info.mtime = mtime
                archive.addfile(info)
            for archive_name_in_payload, source in sorted(snapshot_files.items()):
                with source.open("rb") as stream:
                    info = tarfile.TarInfo(archive_name_in_payload)
                    info.size = source.stat().st_size
                    info.mode = 0o640
                    info.mtime = mtime
                    archive.addfile(info, stream)
        os.replace(partial, archive_path)
    return archive_path


def print_summary(archive_path: Path, *, json_output: bool = False) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "ok": True,
                    "operation": "backup",
                    "archive": str(archive_path),
                    "size_bytes": archive_path.stat().st_size,
                    "schema_version": MANIFEST_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    size_mb = archive_path.stat().st_size / 1024 / 1024
    print(f"备份完成: {archive_path}")
    print(f"大小: {size_mb:.2f} MB")
    print("恢复提示: 停止服务后在项目根目录解压，再运行 make check 和 make smoke-api")


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 CogDoc 本地运行状态")
    parser.add_argument("--output-dir", type=Path, default=_default_backup_dir())
    parser.add_argument("--name", default=None)
    parser.add_argument("--include-traces", action="store_true", default=True)
    parser.add_argument("--no-traces", action="store_false", dest="include_traces")
    parser.add_argument("--include-env", action="store_true")
    parser.add_argument("--extra", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()
    try:
        archive_path = create_backup(
            args.output_dir,
            name=args.name,
            include_traces=args.include_traces,
            include_env=args.include_env,
            extra_paths=args.extra,
        )
        print_summary(archive_path, json_output=args.json)
        return 0
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "operation": "backup",
                        "error": {"code": "BACKUP_FAILED", "message": str(exc)},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"备份失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
