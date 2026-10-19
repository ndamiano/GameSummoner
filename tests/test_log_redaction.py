import logging

import pytest

from api.log_redaction import RedactTokens

CASES = [
    ('%s - "WebSocket %s" [accepted]', ("172.18.0.1:48942", "/api/ws?token=Ul2j32LylChDuPN0Zoc_Dru"),
     '172.18.0.1:48942 - "WebSocket /api/ws?token=[redacted]" [accepted]'),
    ('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5", "GET", "/handoff?t=abc123&next=/g/x", "1.1", 302),
     '10.0.0.1:5 - "GET /handoff?t=[redacted]&next=/g/x HTTP/1.1" 302'),
    ('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5", "GET", "/api/games?limit=20&offset=0", "1.1", 200),
     '10.0.0.1:5 - "GET /api/games?limit=20&offset=0 HTTP/1.1" 200'),
    ("connection to /api/ws?token=secret-value closed", None,
     "connection to /api/ws?token=[redacted] closed"),
]


@pytest.mark.parametrize("msg,args,expected", CASES)
def test_logged_request_lines_never_carry_the_session_or_handoff_token(msg, args, expected):
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)
    assert RedactTokens().filter(record) is True
    assert record.getMessage() == expected
