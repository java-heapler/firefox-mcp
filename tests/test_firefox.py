import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from firefox_library import cli


@pytest.fixture
def library(tmp_path):
    profile = tmp_path / "Profiles" / "demo"
    profile.mkdir(parents=True)
    (tmp_path / "profiles.ini").write_text("[General]\nStartWithLastProfile=1\n[Profile0]\nName=demo\nIsRelative=1\nPath=Profiles/demo\nDefault=1\n")
    with closing(sqlite3.connect(profile / "places.sqlite")) as conn, conn:
        conn.executescript("""
        CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT);
        CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY,guid TEXT,type INTEGER,parent INTEGER,position INTEGER,title TEXT,dateAdded INTEGER,lastModified INTEGER,fk INTEGER);
        INSERT INTO moz_places VALUES(1,'https://example.test/python'),(2,'https://example.test/sql'),(3,'https://private-history.test');
        INSERT INTO moz_bookmarks VALUES
        (1,'root________',2,0,0,'',0,0,NULL),
        (2,'toolbar_____',2,1,0,'Toolbar',0,0,NULL),
        (3,'tags________',2,1,1,'Tags',0,0,NULL),
        (4,'courses_____',2,2,0,'Courses',0,0,NULL),
        (5,'python______',1,4,0,'Python',10,20,1),
        (6,'sql_________',1,4,1,'SQL',11,21,2),
        (7,'tagfolder___',2,3,0,'Learning',0,0,NULL),
        (8,'tagitem_____',1,7,0,NULL,0,0,1),
        (9,'separator___',3,4,2,NULL,0,0,NULL),
        (10,'other_______',2,1,3,NULL,0,0,NULL),
        (11,'duplicate___',1,10,0,NULL,0,0,1);
        """)
    return tmp_path, profile


def invoke(capsys, *args):
    status = cli.main(list(args))
    return status, json.loads(capsys.readouterr().out)


def test_bookmarks_context_tags_no_history(library, capsys):
    root, path = library
    before = (path / 'places.sqlite').read_bytes()
    code, result = invoke(capsys, '--root', str(root), 'bookmarks', '--limit', '2')
    assert code == 0 and result['total'] == 3 and result['next_offset'] == 2
    item = result['items'][0]
    assert item['folder_path'] == ['root________', 'Toolbar', 'Courses']
    assert item['folder_ids'] == [1, 2, 4] and item['tags'] == ['Learning']
    assert item['date_added_us'] == 10
    assert 'private-history' not in json.dumps(result)
    _, last = invoke(capsys, '--profile-path', str(path), 'bookmarks', '--offset', '2')
    assert last['next_offset'] is None and last['items'][0]['title'] is None
    assert (path / 'places.sqlite').read_bytes() == before


def test_folders_search_filters(library, capsys):
    root, path = library
    _, folders = invoke(capsys, '--root', str(root), 'folders', '--folder', '2')
    assert [r['id'] for r in folders['items']] == [4]
    for query, count in [('learning', 2), ('COURSES', 2), ('sql', 1), ('%_', 0), ('no-match', 0)]:
        _, result = invoke(capsys, '--profile', 'Profile0', '--root', str(root), 'search', query)
        assert result['total'] == count
    _, result = invoke(capsys, '--profile-path', str(path), 'bookmarks', '--folder', '4')
    assert result['total'] == 2
    for command in [('search', ' '), ('folders', '--folder', '999')]:
        code, result = invoke(capsys, '--root', str(root), *command)
        assert code == 1 and result['error']


def test_profile_selection(library, capsys):
    root, path = library
    _, result = invoke(capsys, '--root', str(root), 'profiles')
    assert result['profiles'][0]['default'] and result['profiles'][0]['available']
    assert cli.select_profile(root, 'demo', None) == path
    with pytest.raises(ValueError, match='ambiguous'):
        cli.select_profile(root, 'unknown', None)
    ini = root / 'profiles.ini'
    ini.write_text(ini.read_text() + f'\n[Profile1]\nPath={root / "missing"}\nIsRelative=0\nDefault=1\n')
    with pytest.raises(ValueError, match='ambiguous'):
        cli.select_profile(root, None, None)
    ini.write_text(ini.read_text() + '\n[InstallABC]\nDefault=Profiles/demo\n')
    assert cli.select_profile(root, None, None) == path
    assert cli.profiles(root)[1]['name'] == 'Profile1'
    assert not cli.profiles(root)[1]['available']
    ini.write_text('[Profile0]\nPath=Profiles/demo\n')
    assert cli.select_profile(root, None, None) == path
    ini.write_text('[General]\n')
    with pytest.raises(ValueError):
        cli.select_profile(root, None, None)
    ini.write_text('[Profile0]\nName=broken\n')
    with pytest.raises(ValueError, match='Missing Path'):
        cli.profiles(root)


def test_missing_corrupt_and_invalid_config(tmp_path, capsys):
    for args in [('--root', str(tmp_path), 'profiles'), ('--profile-path', str(tmp_path), 'bookmarks')]:
        code, result = invoke(capsys, *args)
        assert code == 1 and result['error']
    assert not (tmp_path / 'places.sqlite').exists()
    (tmp_path / 'profiles.ini').write_text('invalid')
    assert invoke(capsys, '--root', str(tmp_path), 'profiles')[0] == 1
    (tmp_path / 'places.sqlite').write_text('corrupt')
    assert invoke(capsys, '--profile-path', str(tmp_path), 'bookmarks')[0] == 1


def test_cycles_rejected(library):
    _, path = library
    with closing(sqlite3.connect(path / 'places.sqlite')) as conn, conn:
        conn.execute('UPDATE moz_bookmarks SET parent=4 WHERE id=2')
    with pytest.raises(ValueError, match='cycle'):
        cli.snapshot(path)


