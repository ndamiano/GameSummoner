import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn

if __name__ == "__main__":
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["default"]["fmt"] = "%(levelname)s:%(name)s:%(message)s"
    log_config["formatters"]["access"]["fmt"] = "%(levelname)s:%(name)s:%(message)s"
    log_config["filters"] = {"redact_tokens": {"()": "api.log_redaction.RedactTokens"}}
    for handler in log_config["handlers"].values():
        handler["filters"] = ["redact_tokens"]

    # propagate=False: these handlers are uvicorn's own, so propagating logs every line twice.
    for logger_name in ["tools", "maestro", "llm_clients"]:
        log_config["loggers"][logger_name] = {"handlers": ["default"], "level": "INFO", "propagate": False}

    # reload is the dev auto-reloader (file watcher, extra processes) — off by default so the
    # public/prod launch is a plain single worker; set MAESTRO_DEV=1 for local iteration.
    dev = os.getenv("MAESTRO_DEV") == "1"
    uvicorn.run(
        "api.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=dev,
        log_level="info",
        log_config=log_config
    )
