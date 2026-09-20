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
