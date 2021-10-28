from datetime import datetime
from typing import Any, Dict, List, Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, Extra, Field
from sqlalchemy import text


class BaseWAL(BaseModel):
    table: str
    schema_: str = Field(..., alias="schema")
    commit_timestamp: datetime

    class Config:
        extra = Extra.forbid


class Column(BaseModel):
    name: str
    type: str


ColValDict = Dict[str, Any]
Columns = List[Column]


class DeleteWAL(BaseWAL):
    type: Literal["DELETE"]
    columns: Columns
    old_record: ColValDict


class TruncateWAL(BaseWAL):
    type: Literal["TRUNCATE"]
    columns: Columns


class InsertWAL(BaseWAL):
    type: Literal["INSERT"]
    columns: Columns
    record: ColValDict


class UpdateWAL(BaseWAL):
    type: Literal["UPDATE"]
    record: ColValDict
    columns: Columns
    old_record: ColValDict


QUERY = text(
    """
with pub as (
    select
        pp.pubname pub_name,
        bool_or(puballtables) pub_all_tables,
        (
            select
                string_agg(act.name_, ',') actions
            from
                unnest(array[
                    case when bool_or(pubinsert) then 'insert' else null end,
                    case when bool_or(pubupdate) then 'update' else null end,
                    case when bool_or(pubdelete) then 'delete' else null end,
                    case when bool_or(pubtruncate) then 'truncate' else null end
                ]) act(name_)
        ) w2j_actions,
        string_agg(cdc.quote_wal2json(prrelid::regclass), ',') w2j_add_tables
    from
        pg_publication pp
        left join pg_publication_rel ppr
            on pp.oid = ppr.prpubid
    where
        pp.pubname = 'supabase_realtime'
    group by
        pp.pubname
    limit 1
)

select
    w2j.data::jsonb raw,
    xyz.wal,
    xyz.is_rls_enabled,
    xyz.users,
    xyz.errors
from
    pub,
    lateral (
        select
            *
        from
            pg_logical_slot_get_changes(
                'realtime', null, null,
                'include-pk', '1',
                'include-transaction', 'false',
                'include-timestamp', 'true',
                'write-in-chunks', 'true',
                'format-version', '2',
                'actions', coalesce(pub.w2j_actions, ''),
                'add-tables', coalesce(pub.w2j_add_tables, '')
            )
    ) w2j,
    lateral (
        select
            x.wal,
            x.is_rls_enabled,
            x.users,
            x.errors
        from
            cdc.apply_rls(w2j.data::jsonb) x(wal, is_rls_enabled, users, errors)
    ) xyz
where
    pub.pub_all_tables
    or (pub.pub_all_tables is false and pub.w2j_add_tables is not null)
"""
)


def clear_wal(sess):
    data = sess.execute(
        "select * from pg_logical_slot_get_changes('realtime', null, null)"
    ).scalar()
    sess.commit()


def setup_note(sess):
    sess.execute(
        text(
            """
revoke select on public.note from authenticated;
grant select (id, user_id, body, arr_text, arr_int) on public.note to authenticated;
    """
        )
    )
    sess.commit()


def setup_note_rls(sess):
    sess.execute(
        text(
            """
-- Access policy so only the owning user_id may see each row
create policy rls_note_select
on public.note
to authenticated
using (auth.uid() = user_id);

alter table public.note enable row level security;
    """
        )
    )
    sess.commit()


def insert_users(sess, n=10):
    sess.execute(
        text(
            """
insert into auth.users(id)
select extensions.uuid_generate_v4() from generate_series(1,:n);
    """
        ),
        {"n": n},
    )
    sess.commit()


def insert_subscriptions(sess, filters: Dict[str, Any] = {}, n=1):
    sess.execute(
        text(
            """
insert into cdc.subscription(user_id, entity)
select id, 'public.note' from auth.users order by id limit :lim;
    """
        ),
        {"lim": n},
    )
    sess.commit()


def insert_notes(sess, body="take out the trash", n=1):
    sess.execute(
        text(
            """
insert into public.note(user_id, body)
select id, :body from auth.users order by id limit :n;
    """
        ),
        {"n": n, "body": body},
    )
    sess.commit()


