from sqlalchemy import select
from sqlalchemy.orm import Session

from filoutil.db.models import UserPermission

ALLOWED_MODULES = {"monitoring", "moodle"}


def has_permission(db: Session, user_id: int, module: str) -> bool:
    """Check if a user has permission for a specific module."""
    if module not in ALLOWED_MODULES:
        return False
    result = db.execute(
        select(UserPermission).where(
            UserPermission.user_id == user_id, UserPermission.module == module
        )
    ).scalar_one_or_none()
    return result is not None


def grant_permission(
    db: Session, user_id: int, module: str, granted_by: int | None = None
) -> UserPermission:
    """Grant a permission to a user. Returns the permission object."""
    if module not in ALLOWED_MODULES:
        raise ValueError(f"Invalid module: {module}. Allowed modules: {ALLOWED_MODULES}")
    # Check if permission already exists
    existing = db.execute(
        select(UserPermission).where(
            UserPermission.user_id == user_id, UserPermission.module == module
        )
    ).scalar_one_or_none()

    if existing:
        return existing

    permission = UserPermission(user_id=user_id, module=module, granted_by=granted_by)
    db.add(permission)
    db.commit()
    db.refresh(permission)
    return permission


def revoke_permission(db: Session, user_id: int, module: str) -> bool:
    """Revoke a permission from a user. Returns True if permission was revoked, False if it didn't exist."""
    permission = db.execute(
        select(UserPermission).where(
            UserPermission.user_id == user_id, UserPermission.module == module
        )
    ).scalar_one_or_none()

    if permission:
        db.delete(permission)
        db.commit()
        return True
    return False


def get_user_permissions(db: Session, user_id: int) -> list[str]:
    """Get all module permissions for a user."""
    permissions = (
        db.execute(select(UserPermission).where(UserPermission.user_id == user_id)).scalars().all()
    )
    return [p.module for p in permissions]
