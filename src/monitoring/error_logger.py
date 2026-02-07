"""
Error logger for Sticker Trendz.

Records errors to the error_log table in Supabase. Sanitizes all error
messages and context to strip PII and API keys before storage.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from src.db import SupabaseClient, DatabaseError

logger = logging.getLogger(__name__)

# Patterns that look like secrets or PII
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # API keys and tokens
    re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(r8_[a-zA-Z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+[a-zA-Z0-9._\-]{20,})", re.IGNORECASE),
    re.compile(r"(token[=:]\s*[a-zA-Z0-9._\-]{20,})", re.IGNORECASE),
    re.compile(r"(key[=:]\s*[a-zA-Z0-9._\-]{20,})", re.IGNORECASE),
    re.compile(r"(secret[=:]\s*[a-zA-Z0-9._\-]{20,})", re.IGNORECASE),
    re.compile(r"(password[=:]\s*\S+)", re.IGNORECASE),
    # Email addresses
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    # Credit card-like patterns (13-19 digits)
    re.compile(r"\b\d{13,19}\b"),
]

# Keys in context dicts that should never be stored
_PII_KEYS = frozenset({
    "customer_name", "customer_email", "customer_address", "email",
    "address", "phone", "name", "password", "api_key", "secret",
    "access_token", "refresh_token", "credit_card", "ssn",
})


def sanitize_string(text: str) -> str:
    """
    Remove sensitive data patterns from a string.

    Replaces API keys, tokens, email addresses, and other PII patterns
    with '[REDACTED]'.
    """
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def sanitize_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove PII and secrets from a context dictionary.

    Drops keys that are known PII fields and sanitizes string values.
    """
    clean: Dict[str, Any] = {}
    for key, value in context.items():
        if key.lower() in _PII_KEYS:
            continue
        if isinstance(value, str):
            clean[key] = sanitize_string(value)
        elif isinstance(value, dict):
            clean[key] = sanitize_context(value)
        else:
            clean[key] = value
    return clean


class ErrorLogger:
    """
    Logger that records errors to the error_log Supabase table.

    All error messages and context are sanitized to strip PII and API
    keys before they are written to the database.
    """

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self._db = db or SupabaseClient()

    def log_error(
        self,
        workflow: str,
        step: str,
        error_type: str,
        error_message: str,
        service: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
        retry_count: int = 0,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Write an error to the error_log table.

        Args:
            workflow: Which workflow hit the error (e.g. 'trend_monitor').
            step: Which step within the workflow (e.g. 'scoring', 'image_gen').
            error_type: Category of error ('api_error', 'rate_limit', 'timeout',
                        'validation', 'auth').
            error_message: The error message (will be sanitized).
            service: External service involved ('reddit', 'openai', etc.).
            pipeline_run_id: UUID of the current pipeline run, if any.
            retry_count: How many retries were attempted.
            context: Additional context (trend_id, sticker_id, etc.).
                     Will be sanitized to remove PII.

        Returns:
            The error_log row ID, or None if logging failed.
        """
        safe_message = sanitize_string(error_message)
        safe_context = sanitize_context(context) if context else None

        data: Dict[str, Any] = {
            "workflow": workflow,
            "step": step,
            "error_type": error_type,
            "error_message": safe_message,
            "retry_count": retry_count,
            "resolved": False,
        }

        if service:
            data["service"] = service
        if pipeline_run_id:
            data["pipeline_run_id"] = pipeline_run_id
        if safe_context:
            data["context"] = safe_context

        try:
            result = self._db.insert_error(data)
            error_id = result.get("id")
            logger.info(
                "Error logged: workflow=%s step=%s type=%s service=%s",
                workflow, step, error_type, service,
            )
            return error_id
        except DatabaseError as exc:
            # Last-resort: if we can't write to the DB, at least log locally
            logger.critical(
                "Failed to write to error_log table: %s. Original error: %s",
                exc, safe_message,
            )
            return None

    def resolve_error(self, error_id: str) -> None:
        """Mark an error as resolved."""
        try:
            self._db.update("error_log", {"id": error_id}, {"resolved": True})
            logger.info("Error %s marked as resolved", error_id)
        except DatabaseError as exc:
            logger.error("Failed to resolve error %s: %s", error_id, exc)

    def check_consecutive_failures(self, workflow: str, threshold: int = 3) -> bool:
        """
        Check whether the last N errors for a workflow are all unresolved.

        Used by GitHub Actions monitoring to decide if an alert should fire.

        Args:
            workflow: The workflow to check.
            threshold: Number of consecutive failures to look for.

        Returns:
            True if there are >= threshold consecutive unresolved errors.
        """
        try:
            recent = self._db.get_recent_errors(workflow, limit=threshold)
            if len(recent) < threshold:
                return False
            return all(not row.get("resolved", True) for row in recent)
        except DatabaseError as exc:
            logger.error("Failed to check consecutive failures for '%s': %s", workflow, exc)
            return False

    def get_unresolved_errors(
        self, workflow: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Return recent unresolved errors, optionally filtered by workflow."""
        filters: Dict[str, Any] = {"resolved": False}
        if workflow:
            filters["workflow"] = workflow
        try:
            return self._db.select(
                "error_log",
                filters=filters,
                order_by="-created_at",
                limit=limit,
            )
        except DatabaseError as exc:
            logger.error("Failed to fetch unresolved errors: %s", exc)
            return []
