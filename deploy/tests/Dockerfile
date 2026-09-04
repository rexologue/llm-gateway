FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY deploy/tests/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY deploy/tests/smoke /app/tests/smoke

# Default to the endpoint contract suite, which is valid against any
# OpenAI-compatible endpoint. The gateway stack overrides the command to also
# run the gateway-only session suite.
CMD ["pytest", "-q", "tests/smoke/test_backend_contract.py"]
