#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py seed_offset_desk
exec uvicorn config.asgi:application --host 0.0.0.0 --port 8000
