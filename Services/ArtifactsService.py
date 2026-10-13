import json
import mimetypes
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DATABASE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts.sqlite3")

# Version 2 adds searchable columns next to the full JSON record. Older databases are upgraded on start.
SCHEMA_VERSION = 2
COLUMNS = {"hash": "TEXT", "size": "INTEGER", "mtime": "REAL", "artifact_type": "TEXT", "language": "TEXT",
           "sensitive": "INTEGER", "classifier_version": "INTEGER", "discovered_by": "TEXT"}
CLASSIFICATION_FIELDS = ("artifact_type", "language", "is_binary", "sensitive", "sensitive_reason", "confidence",
                         "classifier_version")


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
            self._upgrade(connection)

    @staticmethod
    def _upgrade(connection):
        """Add the version 2 columns and fill them from the saved JSON records."""
        if connection.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        existing = {row[1] for row in connection.execute("PRAGMA table_info(artifacts)")}
        for name, kind in COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {kind}")
        for artifact_id, metadata in connection.execute("SELECT id, metadata FROM artifacts").fetchall():
            artifact = json.loads(metadata)
            connection.execute("UPDATE artifacts SET hash = ?, size = ? WHERE id = ?",
                               (artifact.get("hash"), artifact.get("size_bytes"), artifact_id))
        connection.execute("CREATE INDEX IF NOT EXISTS artifacts_hash ON artifacts (hash)")
        connection.execute("CREATE INDEX IF NOT EXISTS artifacts_type ON artifacts (artifact_type)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
        classification = file_record.get("classification") or {}

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
            **{field: classification.get(field) for field in CLASSIFICATION_FIELDS},
            "classification_reasons": classification.get("reasons", []),
            "discovered_by": file_record.get("discovered_by"),
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
                INSERT INTO artifacts (id, path, metadata, hash, size, mtime, artifact_type, language, sensitive,
                                       classifier_version, discovered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET path = excluded.path, metadata = excluded.metadata,
                    hash = excluded.hash, size = excluded.size, mtime = excluded.mtime,
                    artifact_type = excluded.artifact_type, language = excluded.language,
                    sensitive = excluded.sensitive, classifier_version = excluded.classifier_version,
                    discovered_by = excluded.discovered_by
            """, (artifact["id"], self._path_key(path), json.dumps(artifact), artifact["hash"], artifact["size_bytes"],
                  file_record.get("mtime"), artifact["artifact_type"], artifact["language"],
                  None if artifact["sensitive"] is None else int(artifact["sensitive"]),
                  artifact["classifier_version"], artifact["discovered_by"]))
        return artifact

    def known_state(self, path):
        """What the scanner needs to decide whether a file changed, or None for an unknown path."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, hash, size, mtime, classifier_version FROM artifacts WHERE path = ?",
                (self._path_key(path),),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "hash", "size", "mtime", "classifier_version"), row))

    def touch(self, path, mtime, modified, classification=None):
        """Record a new modification time (and type) for a file whose contents did not change."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT metadata FROM artifacts WHERE path = ?", (self._path_key(path),)).fetchone()
            if row is None:
                return
            artifact = json.loads(row[0])
            artifact["modified"] = modified
            if classification:
                artifact.update({field: classification.get(field) for field in CLASSIFICATION_FIELDS})
                artifact["classification_reasons"] = classification.get("reasons", [])
            connection.execute("""
                UPDATE artifacts SET metadata = ?, mtime = ?, artifact_type = ?, language = ?, sensitive = ?,
                    classifier_version = ? WHERE path = ?
            """, (json.dumps(artifact), mtime, artifact.get("artifact_type"), artifact.get("language"),
                  None if artifact.get("sensitive") is None else int(artifact["sensitive"]),
                  artifact.get("classifier_version"), self._path_key(path)))

    def remove(self, path):
        with self._connect() as connection:
            connection.execute("DELETE FROM artifacts WHERE path = ?", (self._path_key(path),))

    def known_paths(self):
        """Every saved path (in the stored spelling), for finding files that disappeared."""
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM artifacts").fetchall()
        return [json.loads(row[0])["path"] for row in rows]

    def list_artifacts(self):
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM artifacts ORDER BY rowid").fetchall()
        items = [json.loads(row[0]) for row in rows]
        return {"items": items, "count": len(items)}

    def search_artifacts(self, limit=100, offset=0, artifact_type=None, language=None, sensitive=None,
                         path_contains=None):
        """One page of artifacts with optional filters; total is the number of all matches."""
        conditions, values = [], []
        for column, value in (("artifact_type", artifact_type), ("language", language)):
            if value is not None:
                conditions.append(f"{column} = ?")
                values.append(value)
        if sensitive is not None:
            conditions.append("sensitive = ?")
            values.append(int(sensitive))
        if path_contains:
            conditions.append("instr(path, ?) > 0")
            values.append(os.path.normcase(path_contains))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM artifacts{where}", values).fetchone()[0]
            rows = connection.execute(f"SELECT metadata FROM artifacts{where} ORDER BY rowid LIMIT ? OFFSET ?",
                                      values + [limit, offset]).fetchall()
        items = [json.loads(row[0]) for row in rows]
        return {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset}

    def find_artifact(self, artifact_id: str):
        """The saved record, or None when the ID is unknown."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def get_artifact(self, artifact_id: str):
        artifact = self.find_artifact(artifact_id)
        if artifact is not None:
            return artifact
        return {"id": artifact_id, "message": "Artifact not found."}

    def same_content(self, artifact):
        """Paths of other files with exactly the same contents (exact duplicates)."""
        if not artifact.get("hash") or not artifact.get("size_bytes"):
            return []
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM artifacts WHERE hash = ? AND id != ?",
                                      (artifact["hash"], artifact["id"])).fetchall()
        return sorted(json.loads(row[0])["path"] for row in rows)

    @staticmethod
    def _format_size(size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
