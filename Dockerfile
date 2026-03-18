FROM python:3.12-alpine

WORKDIR /app
COPY webhook.py /app/
RUN mkdir -p /data

ENV PORT=3000
ENV PYTHONUNBUFFERED=1

CMD ["python", "webhook.py"]
