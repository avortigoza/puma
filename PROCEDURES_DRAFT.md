# PUMA Procedures — Drafts

Draft Upgrade/Restore procedure content for pasting into each app's
Edit page in PUMA (Applications → *App* → Edit → Procedures). Review
and correct before pasting — these are drafts, not final content.

**Formatting note:** every command here sits in its own standalone
fenced code block, never nested inside a numbered list item. PUMA's
Markdown renderer needs exact leading-whitespace indentation to keep a
code block "attached" to a list item, and that whitespace is easy to
lose when copying through a browser's rendered view and a clipboard -
losing it makes the renderer treat the code block as ending the list
and restart numbering at 1 for whatever follows. Standalone blocks
between plain paragraphs have no such dependency and render reliably
either way.

**Security note:** these are viewable by any logged-in PUMA user, not
just admins - the actual database passwords for each app live in that
app's Backup config, which only admins can open. Procedures reference
credentials as "see this app's Backup config" rather than restating
the real password in a page more people can read.

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

**Note:** production also has a richer, environment-aware version of this procedure already saved directly in PUMA (with a staging-clone reference table and a reusable backup script) - if that's present, prefer it over this simpler draft.

```

---

## GitLab

### Upgrade Procedure

```markdown
## Reference

- Host: this server (mss-serv-cl1), same as PUMA
- Config: `/etc/gitlab/gitlab.rb`
- Secrets: `/etc/gitlab/gitlab-secrets.json` — required to decrypt any backup; never regenerate this file
- Backup output: `/var/opt/gitlab/backups/`
- PUMA triggers backups via a dedicated `gitlab-backup.service` systemd unit (not run directly by PUMA — GitLab's own backup tool needs full root, which PUMA's sandboxed service account deliberately doesn't have)

## Before starting

Trigger a PUMA backup (Backup Now) and confirm **Success** before touching anything.

Check the official GitLab upgrade path at gitlab-com.gitlab.io/support/toolbox/upgrade-path/ before picking a target version — GitLab enforces which versions you're allowed to hop between in one jump, and going straight to the latest release is the single most common way an upgrade breaks. Read the release notes for every version in the resulting hop sequence, watching for required manual steps or background migrations.

## Upgrade steps

Run for each hop in the sequence:

```
sudo apt update
sudo apt install gitlab-ce=<version>-ce.0
sudo gitlab-ctl reconfigure
```

After the final hop, confirm health:

```
sudo gitlab-ctl status
sudo gitlab-rake gitlab:check SANITIZE=true
```

## Verify

- Admin Area → Monitoring → Background Migrations — wait for these to finish before considering the upgrade done.
- Log in, spot-check a project, confirm CI/CD still runs.

```

### Restore Procedure

```markdown
## Reference

Restores from a PUMA backup: the `gitlab-backup` archive, plus `gitlab.rb` and `gitlab-secrets.json` (both included specifically because GitLab's own backup tool excludes them but requires them to restore). Requires root.

## Restore steps

Copy the config and secrets back into place first — this must happen before the restore step runs, not after:

```
sudo cp gitlab.rb /etc/gitlab/gitlab.rb
sudo cp gitlab-secrets.json /etc/gitlab/gitlab-secrets.json
```

Copy the backup tar into GitLab's own backup directory:

```
sudo cp <timestamp>_gitlab_backup.tar /var/opt/gitlab/backups/
```

Stop the services that touch the database:

```
sudo gitlab-ctl stop puma
sudo gitlab-ctl stop sidekiq
```

Restore:

```
sudo gitlab-backup restore BACKUP=<timestamp>
```

Reconfigure and restart everything:

```
sudo gitlab-ctl reconfigure
sudo gitlab-ctl restart
```

## Verify

```
sudo gitlab-rake gitlab:check SANITIZE=true
```

Then log in and spot-check a project.

```

---

## XWiki

### Upgrade Procedure

```markdown
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

```

### Restore Procedure

```markdown
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

```

---

## Authentik

### Upgrade Procedure

```markdown
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

```

### Restore Procedure

```markdown
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

```

---

## Stanza CC / Stanza GMA

### Upgrade Procedure

```markdown
## Reference

- Config/data: `C:\ProgramData\Telestream\Stanza` (`stanza.db`, `appsettings.general.json`, `appsettings.locations.json`)
- Manual backup location: `D:\STANZA_SHARE\POSTMAMS\BACKUP\stanza-mmddyy`
- Installer share: `D:\STANZA_SHARE\POSTMAMS\Stanza Installer\`
- License management: `http://localhost:7000`

## Pre-upgrade checklist

Check for active sessions in the Stanza interface — if any are found, notify the owner or the concerned group before proceeding.

Stop the service and retrieve the license key: open `services.msc`, find **Telestream Stanza Service**, right-click → Stop, wait for status to show Stopped. Then open `http://localhost:7000`, click the license key icon (top-right), copy and save the key securely, then click Deactivate to release it before uninstalling.

Trigger a PUMA backup (Backup Now) as an additional safety net, then take the manual backup too — copy `stanza.db`, `appsettings.general.json`, and `appsettings.locations.json` from `C:\ProgramData\Telestream\Stanza` to a dated folder under `D:\STANZA_SHARE\POSTMAMS\BACKUP\` (e.g. `stanza-083125` for Aug 31, 2025).

Uninstall the current version — Settings → Apps → Installed Apps → search "Telestream Stanza" → the ⋮ menu → Uninstall → follow prompts.

## Upgrade steps

Get the installer, either downloading it from telestream.net/telestream-support/stanza/support.htm, or using the local copy on the installer share (extract the ZIP to a temp folder first). Run the installer, accept the license agreement, complete installation.

## Post-upgrade steps

Activate the license — `http://localhost:7000` → license key icon → enter the key → icon turns green once activated.

Restore database and config: stop the Telestream Stanza Service again via `services.msc`, copy the three files back from your dated backup folder to `C:\ProgramData\Telestream\Stanza`, then restart the service.

## Verify

- Test transcription, project creation, and Stanza AI features.
- Confirm project files and user data are up to date.
- Check logs for errors or warnings.
- Test in the actual bay for AI automation.

```

### Restore Procedure

```markdown
## Reference

Restores from a PUMA backup: `stanza.db` plus `appsettings.general.json` and `appsettings.locations.json`, from `C:\ProgramData\Telestream\Stanza`.

## Restore steps

Stop the Stanza Windows service (check the exact service name in `services.msc`, or run `net stop <servicename>`).

Extract the backup archive to `C:\` — the paths inside were stored with the drive letter stripped, so extracting at the root puts everything back under `C:\ProgramData\Telestream\Stanza\` correctly:

```
tar xzf files.tar.gz -C C:\
```

Restart the Stanza service.

## Verify

The app is reachable and shows the expected configuration/data.

```

---

## GLPI

### Upgrade Procedure

```markdown
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

```

### Restore Procedure

```markdown
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

```
