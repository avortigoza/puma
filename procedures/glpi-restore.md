## Reference

Restores from a PUMA backup: `files.tar.gz` from `/var/www/glpi`, and `database.sql` (a plain MySQL/MariaDB dump), pulled over SSH from 10.0.0.161. Session files (`_sessions`) are deliberately excluded — GLPI regenerates them on its own.

## Restore steps

Check the archive's structure first:

```
tar tzf files.tar.gz | head
```

Extract so the contents land directly in `/var/www/glpi`, not nested inside an extra folder.

Restore the database (a plain SQL dump — restore with the client, not `mysqldump`):

```
mysql -u <db_user> -p <db_name> < database.sql
```

(use the same database name/user GLPI's `config_db.php` and PUMA's backup config both point at)

Confirm file ownership/permissions match what the web server expects:

```
sudo chown -R www-data:www-data /var/www/glpi
```

## Verify

Log in, check a ticket, check Setup → General to confirm settings loaded correctly.