def test_read_wal(sess):
    setup_note(sess)
    insert_users(sess)
    clear_wal(sess)
    insert_notes(sess, 1)
    raw, *_ = sess.execute(QUERY).one()
    assert raw["table"] == "note"


def test_check_wal2json_settings(sess):
    insert_users(sess)
    setup_note(sess)
    clear_wal(sess)
    insert_notes(sess, 1)
    sess.commit()
    raw, *_ = sess.execute(QUERY).one()
    assert raw["table"] == "note"
    # include-pk setting in wal2json output
    assert "pk" in raw


def test_read_wal_w_visible_to_no_rls(sess):
    setup_note(sess)
    insert_users(sess)
    insert_subscriptions(sess)
    clear_wal(sess)
    insert_notes(sess)
    _, wal, is_rls_enabled, users, _ = sess.execute(QUERY).one()
    InsertWAL.parse_obj(wal)
    assert not is_rls_enabled
    # visible_to includes subscribed user when no rls enabled
    assert len(users) == 1

    assert [x for x in wal["columns"] if x["name"] == "id"][0]["type"] == "int8"


def test_read_wal_w_visible_to_has_rls(sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)
    insert_subscriptions(sess, n=2)
    clear_wal(sess)
    insert_notes(sess, n=1)
    sess.commit()
    _, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    InsertWAL.parse_obj(wal)
    assert wal["record"]["id"] == 1
    assert wal["record"]["arr_text"] == ["one", "two"]
    assert wal["record"]["arr_int"] == [1, 2]
    assert [x for x in wal["columns"] if x["name"] == "arr_text"][0]["type"] == "_text"
    assert [x for x in wal["columns"] if x["name"] == "arr_int"][0]["type"] == "_int4"

    assert is_rls_enabled
    # 2 permitted users
    assert len(users) == 1
    # check user_id
    assert isinstance(users[0], UUID)
    # check the "dummy" column is not present in the columns due to
    # role secutiry on "authenticated" role
    columns_in_output = [x["name"] for x in wal["columns"]]
    for col in ["id", "user_id", "body"]:
        assert col in columns_in_output
    assert "dummy" not in columns_in_output


def test_wal_update(sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)
    insert_subscriptions(sess, n=2)
    insert_notes(sess, n=1, body="old body")
    clear_wal(sess)
    sess.execute("update public.note set body = 'new body'")
    sess.commit()
    raw, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    UpdateWAL.parse_obj(wal)
    assert wal["record"]["id"] == 1
    assert wal["record"]["body"] == "new body"

    assert wal["old_record"]["id"] == 1
    # Only the identity of the previous
    assert "old_body" not in wal["old_record"]

    assert is_rls_enabled
    # 2 permitted users
    assert len(users) == 1
    # check the "dummy" column is not present in the columns due to
    # role secutiry on "authenticated" role
    columns_in_output = [x["name"] for x in wal["columns"]]
    for col in ["id", "user_id", "body"]:
        assert col in columns_in_output
    assert "dummy" not in columns_in_output
    assert [x for x in wal["columns"] if x["name"] == "id"][0]["type"] == "int8"


def test_wal_update_changed_identity(sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)
    insert_subscriptions(sess, n=2)
    insert_notes(sess, n=1, body="some body")
    clear_wal(sess)
    sess.execute("update public.note set id = 99")
    sess.commit()
    raw, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    UpdateWAL.parse_obj(wal)
    assert wal["record"]["id"] == 99
    assert wal["record"]["body"] == "some body"
    assert wal["old_record"]["id"] == 1


def test_wal_truncate(sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)
    insert_subscriptions(sess, n=2)
    insert_notes(sess, n=1)
    clear_wal(sess)
    sess.execute("truncate table public.note;")
    sess.commit()
    raw, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    TruncateWAL.parse_obj(wal)
    assert is_rls_enabled
    assert len(users) == 2


def test_wal_delete(sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)
    insert_subscriptions(sess, n=2)
    insert_notes(sess, n=1)
    clear_wal(sess)
    sess.execute("delete from public.note;")
    sess.commit()
    raw, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    DeleteWAL.parse_obj(wal)
    assert wal["old_record"]["id"] == 1
    assert is_rls_enabled
    assert len(users) == 2


