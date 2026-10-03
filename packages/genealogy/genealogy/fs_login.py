"""Local FamilySearch OAuth login (authorization code + PKCE).

Opens the FamilySearch sign-in page, catches the redirect on a localhost
listener, exchanges the code for a token, and saves it via TokenStore — to SSM
when FAMILYSEARCH_TOKEN_SSM_PATH is set (so the Lambda can use it), else to a
local file.

Requires FAMILYSEARCH_CLIENT_ID (your app key) and a redirect URI registered
for that key (FAMILYSEARCH_REDIRECT_URI, default http://127.0.0.1:5000/callback).
"""

import base64
import hashlib
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from genealogy._familysearch import TokenStore, environment, token_request


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def cli() -> None:
    client_id = os.environ.get("FAMILYSEARCH_CLIENT_ID")
    if not client_id:
        sys.exit("Set FAMILYSEARCH_CLIENT_ID to your FamilySearch app key.")
    redirect_uri = os.environ.get(
        "FAMILYSEARCH_REDIRECT_URI", "http://127.0.0.1:5000/callback"
    )
    redirect = urlparse(redirect_uri)
    env = environment()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_url = (
        env.authorize_url
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile",
            }
        )
    )

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            url = urlparse(self.path)
            if url.path != redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"FamilySearch login complete. You can close this tab.")

        def log_message(self, format, *args):  # noqa: A002 - silence request logging
            pass

    server = HTTPServer(
        (redirect.hostname or "127.0.0.1", redirect.port or 80), Handler
    )
    print(f"Opening browser to sign in to FamilySearch ({env.ident})...")
    print(f"If it doesn't open, visit:\n  {auth_url}")
    webbrowser.open(auth_url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        sys.exit(
            f"Authorization failed: {result.get('error')} {result.get('error_description', '')}"
        )
    if result.get("state") != state:
        sys.exit("State mismatch; aborting.")

    with httpx.Client(timeout=30) as http:
        resp = token_request(
            http,
            env,
            {
                "grant_type": "authorization_code",
                "code": result["code"],
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    where = TokenStore().save(resp.json())
    print(f"Saved FamilySearch token to {where}")


if __name__ == "__main__":
    cli()
