begin;

create table public.study_members (
    user_id uuid primary key references auth.users(id) on delete cascade,
    pid text unique check (pid ~ '^P[0-9]{3,4}$'),
    role text not null check (role in ('participant', 'researcher')),
    check ((role = 'participant') = (pid is not null))
);

-- Keep participant IDs and results when an Auth account is removed.
create table public.study_results (
    pid text not null check (pid ~ '^P[0-9]{3,4}$'),
    path text not null,
    content jsonb not null,
    updated_at timestamptz not null default statement_timestamp(),
    primary key (pid, path),
    check (
        (path ~ '^(session|progress|scenario/(card|interview)|day[12]/(comparison|submission)|day1/what-you-would-do|day[12]/[a-z][a-z0-9_-]{0,31}/(after-agent|chat-notes)|day2/[a-z][a-z0-9_-]{0,31}/learning-check)\.json$' and jsonb_typeof(content) = 'object')
        or (path ~ '^(scenario/interview\.md|day[12]/[a-z][a-z0-9_-]{0,31}/conversation\.(md|jsonl))$' and jsonb_typeof(content) = 'string')
        or (path = 'scenario/changes.jsonl' and jsonb_typeof(content) = 'array')
        or (path ~ '^audio/[A-Za-z0-9_.-]{1,80}-[a-f0-9]{32}\.(webm|ogg|m4a|mp3|wav)$' and jsonb_typeof(content) = 'object')
    )
);

create table public.study_result_history (
    id bigint generated always as identity primary key,
    pid text not null,
    path text not null,
    at timestamptz not null default statement_timestamp(),
    before_value jsonb,
    after_value jsonb not null
);

alter table public.study_members enable row level security;
alter table public.study_results enable row level security;
alter table public.study_result_history enable row level security;
revoke all on public.study_members, public.study_results, public.study_result_history from public, anon, authenticated;
grant select on public.study_members, public.study_results, public.study_result_history to authenticated;
grant insert (pid, path, content), update (content) on public.study_results to authenticated;

create policy study_member_read on public.study_members for select to authenticated
    using (user_id = (select auth.uid()));
create policy study_result_read on public.study_results for select to authenticated
    using (exists (select 1 from public.study_members m where m.pid = study_results.pid or m.role = 'researcher'));
create policy study_result_insert on public.study_results for insert to authenticated
    with check (exists (select 1 from public.study_members m where m.pid = study_results.pid and m.role = 'participant'));
create policy study_result_update on public.study_results for update to authenticated
    using (exists (select 1 from public.study_members m where m.pid = study_results.pid and m.role = 'participant'))
    with check (exists (select 1 from public.study_members m where m.pid = study_results.pid and m.role = 'participant'));
create policy study_history_read on public.study_result_history for select to authenticated
    using (exists (select 1 from public.study_members m where m.role = 'researcher'));

create schema if not exists study_private;
revoke all on schema study_private from public, anon, authenticated;

create function study_private.record_study_result() returns trigger
    language plpgsql security definer set search_path = '' as $$
begin
    if tg_op = 'UPDATE' and old.content is not distinct from new.content then
        return new;
    end if;
    if new.path <> 'progress.json' then
        insert into public.study_result_history (pid, path, before_value, after_value)
        values (new.pid, new.path, case when tg_op = 'UPDATE' then old.content end, new.content);
    end if;
    return new;
end;
$$;
revoke all on function study_private.record_study_result() from public, anon, authenticated;

create function study_private.touch_study_result() returns trigger
    language plpgsql set search_path = '' as $$
begin
    new.updated_at := statement_timestamp();
    return new;
end;
$$;
revoke all on function study_private.touch_study_result() from public, anon, authenticated;
create trigger study_result_touch before update on public.study_results
    for each row when (old.content is distinct from new.content)
    execute function study_private.touch_study_result();

create trigger study_result_audit after insert or update on public.study_results
    for each row execute function study_private.record_study_result();

create function public.study_save_result(participant text, file_path text, payload jsonb) returns void
    language sql security invoker set search_path = '' as $$
    insert into public.study_results (pid, path, content)
    values (participant, file_path, payload)
    on conflict (pid, path) do update set content =
        case when excluded.path = 'scenario/changes.jsonl' then study_results.content || coalesce((
            select jsonb_agg(incoming.record order by incoming.ordinal)
            from jsonb_array_elements(excluded.content) with ordinality incoming(record, ordinal)
            where not exists (
                select 1 from jsonb_array_elements(study_results.content) existing(record)
                where existing.record ->> 'event_id' = incoming.record ->> 'event_id'
            )
        ), '[]'::jsonb) else excluded.content end
    where study_results.path not in ('day1/submission.json', 'day2/submission.json');
$$;
revoke all on function public.study_save_result(text, text, jsonb) from public, anon, authenticated;
grant execute on function public.study_save_result(text, text, jsonb) to authenticated;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('study-audio', 'study-audio', false, 20000000, array['audio/webm','audio/ogg','audio/mp4','audio/mpeg','audio/wav']);

create policy study_audio_insert on storage.objects for insert to authenticated
    with check (bucket_id = 'study-audio' and exists (
        select 1 from public.study_members m where m.pid = (storage.foldername(name))[1] and m.role = 'participant'
    ));
create policy study_audio_read on storage.objects for select to authenticated
    using (bucket_id = 'study-audio' and exists (
        select 1 from public.study_members m where m.pid = (storage.foldername(name))[1] or m.role = 'researcher'
    ));
commit;
