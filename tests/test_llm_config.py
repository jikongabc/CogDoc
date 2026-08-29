import os
import stat

import pytest
from dotenv import dotenv_values

from cogdoc.config import llm_config


def test_upsert_env_values_preserves_literals_and_enforces_private_mode(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep\nOTHER=value\nLLM_API_KEY=old\n", encoding="utf-8")
    path.chmod(0o664)

    llm_config.upsert_env_values(
        {
            "LLM_API_KEY": " key#with spaces'and\\slashes ",
            "LLM_MODEL_NAME": "model#1",
        },
        env_path=path,
    )

    values = dotenv_values(path)
    assert values["LLM_API_KEY"] == " key#with spaces'and\\slashes "
    assert values["LLM_MODEL_NAME"] == "model#1"
    assert values["OTHER"] == "value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".env.*.tmp"))


def test_upsert_env_values_rejects_values_dotenv_would_expand(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY='old'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="variable expansion"):
        llm_config.upsert_env_values(
            {"LLM_API_KEY": "key-${HOME}"},
            env_path=path,
        )

    assert dotenv_values(path)["LLM_API_KEY"] == "old"


def test_apply_llm_config_does_not_copy_secret_into_process_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / ".env"
    monkeypatch.setattr(llm_config, "ENV_PATH", path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    llm_config.apply_llm_config(api_key="secret-value")

    assert os.environ.get("LLM_API_KEY") is None
    assert dotenv_values(path)["LLM_API_KEY"] == "secret-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
