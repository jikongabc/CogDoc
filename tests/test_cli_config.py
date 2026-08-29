from types import SimpleNamespace

import cogdoc.cli as cli_module
from cogdoc.config import llm_config


def test_cloud_configuration_reads_api_key_without_terminal_echo(monkeypatch):
    console = cli_module.Console.__new__(cli_module.Console)
    answers = iter(["https://api.example.com/v1", "model-v1"])
    captured = {}

    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://old.example.com/v1",
            llm_model_name="old-model",
            llm_api_key="old-secret",
        ),
    )

    def hidden_key(prompt):
        captured["prompt"] = prompt
        return "new-secret"

    monkeypatch.setattr(
        cli_module.getpass,
        "getpass",
        hidden_key,
    )
    monkeypatch.setattr(
        llm_config,
        "apply_llm_config",
        lambda **values: captured.setdefault("values", values),
    )

    assert console._configure_cloud(first_time=False) is True
    assert "API Key" in captured["prompt"]
    assert captured["values"] == {
        "api_key": "new-secret",
        "base_url": "https://api.example.com/v1",
        "model": "model-v1",
    }
