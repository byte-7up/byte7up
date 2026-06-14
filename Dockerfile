FROM python:3.12-alpine

WORKDIR /app
COPY webhook.py /app/
RUN mkdir -p /data

ENV PORT=3040
ENV PYTHONUNBUFFERED=1

EXPOSE 3040

CMD ["python", "webhook.py"]
