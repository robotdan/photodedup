import sqlite3

from photodedup.photos_write import LibraryIndex


def make_library(tmp_path):
    db = tmp_path / "lib" / "database"
    db.mkdir(parents=True)
    c = sqlite3.connect(db / "Photos.sqlite")
    c.executescript("""
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZTRASHEDSTATE INTEGER);
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER, ZTRASHEDSTATE INTEGER);
        CREATE TABLE Z_3MEMORIESBEINGCURATEDASSETS (Z_3ASSETS INTEGER, Z_40MEMORIES INTEGER);
        CREATE TABLE Z_34ASSETS (Z_34ALBUMS INTEGER, Z_3ASSETS INTEGER);
        INSERT INTO ZASSET VALUES (1, 'a', 0), (2, 'b', 1);
        INSERT INTO ZGENERICALBUM VALUES (10, 'Trip', 2, 0), (11, 'Gone', 2, 1), (12, 'Smart', 1507, 0);
        INSERT INTO Z_34ASSETS VALUES (10, 1), (11, 1), (12, 1);
    """)
    c.commit()
    return tmp_path / "lib"


def test_index_reads_existence_and_user_albums(tmp_path):
    ix = LibraryIndex(make_library(tmp_path))
    assert ix.exists("a") and not ix.exists("b") and not ix.exists("zz")
    assert ix.albums("a") == ("Trip",) and ix.albums("b") == ()
