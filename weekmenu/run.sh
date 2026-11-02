#!/usr/bin/with-contenv bashio
# s6-overlay (the base image's /init) starts this; with-contenv passes the
# container environment (SUPERVISOR_TOKEN, TZ) on to Python.
cd / || exit 1
bashio::log.info "Weekmenu AH start"
exec python3 -m app.main
