from urllib.parse import quote

from sqlalchemy.engine import make_url

from aivo.db.session import normalize_db_url


def test_digitalocean_connection_string_is_accepted_as_pasted():
    out = normalize_db_url("postgresql://doadmin:pw@db-aivo.example.com:25060/defaultdb?sslmode=require")
    u = make_url(out)
    assert u.drivername == "postgresql+asyncpg"
    assert dict(u.query) == {"ssl": "require"}
    assert (u.username, u.password, u.host, u.port, u.database) == (
        "doadmin",
        "pw",
        "db-aivo.example.com",
        25060,
        "defaultdb",
    )


def test_asyncpg_form_unchanged_and_explicit_ssl_wins():
    url = "postgresql+asyncpg://u:p@h:5432/d?ssl=require"
    assert normalize_db_url(url) == url
    u = make_url(normalize_db_url("postgresql+asyncpg://u:p@h/d?ssl=verify-full&sslmode=disable"))
    assert dict(u.query) == {"ssl": "verify-full"}


def test_postgres_alias_and_other_params_kept():
    u = make_url(normalize_db_url("postgres://u@h/d?sslmode=verify-full&application_name=aivo"))
    assert u.drivername == "postgresql+asyncpg"
    assert dict(u.query) == {"ssl": "verify-full", "application_name": "aivo"}


def test_password_with_special_characters_round_trips():
    pw = "p@ss:w/rd%20?#"
    out = normalize_db_url(f"postgresql://u:{quote(pw, safe='')}@h:25060/d?sslmode=require")
    assert make_url(out).password == pw


def test_no_query_untouched():
    assert (
        normalize_db_url("postgresql+asyncpg://aivo@localhost:5432/aivo")
        == "postgresql+asyncpg://aivo@localhost:5432/aivo"
    )
