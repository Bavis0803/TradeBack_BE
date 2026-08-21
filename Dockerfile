FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tradeback ./

RUN useradd --create-home --uid 10001 tradeback \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R tradeback:tradeback /app

USER tradeback

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "tradeback.asgi:application"]
