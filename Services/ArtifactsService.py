import mimetypes
import os
import uuid
from datetime import datetime, timezone


class ArtifactsService:
    """Classifies files and stores their latest metadata in memory."""

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

    def __init__(self):
        self._artifacts = {}
        self._path_to_id = {}

    def classify(self, file_record, change_type, previous_path=None):
        path = os.path.abspath(file_record["path"])
        extension = os.path.splitext(path)[1].lower()
        mime_type, _ = mimetypes.guess_type(path)

        artifact_id = self._path_to_id.get(path)
        if previous_path:
            artifact_id = self._path_to_id.pop(
                os.path.abspath(previous_path), artifact_id
            )
        if artifact_id is None:
            artifact_id = str(uuid.uuid4())

        artifact = {
            "id": artifact_id,
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

        self._artifacts[artifact_id] = artifact
        self._path_to_id[path] = artifact_id
        return artifact

    def remove(self, path):
        artifact_id = self._path_to_id.pop(os.path.abspath(path), None)
        if artifact_id:
            self._artifacts.pop(artifact_id, None)

    def list_artifacts(self):
        return {
            "items": list(self._artifacts.values()),
            "count": len(self._artifacts),
        }

    def get_artifact(self, artifact_id: str):
        return self._artifacts.get(
            artifact_id,
            {"id": artifact_id, "message": "Artifact not found."},
        )

    @staticmethod
    def _format_size(size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"
