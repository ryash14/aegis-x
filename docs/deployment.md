# Private Linux deployment and recovery

The release supports one application instance, persistent SQLite/artifact storage,
CPU embeddings and a separate GPU inference process. The checked configuration is
native systemd plus Caddy; it does not require Docker or Kubernetes. The existing
8 GiB GPU can run the pinned quantized model. Hosting without a suitable GPU needs
a separate private inference host and explicit network/configuration changes.

The application is running locally. No external host, domain or hosting credentials
have been configured. A local TLS proxy/restart/restore drill passed; public DNS,
certificate issuance, firewall and production-host performance remain deployment gates.

## Prepare the host

Use a Linux host with Python 3.12, uv, Tesseract English/orientation data, and the
NVIDIA driver appropriate for its GPU. Install the locked environment in `/opt/aegis`:

```bash
uv sync --locked --extra dense --no-dev
uv run --locked python scripts/fetch_embedding_model.py
uv run --locked python scripts/prepare_local_runtime.py
```

The runtime preparation verifies the Ollama binary and pinned model manifest.
Reuse already verified model/runtime files when moving the release. Do not copy
development storage, credentials, `.git`, test documents or evaluation reports into
the served application directory. Provision the intended private documents later.

As a host administrator, create separate system users `aegis` and `aegis-model`.
Give `aegis-model` the home `/var/lib/aegis-model`; the inference runtime must be able
to write its own key there. Make `/var/lib/aegis` owned by `aegis:aegis`, mode 0700.
Keep `/opt/aegis` code and dependencies administrator-owned and readable/executable
by both services; verified embedding/model assets must be readable by their service.
Grant the model service GPU device access as required by the host's NVIDIA driver.

Copy `deploy/aegis.env.example` to `/etc/aegis/aegis.env`, mode 0640, accessible to
the administrator and application group. Replace **both** the allowed host and
public origin with your domain. Keep secure cookies enabled. The model endpoint
and application listener stay on loopback; expose only Caddy's HTTPS listener.

Provision the first invited account while the app is stopped:

```bash
sudo -u aegis /opt/aegis/.venv/bin/aegis-app --storage /var/lib/aegis user-add owner@example.com
```

The password is entered interactively. There is no default administrator password
and no public registration. An account named admin has normal project isolation.

## Start the services and TLS proxy

Install the two files in `deploy/` as `/etc/systemd/system/aegis.service` and
`/etc/systemd/system/aegis-model.service`. The configured paths must exist before
running `systemd-analyze verify`; verification does not provision users or GPU access.

```bash
sudo systemd-analyze verify /etc/systemd/system/aegis.service /etc/systemd/system/aegis-model.service
sudo systemctl daemon-reload
sudo systemctl enable --now aegis-model aegis
```

Use Caddy 2.11.7 or a compatible release. The workstation copy at
`data/runtime/caddy/caddy` was checked against the official release's SHA-512
checksum list. Install Caddy under a separate proxy service account using its
standard service installation, set `AEGIS_DOMAIN` to your domain in that service's
environment, and use `deploy/Caddyfile`. Validate before restarting:

```bash
AEGIS_DOMAIN=research.example.com caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Public certificate issuance requires correct DNS and reachable proxy ports. Keep
8787 and 11435 inaccessible externally. Caddy terminates TLS; the application uses
its configured HTTPS origin for CSRF checks and ignores untrusted proxy headers.
Persist Caddy's own certificate state independently of application backups.

## Release checks

Check `/health/live` for process liveness and `/health/ready` for storage, retrieval,
workers and the pinned model manifest. Readiness returns 503 until dependencies are
available; it does not prove answer quality or GPU latency. Authenticated
`/api/system` exposes configured resource limits and runtime identities.

On the target host verify login/logout, two-account isolation, upload/OCR, indexing,
a cited answer, revision review, decision/export, restart and recovery with synthetic
documents. Check logs with `journalctl -u aegis -u aegis-model`. The release's local
browser suites are:

```bash
npm ci
npm run test:private
npm run test:research
npm run test:deployment
```

The deployment suite requires the verified Caddy binary at the workstation path.
It uses a temporary local CA without adding it to the system trust store, checks
Secure/HttpOnly cookies over HTTPS, restarts a temporary app and restores its backup.
Its test accounts and storage are temporary; it never modifies the active workspace.

## Back up, restore and upgrade

Stop the application for a consistent snapshot; the command refuses a running
workspace. Inference need not stop. Archives contain original documents, account
hashes, audit records and the application key: store them privately and encrypt
off-host copies using your organization's chosen mechanism. A checksum detects
corruption; it does not authenticate a backup against an attacker.

```bash
sudo systemctl stop aegis
sudo -u aegis /opt/aegis/.venv/bin/aegis-app --storage /var/lib/aegis backup /var/backups/aegis/release.zip
sudo systemctl start aegis
```

The archive directory must exist and be writable by the application account;
archive creation is exclusive and mode 0600. Model weights are outside this archive
and must be preserved separately. To recover, restore into a **new** directory:

```bash
sudo -u aegis /opt/aegis/.venv/bin/aegis-app --storage /var/lib/aegis-recovered restore /var/backups/aegis/release.zip
```

Restore validates paths, sizes, hashes, database integrity and foreign keys before
publishing the directory. Existing storage is never overwritten. All restored
sessions are revoked; users sign in again. Point the stopped service's environment
and `ReadWritePaths` at the restored directory, then restart and run release checks.
Keep the prior directory until recovery is confirmed.

Before upgrading, back up the stopped app, install the locked release, restart and
check readiness plus a synthetic workflow. Schema migrations preserve existing
records; rollback requires restoring a compatible pre-upgrade backup. One server
owns a storage directory. SQLite and exact vector search are bounded single-host
choices; replicas need a different storage/queue deployment.
