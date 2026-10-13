"""Firefox bookmark snapshots; no browser launch or profile mutations."""
import argparse
import configparser
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile

ROOT = Path.home() / "Library/Application Support/Firefox"


def profiles(root: Path) -> list[dict]:
    config = configparser.ConfigParser(interpolation=None)
    with (root / "profiles.ini").open() as stream:
        config.read_file(stream)
    defaults = {(root / config[s]["Default"]).resolve() for s in config.sections()
                if s.startswith("Install") and config[s].get("Default")}
    result = []
    for section in config.sections():
        if not section.startswith("Profile"):
            continue
        item = config[section]
        if "Path" not in item:
            raise ValueError(f"Missing Path in {section}")
        path = Path(item["Path"]).expanduser()
        if item.get("IsRelative", "1") == "1":
            path = root / path
        path = path.resolve()
        result.append({"id": section, "name": item.get("Name", section),
                       "path": str(path), "default": path in defaults if defaults else item.get("Default") == "1",
                       "available": (path / "places.sqlite").is_file()})
    return result


def select_profile(root: Path, name: str | None, path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    found = profiles(root)
    if name:
        choices = [p for p in found if name in (p["name"], p["id"])]
    else:
        choices = [p for p in found if p["default"]] or found
    if len(choices) != 1:
        raise ValueError("Profile selection is missing or ambiguous; run profiles and supply --profile or --profile-path")
    return Path(choices[0]["path"])


def read_rows(db: Path, check=False) -> list[dict]:
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if check and conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Copied Firefox database failed integrity check")
        return [dict(r) for r in conn.execute(
            "SELECT b.id,b.guid,b.type,b.parent,b.position,b.title,b.dateAdded,b.lastModified,p.url "
            "FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk=p.id ORDER BY b.id")]


def signature(paths):
    values = []
    for path in paths:
        try:
            stat = path.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            values.append(None)
    return values


def copied_rows(db: Path) -> list[dict]:
    sources = [db, Path(str(db) + "-wal"), Path(str(db) + "-journal")]
    for _ in range(3):
        before = signature(sources)
        if before[2] and before[2][2] > 0:
            raise ValueError("Active Firefox rollback journal; close Firefox and retry")
        with tempfile.TemporaryDirectory(prefix="firefox-library-") as temporary:
            target = Path(temporary) / "places.sqlite"
            try:
                shutil.copyfile(db, target)
                if before[1] is not None:
                    shutil.copyfile(sources[1], Path(str(target) + "-wal"))
            except FileNotFoundError:
                continue
            if signature(sources) != before:
                continue
            return read_rows(target, check=True)
    raise ValueError("Firefox database kept changing during snapshot; close Firefox and retry")


def snapshot(path: Path) -> tuple[list[dict], list[dict]]:
    db = path / "places.sqlite"
    try:
        rows = read_rows(db)
    except sqlite3.OperationalError as error:
        if "locked" not in str(error).lower():
            raise
        rows = copied_rows(db)
    nodes = {r["id"]: r for r in rows}
    roots = {r["guid"]: r["id"] for r in rows}
    tags_root = roots.get("tags________")
    ancestry_cache = {}

    def ancestry(node_id):
        chain, seen = [], set()
        while node_id in nodes:
            if node_id in seen:
                raise ValueError("Bookmark folder cycle detected")
            if node_id in ancestry_cache:
                chain.extend(ancestry_cache[node_id])
                break
            seen.add(node_id)
            node = nodes[node_id]
            chain.append(node)
            node_id = node["parent"]
        return chain

    for row in rows:
        if row["type"] == 2:
            ancestry_cache[row["id"]] = ancestry(row["id"])
    tags = {}
    for row in rows:
        if row["type"] == 1 and row["parent"] in nodes:
            parent = nodes[row["parent"]]
            if parent["parent"] == tags_root:
                tags.setdefault(row["url"], set()).add(parent["title"] or "")
    folders, bookmarks = [], []
    for row in rows:
        chain = ancestry(row["parent"])
        if row["id"] == tags_root or any(n["id"] == tags_root for n in chain):
            continue
        parents = list(reversed(chain))
        folder_ids = [n["id"] for n in parents]
        folder_path = [n["title"] or n["guid"] for n in parents]
        common = {"id": row["id"], "guid": row["guid"], "title": row["title"],
                  "parent_id": row["parent"], "position": row["position"],
                  "folder_ids": folder_ids, "folder_path": folder_path}
        if row["type"] == 2:
            folders.append(common)
        elif row["type"] == 1:
            bookmarks.append({**common, "url": row["url"], "tags": sorted(tags.get(row["url"], [])),
                              "date_added_us": row["dateAdded"], "last_modified_us": row["lastModified"]})
    return folders, bookmarks


def execute(args):
    root = Path(args.root).expanduser()
    if args.command == "profiles":
        return {"profiles": profiles(root)}
    path = select_profile(root, args.profile, args.profile_path)
    folders, bookmarks = snapshot(path)
    items = folders if args.command == "folders" else bookmarks
    if args.folder is not None:
        if args.folder not in {f["id"] for f in folders}:
            raise ValueError("Unknown folder ID")
        items = [r for r in items if args.folder in r["folder_ids"]]
    if args.command == "search":
        query = args.query.casefold()
        if not query.strip():
            raise ValueError("Search query must not be empty")
        items = [r for r in items if query in "\n".join(
            [r["title"] or "", r["url"] or "", *r["folder_path"], *r["tags"]]).casefold()]
    page = items[args.offset:args.offset + args.limit]
    end = args.offset + len(page)
    return {"profile_path": str(path), "items": page, "total": len(items),
            "offset": args.offset, "limit": args.limit,
            "next_offset": end if end < len(items) else None}


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def positive(value):
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT), help="Firefox configuration directory")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--profile", help="Exact profile name or profiles.ini section ID")
    selector.add_argument("--profile-path", help="Explicit profile directory, bypassing discovery")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("profiles")
    for command in ("folders", "bookmarks", "search"):
        child = subs.add_parser(command)
        child.add_argument("--folder", type=int, help="Folder ID; include descendants")
        child.add_argument("--offset", type=nonnegative, default=0)
        child.add_argument("--limit", type=positive, default=50)
        if command == "search":
            child.add_argument("query")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(execute(args), ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError, configparser.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


def run():
    raise SystemExit(main())


if __name__ == "__main__":
    run()
