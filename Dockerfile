FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 单 worker:核对/发布的串行化依赖进程内锁(见 README「余项」)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
