"""AgentOS — Admin console API (admin role only).

GET    /api/v1/admin/users            — list users with usage in the 5h window
POST   /api/v1/admin/users/{id}/disable | enable — gate account access
GET    /api/v1/admin/stats            — platform-wide counters
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

WINDOW_HOURS = 5


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory




def _require_super(admin) -> None:
    if getattr(admin, "role", "") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")

@router.get("/users")
async def list_users(request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()

    users: List[Dict[str, Any]] = []
    repo = factory.user_repo
    if hasattr(repo, "list_users"):
        raw_users = await repo.list_users()
    else:
        raw_users = []
    for u in raw_users:
        user_id = u.get("user_id") or u.get("id") or ""
        entry = {
            "user_id": user_id,
            "email": u.get("email"),
            "name": u.get("name"),
            "role": u.get("role", "user"),
            "is_active": u.get("is_active", True),
            "created_at": u.get("created_at"),
        }
        try:
            runs = await factory.workflow_repo.list_runs(user_id, limit=200, offset=0)
            window_runs = [r for r in runs if str(r.get("created_at") or "") >= cutoff]
            entry["runs_total"] = len(runs)
            try:
                entry["mcps"] = len(await factory.mcp_repo.list_mcps(user_id=user_id))
            except Exception:
                entry["mcps"] = None
            entry["runs_5h"] = len(window_runs)
            entry["last_run_at"] = max((str(r.get("created_at") or "") for r in runs), default=None) or None
        except Exception:
            entry["runs_total"] = None
            entry["runs_5h"] = None
        users.append(entry)
    return {"users": users, "count": len(users), "window_hours": WINDOW_HOURS}


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: str, request: Request, admin = Depends(require_admin)):
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    factory = _get_factory(request)
    ok = await factory.user_repo.set_user_active(user_id, False) if hasattr(factory.user_repo, "set_user_active") else False
    if not ok:
        raise HTTPException(status_code=400, detail="User repo does not support activation changes")
    await factory.audit_repo.log_event({
        "event_type": "USER_DISABLED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {},
    })
    return {"user_id": user_id, "is_active": False}


@router.post("/users/{user_id}/enable")
async def enable_user(user_id: str, request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    ok = await factory.user_repo.set_user_active(user_id, True) if hasattr(factory.user_repo, "set_user_active") else False
    if not ok:
        raise HTTPException(status_code=400, detail="User repo does not support activation changes")
    await factory.audit_repo.log_event({
        "event_type": "USER_ENABLED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {},
    })
    return {"user_id": user_id, "is_active": True}



@router.delete("/users/{user_id}")
async def delete_any_user(user_id: str, request: Request, admin = Depends(require_admin)):
    """SUPER ADMIN ONLY: permanently delete any account, including admins."""
    _require_super(admin)
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="The sole super admin cannot delete their own account")
    factory = _get_factory(request)
    if not hasattr(factory.user_repo, "delete_user"):
        raise HTTPException(status_code=400, detail="User repo does not support deletion")
    ok = await factory.user_repo.delete_user(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    await factory.audit_repo.log_event({
        "event_type": "USER_DELETED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {"deleted_by": "super_admin"},
    })
    return {"user_id": user_id, "deleted": True}


@router.put("/users/{user_id}/limits")
async def set_user_limits(user_id: str, body: dict, request: Request, admin = Depends(require_admin)):
    """SUPER ADMIN ONLY: set a user's token limit (daily cap) and reset window (hours)."""
    _require_super(admin)
    factory = _get_factory(request)
    updates = {}
    if body.get("daily_token_limit") is not None:
        updates["daily_token_limit"] = max(1000, int(body["daily_token_limit"]))
    if body.get("window_hours") is not None:
        updates["window_hours"] = max(1, min(72, int(body["window_hours"])))
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    await factory.settings_repo.update_settings(user_id, updates)
    await factory.audit_repo.log_event({
        "event_type": "USER_LIMITS_CHANGED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": updates,
    })
    return {"user_id": user_id, **updates}


@router.post("/users/{user_id}/role")

async def set_user_role(user_id: str, body: dict, request: Request, admin = Depends(require_admin)):
    """Promote/demote a user's role: admin | user | viewer | guest."""
    role = str((body or {}).get("role") or "").strip().lower()
    if role not in ("admin", "user", "viewer", "guest"):
        raise HTTPException(status_code=400, detail="Role must be admin, user, viewer, or guest")
    if user_id == admin.user_id and role != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote your own account")
    factory = _get_factory(request)
    if role == "super_admin":
        _require_super(admin)
        # Only ONE super admin may exist: demote any previous holder first.
        try:
            for u in await factory.user_repo.list_users():
                if u.get("role") == "super_admin" and (u.get("user_id") or u.get("id")) != user_id:
                    await factory.user_repo.update_user(u.get("user_id") or u.get("id"), {"role": "admin"})
        except Exception:
            pass
    ok = await factory.user_repo.update_user(user_id, {"role": role}) if hasattr(factory.user_repo, "update_user") else False
    if not ok:
        raise HTTPException(status_code=400, detail="Could not update role")
    await factory.audit_repo.log_event({
        "event_type": "USER_ROLE_CHANGED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {"new_role": role},
    })
    return {"user_id": user_id, "role": role}


@router.get("/stats")
async def platform_stats(request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    users = (await factory.user_repo.list_users()) if hasattr(factory.user_repo, "list_users") else []
    total_runs = active_users_5h = 0
    mcps = 0
    try:
        mcps = len(await factory.mcp_repo.list_mcps())
    except Exception:
        pass
    for u in users:
        uid = u.get("user_id") or u.get("id") or ""
        try:
            runs = await factory.workflow_repo.list_runs(uid, limit=200, offset=0)
            total_runs += len(runs)
            if any(str(r.get("created_at") or "") >= cutoff for r in runs):
                active_users_5h += 1
        except Exception:
            continue
    return {
        "users_total": len(users),
        "users_active_5h": active_users_5h,
        "runs_total": total_runs,
        "integrations": mcps,
        "window_hours": WINDOW_HOURS,
    }
