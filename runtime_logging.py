import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_dashboard_access_logging(settings) -> str:
    """Route routine Werkzeug server/access messages to a rotating file."""

    log_path = Path(str(getattr(settings, "dashboard_access_log_path", "log/dashboard_access.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max(1, int(getattr(settings, "dashboard_access_log_max_bytes", 5_000_000))),
        backupCount=max(0, int(getattr(settings, "dashboard_access_log_backup_count", 3))),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    werkzeug_logger = logging.getLogger("werkzeug")
    for existing_handler in list(werkzeug_logger.handlers):
        werkzeug_logger.removeHandler(existing_handler)
        existing_handler.close()
    werkzeug_logger.addHandler(handler)
    werkzeug_logger.setLevel(logging.INFO)
    werkzeug_logger.propagate = False
    return str(log_path)
