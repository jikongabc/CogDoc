from __future__ import annotations

import hashlib


MUTATION_BACKUP_SUFFIX = ".cogdoc-bak"


def mutation_backup_path(source_path: str, mutation_id: str) -> str:
    """Return a collision-resistant backup path with an unambiguous suffix token."""
    if not mutation_id:
        raise ValueError("mutation_id must not be empty")
    token = hashlib.sha256(mutation_id.encode("utf-8")).hexdigest()
    return f"{source_path}.{token}{MUTATION_BACKUP_SUFFIX}"


def original_name_from_backup(filename: str) -> str:
    """Recover a logical source name from current and legacy backup filenames."""
    if not filename.endswith(MUTATION_BACKUP_SUFFIX):
        return filename
    without_suffix = filename[: -len(MUTATION_BACKUP_SUFFIX)]
    original_name, separator, mutation_token = without_suffix.rpartition(".")
    if separator and original_name and mutation_token:
        return original_name
    return filename
