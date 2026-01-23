# Telegram AI Manager - Comprehensive Refactoring Plan

Complete architectural overhaul of the Telegram AI Manager application. This plan covers queue-based event processing, clean architecture, frontend modernization, AI integrations, and multi-tenancy preparation.

---

## User Review Required

> [!IMPORTANT]
> **Technology Decisions Requiring Confirmation:**
> - **Dramatiq** as queue library (uses Valkey as backend, provides retry/dead-letter/dashboard)
> - **Alpine.js + htmx** for frontend reactivity (no build step, incremental refactor)
> - **File-per-tenant SQLite** for future multi-tenancy

> [!WARNING]
> **Breaking Changes:**
> - Docker image will need additional dependencies (dramatiq, htmx, alpine.js, lottie-web)
> - SSE connection behavior changes (events come from queue, not direct dispatch)
> - Some API endpoints may change signatures during Phase 2

---

## Architecture Overview

~~~mermaid
graph TB
    subgraph "Telethon Layer"
        TG[Telegram API]
        TR[TelethonReader]
        TW[TelethonWriter]
    end

    subgraph "Queue Layer"
        EB[Event Bus]
        TQ[Telegram Operation Queue]
        DLQ[Dead Letter Queue]
    end

    subgraph "Handlers"
        ARH[AutoRead Handler]
        AReH[AutoReact Handler]
        AIH[AI Handlers]
        SSEH[SSE Broadcast Handler]
    end

    subgraph "Web Layer"
        API[Quart API]
        SSE[SSE Stream]
        FE[Alpine.js + htmx Frontend]
    end

    subgraph "Storage"
        VK[Valkey]
        SQL[SQLite per-tenant]
    end

    TG --> TR
    TR --> EB
    EB --> ARH
    EB --> AReH
    EB --> AIH
    EB --> SSEH
    ARH --> TQ
    AReH --> TQ
    TQ --> TW
    TW --> TG
    TQ -.-> DLQ
    SSEH --> SSE
    SSE --> FE
    API --> FE
    VK --> EB
    VK --> TQ
    SQL --> API
~~~

---

## Phase 1: Queue Pipeline Architecture

**Goal:** Replace synchronous Telegram operations with queued, retriable operations using Dramatiq.

### Proposed Changes

---

#### [NEW] [dramatiq_config.py](file:///Users/mayurifag/Code/tg-ai-manager/src/infrastructure/dramatiq_config.py)

Configure Dramatiq with Valkey (Redis-compatible) backend:
- Valkey broker setup
- Exponential backoff middleware (base 1s, max 5 retries)
- Dead-letter queue configuration
- Global rate limiter (0.1s between operations)

---

#### [NEW] [queue_actors.py](file:///Users/mayurifag/Code/tg-ai-manager/src/infrastructure/queue_actors.py)

Dramatiq actors for Telegram operations:
- `mark_as_read_actor(chat_id, topic_id, max_id)` - queued read operation
- `send_reaction_actor(chat_id, msg_id, emoji)` - queued reaction operation
- `auto_engage_actor(chat_id, msg_id, read: bool, react_emoji: str)` - combined operation (Option C)
- Error handling with structured logging
- FloodWaitError detection → pause queue globally

---

#### [NEW] [queue_monitor.py](file:///Users/mayurifag/Code/tg-ai-manager/src/infrastructure/queue_monitor.py)

Queue monitoring service:
- Track failed operations in Valkey
- Store: operation type, chat_id, error message, attempt count, last failure time
- Provide retry-single and retry-all methods
- Expose status for frontend

---

#### [MODIFY] [container.py](file:///Users/mayurifag/Code/tg-ai-manager/src/container.py)

- Add `get_queue_monitor()` factory
- Initialize Dramatiq broker on startup
- Add `get_telegram_writer()` for queue-based writes

---

#### [MODIFY] [docker-compose.yml](file:///Users/mayurifag/Code/tg-ai-manager/docker-compose.yml)

- Add `dramatiq` worker service
- Shared Valkey connection
- Health checks

---

### Verification Plan - Phase 1

**Automated:**
~~~bash
# Start dramatiq worker
dramatiq src.infrastructure.queue_actors

# Test queue enqueue
python -c "from src.infrastructure.queue_actors import mark_as_read_actor; mark_as_read_actor.send(123, None, None)"

