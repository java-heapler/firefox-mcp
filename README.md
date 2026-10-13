# Firefox

Read-only Firefox bookmark access for agents. The `firefox-library` CLI returns JSON with folder ancestry, tags, timestamps, and pagination. It never launches Firefox, fetches bookmarked URLs, returns browsing history, or changes bookmarks. No runtime dependencies beyond Python 3.11+.

```sh
uv tool install .
firefox-library profiles
firefox-library --profile default-release folders
firefox-library --profile default-release bookmarks --folder 42 --limit 100
firefox-library --profile default-release search "python" --limit 50
firefox-library --profile-path "/path/to/profile" bookmarks --offset 50
```

Profile options precede the subcommand. `--root` overrides the macOS Firefox configuration directory. Selection uses a unique installation default from profiles.ini, falling back to legacy Default=1 or a sole profile. Multiple candidates fail explicitly; choose an exact name, section ID, or directory. This does not infer which running Firefox window you mean. New selectable profile groups may not expose every member in profiles.ini; use `--profile-path` for an unlisted profile.

`profiles` returns profile metadata. Other commands return `items`, `total`, `offset`, `limit`, and `next_offset`. Follow `next_offset` until null. IDs ascend deterministically; `position` preserves sibling ordering. Folders are a flat tree with `parent_id`, `folder_ids`, and `folder_path`; `--folder` includes descendants but not the folder itself. Paths are arrays so slash characters in titles remain unambiguous. Tags are attached to bookmarks and their internal duplicate entries excluded. Separators are omitted. Timestamps are original Firefox microseconds since Unix epoch.

Search matches literal, Unicode-casefolded text across title, URL, folder ancestry, and tags. No regex or SQL wildcard expansion. Redirect stdout to export a page as JSON; exports contain private browsing-library data and should stay outside this repository. No remote sending is included.

## Consistency and limits

The normal path reads a single SQLite snapshot using `mode=ro` and `query_only`, including committed WAL data. When Firefox holds an exclusive lock, the CLI makes a temporary copy of the main database and its WAL, without copying shared-memory files. It compares source file identity, size, and nanosecond modification/change times before and after copying, retries at most three times if files change, rejects an active rollback journal, and runs SQLite `quick_check` on the copy. This is a best-effort stable copy, not a transactional guarantee under continuous writes. If stability cannot be established, close Firefox and retry. The temporary directory is mode 700 and is deleted afterward; it briefly contains the complete Places database, including history even though queries return bookmarks only. No immutable mode is used. SQLite may manage source shared-memory bookkeeping; bookmark content is never written. Errors exit 1 as JSON; malformed CLI arguments use argparse stderr and exit 2.

The entire bookmark table is loaded once per command, with cached folder ancestry. This favors straightforward consistent folder/tag handling over persistent caches; pagination limits output, not database reads. Separate invocations may see concurrent bookmark edits, so an export assembled over multiple pages is not an atomic snapshot. Schema changes, corruption, or lock errors fail rather than attempting repair. No page-content retrieval, course-quality assessment, free-access verification, history, passwords, sync, or bookmark editing. A bookmark alone does not establish that a course is still free or fits a curriculum.

The implementation follows Mozilla's [Places overview](https://firefox-source-docs.mozilla.org/browser/places/index.html) and [profile service documentation](https://firefox-source-docs.mozilla.org/toolkit/profile/index.html). Synthetic tests do not guarantee support for every future Firefox schema.

## Development

```sh
uv sync --extra dev
uv run pytest --cov=firefox_library --cov-branch --cov-report=term-missing
uv build
```

Tests use synthetic profiles, real SQLite including a live WAL writer, read-only enforcement, and an actual CLI subprocess. No personal bookmarks are used in CI. The agent skill is `skills/firefox/SKILL.md`.

Measured on Python 3.13: **19 tests passing**, **98.85% statement coverage** (172/174) and **98.61% branch coverage** (71/72). The unmeasured lines are subprocess entrypoint execution; the CLI is additionally exercised in a separate process. Coverage is evidence of exercised paths, not a guarantee of complete correctness.
