from fastapi import FastAPI

from app.api.routes import system

app = FastAPI(title="Code Kamaji")
app.include_router(system.router)
