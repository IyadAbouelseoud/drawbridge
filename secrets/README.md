# `./secrets` — gone as of week 15

This directory used to hold six plaintext credentials, because three images we did not
write — Postgres, MinIO and n8n — read their passwords from a file and cannot be taught to
call a secrets manager. It was the honest arrangement available at the time and it was
still two copies of every credential: one in Vault, one on the host's disk, living as long
as the disk, rotated by nobody, and swept up by whatever backs the host up.

**Nothing goes here now. If these files exist on a deployment, delete them.**

```sh
shred -u secrets/postgres_password secrets/minio_root_user secrets/minio_root_password \
        secrets/authentik_secret_key secrets/n8n_encryption_key secrets/service_token
```

## What replaced them

`vault-agent`, a sidecar in `docker-compose.onprem.yml`. It logs in with its own AppRole,
renders the six values into four tmpfs volumes, and keeps them current. The host filesystem
never holds them; a host restart leaves nothing behind.

| What | Where |
|---|---|
| The sidecar's config and templates | `infra/vault-agent/` |
| The Vault path, policy and role | `python infra/vault_bootstrap.py --infra` |
| Which consumer sees which file | one tmpfs volume per consumer group, `docker-compose.onprem.yml` |

**A different path and a different role from the API's.** The application reads
`secret/drawbridge`; vault-agent reads `secret/drawbridge-infra`, and the two policies are
disjoint. That separation existed by accident before — the API could not read the Postgres
owner password because it was not in `.secrets.json` — and moving the credentials into a
manager had to preserve it or the move would have been a downgrade with a better name.
Verified against a live Vault: the infra role is denied both `secret/drawbridge` and `list`
on the mount.

## The one credential that cannot come from the manager

`DRAWBRIDGE_VAULT_AGENT_SECRET_ID`, because it is what you use to ask the manager for
things. It arrives through the environment as a Docker secret sourced with
`environment:` rather than `file:`, so it is delivered to the container on a tmpfs and this
directory does not need to exist. `role_id` is configuration and may be committed;
`secret_id` is the credential, is revocable, and is reissued by
`python infra/vault_bootstrap.py --infra --rotate`.

## Rotation, and what it costs

Rotation is deliberate: write the new value to Vault, then restart the consumer. Nothing
automates it, and `infra/vault-agent/agent.hcl` deliberately declares no `command` on its
templates — Postgres reads its password once at initdb and MinIO reads its root credentials
at start, so a template that rewrote the file and signalled the process would change the
file and not the credential in force. A rendered file that disagrees with what the service
is actually using is worse than a stale one you know is stale.

Three of the six do not survive rotation quietly:

| Secret | What rotating it breaks |
|---|---|
| `postgres_password` | the owner role's password; `ALTER ROLE` it in the same maintenance window |
| `authentik_secret_key` | every stored session, and every user is logged out |
| `n8n_encryption_key` | every credential stored inside n8n becomes unreadable |

`python infra/vault_bootstrap.py --infra` is safe to re-run for exactly this reason: it
generates only what is missing and never replaces a value already in force.

`service_token` is not generated at all — it is a JWT the identity provider signs. Mint it
with `scripts/mint_token.py --service` and pass it as `--service-token`, or leave it and
Vault keeps the one it has.
