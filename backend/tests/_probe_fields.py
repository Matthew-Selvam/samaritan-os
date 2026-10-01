"""Temporary probe: does appending deps AFTER route decoration still apply?"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.testclient import TestClient

CALLS: list[str] = []


def dep_a(response: Response):
    CALLS.append("a")
    response.headers["X-A"] = "1"


# Order 1: dependency appended BEFORE the route is declared
r1 = APIRouter()
r1.dependencies.append(Depends(dep_a))


@r1.get("/before")
async def before():
    return {"r": "before"}


# Order 2: route declared FIRST, dependency appended after (what api/*.py does)
r2 = APIRouter()


@r2.get("/after")
async def after():
    return {"r": "after"}


r2.dependencies.append(Depends(dep_a))

app = FastAPI()
app.include_router(r1)
app.include_router(r2)

with TestClient(app) as c:
    print("before ->", c.get("/before").headers.get("x-a"), CALLS)
    print("after  ->", c.get("/after").headers.get("x-a"), CALLS)