@pytest.mark.parametrize(
    "filter_str,is_true",
    [
        # The WAL record body is "bbb"
        ("('body', 'eq', 'bbb')", True),
        ("('body', 'eq', 'aaaa')", False),
        ("('body', 'eq', 'cc')", False),
        ("('body', 'neq', 'bbb')", False),
        ("('body', 'neq', 'cat')", True),
        ("('body', 'lt', 'aa')", False),
        ("('body', 'lt', 'ccc')", True),
        ("('body', 'lt', 'bbb')", False),
        ("('body', 'lte', 'aa')", False),
        ("('body', 'lte', 'ccc')", True),
        ("('body', 'lte', 'bbb')", True),
        ("('body', 'gt', 'aa')", True),
        ("('body', 'gt', 'ccc')", False),
        ("('body', 'gt', 'bbb')", False),
        ("('body', 'gte', 'aa')", True),
        ("('body', 'gte', 'ccc')", False),
        ("('body', 'gte', 'bbb')", True),
    ],
)
def test_user_defined_eq_filter(filter_str, is_true, sess):
    insert_users(sess)
    setup_note(sess)
    setup_note_rls(sess)

    # Test does not match
    sess.execute(
        f"""
insert into cdc.subscription(user_id, entity, filters)
select
    id,
    'public.note',
    array[{filter_str}]::cdc.user_defined_filter[]
from
    auth.users order by id
limit 1;
    """
    )
    sess.commit()
    clear_wal(sess)

    insert_notes(sess, n=1, body="bbb")
    raw, wal, is_rls_enabled, users, errors = sess.execute(QUERY).one()
    assert len(users) == (1 if is_true else 0)


@pytest.mark.performance
@pytest.mark.parametrize("rls_on", [False, True])
def test_performance_on_n_recs_n_subscribed(sess, rls_on: bool):
    insert_users(sess, n=10000)
    setup_note(sess)
    if rls_on:
        setup_note_rls(sess)
    clear_wal(sess)

    if not rls_on:
        with open("perf.tsv", "w") as f:
            f.write("n_notes\tn_subscriptions\texec_time\trls_on\n")

    for n_subscriptions in [
        1,
        2,
        3,
        5,
        10,
        25,
        50,
        100,
        250,
        500,
        1000,
        2000,
        5000,
        10000,
    ]:
        insert_subscriptions(sess, n=n_subscriptions)
        for n_notes in [1, 2, 3, 4, 5]:
            clear_wal(sess)
            insert_notes(sess, n=n_notes)

            data = sess.execute(
                text(
                    """
            explain analyze
            select
                cdc.apply_rls(data::jsonb)
            from
                pg_logical_slot_peek_changes(
                    'realtime', null, null,
                    'include-pk', '1',
                    'include-transaction', 'false',
                    'format-version', '2',
                    'filter-tables', 'cdc.*,auth.*'
                )
            """
                )
            ).scalar()

            exec_time = float(
                data[data.find("time=") :].split(" ")[0].split("=")[1].split("..")[1]
            )

            with open("perf.tsv", "a") as f:
                f.write(f"{n_notes}\t{n_subscriptions}\t{exec_time}\t{rls_on}\n")

            # Confirm that the data is correct
            data = sess.execute(QUERY).all()
            assert len(data) == n_notes

            # Accumulate the visible_to person for each change and confirm it matches
            # the number of notes
            all_visible_to = []
            for (raw, wal, is_rls_enabled, users, errors) in data:
                for visible_to in users:
                    all_visible_to.append(visible_to)

            if rls_on:
                try:
                    assert (
                        len(all_visible_to)
                        == len(set(all_visible_to))
                        == min(n_notes, n_subscriptions)
                    )
                except:
                    print(
                        "n_notes",
                        n_notes,
                        "n_subscriptions",
                        n_subscriptions,
                        all_visible_to,
                    )
                    raise
            else:
                assert n_subscriptions == len(set(all_visible_to))

            sess.execute(
                text(
                    """
            truncate table public.note;
            """
                )
            )
            clear_wal(sess)

        sess.execute(
            text(
                """
            truncate table cdc.subscription;
            """
            )
        )
