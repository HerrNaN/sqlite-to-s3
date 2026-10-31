# sqlite-to-s3

One Python script and a Kubernetes CronJob chart:

**SQLite online backup → gzip → age public-key encryption → S3-compatible upload → count retention.**

Works with S3-compatible endpoints, including Scaleway. Only the public age key
is needed in the job. Keep the private identity outside the cluster and Git.

## Local build and checks

Requires Podman and [mise](https://mise.jdx.dev/) (for Helm and yq).

```sh
mise trust
mise install
mise build-image
mise test
mise check-chart
```

Tests run with networking disabled and mocked S3 calls. They exercise live WAL
consistency, real age encryption/decryption, pagination, deletion batches, and
failure paths. No cloud credentials or cluster access are needed.

The Dockerfile installs age and a pinned boto3 version. You can also run
`python -m unittest discover -s tests -v` locally with Python 3.13+, boto3, and age
installed. Run `python backup.py` with the environment variables from the chart
to use the tool without Kubernetes.

## Configure the chart

Generate an age identity **outside this repository**, for example:

```sh
age-keygen -o /secure/location/backup-identity.txt
age-keygen -y /secure/location/backup-identity.txt
```

Save the identity securely. The second command prints the public recipient to
put in your values. Preserve old identities when rotating keys.

Example `values.local.yaml` (PVC name, database filename and bucket are examples):

```yaml
schedule: "0 3 * * *"
timeZone: Etc/UTC
ageRecipient: age1... # Your actual public key
sqlite:
  pvcName: database-data
  path: /data/database.sqlite3
s3:
  endpoint: https://s3.fr-par.scw.cloud
  region: fr-par
  bucket: your-backup-bucket
  prefix: sqlite-backups
  existingSecret: sqlite-to-s3-credentials
  keepLast: 7
```

- The packaged chart already contains a digest-pinned `image`. For a local
  source chart, also set `image` to your built/published image reference.
- The PVC root is mounted at `/data`, regardless of the application's mount
  path. Database permissions must allow access; the mount is writable because
  SQLite WAL readers may need the shared-memory file. `ReadWriteOnce` permits
  another pod on the same node, but `ReadWriteOncePod` does not.
- Endpoint, region, and retention count are required. Use the bucket's region
  and full endpoint URL (including `https://`), even when using AWS S3.
- The referenced Secret must exist in the release namespace and contain
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (optionally
  `AWS_SESSION_TOKEN`). These names also apply to Scaleway credentials.
  Create/manage it manually outside Git, e.g. from a securely stored env file:

  ```sh
  kubectl -n YOUR_NAMESPACE create secret generic sqlite-to-s3-credentials \
    --from-env-file=/secure/location/s3.env
  ```

- Credentials need upload, bucket-list, and object-delete permissions scoped
  to the backup prefix, plus multipart-upload permissions if needed.
- **Use a dedicated prefix per backup job.** After a successful upload, all
  current objects beneath `<prefix>/` count toward `keepLast` (minimum 1), sorted
  newest first by modification time. Avoid other writers to that prefix.
  Versioned history is not purged; use a provider lifecycle rule for old versions.
- Snapshots and compressed plaintext exist only in temporary pod storage.
  Allow ephemeral space for roughly two database copies. Only `.sqlite3.gz.age`
  files are uploaded. Jobs time out after 30 minutes, retry twice, and do not
  overlap within the same CronJob.

Render locally before deployment:

```sh
helm template sqlite-backup deploy/charts/sqlite-to-s3 \
  --namespace YOUR_NAMESPACE -f values.local.yaml \
  --set-string image=localhost/sqlite-to-s3:test
```

## GitHub publishing

On a push to `main`, the workflow:

1. Lints/renders the chart, builds the image using Buildah, and tests it offline.
2. Publishes `ghcr.io/<owner>/sqlite-to-s3:devel` and `:<commit-sha>`.
3. Packages chart version `0.1.0-<UTC timestamp>`, sets `appVersion` to the commit,
   and pins `image` to the published `repository@sha256:...` digest.
4. Publishes to `oci://ghcr.io/<owner>/charts/devel/sqlite-to-s3`.

Repository names/owners are derived from GitHub context, not hardcoded. The
workflow builds the runner's native architecture (currently Linux amd64).
It publishes artifacts only; it never deploys to a cluster.

Manual GitHub setup:

- Create the `sqlite-to-s3` GitHub repository, add its remote, and commit/push
  when ready. No remote or commits are created by this scaffold.
- Enable Actions and allow `GITHUB_TOKEN` to write packages (subject to any
  organization restrictions). No PAT or S3 credentials are needed in Actions.
- After the first run, make both GHCR packages public if you want anonymous
  pulls. For existing packages, grant this repository Actions write access.
  Private artifacts require registry credentials for Helm/image consumers.
- Read the workflow summary for the exact chart version and image digest.

Local packaging without publishing:

```sh
mise package-chart --version 0.1.0-20261031030000 --app-version YOUR_COMMIT \
  --image-repository ghcr.io/YOUR_OWNER/sqlite-to-s3 \
  --image-digest sha256:YOUR_64_HEX_DIGEST
```

To consume a published chart later:

```sh
helm upgrade --install sqlite-backup \
  oci://ghcr.io/YOUR_OWNER/charts/devel/sqlite-to-s3 \
  --version YOUR_CHART_VERSION --namespace YOUR_NAMESPACE -f values.local.yaml
```

Argo CD can use that OCI chart as well; select `*-0` to include timestamped
prereleases. Namespace, PVC, credentials, and the private age identity are not
created or managed by this chart.

## Restore

Download a selected `.sqlite3.gz.age` object, then decrypt and decompress locally:

```sh
age --decrypt -i /secure/location/backup-identity.txt \
  -o restored.sqlite3.gz backup.sqlite3.gz.age
gzip -d restored.sqlite3.gz
sqlite3 restored.sqlite3 'PRAGMA integrity_check;'
```

Before replacing the live database, stop all application writers and preserve
the original database. Restore correct ownership and do not leave stale `-wal`
or `-shm` files alongside the restored database. Restart and verify application
data. Regularly test restores; losing the private identity makes the backups
unrecoverable.
