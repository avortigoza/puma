# PUMA Procedures — Drafts

Draft Upgrade/Restore procedure content for pasting into each app's
Edit page in PUMA (Applications → *App* → Edit → Procedures). Review
and correct before pasting — these are drafts, not final content.

**Formatting note:** commands are written as single-line, inline code
(single backticks) rather than nested multi-line fenced blocks. This
is deliberate, not a style preference — PUMA's Markdown renderer
(Python-Markdown) requires exact leading-whitespace indentation to
keep a code block "attached" to its numbered list item, and that
whitespace is very easy to lose when copying text through a browser's
rendered view and a clipboard. When it's lost, the renderer treats the
code block as ending the list, and restarts numbering at 1 for
whatever comes after — which is exactly what happened with the first,
differently-formatted draft of this file. The inline-command format
below has no such dependency and is not vulnerable to that failure
mode.

---

## Moodle (VLE)

### Upgrade Procedure

```markdown
## Before starting
1. Confirm the target version's minimum requirements — check `max_input_vars` in all three PHP ini files (web FPM, CLI): must be 5000+ for Moodle 5.2+. Check with: `grep max_input_vars /etc/php/8.3/fpm/php.ini /etc/php/8.3/cli/php.ini /etc/php/8.4/cli/php.ini`
2. Confirm which PHP version the CLI actually runs — if it defaults to a different version than the web (FPM) uses, the upgrade must be run explicitly with the matching version: compare `php -v` (CLI default) against `php8.3 -v` (what web/FPM actually uses).
3. Check every third-party plugin (currently: mod/vpl, theme/almondb, local/ai_manager) for a compatible update — do not skip the theme. Moodle core login/UI markup can change between versions, and an outdated theme silently breaks styling even when it "still works."
4. Take a backup. Preferred: trigger it from PUMA (Backup Now on this app) and confirm it shows Success before touching anything.
5. If PUMA is unavailable, or you want an independent copy outside it, do it manually: dump the database with `sudo -u postgres pg_dump moodle | sudo tee /tmp/moodle_db.sql > /dev/null`, archive the code with `sudo tar -czf /tmp/moodle_code.tar.gz -C /var/www moodle`, archive the data with `sudo tar -czf /tmp/moodle_data.tar.gz -C /var moodledata`, copy the vhost with `sudo cp /etc/apache2/sites-available/moodle.conf /tmp/moodle_vhost.conf.bak`, and export the crontab with `sudo -u www-data crontab -l > /tmp/moodle_crontab.bak`. Keep these somewhere safe until the upgrade is verified working.

## Rehearse first
6. Do a full dry run on a staging copy before touching production. Don't skip this even for a routine-looking upgrade — the 5.1→5.2 upgrade surfaced a real visual regression (broken login page) that only showed up once actually tested, not from release notes alone.

## Production upgrade
7. Put the site in maintenance mode.
8. Update Moodle core code to the target version.
9. Update any plugins identified in step 3 to their compatible versions.
10. Run the upgrade (web UI, or `php admin/cli/upgrade.php` using the correct PHP version identified in step 2).
11. Take the site out of maintenance mode.
12. Verify: log in, check the homepage, spot-check a course, confirm cron is still running (`sudo -u www-data crontab -l`).

## After
13. Leave the pre-upgrade code/data copies in place for about a week before deleting, in case something surfaces late.
14. Update the "installed version" shown in PUMA if it doesn't pick it up automatically.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (`files.tar.gz` + `database.dump`) for this server. Requires root.

1. Extract files — this puts everything back in its original location (Moodle code, moodledata, the Apache vhost, and the www-data crontab file), since it was archived from absolute paths as root: `sudo tar xzf files.tar.gz -C /`
2. Restore the database (this is a `pg_dump -Fc` archive, needs `pg_restore`, not `psql`): `sudo -u postgres pg_restore -d moodle --clean --if-exists database.dump`
3. Reinstall the crontab properly (a raw file copy isn't reliably picked up by cron): `sudo crontab -u www-data /var/spool/cron/crontabs/www-data`
4. Reload Apache: `sudo systemctl reload apache2`
5. Verify: log in, check the homepage, spot-check a course.

**Note:** this backup does not include the system nginx config that proxies to Apache. On this same server that's already in place and untouched — this only matters if restoring onto a different server.

```