# Verify in Valkey
valkey-cli KEYS "dramatiq:*"
~~~

**Manual:**
- Trigger mark-as-read from UI, verify operation logged in Valkey
- Simulate Telegram error, verify retry with backoff
- Check dead-letter queue after max retries

---

## Phase 2: Event-Driven Architecture Refactor

**Goal:** Decouple event producers from consumers. Each handler (autoread, autoreact, SSE) subscribes independently.

### Proposed Changes

---

#### [NEW] [event_bus.py](file:///Users/mayurifag/Code/tg-ai-manager/src/domain/event_bus.py)

Internal event bus abstraction:
- `publish(event: DomainEvent)` - push to Valkey stream
- `subscribe(event_type: str, handler: Callable)` - register handler
- Use Valkey Streams for durability
- Support reconnection without losing events

---

#### [MODIFY] [event_handlers.py](file:///Users/mayurifag/Code/tg-ai-manager/src/adapters/telegram/event_handlers.py)

Change from direct dispatch to event bus publish:
- `_dispatch()` → `event_bus.publish()`
- Remove listener list management
- File stays under 100 lines after split

---

#### [NEW] [handlers/autoread_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/autoread_handler.py)

Dedicated autoread handler:
- Subscribe to `message` events
- Check autoread rules
- Enqueue `auto_engage_actor` if enabled
- ~50 lines

---

#### [NEW] [handlers/autoreact_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/autoreact_handler.py)

Dedicated autoreact handler:
- Subscribe to `message` events
- Check autoreact rules
- If autoreact enabled → enqueue `auto_engage_actor` with emoji
- Skip autoread if autoreact fires (Option C: combined engagement)
- ~60 lines

---

#### [NEW] [handlers/sse_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/sse_handler.py)

SSE broadcast handler:
- Subscribe to ALL event types
- Render templates (moved from sse.py)
- Push to connected client queues
- Include `is_read` status in events

---

#### [DELETE] [sse.py](file:///Users/mayurifag/Code/tg-ai-manager/src/web/sse.py)

Replaced by `handlers/sse_handler.py` and route-only code in `routes/sse.py`

---

#### [MODIFY] [service.py](file:///Users/mayurifag/Code/tg-ai-manager/src/rules/service.py)

- Remove `handle_new_message_event()` - logic moved to handlers
- Keep rule CRUD operations
- Keep `simulate_process_message()` for debugging
- File shrinks from 377 to ~150 lines

---

### Verification Plan - Phase 2

**Automated:**
~~~bash
# Event bus integration test
pytest tests/integration/test_event_bus.py
~~~

**Manual:**
- Send message to chat with autoread enabled
- Verify: event published → autoread handler fires → queue job created → read executed
- Disable autoreact, verify only autoread fires
- Enable autoreact, verify combined engagement (reads implicitly)

---

## Phase 3: File Structure Refactor (~100 Line Limit)

**Goal:** Split large files into focused, single-responsibility modules.

### Files Requiring Split

---

#### [chat_operations.py](file:///Users/mayurifag/Code/tg-ai-manager/src/adapters/telegram/chat_operations.py) (564 lines)

Split into:

| New File             | Responsibility                                            | Lines |
| -------------------- | --------------------------------------------------------- | ----- |
| `chat_reader.py`     | `get_chats`, `get_chat`, `get_all_unread_chats`           | ~80   |
| `message_reader.py`  | `get_messages`, `get_recent_authors`                      | ~70   |
| `forum_reader.py`    | `get_forum_topics`, `get_unread_topics`, `get_topic_name` | ~80   |
| `telegram_writer.py` | `mark_as_read`, `send_reaction` (queue integration)       | ~90   |
| `premium_status.py`  | `get_self_premium_status`                                 | ~20   |

---

#### [event_handlers.py](file:///Users/mayurifag/Code/tg-ai-manager/src/adapters/telegram/event_handlers.py) (299 lines)

Split into:

| New File                    | Responsibility                                      | Lines |
| --------------------------- | --------------------------------------------------- | ----- |
| `event_dispatcher.py`       | Listener management, `_dispatch`                    | ~40   |
| `message_event_handler.py`  | `_handle_new_message`, `_handle_edited_message`     | ~80   |
| `delete_event_handler.py`   | `_handle_deleted_message`                           | ~50   |
| `action_event_handler.py`   | `_handle_chat_action`                               | ~45   |
| `reaction_event_handler.py` | `_handle_other_updates`, `_process_reaction_update` | ~60   |

