"""OAuth for the remote server: MCP clients sign users in with their own Splitwise account.

The MCP server is the OAuth authorization server that Claude, ChatGPT, and other MCP
clients talk to (with dynamic client registration and PKCE). It delegates the actual
login to Splitwise:

    MCP client --/authorize--> consent page --approve--> Splitwise login
    MCP client <--code-------- this server <--callback-- Splitwise

The consent page names the requesting app and where it will send the user. Anyone can
register a client (with any redirect URI), and Splitwise's own screen only names this
server, so without it a crafted link could hand a user's access to someone else.

Nothing is stored server-side. Registered clients, in-flight authorization state,
authorization codes, and tokens are all sealed with Fernet (authenticated encryption)
under SERVER_SECRET and handed to the client as opaque strings. Each user's Splitwise
token lives only inside their own sealed MCP token. Consequences:

- Rotating SERVER_SECRET signs everyone out and invalidates registered clients.
- Revocation is not supported: tokens stay valid until they expire. A user can
  revoke the server's access to their account from Splitwise's settings.
- Authorization codes are not strictly single-use. They expire after 5 minutes and
  are bound to the client's PKCE verifier, which an attacker would also need.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SPLITWISE_AUTHORIZE_URL = "https://secure.splitwise.com/oauth/authorize"
SPLITWISE_TOKEN_URL = "https://secure.splitwise.com/oauth/token"
CALLBACK_PATH = "/oauth/splitwise/callback"
CONSENT_PATH = "/oauth/consent"
SCOPE = "splitwise"

STATE_TTL = 10 * 60
CODE_TTL = 5 * 60
ACCESS_TTL = 24 * 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60


@dataclass(frozen=True)
class OAuthConfig:
    public_url: str  # e.g. https://example.up.railway.app (no trailing slash)
    splitwise_client_id: str
    splitwise_client_secret: str
    server_secret: str

    @property
    def callback_url(self) -> str:
        return self.public_url + CALLBACK_PATH

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=self.public_url + "/mcp",
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
        )


class Sealer:
    """Authenticated encryption for the opaque strings this server hands out.

    Every payload carries a `typ`, so a sealed value of one kind (e.g. a refresh
    token) can never be accepted as another (e.g. an access token).
    """

    def __init__(self, secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def seal(self, typ: str, payload: dict[str, Any]) -> str:
        raw = json.dumps({"typ": typ, **payload}, separators=(",", ":")).encode()
        return self._fernet.encrypt(zlib.compress(raw, 9)).decode()

    def open(self, typ: str, token: str, ttl: int | None = None) -> dict[str, Any] | None:
        try:
            data = json.loads(zlib.decompress(self._fernet.decrypt(token.encode(), ttl=ttl)))
        except (InvalidToken, ValueError, TypeError, zlib.error):
            return None
        if not isinstance(data, dict) or data.pop("typ", None) != typ:
            return None
        return data


class SplitwiseAuthorizationCode(AuthorizationCode):
    splitwise_token: str


class SplitwiseRefreshToken(RefreshToken):
    splitwise_token: str


class SplitwiseAccessToken(AccessToken):
    splitwise_token: str


class SplitwiseOAuthProvider:
    """OAuthAuthorizationServerProvider backed by Splitwise login and sealed tokens."""

    def __init__(self, config: OAuthConfig, http: httpx2.AsyncClient | None = None):
        self.config = config
        self.sealer = Sealer(config.server_secret)
        self._http = http

    # ------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.sealer.open("client", client_id)
        if data is None:
            return None
        return OAuthClientInformationFull.model_validate({**data, "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK assigns a random client_id and returns this same object to the
        # client after we return. Replace the id with the sealed registration so
        # get_client can recover it without a database. tests/test_oauth.py checks
        # that the registration response carries the sealed id.
        client_info.client_id = self.sealer.seal("client", client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True))

    # ------------------------------------------------------------ authorize

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.sealer.seal(
            "state",
            {
                "client_id": client.client_id,
                "client_name": client.client_name or "An unnamed app",
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
                "state": params.state,
            },
        )
        return self.config.public_url + CONSENT_PATH + "?" + urlencode({"state": state})

    async def handle_consent(self, request: Request) -> Response:
        """GET shows who is asking; POST (the Allow button) continues to Splitwise."""
        if request.method == "POST":
            form = await request.form()
            state = str(form.get("state", ""))
        else:
            state = request.query_params.get("state", "")
        flow = self.sealer.open("state", state, ttl=STATE_TTL)
        if flow is None:
            return _page("Sign-in expired", "<p>This sign-in link is invalid or has expired. Start again from your chat app.</p>", 400)

        if request.method == "POST":
            if form.get("action") != "allow":
                url = construct_redirect_uri(
                    flow["redirect_uri"], state=flow["state"], error="access_denied", error_description="User denied access"
                )
                return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
            url = SPLITWISE_AUTHORIZE_URL + "?" + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.config.splitwise_client_id,
                    "redirect_uri": self.config.callback_url,
                    "state": state,
                }
            )
            return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

        name = html.escape(flow["client_name"])
        host = html.escape(urlparse(flow["redirect_uri"]).netloc or flow["redirect_uri"])
        body = f"""
