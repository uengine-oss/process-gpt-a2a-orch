import os
import logging
from typing import List, Dict, Any, Optional
from supabase import create_client, Client
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Module-level Supabase client (singleton pattern)
_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """
    Get or create a Supabase client instance (singleton).
    The client is created once and reused for all subsequent calls.
    
    Returns:
        Client: Supabase client instance
        
    Raises:
        ValueError: If required environment variables are not set
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
    
    try:
        _supabase_client = create_client(supabase_url, supabase_key)
        logger.info("Supabase client created successfully")
        return _supabase_client
    except Exception as e:
        logger.error(f"Failed to create Supabase client: {e}")
        raise


async def fetch_form_by_id(form_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch records from the form_def table.
    
    Args:
        form_id: form ID to filter by
        tenant_id: Optional tenant ID to filter by

    Returns:
        List[Dict[str, Any]]: List of form_def records
        
    Raises:
        Exception: If the query fails
    """
    try:
        client = get_supabase_client()
        query = client.table("form_def").select("*").eq("id", form_id)
        
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)

        response = query.execute()
        if len(response.data) == 0:
            return None
        
        logger.info(f"Retrieved {len(response.data)} records from form_def table")
        return response.data
    except Exception as e:
        logger.error(f"Failed to query form_def table: {e}")
        raise


async def fetch_workitem_by_id(task_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch records from the todolist table.
    
    Args:
        task_id: todo ID to filter by
        tenant_id: Optional tenant ID to filter by
        
    Returns:
        List[Dict[str, Any]]: List of todolist records
        
    Raises:
        Exception: If the query fails
    """
    try:
        client = get_supabase_client()
        query = client.table("todolist").select("*").eq("id", task_id)
        
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        
        response = query.execute()
        if len(response.data) == 0:
            return None
        
        logger.info(f"Retrieved {len(response.data)} records from todolist table")
        return response.data
    except Exception as e:
        logger.error(f"Failed to query todolist table: {e}")
        raise

