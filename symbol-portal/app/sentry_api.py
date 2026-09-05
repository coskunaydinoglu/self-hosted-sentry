"""Thin async client for the handful of Sentry API endpoints the portal needs."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"(?P<rest>[^,]*)')


class SentryError(Exception):
    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def parse_next_link(link_header: str | None) -> str | None:
    """Return the `next` URL from a Sentry Link header, or None when there are no more results."""
    if not link_header:
        return None
    for match in _LINK_RE.finditer(link_header):
        if match.group("rel") != "next":
            continue
        if 'results="false"' in match.group("rest"):
            return None
        return match.group("url")
    return None


class SentryClient:
    def __init__(
        self,
        base_url: str,
        org: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        rewrite_chunk_url: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.org = org
        self.rewrite_chunk_url = rewrite_chunk_url
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "sentry-symbol-portal/0.1"},
            timeout=httpx.Timeout(30.0, read=300.0, write=300.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ---------------------------------------------------------------

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, self._url(path), **kwargs)
        except httpx.HTTPError as exc:
            raise SentryError(f"Sentry'ye ulaşılamadı: {exc}") from exc
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:500]
            message = detail.get("detail") if isinstance(detail, dict) and detail.get("detail") else str(detail)
            raise SentryError(f"Sentry {resp.status_code}: {message}", status=resp.status_code, detail=detail)
        return resp

    async def _paginate(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict]:
        url: str | None = path
        first = True
        while url:
            resp = await self._request("GET", url, params=params if first else None)
            first = False
            for item in resp.json():
                yield item
            url = parse_next_link(resp.headers.get("Link"))

    # -- projects ---------------------------------------------------------------

    async def list_projects(self) -> list[dict]:
        projects = []
        async for project in self._paginate(f"/api/0/organizations/{self.org}/projects/"):
            projects.append(
                {
                    "slug": project["slug"],
                    "name": project.get("name") or project["slug"],
                    "platform": project.get("platform"),
                    "id": project.get("id"),
                }
            )
        return sorted(projects, key=lambda p: p["name"].lower())

    # -- debug files ------------------------------------------------------------

    async def chunk_upload_options(self) -> dict:
        resp = await self._request("GET", f"/api/0/organizations/{self.org}/chunk-upload/")
        options = resp.json()
        if self.rewrite_chunk_url and options.get("url"):
            # Keep the path Sentry gave us, but point the host at our internal base URL.
            public = urlsplit(options["url"])
            internal = urlsplit(self.base_url)
            options["url"] = urlunsplit((internal.scheme, internal.netloc, public.path, public.query, ""))
        return options

    async def upload_chunks(self, url: str, chunks: list[tuple[str, bytes]]) -> None:
        files = [("file", (sha, data, "application/octet-stream")) for sha, data in chunks]
        await self._request("POST", url, files=files)

    async def assemble_difs(self, project: str, payload: dict[str, dict]) -> dict:
        resp = await self._request("POST", f"/api/0/projects/{self.org}/{project}/files/difs/assemble/", json=payload)
        return resp.json()

    async def upload_dif_archive(self, project: str, archive: Path) -> list[dict]:
        """Legacy endpoint: Sentry unpacks the zip itself. Used for ProGuard mappings."""
        with open(archive, "rb") as fh:
            resp = await self._request(
                "POST",
                f"/api/0/projects/{self.org}/{project}/files/dsyms/",
                files=[("file", (archive.name, fh, "application/zip"))],
            )
        return resp.json()

    async def find_debug_files(self, project: str, debug_id: str) -> list[dict]:
        resp = await self._request(
            "GET", f"/api/0/projects/{self.org}/{project}/files/dsyms/", params={"debug_id": debug_id}
        )
        return resp.json()

    async def list_debug_files(self, project: str, query: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if query:
            params["query"] = query
        resp = await self._request("GET", f"/api/0/projects/{self.org}/{project}/files/dsyms/", params=params)
        return resp.json()

    # -- events -----------------------------------------------------------------

    async def get_event_json(self, project: str, event_id: str) -> dict:
        """Raw stored event (debug_meta, raw stack traces with instruction addresses)."""
        resp = await self._request("GET", f"/api/0/projects/{self.org}/{project}/events/{event_id}/json/")
        return resp.json()

    async def iter_events(self, project: str, max_events: int) -> AsyncIterator[dict]:
        """Newest-first full events for a project. Callers stop iterating once they are past their window."""
        count = 0
        async for event in self._paginate(f"/api/0/projects/{self.org}/{project}/events/", params={"full": "true"}):
            yield event
            count += 1
            if count >= max_events:
                return


class SymbolicatorClient:
    """Direct access to the Symbolicator HTTP API for on-demand re-symbolication."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def symbolicate(self, request: dict, timeout: int = 20, max_polls: int = 30) -> dict:
        try:
            resp = await self._client.post(f"{self.base_url}/symbolicate", params={"timeout": timeout}, json=request)
            resp.raise_for_status()
            body = resp.json()
            polls = 0
            while body.get("status") == "pending" and polls < max_polls:
                polls += 1
                resp = await self._client.get(
                    f"{self.base_url}/requests/{body['request_id']}", params={"timeout": timeout}
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise SentryError(f"Symbolicator {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise SentryError(f"Symbolicator'a ulaşılamadı: {exc}") from exc
        if body.get("status") == "pending":
            raise SentryError("Symbolicator zamanında yanıt vermedi; tekrar deneyin")
        return body
