CREATE TABLE media_capacity (
	id INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE media_jobs (
	id VARCHAR(32) NOT NULL, 
	owner VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	stage VARCHAR(30) NOT NULL, 
	created FLOAT NOT NULL, 
	started FLOAT, 
	expires FLOAT, 
	publish_after FLOAT NOT NULL, 
	lease_until FLOAT, 
	run_token VARCHAR(32), 
	attempts INTEGER NOT NULL, 
	cancel_requested BOOLEAN NOT NULL, 
	error_code VARCHAR(50), 
	error TEXT, 
	object_key TEXT, 
	filename VARCHAR(100), 
	size INTEGER, 
	stats JSON, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_media_jobs_expires ON media_jobs (expires);

CREATE INDEX ix_media_jobs_lease_until ON media_jobs (lease_until);

CREATE INDEX ix_media_jobs_owner ON media_jobs (owner);

CREATE INDEX ix_media_jobs_publish_after ON media_jobs (publish_after);

CREATE INDEX ix_media_jobs_status ON media_jobs (status);
