FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8000

COPY src ./src

EXPOSE 8000

CMD ["python", "-m", "unit_expert_mcp.server"]
