import os
import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("code-steward.scanner")
CHECKPOINT_FILE = "scan_checkpoint.json"

class ScanService:
    def __init__(self, config_service, logs_service, artifacts_service=None, code_steward_service=None):
        self.config_service = config_service
        self.logs_service = logs_service
        self.artifacts_service = artifacts_service
        self.code_steward_service = code_steward_service
        self._is_scanning   = False
        self._last_scan_at  = None
        self._last_results  = []

    def get_status(self):
        return {
            "is_scanning":   self._is_scanning,
            "last_scan_at":  self._last_scan_at.isoformat() if self._last_scan_at else None,
            "files_found":   len(self._last_results),
        }
    
    def _is_service_running(self) -> bool:
        if self.code_steward_service is None:
            return True
        return self.code_steward_service.state.value == "running"

    def enqueue_manual_scan(self, events=None):
        if not self._is_service_running():
            return {"message": "Service is not running. Start the service before scanning."}
        if self._is_scanning:
            return {"message": "Scan already in progress."}
        results = self._run_scan(events=events)
        return {
            "message": "Scan complete.",
            "files_found": len(results),
            "results": results,
    }
    
    def _run_scan(self, events=None):
        self._is_scanning  = True
        self._last_scan_at = datetime.now(timezone.utc)
        results = []
    
        try:
            
            # Read settings from config
            root_paths = self.config_service.get("scanning", "root_paths", default=[])
            include_extensions = self.config_service.get("scanning", "include_extensions", default=[])
            exclude_folders = self.config_service.get("scanning", "exclude_folders", default=[])
            max_file_size = self.config_service.get("scanning", "max_file_size_bytes", default=10485760)
            
            # Load checkpoint to skip already seen files
            checkpoint = self._load_checkpoint()
            event_hints = self._build_event_hints(events or [], checkpoint)
            
            for root_path in root_paths:
                if not os.path.exists(root_path):
                    logger.warning("Rooth path does not exist, skipping: %s", root_path)
                    continue
                
                logger.info("Scanning: %s", root_path)
                
                for dirpath, dirnames, filenames in os.walk(root_path):
                    
                    # Remove excluded folders 
                    dirnames[:] = [
                        d for d in dirnames
                        if d not in exclude_folders
                    ]
                    
                    for filename in filenames:
                        # Only process files with an included extension (if a filter is set)
                        if include_extensions and not any(
                            filename.endswith(ext) for ext in include_extensions
                        ):
                            continue
     
                        filepath = os.path.join(dirpath, filename)
                        
                        # Skip files that are too large
                        
                        try:
                            size = os.path.getsize(filepath)
                        except PermissionError:
                            logger.warning("Permision denied reading size: %s", filepath)
                            continue
                        if size > max_file_size:
                            logger.info("Skipping large file %s (%d bytes)", filepath, size)
                            continue
                    
                    
                        # Compute hash
                        file_hash = self._hash_file(filepath)
                        if file_hash is None:
                            continue
                        
                        
                        # Get last modified timestamp
                        try:
                            modified = datetime.fromtimestamp(
                                os.path.getmtime(filepath), timezone.utc
                            ).isoformat()
                        except PermissionError:
                            modified = None
                            
                        record = {
                            "path":		filepath,
                            "size":		size,
                            "hash":		file_hash,
                            "modified":	modified,
                        }
                        
                        # Only add to results if the file is new or changed
                        old_hash = checkpoint.get(filepath)
                        if old_hash != file_hash:
                            hint = event_hints.get(os.path.abspath(filepath), {})
                            change_type = hint.get(
                                "change_type",
                                "created" if old_hash is None else "modified",
                            )
                            previous_path = hint.get("previous_path")

                            classified = record
                            if self.artifacts_service is not None:
                                classified = self.artifacts_service.classify(
                                    record,
                                    change_type=change_type,
                                    previous_path=previous_path,
                                )

                            results.append(classified)
                            checkpoint[filepath] = file_hash
                            logger.info("Found: %s", filepath)
                        
                        
            self._save_checkpoint(checkpoint)
            
            self._last_results = results
            self._is_scanning = False
            logger.info("Scan complete. %d new/changed files found.", len(results))
            return results
    
        except Exception:
             logger.exception("Scan failed unexpectedly.")
             raise
        finally:
            self._is_scanning = False
    
    
    #Hashing
    
    def _hash_file(self, filepath) -> str | None:
        """Compute SHA-256 hash of a file. Returns None on permission error."""
        hasher = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter (lambda: f.read(65536), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except PermissionError:
            logger.warning("Permission denied reading file: %s", filepath)
            return None
        except OSError as e:
            logger.warning("Could not read file %s", filepath, e)
            return None
                
    
    #checkpoint for resumable scans
                
    def _load_checkpoint(self) -> dict:
        """Load the last scan checkpint form disk."""
        if not os.path.exists(CHECKPOINT_FILE):
            return {}
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not load checkpoint: %s", e)
            return {}
            
    def _save_checkpoint(self, checkpoint: dict):
        """Save the current scan checkpoint to disk"""
        try:
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump(checkpoint, f, indent=2)
        except Exception as e:
            logger.warning("Could not save checkpoint: %s", e)

    def _build_event_hints(self, events, checkpoint):
        """Translate debounced watcher events into scan classification hints."""
        hints = {}
        priorities = {"modified": 1, "created": 2, "moved": 3}

        for event in events:
            event_type = event.get("event_type")
            source_path = os.path.abspath(event["source_path"])

            if event_type == "deleted":
                checkpoint.pop(source_path, None)
                if self.artifacts_service is not None:
                    self.artifacts_service.remove(source_path)
                continue

            target_path = source_path
            previous_path = None
            if event_type == "moved":
                target_path = os.path.abspath(event["destination_path"])
                previous_path = source_path
                checkpoint.pop(source_path, None)

            current = hints.get(target_path)
            if current and priorities[current["change_type"]] > priorities[event_type]:
                continue
            hints[target_path] = {
                "change_type": event_type,
                "previous_path": previous_path,
            }

        return hints
