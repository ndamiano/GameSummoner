import logging
import re

_SECRET_PARAM = re.compile(r"([?&](?:token|t)=)[^&\s\"]+")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1[redacted]", text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True
