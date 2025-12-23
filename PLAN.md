# Refactoring Plan: Eliminate Technical Debt

A comprehensive plan to improve code quality, reduce code smells, apply DRY principles, and split oversized files in the tg-ai-manager repository.

---

## Executive Summary

| Area                          | Severity   | Effort | Impact |
| ----------------------------- | ---------- | ------ | ------ |
| Split `telegram.py` God Class | 🔴 Critical | High   | High   |
| Extract `app.py` Concerns     | 🟠 High     | Medium | High   |
| DRY SQLite Repositories       | 🟡 Medium   | Low    | Medium |
| Fix Circular Import           | 🟡 Medium   | Low    | High   |
| Add Structured Logging        | 🟡 Medium   | Medium | High   |

---

## Issue 1: Split `telegram.py` God Class (632 lines)

### Problem
[telegram.py](file:///home/mayurifag/Code/tg-ai-manager/src/adapters/telegram.py) is a "God Class" handling **6+ distinct responsibilities**:
1. Telegram client lifecycle management
2. Event handling (4 different handlers)
3. Media downloading/caching
4. Message parsing/transformation
5. Chat/Forum topic fetching
6. Message read acknowledgment

### Proposed Changes

#### [NEW] `src/adapters/telegram/client.py`
- Move `TelethonAdapter.__init__`, `connect()`, `disconnect()`
- Keep client lifecycle and basic setup

#### [NEW] `src/adapters/telegram/event_handlers.py`
- Move `_handle_new_message()`, `_handle_edited_message()`, `_handle_deleted_message()`, `_handle_chat_action()`
- Move `_dispatch()`, `add_event_listener()`, event registration

#### [NEW] `src/adapters/telegram/media.py`
- Move `download_media()`, `_get_chat_image()`
- Media caching logic

#### [NEW] `src/adapters/telegram/message_parser.py`
- Move `_parse_message()`, `_extract_text()`, `_extract_topic_id()`
- Sender info extraction (`_get_sender_color()`, `_get_sender_initials()`)

#### [NEW] `src/adapters/telegram/chat_operations.py`
- Move `get_chats()`, `get_chat()`, `get_messages()`, `get_forum_topics()`, `get_topic_name()`, `mark_as_read()`

#### [MODIFY] `src/adapters/telegram/__init__.py`
- Re-export `TelethonAdapter` for backward compatibility

```
src/adapters/telegram/
├── __init__.py          (re-exports TelethonAdapter)
├── client.py            (~50 lines - lifecycle)
├── event_handlers.py    (~130 lines - event handling)
├── media.py             (~80 lines - media download)
├── message_parser.py    (~120 lines - message parsing)
├── chat_operations.py   (~180 lines - chat/forum ops)
└── types.py             (~30 lines - ITelethonClient protocol)
```

---

## Issue 2: Extract `app.py` Concerns (263 lines)

### Problem
[app.py](file:///home/mayurifag/Code/tg-ai-manager/src/app.py) mixes:
- Flask app setup & lifecycle hooks
- Signal handling
- SSE broadcasting/queue management
- 13+ route handlers
- JSON serialization utilities
- Inline template string (line 246-251)

### Proposed Changes

#### [NEW] `src/web/__init__.py`
- Create Quart app factory

#### [NEW] `src/web/sse.py`
- Move `broadcast_event()`, `connected_queues`, `event_stream()`
- SSE connection management

#### [NEW] `src/web/routes/chat.py`
- Move chat/topic view routes and API endpoints

#### [NEW] `src/web/routes/forum.py`
- Move forum routes

#### [NEW] `src/web/routes/rules.py`
- Move `/api/rules/*` endpoints

#### [NEW] `src/web/routes/media.py`
- Move `/media/`, `/cache/`, `/static/css/` serving routes

#### [NEW] `src/web/routes/__init__.py`
- Blueprint registration

#### [MODIFY] `src/app.py`
- Simplify to app factory that imports and registers blueprints

```
src/web/
├── __init__.py         (app factory, lifecycle hooks)
├── sse.py              (SSE broadcast logic)
├── serializers.py      (json_serializer)
└── routes/
    ├── __init__.py     (register blueprints)
    ├── chat.py
    ├── forum.py
    ├── rules.py
    └── media.py
```

---

## Issue 3: DRY SQLite Repositories

### Problem
[sqlite_repo.py](file:///home/mayurifag/Code/tg-ai-manager/src/adapters/sqlite_repo.py) and [rules/sqlite_repo.py](file:///home/mayurifag/Code/tg-ai-manager/src/rules/sqlite_repo.py) duplicate:
- `asyncio.to_thread()` wrapper pattern
- `sqlite3.connect()` context manager usage
- Row → dataclass mapping boilerplate

### Proposed Changes

#### [NEW] `src/infrastructure/db.py`
```python
class BaseSqliteRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _execute(self, func: Callable) -> Any:
        return await asyncio.to_thread(func)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)
```

#### [MODIFY] `src/adapters/sqlite_repo.py`
- Inherit from `BaseSqliteRepository`
- Use shared helpers

#### [MODIFY] `src/rules/sqlite_repo.py`
- Inherit from `BaseSqliteRepository`
- Use shared helpers

---

## Issue 4: Fix Circular Import

### Problem
[rules/service.py:60](file:///home/mayurifag/Code/tg-ai-manager/src/rules/service.py#L60-L62) has a **runtime circular import**:
```python
async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
    ...
    from src.container import get_chat_interactor  # ⚠️ Circular!
```

### Proposed Changes

#### [MODIFY] `src/rules/service.py`
- Accept `chat_repository` or topic fetcher as constructor dependency
- Remove runtime import

```diff
class RuleService:
-   def __init__(self, rule_repo, action_repo):
+   def __init__(self, rule_repo, action_repo, chat_repo: ChatRepository):
        self.rule_repo = rule_repo
        self.action_repo = action_repo
+       self.chat_repo = chat_repo

    async def apply_autoread_to_all_topics(self, forum_id: int, enabled: bool):
-       from src.container import get_chat_interactor
-       interactor = get_chat_interactor()
-       topics = await interactor.get_forum_topics(forum_id)
+       topics = await self.chat_repo.get_forum_topics(forum_id)
```

#### [MODIFY] `src/rules/container.py`
- Pass `chat_repo` when creating `RuleService`

---

## Issue 5: Consolidate Containers

### Problem
Two separate containers with duplicated `SqliteActionRepository` instantiation:
- [container.py](file:///home/mayurifag/Code/tg-ai-manager/src/container.py)
- [rules/container.py](file:///home/mayurifag/Code/tg-ai-manager/src/rules/container.py#L11)

### Proposed Changes

#### [MODIFY] `src/container.py`
- Move all DI logic here
- Share single `ActionRepository` instance across services
- Export `get_rule_service()` from here

#### [DELETE] `src/rules/container.py`

---

## Issue 6: Add Structured Logging

### Problem
Currently using `print()` statements for error logging (20+ occurrences across codebase). For Docker deployments, need structured JSON logs to stdout for log aggregation.

### Proposed Changes

#### [MODIFY] `pyproject.toml`
```toml
[project]
dependencies = [
    "pydantic-settings",
    "python-dotenv>=1.2.1",
    "quart",
    "telethon>=1.42.0",
    "structlog>=24.1.0",  # Add structlog for structured logging
]
```

#### [NEW] `src/infrastructure/logging.py`
```python
import structlog
import sys

def configure_logging():
    """Configure structlog for JSON output to stdout (Docker-friendly)."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

def get_logger(name: str):
    """Get a structured logger instance."""
    return structlog.get_logger(name)
```

#### [MODIFY] `src/app.py`
- Call `configure_logging()` at startup
- Replace `print()` with structured logger

#### [MODIFY] All files with `print()` statements
- Replace with `logger.info()`, `logger.error()`, etc.
- Key files: `telegram.py`, `app.py`, `sqlite_repo.py`
- Example: `logger.error("failed_to_fetch_topics", chat_id=chat_id, error=str(e))`
- Add some really needed meaningful logs, because for now not much logged

---

## Issue 7: Extract Inline Template

### Problem
[app.py:246-251](file:///home/mayurifag/Code/tg-ai-manager/src/app.py#L246-L251) has inline template string:
```python
template = """\
{% import "macros/chat_card.html.j2" as cards %}\
...
```

### Proposed Changes

#### [NEW] `src/templates/partials/chat_card_wrapper.html.j2`
- Move the inline template to a proper file

#### [MODIFY] `src/app.py`
- Use `render_template()` instead of `render_template_string()`

---

## Recommended Execution Order

| Phase | Tasks                                       | Risk   |
| ----- | ------------------------------------------- | ------ |
| 1️⃣     | Fix circular import, consolidate containers | Low    |
| 2️⃣     | Add test infrastructure, test mappers       | Low    |
| 3️⃣     | Add structured logging                      | Low    |
| 4️⃣     | DRY SQLite repositories                     | Low    |
| 5️⃣     | Extract inline template                     | Low    |
| 6️⃣     | Split `telegram.py` into package            | Medium |
| 7️⃣     | Extract `app.py` into blueprints            | Medium |

---

## Verification Plan

### Automated Tests
Since no tests exist yet, verification will initially be manual:

```bash
# After each refactoring step, run the dev server:
uv sync --dev
uv run hypercorn src.app:app --reload -b 0.0.0.0:8000
```

### Manual Verification Checklist
1. **Homepage loads** - Open http://localhost:8000, verify chat list appears
2. **Chat view works** - Click on a chat, verify messages load
3. **Forum topics work** - Open a forum chat, verify topics display
4. **Live events work** - Send a test message, verify it appears in sidebar
5. **Mark as read works** - Click "Mark as Read" button
6. **Autoread toggle works** - Toggle autoread on a chat

### Logging Verification
After adding structured logging, verify JSON output:
```bash
uv run hypercorn src.app:app --reload -b 0.0.0.0:8000 2>&1 | head -n 20
# Should see JSON-formatted log entries
```