---

## GitLab

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup (Backup Now) and confirm Success before touching anything.
2. Check the official GitLab upgrade path at gitlab-com.gitlab.io/support/toolbox/upgrade-path/ — GitLab enforces which versions you're allowed to hop between in one jump. Skipping this step is the single most common way a GitLab upgrade breaks; do not go straight to the latest version if the tool says you need intermediate stops.
3. Read the release notes for every version in that hop sequence for background migrations or manual steps.
4. For each hop in sequence, run: `sudo apt update && sudo apt install gitlab-ce=<version>-ce.0 && sudo gitlab-ctl reconfigure`
5. After the final hop, confirm health: `sudo gitlab-ctl status` and `sudo gitlab-rake gitlab:check SANITIZE=true`
6. Check Admin Area → Monitoring → Background Migrations — wait for these to finish before considering the upgrade done.
7. Log in, spot-check a project, confirm CI/CD still runs.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (the `gitlab-backup` archive, plus `gitlab.rb` and `gitlab-secrets.json`). Requires root.

1. Copy `gitlab.rb` and `gitlab-secrets.json` back into `/etc/gitlab/` — this must happen before the restore step, not after: `sudo cp gitlab.rb /etc/gitlab/gitlab.rb && sudo cp gitlab-secrets.json /etc/gitlab/gitlab-secrets.json`
2. Copy the backup tar into GitLab's own backup directory: `sudo cp <timestamp>_gitlab_backup.tar /var/opt/gitlab/backups/`
3. Stop the services that touch the database: `sudo gitlab-ctl stop puma && sudo gitlab-ctl stop sidekiq`
4. Restore: `sudo gitlab-backup restore BACKUP=<timestamp>`
5. Reconfigure and restart everything: `sudo gitlab-ctl reconfigure && sudo gitlab-ctl restart`
6. Verify: run `sudo gitlab-rake gitlab:check SANITIZE=true`, then log in and spot-check a project.

