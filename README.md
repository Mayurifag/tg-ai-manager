# Telegram AI Manager

A LOT OF WORK TO BE DONE IN THIS REPO! ASLO ITS VIBE-CODED PRETTY MUCH!!

Personal assisstant for telegram accounts with tons of (yet developing)
features like auto-read/skipping ads or trash messages or finding information in
those shitty city chats, auto-replying/likes, etc. Might manage multiple user
accounts via MTProto API. In future I might also add local models which might be
learned on the context of specific chat and user might ask questions to it.

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

### **Using Docker Compose (Development Environment)**

~~~bash
make
~~~

### **Access Web Interface**

Open <http://localhost:8000>

## TODO

- add migrations tool on start of container for sqlite and/or redis
- Make global_settings to users. We will have users table. TG_API_ID and TG_API_HASH wont be used anymore, we use db. Add encryption with generated key from app_data. Session will be persisted on disk.
- forums bug that it doesnt updates and shows unread messages even though in reality there is no msgs to read
- performance issues after some time. seems i am throttled by telegram but not sure
