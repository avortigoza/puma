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
