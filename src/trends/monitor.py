"""
Trend monitor orchestrator for Sticker Trendz.

Orchestrates the 2-hour trend detection cycle:
  1. Fetch from Reddit and Google Trends
  2. Deduplicate
  3. Check blocklists
  4. Score with GPT-4o-mini
  5. Store qualifying trends in Supabase

Enforces per-cycle cap (5 trends) and daily cap (30 scored trends).
Sets GitHub Actions output for conditional job execution.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.config import load_config, setup_logging
from src.db import SupabaseClient, DatabaseError
from src.monitoring.pipeline_logger import PipelineRunLogger
from src.monitoring.error_logger import ErrorLogger
from src.monitoring.alerter import EmailAlerter
from src.monitoring.spend_tracker import SpendTracker
from src.publisher.etsy_rate_limiter import EtsyRateLimiter
from src.trends.sources.reddit import RedditSource
from src.trends.sources.google_trends import GoogleTrendsSource
from src.trends.dedup import deduplicate_trends, check_existing_trends
from src.trends.scorer import TrendScorer, OVERALL_THRESHOLD
from src.moderation.blocklist import check_all as check_blocklists

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "trend_monitor"


class TrendMonitor:
    """
    Main trend monitor that orchestrates the 2-hour trend detection cycle.

    Handles partial failures gracefully -- if one source fails, continues
    with the remaining source(s).
    """

    def __init__(
        self,
        db: Optional[SupabaseClient] = None,
        reddit_source: Optional[RedditSource] = None,
        google_source: Optional[GoogleTrendsSource] = None,
        scorer: Optional[TrendScorer] = None,
        rate_limiter: Optional[EtsyRateLimiter] = None,
        pipeline_logger: Optional[PipelineRunLogger] = None,
        error_logger: Optional[ErrorLogger] = None,
        alerter: Optional[EmailAlerter] = None,
        spend_tracker: Optional[SpendTracker] = None,
        max_trends_per_cycle: int = 5,
        max_daily_scored: int = 30,
    ) -> None:
        self._db = db or SupabaseClient()
        self._reddit = reddit_source
        self._google = google_source
        self._scorer = scorer
        self._rate_limiter = rate_limiter
        self._pipeline_logger = pipeline_logger or PipelineRunLogger(self._db)
        self._error_logger = error_logger or ErrorLogger(self._db)
        self._alerter = alerter
        self._spend_tracker = spend_tracker
        self._max_per_cycle = max_trends_per_cycle
        self._max_daily_scored = max_daily_scored

    def run(self) -> bool:
        """
        Execute one trend monitoring cycle.

        Returns:
            True if new qualifying trends were found and stored.
        """
        run_id = self._pipeline_logger.start_run(WORKFLOW_NAME)
        new_trends_found = False
        trends_found = 0
        trends_scored = 0
        errors_count = 0

        try:
            # Acquire concurrency lock
            if self._rate_limiter:
                if not self._rate_limiter.acquire_lock(WORKFLOW_NAME):
                    logger.info("Another trend monitor is running, exiting")
                    self._pipeline_logger.complete_run(
                        run_id, counts={"trends_found": 0},
                        metadata={"skipped": "lock_held"},
                    )
                    return False

            # Check AI budget
            if self._spend_tracker:
                budget = self._spend_tracker.check_budget()
                if not budget["can_proceed"]:
                    logger.warning("AI budget exceeded, skipping scoring")
                    self._pipeline_logger.complete_run(
                        run_id, counts={"trends_found": 0},
                        metadata={"skipped": "budget_exceeded"},
                    )
                    return False

            # Step 1: Fetch from all sources
            all_candidates: List[Dict[str, Any]] = []
            source_failures = 0

            # Reddit
            if self._reddit:
                try:
                    reddit_trends = self._reddit.fetch_trends()
                    all_candidates.extend(reddit_trends)
                    logger.info("Reddit returned %d candidates", len(reddit_trends))
                except Exception as exc:
                    source_failures += 1
                    errors_count += 1
                    logger.error("Reddit source failed: %s", exc)
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME,
                        step="trend_fetch",
                        error_type="api_error",
                        error_message=str(exc),
                        service="reddit",
                        pipeline_run_id=run_id,
                    )

            # Google Trends
            if self._google:
                try:
                    google_trends = self._google.fetch_trends()
                    all_candidates.extend(google_trends)
                    logger.info("Google Trends returned %d candidates", len(google_trends))
                except Exception as exc:
                    source_failures += 1
                    errors_count += 1
                    logger.error("Google Trends source failed: %s", exc)
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME,
                        step="trend_fetch",
                        error_type="api_error",
                        error_message=str(exc),
                        service="google_trends",
                        pipeline_run_id=run_id,
                    )

            # If all sources failed, alert and exit
            active_sources = sum(1 for s in [self._reddit, self._google] if s is not None)
            if source_failures >= active_sources and active_sources > 0:
                logger.error("All trend sources failed")
                if self._alerter:
                    self._alerter.send_alert(
                        "All trend sources unreachable",
                        "Both Reddit and Google Trends APIs failed this cycle.",
                    )
                self._pipeline_logger.fail_run(
                    run_id,
                    error_message="All trend sources unreachable",
                    counts={"trends_found": 0, "errors_count": errors_count},
                )
                return False

            trends_found = len(all_candidates)
            if not all_candidates:
                logger.info("No trend candidates found from any source")
                self._pipeline_logger.complete_run(
                    run_id, counts={"trends_found": 0},
                )
                return False

            # Step 2: Deduplicate across sources
            canonical = deduplicate_trends(all_candidates)
            logger.info("Dedup: %d -> %d canonical trends", trends_found, len(canonical))

            # Step 3: Check against existing trends in DB
            new_candidates = check_existing_trends(canonical, self._db)
            logger.info("After DB check: %d new candidates", len(new_candidates))

            if not new_candidates:
                logger.info("No new trends after deduplication and DB check")
                self._pipeline_logger.complete_run(
                    run_id,
                    counts={"trends_found": trends_found, "trends_scored": 0},
                )
                return False

            # Step 4: Check blocklists (pre-filter before spending on scoring)
            clean_candidates: List[Dict[str, Any]] = []
            for trend in new_candidates:
                topic = trend.get("topic", "")
                is_blocked, match_term, blocklist_type = check_blocklists(topic)
                if is_blocked:
                    logger.info(
                        "Trend '%s' blocked by %s blocklist (matched '%s')",
                        topic[:50], blocklist_type, match_term,
                    )
                    continue
                clean_candidates.append(trend)

            if not clean_candidates:
                logger.info("All trends blocked by blocklists")
                self._pipeline_logger.complete_run(
                    run_id,
                    counts={"trends_found": trends_found, "trends_scored": 0},
                )
                return False

            # Step 5: Score with GPT-4o-mini
            if self._scorer:
                qualified = self._scorer.score_and_filter(
                    clean_candidates[:self._max_daily_scored],
                    threshold=OVERALL_THRESHOLD,
                )
                trends_scored = len(clean_candidates[:self._max_daily_scored])
            else:
                qualified = clean_candidates
                trends_scored = 0

            if not qualified:
                logger.info("No qualifying trends found (all below %.1f threshold)", OVERALL_THRESHOLD)
                self._pipeline_logger.complete_run(
                    run_id,
                    counts={"trends_found": trends_found, "trends_scored": trends_scored},
                )
                return False

            # Step 6: Store top N trends (per-cycle cap)
            top_trends = sorted(
                qualified,
                key=lambda t: t.get("score_overall", 0),
                reverse=True,
            )

            stored_count = 0
            queued_count = 0

            for i, trend in enumerate(top_trends):
                status = "discovered" if i < self._max_per_cycle else "queued"
                try:
                    self._db.insert_trend({
                        "topic": trend.get("topic", ""),
                        "topic_normalized": trend.get("topic_normalized", ""),
                        "keywords": trend.get("keywords", []),
                        "sources": trend.get("sources", [trend.get("source", "")]),
                        "score_velocity": trend.get("score_velocity"),
                        "score_commercial": trend.get("score_commercial"),
                        "score_safety": trend.get("score_safety"),
                        "score_uniqueness": trend.get("score_uniqueness"),
                        "score_overall": trend.get("score_overall"),
                        "reasoning": trend.get("reasoning", ""),
                        "status": status,
                        "source_data": trend.get("source_data", {}),
                    })
                    if status == "discovered":
                        stored_count += 1
                    else:
                        queued_count += 1
                except DatabaseError as exc:
                    errors_count += 1
                    logger.error("Failed to store trend '%s': %s", trend.get("topic", "")[:50], exc)
                    self._error_logger.log_error(
                        workflow=WORKFLOW_NAME,
                        step="trend_store",
                        error_type="api_error",
                        error_message=str(exc),
                        service="supabase",
                        pipeline_run_id=run_id,
                    )

            new_trends_found = stored_count > 0
            logger.info(
                "Stored %d discovered + %d queued trends",
                stored_count, queued_count,
            )

            # Complete the run
            status = "completed" if errors_count == 0 else "partial"
            if status == "partial":
                self._pipeline_logger.partial_run(
                    run_id,
                    counts={
                        "trends_found": trends_found,
                        "trends_scored": trends_scored,
                        "errors_count": errors_count,
                    },
                )
            else:
                self._pipeline_logger.complete_run(
                    run_id,
                    counts={
                        "trends_found": trends_found,
                        "trends_scored": trends_scored,
                    },
                )

        except Exception as exc:
            logger.error("Trend monitor failed: %s", exc)
            self._pipeline_logger.fail_run(
                run_id,
                error_message=str(exc),
                counts={"trends_found": trends_found, "errors_count": errors_count + 1},
            )
            if self._alerter:
                self._alerter.send_alert(
                    "Trend monitor failed",
                    f"Unhandled error: {str(exc)[:500]}",
                )
            raise
        finally:
            if self._rate_limiter:
                self._rate_limiter.release_lock(WORKFLOW_NAME)

        return new_trends_found


def _set_github_output(key: str, value: str) -> None:
    """Set a GitHub Actions output variable."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")
        logger.info("Set GitHub output: %s=%s", key, value)
    else:
        logger.debug("GITHUB_OUTPUT not set (not running in GitHub Actions)")


def main() -> None:
    """Entry point for `python -m src.trends.monitor`."""
    setup_logging()
    logger.info("Starting trend monitor")

    try:
        cfg = load_config()
    except Exception as exc:
        logger.critical("Failed to load config: %s", exc)
        sys.exit(1)

    db = SupabaseClient()

    monitor = TrendMonitor(
        db=db,
        reddit_source=RedditSource(),
        google_source=GoogleTrendsSource(),
        scorer=TrendScorer(),
        rate_limiter=EtsyRateLimiter(),
        alerter=EmailAlerter(),
        spend_tracker=SpendTracker(db=db),
        max_trends_per_cycle=cfg.caps.max_trends_per_cycle,
    )

    try:
        new_trends = monitor.run()
        _set_github_output("new_trends", str(new_trends).lower())

        if new_trends:
            logger.info("New trends discovered - generation job should run")
        else:
            logger.info("No qualifying trends this cycle")

        sys.exit(0)

    except Exception as exc:
        logger.critical("Trend monitor failed: %s", exc)
        _set_github_output("new_trends", "false")
        sys.exit(1)


if __name__ == "__main__":
    main()
