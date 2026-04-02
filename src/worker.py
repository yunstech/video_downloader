"""
RQ worker entry point with custom setup.

Can be used instead of the default `rq worker` command
for custom initialization, logging, and error handling.

Usage:
    python -m src.worker
"""
import logging
import sys

from redis import Redis
from rq import Worker, Queue

from src import config

logger = logging.getLogger(__name__)


def run_worker():
    """Initialize and run the RQ worker."""
    logger.info("Starting RQ worker...")
    logger.info(f"Redis URL: {config.REDIS_URL}")
    logger.info(f"Queue: video-downloads")
    logger.info(f"Download dir: {config.DOWNLOAD_DIR}")

    # Connect to Redis with retry
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            redis_conn = Redis.from_url(config.REDIS_URL)
            redis_conn.ping()
            logger.info("Connected to Redis")
            break
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Could not connect to Redis after {max_retries} attempts: {e}")
                sys.exit(1)
            logger.warning(f"Redis connection attempt {attempt}/{max_retries} failed: {e}")
            import time
            time.sleep(2 * attempt)  # Exponential backoff

    queues = [Queue("video-downloads", connection=redis_conn)]

    worker = Worker(
        queues,
        connection=redis_conn,
        name=None,  # Auto-generated name
    )

    logger.info("Worker is ready. Waiting for jobs...")

    worker.work(
        max_jobs=0,            # Unlimited
        with_scheduler=False,  # No scheduled jobs
        logging_level=config.LOG_LEVEL,
    )


if __name__ == "__main__":
    run_worker()
