import json
import logging
import math
from time import perf_counter
from uuid import uuid4

from .errors import ApiError, error_response

logger = logging.getLogger("saferoute.http")
SAFE_INTEGER = (1 << 53) - 1


def decode_http_json(raw: bytes):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ApiError(422, "DUPLICATE_JSON_KEY", "body", "Duplicate object key")
            result[key] = value
        return result

    def bad_constant(_):
        raise ApiError(422, "INVALID_JSON_NUMBER", "body", "Finite JSON numbers required")

    def inspect(value, depth=0):
        if depth >= 96:
            raise ApiError(422, "JSON_DEPTH_EXCEEDED", "body", "JSON nesting limit exceeded")
        if isinstance(value, dict):
            for child in value.values():
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
        elif type(value) is int and abs(value) > SAFE_INTEGER:
            raise ApiError(422, "UNSAFE_JSON_INTEGER", "body", "Exact large integers must use decimal strings")
        elif type(value) is float and not math.isfinite(value):
            bad_constant(value)

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=bad_constant)
        inspect(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ApiError(422, "INVALID_JSON", "body", "Valid UTF-8 JSON required") from error
    if not isinstance(value, dict):
        raise ApiError(422, "INVALID_JSON", "body", "JSON object required")
    return value


class HttpBoundaryMiddleware:
    """ASGI body validation before FastAPI decoding, plus request audit/headers."""
    def __init__(self, app, max_body_bytes: int, cors_origins: tuple[str, ...]):
        self.app, self.limit, self.origins = app, max_body_bytes, cors_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["request_id"] = str(uuid4())
        started, sent, status = perf_counter(), False, 500
        headers = {key.lower(): value for key, value in scope["headers"]}

        async def traced_send(message):
            nonlocal sent, status
            if message["type"] == "http.response.start":
                sent, status = True, message["status"]
                response_headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"x-request-id", b"x-process-time")]
                response_headers.extend([(b"x-request-id", state["request_id"].encode()),
                                         (b"x-process-time", f"{perf_counter() - started:.6f}".encode())])
                if scope["path"].startswith("/api/"):
                    response_headers = [(k, v) for k, v in response_headers if k.lower() != b"cache-control"]
                    response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        async def failure(error):
            response = error_response(scope, error)
            origin = headers.get(b"origin", b"").decode("latin-1")
            if origin in self.origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "X-Request-ID, X-Process-Time"
            await response(scope, receive, traced_send)

        try:
            if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
                raw = bytearray()
                while True:
                    chunk = await receive()
                    if chunk["type"] == "http.disconnect":
                        raise ApiError(400, "CLIENT_DISCONNECTED", "body", "Request body was interrupted")
                    raw.extend(chunk.get("body", b""))
                    if len(raw) > self.limit:
                        raise ApiError(413, "BODY_TOO_LARGE", "body", "Request body exceeds configured limit")
                    if not chunk.get("more_body", False):
                        break
                if raw:
                    content_type = headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0].strip().lower()
                    if content_type != "application/json" and not content_type.endswith("+json"):
                        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "body", "application/json required")
                    decode_http_json(bytes(raw))
                consumed = False

                async def replay_body():
                    nonlocal consumed
                    if not consumed:
                        consumed = True
                        return {"type": "http.request", "body": bytes(raw), "more_body": False}
                    return await receive()

                await self.app(scope, replay_body, traced_send)
            else:
                await self.app(scope, receive, traced_send)
        except ApiError as error:
            if sent:
                raise
            await failure(error)
        except Exception:
            if sent:
                raise
            logger.error("Unhandled HTTP error request_id=%s", state["request_id"])
            await failure(ApiError(500, "INTERNAL_ERROR", "server", "Internal server error"))
        finally:
            logger.info(json.dumps({"event": "http_request", "request_id": state["request_id"],
                                   "actor_id": state.get("actor_id"), "method": scope["method"], "path": scope["path"],
                                   "status": status, "duration_ms": round((perf_counter() - started) * 1000, 3)}))
