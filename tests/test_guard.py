import pytest

from desk import guard
from desk.config import Config, Environment


def test_environment_enum_has_only_sim():
    assert [e.value for e in Environment] == ["SIM"]


def test_live_env_var_is_fatal(monkeypatch):
    monkeypatch.setenv("SAXO_ALLOW_LIVE", "1")   # saxo-mcp's own live switch
    cfg = Config.model_construct(environment=Environment.SIM)
    with pytest.raises(guard.GuardFailure):
        guard.check_environment(cfg)


def _summary(keys=("A",), trading="DISABLED (hard block)"):
    return {"trading": trading, "client": {"DefaultAccountKey": keys[0]},
            "accounts": [{"AccountKey": k} for k in keys]}


def test_summary_ok(cfg):
    assert guard.check_summary(cfg, _summary(("A",)), {"A"}) == "A"


def test_summary_rejects_empty_allowlist(cfg):
    with pytest.raises(guard.GuardFailure, match="allowlist"):
        guard.check_summary(cfg, _summary(), set())


def test_summary_rejects_unlisted_account(cfg):
    with pytest.raises(guard.GuardFailure, match="not in SIM allowlist"):
        guard.check_summary(cfg, _summary(("A", "B")), {"A"})


def test_summary_rejects_trading_enabled(cfg):
    with pytest.raises(guard.GuardFailure, match="trading"):
        guard.check_summary(cfg, _summary(trading="ENABLED (SAXO_TRADING=enabled)"), {"A"})


def test_summary_rejects_missing_accounts(cfg):
    with pytest.raises(guard.GuardFailure):
        guard.check_summary(cfg, {"trading": "DISABLED", "accounts": []}, {"A"})
