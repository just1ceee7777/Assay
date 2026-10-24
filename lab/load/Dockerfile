FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /load
RUN pip install "httpx>=0.27,<1"
COPY load.py .

RUN useradd --uid 10002 --no-create-home load
USER load

CMD ["python", "load.py"]
