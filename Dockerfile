# LameStreet in a container.
#
# The image holds only code. Everything personal — config.json, .env, the
# data/ archive — lives in a directory you mount at /data (PM_ROOT), so the
# container is disposable and upgrades are a rebuild.
#
#   docker compose up -d          # uses the compose file next to this
#
# or by hand:
#
#   docker build -t lamestreet .
#   docker run -d -p 127.0.0.1:3002:3002 -v "$PWD":/data lamestreet

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pm/ pm/
COPY viewer/ viewer/

# Inside the container the server must bind 0.0.0.0 to be reachable through
# the published port — which the app only allows with PM_AUTH_USER and
# PM_AUTH_PASSWORD set in /data/.env. Real environment beats .env, so a
# PM_HOST=127.0.0.1 left in the mounted file cannot break the container.
#
# PORT rather than PM_PORT: hosting platforms inject PORT and expect the app
# to bind exactly that. Hardcoding PM_PORT here would silently outrank it.
ENV PM_ROOT=/data PM_HOST=0.0.0.0 PORT=3002

EXPOSE 3002
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '3002') + '/healthz')"]

CMD ["python", "-m", "pm", "serve"]
