# How to restore the Argus accounts database from a backup

Backups: `/opt/argus-server/backups/argus-accounts-YYYYMMDD.sqlite.gz` (every day at 03:30, kept for 30 days).

1. Stop the server: `cd /opt/argus-server/deploy/server && docker compose stop argus-server`
2. Save what is currently in the volume, just in case:
   `docker run --rm -v server_argus-server-db:/data -v /opt/argus-server/backups:/b alpine cp /data/argus-accounts.sqlite /b/before-restore.sqlite`
3. Put the backup into the volume:
   `gunzip -k /opt/argus-server/backups/argus-accounts-YYYYMMDD.sqlite.gz`
   `docker run --rm -v server_argus-server-db:/data -v /opt/argus-server/backups:/b alpine cp /b/argus-accounts-YYYYMMDD.sqlite /data/argus-accounts.sqlite`
4. Start it: `docker compose start argus-server` and check `curl https://api.arguspilot.com/pricing`.

The volume name is in `docker volume ls | grep argus`. The backups sit on the same server: if the server is lost, they are lost too,
so once a week it is worth pulling a fresh one to your own machine (`scp root@203.0.113.10:/opt/argus-server/backups/<file> .`).
