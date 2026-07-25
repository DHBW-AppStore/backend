from celery import Celery

from .config import settings

celery_app = Celery("worker", broker=settings.CELERY_BROKER_URL)  # no include here

celery_app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    result_backend=settings.CELERY_RESULT_BACKEND,

    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    task_track_started=True,
    task_send_sent_event=True,
    result_extended=True,
)

if __name__ == "__main__":
    celery_app.start()
