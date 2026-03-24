"""
Local LLM helper (no external llm_factory dependency).

We intentionally avoid `llm_factory` and build ChatOpenAI directly.
This also hardens against transient streaming/SSE transport errors like:
  httpx.RemoteProtocolError: incomplete chunked read

Key point: LangChain agents may use `.astream()` internally even when `streaming=False`.
Setting `disable_streaming=True` forces the underlying model to not use streaming transport.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple, Union

TimeoutType = Union[float, Tuple[float, float]]


def create_llm(
    model: Optional[str] = None,
    streaming: bool = False,  # compatibility + opt-in streaming support
    temperature: float = 0.0,
    timeout: Optional[TimeoutType] = (10.0, 120.0),  # connect, read
    max_retries: int = 6,
):
    """
    Standard ChatOpenAI constructor wrapper used across the project.
    """
    from langchain_openai import ChatOpenAI

    base_url = os.getenv("LLM_PROXY_URL", "http://litellm-proxy:4000")
    api_key = os.getenv("LLM_PROXY_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    resolved_model = model or (os.getenv("LLM_MODEL") or "").strip()
    if not resolved_model:
        raise ValueError("LLM model is not set. Please configure LLM_MODEL or pass model explicitly.")

    # Full compatibility mode:
    # - streaming=False: force non-streaming transport for stability
    # - streaming=True: allow original streaming behavior
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=resolved_model,
        temperature=temperature,
        streaming=streaming,
        disable_streaming=not streaming,
        timeout=timeout,
        max_retries=max_retries,
    )
