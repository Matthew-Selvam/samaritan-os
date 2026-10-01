"""Temporary probe: does an appended router dependency actually EXECUTE?"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.testclient import TestClient

CALLS: list[str] = []


def dep_ctor(response: Response):
    CALLS.append("ctor")
    response.headers["X-Ctor"] = "1"


def dep_append(response: Response):
    CALLS.append("append")
    response.headers["X-Append"] = "1"


r_ctor = APIRouter(dependencies=[Depends(dep_ctor)])


@r_ctor.get("/ctor")
async def ctor():
    return {"r": "ctor"}


r_append = APIRouter()
r_append.dependencies.append(Depends(dep_append))


@r_append.get("/append")
async def append():
    return {"r": "append"}


app = FastAPI()
app.include_router(r_append)
app.include_router(r_ctor)

with TestClient(app) as c:
    print("ctor  ->", c.get("/ctor").headers.get("x-ctor"), "calls:", list(CALLS))
    print("append->", c.get("/append").headers.get("x-append"), "calls:", list(CALLS))

# Does a router added to an app AFTER include_router pick up new routes?
@r_append.get("/late")
async def late():
    return {"r": "late"}


with TestClient(app) as c:
    print("late  ->", c.get("/late").status_code)