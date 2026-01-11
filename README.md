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
   - Copy `api_id` and `api_hash` and add them into `.env`
3. Launch app `make`.

### **Access Web Interface**

Open <http://localhost:8000>

If its exposed to external internet or you are running production image, it MUST
BE PROTECTED via some app like tinyauth.

## Production Deployment reference (docker-compose file)

~~~yaml
services:
  # it actually that minimal config, single monolith image container
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
~~~

## TODO

- Debug settings. Message debug on hover button becomes debug option.
  - Debug mode enable/disable in settings
  - Debug option - for live events on hover show original json of event
  - Debug option - "Reset everything" has to be debug option
  - Debug option - Live events have to be hidden
- Live events to include user/avatar
- **Multi-tenancy Support:** Currently, the application supports a single active user session in the database. Future refactoring should introduce a `ClientManager` to handle multiple `TelethonAdapter` instances for different users simultaneously.
- Restyle user indication in sidebar. Switch between users easily
- AI integration to skip ads
- AI integration to help with advice
- AI integration to notify on liked and/or useful posts. Make a feed?
- AI integration to help find answer in chats
- Refactor to have Telethon queue - mark as read / react and so on have to be done on queue with telethon internal throttling
- Support large quotes on frontend.
- png -> webp
- I have to cache messages so I can use them to show in live updates what exactly message was deleted or reacted
- Settings cards - have better UI what do they do

## Known bugs

- Animated custom emojis arent working
- Reacts - not correctly done for posts in groupchat that are reposts from group.
- Reacts - some messages are not correctly shown that im the author of reaction
- forums bug that it doesnt updates and shows unread messages even though in reality there is no msgs to read

### Bugs that perhaps fixed already

- performance issues after some time. seems i am throttled by telegram but not sure
- Load pred messages fix in groups - wrong place of loads, slow, etc. On chats load actually load from the bottom even with pictures. We know their max height so its fine
