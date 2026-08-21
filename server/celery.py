"""Celery application for the project.

Imported by ``server/__init__.py`` so ``@shared_task`` in any installed app
binds to this app, and so ``celery -A server worker`` finds it.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")

app = Celery("server")

# Every CELERY_* setting in settings.py becomes a Celery config key.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