---

#### [service.py](file:///Users/mayurifag/Code/tg-ai-manager/src/rules/service.py) (377 lines)

Split into:

| New File             | Responsibility                                                   | Lines |
| -------------------- | ---------------------------------------------------------------- | ----- |
| `rule_service.py`    | CRUD operations, `_toggle_rule`                                  | ~80   |
| `rule_checker.py`    | `get_rule`, `is_autoread_enabled`, `check_global_autoread_rules` | ~60   |
| `startup_scanner.py` | `run_startup_scan`                                               | ~60   |
| `rule_simulator.py`  | `simulate_process_message`                                       | ~60   |

---

#### [base.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/base.html.j2) (339 lines)

Split into:

| New File                         | Responsibility                    |
| -------------------------------- | --------------------------------- |
| `base.html.j2`                   | Layout only (~60 lines)           |
| `partials/sidebar_left.html.j2`  | Left sidebar                      |
| `partials/sidebar_right.html.j2` | Right sidebar (debug events)      |
| `partials/lightbox.html.j2`      | Lightbox modal                    |
| `static/js/tooltip.js`           | Tooltip logic                     |
| `static/js/lightbox.js`          | Lightbox logic                    |
| `static/js/sse_handler.js`       | SSE connection and event handling |
| `static/js/time_utils.js`        | Timezone rendering                |

---

### Verification Plan - Phase 3

**Automated:**
~~~bash
# Line count check
find src -name "*.py" -exec wc -l {} \; | awk '$1 > 100 {print}'
# Should return nothing

# Import test
python -c "from src.adapters.telegram import TelethonAdapter; print('OK')"
~~~

---

## Phase 4: Frontend Overhaul

**Goal:** Replace ad-hoc JavaScript with Alpine.js for reactivity and htmx for AJAX. Add Lottie support for animated emojis.

### Proposed Changes

---

#### [NEW] [static/libs/alpine.min.js](file:///Users/mayurifag/Code/tg-ai-manager/static/libs/alpine.min.js)

Vendored Alpine.js (no build step)

---

#### [NEW] [static/libs/htmx.min.js](file:///Users/mayurifag/Code/tg-ai-manager/static/libs/htmx.min.js)

Vendored htmx

---

#### [NEW] [static/libs/lottie-web.min.js](file:///Users/mayurifag/Code/tg-ai-manager/static/libs/lottie-web.min.js)

Telegram-compatible Lottie renderer (nicegram fork or similar)

---

#### [MODIFY] [base.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/base.html.j2)

- Add Alpine.js and htmx script tags
- Convert inline scripts to Alpine components
- Add `x-data` for reactive state

---

#### [MODIFY] [settings/index.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/settings/index.html.j2)

- Add queue status section (hidden if no failures)
- Alpine.js reactive toggle states
- htmx for saving settings without page reload
- Hide AI features if no API key configured

---

#### [NEW] [partials/queue_status.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/partials/queue_status.html.j2)

Queue failure display:
- List of failed operations with error messages
- "Retry" button per operation
- "Retry All" button
- htmx for in-place updates

---

#### [NEW] [routes/queue.py](file:///Users/mayurifag/Code/tg-ai-manager/src/web/routes/queue.py)

API endpoints:
- `GET /api/queue/failed` - list failed operations
- `POST /api/queue/retry/<id>` - retry single operation
- `POST /api/queue/retry-all` - retry all failed

---

#### [MODIFY] Emoji rendering

For animated custom emojis:
- Download TGS file to cache
- Decompress (gzip → JSON)
- Render with Lottie at 24x24px
- Add CSS for sizing consistency

---

### Verification Plan - Phase 4

**Automated:**
~~~bash
# Lighthouse accessibility check
npx lighthouse http://localhost:8000 --only-categories=accessibility
~~~

**Manual:**
- Toggle autoread in settings → verify no page reload
- Fail a queue operation → verify it appears in queue status
- Click retry → verify operation retried
- View animated emoji → verify Lottie renders at correct size

---

## Phase 5: Bug Fixes

**Goal:** Fix all known bugs from README.

