-- Main application database: extensions, awkward text, bytea, large objects,
-- partitions, views, triggers and a function whose body has a CREATE TABLE
-- at the start of a line (a dump scanner must not count it as a table).
CREATE EXTENSION IF NOT EXISTS hstore;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA reporting;

CREATE TABLE notes (
    id serial PRIMARY KEY,
    body text NOT NULL,
    tags hstore,
    email citext,
    uid uuid DEFAULT uuid_generate_v4(),
    doc jsonb,
    raw bytea,
    amounts numeric(12, 2)[],
    created timestamptz DEFAULT now()
);
COMMENT ON TABLE notes IS 'notes ✓ with a comment';
INSERT INTO notes (body, tags, email, doc, raw, amounts) VALUES
    ('emoji 🎉🚀 CJK 漢字 RTL עברית combining é', 'a=>1,b=>2', 'Alice@Example.com',
     '{"k": "v", "n": [1, 2, 3]}', '\x00ff10deadbeef00', '{1.50,2.25}'),
    (E'tab\there\nnewline, backslash \\ and quote '' end', NULL, 'bob@example.com', '{}', '\x', '{}');
INSERT INTO notes (body) SELECT 'row ' || g FROM generate_series(1, 5000) g;
CREATE INDEX notes_body_trgm ON notes USING gin (body gin_trgm_ops);

CREATE TABLE attachments (id serial PRIMARY KEY, name text, content oid);
INSERT INTO attachments (name, content) VALUES
    ('a.bin', lo_from_bytea(0, decode(repeat('00ff7f80', 50000), 'hex'))),
    ('b.txt', lo_from_bytea(0, convert_to('large object ✓', 'UTF8')));

CREATE TABLE events (id bigint, at date NOT NULL, payload text) PARTITION BY RANGE (at);
CREATE TABLE events_2025 PARTITION OF events FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE events_2026 PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
INSERT INTO events SELECT g, DATE '2025-06-01' + (g % 400), 'p' || g FROM generate_series(1, 2000) g;

CREATE TABLE reporting.summary (k text PRIMARY KEY, v bigint);
INSERT INTO reporting.summary VALUES ('notes', 5002);

CREATE VIEW recent_notes AS SELECT id, body FROM notes ORDER BY id DESC LIMIT 10;
CREATE MATERIALIZED VIEW note_counts AS SELECT count(*) AS n FROM notes;

CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.created := now();
    RETURN NEW;
END
$$;
CREATE TRIGGER notes_touch BEFORE UPDATE ON notes FOR EACH ROW EXECUTE FUNCTION touch();

CREATE FUNCTION ensure_audit_log() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
CREATE TABLE IF NOT EXISTS audit_log (id int);
END
$$;

GRANT SELECT ON notes TO app_reader;
