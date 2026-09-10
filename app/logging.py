import logging
import sys

from pythonjsonlogger import json as jsonlogger


def setup_logging(service_name: str, debug: bool = False) -> logging.Logger:
    """Configures structured JSON logging for Revix services."""
    root_logger = logging.getLogger()

    # Clear existing handlers to prevent duplicate lines
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if not debug:
        formatter = jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handler.setFormatter(formatter)

    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)

    return logging.getLogger(service_name)
