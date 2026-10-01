"""Temporary probe: run the WS-API suite to a dedicated file."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path("/tmp/wsapi_pytest_run.txt")

with OUT.open("w") as handle:
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", "tests/test_api.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
    )
print("exit", proc.returncode)
print(OUT.read_text()[-5000:])