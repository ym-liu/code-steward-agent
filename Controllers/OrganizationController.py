from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from Services.ServiceContainer import services
from Services.ExecutionService import ExecutionError

router = APIRouter(prefix="/organization", tags=["organization"])
NO_PLAN = {404: {"description": "No plan has been built yet; POST /organization/plan builds one."}}


@router.post("/plan", status_code=202, responses={409: {"description": "A plan is already being built."}})
def build_plan(use_ai: bool = Query(default=True, description="false uses keyword hints only, without the model")):
    """Build a plan: groups, primary topics, proposed folders and optional AI reuse suggestions.
    Nothing is copied, moved or deleted. Follow it with GET /organization/status."""
    if not services.organization_service.start(use_ai):
        return JSONResponse(status_code=409, content={"detail": "An organization operation is already in progress."})
    return {"message": "Building the organization plan.", **services.organization_service.status()}


@router.get("/status")
def get_status():
    """building, ready, failed or no plan yet; the current step while building; the last plan's summary."""
    return services.organization_service.status()


@router.get("/plan", responses=NO_PLAN)
def get_plan():
    """The latest plan: every file with its type, the groups with topic, reason, confidence and proposed
    folder, and the actions a later step would take (copy, skip duplicate, keep out of Git, write README)."""
    plan = services.organization_service.latest()
    if plan is None:
        raise HTTPException(status_code=404, detail="No plan yet. POST /organization/plan builds one.")
    return plan


@router.get("/plan/page", response_class=HTMLResponse, responses=NO_PLAN)
def get_plan_page():
    """The latest plan as a readable page for people (open it in a browser)."""
    page = services.organization_service.page()
    if page is None:
        raise HTTPException(status_code=404, detail="No plan yet. POST /organization/plan builds one.")
    return HTMLResponse(page)


@router.get("/topics")
def get_topics():
    """The topic folders the plan chooses from (organization.topics_file in config.yaml)."""
    topics = services.organization_service.topics()
    return {"version": topics["version"],
            "topics": [{"id": topic["id"], "description": topic["description"]} for topic in topics["topics"]]}


class ReviewedPlan(BaseModel):
    plan_id: str = Field(min_length=1, description="plan_id of the plan you reviewed")


class UndoRun(BaseModel):
    run_id: str = Field(min_length=1, description="The last execution run to undo")


class ReuseReview(ReviewedPlan):
    suggestion_id: str = Field(min_length=1)
    accept: bool = Field(description="true adds separate copies to the plan; false removes that suggestion's copies")


class ReuseCheck(ReviewedPlan):
    source: str = Field(min_length=1, description="Source group ID from /organization/reuse/candidates")
    target: str = Field(min_length=1, description="Target group ID")


@router.get("/reuse/candidates")
def reuse_candidates():
    """Shortlist potential project pairs using saved data, with no AI requests."""
    try:
        return services.organization_service.reuse_candidates()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/reuse/check", status_code=202)
def check_reuse(request: ReuseCheck):
    """Check ONE source/target pair on demand. Poll /organization/reuse/status for the new plan_id."""
    try:
        return services.organization_service.start_reuse_check(request.plan_id, request.source, request.target)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/reuse/status")
def reuse_status():
    return services.organization_service.reuse_status()


@router.post("/reuse/review")
def review_reuse(request: ReuseReview):
    """Accept/reject an AI reuse suggestion. Use the returned NEW plan_id for further reviews and execution.
    This edits the plan only. Copies preserve source group layout; target imports are not modified."""
    try:
        return services.organization_service.review_reuse(request.plan_id, request.suggestion_id, request.accept)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def execution_call(method, value):
    try:
        return method(value)
    except ExecutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/preflight")
def preflight(request: ReviewedPlan):
    """Check a reviewed plan's sources, conflicts and space now; no files or run records are written."""
    return execution_call(services.execution_service.preflight, request.plan_id)


@router.post("/execute", status_code=202)
def execute(request: ReviewedPlan):
    """Copy the reviewed plan in the background without overwriting or changing originals."""
    return execution_call(services.execution_service.start, request.plan_id)


@router.get("/execution/status")
def execution_status():
    """Current execution state and the last saved run, including every action result."""
    return services.execution_service.status()


@router.get("/runs/{run_id}")
def execution_run(run_id: str):
    report = services.execution_service.get_run(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Organization run not found.")
    return report


@router.post("/undo", status_code=202)
def undo(request: UndoRun):
    """Explicitly undo the last run; retain edited/replaced files and existing files/directories."""
    return execution_call(services.execution_service.start_undo, request.run_id)


@router.get("/repository")
def repository():
    """The library's Git history: branch, last commits, uncommitted files and repository problems."""
    return services.execution_service.repository_status()


@router.post("/repository/repair")
def repair_repository():
    """Fix what interrupted Git commands leave behind (lock file, damaged index, detached HEAD, lost branch)."""
    return execution_call(lambda _: services.execution_service.repair_repository(), None)
