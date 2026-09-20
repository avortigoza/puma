# PUMA Procedures — Drafts

Draft Upgrade/Restore procedure content for pasting into each app's
Edit page in PUMA (Applications → *App* → Edit → Procedures). Review
and correct before pasting — these are drafts, not final content.

---

## Moodle (VLE)

### Upgrade Procedure

```markdown
## Before starting
1. Confirm the target version's minimum requirements — check `max_input_vars`
   in **all three** PHP ini files (web FPM, CLI): must be 5000+ for Moodle 5.2+.
   ```
   grep max_input_vars /etc/php/8.3/fpm/php.ini /etc/php/8.3/cli/php.ini /etc/php/8.4/cli/php.ini
   ```
2. Confirm which PHP version the CLI actually runs — if it defaults to a
   different version than the web (FPM) uses, the upgrade must be run
   explicitly with the matching version:
   ```
   php -v          # what CLI defaults to
   php8.3 -v       # what web/FPM actually uses
   ```
3. Check every third-party plugin (currently: mod/vpl, theme/almondb,
   local/ai_manager) for a compatible update — **do not skip the theme**.
   Moodle core login/UI markup can change between versions, and an
   outdated theme silently breaks styling even when it "still works."

4. **Take a backup.** Preferred: trigger it from PUMA (**Backup Now** on
   this app) and confirm it shows **Success** before touching anything.

   If PUMA is unavailable, or you want an independent copy outside it,
   do it manually with the same commands PUMA uses under the hood:
   ```
   DATE=$(date +%Y%m%d-%H%M%S)
   sudo -u postgres pg_dump moodle | sudo tee /tmp/moodle_${DATE}_db.sql > /dev/null
   sudo tar -czf /tmp/moodle_${DATE}_code.tar.gz -C /var/www moodle
   sudo tar -czf /tmp/moodle_${DATE}_data.tar.gz -C /var moodledata
   sudo cp /etc/apache2/sites-available/moodle.conf /tmp/moodle_${DATE}_vhost.conf.bak
   sudo -u www-data crontab -l > /tmp/moodle_${DATE}_crontab.bak
   ```
   Keep these somewhere safe until the upgrade is verified working.

## Rehearse first
5. Do a full dry run on a staging copy before touching production. Don't
   skip this even for a routine-looking upgrade — the 5.1→5.2 upgrade
   surfaced a real visual regression (broken login page) that only
   showed up once actually tested, not from release notes alone.

## Production upgrade
6. Put the site in maintenance mode.
7. Update Moodle core code to the target version.
8. Update any plugins identified in step 3 to their compatible versions.
9. Run the upgrade (web UI or `php admin/cli/upgrade.php`), using the
   **correct PHP version** identified in step 2.
10. Take the site out of maintenance mode.
11. Verify: log in, check the homepage, spot-check a course, confirm cron
    is still running (`sudo -u www-data crontab -l`).

## After
12. Leave the pre-upgrade code/data copies in place for about a week
    before deleting, in case something surfaces late.
13. Update the "installed version" shown in PUMA if it doesn't pick it
    up automatically.
```

### Restore Procedure

```markdown
Restores from a PUMA backup (`files.tar.gz` + `database.dump`) for this
server. Requires root.

1. Extract files — this puts everything back in its original location
   (Moodle code, moodledata, the Apache vhost, and the www-data crontab
   file), since it was archived from absolute paths as root:
   ```
   sudo tar xzf files.tar.gz -C /
   ```
2. Restore the database (this is a `pg_dump -Fc` archive, needs
   `pg_restore`, not `psql`):
   ```
   sudo -u postgres pg_restore -d moodle --clean --if-exists database.dump
   ```
3. Reinstall the crontab properly (a raw file copy isn't reliably picked
   up by cron):
   ```
   sudo crontab -u www-data /var/spool/cron/crontabs/www-data
   ```
4. Reload Apache:
   ```
   sudo systemctl reload apache2
   ```
5. Verify: log in, check the homepage, spot-check a course.

**Note:** this backup does not include the system nginx config that
proxies to Apache. On this same server that's already in place and
untouched — this only matters if restoring onto a different server.
```

---

## GitLab

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup (**Backup Now**) and confirm **Success** before touching anything.
2. **Check the official GitLab upgrade path** at
   https://gitlab-com.gitlab.io/support/toolbox/upgrade-path/ — GitLab
   enforces which versions you're allowed to hop between in one jump.
   Skipping this step is the single most common way a GitLab upgrade
   breaks; do not go straight to the latest version if the tool says
   you need intermediate stops.
3. Read the release notes for every version in that hop sequence for
   background migrations or manual steps.
