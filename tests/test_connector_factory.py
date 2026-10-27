import pytest

import llm_clients.connector as mod
from config.llm_models import UnknownModel
from llm_clients.connector import LLMConnector


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(mod, "_connectors", {})


def test_each_entry_gets_its_own_connector_from_its_own_fields(llm_entries):
    llm_entries["deepseek-4.1"] = {"model": "deepseek-v4.1-flash", "n_ctx": 131072,
                                   "max_tokens": 900, "reasoning": "off",
                                   "api": {"base_url": "u", "usd_per_mtok": {}}}
    qwen, deepseek = mod.get_connector("qwen-flash-next"), mod.get_connector("deepseek-4.1")
    assert type(deepseek) is LLMConnector
    assert [(c.entry, c.model_name, c.n_ctx, c.max_tokens, c.reasoning, c.queue)
            for c in (qwen, deepseek)] == [
        ("qwen-flash-next", "qwen3.8-flash-next-basalt", 32768, 50000, "medium", "llm"),
        ("deepseek-4.1", "deepseek-v4.1-flash", 131072, 900, "none", "llm")]
    assert mod.get_connector("deepseek-4.1") is deepseek
    payload, entry = deepseek.build_llm_job([{"role": "user", "content": "hi"}])
    assert (payload["body"]["model"], entry) == ("deepseek-v4.1-flash", "deepseek-4.1")


def test_job_timeout_comes_from_the_workqueue_block(monkeypatch):
    monkeypatch.setitem(mod.settings_manager._settings, "workqueue", {"job_timeout_seconds": 42})
    assert mod.get_connector("qwen-flash-next").job_timeout_seconds == 42


def test_a_name_settings_lack_is_an_unknown_model():
    with pytest.raises(UnknownModel, match="no llm entry 'opus' in settings"):
        mod.get_connector("opus")
