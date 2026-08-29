from cogdoc.ha.dbapi_compat import BackendDBAPIConnection


def test_writable_cte_is_never_classified_as_a_read() -> None:
    assert BackendDBAPIConnection._write(
        "WITH changed AS (UPDATE rows SET value=1 RETURNING *) SELECT * FROM changed"
    )
    assert BackendDBAPIConnection._write("WITH rows AS (SELECT 1) SELECT * FROM rows")
    assert BackendDBAPIConnection._write("SELECT 1") is False
