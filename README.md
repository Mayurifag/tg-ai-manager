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

- AI integration to skip ads - we will have prompt which will ask LLM if message is ad or not and auto-read if it is
- AI integration to make a summary on person and save it to db based on all messages (should save latest message which it parsed and may be continued to save context notes)
- AI integration to help with advice - ask LLM to give some advice on recent messages what to answer next with context. For example being a lawyer or teacher or else based on prompt
- AI integration to notify on liked and/or useful posts in channel. Every n hours read messages of previous 3 days and find useful ones (might be based also on likes context) and rephrase them and put into feed channel with link to original
- AI integration to help find answer in chats - ask LLM on some prompt which will use all chat to find answer to some question. Thats the tough one, but research may be done in some hours. This also might be used on several channels. This require a lot of work
- Refactor to have Telethon queue - mark as read / react and so on have to be done on queue with telethon internal throttling
- Support large quotes posts on frontend.
- we save content in png/jpg from telegram -> but we actually may convert and use webp
- I have to cache messages so I can use them to show in live updates what exactly message was deleted or reacted
- Settings cards - have better UI/UX what do they do
- Restyle user indication in sidebar. Switch between users easily in future
- **Multi-tenancy Support:** we have to support multiple users

## Known bugs

- Animated custom emojis arent working
- Reacts - not correctly done for posts in groupchat that are reposts from group. For example we have group and chat for group. We have auto like for both group and main user of groupchat. Group message will be liked and user's groupchat message will not be liked.

### Bugs that perhaps fixed already

- Certain groups autoread doesnt work
- Autoreact - disabling - until reload wrong "dot" on card
- performance issues after some time. seems i am throttled by telegram but not sure
- Load pred messages fix in groups - wrong place of loads, slow, etc. On chats load actually load from the bottom even with pictures. We know their max height so its fine
- Reacts - some messages are not correctly shown that im the author of reaction
- forums bug that it doesnt updates and shows unread messages even though in reality there is no msgs to read
