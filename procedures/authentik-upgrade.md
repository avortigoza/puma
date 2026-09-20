## Reference

- Host: separate server, 10.0.0.161 (same box as GLPI)
- Compose project: `/var/www/authentik`
- Containers: `authentik-server-1`, `authentik-worker-1`, `authentik-postgresql-1`, `authentik-redis-1`
- Media/templates: local bind mounts (`/var/www/authentik/media`, `/var/www/authentik/custom-templates`) — no S3 configured
- `.env` holds `AUTHENTIK_SECRET_KEY` and the database password — required for a real restore, treat as sensitive

## Before starting

Trigger a PUMA backup and confirm success. Check Authentik's release notes for the target version for any required upgrade-path notes or breaking changes.

## Upgrade steps

In `/var/www/authentik`, update the image tag(s) in `docker-compose.yml` (or `.env` if a tag variable is used there), then pull and recreate:

```
docker compose pull
docker compose up -d
```

Database migrations run automatically on startup.

## Verify

Log into the admin interface, check System → System Tasks for any failed background tasks.
