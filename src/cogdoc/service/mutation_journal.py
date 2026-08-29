import json
import os
from threading import Lock
from cogdoc.config.settings import get_settings
from cogdoc.service.durable_io import atomic_write_json, atomic_write_text, durable_remove

OP_UPLOAD = "upload"
OP_DELETE_DOC = "delete_doc"


# 表示 MutationJournalError 异常。
class MutationJournalError(RuntimeError):
    pass


# 源文件 mutation 的崩溃恢复日志；每个 job 一个 JSON 条目，记录目标/备份路径与所属 generation；提交点是 state.json 的 switch_active（tmp+rename 原子写）；恢复时直接读 state.json 判定真值： 条目的 gen_id == active → 已提交，前滚保留新文件；否则未提交，回滚旧文件；两文件无需各自原子。
class MutationJournal:
    # 源文件 mutation 的崩溃恢复日志；每个 job 一个 JSON 条目，记录目标/备份路径与所属 generation；提交点是 state.json 的 switch_active（tmp+rename 原子写）；恢复时直接读 state.json 判定真值： 条目的 gen_id == active → 已提交，前滚保留新文件；否则未提交，回滚旧文件；两文件无需各自原子。
    def __init__(self, journal_dir: str | None = None):
        self._dir = journal_dir or os.path.join(
            get_settings().kb_root, "mutation_journal"
        )
        self._degraded_path = os.path.join(self._dir, ".degraded")
        self._lock = Lock()

    # 完成 路径 处理。
    def _path(self, job_id: str) -> str:
        return os.path.join(self._dir, f"{job_id}.json")

    # 写入结果。
    def _write(self, job_id: str, entry: dict) -> None:
        atomic_write_json(self._path(job_id), entry)

    # 完成 begin上传 处理。
    def begin_upload(
        self, job_id: str, kb_id: str, dest: str, backup: str, had_old: bool
    ) -> None:
        with self._lock:
            self._write(
                job_id,
                {
                    "op": OP_UPLOAD,
                    "kb_id": kb_id,
                    "dest": dest,
                    "backup": backup,
                    "had_old": had_old,
                    "source_moved": False,
                    "gen_id": None,
                    "committed": False,
                    "rolled_back": False,
                },
            )

    # 完成 begin删除 处理。
    def begin_delete(self, job_id: str, kb_id: str, dest: str, backup: str) -> None:
        with self._lock:
            self._write(
                job_id,
                {
                    "op": OP_DELETE_DOC,
                    "kb_id": kb_id,
                    "dest": dest,
                    "backup": backup,
                    "had_old": True,
                    "source_moved": False,
                    "gen_id": None,
                    "committed": False,
                    "rolled_back": False,
                },
            )

    # 标记来源moved。
    def mark_source_moved(self, job_id: str) -> None:
        # 原文件已进入 backup/quarantine 后立刻持久化。恢复时若该标记为真但备份缺失， 不能把当前磁盘状态猜成安全，必须保留 journal 并 fail-closed。
        with self._lock:
            with open(self._path(job_id), encoding="utf-8") as f:
                entry = json.load(f)
            entry["source_moved"] = True
            self._write(job_id, entry)

    # 记录索引代。
    def record_generation(self, job_id: str, gen_id: str) -> None:
        # 提交前（switch_active 之前）记录待提交 gen_id；写失败抛出，调用方据此中止提交以保持一致。
        with self._lock:
            with open(self._path(job_id), encoding="utf-8") as f:
                entry = json.load(f)
            entry["gen_id"] = gen_id
            self._write(job_id, entry)

    # 标记committed。
    def mark_committed(self, job_id: str) -> bool:
        # switch_active 成功后写入不可逆的 committed 标记：恢复时据此前滚，绝不因 active 已切代而误回滚。
        with self._lock:
            try:
                with open(self._path(job_id), encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, json.JSONDecodeError):
                return False
            entry["committed"] = True
            try:
                self._write(job_id, entry)
            except OSError:
                return False
            return True

    # 标记rolledback。
    def mark_rolled_back(self, job_id: str) -> bool:
        # 磁盘已恢复到 mutation 前状态后写入；clear 失败时，启动恢复据此只需删除残留条目。
        with self._lock:
            try:
                with open(self._path(job_id), encoding="utf-8") as f:
                    entry = json.load(f)
                entry["rolled_back"] = True
                self._write(job_id, entry)
            except (OSError, json.JSONDecodeError):
                return False
            return True

    # 清理。
    def clear(self, job_id: str) -> bool:
        with self._lock:
            try:
                durable_remove(self._path(job_id))
                return True
            except FileNotFoundError:
                return True
            except OSError:
                return False

    def clear_kb(self, kb_id: str) -> int:
        """Discard every recovery capability for a permanently deleted KB.

        The caller must already have fenced the KB against writes. A malformed
        journal cannot be safely attributed to another KB, so fail closed
        instead of letting a same-name incarnation inherit a global blocker.
        """

        with self._lock:
            if os.path.exists(self._degraded_path):
                raise MutationJournalError(
                    f"mutation journal 处于 degraded 状态，需人工恢复: {self._dir}"
                )
            try:
                names = os.listdir(self._dir)
            except FileNotFoundError:
                return 0
            matched: list[str] = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self._dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        entry = json.load(f)
                except (OSError, json.JSONDecodeError) as exc:
                    raise MutationJournalError(
                        f"mutation journal 无法安全清理: {name}"
                    ) from exc
                if not _valid_entry(entry):
                    raise MutationJournalError(
                        f"mutation journal 记录非法，无法安全清理: {name}"
                    )
                if entry.get("kb_id") == kb_id:
                    matched.append(path)
            removed = 0
            for path in matched:
                try:
                    durable_remove(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise MutationJournalError(
                        f"mutation journal 无法删除: {os.path.basename(path)}"
                    ) from exc
                removed += 1
            return removed

    # 判断是否存在 entries。
    def has_entries(self, kb_id: str) -> bool:
        with self._lock:
            if os.path.exists(self._degraded_path):
                return True
            try:
                names = os.listdir(self._dir)
            except FileNotFoundError:
                return False
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self._dir, name), encoding="utf-8") as f:
                        entry = json.load(f)
                except (OSError, json.JSONDecodeError):
                    return True
                if entry.get("kb_id") == kb_id:
                    if not _valid_entry(entry):
                        return True
                    # 终态条目只表示 journal 文件清理失败，源目录已确定一致，不应阻塞后续 mutation。
                    if (
                        entry.get("committed") is True
                        or entry.get("rolled_back") is True
                    ):
                        continue
                    return True
            return False

    # 恢复all。
    def recover_all(self) -> list[str]:
        # 启动期回放所有残留条目，返回已处理的 job_id 列表供日志。
        with self._lock:
            if os.path.exists(self._degraded_path):
                raise MutationJournalError(
                    f"mutation journal 处于 degraded 状态，需人工恢复: {self._dir}"
                )
            try:
                names = os.listdir(self._dir)
            except FileNotFoundError:
                return []
            recovered = []
            unresolved = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self._dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        entry = json.load(f)
                except OSError:
                    unresolved.append(name)
                    continue
                except json.JSONDecodeError:
                    _quarantine(path)  # 损坏改名留存供取证，不静默删除
                    _mark_degraded(self._degraded_path)
                    unresolved.append(name)
                    continue
                if not _valid_entry(entry):
                    _quarantine(path)  # 结构损坏（缺字段/类型错）同样隔离
                    _mark_degraded(self._degraded_path)
                    unresolved.append(name)
                    continue
                # 已终态条目（上次已定方向、仅 clear 失败而残留）：仍执行 _recover_entry 做幂等清理 （rolled_back→no-op、committed→前滚清孤儿备份），但绝不重写方向标记——否则 KB 此刻 处于 deleting/deleted 时会把 rolled_back 又判成 committed，写出互斥双终态致下次判损坏。
                already_terminal = (
                    entry.get("committed") is True or entry.get("rolled_back") is True
                )
                # 恢复成功才删条目；失败保留，下次启动重试，不丢回滚依据。
                committed = _is_committed(entry)
                if _recover_entry(entry):
                    if not already_terminal:
                        # 非终态才写入恢复结论；写后即便 journal 删除失败，下次启动也只会重放同一方向。
                        entry["committed" if committed else "rolled_back"] = True
                        try:
                            self._write(name[:-5], entry)
                        except OSError:
                            unresolved.append(name)
                            continue
                    if _silent_remove(path):
                        recovered.append(name[:-5])
                    else:
                        unresolved.append(name)
                else:
                    unresolved.append(name)
            if unresolved:
                raise MutationJournalError(
                    f"mutation journal 存在未恢复条目: {', '.join(unresolved)}"
                )
            return recovered


