import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from splitwise_mcp_server.oauth import OAuthConfig, SplitwiseAccessToken
from splitwise_mcp_server.server import create_server

pytestmark = pytest.mark.anyio

PUBLIC = "https://mcp.example.test"
CLIENT_REDIRECT = "https://client.example.test/callback"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def config(secret="s" * 40):
    return OAuthConfig(
        public_url=PUBLIC,
        splitwise_client_id="sw-client",
        splitwise_client_secret="sw-secret",
        server_secret=secret,
    )


def fake_splitwise(calls):
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form.get("code") != ["good-code"]:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"access_token": "sw-user-token", "token_type": "bearer"})
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def run_flow(auth_method="none"):
    calls = []
    server = create_server(config(), http=fake_splitwise(calls))
    app = server.streamable_http_app(stateless_http=True)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["registration_endpoint"] == PUBLIC + "/register"

        reg = await c.post("/register", json={
            "redirect_uris": [CLIENT_REDIRECT],
            "token_endpoint_auth_method": auth_method,
            "grant_types": ["authorization_code", "refresh_token"],
            "client_name": "test client",
        })
        assert reg.status_code == 201, reg.text
        client = reg.json()
        # The returned id must be the sealed registration, not the SDK's random uuid.
        assert len(client["client_id"]) > 100

        verifier, challenge = pkce()
        auth = await c.get("/authorize", params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": CLIENT_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
        })
        assert auth.status_code == 302, auth.text
        to_consent = urlparse(auth.headers["location"])
        assert to_consent.path == "/oauth/consent"
        state = parse_qs(to_consent.query)["state"][0]

        page = await c.get("/oauth/consent", params={"state": state})
        assert page.status_code == 200
        assert "test client" in page.text and "client.example.test" in page.text
        assert page.headers["x-frame-options"] == "DENY"

        allow = await c.post("/oauth/consent", data={"state": state, "action": "allow"})
        assert allow.status_code == 303
        to_splitwise = urlparse(allow.headers["location"])
        assert to_splitwise.netloc == "secure.splitwise.com"
        q = parse_qs(to_splitwise.query)
        assert q["client_id"] == ["sw-client"]
        assert q["redirect_uri"] == [PUBLIC + "/oauth/splitwise/callback"]

        cb = await c.get("/oauth/splitwise/callback", params={"code": "good-code", "state": q["state"][0]})
        assert cb.status_code == 302
        back = urlparse(cb.headers["location"])
        assert f"{back.scheme}://{back.netloc}{back.path}" == CLIENT_REDIRECT
        bq = parse_qs(back.query)
        assert bq["state"] == ["client-state"]

        form = {
            "grant_type": "authorization_code",
            "code": bq["code"][0],
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        }
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        tok = await c.post("/token", data=form)
        assert tok.status_code == 200, tok.text
        return server, app, c, client, tok.json(), calls


async def test_full_flow_public_client():
    server, app, _, _, tokens, calls = await run_flow("none")
    sw_token_call = [r for r in calls if r.url.path == "/oauth/token"][0]
    sent = parse_qs(sw_token_call.content.decode())
    assert sent["client_secret"] == ["sw-secret"]
    assert sent["redirect_uri"] == [PUBLIC + "/oauth/splitwise/callback"]

    access = await server._auth_server_provider.load_access_token(tokens["access_token"])
    assert isinstance(access, SplitwiseAccessToken)
    assert access.splitwise_token == "sw-user-token"
    # The Splitwise token is never visible in what the client receives.
    assert "sw-user-token" not in str(tokens)


async def test_full_flow_confidential_client_and_refresh():
    server, app, _, client, tokens, _ = await run_flow("client_secret_post")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.post("/token", data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        })
        assert r.status_code == 200, r.text
        refreshed = await server._auth_server_provider.load_access_token(r.json()["access_token"])
        assert refreshed.splitwise_token == "sw-user-token"

        bad = await c.post("/token", data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": "wrong",
        })
        assert bad.status_code in (400, 401)


async def test_mcp_endpoint_requires_token():
    server = create_server(config())
    app = server.streamable_http_app(stateless_http=True)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
        prm = await c.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == [PUBLIC]
        assert (await c.get("/health")).status_code == 200


async def test_tokens_do_not_cross_types_or_secrets():
    _, _, _, _, tokens, _ = await run_flow("none")
    provider = create_server(config())._auth_server_provider
    assert await provider.load_access_token(tokens["refresh_token"]) is None
    other = create_server(config("x" * 40))._auth_server_provider
    assert await other.load_access_token(tokens["access_token"]) is None


async def test_denied_and_tampered_callbacks():
    server = create_server(config())
    app = server.streamable_http_app(stateless_http=True)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        r = await c.get("/oauth/splitwise/callback", params={"code": "x", "state": "forged"})
        assert r.status_code == 400
        state = server._auth_server_provider.sealer.seal("state", {
            "client_id": "c", "redirect_uri": CLIENT_REDIRECT, "redirect_uri_provided_explicitly": True,
            "code_challenge": "x", "scopes": ["splitwise"], "resource": None, "state": "s",
        })
        r = await c.get("/oauth/splitwise/callback", params={"error": "access_denied", "state": state})
        assert r.status_code == 302
        assert "error=access_denied" in r.headers["location"]


async def test_consent_cancel_and_escaping():
    server = create_server(config())
    provider = server._auth_server_provider
    app = server.streamable_http_app(stateless_http=True)
    state = provider.sealer.seal("state", {
        "client_id": "c", "client_name": "<script>x</script>", "redirect_uri": CLIENT_REDIRECT,
        "redirect_uri_provided_explicitly": True, "code_challenge": "x", "scopes": ["splitwise"],
        "resource": None, "state": "s",
    })
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=PUBLIC) as c:
        page = await c.get("/oauth/consent", params={"state": state})
        assert "<script>x" not in page.text and "&lt;script&gt;" in page.text
        deny = await c.post("/oauth/consent", data={"state": state, "action": "deny"})
        assert deny.status_code == 303
        assert deny.headers["location"].startswith(CLIENT_REDIRECT) and "error=access_denied" in deny.headers["location"]
        forged = await c.post("/oauth/consent", data={"state": "forged", "action": "allow"})
        assert forged.status_code == 400
