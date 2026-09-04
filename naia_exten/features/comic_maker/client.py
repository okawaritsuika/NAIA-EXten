from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


class ComicServerError(RuntimeError):
    pass


class ComicPlanNotFound(ComicServerError):
    pass


class ComicServerClient:
    def __init__(self, base_url: str, *, timeout: float = 2.0):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {
                key: str(value).lower() if isinstance(value, bool) else value
                for key, value in query.items()
                if value is not None and value != ""
            }
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        # Story Writer + Comic Director can take several minutes on NovelAI.
        # Keep normal APIs on the existing timeout, but allow the full automatic
        # comic generation route to wait up to 15 minutes.
        comic_generate_timeout = 900.0
        request_timeout = (
            max(self.timeout, comic_generate_timeout)
            if path == "/api/comics/generate"
            else self.timeout
        )

        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ComicPlanNotFound("조건에 맞는 ComicPlan이 없습니다.") from exc
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise ComicServerError(f"PromptServer HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ComicServerError(f"PromptServer 연결 실패: {exc}") from exc
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ComicServerError("PromptServer가 잘못된 JSON을 반환했습니다.") from exc

    def presets(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/comic-presets")
        return payload if isinstance(payload, list) else []

    def random_plan(self, **filters: Any) -> dict[str, Any]:
        payload = self._request("GET", "/api/comics/random", query=filters)
        if not isinstance(payload, dict):
            raise ComicServerError("PromptServer ComicPlan 응답 형식이 올바르지 않습니다.")
        return payload

    def generate_plan(
        self,
        payload: dict[str, Any],
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Generate a plan while forwarding NDJSON progress events immediately."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/comics/generate/stream",
            data=body,
            headers={
                "Accept": "application/x-ndjson",
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 900.0)) as response:
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ComicServerError(
                            "PromptServer가 잘못된 스트림 JSON을 반환했습니다."
                        ) from exc
                    if not isinstance(event, dict):
                        raise ComicServerError("PromptServer 스트림 응답 형식이 올바르지 않습니다.")
                    event_type = str(event.get("type") or "")
                    if event_type == "progress":
                        if progress is not None and event.get("message") is not None:
                            try:
                                progress(str(event["message"]))
                            except Exception:
                                pass
                    elif event_type == "complete":
                        result = event.get("plan")
                        if not isinstance(result, dict):
                            raise ComicServerError(
                                "PromptServer 자동 ComicPlan 응답 형식이 올바르지 않습니다."
                            )
                        return result
                    elif event_type == "error":
                        raise ComicServerError(
                            str(event.get("message") or "PromptServer ComicPlan 생성에 실패했습니다.")
                        )
                raise ComicServerError("PromptServer 스트림이 완료 응답 없이 종료되었습니다.")
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405):
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:500]
                except Exception:
                    pass
                raise ComicServerError(f"PromptServer HTTP {exc.code}: {detail}") from exc
            # Older PromptServer versions only expose the JSON route.
            result = self._request("POST", "/api/comics/generate", payload=payload)
        except (OSError, urllib.error.URLError) as exc:
            raise ComicServerError(f"PromptServer 연결 실패: {exc}") from exc
        if not isinstance(result, dict):
            raise ComicServerError("PromptServer 자동 ComicPlan 응답 형식이 올바르지 않습니다.")
        return result

    def mark_used(self, comic_id: Any) -> None:
        self._request("POST", f"/api/comics/{int(comic_id)}/use")

