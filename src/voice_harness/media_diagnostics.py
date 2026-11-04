"""Log all media handshakes, including requests that never match a route."""

from urllib.parse import parse_qs

from starlette.datastructures import Headers

from .auth import bearer_value
from .observability import log_event


class MediaConnectionLoggingMiddleware:
    def __init__(self, app, auth_mode: str):
        self.app, self.auth_mode = app, auth_mode

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"websocket", "http"}:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        parts = path.split("/")
        route = ("/calls/{call_id}" if len(parts) == 3 and parts[1] == "calls" and parts[2]
                 else "/stream" if path == "/stream" else "unknown")
        fields = {"stage": "media_auth", "route": route,
                  "call_id": parts[2] if route == "/calls/{call_id}" else None}
        accepted = False
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            try:
                query = parse_qs(scope.get("query_string", b"").decode("ascii", "replace"), max_num_fields=100)
            except ValueError:
                query = {}
            query_token = bool(query.get("token", [""])[0])
            header_token = bool(bearer_value(headers.get("authorization")))
            log_event("media_connection_attempt", **fields, auth_mode=self.auth_mode,
                      query_token_present=query_token, authorization_present=header_token,
                      token_present=header_token if self.auth_mode == "header" else query_token)

        async def logged_send(message):
            nonlocal accepted
            if message["type"] == "websocket.accept":
                accepted = True
            elif message["type"] == "websocket.close":
                log_event("media_connection_closed", **fields, accepted=accepted,
                          close_code=message.get("code", 1000))
            elif message["type"] == "http.response.start":
                log_event("media_http_response", stage="http", route=route, method=scope.get("method"),
                          status_code=message["status"])
            await send(message)

        await self.app(scope, receive, logged_send)
