from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

from a2a_agent_webhook_receiver.smart_logger import SmartLogger

LOG_CATEGORY = "A2A_WEBHOOK_RECEIVER_DB"

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
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
    SmartLogger.log(
        "INFO",
        "Supabase client created successfully (webhook receiver)",
        category=LOG_CATEGORY,
    )
    return _supabase_client


async def fetch_workitem_by_id(
    task_id: str, tenant_id: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch records from the todolist table by id.
    """
    client = get_supabase_client()
    query = client.table("todolist").select("*").eq("id", task_id)
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)

    response = query.execute()
    if not response.data:
        return None
    return response.data


def _looks_like_webhook_accepted_event_data(data: Any) -> bool:
    """
    Webhook mode에서 executor가 남기는 'webhook_accepted' 이벤트는 event_type='task_started'로 저장됩니다.
    이 이벤트는 프론트의 task 카드 매칭 키(job_id)로 쓰면 안 되므로 제외합니다.
    """
    try:
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            return (data.get("subtype") or "").strip() == "webhook_accepted"
    except Exception:
        return False
    return False


async def fetch_task_started_job_id_for_todolist(
    todolist_id: str,
    *,
    limit: int = 10,
) -> Optional[str]:
    """
    동일 todolist 작업의 시작 이벤트(task_started)의 job_id를 찾아 반환합니다.

    목적:
    - webhook receiver가 task_completed/task_failed 이벤트를 기록할 때,
      executor가 기록한 task_started(job_id)와 동일한 키를 재사용해
      프론트에서 started↔completed 매칭이 깨지지 않게 합니다.

    조회 규칙:
    - todo_id == todolist_id
    - crew_type == 'task'
    - event_type == 'task_started'
    - data.subtype == 'webhook_accepted' 인 이벤트는 제외(부가 이벤트)
    """
    client = get_supabase_client()

    def _query_with_order(order_col: Optional[str]) -> List[Dict[str, Any]]:
        q = (
            client.table("events")
            .select("job_id,data,event_type,crew_type,todo_id,timestamp")
            .eq("todo_id", str(todolist_id))
            .eq("crew_type", "task")
            .eq("event_type", "task_started")
        )
        if order_col:
            q = q.order(order_col, desc=True)
        resp = q.limit(int(limit)).execute()
        return resp.data or []

    rows: List[Dict[str, Any]] = []
    try:
        rows = _query_with_order("timestamp")
    except Exception:
        try:
            rows = _query_with_order(None)
        except Exception:
            rows = []


    for r in rows:
        job_id_val = (r.get("job_id") or "").strip()
        if not job_id_val:
            continue
        if _looks_like_webhook_accepted_event_data(r.get("data")):
            continue
        return job_id_val

    return None


