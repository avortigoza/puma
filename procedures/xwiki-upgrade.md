## Reference

- Host: this server (mss-serv-cl1), same as PUMA
- Compose project: `~/xwiki-docker`
- Containers: `xwiki-docker-xwiki-1` (app, published on port 8888), `xwiki-docker-db-1` (Postgres)
- Database credentials: see this app's Backup config in PUMA (Applications → XWiki → Edit → Backups)

## Before starting

Trigger a PUMA backup and confirm success. Check XWiki's release notes for the target version — especially whether any installed extensions need updating for compatibility.

## Upgrade steps

In `~/xwiki-docker`, update the image tag in `docker-compose.yml` to the target version, then pull and recreate:

```
docker compose pull
docker compose up -d
```

XWiki runs its own upgrade/distribution wizard automatically on first access after the version bump — visit the site and follow any prompts it shows.

## Verify

- Log in, check a page.
- Check the Extension Manager for anything flagged as incompatible.