<p><strong>{name}</strong> wants to use splitwise-mcp-server with your Splitwise account.
It will be able to read your friends and groups and add expenses on your behalf.</p>
<p>After you sign in, you will be sent to <strong>{host}</strong>.
Only continue if you started this from an app you trust and that address is expected.</p>
<form method="post" action="{CONSENT_PATH}">
  <input type="hidden" name="state" value="{html.escape(state)}">
  <button type="submit" name="action" value="allow">Continue to Splitwise</button>
  <button type="submit" name="action" value="deny">Cancel</button>
</form>"""
        return _page("Connect Splitwise", body)

    async def handle_callback(self, request: Request) -> Response:
        """Splitwise redirects here after the user approves (or denies) access."""
        flow = self.sealer.open("state", request.query_params.get("state", ""), ttl=STATE_TTL)
        if flow is None:
            return _page("Sign-in expired", "<p>This sign-in link is invalid or has expired. Start again from your chat app.</p>", 400)

        def back_to_client(**params: str | None) -> RedirectResponse:
            url = construct_redirect_uri(flow["redirect_uri"], state=flow["state"], **params)
            return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

        if "error" in request.query_params or "code" not in request.query_params:
            return back_to_client(error="access_denied", error_description="Splitwise access was not granted")

        try:
            splitwise_token = await self._exchange_splitwise_code(request.query_params["code"])
        except Exception:
            return back_to_client(error="server_error", error_description="Could not complete Splitwise sign-in")

        code = self.sealer.seal(
            "code",
            {
                "nonce": secrets.token_urlsafe(16),
                "client_id": flow["client_id"],
                "redirect_uri": flow["redirect_uri"],
                "redirect_uri_provided_explicitly": flow["redirect_uri_provided_explicitly"],
                "code_challenge": flow["code_challenge"],
                "scopes": flow["scopes"],
                "resource": flow["resource"],
                "expires_at": time.time() + CODE_TTL,
                "splitwise_token": splitwise_token,
            },
        )
        return back_to_client(code=code)

    async def _exchange_splitwise_code(self, code: str) -> str:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.callback_url,
            "client_id": self.config.splitwise_client_id,
            "client_secret": self.config.splitwise_client_secret,
        }
        if self._http is not None:
            resp = await self._http.post(SPLITWISE_TOKEN_URL, data=data)
        else:
            async with httpx2.AsyncClient(timeout=20) as http:
                resp = await http.post(SPLITWISE_TOKEN_URL, data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("Splitwise token response had no access_token")
        return token

    # ------------------------------------------------------------ codes and tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SplitwiseAuthorizationCode | None:
        data = self.sealer.open("code", authorization_code, ttl=CODE_TTL)
        if data is None or data["client_id"] != client.client_id:
            return None
        data.pop("nonce", None)
        return SplitwiseAuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: SplitwiseAuthorizationCode
    ) -> OAuthToken:
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.splitwise_token)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> SplitwiseRefreshToken | None:
        data = self.sealer.open("refresh", refresh_token, ttl=REFRESH_TTL)
        if data is None or data["client_id"] != client.client_id:
            return None
        return SplitwiseRefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: SplitwiseRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "Requested scopes exceed the original grant")
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.splitwise_token)

    async def load_access_token(self, token: str) -> SplitwiseAccessToken | None:
        data = self.sealer.open("access", token, ttl=ACCESS_TTL)
        if data is None:
            return None
        return SplitwiseAccessToken(token=token, **data)

    async def revoke_token(self, token: Any) -> None:  # revocation is not enabled
        return None

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Not supported")

    def _issue(self, client_id: str, scopes: list[str], splitwise_token: str) -> OAuthToken:
        now = int(time.time())
        claims = {"client_id": client_id, "scopes": scopes, "splitwise_token": splitwise_token}
        access = self.sealer.seal("access", {**claims, "expires_at": now + ACCESS_TTL})
        refresh = self.sealer.seal("refresh", {**claims, "expires_at": now + REFRESH_TTL})
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;line-height:1.5}}
button{{font-size:1rem;padding:.5rem 1rem;margin-right:.5rem}}</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>"""
    return HTMLResponse(
        doc,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'; default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://secure.splitwise.com",
            "Referrer-Policy": "no-referrer",
        },
    )
