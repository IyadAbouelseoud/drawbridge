# `./secrets` — the six files the on-prem deployment reads directly

Everything Drawbridge's own services need comes from Vault. These six exist because three
images we did not write — Postgres, MinIO and n8n — read credentials from a file or from
the environment and cannot be taught to call a secrets manager. A file is the better of
the two: it is not in `docker inspect`, not inherited by child processes, and not in
`/proc/<pid>/environ`.

The directory is gitignored except for this file. Nothing here is committed, ever.

| File | Read by | Notes |
|---|---|---|
| `postgres_password` | postgres, authentik, n8n | the **owner** role's password |
| `minio_root_user` | minio, minio-init | |
| `minio_root_password` | minio, minio-init | |
| `authentik_secret_key` | authentik server and worker | rotating it invalidates every session |
| `n8n_encryption_key` | n8n | rotating it makes stored workflow credentials unreadable |
| `service_token` | n8n | cross-tenant; the most valuable credential in the deployment |

## Creating them

```sh
mkdir -p secrets && cd secrets
for f in postgres_password minio_root_user minio_root_password \
         authentik_secret_key n8n_encryption_key; do
  head -c 32 /dev/urandom | base64 | tr -d '\n=+/' > "$f"
done
chmod 400 *
```

`service_token` is not random — it is a JWT the API's own issuer signs. Mint it with
`scripts/mint_token.py --service` against the deployment's identity provider and write the
output here.

**No trailing newline.** Postgres and MinIO read the file verbatim, so a newline becomes
part of the password and the failure looks like a wrong password rather than a wrong file.
`printf '%s' "$value" > secrets/postgres_password` if you are pasting one in.

Mode `0400`, owned by the user the Docker daemon runs as. Docker copies the contents into
a tmpfs inside the container at `/run/secrets/<name>`; the host file is the durable copy
and is the one that has to be protected.

## These are duplicates, and that is the trade

The same values are in Vault, where the application reads them from. Two copies of a
secret is worse than one, and it is the price of running three images that cannot read a
manager. The alternative is a sidecar that templates them out of Vault at start
(`vault agent`), which removes the durable copy and adds a process to every one of those
three containers. That is the right end state and it is not built.
