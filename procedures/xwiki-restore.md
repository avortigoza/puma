## Reference

Restores from a PUMA backup: two Docker volume archives, a database dump, and `docker-compose.yml`.

## Restore steps

Stop the stack (in `~/xwiki-docker`):

```
docker compose down
```

Restore the wiki-data volume:

```
docker run --rm -v xwiki-docker_xwiki-data:/vol -v $(pwd):/backup alpine \
  sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-data.tar.gz -C /vol"
```

Restore the postgres-data volume the same way:

```
docker run --rm -v xwiki-docker_xwiki-postgres-data:/vol -v $(pwd):/backup alpine \
  sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-postgres-data.tar.gz -C /vol"
```

The volume restore above already restores the database itself (it's the raw Postgres data directory) — `database.dump` is kept as a portable fallback, useful mainly if migrating to a different Postgres version. Only use it if the volume restore doesn't work:

```
docker compose up -d db
docker exec -i xwiki-docker-db-1 pg_restore -U xwiki -d xwiki --clean --if-exists < database.dump
```

Start everything:

```
docker compose up -d
```

## Verify

Load the site, log in, check a page.
