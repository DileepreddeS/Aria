-- Three roles, because row-level security is bypassed by a table's owner.
--
--   aria_owner   owns the schema. No runtime component connects as this role.
--   aria_migrate runs Alembic. Can create and alter tables it is granted.
--   aria_app     the API and gateway. Subject to RLS, cannot alter tables,
--                cannot turn row security off (tested in test_rls_cross_tenant.py).
--
-- Runs once, on first container start, as aria_owner (POSTGRES_USER).

\set app_password `echo "$ARIA_APP_PASSWORD"`
\set migrate_password `echo "$ARIA_MIGRATE_PASSWORD"`

CREATE ROLE aria_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
CREATE ROLE aria_migrate LOGIN PASSWORD :'migrate_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- aria_migrate creates objects in the public schema; aria_owner keeps ownership
-- of the schema itself so aria_migrate cannot drop it.
GRANT USAGE, CREATE ON SCHEMA public TO aria_migrate;
GRANT USAGE ON SCHEMA public TO aria_app;

-- Anything aria_migrate creates later is readable/writable by aria_app by default.
-- Table-level grants are still made explicitly in the migration; this is the floor,
-- and it deliberately does not include TRUNCATE or REFERENCES.
ALTER DEFAULT PRIVILEGES FOR ROLE aria_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aria_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aria_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO aria_app;

-- No connection to the database for the world.
REVOKE ALL ON DATABASE aria FROM PUBLIC;
GRANT CONNECT ON DATABASE aria TO aria_app, aria_migrate;

-- PUBLIC can create objects in `public` on Postgres < 15 only, but be explicit.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
