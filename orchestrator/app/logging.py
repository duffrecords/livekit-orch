"""JSON-lines logging on stdout, in the same shape the agent workers use."""

import json
import logging
import sys
from datetime import datetime, timezone

# attributes every LogRecord has (plus uvicorn's ANSI duplicate of the message);
# anything else was passed via `extra=`
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "message": record.getMessage(),
            "level": record.levelname,
            "name": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        entry["timestamp"] = datetime.fromtimestamp(record.created, timezone.utc).isoformat()
        return json.dumps(entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own plain-text handlers; route its loggers through ours
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # the app logs each request itself
    logging.getLogger("uvicorn.access").disabled = True
