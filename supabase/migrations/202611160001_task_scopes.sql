begin;
alter table public.study_results drop constraint study_results_check;
alter table public.study_results add constraint study_results_check
    check (
        (regexp_replace(path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') ~ '^(session|progress|scenario/(card|interview)|day[12]/(comparison|submission)|day1/what-you-would-do|day[12]/[a-z][a-z0-9_-]{0,31}/(after-agent|chat-notes)|day2/[a-z][a-z0-9_-]{0,31}/learning-check)\.json$' and jsonb_typeof(content) = 'object')
        or (regexp_replace(path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') ~ '^(scenario/interview\.md|day[12]/[a-z][a-z0-9_-]{0,31}/conversation\.(md|jsonl))$' and jsonb_typeof(content) = 'string')
        or (regexp_replace(path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') = 'scenario/changes.jsonl' and jsonb_typeof(content) = 'array')
        or (regexp_replace(path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') ~ '^audio/[A-Za-z0-9_.-]{1,80}-[a-f0-9]{32}\.(webm|ogg|m4a|mp3|wav)$' and jsonb_typeof(content) = 'object')
    );
create or replace function public.study_save_result(participant text, file_path text, payload jsonb) returns void
    language sql security invoker set search_path = '' as $$
    insert into public.study_results (pid, path, content)
    values (participant, file_path, payload)
    on conflict (pid, path) do update set content =
        case when regexp_replace(excluded.path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') = 'scenario/changes.jsonl' then study_results.content || coalesce((
            select jsonb_agg(incoming.record order by incoming.ordinal)
            from jsonb_array_elements(excluded.content) with ordinality incoming(record, ordinal)
            where not exists (
                select 1 from jsonb_array_elements(study_results.content) existing(record)
                where existing.record ->> 'event_id' = incoming.record ->> 'event_id'
            )
        ), '[]'::jsonb) else excluded.content end
    where regexp_replace(study_results.path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') not in ('day1/submission.json', 'day2/submission.json');
$$;

create or replace function study_private.record_study_result() returns trigger
    language plpgsql security definer set search_path = '' as $$
begin
    if tg_op = 'UPDATE' and old.content is not distinct from new.content then
        return new;
    end if;
    if regexp_replace(new.path, '^tasks/[a-z][a-z0-9_-]{0,63}/', '') <> 'progress.json' then
        insert into public.study_result_history (pid, path, before_value, after_value)
        values (new.pid, new.path, case when tg_op = 'UPDATE' then old.content end, new.content);
    end if;
    return new;
end;
$$;

commit;
