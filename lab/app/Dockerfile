FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY shop ./shop

RUN useradd --uid 10001 --no-create-home shop
USER shop

EXPOSE 8000
CMD ["uvicorn", "shop.main:build", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
