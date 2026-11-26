# EVA MCP

An [MCP](https://modelcontextprotocol.io) server for [EVA (EvaTeam)](https://evateam.ru): documents, tasks, projects,
time tracking, reports, planning, Service Desk, Git and **the rest of the API**.
Plain Python 3.8+ with no dependencies; the MCP client starts it locally over stdio. Nothing to deploy.

> Kratko po-russki: MCP-сервер для EVA (EvaTeam). Установка — `./install.sh` (спросит адрес вашей EVA, путь к CA при
> необходимости и личный API-токен). Настройки — env или `~/.config/eva-mcp/config.json`.

## Install

1. Issue a personal API token in EVA (profile → **Security** → **Generate API token**). See [docs/TOKEN.md](docs/TOKEN.md).
2. Run:

   ```bash
   ./install.sh
   ```

   The script asks for your EVA URL (e.g. `https://eva.example.com`) and, optionally, a CA bundle (PEM) if your instance
   uses a private CA. It saves them to `~/.config/eva-mcp/config.json`, stores the token (macOS: Keychain item
   `eva-api-token`; Linux/Windows: `~/.config/eva-mcp/token`, mode 600), runs `server.py --check` and registers
   the server in Claude Code (`claude mcp add eva -s user`).
3. Start a new Claude Code session.

### Manual setup and other clients

```bash
mkdir -p ~/.config/eva-mcp
echo '{"url": "https://eva.example.com"}' > ~/.config/eva-mcp/config.json   # or export EVA_URL
security add-generic-password -a "$USER" -s eva-api-token -w                # macOS; elsewhere ~/.config/eva-mcp/token (600)
python3 server.py --check                                                   # shows whose token it is
```

Register with the full path to `server.py` (`~` is not expanded in JSON/TOML; on Windows the command is usually `python`):

| Client | How |
|---|---|
| Claude Code | `claude mcp add eva -s user -- python3 /full/path/server.py` |
| Codex | `codex mcp add eva -- python3 /full/path/server.py` |
| Cursor, Claude Desktop, others | `{ "mcpServers": { "eva": { "command": "python3", "args": ["/full/path/server.py"] } } }` |

### TLS

Certificates are always verified. By default Python's system trust store is used. If your EVA uses a private or
corporate CA, point `EVA_CA_BUNDLE` (or `ca_bundle` in `config.json`) to a PEM file with the chain. Python on macOS does
not read the Keychain, so pass the chain as a file even if the system already trusts it.
`EVA_INSECURE=1` disables verification (with a warning on stderr); use it only for debugging.

## Settings

Each setting is taken from the environment, then `~/.config/eva-mcp/config.json`, then `site.json` next to
`server.py` (optional build defaults, not shipped here).

| Env | config.json | Default | |
|---|---|---|---|
| `EVA_URL` | `url` | — (required) | base URL of your EVA |
| `EVA_TOKEN` | — | — | fallback way to pass the token |
| `EVA_CA_BUNDLE` | `ca_bundle` | system trust store | PEM chain to verify TLS |
| `EVA_INSECURE` | `insecure` | off | `1` = do not verify TLS |
| `EVA_READONLY` | `readonly` | off | `1` = read-only: write tools hidden, any write refused before it reaches EVA |
| `EVA_TIMEOUT` | `timeout` | `120` | seconds; full-text search can take 20–45 s |
| `EVA_CACHE_TTL` | `cache_ttl` | `300` | seconds, read cache; `0` disables |
| `EVA_DOWNLOAD_DIR` | — | `~/Downloads/eva` | where exports and attachments go |
| `EVA_AUDIT_LOG` | — | `~/.local/state/eva-mcp/audit.jsonl` | log of changes made through MCP; `0` disables |
| `EVA_CONFIG` | — | `~/.config/eva-mcp/config.json` | path to the config file |

## Safety

- **Read-only mode** (`EVA_READONLY=1`): one guard in the function every API call goes through rejects any method that
  is not a read (by name) and not on a short allow-list; write tools are not listed in `tools/list`.
- **Audit log**: every change made through MCP (method, object ids, key parameters, result) is appended as a JSON line
  to `~/.local/state/eva-mcp/audit.jsonl`. The token is never logged. Tool: `eva_api` → `audit_log`.

## Tools

Grouped tools take an `action` parameter; write actions are marked `⚠write` and disappear in read-only mode.

| Tool | What it does |
|---|---|
| `eva_document` | get (Markdown), search, list, create, update, delete, move, copy, archive, export (PDF/DOCX), versions, diff, restore, rate, template, approvals |
| `eva_comment` | list, add (threads), delete, inline comments |
| `eva_task` | get, search, statuses, create, update, set_status, delete, sprint/release membership |
| `eva_attachment` | list, download, upload, download any `/files/...` |
| `eva_find` | global search, UBQL queries, saved filters, my tasks, notifications, people, projects |
| `eva_board` | boards and sprints/releases |
| `eva_api` | call **any** API method (writes only with `allow_write: true`), browse classes and methods, export lists, whoami, audit log, refresh catalog |
| `eva_watch` | wait for live changes of an object or class (Socket.IO) |
| `eva_git` | repositories, branches, files, commits, diffs, merge requests |
| `eva_reports`, `eva_report`, `eva_task_stats`, `eva_analytics` | dashboards, reports, aggregates, analytics |
| `eva_task_worklog`, `eva_my_time`, `eva_timer_status`, `eva_log_time`, `eva_update_worklog`, `eva_delete_worklog`, `eva_timer` | time tracking |
| `eva_roadmaps`, `eva_gantt`, `eva_calendar`, `eva_workload` | planning (read-only) |
| `eva_sd_*` | Service Desk: portals, requests, SLA, knowledge base, replies |

Examples: a document code looks like `DOC-123`, a project code like `my-project`, a UBQL query like
`project = my-project and status != "Closed"`.

## API notes

- JSON-RPC: `POST /api/?m=<Class>.<method>`, `Authorization: Bearer <token>`.
- `catalog.json` (every class and method from EVA's own dump, `GET /api/?m=`) and `protocol.json` (the call shapes
  the web UI uses, extracted from its frontend bundle and enabled plugins by `tools/extract_protocol.py`) depend on your
  instance and its plugins, so they are not shipped. `install.sh` generates both on first run; regenerate any time with
  `--refresh-catalog` / `--refresh-protocol`. Without them the server still works, `eva_api` just knows less about methods.

## Tests

```bash
EVA_TOKEN=offline python3 -m unittest discover -s tests -v   # offline: no network, no token
EVA_LIVE=1 python3 -m unittest tests.test_live_readonly -v     # live, read-only (EVA_READONLY is forced)
```

Live tests take sample objects only from the environment (`EVA_TEST_DOC`, `EVA_TEST_TASK`, `EVA_TEST_PROJECT`,
`EVA_TEST_TT_TASK`, `EVA_TEST_ROADMAP`); a test whose sample is not set is skipped.

## Maintenance

```bash
python3 server.py --check             # token, TLS and connectivity
python3 server.py --refresh-catalog   # regenerate catalog.json
python3 server.py --refresh-protocol  # regenerate protocol.json
```

## License

MIT, see [LICENSE](LICENSE).
