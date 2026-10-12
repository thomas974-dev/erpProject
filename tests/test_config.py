import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERP_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("ERP_HTTP_TIMEOUT_SECONDS", "0.5")

    settings = Settings(_env_file=None)

    assert settings.erp_max_attempts == 5
    assert settings.erp_http_timeout_seconds == 0.5


def test_invalid_failure_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_ERP_FAILURE_MODE", "explode")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_non_positive_max_attempts_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERP_MAX_ATTEMPTS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
