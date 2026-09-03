from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from concurrent import futures

import capability_pb2
import capability_pb2_grpc
import grpc
from artifacts import ArtifactStore
from iserv_client import AuthenticationError, IServClient, IServError, validate_integer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("iserv-capability")

GRPC_PORT = 50051
CHUNK_SIZE = 256 * 1024  # 256 KB
MAX_JSON_BYTES = 16 * 1024

TOOL_HANDLERS = {
    "check_parent_letters",
    "get_parent_letter",
    "confirm_parent_letter",
    "download_attachment",
    "parse_pdf_attachment",
    "check_notifications",
}


class CapabilityState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client = IServClient()
        self.artifacts = ArtifactStore()

    def invoke(
        self, tool_name: str, args: dict, config: dict, context=None
    ) -> dict:
        validate_args(tool_name, args)
        if not isinstance(config, dict):
            raise IServError("config must be a JSON object")
        remaining = context.time_remaining() if context else None
        if not self._lock.acquire(timeout=max(0, min(remaining if remaining is not None else 30, 30))):
            raise IServError("IServ is busy; request expired before execution")
        try:
            if context and not context.is_active():
                raise IServError("Request expired before execution")
            username = config.get("USERNAME")
            password = config.get("PASSWORD")
            base_url = config.get("ISERV_BASE_URL")
            if not username or not password:
                raise AuthenticationError("IServ credentials (USERNAME, PASSWORD) are required")

            self._client.set_credentials(username, password)
            self._client.set_base_url(base_url)
            self._client.set_request_context(context)

            if tool_name == "check_parent_letters":
                return self._client.get_parent_letters(
                    limit=args.get("limit", 20),
                    offset=args.get("offset", 0),
                    unread_only=args.get("unread_only", False),
                )

            if tool_name == "get_parent_letter":
                href = args.get("href")
                if not href:
                    return {"error": "href is required"}
                return self._client.get_parent_letter_content(href)

            if tool_name == "confirm_parent_letter":
                href = args.get("href")
                if not href:
                    return {"error": "href is required"}
                return self._client.confirm_parent_letter(href)

            if tool_name == "download_attachment":
                attachment_href = args.get("attachment_href")
                if not attachment_href:
                    return {"error": "attachment_href is required"}
                return self._download(attachment_href)

            if tool_name == "parse_pdf_attachment":
                attachment_href = args.get("attachment_href")
                if not attachment_href:
                    return {"error": "attachment_href is required"}
                return self._client.parse_pdf_attachment(attachment_href)

            if tool_name == "check_notifications":
                return self._client.get_notifications(
                    limit=args.get("limit", 20),
                )

            raise IServError("Unknown tool")
        finally:
            self._client.clear_request_context()
            self._lock.release()

    def _download(self, attachment_href: str) -> dict:
        result = self._client.download_attachment(attachment_href)
        artifact_id = self.artifacts.put(result)

        return {
            "ok": True,
            "artifact": {
                "capability_artifact_id": artifact_id,
                "filename": result["filename"],
                "mime_type": result["mime_type"],
            },
            "size_bytes": result["size_bytes"],
        }


STATE = CapabilityState()


def validate_args(tool_name: str, args: dict) -> None:
    if not isinstance(args, dict):
        raise IServError("args must be a JSON object")
    fields = {
        "check_parent_letters": {"limit", "offset", "unread_only"},
        "check_notifications": {"limit"},
        "get_parent_letter": {"href"},
        "confirm_parent_letter": {"href"},
        "download_attachment": {"attachment_href"},
        "parse_pdf_attachment": {"attachment_href"},
    }
    required = {
        "get_parent_letter": {"href"}, "confirm_parent_letter": {"href"},
        "download_attachment": {"attachment_href"}, "parse_pdf_attachment": {"attachment_href"},
    }
    if (tool_name not in fields or args.keys() - fields[tool_name]
            or required.get(tool_name, set()) - args.keys()):
        raise IServError("Unknown tool or unsupported arguments")
    if "limit" in args:
        validate_integer(args["limit"], "limit", 1, 100)
    if "offset" in args:
        validate_integer(args["offset"], "offset", 0, 100_000)
    if "unread_only" in args and type(args["unread_only"]) is not bool:
        raise IServError("unread_only must be a boolean")
    for key in fields[tool_name] & {"href", "attachment_href"}:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise IServError(f"{key} must be a non-empty URL of at most 4096 characters")


