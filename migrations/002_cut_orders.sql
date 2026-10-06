-- TPMS build 2: cut orders, team members, scan on / scan off, off order activities.

-- Plant-wide numbers a supervisor can change without a code change.
create table setting (
    key    text primary key,
    value  text not null
);
insert into setting (key, value) values
    ('changeover_minutes', '15'),     -- earned for changing from one program to the next
    ('mostly_cut_pct', '90'),         -- scan-on warns at or above this share cut
    ('day_end', '17:00'),             -- plant time; forgotten scans are closed back to this time
    ('auto_close_grace_hours', '4');  -- how long after day_end a forgotten scan is left open

create table team_member (
    id            serial primary key,
    name          text not null unique,
    pin_hash      text not null default '',
    failed_pins   integer not null default 0,
    locked_until  timestamptz,
    active        boolean not null default true,
    created_at    timestamptz not null default now()
);

-- One cut order = one program for one job. It is what gets printed and scanned.
-- Program is optional so a job can be filed before the program is known; without one it earns no minutes.
create table cut_order (
    id               serial primary key,
    token            text not null unique,          -- in the QR codes; never reused
    status           text not null default 'filed'
                     check (status in ('filed', 'queued', 'complete', 'cancelled')),
    shop_order       text not null default '',
    job_number       text not null default '',
    description      text not null default '',
    program_id       integer references program(id),
    item_id          integer references item(id),
    machine_id       integer references machine(id),
    qty_units        numeric(10,2) check (qty_units > 0),
    sheets_required  integer check (sheets_required > 0),
    due_date         date,
    notes            text not null default '',
    source           text not null default '',       -- 'workbook' for rows loaded from the legacy sheets
    created_by       text not null default '',
    created_at       timestamptz not null default now(),
    released_by      text not null default '',
    released_at      timestamptz,
    completed_at     timestamptz
);
create index cut_order_status_idx on cut_order (status);
create index cut_order_so_idx on cut_order (shop_order);

-- Off Order Activities: the codes on the scan poster. A code is the activity's token plus the router,
-- so adding an activity never breaks a poster already on the wall.
create table activity (
    id              serial primary key,
    name            text not null unique,
    token           text not null unique,
    earns           boolean not null default true,
    earned_minutes  integer not null default 0 check (earned_minutes >= 0),
    active          boolean not null default true,
    sort            integer not null default 0
);
insert into activity (name, token, earns, earned_minutes, sort) values
    ('Tool Change',         'a' || substr(md5(random()::text), 1, 9), true,  20, 1),
    ('Fly Cut Table',       'a' || substr(md5(random()::text), 1, 9), true,  45, 2),
    ('Clean Up Machine',    'a' || substr(md5(random()::text), 1, 9), true,   0, 3),
    ('Waiting on Material', 'a' || substr(md5(random()::text), 1, 9), false,  0, 4),
    ('Machine Down',        'a' || substr(md5(random()::text), 1, 9), false,  0, 5);

-- One team member on one cut order, or on one off order activity.
create table scan_session (
    id               bigserial primary key,
    team_member_id   integer not null references team_member(id),
    cut_order_id     integer references cut_order(id),
    activity_id      integer references activity(id),
    machine_id       integer references machine(id),
    started_at       timestamptz not null default now(),
    ended_at         timestamptz,
    sheets           integer check (sheets >= 0),       -- confirmed at scan off (cut orders only)
    fixed_minutes    integer not null default 0,        -- changeover credit, or the activity's standard, as of scan on
    closed_by        text not null default '' check (closed_by in ('', 'self', 'auto', 'supervisor')),
    needs_review     boolean not null default false,
    note             text not null default '',
    check ((cut_order_id is null) <> (activity_id is null)),
    check (ended_at is null or ended_at >= started_at)
);
create index scan_session_member_idx on scan_session (team_member_id, started_at);
create index scan_session_cut_order_idx on scan_session (cut_order_id);
create index scan_session_machine_idx on scan_session (machine_id, started_at);
create index scan_session_open_idx on scan_session (team_member_id) where ended_at is null;
-- A person can only be on a given cut order, or a given activity at a given router, once at a time.
create unique index scan_session_one_open_co on scan_session (team_member_id, cut_order_id)
    where ended_at is null and cut_order_id is not null;
create unique index scan_session_one_open_act on scan_session (team_member_id, activity_id, coalesce(machine_id, 0))
    where ended_at is null and activity_id is not null;

-- One tap of the sheet counter. A convenience for the team member and a record of real cycle times;
-- the count confirmed at scan off is the number of record.
create table sheet_tap (
    id          bigserial primary key,
    session_id  bigint not null references scan_session(id) on delete cascade,
    tapped_at   timestamptz not null default now()
);
create index sheet_tap_session_idx on sheet_tap (session_id);

-- Ties each cut-order deduction back to the scan that caused it.
alter table inventory_txn add column scan_session_id bigint references scan_session(id);
create index inventory_txn_session_idx on inventory_txn (scan_session_id) where scan_session_id is not null;