# 完成 合法性条目 处理。
def _valid_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    op = entry.get("op")
    gen_id = entry.get("gen_id")
    return (
        op in (OP_UPLOAD, OP_DELETE_DOC)
        and isinstance(entry.get("kb_id"), str)
        and bool(entry["kb_id"])
        and isinstance(entry.get("dest"), str)
        and bool(entry["dest"])
        and isinstance(entry.get("backup"), str)
        and bool(entry["backup"])
        and isinstance(entry.get("had_old"), bool)
        and isinstance(entry.get("source_moved", False), bool)
        and (gen_id is None or isinstance(gen_id, str))
        and isinstance(entry.get("committed", False), bool)
        and isinstance(entry.get("rolled_back", False), bool)
        and not (
            entry.get("committed", False) is True
            and entry.get("rolled_back", False) is True
        )
        and (op != OP_DELETE_DOC or entry.get("had_old") is True)
        and (gen_id is None or bool(gen_id))
    )


# 隔离结果。
def _quarantine(path: str) -> None:
    import time

    try:
        os.replace(path, f"{path}.corrupt-{time.time_ns()}")
    except OSError:
        pass


# 标记降级状态。
def _mark_degraded(path: str) -> None:
    try:
        atomic_write_text(path, "1")
    except OSError:
        pass


