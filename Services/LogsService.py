import json
import logging
from pathlib import Path
import re
import threading
from datetime import datetime, timezone


LOG_FILE = Path(__file__).resolve().parents[1] / "service.log"
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_WRITE_LOCK = threading.RLock()
_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?) "
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]"
    r"(?: \[(?P<source>[^\]\r\n]+)\])? (?P<message>.*)$"
)


class UTCLogFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")


def _formatter():
    return UTCLogFormatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")


def configure_logging(log_file=LOG_FILE):
    """Capture project loggers without changing unrelated library loggers."""
    project_logger = logging.getLogger("code-steward")
    project_logger.setLevel(logging.INFO)
    project_logger.propagate = False
    target = str(Path(log_file).resolve())
    for handler in project_logger.handlers:
        if getattr(handler, "_code_steward_log_file", None) == target:
            return
    file_handler = logging.FileHandler(target, encoding="utf-8", errors="backslashreplace")
    file_handler.lock = _WRITE_LOCK
    file_handler._code_steward_log_file = target
    file_handler.setFormatter(_formatter())
    project_logger.addHandler(file_handler)
    if not any(getattr(handler, "_code_steward_console", False) for handler in project_logger.handlers):
        console = logging.StreamHandler()
        console._code_steward_console = True
        console.setFormatter(_formatter())
        project_logger.addHandler(console)


class LogsService:
    """Read persisted application logs, including the original plain-text format."""

    def __init__(self, log_file=None):
        self.log_file = Path(log_file) if log_file is not None else LOG_FILE

    def append_log(self, level: str, message: str, context=None):
        level = level.upper()
        if level not in LOG_LEVELS:
            raise ValueError("Unsupported log level")
        if context is not None:
            message += " | context=" + json.dumps(context, ensure_ascii=False, default=str)
        record = logging.LogRecord("code-steward.logs", getattr(logging, level), "", 0, message, (), None)
        rendered = _formatter().format(record)
        with _WRITE_LOCK:
            with self.log_file.open("a", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        entry = self._parse_header(rendered.splitlines()[0])
        entry["message"] = message
        return entry

    @staticmethod
    def _reverse_lines(stream):
        """Read from the end in chunks instead of loading the entire log file."""
        position = stream.seek(0, 2)
        remainder = b""
        while position > 0:
            size = min(8192, position)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + remainder).split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                yield line.rstrip(b"\r").decode("utf-8", errors="replace")
        if remainder:
            yield remainder.rstrip(b"\r").decode("utf-8", errors="replace")

    @staticmethod
    def _parse_header(line):
        match = _HEADER.match(line)
        if match is None:
            return None
        entry = match.groupdict()
        # Preserve legacy local timestamps without inventing a timezone.
        entry["timestamp"] = entry["timestamp"].replace(" ", "T").replace(",", ".")
        return entry

    def list_logs(self, limit: int = 100, level=None, search=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if level is not None:
            level = level.upper()
            if level not in LOG_LEVELS:
                raise ValueError("Unsupported log level")
        if search is not None and len(search) > 200:
            raise ValueError("search must not exceed 200 characters")
        needle = search.casefold() if search else None
        items = []
        try:
            with _WRITE_LOCK, self.log_file.open("rb") as stream:
                continuation = []
                for line in self._reverse_lines(stream):
                    entry = self._parse_header(line)
                    if entry is None:
                        continuation.append(line)
                        continue
                    if continuation:
                        entry["message"] += "\n" + "\n".join(reversed(continuation)).rstrip()
                        entry["message"] = entry["message"].rstrip()
                        continuation = []
                    if level is not None and entry["level"] != level:
                        continue
                    if needle and needle not in entry["message"].casefold():
                        continue
                    items.append(entry)
                    if len(items) == limit:
                        break
        except FileNotFoundError:
            pass
        return {"items": items, "count": len(items), "limit": limit, "level": level, "search": search}
