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
   - Copy `App api_id` and `App api_hash` and add them into `.env`
3. Launch app `make`.

### **Access Web Interface**

Open <http://localhost:8000>

If its exposed to external internet or you are running production image, it MUST
BE PROTECTED via some app like tinyauth.

## Production Deployment reference (docker-compose file)

~~~yaml
services:
  tg-manager:
    image: ghcr.io/mayurifag/tg-ai-manager:latest
    ports:
      - "14123:8000"
    environment:
      # Must have:
      - TG_API_ID=123456          # Replace with your ID
      - TG_API_HASH=abcdef123...  # Replace with your Hash
      # Optional:
      - CACHE_MAX_SIZE_MB=500
    volumes:
      - ./tg_data:/app_data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/login"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
~~~

## TODO

- Reposts are not seen. Quotes are not seen on frontend. Reacts on repost are not working correctly. Optimistic updates might be not needed??
- Pictures - read previews as webp. Ability to render full webp on click like on imageboards
- Load pred messages fix - wrong place of loads, slow, etc.
- autoread if single message - Multiple media messages count as single message or not?
- Can i do something if I done anything like reading on another client? Only autofetch every n seconds?
- **Multi-tenancy Support:** Currently, the application supports a single active user session in the database. Future refactoring should introduce a `ClientManager` to handle multiple `TelethonAdapter` instances for different users simultaneously.
- forums bug that it doesnt updates and shows unread messages even though in reality there is no msgs to read
- performance issues after some time. seems i am throttled by telegram but not sure