# 判断 committed 是否成立。
def _is_committed(entry: dict) -> bool:
    # 已提交判定：不可逆 committed 标记优先（即便 active 已切代/KB 已删也判为已提交，杜绝误回滚）； 否则回退到 gen_id == 当前 active（覆盖 switch_active 与写 committed 标记之间的崩溃窗口）。
    if entry.get("committed") is True:
        return True
    gen_id = entry.get("gen_id")
    if not gen_id:
        return False
    try:
        from cogdoc.service.kb_state import KBState, KBStateCorruptError

        if KBState(entry["kb_id"]).generation_is_committed(gen_id):
            return True
    except KBStateCorruptError as exc:
        raise MutationJournalError(
            "KB generation state is unreadable; mutation recovery is paused"
        ) from exc
    # KB 已进入删除流程时绝不能因 state 目录已移除而恢复源文件，否则会复活已删除内容。
    try:
        from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store

        return shared_lifecycle_store().status(entry["kb_id"]) != LIFECYCLE_ACTIVE
    except Exception:
        return False


# 恢复条目。
def _recover_entry(entry: dict) -> bool:
    # 据 state.json 真值把源文件恢复到与 active 代一致。返回是否恢复成功；失败保留 journal 供下次重试。
    if entry.get("rolled_back") is True:
        return True
    dest = entry.get("dest")
    backup = entry.get("backup")
    if not dest or not backup:
        return True  # 损坏条目无可恢复对象，直接丢弃
    committed = _is_committed(entry)
    had_old = entry.get("had_old", False)
    source_moved = entry.get("source_moved", False)
    op = entry.get("op")

    if op == OP_DELETE_DOC:
        if committed:
            _silent_remove(
                backup
            )  # 前滚：删除确认，清隔离文件（孤儿无害，best-effort）
            return True
        if os.path.exists(backup):
            return _safe_replace(backup, dest)  # 回滚：恢复被删文件，失败保留 journal
        # 明确记过移动却找不到备份时无法证明源文件一致，保留 journal 等人工/后续恢复。
        return not source_moved

    # 上传。
    if committed:
        if had_old and os.path.exists(backup):
            _silent_remove(
                backup
            )  # 前滚：新文件保留，清旧备份（孤儿无害，best-effort）
        return True
    if had_old:
        if os.path.exists(backup):
            return _safe_replace(backup, dest)  # 回滚：恢复旧文件，失败保留 journal
        return not source_moved
    return _silent_remove(
        dest
    )  # 回滚：删除未提交的新文件；删除失败保留 journal 下次重试


# 完成 silentremove 处理。
def _silent_remove(path: str) -> bool:
    # 成功或本就不存在返回 True；删除失败返回 False，供调用方据此保留 journal。
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# 完成 safereplace 处理。
def _safe_replace(src: str, dst: str) -> bool:
    try:
        os.replace(src, dst)
        return True
    except OSError:
        return False


_shared: MutationJournal | None = None
_shared_lock = Lock()


# 完成 sharedmutation变更日志 处理。
def shared_mutation_journal() -> MutationJournal:
    # 进程内共享单例；双重检查锁防并发重复构造。
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = MutationJournal()
    return _shared
