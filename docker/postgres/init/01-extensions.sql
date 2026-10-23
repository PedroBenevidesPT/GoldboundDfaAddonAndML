-- Runs once, on an empty volume, before Alembic takes over.
--
-- Two things belong here and nowhere else:
--
--   * extensions the schema depends on.  Creating them from a migration
--     needs superuser rights, which the application role deliberately does
--     not have.
--   * nothing else.  Tables are owned by Alembic (`db/migrations`), so that
--     `alembic upgrade head` on a fresh database produces exactly the schema
--     a migrated one has.  A table created here and not in a migration is a
--     schema difference that only shows up in production.

-- Trigram matching for realm and item names.  `ah.realm_match` compares
-- lowercased names; an index on lower(name) keeps that a lookup instead of a
-- scan once the realm table grows.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Deterministic text ordering is set on the cluster (`--locale=C` in the
-- compose file), so nothing here needs to override it.
