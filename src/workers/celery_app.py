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

# Schedule daily summaries
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
}
