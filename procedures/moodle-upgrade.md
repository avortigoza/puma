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
