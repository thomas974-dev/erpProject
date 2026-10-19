import json
import logging

import pytest

from app.core.logging import JsonFormatter, log_event, redact


def format_last(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    return json.loads(JsonFormatter().format(caplog.records[-1]))


def test_event_and_fields_become_json_keys(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.logging")
    with caplog.at_level(logging.INFO):
        log_event(logger, "order_created", order_id="abc", attempt=1, error=None)

    line = format_last(caplog)
    assert line["event"] == "order_created"
    assert line["level"] == "INFO"
    assert line["logger"] == "test.logging"
    assert (line["order_id"], line["attempt"]) == ("abc", 1)
    assert "error" not in line  # None fields are omitted
    assert "timestamp" in line


def test_log_location_points_to_the_caller(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("test.logging"), "where_am_i")

    assert caplog.records[-1].filename == "test_logging.py"


def test_plain_log_calls_still_work(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        logging.getLogger("celery.worker").info("Task %s received", "abc")

    assert format_last(caplog)["event"] == "Task abc received"


def test_exceptions_are_included(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.logging")
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("boom")
        except ValueError:
            log_event(logger, "failed", level=logging.ERROR, exc_info=True)

    assert "ValueError: boom" in str(format_last(caplog)["exception"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://user:p4ss@erp.local/x", "http://***@erp.local/x"),
        (
            "postgresql+psycopg://orders:secret@db:5432/orders",
            "postgresql+psycopg://***@db:5432/orders",
        ),
        ("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/0"),
        ("no url here", "no url here"),
    ],
)
def test_redact_masks_url_credentials(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_formatter_redacts_any_field(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("test.logging"), "oops", url="http://u:topsecret@h/")

    assert "topsecret" not in JsonFormatter().format(caplog.records[-1])