### Bug 1: Animated Custom Emojis

**Root cause:** No Lottie renderer
**Fix:** Phase 4 adds lottie-web integration

---

### Bug 2: Reactions on Forum Posts Linked to Channel

**Root cause:** Autoreact only triggers on channel, not linked discussion group

**Fix:**
- In `autoreact_handler.py`, after reacting to channel post:
  - Check if message has linked discussion group
  - If yes, also enqueue reaction for the discussion group message
- This requires Telegram API call to get linked message ID

---

#### [MODIFY] [autoreact_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/autoreact_handler.py)

Add linked group reaction propagation:
~~~python
async def handle_autoreact(event):
    # ... existing logic ...
    if should_react:
        await queue_reaction(chat_id, msg_id, emoji)

        # Propagate to linked discussion if applicable
        linked_msg = await get_linked_discussion_message(chat_id, msg_id)
        if linked_msg:
            await queue_reaction(linked_msg.chat_id, linked_msg.id, emoji)
~~~

---

### Previously Fixed Bugs (verify still working)

- Certain groups autoread
- Autoreact toggle dot state
- Performance issues (queue rate limiting should help)
- Load previous messages in groups
- Reaction author display
- Forum unread status

**Verification:** Manual testing after Phase 4 deployment

---

## Phase 6: Error Handling & Observability

**Goal:** Structured logging, health endpoints, in-app alerts for critical failures.

### Proposed Changes

---

#### [MODIFY] [logging.py](file:///Users/mayurifag/Code/tg-ai-manager/src/infrastructure/logging.py)

- Structured JSON logging to file
- Log rotation
- Log levels configurable via env

---

#### [NEW] [health.py](file:///Users/mayurifag/Code/tg-ai-manager/src/web/routes/health.py)

Health endpoint `GET /health`:
~~~json
{
  "status": "healthy",
  "telegram_connected": true,
  "valkey_connected": true,
  "queue_workers": 1,
  "failed_jobs": 0
}
~~~

---

#### [NEW] [alerts.py](file:///Users/mayurifag/Code/tg-ai-manager/src/infrastructure/alerts.py)

In-app alert system:
- Store alerts in Valkey
- Types: error, warning, info
- Dismissible via UI
- Auto-expire after 24h

---

#### [MODIFY] [base.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/base.html.j2)

Add alert banner at top for critical errors

---

### Verification Plan - Phase 6

~~~bash
# Health check
curl http://localhost:8000/health | jq

# Log inspection
tail -f logs/app.log | jq
~~~

---

## Phase 7: AI Provider Abstraction

**Goal:** Create pluggable AI provider system supporting multiple providers and models per feature.

### Proposed Changes

---

#### [NEW] [ai/ports.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/ports.py)

Abstract AI provider interface:
~~~python
class AIProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, system: str = None) -> str:
        pass

    @abstractmethod
    async def classify(self, text: str, categories: list[str]) -> str:
        pass
~~~

---

#### [NEW] [ai/providers/gemini.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/providers/gemini.py)

Google Gemini implementation:
- Support for gemini-pro, gemini-lite
- API key from encrypted storage
- Rate limiting

---

#### [NEW] [ai/providers/openai_compatible.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/providers/openai_compatible.py)

OpenAI-compatible API (Grok, local models via Ollama):
- Base URL configurable
- Model selection

---

#### [NEW] [ai/factory.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/factory.py)

Provider factory:
~~~python
def get_provider(feature: str, user_id: int) -> AIProvider:
    # Look up user's provider config for this feature
    # Return appropriate provider instance
~~~

---

#### [MODIFY] [settings/models.py](file:///Users/mayurifag/Code/tg-ai-manager/src/settings/models.py)

Add AI configuration:
~~~python
@dataclass
class AIConfig:
    provider: str  # "gemini", "openai", "local"
    model: str
    api_key_encrypted: Optional[str]
    base_url: Optional[str]  # For local/custom endpoints
~~~

---

#### [NEW] [settings/ai_settings.html.j2](file:///Users/mayurifag/Code/tg-ai-manager/src/templates/settings/ai_settings.html.j2)

AI settings page:
- API key input (masked, encrypted on save)
- Per-feature provider/model selection
- Test connection button
- Hidden entirely if no API keys configured

---

### Verification Plan - Phase 7

