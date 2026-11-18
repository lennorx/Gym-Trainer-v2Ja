-- GymBro AI: conversații + cont opțional (email + parolă), tabele PROPRII cu prefixul gym_.
-- Nu atinge tabelele Profesor ETTI / Liceu (sessions, history, etti_*, liceu_*) și nici view-ul session_previews.
-- Aplicația folosește cheia SECRETĂ (sb_secret_...), deci rolurile publice nu primesc niciun drept.

create table if not exists public.gym_users (
    id            uuid primary key default gen_random_uuid(),
    email         text not null unique,          -- salvat mereu cu litere mici
    password_hash text not null,                 -- scrypt$n$r$p$salt_hex$hash_hex
    recovery_hash text,                          -- scrypt(cod de recuperare)
    display_name  text,
    created_at    double precision not null,
    last_login    double precision
);

create table if not exists public.gym_tokens (
    token_hash text primary key,                 -- sha256(token); tokenul brut stă doar în browser
    user_id    uuid not null references public.gym_users(id) on delete cascade,
    created_at double precision not null,
    expires_at double precision not null,
    last_used  double precision
);
create index if not exists idx_gym_tokens_user_id on public.gym_tokens(user_id);

create table if not exists public.gym_password_resets (
    user_id    uuid primary key references public.gym_users(id) on delete cascade,
    code_hash  text not null,                    -- sha256 al codului de 6 cifre trimis pe email
    expires_at double precision not null,        -- 15 minute
    attempts   integer not null default 0,       -- max 5 încercări
    created_at double precision not null
);

create table if not exists public.gym_sessions (
    session_id  text primary key,                -- UUID generat de aplicație
    user_id     uuid references public.gym_users(id) on delete set null,   -- null = conversație anonimă
    title       text,                            -- începutul primului mesaj (pentru lista de conversații)
    created_at  double precision not null,
    last_active double precision not null
);
create index if not exists idx_gym_sessions_user on public.gym_sessions(user_id, last_active desc);

create table if not exists public.gym_history (
    id         bigint generated always as identity primary key,
    session_id text not null references public.gym_sessions(session_id) on delete cascade,
    role       text not null check (role in ('user', 'assistant')),
    content    text not null,
    created_at double precision not null
);
create index if not exists idx_gym_history_session on public.gym_history(session_id, created_at);

alter table public.gym_users           enable row level security;
alter table public.gym_tokens          enable row level security;
alter table public.gym_password_resets enable row level security;
alter table public.gym_sessions        enable row level security;
alter table public.gym_history         enable row level security;
revoke all on public.gym_users, public.gym_tokens, public.gym_password_resets,
              public.gym_sessions, public.gym_history from anon, authenticated;

-- 18 nov. 2026: profilul utilizatorului (vârstă, nivel, obiectiv, zile, echipament, limitări)
alter table public.gym_users    add column if not exists profile jsonb;   -- cu cont
alter table public.gym_sessions add column if not exists profile jsonb;   -- fără cont (pe conversație)

-- ROLLBACK:
-- drop table public.gym_history; drop table public.gym_sessions; drop table public.gym_password_resets;
-- drop table public.gym_tokens; drop table public.gym_users;
