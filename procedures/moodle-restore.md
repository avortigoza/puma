Restores from a PUMA backup (`files.tar.gz` + `database.dump`) for this server. Requires root.

1. Extract files — this puts everything back in its original location (Moodle code, moodledata, the Apache vhost, and the www-data crontab file), since it was archived from absolute paths as root: `sudo tar xzf files.tar.gz -C /`
2. Restore the database (this is a `pg_dump -Fc` archive, needs `pg_restore`, not `psql`): `sudo -u postgres pg_restore -d moodle --clean --if-exists database.dump`
3. Reinstall the crontab properly (a raw file copy isn't reliably picked up by cron): `sudo crontab -u www-data /var/spool/cron/crontabs/www-data`
4. Reload Apache: `sudo systemctl reload apache2`
5. Verify: log in, check the homepage, spot-check a course.

**Note:** this backup does not include the system nginx config that proxies to Apache. On this same server that's already in place and untouched — this only matters if restoring onto a different server.

**Note:** production also has a richer, environment-aware version of this procedure already saved directly in PUMA (with a staging-clone reference table and a reusable backup script) - if that's present, prefer it over this simpler draft.
