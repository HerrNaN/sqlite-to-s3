import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "live #1?.sqlite3"
        self.connection = sqlite3.connect(self.source)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE items (name TEXT)")
        self.connection.execute("INSERT INTO items VALUES ('committed')")
        self.connection.commit()
        self.env = {
            "SQLITE_PATH": str(self.source),
            "S3_PREFIX": "backups/",
            "S3_KEEP_LAST": "7",
            "S3_BUCKET": "test-bucket",
            "S3_ENDPOINT": "https://s3.fr-par.scw.cloud",
            "AWS_DEFAULT_REGION": "fr-par",
            "AGE_RECIPIENT": "test-recipient",
        }
        self.s3 = Mock()
        self.s3.get_paginator.return_value.paginate.return_value = [{}]
        self.s3.delete_objects.return_value = {}

    def test_live_wal_age_roundtrip_and_paginated_retention(self):
        identity = self.root / "identity.txt"
        subprocess.run(
            ["age-keygen", "-o", str(identity)], check=True, capture_output=True
        )
        self.env["AGE_RECIPIENT"] = subprocess.check_output(
            ["age-keygen", "-y", str(identity)], text=True
        ).strip()
        # The snapshot must include committed WAL data, but not this pending write.
        self.connection.execute("INSERT INTO items VALUES ('uncommitted')")
        start = datetime(2020, 1, 1, tzinfo=UTC)
        objects = [
            {
                "Key": f"backups/{i:04d}.sqlite3.gz.age",
                "LastModified": start + timedelta(seconds=i),
            }
            for i in range(2050)
        ]
        uploaded = {}

        def upload(filename, bucket, key):
            self.assertEqual(bucket, "test-bucket")
            self.assertTrue(key.endswith(".sqlite3.gz.age"))
            uploaded["body"] = Path(filename).read_bytes()
            uploaded["key"] = key
            objects.append({"Key": key, "LastModified": datetime.now(UTC)})

        def pages(**kwargs):
            self.assertIn("body", uploaded, "listing must follow successful upload")
            self.assertEqual(
                kwargs, {"Bucket": "test-bucket", "Prefix": "backups/"}
            )
            # Deliberately out of order, with an empty page.
            return [{"Contents": objects[1000:]}, {}, {"Contents": objects[:1000]}]

        self.s3.upload_file.side_effect = upload
        self.s3.get_paginator.return_value.paginate.side_effect = pages
        with patch.dict(os.environ, self.env), patch.object(
            backup.boto3, "client", return_value=self.s3
        ) as client:
            backup.main()
        client.assert_called_once_with(
            "s3", endpoint_url=self.env["S3_ENDPOINT"], region_name="fr-par"
        )
        self.s3.get_paginator.assert_called_once_with("list_objects_v2")
        batches = [
            call.kwargs["Delete"]["Objects"]
            for call in self.s3.delete_objects.call_args_list
        ]
        self.assertEqual([len(batch) for batch in batches], [1000, 1000, 44])
        deleted = {item["Key"] for batch in batches for item in batch}
        self.assertEqual(deleted, {obj["Key"] for obj in objects[:2044]})
        self.assertNotIn(uploaded["key"], deleted)

        encrypted = self.root / "backup.age"
        encrypted.write_bytes(uploaded["body"])
        compressed = self.root / "restored.gz"
        subprocess.run(
            ["age", "-d", "-i", str(identity), "-o", str(compressed), str(encrypted)],
            check=True,
        )
        restored = self.root / "restored.sqlite3"
        with gzip.open(compressed, "rb") as src, restored.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        with closing(sqlite3.connect(restored)) as snapshot:
            self.assertEqual(
                snapshot.execute("SELECT * FROM items").fetchall(), [("committed",)]
            )
            self.assertEqual(
                snapshot.execute("PRAGMA integrity_check").fetchone(), ("ok",)
            )

    def test_encryption_failure_never_uploads_or_prunes(self):
        with patch.dict(os.environ, self.env), patch.object(
            backup.boto3, "client", return_value=self.s3
        ), patch.object(
            backup.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "age")
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                backup.main()
        self.s3.upload_file.assert_not_called()
        self.s3.get_paginator.assert_not_called()
        self.s3.delete_objects.assert_not_called()

    def test_upload_failure_never_prunes(self):
        self.s3.upload_file.side_effect = RuntimeError("upload failed")
        with patch.dict(os.environ, self.env), patch.object(
            backup.boto3, "client", return_value=self.s3
        ), patch.object(backup.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                backup.main()
        self.s3.upload_file.assert_called_once()
        self.s3.get_paginator.assert_not_called()
        self.s3.delete_objects.assert_not_called()

    def test_missing_database_is_not_created_or_pruned(self):
        missing = self.root / "missing.sqlite3"
        self.env["SQLITE_PATH"] = str(missing)
        with patch.dict(os.environ, self.env), patch.object(
            backup.boto3, "client", return_value=self.s3
        ):
            with self.assertRaises(sqlite3.OperationalError):
                backup.main()
        self.assertFalse(missing.exists())
        self.s3.upload_file.assert_not_called()
        self.s3.get_paginator.assert_not_called()

    def test_unsafe_settings_fail_before_s3_access(self):
        for key, value in [
            ("S3_PREFIX", ""), ("S3_PREFIX", "/"),
            ("S3_KEEP_LAST", "0"), ("S3_KEEP_LAST", "-1"),
            ("S3_KEEP_LAST", "1.5"), ("AGE_RECIPIENT", ""),
            ("S3_ENDPOINT", ""), ("AWS_DEFAULT_REGION", ""),
        ]:
            with self.subTest(key=key, value=value), patch.dict(
                os.environ, self.env | {key: value}
            ), patch.object(backup.boto3, "client") as client:
                with self.assertRaises(ValueError):
                    backup.main()
                client.assert_not_called()

    def test_empty_listing(self):
        backup.prune(self.s3, "bucket", "backups/", 7)
        self.s3.delete_objects.assert_not_called()

    def test_exact_retention_count_does_not_delete(self):
        self.s3.get_paginator.return_value.paginate.return_value = [{
            "Contents": [{"Key": str(i), "LastModified": i} for i in range(7)]
        }]
        backup.prune(self.s3, "bucket", "backups/", 7)
        self.s3.delete_objects.assert_not_called()

    def test_partial_delete_error_fails_job(self):
        self.s3.get_paginator.return_value.paginate.return_value = [{"Contents": [
            {"Key": "old", "LastModified": 1},
            {"Key": "new", "LastModified": 2},
        ]}]
        self.s3.delete_objects.return_value = {
            "Errors": [{"Key": "old", "Code": "AccessDenied"}]
        }
        with self.assertRaisesRegex(RuntimeError, "AccessDenied"):
            backup.prune(self.s3, "bucket", "backups/", 1)

    def test_listing_failure_does_not_delete(self):
        self.s3.get_paginator.return_value.paginate.side_effect = RuntimeError("listing failed")
        with self.assertRaisesRegex(RuntimeError, "listing failed"):
            backup.prune(self.s3, "bucket", "backups/", 7)
        self.s3.delete_objects.assert_not_called()


if __name__ == "__main__":
    unittest.main()
