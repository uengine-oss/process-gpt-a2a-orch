# logger_config.py
"""
web_hook 모듈용 SmartLogger 설정
A2A 서버/클라이언트에서 공통으로 사용하는 SmartLogger 인스턴스를 제공합니다.
"""

import sys
from pathlib import Path

# SmartLogger 임포트를 위한 경로 설정
_SANDBOX_DIR = Path(__file__).parent.parent.parent  # sandbox 폴더
sys.path.insert(0, str(_SANDBOX_DIR))

from smart_logger import SmartLogger

# 로그 디렉토리 설정
_LOG_DIR = Path(__file__).parent / "logs"

# 공통 SmartLogger 인스턴스 생성
smart_logger = SmartLogger(
    main_log_path=str(_LOG_DIR / "a2a_flow.jsonl"),
    detail_log_dir=str(_LOG_DIR / "details"),
    blacklisted_log_path=str(_LOG_DIR / "a2a_flow_blacklisted.jsonl"),
    blacklisted_detail_log_dir=str(_LOG_DIR / "details_blacklisted"),
    min_level="DEBUG",
    console_output=True,
    file_output=True,
    remove_log_on_create=True
)


# 카테고리 상수 정의
class LogCategory:
    """로그 카테고리 상수"""
    CLIENT = "a2a_client"
    WEBHOOK_RECEIVER = "webhook_receiver"
    SERVER = "a2a_server"
    EXECUTOR = "agent_executor"


def get_logger():
    """SmartLogger 인스턴스 반환"""
    return smart_logger


if __name__ == "__main__":
    # 테스트
    logger = get_logger()
    logger.log("INFO", "SmartLogger 테스트", category=LogCategory.SERVER, params={"test": True})
    print("✅ SmartLogger 설정 완료!")