4. For each hop in sequence:
   ```
   sudo apt update
   sudo apt install gitlab-ce=<version>-ce.0
   sudo gitlab-ctl reconfigure
   ```
5. After the final hop, confirm health:
   ```
   sudo gitlab-ctl status
   sudo gitlab-rake gitlab:check SANITIZE=true
   ```
6. Check **Admin Area → Monitoring → Background Migrations** — wait for these to finish before considering the upgrade done.
7. Log in, spot-check a project, confirm CI/CD still runs.
```

### Restore Procedure

```markdown
Restores from a PUMA backup (the `gitlab-backup` archive, plus
`gitlab.rb` and `gitlab-secrets.json`). Requires root.

1. Copy `gitlab.rb` and `gitlab-secrets.json` back into `/etc/gitlab/`
   — **this must happen before the restore step**, not after:
   ```
   sudo cp gitlab.rb /etc/gitlab/gitlab.rb
   sudo cp gitlab-secrets.json /etc/gitlab/gitlab-secrets.json
   ```
2. Copy the backup tar into GitLab's own backup directory:
   ```
   sudo cp <timestamp>_gitlab_backup.tar /var/opt/gitlab/backups/
   ```
3. Stop the services that touch the database:
   ```
   sudo gitlab-ctl stop puma
   sudo gitlab-ctl stop sidekiq
   ```
4. Restore:
   ```
   sudo gitlab-backup restore BACKUP=<timestamp>
   ```
5. Reconfigure and restart everything:
   ```
   sudo gitlab-ctl reconfigure
   sudo gitlab-ctl restart
   ```
6. Verify: `sudo gitlab-rake gitlab:check SANITIZE=true`, then log in and spot-check a project.
```

---

## XWiki

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup and confirm success.
2. Check XWiki's release notes for the target version — especially
   whether any installed extensions need updating for compatibility.
3. In `~/xwiki-docker`, update the image tag in `docker-compose.yml`
   to the target version.
4. Pull and recreate:
   ```
   docker compose pull
   docker compose up -d
   ```
5. XWiki runs its own upgrade/distribution wizard automatically on
   first access after the version bump — visit the site and follow
   any prompts it shows.
6. Verify: log in, check a page, check the Extension Manager for
   anything flagged as incompatible.
```

### Restore Procedure

```markdown
Restores from a PUMA backup (two volume archives, a database dump,
and `docker-compose.yml`).

1. Stop the stack: `docker compose down` (in `~/xwiki-docker`)
2. Restore the volumes:
   ```
   docker run --rm -v xwiki-docker_xwiki-data:/vol -v $(pwd):/backup alpine \
     sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-data.tar.gz -C /vol"
   docker run --rm -v xwiki-docker_xwiki-postgres-data:/vol -v $(pwd):/backup alpine \
     sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-postgres-data.tar.gz -C /vol"
   ```
   The Postgres volume restore above already restores the database
   itself (it's the raw data directory) — `database.dump` is kept as
   a portable fallback, useful mainly if migrating to a different
   Postgres version. Only use it if the volume restore doesn't work:
   ```
   docker compose up -d db
   docker exec -i xwiki-docker-db-1 pg_restore -U xwiki -d xwiki --clean --if-exists < database.dump
   ```
3. Start everything: `docker compose up -d`
4. Verify: load the site, log in, check a page.
```

---

## Authentik

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup and confirm success.
2. Check Authentik's release notes for the target version for any
   required upgrade-path notes or breaking changes.
3. In `/var/www/authentik`, update the image tag(s) in
   `docker-compose.yml` (or `.env` if a tag variable is used there).
4. Pull and recreate:
   ```
   docker compose pull
   docker compose up -d
   ```
   Database migrations run automatically on startup.
5. Verify: log into the admin interface, check **System → System
   Tasks** for any failed background tasks.
```

### Restore Procedure

```markdown
Restores from a PUMA backup (files + a docker-exec database dump).

1. Stop the stack: `docker compose down` (in `/var/www/authentik`)
2. Extract the files archive back into place:
   ```
   tar xzf files.tar.gz -C /var/www/authentik --strip-components=2
   ```
   (adjust `--strip-components` if needed so `docker-compose.yml`,
   `.env`, `media/`, and `custom-templates/` land directly in
   `/var/www/authentik`, not nested inside extra folders)
3. Start just the database:
   ```
   docker compose up -d postgresql
   ```
4. Restore the dump:
   ```
   docker exec -i authentik-postgresql-1 pg_restore -U authentik -d authentik --clean --if-exists < database.dump
   ```
5. Start everything: `docker compose up -d`
6. Verify: log into the admin interface, confirm existing users/apps are visible.
```

---

## Stanza CC / Stanza GMA

### Upgrade Procedure

```markdown
## Pre-Upgrade Checklist

