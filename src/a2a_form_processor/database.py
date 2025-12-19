import logging
import os
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

logger = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """
    Get or create a Supabase client instance (singleton).
    """
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url:
        raise ValueError("SUPABASE_URL environment variable is not set")
    if not supabase_key:
        raise ValueError("SUPABASE_KEY environment variable is not set")

    _supabase_client = create_client(supabase_url, supabase_key)
    logger.info("Supabase client created successfully (a2a_form_processor)")
    return _supabase_client


async def fetch_form_by_id(form_id: str, tenant_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    client = get_supabase_client()
    query = client.table("form_def").select("*").eq("id", form_id)
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    response = query.execute()
    if not response.data:
        return None
    return response.data


async def fetch_workitem_by_id(task_id: str, tenant_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    client = get_supabase_client()
    query = client.table("todolist").select("*").eq("id", task_id)
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    response = query.execute()
    if not response.data:
        return None
    return response.data


