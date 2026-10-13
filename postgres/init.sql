-- CDC source table (S7 mirrors app_users to ClickHouse via Conduit).
-- Mounted into /docker-entrypoint-initdb.d, so it runs once on an empty
-- volume; the INSERT guard keeps it safe on any re-run.

CREATE TABLE IF NOT EXISTS app_users (
    id         bigserial PRIMARY KEY,
    email      text NOT NULL,
    plan       text NOT NULL,
    status     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO app_users (email, plan, status, created_at, updated_at)
SELECT email, plan, status, created_at, updated_at
FROM (VALUES
    ('alice.mercer@example.com',    'teams', 'active',    timestamptz '2025-01-14 09:22:00+00', timestamptz '2026-09-30 16:05:00+00'),
    ('bruno.santos@example.com',    'pro',   'active',    timestamptz '2025-02-03 11:41:00+00', timestamptz '2026-10-01 08:12:00+00'),
    ('chiara.rossi@example.com',    'free',  'trial',     timestamptz '2026-09-28 14:03:00+00', timestamptz '2026-09-28 14:03:00+00'),
    ('dev.patel@example.com',       'pro',   'active',    timestamptz '2025-03-19 10:15:00+00', timestamptz '2026-08-21 19:44:00+00'),
    ('elena.kovac@example.com',     'free',  'active',    timestamptz '2025-05-07 08:30:00+00', timestamptz '2026-06-11 12:27:00+00'),
    ('felix.wagner@example.com',    'teams', 'active',    timestamptz '2025-01-29 16:58:00+00', timestamptz '2026-10-05 07:19:00+00'),
    ('grace.liu@example.com',       'pro',   'churned',   timestamptz '2025-04-02 13:47:00+00', timestamptz '2026-03-08 18:02:00+00'),
    ('henrik.olsen@example.com',    'free',  'churned',   timestamptz '2025-06-21 09:09:00+00', timestamptz '2025-12-30 11:55:00+00'),
    ('imani.brooks@example.com',    'teams', 'active',    timestamptz '2025-02-11 15:24:00+00', timestamptz '2026-09-17 10:33:00+00'),
    ('jonas.weber@example.com',     'pro',   'active',    timestamptz '2025-08-14 12:12:00+00', timestamptz '2026-10-02 20:41:00+00'),
    ('kavya.nair@example.com',      'free',  'active',    timestamptz '2025-09-05 17:36:00+00', timestamptz '2026-07-24 09:58:00+00'),
    ('liam.odonnell@example.com',   'pro',   'cancelled', timestamptz '2025-03-30 10:50:00+00', timestamptz '2026-04-19 14:16:00+00'),
    ('meera.shah@example.com',      'teams', 'active',    timestamptz '2025-01-06 08:05:00+00', timestamptz '2026-10-08 13:29:00+00'),
    ('noah.kim@example.com',        'free',  'trial',     timestamptz '2026-10-06 11:18:00+00', timestamptz '2026-10-06 11:18:00+00'),
    ('olga.petrova@example.com',    'pro',   'active',    timestamptz '2025-07-16 14:44:00+00', timestamptz '2026-09-01 17:51:00+00'),
    ('pedro.alvarez@example.com',   'free',  'churned',   timestamptz '2025-05-25 16:20:00+00', timestamptz '2026-01-11 10:07:00+00'),
    ('quinn.harper@example.com',    'teams', 'active',    timestamptz '2025-02-27 09:33:00+00', timestamptz '2026-09-12 15:46:00+00'),
    ('rosa.delgado@example.com',    'pro',   'active',    timestamptz '2025-10-12 13:08:00+00', timestamptz '2026-08-30 08:54:00+00'),
    ('stefan.lindqvist@example.com','free',  'active',    timestamptz '2025-11-08 18:41:00+00', timestamptz '2026-06-02 12:22:00+00'),
    ('tara.mensah@example.com',     'pro',   'active',    timestamptz '2025-12-01 10:26:00+00', timestamptz '2026-10-10 09:14:00+00')
) AS seed(email, plan, status, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM app_users);
