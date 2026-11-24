"""Writing an approval task into a hospital's own FHIR endpoint.

Shared by both adapters, because both hospitals expose FHIR R4 `Task` even
though only one of them speaks FHIR for appointments. Bahmni's own appointment
API is not FHIR at all, but OpenMRS underneath it serves
`/openmrs/ws/fhir2/R4/Task` — verified against a running instance — so the
approval request lands in the hospital's record either way.

**A caveat that belongs in the code, not only in a slide.** Bahmni accepts a
Task over its API and has no screen that displays one. The task is real,
auditable and queryable; it is not visible to a desk clerk in Bahmni's UI.
That is why staff approve in Saathi's panel and this writes the record
alongside — a system that only wrote the Task would have put the request
somewhere nobody looks, which is worse than not writing it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from ...ports.hospital import HospitalUnavailable

logger = logging.getLogger(__name__)


class FhirTasks:
    """`Task` create and update against one FHIR R4 root."""

    def __init__(
        self,
        client: httpx.Client,
        fhir_root: str,
        token: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._root = fhir_root.rstrip("/")
        # When the adapter authenticates with a short-lived token the shared
        # client carries no Authorization header, so these requests have to
        # ask for one too. Without this every Task call is unauthenticated and
        # comes back 401 — which reads exactly like a missing scope.
        self._token = token

    def create_task(self, task: dict[str, Any]) -> str:
        """Write the task and return its reference, or raise.

        Raises rather than returning None because a caller that cannot tell a
        written task from an unwritten one would report an audit trail the
        hospital does not actually have.
        """
        created = self._send("POST", "/Task", json=task)
        if not isinstance(created, dict) or not created.get("id"):
            raise HospitalUnavailable("the hospital accepted a task but returned no id")
        return str(created["id"])

    def read_task(self, ref: str) -> dict[str, Any] | None:
        found = self._send("GET", f"/Task/{ref}")
        return found if isinstance(found, dict) else None

    def update_task(self, ref: str, task: dict[str, Any]) -> None:
        body = dict(task)
        body["id"] = ref
        self._send("PUT", f"/Task/{ref}", json=body)

    def _send(self, method: str, path: str, **kwargs: object) -> object:
        url = f"{self._root}{path}"
        headers = {"Accept": "application/fhir+json"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/fhir+json"
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token()}"
        try:
            response = self._client.request(
                method, url, headers=headers, **kwargs  # type: ignore[arg-type]
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HospitalUnavailable(
                f"fhir task {method} {path} -> {exc.response.status_code}: "
                f"{exc.response.text[:240].strip()}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HospitalUnavailable(f"fhir task {method} {path}: {exc}") from exc

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise HospitalUnavailable(f"fhir task {method} {path}: body was not JSON") from exc
