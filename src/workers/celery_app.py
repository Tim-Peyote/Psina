from celery import Celery

from src.config import settings

celery_app = Celery(
    "zalutka",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
)

# Schedule periodic tasks
celery_app.conf.beat_schedule = {
    "daily-summary-every-chat": {
        "task": "src.workers.tasks.generate_daily_summaries",
        "schedule": 86400.0,  # every 24 hours
        "options": {"expires": 3600},
    },
    "check-reminders": {
        "task": "src.workers.tasks.check_reminders",
        "schedule": 60.0,  # every 60 seconds
        "options": {"expires": 55},
    },
    # Memory system tasks
    "extract-memory-batch": {
        "task": "src.workers.memory_tasks.extract_memory_batch",
        "schedule": 300.0,  # every 5 minutes
        "options": {"expires": 240},
    },
    "compact-old-sessions": {
        "task": "src.workers.memory_tasks.compact_old_sessions",
        "schedule": 3600.0,  # every hour
        "options": {"expires": 1800},
    },
    "cleanup-expired-memory": {
        "task": "src.workers.memory_tasks.cleanup_expired_memory",
        "schedule": 21600.0,  # every 6 hours
        "options": {"expires": 3600},
    },
    "rebuild-embeddings": {
        "task": "src.workers.memory_tasks.rebuild_embeddings",
        "schedule": 86400.0,  # every 24 hours
        "options": {"expires": 3600},
    },
}
