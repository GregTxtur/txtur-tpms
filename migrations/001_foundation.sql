-- TPMS build 1: item master, program list, inventory ledger.
-- Rule: on-hand is always SUM(inventory_txn.qty_sheets). Never store a balance.

create table plywood_type (
    id      serial primary key,
    name    text not null unique,
    active  boolean not null default true
);

create table supplier (
    id      serial primary key,
    name    text not null unique,
    notes   text not null default '',
    active  boolean not null default true
);

-- Item master: one row per stocked plywood. Type + thickness + sheet size say what it is;
-- grade is only filled in where more than one face grade of the same sheet is stocked.
create table item (
    id               serial primary key,
    code             text not null unique,
    type_id          integer not null references plywood_type(id),
    thickness        text not null,
    sheet_size       text not null,
    grade            text not null default '',
    supplier_id      integer references supplier(id),
    sheets_per_pack  integer check (sheets_per_pack > 0),   -- sheets in one pack as delivered; stock is always counted in sheets
    cost_per_sheet   numeric(10,2) check (cost_per_sheet >= 0),
    lead_time_days   integer check (lead_time_days >= 0),
    reorder_point    integer check (reorder_point >= 0),
    notes            text not null default '',
    active           boolean not null default true,
    created_at       timestamptz not null default now(),
    unique (type_id, thickness, sheet_size, grade)
);

create table machine (
    id      serial primary key,
    code    text not null unique,
    name    text not null,
    active  boolean not null default true,
    sort    integer not null default 0
);

insert into machine (code, name, active, sort) values
    ('H1', 'Heian 1', true, 1),
    ('H2', 'Heian 2', true, 2),
    ('S1', 'Shoda 1', true, 3),
    ('S2', 'Shoda 2', false, 4);

-- CNC program list. Program numbers are NOT unique in the legacy list (52 numbers were used twice),
-- so the key is the row id and duplicates are flagged on screen for the programmer to sort out.
create table program (
    id                serial primary key,
    number            text not null,
    name              text not null default '',
    description       text not null default '',
    machine_code      text not null default '',
    units_per_sheet   numeric(10,4) check (units_per_sheet > 0),
    units_text        text not null default '',
    sheets_per_unit   numeric(10,4) check (sheets_per_unit > 0),
    parts_on_sheet    integer,
    parts_in_unit     integer,
    multi_sheet       text not null default '',
    addl_machine      text not null default '',
    assembly          text not null default '',
    sheet_size        text not null default '',
    thickness         text not null default '',
    material_text     text not null default '',
    item_id           integer references item(id),
    status            text not null default 'unknown'
                      check (status in ('approved', 'sample', 'retired', 'unknown')),
    program_date      date,
    notes             text not null default '',
    number_flag       boolean not null default false,
    source_row        integer,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    unique (number, name)
);
create index program_number_idx on program (number);
create index program_item_idx on program (item_id);

-- Standard router time per sheet, per machine.
create table program_time (
    program_id         integer not null references program(id) on delete cascade,
    machine_id         integer not null references machine(id),
    seconds_per_sheet  integer not null check (seconds_per_sheet > 0),
    source_text        text not null default '',
    primary key (program_id, machine_id)
);

-- Every movement of sheets. Receipts and returns are positive, issues negative,
-- adjustments and opening balances either way.
create table inventory_txn (
    id           bigserial primary key,
    item_id      integer not null references item(id),
    txn_type     text not null check (txn_type in ('opening', 'receipt', 'issue', 'adjust', 'return')),
    qty_sheets   integer not null check (qty_sheets <> 0),
    reason       text not null default '',
    reference    text not null default '',
    job_number   text not null default '',
    shop_order   text not null default '',
    team_member  text not null default '',
    note         text not null default '',
    entered_by   text not null default '',
    created_at   timestamptz not null default now()
);
create index inventory_txn_item_idx on inventory_txn (item_id, created_at);
create index inventory_txn_created_idx on inventory_txn (created_at);
create index inventory_txn_so_idx on inventory_txn (shop_order) where shop_order <> '';
create index inventory_txn_job_idx on inventory_txn (job_number) where job_number <> '';