1. **Trigger a PUMA backup** (Backup Now) as an additional safety net alongside the manual backup below.

2. **Check for active sessions.** Open the Stanza interface and check for any in progress. If found, notify the owner or the concerned group before proceeding.

3. **Stop the service and retrieve the license key:**
   - `Win + R` → `services.msc` → find **Telestream Stanza Service** → right-click → **Stop** → wait for status to show Stopped.
   - Open `http://localhost:7000`, click the license key icon (top-right), copy and save the key securely, then click **Deactivate** to release it before uninstalling.

4. **Manual backup.** After the service is stopped, copy these files from `C:\ProgramData\Telestream\Stanza`:
   - `stanza.db`
   - `appsettings.general.json`
   - `appsettings.locations.json`

   To: `D:\STANZA_SHARE\POSTMAMS\BACKUP\stanza-mmddyy` (e.g. `stanza-083125` for Aug 31, 2025)

5. **Uninstall the current version** — Settings → Apps → Installed Apps → search "Telestream Stanza" → ⋮ → Uninstall → follow prompts.

## Upgrade Steps

1. **Get the installer**, either:
   - Download from https://www.telestream.net/telestream-support/stanza/support.htm, or
   - Use the local copy at `D:\STANZA_SHARE\POSTMAMS\Stanza Installer\` (extract the ZIP to a temp folder first)

2. **Run the installer**, accept the license agreement, complete installation.

## Post-Upgrade Steps

1. **Activate the license** — `http://localhost:7000` → license key icon → enter the key → icon turns green once activated.

2. **Restore database and config:**
   - Stop the Telestream Stanza Service again via `services.msc`
   - Copy `stanza.db`, `appsettings.general.json`, `appsettings.locations.json` from your dated backup folder back to `C:\ProgramData\Telestream\Stanza`
   - Restart the Telestream Stanza Service

3. **Verify functionality:**
   - Test transcription, project creation, and Stanza AI features
   - Confirm project files and user data are up to date
   - Check logs for errors or warnings
   - Test in the actual bay for AI automation
```

### Restore Procedure

```markdown
Restores from a PUMA backup (`stanza.db` + two config JSON files).

1. Stop the Stanza Windows service (check the exact service name in
   `services.msc`, or run `net stop <servicename>`).
2. Extract the backup archive to `C:\` (the paths inside were stored
   with the drive letter stripped, so extracting at the root puts
   everything back under `C:\ProgramData\Telestream\Stanza\`
   correctly):
   ```
   tar xzf files.tar.gz -C C:\
   ```
3. Restart the Stanza service.
4. Verify: the app is reachable and shows the expected configuration/data.
```

---

## GLPI

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup (Backup Now) and confirm Success.
2. Read the release notes for the target version — check for required PHP/MySQL version bumps and plugin compatibility.
3. Download the new release, extract it to a temporary directory.
4. Preserve your existing config and files directories — copy them
   from the current install into the new release's directory
   (typically `files/` and the `.env`/`config_db.php` config).
5. Put the current `/var/www/glpi` aside (rename, don't delete) and
   move the new release into place.
6. GLPI detects the version mismatch automatically — either visit the
   site to trigger the web-based migration wizard, or run it via CLI:
   ```
   php bin/console db:update
   ```
7. Verify: log in, check a ticket, check that plugins still show as active under Setup → Plugins.
8. Once confirmed working, remove the old renamed install.
```

### Restore Procedure

```markdown
Restores from a PUMA backup (`files.tar.gz` from `/var/www/glpi`, and
`database.sql`, a MySQL/MariaDB dump).

1. Extract the files back into place on the GLPI server:
   ```
   sudo tar xzf files.tar.gz -C /var/www --strip-components=1 -C /var/www/glpi
   ```
   (adjust so the extracted contents land directly in `/var/www/glpi`,
   not nested inside an extra folder — check the archive's structure
   first with `tar tzf files.tar.gz | head`)

2. Restore the database (plain SQL dump — restore with the client,
   not `mysqldump`):
   ```
   mysql -u <db_user> -p <db_name> < database.sql
   ```
   (use the same database name/user GLPI's `config_db.php` and PUMA's
   backup config both point at)

3. Confirm file ownership/permissions match what the web server
   expects (typically `www-data:www-data`):
   ```
   sudo chown -R www-data:www-data /var/www/glpi
   ```

4. Verify: log in, check a ticket, check Setup → General to confirm settings loaded correctly.

**Note:** session files (`_sessions`) are deliberately excluded from this backup — nothing to restore there, GLPI regenerates them on its own.
```
