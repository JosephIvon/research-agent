"""审计日志模块 - 记录所有重要操作"""
import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

logger = logging.getLogger("audit")

class AuditEvent(str, Enum):
    USER_LOGIN = "user.login"
    USER_REGISTER = "user.register"
    RESEARCH_START = "research.start"
    RESEARCH_COMPLETE = "research.complete"
    RESEARCH_FAIL = "research.fail"
    CRAWL_PAGE = "crawl.page"
    LLM_CALL = "llm.call"
    DOC_SYNC = "doc.sync"
    SETTINGS_UPDATE = "settings.update"
    CIRCUIT_BREAKER_OPEN = "circuit.breaker.open"

class AuditLogger:
    def log(self, event: AuditEvent, user_id: Optional[str], details: Dict[str, Any], level: str = "info"):
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event.value,
            "user_id": user_id or "anonymous",
            "details": details,
        }
        getattr(logger, level)(json.dumps(entry, ensure_ascii=False))

_audit_logger: Optional[AuditLogger] = None

def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger