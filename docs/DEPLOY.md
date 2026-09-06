# Dedicated Dokku deployment

Target: existing `dokku@vps.facab.se`, app `hjerte`, hostname `hjerte.facab.se`. Do not alter other applications or wildcard DNS. A new app needs no production-data migration from other services.

1. Verify tests and migration checks, then commit/push the exact source to the user's Hjerte repository.
2. DNS: A record `hjerte.facab.se` → `103.177.249.101`; do not create an AAAA record without a verified IPv6 route.
3. Create app `hjerte`; named storage `hjerte-data` and `hjerte-backups` with owner 10001:10001 and mode 0750. Mount at `/app/data` and `/app/backups`.
4. Set dedicated Django secret, production host and CSRF origin, `DJANGO_DEBUG=0`, `DATA_DIR=/app/data`, `BACKUP_DIR=/app/backups`. Set `DJANGO_ALLOWED_HOSTS=hjerte.facab.se,localhost,127.0.0.1` and `DJANGO_CSRF_TRUSTED_ORIGINS=https://hjerte.facab.se`. Import no secrets via Git or image build arguments.
5. Use one authoritative allowance ledger. After the local pilot finishes, produce a consistent `backup_hjerte` snapshot and restore it only to this new empty application's data mount. Disable local paid generation. Do not run separately funded local and production ledgers under the same approval.
6. Deploy via `git push dokku HEAD:main` using remote `dokku@vps.facab.se:hjerte`. Initial scale `web=1,worker=0`. Worker can be enabled after data and allowance readback.
7. Configure `ports:set hjerte http:80:8000`, `domains:set hjerte hjerte.facab.se`, then enable Let's Encrypt using the VPS's existing configured account. Confirm HTTPS, database health, login, authenticated source access and deployment checks. HTTP should redirect to HTTPS; no raw login over HTTP.
8. Set the dedicated OpenAI key in runtime config without printing it. Enable `worker=1` only after confirming allowance, model settings and queue. One worker is enough for this personal app.
9. `app.json` schedules `backup_hjerte` nightly. Verify the cron report and create a first backup. Backup retains 30 archives and checks database integrity. Test restoration into a separate temporary directory with `PRAGMA integrity_check` and count the main tables; never test restore over the live DB.

Health endpoint `/health/` is exempt from SSL redirection for internal container checks but returns no private data. All learner/admin pages remain HTTPS-only. Unauthenticated source-file requests redirect to login, with no public media mount.

If a deployment fails, restore the last known source commit and use the existing database only for backward-compatible schema changes. For data recovery, stop web/worker and take a fresh backup before restoring a verified earlier archive. Dokku 0.38.27 does not provide the previously assumed `releases:*` rollback interface.

The `.env` file is local only. `HJERTE_BOOTSTRAP_PASSWORD` initializes tomas once; it never resets an existing account. Change the password through the account page or the Django `changepassword` command. Keep backups and Django/API credentials under the user's own control.

The optional WebMCP current-question reader and learning-flag action are feature-detected. No supported WebMCP execution context was available for contract validation during initial deployment; ordinary study does not depend on this integration.
