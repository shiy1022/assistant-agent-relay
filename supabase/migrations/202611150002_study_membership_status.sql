begin;
alter table public.study_members add column enabled boolean not null default true;
alter policy study_member_read on public.study_members
    using (enabled and user_id = (select auth.uid()));
commit;
