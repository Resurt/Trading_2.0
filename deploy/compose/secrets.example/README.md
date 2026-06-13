# Local Docker Compose Secrets

Do not commit real secrets.

Create local files under repository-root `secrets/`:

- `secrets/tbank_full_access_token`
- `secrets/tbank_readonly_token`
- `secrets/postgres_password`
- `secrets/grafana_admin_password`

The repository `.gitignore` excludes `secrets/`.
