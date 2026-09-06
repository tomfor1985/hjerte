#!/bin/sh
set -eu
umask 077
if [ "${1:-web}" = worker ]; then
    exec python -u manage.py generation_worker
fi
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn hjerte.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile -
