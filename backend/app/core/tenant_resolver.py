"""Resolve tenant membership from server-controlled, verified identity metadata."""
from typing import Optional


class TenantResolver:
    @staticmethod
    def resolve_tenant_from_token(token_payload: dict) -> Optional[str]:
        """Caller must verify the token first; user-editable metadata is not authority."""
        metadata = token_payload.get("app_metadata") or {}
        tenant_id = metadata.get("tenant_id") or token_payload.get("tenant_id")
        return tenant_id if isinstance(tenant_id, str) and tenant_id.strip() else None

    @staticmethod
    def resolve_tenant_from_user(user_data: dict) -> Optional[str]:
        return TenantResolver.resolve_tenant_from_token(user_data)

    @staticmethod
    async def resolve_tenant_id(user_id: str, user_email: str, token: Optional[str] = None) -> Optional[str]:
        # Legacy callers: read server metadata instead of assigning a default tenant.
        from app.database import supabase
        response = supabase.auth.admin.get_user_by_id(user_id)
        user = getattr(response, "user", None)
        if user is None:
            return None
        return TenantResolver.resolve_tenant_from_user({
            "app_metadata": getattr(user, "app_metadata", {}),
        })

    @staticmethod
    async def update_user_tenant_metadata(user_id: str, tenant_id: str) -> None:
        pass
