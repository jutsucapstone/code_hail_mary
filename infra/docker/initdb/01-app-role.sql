-- Creates the role the application connects as, for local development.
--
-- This exists because of a failure found the hard way: the bootstrap role
-- (POSTGRES_USER) is a SUPERUSER, and superusers bypass row-level security
-- unconditionally. FORCE ROW LEVEL SECURITY does not change that — FORCE only covers
-- the table *owner*. Connecting the app as the bootstrap role means every RLS policy
-- is silently inert, every isolation test passes against nothing, and the leak appears
-- only in an environment that happens to use a restricted role.
--
-- So dev gets the same shape as production: migrations run as the owner, the
-- application runs as a role that cannot bypass anything.
--
-- Runs once, on first initialisation of an empty data volume. If you change it, you
-- must recreate the volume (`docker compose down -v`) for it to take effect.
--
-- The password here is dev-only and deliberately obvious. Staging and production create
-- this role through Terraform with a Secret Manager value (§4.10, §20).

CREATE ROLE jutsu_app
    LOGIN
    PASSWORD 'jutsu_app_dev_only'
    NOSUPERUSER
    NOBYPASSRLS
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT;

-- Connect and read the schema. Table privileges are granted by migration 0001, which
-- is the thing that knows which tables exist.
GRANT CONNECT ON DATABASE jutsu TO jutsu_app;
GRANT USAGE ON SCHEMA public TO jutsu_app;

-- The test database, created alongside so `make up` yields a working test target.
CREATE DATABASE jutsu_test OWNER jutsu;
GRANT CONNECT ON DATABASE jutsu_test TO jutsu_app;
