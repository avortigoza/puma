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
