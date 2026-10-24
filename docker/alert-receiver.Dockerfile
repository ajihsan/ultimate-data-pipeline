# Mock alert receiver: a stand-in for Telegram and Discord.
# Dependencies are baked at build time; the container needs no network.
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.143.0 uvicorn==0.54.0

COPY tests/mocks/mock_alert_receiver.py /app/mock_alert_receiver.py

WORKDIR /app
EXPOSE 8082
CMD ["uvicorn", "mock_alert_receiver:app", "--host", "0.0.0.0", "--port", "8082"]
