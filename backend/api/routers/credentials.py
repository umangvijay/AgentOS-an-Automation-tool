"""
AgentOS — User Credentials Router

Named, vault-encrypted credentials the agents can use on the user's behalf:
site logins for the browser agent, SMTP settings for email, API keys for
integrations (e.g. Stripe).

POST   /api/v1/credentials          — Store/replace a named credential
GET    /api/v1/credentials          — List credential names (values are never returned)
DELETE /api/v1/credentials/{name}   — Delete a credential

Storage format: the values dict is JSON-encoded, encrypted with AES-256-GCM
via the SecretsVault, and stored under the key "cred:{name}".
"""

import json
import logging
import re
from typing import Dict

from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.secrets_vault import secrets_vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credentials", tags=["credentials"])

CRED_PREFIX = "cred:"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


class StoreCredentialRequest(BaseModel):
    name: str                    # e.g. "fluentedge", "smtp", "stripe"
    values: Dict[str, str]       # e.g. {"username": "...", "password": "..."} or {"api_key": "..."}


@router.post("", status_code=status.HTTP_201_CREATED)
async def store_credential(
    body: StoreCredentialRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Encrypt and store a named credential for the current user."""
    factory = _get_factory(request)

    if not NAME_PATTERN.match(body.name):
        raise HTTPException(
            status_code=400,
            detail="Credential name must be 1-64 chars: letters, digits, dot, dash, underscore",
        )
    if not body.values:
        raise HTTPException(status_code=400, detail="Credential values cannot be empty")
    if len(json.dumps(body.values)) > 16_000:
        raise HTTPException(status_code=400, detail="Credential payload too large")
    # Validate LLM provider keys server-side before storing — reject junk with 400
    if body.name in ("gemini", "grok") and "api_key" in body.values:
        import httpx
        key = body.values["api_key"].strip()
        if not key:
            raise HTTPException(status_code=400, detail="API key cannot be empty")
        if body.name == "gemini":
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}",
                        json={"contents": [{"parts": [{"text": "Reply VALID"}]}]},
                    )
                    if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                        raise HTTPException(status_code=400, detail="Invalid Gemini API key")
                    if resp.status_code == 403:
                        raise HTTPException(status_code=400, detail="Gemini API key is forbidden — check project restrictions")
                    if resp.status_code not in (200, 429):
                        raise HTTPException(status_code=400, detail=f"Gemini key check failed: HTTP {resp.status_code}")
            except httpx.HTTPError as e:
                raise HTTPException(status_code=400, detail=f"Could not validate Gemini key: {str(e)[:200]}")
        elif body.name == "grok":
            if not key.startswith("xai-"):
                raise HTTPException(status_code=400, detail="Grok key must start with 'xai-'")
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": "grok-3-mini-fast", "messages": [{"role": "user", "content": "VALID"}], "max_tokens": 5},
                    )
                    if resp.status_code == 401:
                        raise HTTPException(status_code=400, detail="Invalid xAI API key")
                    if resp.status_code not in (200, 429):
                        raise HTTPException(status_code=400, detail=f"xAI key check failed: HTTP {resp.status_code}")
            except httpx.HTTPError as e:
                raise HTTPException(status_code=400, detail=f"Could not validate Grok key: {str(e)[:200]}")

    encrypted = secrets_vault.encrypt(json.dumps(body.values))
    await factory.secrets_repo.store_secret(user.user_id, f"{CRED_PREFIX}{body.name}", encrypted)

    await factory.audit_repo.log_event({
        "event_type": "CREDENTIAL_STORED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": body.name,
        "details": {"fields": sorted(body.values.keys())},  # field names only, never values
    })

    return {"name": body.name, "fields": sorted(body.values.keys()), "stored": True}


class ValidateCredentialRequest(BaseModel):
    name: str
    values: Dict[str, str]


@router.post("/validate")
async def validate_credential(
    body: ValidateCredentialRequest,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Validate a credential by testing it against the provider's API.
    Returns { valid: true } or { valid: false, error: "..." }.
    Does NOT store anything — frontend should call this BEFORE store.
    """
    key = body.values.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key is required")

    if body.name == "gemini":
        # Validate Gemini key with a real API call
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": "Reply with exactly: VALID"}]}]},
                )
                if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                    return {"valid": False, "error": "Invalid Gemini API key. Get one from https://aistudio.google.com/apikey"}
                if resp.status_code == 403:
                    return {"valid": False, "error": "API key is forbidden — check project restrictions."}
                if resp.status_code == 429:
                    # Key is valid but rate-limited — still a valid key
                    return {"valid": True}
                if resp.status_code == 200:
                    return {"valid": True}
                return {"valid": False, "error": f"Gemini returned HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach Gemini API: {str(e)[:200]}"}

    elif body.name == "grok":
        # Validate xAI/Grok key
        import httpx
        if not key.startswith("xai-"):
            return {"valid": False, "error": "Grok key must start with 'xai-'"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": "grok-3-mini-fast", "messages": [{"role": "user", "content": "Reply VALID"}], "max_tokens": 5},
                )
                if resp.status_code == 401:
                    return {"valid": False, "error": "Invalid xAI API key."}
                if resp.status_code == 429:
                    return {"valid": True}  # rate-limited but valid
                if resp.status_code == 200:
                    return {"valid": True}
                return {"valid": False, "error": f"xAI returned HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach xAI API: {str(e)[:200]}"}

    elif body.name == "claude":
        # Validate Anthropic key with a tiny live completion
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5", "max_tokens": 5, "messages": [{"role": "user", "content": "Reply VALID"}]},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid Anthropic API key."}
            if resp.status_code in (200, 429):
                return {"valid": True}
            return {"valid": False, "error": f"Anthropic returned HTTP {resp.status_code}: {resp.text[:150]}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach Anthropic: {str(e)[:150]}"}

    elif body.name == "zai":
        # Z.ai GLM — OpenAI-compatible endpoint
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.z.ai/api/paas/v4/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": "glm-4.5-flash", "messages": [{"role": "user", "content": "Reply VALID"}], "max_tokens": 5},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid Z.ai API key."}
            if resp.status_code in (200, 429):
                return {"valid": True}
            return {"valid": False, "error": f"Z.ai returned HTTP {resp.status_code}: {resp.text[:150]}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach Z.ai: {str(e)[:150]}"}

    elif body.name == "openai":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid OpenAI API key."}
            if resp.status_code == 200:
                return {"valid": True}
            return {"valid": False, "error": f"OpenAI returned HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach OpenAI: {str(e)[:150]}"}

    # Unknown credential type — skip validation, just allow
    return {"valid": True}


@router.get("")
async def list_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List the user's credential names. Values are never returned."""
    factory = _get_factory(request)
    keys = await factory.secrets_repo.list_secret_keys(user.user_id)
    names = [k[len(CRED_PREFIX):] for k in keys if k.startswith(CRED_PREFIX)]
    return {"credentials": names, "count": len(names)}


@router.delete("/{name}")
async def delete_credential(
    name: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Delete a named credential."""
    factory = _get_factory(request)
    deleted = await factory.secrets_repo.delete_secret(user.user_id, f"{CRED_PREFIX}{name}")
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")

    await factory.audit_repo.log_event({
        "event_type": "CREDENTIAL_DELETED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": name,
        "details": {},
    })
    return {"name": name, "deleted": True}


async def load_credential(secrets_repo, user_id: str, name: str) -> Dict[str, str]:
    """Resolve and decrypt a named credential for internal service use."""
    encrypted = await secrets_repo.get_secret(user_id, f"{CRED_PREFIX}{name}")
    if not encrypted:
        raise ValueError(f"No credential named '{name}' is stored")
    return json.loads(secrets_vault.decrypt(encrypted))
