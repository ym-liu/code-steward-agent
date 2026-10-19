from fastapi import FastAPI

from app.api import system

app = FastAPI(title="Code Kamaji")
app.include_router(system.router)
