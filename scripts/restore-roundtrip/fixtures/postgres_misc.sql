-- "Mixed Case DB": a database name with spaces and capitals.
\connect "Mixed Case DB"
CREATE TABLE "Weird Table" (id int PRIMARY KEY, "Value" text);
INSERT INTO "Weird Table" SELECT g, 'v' || g FROM generate_series(1, 50) g;

-- latin1db: a non-UTF8 server encoding.
\connect latin1db
CREATE TABLE words (w text);
INSERT INTO words VALUES ('café'), ('naïve'), ('Ærøskøbing');
