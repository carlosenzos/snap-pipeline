from celery import Celery
from config.settings import get_settings

settings = get_settings()

app = Celery(
    "snap_pipeline",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "workers.tasks.pipeline",
        "workers.tasks.script",
        "workers.tasks.deliver",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
