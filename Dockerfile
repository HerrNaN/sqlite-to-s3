# Build from this directory, then push to your container registry.
FROM python:3.13-alpine
RUN apk add --no-cache age \
    && pip install --no-cache-dir boto3==1.43.123
WORKDIR /app
COPY backup.py /app/backup.py
ENTRYPOINT ["python", "/app/backup.py"]
