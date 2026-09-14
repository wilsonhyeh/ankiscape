-- 0010_account_deletion_cleanup.sql - Transactional account-deletion cleanup.
-- Append-only migration: never edit 0001-0009 as a rollout strategy.
--
-- Deleting an Auth user cascades to public.players (FK on delete cascade) and
-- from there to game_operations, review_claims and game_state (its
-- achievements/inventory live inside that row). Two tables key on the
-- game/user WITHOUT a foreign key, so they are cleaned here, in the SAME
-- transaction as the Auth deletion, by a narrowly scoped BEFORE DELETE
-- trigger:
--   * public.game_checkpoints - keyed by game_uuid only.
--   * public.moderation_audit - keyed by target_user_id only.
--
-- Scope is exact: one game's checkpoints and one user's audit rows. No broad
-- table deletes and no historical orphan sweep. A failed Auth deletion or a
-- fixture_registry RESTRICT rolls back the trigger work with it.
--
-- The scoring RPCs lock the owning players row before writing state
-- (`... from public.players where game_uuid = ... for update`), and this
-- trigger fires while that same row delete lock is held. A concurrent scoring
-- transaction therefore serializes with the delete and cannot leave a new
-- orphan checkpoint behind.

create or replace function public._account_delete_cleanup()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if OLD.game_uuid is not null then
    delete from public.game_checkpoints where game_uuid = OLD.game_uuid;
  end if;
  delete from public.moderation_audit where target_user_id = OLD.user_id;
  return OLD;
end;
$$;

revoke all on function public._account_delete_cleanup()
  from public, anon, authenticated;

drop trigger if exists ankiscape_account_delete_cleanup on public.players;
create trigger ankiscape_account_delete_cleanup
  before delete on public.players
  for each row execute function public._account_delete_cleanup();