class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    def __init__(self, state: CapabilityState | None = None) -> None:
        self.state = state if state is not None else STATE

    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="iserv ready")

    def Invoke(self, request, context):
        tool_name = request.tool_name
        if tool_name not in TOOL_HANDLERS:
            return capability_pb2.InvokeResponse(
                error="Unknown tool"
            )
        log.info("Invoke: tool=%s", tool_name)

        try:
            if len(request.args_json) > MAX_JSON_BYTES or len(request.config_json) > MAX_JSON_BYTES:
                return capability_pb2.InvokeResponse(error="Tool request JSON is too large")
            args = json.loads(request.args_json) if request.args_json else {}
            config = json.loads(request.config_json) if request.config_json else {}
        except (ValueError, UnicodeDecodeError):
            return capability_pb2.InvokeResponse(error="Invalid request JSON")

        try:
            result = self.state.invoke(tool_name, args, config, context=context)
            result_bytes = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return capability_pb2.InvokeResponse(result_json=result_bytes)
        except AuthenticationError as exc:
            log.warning("Auth error for %s: %s", tool_name, exc)
            return capability_pb2.InvokeResponse(
                error=f"Authentication failed: {exc}"
            )
        except IServError as exc:
            log.warning("IServ error for %s: %s", tool_name, exc)
            return capability_pb2.InvokeResponse(error=str(exc))
        except Exception as exc:
            # Exception messages/tracebacks can include school content and tokens.
            log.error("Unexpected %s in %s", type(exc).__name__, tool_name)
            return capability_pb2.InvokeResponse(error="Internal IServ capability error")

    def StreamInvoke(self, request, context):
        response = self.Invoke(request, context)
        if response.error:
            yield capability_pb2.InvokeChunk(error=response.error, done=True)
        else:
            yield capability_pb2.InvokeChunk(data=response.result_json, done=True)

    def DownloadOutputArtifact(self, request, context):
        artifact_id = request.capability_artifact_id
        if not context.is_active():
            yield capability_pb2.ArtifactChunk(error="Artifact download was cancelled", done=True)
            return
        artifact = self.state.artifacts.start(artifact_id)
        if artifact is None:
            yield capability_pb2.ArtifactChunk(
                error="Artifact not found, expired, or already retrieved", done=True
            )
            return

        data = artifact["data"]
        filename = artifact["filename"]
        mime_type = artifact["mime_type"]
        offset = 0
        first = True

        completed = False
        try:
            while offset < len(data):
                if not context.is_active():
                    return
                end = min(offset + CHUNK_SIZE, len(data))
                chunk = capability_pb2.ArtifactChunk(data=data[offset:end], done=(end >= len(data)))
                if first:
                    chunk.filename = filename
                    chunk.mime_type = mime_type
                    first = False
                yield chunk
                offset = end
            if not data:
                yield capability_pb2.ArtifactChunk(filename=filename, mime_type=mime_type, data=b"", done=True)
            completed = True
        finally:
            if completed:
                self.state.artifacts.complete(artifact_id)
            else:
                self.state.artifacts.release(artifact_id)

    def UploadInputArtifact(self, request_iterator, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("This capability does not accept input artifacts")
        return capability_pb2.UploadInputArtifactResponse(
            error="Not supported"
        )


def serve() -> None:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4), maximum_concurrent_rpcs=8,
        options=[("grpc.max_receive_message_length", 64 * 1024),
                 ("grpc.max_send_message_length", 8 * 1024 * 1024)],
    )
    capability_pb2_grpc.add_CapabilityServicer_to_server(
        CapabilityServicer(), server
    )
    if not server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}"):
        raise RuntimeError("Could not bind IServ capability port")
    server.start()
    log.info("IServ capability server listening on :%d", GRPC_PORT)

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %s, shutting down", signum)
        stop_event.set()
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop_event.wait(timeout=30):
        STATE.artifacts.purge_expired()
    log.info("Server stopped")


if __name__ == "__main__":
    serve()
