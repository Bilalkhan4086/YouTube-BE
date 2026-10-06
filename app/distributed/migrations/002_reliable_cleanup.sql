ALTER TABLE media_jobs ADD COLUMN cleanup_after FLOAT NOT NULL DEFAULT 0;
ALTER TABLE media_jobs ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_media_jobs_cleanup_after ON media_jobs (cleanup_after);
CREATE TABLE media_admissions (
    id VARCHAR(32) PRIMARY KEY,
    owner VARCHAR(64) NOT NULL,
    expires FLOAT NOT NULL
);
CREATE INDEX ix_media_admissions_expires ON media_admissions (expires);
