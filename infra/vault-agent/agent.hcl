# Vault Agent — the sidecar that removes ./secrets.
#
# Six credentials in this deployment are read by images we did not write: Postgres, MinIO
# and n8n take a password from a file and cannot be taught to call a secrets manager.
# Until week 15 that meant six plaintext files on the host, checked out next to the compose
# file, backed up by whatever backs up the host, and living exactly as long as the disk
# does. That is a second copy of every credential, and the copy nobody rotates.
#
# Vault Agent logs in with its own AppRole, renders the six values into a tmpfs, and keeps
# them current. The host filesystem never holds them.
#
# ---------------------------------------------------------------------------------------
# Its own AppRole, and a different path from the API's.
#
# The API's role reads `secret/drawbridge`. This role reads `secret/drawbridge-infra` and
# nothing else. They are disjoint on purpose: the API process has no business being able
# to read the Postgres *owner* password or the n8n encryption key, and before this file
# existed it could not — the split has to survive the move into Vault or the move is a
# downgrade wearing a manager's name.
#
# ---------------------------------------------------------------------------------------
# What this does not do: restart anything.
#
# Postgres reads its password once at initdb and never again; MinIO reads its root
# credentials at start. A `command` on the template blocks below would rewrite the file
# and change nothing in the running process, which is worse than not trying, because the
# rendered file would then disagree with the credential actually in force. Rotation here
# means: write the new value to Vault, then restart the consumer. `secrets/README.md`
# says so, and there is no automation pretending otherwise.

pid_file = "/run/vault-agent/pid"

vault {
  # address comes from VAULT_ADDR in the environment. Not written here, because the same
  # config file has to work against the bootstrap Vault and against the broker's own.
  retry {
    # An unreachable Vault must stop the deployment rather than let it come up on
    # whatever a previous run left in the tmpfs — the tmpfs is empty at boot, so the
    # consumers' healthchecks fail and the stack does not come up half-credentialled.
    num_retries = 5
  }
}

auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path   = "/vault/approle/role_id"
      secret_id_file_path = "/vault/approle/secret_id"
      # The file is a read-only bind mount from a docker secret, so the agent cannot
      # delete it after reading and must not try. The bound that matters is the token TTL
      # the role issues, not whether this one file survives.
      remove_secret_id_file_after_reading = false
    }
  }

  # No sink. A sink writes the Vault token itself to a file, and nothing in this
  # deployment consumes it — the agent is the only thing that speaks to Vault. Writing it
  # anyway would put a live token on a volume three other containers can read.
}

# Templates are rendered even before a consumer asks, so the file is present when the
# container starts rather than seconds into its first query.
template_config {
  exit_on_retry_failure = true
  static_secret_render_interval = "5m"
}

# ---------------------------------------------------------------------------- postgres
template {
  source      = "/vault/templates/postgres_password.ctmpl"
  destination = "/vault/render/postgres/postgres_password"
  perms       = "0644"
}

# --------------------------------------------------------------------------- authentik
# Authentik needs the database password too, and gets its own copy rather than sharing
# Postgres's volume: a rendered credential should be reachable by the containers that
# need it and by no others, and one volume per consumer group is how that is stated.
template {
  source      = "/vault/templates/postgres_password.ctmpl"
  destination = "/vault/render/authentik/postgres_password"
  perms       = "0644"
}

template {
  source      = "/vault/templates/authentik_secret_key.ctmpl"
  destination = "/vault/render/authentik/authentik_secret_key"
  perms       = "0644"
}

# ------------------------------------------------------------------------------- minio
template {
  source      = "/vault/templates/minio_root_user.ctmpl"
  destination = "/vault/render/minio/minio_root_user"
  perms       = "0644"
}

template {
  source      = "/vault/templates/minio_root_password.ctmpl"
  destination = "/vault/render/minio/minio_root_password"
  perms       = "0644"
}

# --------------------------------------------------------------------------------- n8n
template {
  source      = "/vault/templates/postgres_password.ctmpl"
  destination = "/vault/render/n8n/postgres_password"
  perms       = "0644"
}

template {
  source      = "/vault/templates/n8n_encryption_key.ctmpl"
  destination = "/vault/render/n8n/n8n_encryption_key"
  perms       = "0644"
}

template {
  source      = "/vault/templates/service_token.ctmpl"
  destination = "/vault/render/n8n/service_token"
  perms       = "0644"
}