~~~bash
# Test Gemini provider
python -c "
from src.ai.providers.gemini import GeminiProvider
p = GeminiProvider(api_key='test')
# Mock test
"
~~~

---

## Phase 8: AI Feature - Skip Ads

**Goal:** Automatically mark messages as read if AI classifies them as advertisements.

### Proposed Changes

---

#### [NEW] [ai/classifiers/ad_classifier.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/classifiers/ad_classifier.py)

Ad classification:
~~~python
SYSTEM_PROMPT = """
Classify if the following Telegram message is an advertisement.
Respond with only: AD or NOT_AD
"""

async def is_ad(text: str, provider: AIProvider) -> bool:
    result = await provider.classify(text, ["AD", "NOT_AD"])
    return result == "AD"
~~~

---

#### [NEW] [handlers/skip_ads_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/skip_ads_handler.py)

Handler for skip-ads rule:
- Subscribe to `message` events
- Check if chat has skip-ads enabled
- Call AI classifier
- If AD → enqueue autoread

---

#### [MODIFY] [rules/models.py](file:///Users/mayurifag/Code/tg-ai-manager/src/rules/models.py)

Add `RuleType.SKIP_ADS`

---

#### UI Changes

- Add "Skip Ads" toggle in chat settings (next to autoread/autoreact)
- Only visible if AI API key configured

---

### Verification Plan - Phase 8

**Manual:**
1. Configure Gemini API key
2. Enable skip-ads on a channel
3. Post test ad message
4. Verify auto-read triggers

---

## Phase 9: AI Feature - Person Summary

**Goal:** Build and maintain AI-generated profiles of people based on their messages.

### Proposed Changes

---

#### [NEW] [ai/summarizers/person_summarizer.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/summarizers/person_summarizer.py)

Person summary generation:
~~~python
SYSTEM_PROMPT = """
Based on the messages, extract and update facts about this person.
Categories: location, birthday, interests, preferences, relationships, work, memorable quotes.
Return JSON format.
"""

async def summarize_person(
    existing_summary: dict,
    new_messages: list[str],
    provider: AIProvider
) -> dict:
    # Merge new info with existing
~~~

---

#### [NEW] [person/models.py](file:///Users/mayurifag/Code/tg-ai-manager/src/person/models.py)

~~~python
@dataclass
class PersonProfile:
    user_id: int
    telegram_id: int
    summary: dict  # JSON blob
    notes: str  # User-editable notes
    last_processed_msg_id: int
    updated_at: datetime
~~~

---

#### [NEW] [person/sqlite_repo.py](file:///Users/mayurifag/Code/tg-ai-manager/src/person/sqlite_repo.py)

CRUD for person profiles

---

#### [NEW] [handlers/person_summary_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/person_summary_handler.py)

Scheduled task (not event-driven):
- Run every N hours (configurable)
- For each tracked person, fetch new messages since last_processed_msg_id
- Generate updated summary
- Save to database

---

#### UI Changes

- Person profiles page (list of tracked users)
- Individual profile view with summary + editable notes
- "Track this person" button in chat

---

### Verification Plan - Phase 9

**Manual:**
1. Track a person from a group chat
2. Wait for scheduled job (or trigger manually)
3. Verify summary generated
4. Edit notes, verify persisted

---

## Phase 10: AI Feature - Advice on Replies

**Goal:** AI assistant that suggests reply options based on conversation context.

### Proposed Changes

---

#### [NEW] [ai/advisors/reply_advisor.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/advisors/reply_advisor.py)

Reply suggestion:
~~~python
SYSTEM_PROMPT = """
You are a {role} (e.g., lawyer, teacher, friend).
Based on the conversation, suggest 3 possible replies.
Consider context, tone, and relationship.
"""

async def suggest_replies(
    messages: list[Message],
    role: str,
    provider: AIProvider
) -> list[str]:
~~~

---

#### UI Changes

- "Get advice" button in chat view
- Modal with role selector (lawyer, teacher, friend, etc.)
- Display 3 suggested replies
- Click to copy or insert into reply box (future)

---

### Verification Plan - Phase 10

**Manual:**
1. Open chat with some messages
2. Click "Get advice"
3. Select role
4. Verify 3 suggestions appear

---

## Phase 11: AI Feature - Useful Post Aggregation

