import json
import mimetypes
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DATABASE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts.sqlite3")


class ArtifactsService:
    """Classifies files and keeps their latest metadata in a local SQLite file."""

    TYPE_NAMES = {
        ".py": "Python source file",
        ".js": "JavaScript source file",
        ".ts": "TypeScript source file",
        ".rb": "Ruby source file",
        ".pl": "Perl source file",
        ".sh": "Shell script",
        ".bat": "Windows batch script",
        ".json": "JSON data file",
        ".yaml": "YAML data file",
        ".yml": "YAML data file",
        ".md": "Markdown document",
        ".txt": "Text document",
    }

    def __init__(self, database_path=None):
        self.database_path = os.path.abspath(database_path or DATABASE_FILE)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    metadata TEXT NOT NULL
                )
            """)

    @contextmanager
    def _connect(self):
        # Each call has its own connection, so API and scanner threads can use it.
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path))

    def is_storage_file(self, path):
        # The scanner must never register its own database or SQLite sidecars.
        return self._path_key(path) in {
            self._path_key(self.database_path + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")
        }

    def classify(self, file_record, change_type, previous_path=None):
        path = os.path.abspath(file_record["path"])
        extension = os.path.splitext(path)[1].lower()
        mime_type, _ = mimetypes.guess_type(path)

        artifact = {
            "name": os.path.basename(path),
            "path": path,
            "type": self.TYPE_NAMES.get(extension, "Unknown file type"),
            "extension": extension or None,
            "mime_type": mime_type or "application/octet-stream",
            "size_bytes": file_record["size"],
            "size_display": self._format_size(file_record["size"]),
            "hash": file_record["hash"],
            "modified": file_record["modified"],
            "change_type": change_type,
            "previous_path": previous_path,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }

        with self._connect() as connection:
            # Keep identity lookup and the update in one transaction.
            connection.execute("BEGIN IMMEDIATE")
            existing = None
            if previous_path:
                existing = connection.execute(
                    "SELECT id FROM artifacts WHERE path = ?",
                    (self._path_key(previous_path),),
                ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT id FROM artifacts WHERE path = ?",
                    (self._path_key(path),),
                ).fetchone()
            artifact["id"] = existing[0] if existing else str(uuid.uuid4())
            # A move onto an existing destination replaces its old record.
            connection.execute(
                "DELETE FROM artifacts WHERE path = ? AND id != ?",
                (self._path_key(path), artifact["id"]),
            )
            connection.execute("""
                INSERT INTO artifacts (id, path, metadata) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET path = excluded.path, metadata = excluded.metadata
            """, (artifact["id"], self._path_key(path), json.dumps(artifact)))
        return artifact

    def remove(self, path):
        with self._connect() as connection:
            connection.execute("DELETE FROM artifacts WHERE path = ?", (self._path_key(path),))

    def list_artifacts(self):
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM artifacts ORDER BY rowid").fetchall()
        items = [json.loads(row[0]) for row in rows]
        return {"items": items, "count": len(items)}

    def get_artifact(self, artifact_id: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is not None:
            return json.loads(row[0])
        return {"id": artifact_id, "message": "Artifact not found."}

    @staticmethod
    def _format_size(size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
