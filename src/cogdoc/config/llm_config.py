from pathlib import Path

from cogdoc.config.settings import PROJECT_ROOT, get_settings
from cogdoc.service.durable_io import atomic_write_text

ENV_PATH = PROJECT_ROOT / ".env"


def _encode_env_value(value: str) -> str:
    """Encode one literal dotenv value without comments or whitespace loss."""

    if not isinstance(value, str) or any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError("environment value must be one line")
    # python-dotenv expands ${NAME} even inside quotes. Reject it rather than
    # silently persisting a different credential from the one the user entered.
    if "${" in value:
        raise ValueError("environment value cannot contain variable expansion")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# 增量写入环境变量值。
def upsert_env_values(updates: dict[str, str], env_path: Path | None = None) -> None:
    # 原子写：保留原有注释/空行/其他键，命中的键就地改值，未命中的追加到末尾。
    path = env_path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={_encode_env_value(remaining.pop(key))}")
                continue
        out.append(line)
    out.extend(f"{key}={_encode_env_value(value)}" for key, value in remaining.items())
    content = "\n".join(out) + "\n"
    atomic_write_text(str(path), content, mode=0o600)


# 应用LLM配置。
def apply_llm_config(
    *, api_key: str | None = None, base_url: str | None = None, model: str | None = None
) -> None:
    updates = {}
    if api_key is not None:
        updates["LLM_API_KEY"] = api_key
    if base_url is not None:
        updates["LLM_BASE_URL"] = base_url
    if model is not None:
        updates["LLM_MODEL_NAME"] = model
    if not updates:
        return
    upsert_env_values(updates)
    # Settings reloads the atomically replaced .env. Avoid copying the secret
    # into the process environment, where child processes could inherit it.
    # 清两处缓存：settings 的 lru_cache，与按 base_url+model 缓存的 LLM 客户端（键不含 key，只改 key 必须清）。
    get_settings.cache_clear()
    from cogdoc.agents.qa_generator import Generator

    Generator.clear_clients()