**Goal:** Periodically scan channels and aggregate useful/valuable posts into a summary feed.

### Proposed Changes

---

#### [NEW] [ai/aggregators/post_aggregator.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/aggregators/post_aggregator.py)

Post aggregation:
~~~python
SYSTEM_PROMPT = """
From these messages, identify the most valuable/useful ones.
Consider: information density, insights, actionable advice.
Return IDs of top N posts with brief explanation why each is valuable.
"""

async def aggregate_useful_posts(
    messages: list[Message],
    context: str,  # What type of content user finds valuable
    provider: AIProvider
) -> list[dict]:  # [{msg_id, reason}]
~~~

Processing strategy:
- Batch messages (e.g., 50 per API call)
- First pass: filter candidates per batch
- Second pass: rank all candidates

---

#### [NEW] [aggregation/models.py](file:///Users/mayurifag/Code/tg-ai-manager/src/aggregation/models.py)

~~~python
@dataclass
class AggregationConfig:
    id: int
    user_id: int
    source_chat_ids: list[int]
    target_channel_id: Optional[int]  # Where to repost
    schedule_hours: int  # Every N hours
    lookback_days: int  # How far back to scan
    max_posts: int  # Max posts per aggregation
    context_prompt: str  # What is "useful"
~~~

---

#### [NEW] [handlers/post_aggregation_handler.py](file:///Users/mayurifag/Code/tg-ai-manager/src/handlers/post_aggregation_handler.py)

Scheduled task:
- Run on schedule per config
- Fetch messages from source channels
- Process in batches
- Generate rephrased summaries
- Optionally post to target channel

---

#### UI Changes

- Aggregation configs page
- Create/edit config: source channels, target, schedule
- View aggregated posts (feed in app)

---

### Verification Plan - Phase 11

**Manual:**
1. Create aggregation config for a channel
2. Trigger aggregation manually
3. Verify useful posts identified
4. Check feed display

---

## Phase 12: AI Feature - Search Across Chats

**Goal:** Natural language search across messages using AI.

> [!CAUTION]
> This is the most complex AI feature. Implementation will be iterative.

### Approach Options

**Option A: Simple Context Window**
- Fetch recent N messages from selected chats
- Send all to LLM with query
- Works for small context
- Limitation: can't search historical data effectively

**Option B: RAG with Embeddings**
- Generate embeddings for all messages
- Store in vector DB (could use Valkey with VSS module, or SQLite FTS)
- Query: embed question → find similar messages → send to LLM for answer
- More complex but scales better

### Proposed Changes (Option A for MVP)

---

#### [NEW] [ai/search/chat_search.py](file:///Users/mayurifag/Code/tg-ai-manager/src/ai/search/chat_search.py)

~~~python
SYSTEM_PROMPT = """
Search through these chat messages to answer the user's question.
If the answer spans multiple messages, synthesize them.
Always cite which message(s) the answer comes from.
"""

async def search_chats(
    query: str,
    chat_ids: list[int],
    provider: AIProvider,
    chat_repo: ChatRepository
) -> SearchResult:
~~~

---

#### UI Changes

- Search bar in main navigation
- Search results page with highlighted snippets
- Click to jump to message in chat

---

### Future Enhancement (Option B)

Would require:
- Background job to embed new messages
- Vector storage (Valkey VSS or dedicated vector DB)
- Incremental indexing

---

### Verification Plan - Phase 12

**Manual:**
1. Search for a topic mentioned in chats
2. Verify relevant messages found
3. Verify can navigate to original message

---

## Phase 13: Testing Infrastructure

**Goal:** Add test coverage for critical paths.

### Proposed Changes

---

#### [NEW] tests/conftest.py

Pytest fixtures:
- Test database (SQLite in-memory)
- Mock Telegram client
- Mock AI providers
- Mock Valkey

---

#### [NEW] tests/unit/

Unit tests for:
- Rule checker logic
- AI classifiers (with mock provider)
- Queue actors (mock execution)
- Event bus (mock Valkey)

---

#### [NEW] tests/integration/

Integration tests:
- Event flow: Telegram event → queue → execution
- SSE connection and event delivery
- Settings persistence

---

#### [NEW] tests/e2e/

Browser tests (Playwright):
- Settings page toggles work
- Queue retry works
- AI features hidden without API key

---

### Verification Plan - Phase 13