def test_wal_visible_and_enforced_read_only(library, monkeypatch):
    _, path = library
    original = sqlite3.connect
    writer = original(path / 'places.sqlite')
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE moz_bookmarks SET title='Committed WAL' WHERE id=5")
        writer.commit()
        db_before = (path / 'places.sqlite').read_bytes()
        wal_before = (path / 'places.sqlite-wal').read_bytes()
        def checked(*args, **kwargs):
            assert 'mode=ro' in args[0] and 'immutable' not in args[0]
            conn = original(*args, **kwargs)
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                conn.execute("UPDATE moz_bookmarks SET title='oops'")
            return conn
        monkeypatch.setattr(cli.sqlite3, 'connect', checked)
        _, books = cli.snapshot(path)
        assert books[0]['title'] == 'Committed WAL'
        assert (path / 'places.sqlite').read_bytes() == db_before
        assert (path / 'places.sqlite-wal').read_bytes() == wal_before
    finally:
        writer.close()


def test_missing_tags_and_orphan(library):
    _, path = library
    with closing(sqlite3.connect(path / 'places.sqlite')) as conn, conn:
        conn.execute('DELETE FROM moz_bookmarks WHERE id IN (3,7,8)')
        conn.execute('UPDATE moz_bookmarks SET parent=999 WHERE id=5')
    _, books = cli.snapshot(path)
    assert books[0]['folder_path'] == [] and books[0]['tags'] == []


@pytest.mark.parametrize('args', [('bookmarks','--limit','0'), ('bookmarks','--offset','-1'), ('bookmarks','--limit','bad'), ('--profile','a','--profile-path','b','bookmarks'), ()])
def test_argument_errors(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(args))
    assert exc.value.code == 2


def test_cli_subprocess(library):
    _, path = library
    result = subprocess.run([sys.executable, '-m', 'firefox_library.cli', '--profile-path', str(path), 'search', 'Python'], capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)['total'] == 2
    assert not result.stderr


def test_locked_fallback_wal_and_cleanup(library, monkeypatch):
    _, path = library
    writer = sqlite3.connect(path / 'places.sqlite')
    original = cli.read_rows
    targets = []
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE moz_bookmarks SET title='WAL fallback' WHERE id=5")
        writer.commit()
        def locked(db, check=False):
            if db.parent == path:
                raise sqlite3.OperationalError('database is locked')
            targets.append(db.parent)
            assert db.parent.stat().st_mode & 0o777 == 0o700
            assert not Path(str(db) + '-shm').exists()
            return original(db, check)
        monkeypatch.setattr(cli, 'read_rows', locked)
        assert cli.snapshot(path)[1][0]['title'] == 'WAL fallback'
        assert targets and all(not p.exists() for p in targets)
    finally:
        writer.close()


@pytest.mark.parametrize('change', ['main', 'create-wal', 'remove-wal'])
def test_copy_retry_changes(library, monkeypatch, change):
    _, path = library
    db = path / 'places.sqlite'
    wal = Path(str(db) + '-wal')
    if change == 'remove-wal':
        wal.write_bytes(b'')
    original = cli.shutil.copyfile
    calls = []
    def changing(source, target):
        calls.append(source)
        result = original(source, target)
        if len(calls) == 1:
            if change == 'main':
                import os
                stat = db.stat()
                os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
            elif change == 'create-wal':
                wal.write_bytes(b'')
            else:
                wal.unlink()
        return result
    monkeypatch.setattr(cli.shutil, 'copyfile', changing)
    assert len(cli.copied_rows(db)) == 11
    assert calls.count(db) == 2


def test_copy_fail_closed(library, monkeypatch):
    _, path = library
    db = path / 'places.sqlite'
    journal = Path(str(db) + '-journal')
    journal.write_bytes(b'active')
    with pytest.raises(ValueError, match='rollback journal'):
        cli.copied_rows(db)
    journal.unlink()
    original = cli.signature
    tick = 0
    def unstable(paths):
        nonlocal tick
        tick += 1
        return [(*original(paths)[0][:-1], tick), None, None]
    monkeypatch.setattr(cli, 'signature', unstable)
    with pytest.raises(ValueError, match='kept changing'):
        cli.copied_rows(db)


def test_copied_integrity_failure(library, monkeypatch):
    _, path = library
    original = cli.sqlite3.connect
    class BrokenCheck:
        def __init__(self, *args, **kwargs):
            self.conn = original(*args, **kwargs)
        def execute(self, query):
            if query == 'PRAGMA quick_check':
                class Result:
                    def fetchone(self):
                        return ['corrupt']
                return Result()
            return self.conn.execute(query)
        def close(self):
            self.conn.close()
    monkeypatch.setattr(cli.sqlite3, 'connect', BrokenCheck)
    with pytest.raises(ValueError, match='integrity'):
        cli.read_rows(path / 'places.sqlite', check=True)


@pytest.mark.parametrize('missing', [False, True])
def test_module_entrypoint(library, monkeypatch, capsys, missing):
    """The real module wrapper must preserve JSON and its application exit status."""
    import runpy

    _, profile = library
    if missing:
        profile = profile / 'absent'
    monkeypatch.setattr(sys, 'argv', ['firefox-library', '--profile-path', str(profile), 'bookmarks'])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(cli.__file__, run_name='__main__')
    assert result.value.code == (1 if missing else 0)
    payload = json.loads(capsys.readouterr().out)
    if missing:
        assert payload['error']
    else:
        assert payload['total'] == 3
        assert payload['items'][0]['title'] == 'Python'
