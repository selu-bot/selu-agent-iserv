import json
from io import BytesIO

import capability_pb2
from iserv_client import IServClient, IServError
from server import CapabilityServicer, CapabilityState


class Context:
    def __init__(self, active=True):
        self.active = active

    def is_active(self):
        return self.active

    def time_remaining(self):
        return 30


def request(tool, args=None, config=None):
    return capability_pb2.InvokeRequest(
        tool_name=tool,
        args_json=json.dumps(args or {}).encode(),
        config_json=json.dumps(config or {}).encode(),
    )


def test_rpc_rejects_unknown_or_missing_arguments_without_network():
    servicer = CapabilityServicer(CapabilityState())
    assert servicer.Invoke(request("made_up"), Context()).error == "Unknown tool"
    response = servicer.Invoke(request("get_parent_letter"), Context())
    assert "unsupported arguments" in response.error


def test_rpc_requires_credentials_and_does_not_log_them():
    servicer = CapabilityServicer(CapabilityState())
    response = servicer.Invoke(request("check_notifications"), Context())
    assert response.error == "Authentication failed: IServ credentials (USERNAME, PASSWORD) are required"


def test_rpc_rejects_oversized_json():
    servicer = CapabilityServicer(CapabilityState())
    req = request("check_notifications")
    req.args_json = b"x" * (16 * 1024 + 1)
    assert servicer.Invoke(req, Context()).error == "Tool request JSON is too large"


def test_redirect_is_same_origin_and_post_is_never_replayed():
    client = IServClient()
    client.set_request_context(Context())

    first = _response(302, "https://mags-greven.de/iserv/auth/auth", {"Location": "/iserv/next"})
    second = _response(200, "https://mags-greven.de/iserv/next", {}, b"<html>ok</html>")
    responses = iter([first, second])
    client.session.request = lambda *args, **kwargs: next(responses)
    assert client._request("GET", "/iserv/start").content == b"<html>ok</html>"

    cross = _response(302, "https://mags-greven.de/iserv/start", {"Location": "https://evil.example/"})
    client.session.request = lambda *args, **kwargs: cross
    try:
        client._request("POST", "/iserv/start", data={"x": "y"})
    except IServError as exc:
        assert "configured IServ HTTPS host" in str(exc)
    else:
        raise AssertionError("cross-origin redirect was accepted")
    client.clear_request_context()


def test_html_auth_bridge_is_followed_and_cross_origin_bridge_is_rejected():
    client = IServClient()
    bridge = _response(
        200, "https://mags-greven.de/iserv/auth/auth", {"Content-Type": "text/html"},
        b'<meta http-equiv="refresh" content="0;url=/iserv/app/authentication/redirect?state=x&amp;code=y">',
    )
    destination = _response(200, "https://mags-greven.de/iserv/home", {}, b"ok")
    responses = iter([bridge, destination])
    client.session.request = lambda *args, **kwargs: next(responses)
    assert client._request("GET", "/iserv/start").content == b"ok"

    malicious = _response(
        200, "https://mags-greven.de/iserv/auth/auth", {"Content-Type": "text/html"},
        b'<meta http-equiv="refresh" content="0;url=https://evil.example/steal">',
    )
    client.session.request = lambda *args, **kwargs: malicious
    try:
        client._request("GET", "/iserv/start")
    except IServError as exc:
        assert "configured IServ HTTPS host" in str(exc)
    else:
        raise AssertionError("cross-origin HTML auth bridge was accepted")


def _response(status, url, headers, content=b""):
    import requests
    response = requests.Response()
    response.status_code, response.url = status, url
    response.headers.update(headers)
    response._content = content
    response.raw = BytesIO(content)
    return response
