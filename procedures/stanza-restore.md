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
