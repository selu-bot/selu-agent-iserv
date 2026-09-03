"""Generate the actual RPC interface and block unintended school requests."""
import sys
import tempfile
from pathlib import Path

import pytest
import requests
from grpc_tools import protoc

_generated = tempfile.TemporaryDirectory(prefix="iserv-proto-")
_container = Path(__file__).resolve().parents[1] / "capabilities/iserv/container"
if protoc.main(["protoc", f"-I{_container}", f"--python_out={_generated.name}",
                f"--grpc_python_out={_generated.name}", str(_container / "capability.proto")]):
    raise RuntimeError("Could not compile the capability protocol")
sys.path.insert(0, _generated.name)


@pytest.fixture(autouse=True)
def no_school_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Tests must mock IServ HTTP requests")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
