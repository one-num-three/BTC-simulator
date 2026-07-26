# A single classroom node.
#
#   docker build -t btc-simulator .
#   docker run --rm -p 8000:8000 -p 7464:7464 -v "$PWD/data:/app/data" btc-simulator
#
# The web console binds 0.0.0.0 inside the container because the container's
# own loopback is not reachable from the host. That means anyone who can reach
# the published port can read this node. Writes still require the admin token,
# printed on startup and stored in data/admin_token.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY main.py ./
COPY docker/config.docker.json ./config.json

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000 7464

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4).status==200 else 1)"

CMD ["python", "main.py", "--config", "config.json"]
