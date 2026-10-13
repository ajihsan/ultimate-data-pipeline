# Mock Jev /v1/systemone server (S4 verification stand-in for the S5 Laya
# server). Dependencies are baked at build time — no network access at runtime.
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.143.0 uvicorn==0.54.0

COPY tests/mocks/mock_jev.py /app/mock_jev.py

WORKDIR /app
EXPOSE 8081
CMD ["uvicorn", "mock_jev:app", "--host", "0.0.0.0", "--port", "8081"]
