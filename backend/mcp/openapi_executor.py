"""Execute registered OpenAPI tools as real HTTP calls. No synthetic responses."""

import base64
import json
import logging
from typing import Any, Dict, Optional

import httpx

from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
from backend.security.secrets_vault import secrets_vault

logger = logging.getLogger(__name__)


def _apply_auth(headers: Dict[str, str], query_params: Dict[str, Any], auth: Dict[str, Any], secret: str) -> None:
    """Attach the stored secret using the auth scheme the API actually expects.

    The MCP factory records where the spec says the credential goes
    ({"in": "header"|"query", "name": "X-API-Key"} / Bearer / Basic).
    Legacy manifests without placement default to Bearer, matching prior behavior.
    """
    auth_type = str(auth.get("type") or "API_KEY").upper()
    location = str(auth.get("in") or "header").lower()
    name = str(auth.get("name") or "").strip()

    if auth_type == "BASIC":
        token = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        return
    if auth_type in ("BEARER", "OAUTH2", "OAUTH"):
        headers["Authorization"] = f"Bearer {secret}"
        return
    if auth_type == "NONE":
        return
    # API_KEY (default): respect the spec's declared placement.
    if location == "query":
        query_params[name or "api_key"] = secret
        return
    header_name = name or "Authorization"
    if header_name.lower() == "authorization":
        headers["Authorization"] = f"Bearer {secret}"
    else:
        headers[header_name] = secret


async def resolve_secret(user_id: str, credential_ref: Optional[str], secrets_repo=None) -> Optional[str]:
    if not credential_ref or not secrets_repo:
        return None
    encrypted = await secrets_repo.get_secret(user_id, credential_ref)
    if not encrypted:
        return None
    decrypted = secrets_vault.decrypt(encrypted)
    # Named credentials (cred:*) store a JSON dict; extract the token field.
    if decrypted.lstrip().startswith("{"):
        try:
            values = json.loads(decrypted)
            if isinstance(values, dict):
                for field in ("api_key", "token", "access_token", "secret_key", "password"):
                    if values.get(field):
                        return str(values[field])
                if len(values) == 1:
                    return str(next(iter(values.values())))
        except (ValueError, TypeError):
            pass
    return decrypted


async def execute_openapi_tool(
    manifest: Dict[str, Any],
    tool: Dict[str, Any],
    arguments: Dict[str, Any],
    *,
    user_id: str = "system",
    secrets_repo=None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    operation = tool.get("operation") or {}
    if not operation:
        raise ValueError(f"Tool {tool.get('tool_name')} has no stored OpenAPI operation")
    if operation.get("kind") == "browser":
        from backend.mcp.website_mcp import execute_browser_tool
        return await execute_browser_tool(
            tool, arguments, user_id=user_id, secrets_repo=secrets_repo,
            run_id=run_id, task_id=task_id,
        )

    parser = OpenAPIParser()
    servers = operation.get("servers") or []
    base_url = servers[0]["url"] if servers and isinstance(servers[0], dict) else (servers[0] if servers else "")
    if not base_url:
        raise ValueError("OpenAPI operation has no server URL")
    parser._validate_ssrf(base_url)

    path = operation.get("path") or ""
    query_params: Dict[str, Any] = {}
    headers: Dict[str, str] = {}
    json_body = None
    arguments = arguments or {}

    for param in operation.get("parameters") or []:
        name = param.get("name")
        val = arguments.get(name)
        if val is None:
            if param.get("required"):
                raise ValueError(f"Missing required parameter: {name}")
            continue
        loc = param.get("in", "query")
        if loc == "path":
            path = path.replace(f"{{{name}}}", str(val))
        elif loc == "header":
            headers[name] = str(val)
        else:
            query_params[name] = val

    param_names = {p.get("name") for p in (operation.get("parameters") or [])}
    extra = {k: v for k, v in arguments.items() if k not in param_names}
    if extra:
        json_body = extra.get("request_body") if "request_body" in extra and len(extra) == 1 else extra

    auth = manifest.get("auth") or {}
    if isinstance(auth, str):
        auth = json.loads(auth)
    credential_ref = auth.get("credential_ref")
    secret = await resolve_secret(user_id, credential_ref, secrets_repo)
    if secret:
        _apply_auth(headers, query_params, auth, secret)

    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    parser._validate_ssrf(url)

    method = (operation.get("http_method") or "GET").upper()
    redirect_hops = 0
    while True:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    params=query_params,
                    headers=headers,
                    json=json_body,
                )
            except SSRFViolationError:
                raise
            except httpx.HTTPError as e:
                from backend.engine.failure_classifier import classify_failure
                failure = classify_failure(message=str(e))
                return {"ok": False, "error": str(e), "url": url, "method": method,
                        "failure": {"category": failure.category, "retryable": failure.retryable,
                                    "recovery_hint": failure.recovery_hint}}

        if response.status_code in (301, 302, 303, 307, 308) and redirect_hops < 3:
            loc = response.headers.get("Location", "")
            if not loc:
                break
            next_url = httpx.URL(url).join(loc)
            parser._validate_ssrf(str(next_url))
            url = str(next_url)
            if next_url.params:
                query_params = {}
            redirect_hops += 1
            # 303 (and de-facto 301/302 on non-GET) convert the body to a GET
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method != "GET"
            ):
                method = "GET"
                json_body = None
            continue
        break

    body: Any
    try:
        body = response.json()
    except Exception:
        body = response.text

    if response.is_error:
        from backend.engine.failure_classifier import classify_failure
        failure = classify_failure(status=response.status_code, message=str(body)[:300])
        return {
            "ok": False,
            "status": response.status_code,
            "url": url,
            "method": method,
            "body": body,
            "failure": {
                "category": failure.category,
                "retryable": failure.retryable,
                "recovery_hint": failure.recovery_hint,
            },
        }
    return {
        "ok": True,
        "status": response.status_code,
        "url": url,
        "method": method,
        "body": body,
    }
