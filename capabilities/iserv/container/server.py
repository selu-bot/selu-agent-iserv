from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from concurrent import futures
from uuid import uuid4

import grpc

import capability_pb2
import capability_pb2_grpc
from iserv_client import AuthenticationError, IServClient, IServError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("iserv-capability")

GRPC_PORT = 50051
CHUNK_SIZE = 256 * 1024  # 256 KB
MAX_OUTPUT_ARTIFACT_BYTES = 5 * 1024 * 1024  # 5 MB

TOOL_HANDLERS = {
    "check_parent_letters",
    "get_parent_letter",
    "confirm_parent_letter",
    "download_attachment",
    "check_notifications",
}


class CapabilityState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client = IServClient()

    def invoke(
        self, tool_name: str, args: dict, config: dict
    ) -> dict:
        with self._lock:
            username = config.get("USERNAME")
            password = config.get("PASSWORD")
            base_url = config.get("ISERV_BASE_URL")
            if not username or not password:
                return {"error": "IServ credentials (USERNAME, PASSWORD) are required."}

            self._client.set_credentials(username, password)
            self._client.set_base_url(base_url)

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

            if tool_name == "check_notifications":
                return self._client.get_notifications(
                    limit=args.get("limit", 20),
                )

            return {"error": f"Unknown tool: {tool_name}"}

    def _download(self, attachment_href: str) -> dict:
        result = self._client.download_attachment(attachment_href)
        data = result.pop("data")

        if len(data) > MAX_OUTPUT_ARTIFACT_BYTES:
            return {
                "error": f"Attachment too large ({len(data)} bytes, max {MAX_OUTPUT_ARTIFACT_BYTES})"
            }

        artifact_id = str(uuid4())
        self._output_artifacts[artifact_id] = {
            "filename": result["filename"],
            "mime_type": result["mime_type"],
            "data": data,
        }

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
STATE._output_artifacts = {}


class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    def __init__(self) -> None:
        self._output_artifacts = STATE._output_artifacts

    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="iserv ready")

    def Invoke(self, request, context):
        tool_name = request.tool_name
        log.info("Invoke: tool=%s", tool_name)

        if tool_name not in TOOL_HANDLERS:
            return capability_pb2.InvokeResponse(
                error=f"Unknown tool: {tool_name}"
            )

        try:
            args = json.loads(request.args_json) if request.args_json else {}
            config = json.loads(request.config_json) if request.config_json else {}
        except json.JSONDecodeError as exc:
            return capability_pb2.InvokeResponse(error=f"Invalid JSON: {exc}")

        log.debug("Invoke: config keys=%s", list(config.keys()))

        try:
            result = STATE.invoke(tool_name, args, config)
            result_bytes = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
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
            log.exception("Unexpected error in %s", tool_name)
            return capability_pb2.InvokeResponse(error=f"Internal error: {exc}")

    def StreamInvoke(self, request, context):
        response = self.Invoke(request, context)
        if response.error:
            yield capability_pb2.InvokeChunk(error=response.error, done=True)
        else:
            yield capability_pb2.InvokeChunk(data=response.result_json, done=True)

    def DownloadOutputArtifact(self, request, context):
        artifact_id = request.capability_artifact_id
        artifact = self._output_artifacts.pop(artifact_id, None)
        if artifact is None:
            yield capability_pb2.ArtifactChunk(
                error=f"Artifact {artifact_id} not found", done=True
            )
            return

        data = artifact["data"]
        filename = artifact["filename"]
        mime_type = artifact["mime_type"]
        offset = 0
        first = True

        while offset < len(data):
            end = min(offset + CHUNK_SIZE, len(data))
            chunk = capability_pb2.ArtifactChunk(
                data=data[offset:end],
                done=(end >= len(data)),
            )
            if first:
                chunk.filename = filename
                chunk.mime_type = mime_type
                first = False
            yield chunk
            offset = end

        if not data:
            yield capability_pb2.ArtifactChunk(
                filename=filename, mime_type=mime_type, data=b"", done=True
            )

    def UploadInputArtifact(self, request_iterator, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("This capability does not accept input artifacts")
        return capability_pb2.UploadInputArtifactResponse(
            error="Not supported"
        )


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    capability_pb2_grpc.add_CapabilityServicer_to_server(
        CapabilityServicer(), server
    )
    server.add_insecure_port(f"0.0.0.0:{GRPC_PORT}")
    server.start()
    log.info("IServ capability server listening on :%d", GRPC_PORT)

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %s, shutting down", signum)
        stop_event.set()
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop_event.wait()
    log.info("Server stopped")


if __name__ == "__main__":
    serve()
