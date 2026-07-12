from chanlun.persistence.db import _build_mysql_database_url


def test_mysql_url_preserves_reserved_characters_in_credentials():
    url = _build_mysql_database_url(
        user="name@example.com",
        password="p@ss:/%#word",
        host="db.internal",
        port=3306,
        database="chanlun",
    )

    assert url.username == "name@example.com"
    assert url.password == "p@ss:/%#word"
    assert url.host == "db.internal"
    assert url.port == 3306
    assert url.database == "chanlun"
    assert url.query["charset"] == "utf8mb4"
