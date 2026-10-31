"""Snapshot SQLite, gzip, encrypt with age, upload, then prune old backups."""

import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import boto3


def prune(s3, bucket, prefix, keep_last):
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    objects.sort(key=lambda obj: (obj["LastModified"], obj["Key"]), reverse=True)
    expired = objects[keep_last:]
    for start in range(0, len(expired), 1000):
        response = s3.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": obj["Key"]} for obj in expired[start : start + 1000]],
                "Quiet": True,
            },
        )
        if response.get("Errors"):
            raise RuntimeError(f"Retention deletion failed: {response['Errors']}")
    print(f"Found {len(objects)} objects; deleted {len(expired)} old backups", flush=True)


def main():
    prefix = os.environ["S3_PREFIX"].rstrip("/")
    keep_last = int(os.environ["S3_KEEP_LAST"])
    recipient = os.environ["AGE_RECIPIENT"].strip()
    endpoint = os.environ["S3_ENDPOINT"].strip()
    region = os.environ["AWS_DEFAULT_REGION"].strip()
    if not prefix or keep_last < 1 or not recipient:
        raise ValueError("A dedicated prefix, positive keepLast, and age recipient are required")
    if not endpoint or not region:
        raise ValueError("S3 endpoint and region are required")
    prefix += "/"
    bucket = os.environ["S3_BUCKET"]
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
    )

    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "database.sqlite3"
        compressed = snapshot.with_suffix(".sqlite3.gz")
        encrypted = compressed.with_suffix(".gz.age")
        uri = Path(os.environ["SQLITE_PATH"]).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("SQLite snapshot integrity check failed")

        with snapshot.open("rb") as source, gzip.open(compressed, "wb") as target:
            shutil.copyfileobj(source, target)
        snapshot.unlink()
        subprocess.run(
            ["age", "--recipient", recipient, "--output", str(encrypted), str(compressed)],
            check=True,
        )
        compressed.unlink()

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        key = f"{prefix}{timestamp}-{uuid4().hex}.sqlite3.gz.age"
        s3.upload_file(str(encrypted), bucket, key)
        print(f"Uploaded s3://{bucket}/{key}", flush=True)

    # Never remove old backups unless snapshot, encryption, and upload succeeded.
    prune(s3, bucket, prefix, keep_last)


if __name__ == "__main__":
    main()
