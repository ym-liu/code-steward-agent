class ArtifactsService:
    def __init__(self):
        pass

    def list_artifacts(self):
        return {"items": []}

    def get_artifact(self, artifact_id: str):
        return {
            "id": artifact_id,
            "message": "Artifact lookup noop.",
        }
