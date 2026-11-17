-- Captcher's database: one SQLite file (CAPTCHER_HOME/captcher.db by default).
--
-- Applied by db.py when the file is new (PRAGMA user_version = 0). Later
-- changes go in numbered upgrade steps in db.py, never by editing this file in
-- place for an existing database.
--
-- Conventions, all enforced by db.py rather than by SQLite's loose typing:
--   - ids are uuid strings. The DEFAULT below builds a v4 uuid in pure SQL so
--     inserts that omit the id work exactly as they did on Postgres.
--   - timestamps are UTC text, 'YYYY-MM-DDTHH:MM:SS.mmmZ' — one fixed width,
--     so they sort and compare as text. db.now() writes the same shape.
--   - JSON columns hold JSON text; db.py decodes them by column name.
--   - booleans are 0/1.

-- One row per owner. A self-hosted server has exactly one (auth.LOCAL_OWNER_ID);
-- the column stays on every owned table so a multi-user mode remains possible.
create table members (
  id                       text primary key,
  email                    text not null,
  created_at               text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  active_brand_profile_id  text references brand_profiles (id) on delete set null
);

create table capture_tokens (
  id            text primary key,
  member_id     text not null references members (id) on delete cascade,
  token_hash    text not null unique,
  label         text not null default 'Browser extension',
  created_at    text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_used_at  text
);
create index capture_tokens_member_idx on capture_tokens (member_id);

create table walkthroughs (
  id             text primary key default (
                   lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                   substr(lower(hex(randomblob(2))), 2) || '-' ||
                   substr('89ab', 1 + (abs(random()) % 4), 1) ||
                   substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
  owner_id       text not null references members (id) on delete cascade,
  -- Derived from the title at creation, de-duplicated within the owner. Free to
  -- change: nothing addresses a walkthrough by it.
  slug           text not null check (slug glob '[a-z0-9]*' and slug not glob '*[^a-z0-9-]*'
                                      and slug not like '-%' and slug not like '%-'
                                      and slug not like '%--%'),
  title          text not null,
  definition     text not null,
  -- Written once, before the first authoring pass, and never overwritten.
  draft          text,
  -- From the capture manifest. The catalog's FIFO sort key.
  recorded_at    text,
  -- Presence is what moves a walkthrough out of the authoring queue.
  authored_at    text,
  render_check   text,
  verify_check   text,
  embed          text not null default '{"enabled": false}',
  created_at     text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at     text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  unique (owner_id, slug)
);
create index walkthroughs_owner_idx on walkthroughs (owner_id);

create trigger walkthroughs_touch after update on walkthroughs
  for each row when new.updated_at = old.updated_at
  begin
    update walkthroughs set updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     where id = new.id;
  end;

create table walkthrough_history (
  id             integer primary key autoincrement,
  walkthrough_id text not null references walkthroughs (id) on delete cascade,
  -- YYYYmmdd-HHMMSS, matching the restore route's STAMP_RE.
  stamp          text not null,
  definition     text not null,
  -- Prefix (under captures/) holding the pre-edit copies of any capture pages
  -- this save rewrote, or null when the save touched only the definition.
  pages_prefix   text,
  created_at     text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  unique (walkthrough_id, stamp)
);
create index walkthrough_history_lookup_idx on walkthrough_history (walkthrough_id, created_at);

create table collections (
  id           text primary key default (
                 lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                 substr(lower(hex(randomblob(2))), 2) || '-' ||
                 substr('89ab', 1 + (abs(random()) % 4), 1) ||
                 substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
  owner_id     text not null references members (id) on delete cascade,
  -- Slugified once at create and never recomputed on rename, so a link to a
  -- collection survives it being retitled.
  slug         text not null,
  name         text not null check (length(name) between 1 and 80),
  description  text not null default '' check (length(description) <= 200),
  created_at   text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at   text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  unique (owner_id, slug)
);

create table collection_items (
  collection_id  text    not null references collections (id) on delete cascade,
  walkthrough_id text    not null references walkthroughs (id) on delete cascade,
  position       integer not null,
  primary key (collection_id, walkthrough_id)
);
create index collection_items_order_idx on collection_items (collection_id, position);

create table brand_profiles (
  id                text primary key default (
                      lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                      substr(lower(hex(randomblob(2))), 2) || '-' ||
                      substr('89ab', 1 + (abs(random()) % 4), 1) ||
                      substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
  owner_id          text not null references members (id) on delete cascade,
  slug              text not null,
  brand_name        text not null check (length(brand_name) between 1 and 80),
  tagline           text not null default '',
  logo_light_url    text,
  logo_dark_url     text,
  favicon_url       text,
  -- The ten-name contract: primary, primary_hover, accent, background, surface,
  -- text, muted, border, success, error.
  colors            text not null,
  -- heading, body, and a webfont_url ALWAYS derived server-side from the two
  -- family names — never accepted from the client.
  fonts             text not null,
  show_attribution  integer not null default 1,
  created_at        text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at        text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  unique (owner_id, slug)
);

create table walkthrough_logs (
  id             integer primary key autoincrement,
  walkthrough_id text not null references walkthroughs (id) on delete cascade,
  -- Who caused the entry. Null once that member is gone.
  member_id      text references members (id) on delete set null,
  kind           text not null check (kind in ('capture', 'ai_edit', 'author')),
  entry          text not null,
  created_at     text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index walkthrough_logs_lookup_idx on walkthrough_logs (walkthrough_id, kind, created_at);

-- One row per billed Claude API attempt. No foreign keys on purpose: spend
-- outlives the walkthrough and member it was spent on.
create table ai_usage (
  id                    integer primary key autoincrement,
  created_at            text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  -- One API request; attempt numbers its billed attempts from 0.
  call_id               text not null,
  attempt               integer not null default 0,
  -- What the server was doing: author, ai-edit, ai-assist, render-check,
  -- verify-replay, brand-import, or other (a CLI run, a script).
  operation             text not null,
  -- The jobs.Job the call ran under, when there was one.
  job_id                text,
  walkthrough_id        text,
  walkthrough_title     text,
  member_id             text,
  -- The model that ran this attempt, and the one the server asked for.
  model                 text not null,
  requested_model       text,
  input_tokens          integer not null default 0,
  output_tokens         integer not null default 0,
  cache_read_tokens     integer not null default 0,
  cache_write_5m_tokens integer not null default 0,
  cache_write_1h_tokens integer not null default 0,
  stop_reason           text,
  message_id            text
);
create index ai_usage_created_idx     on ai_usage (created_at);
create index ai_usage_walkthrough_idx on ai_usage (walkthrough_id, created_at);
create index ai_usage_job_idx         on ai_usage (job_id);
