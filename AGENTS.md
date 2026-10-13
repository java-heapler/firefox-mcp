Follow the standalone apple-books conventions: hatchling, src layout, uv lock, JSON CLI, skill under skills/firefox.
Keep all Firefox access read-only: SQLite mode=ro and query_only. Never use immutable on a live WAL database or copy only places.sqlite. Do not commit personal profiles, bookmarks, or exports.
Run uv run pytest --cov=firefox_library --cov-branch and uv build after implementation changes. Use synthetic fixtures. Keep README, help, and skill aligned. Do not modify central skill repositories.
