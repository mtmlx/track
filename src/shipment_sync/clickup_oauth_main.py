from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from dotenv import load_dotenv
import requests

from .project_paths import REPO_ROOT

AUTH_URL = "https://app.clickup.com/api"
TOKEN_URL = "https://api.clickup.com/api/v2/oauth/token"
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    env_path: Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize ClickUp OAuth for shipment sync")
    parser.add_argument("--code", help="Authorization code returned by ClickUp")
    parser.add_argument("--print-url", action="store_true", help="Print the authorization URL and exit")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the authorization URL in the default browser",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="How long to wait for the localhost callback when no --code is supplied",
    )
    args = parser.parse_args()

    load_dotenv()
    config = _load_config()
    state = secrets.token_urlsafe(32)
    auth_url = _build_auth_url(config, state=state)

    if args.print_url:
        print(auth_url)
        return

    code = args.code
    if not code:
        code = _capture_code(config, auth_url, state=state, timeout_seconds=max(args.timeout_seconds, 1), open_browser=not args.no_browser)

    try:
        token = _exchange_code(config, code)
    except requests.HTTPError as exc:
        response = exc.response
        if response is not None:
            snippet = (response.text or "").strip().replace("\n", " ")[:400]
            raise ValueError(
                f"ClickUp OAuth token exchange failed with HTTP {response.status_code}: {snippet}"
            ) from exc
        raise
    _write_env_value(config.env_path, "CLICKUP_OAUTH_ACCESS_TOKEN", token)
    _write_env_value(config.env_path, "CLICKUP_OAUTH_CLIENT_ID", config.client_id)
    _write_env_value(config.env_path, "CLICKUP_OAUTH_CLIENT_SECRET", config.client_secret)
    _write_env_value(config.env_path, "CLICKUP_OAUTH_REDIRECT_URI", config.redirect_uri)

    print("ClickUp OAuth access token saved to .env")
    print(f"Redirect URI: {config.redirect_uri}")
    print("Shipment sync will now prefer CLICKUP_OAUTH_ACCESS_TOKEN over CLICKUP_API_TOKEN.")


def _load_config() -> OAuthConfig:
    client_id = (os.getenv("CLICKUP_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("CLICKUP_OAUTH_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("CLICKUP_OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip()
    if not client_id:
        raise ValueError("Missing CLICKUP_OAUTH_CLIENT_ID in .env")
    if not client_secret:
        raise ValueError("Missing CLICKUP_OAUTH_CLIENT_SECRET in .env")
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        env_path=REPO_ROOT / ".env",
    )


def _build_auth_url(config: OAuthConfig, *, state: str) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def _capture_code(
    config: OAuthConfig,
    auth_url: str,
    *,
    state: str,
    timeout_seconds: int,
    open_browser: bool,
) -> str:
    parsed = urlparse(config.redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise ValueError(
            "Automatic OAuth callback capture requires CLICKUP_OAUTH_REDIRECT_URI to use "
            "http://localhost:<port>/... or http://127.0.0.1:<port>/.... "
            "Otherwise run clickup-oauth --print-url, complete approval manually, "
            "and rerun with --code."
        )

    path = parsed.path or "/"
    holder: dict[str, Any] = {"code": None, "error": None}
    server = HTTPServer((parsed.hostname, parsed.port), _build_handler(path, holder, expected_state=state))
    server.timeout = timeout_seconds

    print("Open this URL in your browser and approve the ClickUp app:")
    print(auth_url)
    if open_browser:
        webbrowser.open(auth_url)

    deadline_message = f"Waiting up to {timeout_seconds} seconds for ClickUp callback on {config.redirect_uri}..."
    print(deadline_message)

    deadline = time.monotonic() + timeout_seconds
    try:
        while holder["code"] is None and holder["error"] is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()

    if holder["error"]:
        raise ValueError(f"ClickUp OAuth failed: {holder['error']}")
    if not holder["code"]:
        raise TimeoutError(
            "Timed out waiting for ClickUp OAuth callback. "
            "Rerun with --print-url or --code if you already approved the app."
        )
    return str(holder["code"])


def _build_handler(expected_path: str, holder: dict[str, Any], *, expected_state: str):
    if not expected_state:
        raise ValueError("OAuth state must not be empty")

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return

            query = parse_qs(parsed.query, keep_blank_values=True)
            states = query.get("state", [])
            if len(states) != 1 or not secrets.compare_digest(states[0].encode(), expected_state.encode()):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OAuth state.")
                return
            holder["code"] = _first(query.get("code"))
            holder["error"] = _first(query.get("error")) or _first(query.get("error_description"))

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if holder["code"]:
                body = (
                    "<html><body><h2>ClickUp authorization received.</h2>"
                    "<p>You can close this window and return to Terminal.</p></body></html>"
                )
            else:
                detail = html.escape(str(holder["error"] or "Unknown error"))
                body = (
                    "<html><body><h2>ClickUp authorization failed.</h2>"
                    f"<p>{detail}</p></body></html>"
                )
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            return

    return CallbackHandler


def _exchange_code(config: OAuthConfig, code: str) -> str:
    payload = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
    }
    response = requests.post(TOKEN_URL, json=payload, timeout=30)
    response.raise_for_status()
    data = _safe_json(response)
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise ValueError(f"ClickUp OAuth token exchange did not return access_token: {data}")
    return access_token


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError(
            f"Could not parse ClickUp OAuth response as JSON: {(response.text or '').strip()[:240]}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected ClickUp OAuth response payload: {json.dumps(payload)[:240]}")
    return payload


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    first = values[0].strip()
    return first or None


def _write_env_value(path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    rendered = f"{key}={value}"
    updated = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            lines[idx] = rendered
            updated = True
            break

    if not updated:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(rendered)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
