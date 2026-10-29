FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY strategy_engine.py cloud_main.py ./
CMD ["python", "cloud_main.py"]
