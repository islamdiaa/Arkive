-- MariaDB / MySQL fixture (run as root). The configured database (MYSQL_DATABASE
-- / MARIADB_DATABASE = appdb) gets utf8mb4 text, binary BLOBs, JSON, a latin1
-- table, a view, a trigger, a stored procedure, a function and an event. A
-- second application database (photos) lives in the same server, as on a
-- shared MariaDB container.
USE appdb;

CREATE TABLE files (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
    data LONGBLOB,
    meta JSON,
    created DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE file_log (id INT AUTO_INCREMENT PRIMARY KEY, file_id INT, action VARCHAR(20)) ENGINE=InnoDB;

CREATE TRIGGER files_ai AFTER INSERT ON files FOR EACH ROW
    INSERT INTO file_log (file_id, action) VALUES (NEW.id, 'insert');

INSERT INTO files (name, data, meta) VALUES
    ('emoji 🎉🚀 CJK 漢字 RTL עברית', UNHEX('00FF7F80DEADBEEF005C27'), '{"a": 1, "b": [true, null]}'),
    ('quote '' backslash \\ tab \t end', NULL, NULL);
INSERT INTO files (name)
    WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 900)
    SELECT CONCAT('row ', a.n, '-', b.n) FROM seq a CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3) b;

CREATE TABLE legacy (s VARCHAR(50)) CHARACTER SET latin1;
INSERT INTO legacy VALUES ('café'), ('naïve');

CREATE VIEW files_with_data AS SELECT id, name FROM files WHERE data IS NOT NULL;

DELIMITER //
CREATE PROCEDURE count_files (OUT n INT)
BEGIN
    SELECT COUNT(*) INTO n FROM files;
END//
CREATE FUNCTION file_label (i INT) RETURNS VARCHAR(300) CHARSET utf8mb4 DETERMINISTIC READS SQL DATA
    RETURN (SELECT CONCAT('#', id, ' ', name) FROM files WHERE id = i)//
DELIMITER ;

CREATE EVENT purge_file_log ON SCHEDULE EVERY 1 DAY DO DELETE FROM file_log WHERE id < 0;

CREATE DATABASE photos CHARACTER SET utf8mb4;
USE photos;
CREATE TABLE albums (id INT PRIMARY KEY, title VARCHAR(100)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO albums VALUES (1, 'Summer ☀️'), (2, 'Winter ❄️');
CREATE USER 'photos'@'%' IDENTIFIED BY 'photos-pass';
GRANT ALL ON photos.* TO 'photos'@'%';
