# Process types for Heroku-style platforms (Heroku, Railway, Dokku, and Render when it
# is pointed at a Procfile instead of a start command).
#
# `web` is the process the platform routes HTTP to and the one it gives $PORT; both
# scripts read $PORT and bind 0.0.0.0, so neither needs a platform-specific flag.
#
# The API is `web` and the console is a second process type because they are two servers
# and a platform routes traffic to exactly one of them per service. On Heroku that means
# deploying this repository twice - once running `web`, once running `ui` - or scaling
# `web=0 ui=1` on the second app; on Render, render.yaml declares both properly.
#
# Invoked as `sh scripts/...` rather than by path, so a platform that does not preserve
# the executable bit through its build still starts.
web: sh scripts/start_api.sh
ui: sh scripts/start_ui.sh
