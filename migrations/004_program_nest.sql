-- A picture (or PDF) of the cutting nest for a program. One per program; uploading again replaces it.
-- Kept in the database so it is backed up with everything else and never goes into the code repository.
create table program_nest (
    program_id    integer primary key references program(id) on delete cascade,
    filename      text not null,
    content_type  text not null,
    size_bytes    integer not null,
    original      bytea not null,           -- the file exactly as uploaded
    preview       bytea not null,           -- the picture, or page 1 of a PDF, sized for screens and the cover sheet
    preview_type  text not null,            -- image/png or image/jpeg
    width         integer not null,
    height        integer not null,
    pages         integer not null default 1,
    uploaded_by   text not null default '',
    uploaded_at   timestamptz not null default now()
);
