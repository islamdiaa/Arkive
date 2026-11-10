-- Cluster-level fixture for the restore round trip (run as the bootstrap
-- superuser, connected to the maintenance database).
--
-- shopdb is owned by a non-superuser application role and read by a second
-- role: after a restore the application must still own (and be able to write)
-- its tables, and the reader must still be able to read them.
CREATE ROLE app_owner LOGIN PASSWORD 'owner-pass';
CREATE ROLE app_reader LOGIN PASSWORD 'reader-pass';
ALTER ROLE app_reader SET statement_timeout = '15s';
CREATE DATABASE shopdb OWNER app_owner;
CREATE DATABASE "Mixed Case DB";
CREATE DATABASE latin1db ENCODING 'LATIN1' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
