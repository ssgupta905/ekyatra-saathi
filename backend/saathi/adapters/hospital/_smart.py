"""SMART Backend Services: how a system, not a person, signs in to an EHR.

OpenEMR's FHIR API will not take a password or a client secret. System-level
access uses the SMART "Backend Services" profile: the client holds a private
key, signs a short-lived JSON Web Token asserting its own identity, and trades
that assertion for an access token. No human, no browser, no secret shared
over the wire.

Two facts about it shape this module, both found by running it:

**Tokens last five minutes.** OpenEMR answers `expires_in: 300`. A connector
polling a hospital for an afternoon needs something that re-mints on demand,
so this is a *provider* — a callable — rather than a token fetched once at
startup. The adapter calls it before every request and it returns the cached
one until it is nearly expired.

**Scopes are fixed when the client is registered.** Asking the token endpoint
for a scope the client was not registered with does not fail at the token
endpoint; it succeeds, and then the FHIR read returns a bare `401
Unauthorized` with no indication that a scope is the reason. So the scopes
asked for here must be a subset of those registered, and registration is a
deliberate, documented step — see `docs/openemr.md`.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

import httpx

from ...ports.hospital import HospitalUnavailable

logger = logging.getLogger(__name__)

#: Re-mint this many seconds before expiry. A token that expires mid-flight
#: produces a 401 that looks exactly like a permissions problem, which is a
#: miserable thing to debug on a stage.
_EARLY = 30.0

#: What the assertion is signed with. OpenEMR rejects RS256 for this profile.
_ALGORITHM = "RS384"

#: Everything the connector needs, in one place because it is needed in two:
#: the client is registered with these, and the token is asked for these, and
#: a scope in one list but not the other fails as a bare 401 that names
#: nothing. `Practitioner`, `Organization` and `Location` look unnecessary —
#: we never read them for their own sake — but booking an appointment names
#: all three, so leaving them out breaks write-back and nothing else.
SYSTEM_SCOPES = " ".join(
    f"system/{resource}.{verb}"
    for resource in (
        "Patient",
        "Appointment",
        "Task",
        "ServiceRequest",
        "Observation",
        "Practitioner",
        "Location",
        "Organization",
        "Encounter",
    )
    for verb in ("read", "write")
)


@dataclass
class SmartTokens:
    """Mints and caches an access token for one FHIR server."""

    token_url: str
    client_id: str
    #: PEM-encoded private key, matching the public key in the client's JWKS.
    private_key_pem: str
    #: The `kid` of that key, so the server knows which JWKS entry to verify with.
    key_id: str
    #: Must be a subset of what the client was registered with; see above.
    scope: str
    timeout: float = 20.0
    client: httpx.Client | None = None

    _token: str = field(default="", repr=False)
    _expires_at: float = 0.0

    def __call__(self) -> str:
        """The current access token, minting a new one when needed."""
        if self._token and time.time() < self._expires_at - _EARLY:
            return self._token
        self._token, lifetime = self._mint()
        self._expires_at = time.time() + lifetime
        return self._token

    def _assertion(self) -> str:
        """A short-lived JWT proving we hold the registered private key.

        `iss` and `sub` are both the client id, and `aud` is the token
        endpoint — a server that did not check `aud` would accept an assertion
        minted for somebody else's token endpoint, so it is spelled out rather
        than defaulted.
        """
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - a missing dependency
            raise HospitalUnavailable(
                "PyJWT is needed to sign a SMART assertion; install it to connect to OpenEMR"
            ) from exc

        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.client_id,
                "sub": self.client_id,
                "aud": self.token_url,
                "jti": uuid.uuid4().hex,
                "iat": now,
                "exp": now + 300,
            },
            self.private_key_pem,
            algorithm=_ALGORITHM,
            headers={"kid": self.key_id},
        )

    def _mint(self) -> tuple[str, float]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": self.scope,
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": self._assertion(),
            }
        )
        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                self.token_url,
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:240].strip()
            # `invalid_client` almost always means the client exists but is
            # disabled, which is the default after registration and is not
            # something the message says. Say it here instead.
            hint = (
                " — a newly registered client is disabled until "
                "`oauth_clients.is_enabled` is set; see docs/openemr.md"
                if "invalid_client" in detail
                else ""
            )
            raise HospitalUnavailable(
                f"could not get a token from {self.token_url}: "
                f"{exc.response.status_code} {detail}{hint}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HospitalUnavailable(f"could not reach {self.token_url}: {exc}") from exc
        except ValueError as exc:
            raise HospitalUnavailable("the token endpoint did not return JSON") from exc
        finally:
            if self.client is None:
                client.close()

        token = payload.get("access_token")
        if not token:
            raise HospitalUnavailable("the token endpoint returned no access_token")

        lifetime = float(payload.get("expires_in") or 300)
        logger.info(
            "minted a hospital access token, good for %.0fs", lifetime
        )
        return str(token), lifetime


__all__ = ["SYSTEM_SCOPES", "SmartTokens"]
