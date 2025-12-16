# Telegram AI Manager

A LOT OF WORK TO BE DONE IN THIS REPO!

Personal assisstant for telegram accounts with tons of (yet developing)
features like auto-read/skipping ads or trash messages or finding information in
those shitty city chats, auto-replying/likes, etc. Might manage multiple user
accounts via MTProto API. In future I might also add local models which might be
learned on the context of specific chat.

Managed via web interface, which deployed to VPS using docker image and env
variables. Built using Telethon. Async event-driven architecture.
Gemini used as a free AI provider. Perhaps Kafka/Valkey/SQLite or else will be
added in future. There is also logging of actions done by manager.

## Setup

1. Install uv
2. **Get Telegram Credentials**
   - Go to [my.telegram.org](https://my.telegram.org)
   - Login with your phone number
   - Go to "API development tools"
   - Create a new application (values don't matter much)
   - Copy `App api_id` and `App api_hash`

3. **Configure Environment**

~~~bash
cp .env.example .env
~~~

Edit `.env` and paste your `TG_API_ID` and `TG_API_HASH`.

## Running

### **Install dependencies**

Ensure all dependencies, including development ones, are installed.

~~~bash
uv sync --dev
~~~

### **Run with Live Reload (Development)**

Use `hypercorn` with the `--reload` flag for development, which will monitor file changes and restart the server.

~~~bash
uv run hypercorn src.app:app --reload -b 0.0.0.0:8000
~~~

### **Access Web Interface**

Open <http://localhost:8000>

## TODO

- global setting field with tooltip to autoread pinned events, someone entered or removed from chat, etc.
- refactoring. Plan is: Look at my repository and give me plan what could you make in my app better - I mean DRY or splitting big files or else, give me plan. Maybe there are too much smelly code and so on. eliminate tech debt
- saving msgs in valkey/[dramatiq](https://dramatiq.io/)
