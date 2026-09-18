#!/bin/sh
set -e
export COPAW_PORT="${COPAW_PORT:-8088}"
envsubst '${COPAW_PORT}' < /etc/supervisor/conf.d/supervisord.conf > /etc/supervisor/conf.d/supervisord.conf.tmp
mv /etc/supervisor/conf.d/supervisord.conf.tmp /etc/supervisor/conf.d/supervisord.conf
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
