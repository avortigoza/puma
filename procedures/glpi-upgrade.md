## Reference

- Host: separate server, 10.0.0.161 (same box as Authentik)
- Code root: `/var/www/glpi`
- Database: MySQL/MariaDB — see this app's Backup config in PUMA for credentials

## Before starting

Trigger a PUMA backup (Backup Now) and confirm Success. Read the release notes for the target version — check for required PHP/MySQL version bumps and plugin compatibility.

## Upgrade steps

Download the new release and extract it to a temporary directory. Preserve your existing config and files directories — copy them from the current install into the new release's directory (typically `files/` and the `.env`/`config_db.php` config). Put the current `/var/www/glpi` aside (rename, don't delete) and move the new release into place.

GLPI detects the version mismatch automatically — either visit the site to trigger the web-based migration wizard, or run it via CLI:

```
php bin/console db:update
```

## Verify

- Log in, check a ticket.
- Check that plugins still show as active under Setup → Plugins.
- Once confirmed working, remove the old renamed install.
