FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8089

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY demo ./demo

RUN pip install --no-cache-dir -e .

EXPOSE 8089

CMD ["python", "-m", "transition_pilot.server"]