```

---

## XWiki

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup and confirm success.
2. Check XWiki's release notes for the target version — especially whether any installed extensions need updating for compatibility.
3. In `~/xwiki-docker`, update the image tag in `docker-compose.yml` to the target version.
4. Pull and recreate: `docker compose pull && docker compose up -d`
5. XWiki runs its own upgrade/distribution wizard automatically on first access after the version bump — visit the site and follow any prompts it shows.
6. Verify: log in, check a page, check the Extension Manager for anything flagged as incompatible.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (two volume archives, a database dump, and `docker-compose.yml`).

1. Stop the stack (in `~/xwiki-docker`): `docker compose down`
2. Restore the wiki-data volume: `docker run --rm -v xwiki-docker_xwiki-data:/vol -v $(pwd):/backup alpine sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-data.tar.gz -C /vol"`
3. Restore the postgres-data volume the same way: `docker run --rm -v xwiki-docker_xwiki-postgres-data:/vol -v $(pwd):/backup alpine sh -c "rm -rf /vol/* && tar xzf /backup/xwiki-docker_xwiki-postgres-data.tar.gz -C /vol"`
4. The volume restore above already restores the database itself (it's the raw data directory) — `database.dump` is kept as a portable fallback, useful mainly if migrating to a different Postgres version. Only use it if the volume restore doesn't work: start just the db with `docker compose up -d db`, then `docker exec -i xwiki-docker-db-1 pg_restore -U xwiki -d xwiki --clean --if-exists < database.dump`
5. Start everything: `docker compose up -d`
6. Verify: load the site, log in, check a page.

```

---

## Authentik

### Upgrade Procedure

```markdown
1. Trigger a PUMA backup and confirm success.
2. Check Authentik's release notes for the target version for any required upgrade-path notes or breaking changes.
3. In `/var/www/authentik`, update the image tag(s) in `docker-compose.yml` (or `.env` if a tag variable is used there).
4. Pull and recreate: `docker compose pull && docker compose up -d` — database migrations run automatically on startup.
5. Verify: log into the admin interface, check System → System Tasks for any failed background tasks.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (files + a docker-exec database dump).

1. Stop the stack (in `/var/www/authentik`): `docker compose down`
2. Extract the files archive back into place: `tar xzf files.tar.gz -C /var/www/authentik --strip-components=2` (adjust `--strip-components` if needed so `docker-compose.yml`, `.env`, `media/`, and `custom-templates/` land directly in `/var/www/authentik`, not nested inside extra folders)
3. Start just the database: `docker compose up -d postgresql`
4. Restore the dump: `docker exec -i authentik-postgresql-1 pg_restore -U authentik -d authentik --clean --if-exists < database.dump`
5. Start everything: `docker compose up -d`
6. Verify: log into the admin interface, confirm existing users/apps are visible.

```

---

## Stanza CC / Stanza GMA

### Upgrade Procedure

```markdown
## Pre-Upgrade Checklist
1. Trigger a PUMA backup (Backup Now) as an additional safety net alongside the manual backup below.
2. Check for active sessions. Open the Stanza interface and check for any in progress. If found, notify the owner or the concerned group before proceeding.
3. Stop the service and retrieve the license key: open services.msc, find Telestream Stanza Service, right-click, Stop, wait for status to show Stopped. Then open `http://localhost:7000`, click the license key icon (top-right), copy and save the key securely, then click Deactivate to release it before uninstalling.
4. Manual backup. After the service is stopped, copy `stanza.db`, `appsettings.general.json`, and `appsettings.locations.json` from `C:\ProgramData\Telestream\Stanza` to `D:\STANZA_SHARE\POSTMAMS\BACKUP\stanza-mmddyy` (e.g. stanza-083125 for Aug 31, 2025).
5. Uninstall the current version — Settings → Apps → Installed Apps → search "Telestream Stanza" → the ⋮ menu → Uninstall → follow prompts.

## Upgrade Steps
6. Get the installer, either downloading from telestream.net/telestream-support/stanza/support.htm, or using the local copy at `D:\STANZA_SHARE\POSTMAMS\Stanza Installer\` (extract the ZIP to a temp folder first).
7. Run the installer, accept the license agreement, complete installation.

## Post-Upgrade Steps
8. Activate the license — `http://localhost:7000` → license key icon → enter the key → icon turns green once activated.
9. Restore database and config: stop the Telestream Stanza Service again via services.msc, copy `stanza.db`, `appsettings.general.json`, `appsettings.locations.json` from your dated backup folder back to `C:\ProgramData\Telestream\Stanza`, then restart the service.
10. Verify functionality: test transcription, project creation, and Stanza AI features; confirm project files and user data are up to date; check logs for errors or warnings; test in the actual bay for AI automation.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (`stanza.db` + two config JSON files).

1. Stop the Stanza Windows service (check the exact service name in services.msc, or run `net stop <servicename>`).
2. Extract the backup archive to `C:\` (the paths inside were stored with the drive letter stripped, so extracting at the root puts everything back under `C:\ProgramData\Telestream\Stanza\` correctly): `tar xzf files.tar.gz -C C:\`
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
4. Preserve your existing config and files directories — copy them from the current install into the new release's directory (typically `files/` and the `.env`/`config_db.php` config).
5. Put the current `/var/www/glpi` aside (rename, don't delete) and move the new release into place.
6. GLPI detects the version mismatch automatically — either visit the site to trigger the web-based migration wizard, or run it via CLI: `php bin/console db:update`
7. Verify: log in, check a ticket, check that plugins still show as active under Setup → Plugins.
8. Once confirmed working, remove the old renamed install.

```

### Restore Procedure

```markdown
Restores from a PUMA backup (`files.tar.gz` from `/var/www/glpi`, and `database.sql`, a MySQL/MariaDB dump).

1. Extract the files back into place on the GLPI server. Check the archive's structure first with `tar tzf files.tar.gz | head`, then extract so the contents land directly in `/var/www/glpi`, not nested inside an extra folder.
2. Restore the database (plain SQL dump — restore with the client, not mysqldump): `mysql -u <db_user> -p <db_name> < database.sql` (use the same database name/user GLPI's `config_db.php` and PUMA's backup config both point at).
3. Confirm file ownership/permissions match what the web server expects: `sudo chown -R www-data:www-data /var/www/glpi`
4. Verify: log in, check a ticket, check Setup → General to confirm settings loaded correctly.

**Note:** session files (`_sessions`) are deliberately excluded from this backup — nothing to restore there, GLPI regenerates them on its own.

```
