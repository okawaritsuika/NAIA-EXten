from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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

    def mark_used(self, comic_id: Any) -> None:
        self._request("POST", f"/api/comics/{int(comic_id)}/use")