~~~bash
pytest tests/ -v --cov=src --cov-report=html
~~~

---

## Phase 14: Multi-Tenancy Preparation

**Goal:** Prepare architecture for multiple users without full implementation.

### Proposed Changes

---

#### Current Singletons to Abstract

| Current                 | Change                    |
| ----------------------- | ------------------------- |
| `_tg_adapter` global    | Per-user adapter registry |
| `_rule_service` global  | Factory with user_id      |
| `user_id = 1` hardcoded | Extract from session      |
| Single SQLite file      | File-per-user pattern     |

---

#### [MODIFY] [container.py](file:///Users/mayurifag/Code/tg-ai-manager/src/container.py)

Add user-scoped factories:
~~~python
def get_tg_adapter(user_id: int) -> TelethonAdapter:
    # Return cached or create new

def get_user_db_path(user_id: int) -> str:
    return f"{DATA_DIR}/user_{user_id}.db"
~~~

---

#### [MODIFY] [routes/*.py](file:///Users/mayurifag/Code/tg-ai-manager/src/web/routes)

Extract user_id from session/request context instead of hardcoding

---

#### Database Schema

Each user gets own SQLite file:
- `user_1.db` - rules, settings, person profiles
- `user_2.db` - ...

Shared data stays in Valkey:
- Action logs (keyed by user)
- Event streams (keyed by user)
- Queue jobs (tagged with user)

---

#### [NEW] [users/auth.py](file:///Users/mayurifag/Code/tg-ai-manager/src/users/auth.py)

Multi-user preparation:
- Abstract user lookup
- Session management hooks
- User switching (UI ready, logic stubbed)

---

### Verification Plan - Phase 14

~~~bash
# Verify no hardcoded user_id = 1
grep -r "user_id.*=.*1" src/ --include="*.py"
# Should return minimal results (only defaults)
~~~

---

## Implementation Order Summary

~~~mermaid
gantt
    title Refactoring Phases
    dateFormat X
    axisFormat %s

    section Foundation
    Phase 1 - Queue Pipeline :p1, 0, 3
    Phase 2 - Event Architecture :p2, after p1, 3
    Phase 3 - File Structure :p3, after p2, 2

    section Frontend
    Phase 4 - Frontend Overhaul :p4, after p3, 3

    section Stability
    Phase 5 - Bug Fixes :p5, after p4, 2
    Phase 6 - Observability :p6, after p5, 2

    section AI
    Phase 7 - AI Abstraction :p7, after p6, 2
    Phase 8 - Skip Ads :p8, after p7, 1
    Phase 9 - Person Summary :p9, after p8, 2
    Phase 10 - Reply Advice :p10, after p9, 1
    Phase 11 - Post Aggregation :p11, after p10, 3
    Phase 12 - Chat Search :p12, after p11, 3

    section Testing
    Phase 13 - Testing :p13, after p6, 4

    section Multi-tenant
    Phase 14 - Multi-Tenancy Prep :p14, after p12, 2
~~~

---

## File Count Impact

| Category       | Current    | After Refactor |
| -------------- | ---------- | -------------- |
| Python files   | 46         | ~85            |
| Max file lines | 564        | ≤100           |
| Template files | 11         | ~18            |
| JS files       | 0 (inline) | 6              |
| New handlers   | 0          | 8+             |

---

## Docker Changes Required

~~~yaml
# docker-compose.yml additions
services:
  dramatiq-worker:
    build: .
    command: dramatiq src.infrastructure.queue_actors
    depends_on:
      - valkey
    environment:
      - VALKEY_URL=valkey://valkey:6379
~~~

~~~dockerfile
# Dockerfile additions
RUN pip install dramatiq[redis] lottie
~~~

---

## Questions Resolved

1. ✅ Queue backend: Dramatiq + Valkey
2. ✅ Frontend: Alpine.js + htmx (no build step)
3. ✅ Autoread/Autoreact: Combined engagement operation
4. ✅ SQLite stays, file-per-tenant for multi-tenancy
5. ✅ AI features hidden without API key
6. ✅ Per-feature AI provider/model selection

---

## Next Steps

After approval:
1. Begin Phase 1 implementation
2. Set up Dramatiq in existing infrastructure
3. Create first queue actors
4. Migrate one operation (mark_as_read) as proof of concept
