## Reference

Restores from a PUMA backup: files (`docker-compose.yml`, `.env`, `media/`, `custom-templates/`) plus a docker-exec database dump, all pulled over SSH from 10.0.0.161.

## Restore steps

Stop the stack (in `/var/www/authentik`):

```
docker compose down
```

Extract the files archive back into place:

```
tar xzf files.tar.gz -C /var/www/authentik --strip-components=2
```

(adjust `--strip-components` if needed so `docker-compose.yml`, `.env`, `media/`, and `custom-templates/` land directly in `/var/www/authentik`, not nested inside extra folders)

Start just the database:

```
docker compose up -d postgresql
```

Restore the dump:

```
docker exec -i authentik-postgresql-1 pg_restore -U authentik -d authentik --clean --if-exists < database.dump
```

Start everything:

```
docker compose up -d
```

## Verify

Log into the admin interface, confirm existing users/apps are visible.
