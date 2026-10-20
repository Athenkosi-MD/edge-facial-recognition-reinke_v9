-- ============================================================================
-- Driver enrollment states: UNIDENTIFIED -> AUTO_ENROLLED -> FULLY_ENROLLED
-- (+ REJECTED, MERGED, EXPIRED). Safe to run more than once.
-- The web app runs the same statements on start-up; this file is for running
-- it by hand / in a release script.
-- ============================================================================

ALTER TABLE drivers_reinke_logistics
    ADD COLUMN IF NOT EXISTS enrollment_state        VARCHAR(20) NOT NULL DEFAULT 'FULLY_ENROLLED',
    ADD COLUMN IF NOT EXISTS trusttrack_id           VARCHAR(64),
    ADD COLUMN IF NOT EXISTS trusttrack_sync_pending BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS first_seen_at           TIMESTAMP,
    ADD COLUMN IF NOT EXISTS first_seen_vehicle      VARCHAR(100),
    ADD COLUMN IF NOT EXISTS last_seen_at            TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_seen_vehicle       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS sighting_count          INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS confirmed_by            VARCHAR(100),
    ADD COLUMN IF NOT EXISTS confirmed_at            TIMESTAMP,
    ADD COLUMN IF NOT EXISTS merged_into             VARCHAR(30),
    ADD COLUMN IF NOT EXISTS state_note              TEXT,
    ADD COLUMN IF NOT EXISTS lookalikes              TEXT,   -- JSON, written by the recognizer
    ADD COLUMN IF NOT EXISTS confirmed_by_client     VARCHAR(150),  -- client contact who vouched for the driver
    ADD COLUMN IF NOT EXISTS name_pending            BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_drivers_rl_enrollment_state ON drivers_reinke_logistics (enrollment_state);

-- Who moved which driver between states, when and why (audit trail for OCC).
CREATE TABLE IF NOT EXISTS driver_state_history_reinke_logistics (
    id          SERIAL PRIMARY KEY,
    driver_id   VARCHAR(30) NOT NULL,
    from_state  VARCHAR(20),
    to_state    VARCHAR(20) NOT NULL,
    changed_by  VARCHAR(100),
    note        TEXT,
    changed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_dsh_rl_driver ON driver_state_history_reinke_logistics (driver_id);

-- Work queue: the web app (at the client) records OCC decisions in the DB and
-- queues the side effects here. The recognizer scripts own the R2 face
-- templates and Trust Track, and process PENDING rows on every poll.
CREATE TABLE IF NOT EXISTS driver_lifecycle_actions_reinke_logistics (
    id            SERIAL PRIMARY KEY,
    driver_id     VARCHAR(30) NOT NULL,
    action        VARCHAR(20) NOT NULL,   -- CONFIRM|REJECT|MERGE|ENROLL|PROMOTE|SYNC_TT|DELETE
    payload       TEXT,                   -- JSON
    requested_by  VARCHAR(100),
    requested_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status        VARCHAR(12) NOT NULL DEFAULT 'PENDING',   -- PENDING|RUNNING|DONE|FAILED
    attempts      INTEGER NOT NULL DEFAULT 0,
    processed_at  TIMESTAMP,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_dla_rl_status ON driver_lifecycle_actions_reinke_logistics (status);
CREATE INDEX IF NOT EXISTS ix_dla_rl_driver ON driver_lifecycle_actions_reinke_logistics (driver_id);

-- Alert throttle: one row per (driver, vehicle). "Keep alerting until sorted"
-- = re-alert every N minutes per driver per vehicle, not once per snapshot.
CREATE TABLE IF NOT EXISTS driver_alert_throttle_reinke_logistics (
    driver_id     VARCHAR(30)  NOT NULL,
    vehicle       VARCHAR(100) NOT NULL,
    kind VARCHAR(20) NOT NULL DEFAULT 'unidentified',
    last_alert_at TIMESTAMP    NOT NULL,
    alert_count   INTEGER      NOT NULL DEFAULT 1,
    PRIMARY KEY (driver_id, vehicle, kind)
);

-- Tables created first by the web app (SQLAlchemy) have no database-side
-- defaults; add them so inserts from the recognizer never hit NOT NULL errors.
ALTER TABLE driver_state_history_reinke_logistics     ALTER COLUMN changed_at   SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE driver_lifecycle_actions_reinke_logistics ALTER COLUMN requested_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE driver_lifecycle_actions_reinke_logistics ALTER COLUMN status       SET DEFAULT 'PENDING';
ALTER TABLE driver_lifecycle_actions_reinke_logistics ALTER COLUMN attempts     SET DEFAULT 0;
ALTER TABLE driver_alert_throttle_reinke_logistics    ALTER COLUMN alert_count  SET DEFAULT 1;
ALTER TABLE driver_alert_throttle_reinke_logistics    ALTER COLUMN kind         SET DEFAULT 'unidentified';

-- Existing rows: everyone enrolled through the web app so far is FULLY_ENROLLED
-- (that is the column default, so nothing else to back-fill).
