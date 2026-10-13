from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from Services.ServiceContainer import services

router = APIRouter(prefix="/organization", tags=["organization"])
NO_PLAN = {404: {"description": "No plan has been built yet; POST /organization/plan builds one."}}


@router.post("/plan", status_code=202, responses={409: {"description": "A plan is already being built."}})
def build_plan(use_ai: bool = Query(default=True, description="false uses keyword hints only, without the model")):
    """Build a new organization plan in the background: groups, a topic per group and proposed folders.
    Nothing is copied, moved or deleted. Follow it with GET /organization/status."""
    if not services.organization_service.start(use_ai):
        return JSONResponse(status_code=409, content={"detail": "A plan is already being built."})
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
