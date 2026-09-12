"""
src/io.py — TRPA standard I/O helpers.

Provides:
    load_config()   — reads config.yaml from the repo root
    load_secrets()  — loads .env into os.environ
    get_logger()    — returns a configured logger (console + timestamped file)

Copy this file verbatim into src/io.py of any new TRPA pipeline repo.
"""
import logging
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    """Return the repo root (parent of src/)."""
    return Path(__file__).resolve().parent.parent


def load_config(path: str = "config.yaml") -> dict:
    """Load config.yaml from the project root."""
    config_path = project_root() / path
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_secrets() -> None:
    """Load .env into os.environ. Call before reading any secrets."""
    load_dotenv(project_root() / ".env")


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Return a configured logger that writes to console and a timestamped
    file in logs/<name>_<YYYY-MM-DD_HHMMSS>.log.

    Safe to call multiple times with the same name in a notebook —
    existing handlers are cleared so logs don't duplicate on re-run.
    """
    logs_dir = project_root() / "logs"
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = logs_dir / f"{name}_{timestamp}.log"

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.info(f"Log file: {log_file}")
    return logger
