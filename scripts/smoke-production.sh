#!/bin/sh
set -eu

image=${1:?Pass the locally built production image name}
volume=$(docker volume create)
container=

cleanup() {
	if [ -n "$container" ]; then
		docker rm -f "$container" >/dev/null 2>&1 || true
	fi
	docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

start_container() {
	container=$(docker run -d --pull never --network none \
		--mount "type=volume,src=$volume,dst=/app_data" \
		-e TG_API_ID=12345 -e TG_API_HASH=synthetic-hash-only "$image")
	if ! docker exec "$container" sh -c '
        for n in $(seq 1 40); do
            if curl -fsS http://127.0.0.1:8000/api/live >/dev/null 2>&1; then exit 0; fi
            sleep 1
        done
        exit 1
    '; then
		docker logs "$container"
		return 1
	fi
}

stop_container() {
	docker stop --timeout 25 "$container" >/dev/null
	test "$(docker inspect --format '{{.State.ExitCode}}' "$container")" = 0
	docker logs "$container" 2>&1 | grep -F '"event": "application_shutdown"' >/dev/null
}

start_container
docker exec "$container" curl -fsS -o /dev/null http://127.0.0.1:8000/login
docker exec -i "$container" /venv/bin/python - <<'PY'
import asyncio
import json
import sqlite3
import urllib.error
import urllib.request

from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from src.rules.models import Rule, RuleType
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.models import User
from src.users.sqlite_repo import SqliteUserRepository

try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)
    raise AssertionError('Disconnected account must not be ready')
except urllib.error.HTTPError as error:
    assert error.code == 503
    health = json.load(error)
    assert health['db'] and health['cache'] and not health['telegram_authorized']

async def seed():
    session = StringSession()
    session.set_dc(2, '127.0.0.1', 443)
    session.auth_key = AuthKey(b'\x05' * 256)
    encoded = session.save()
    users = SqliteUserRepository('/app_data/data.db')
    rules = SqliteRuleRepository('/app_data/data.db')
    await users.save_user(User(username='synthetic', session_string=encoded, ai_api_key='synthetic-key', is_premium=True))
    await rules.add(Rule(rule_type=RuleType.AUTOREACT, chat_id=-100123, topic_id=7, config={'emoji': '👍', 'target_users': [123]}))
    with sqlite3.connect('/app_data/data.db') as source:
        stored = source.execute('SELECT session_string, ai_api_key FROM users WHERE id=1').fetchone()
        assert stored[0] != encoded and stored[1] != 'synthetic-key'
        with sqlite3.connect('/app_data/backup.db') as backup:
            source.backup(backup)
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

asyncio.run(seed())
PY
docker exec "$container" /usr/local/bin/valkey-cli SET acceptance:synthetic persistence-ok >/dev/null
stop_container
docker rm "$container" >/dev/null
container=

start_container
premium_lookup_failed=false
for _ in $(seq 1 15); do
	if docker logs "$container" 2>&1 | grep -F '"event": "get_premium_status_failed"' >/dev/null; then
		premium_lookup_failed=true
		break
	fi
	sleep 1
done
if [ "$premium_lookup_failed" != true ]; then
	docker logs "$container"
	exit 1
fi
docker exec -i "$container" /venv/bin/python - <<'PY'
import asyncio
import sqlite3
import time

from src.adapters.valkey_repo import ValkeyActionRepository, ValkeyEventRepository
from src.rules.sqlite_repo import SqliteRuleRepository
from src.users.sqlite_repo import SqliteUserRepository

async def verify(path):
    user = await SqliteUserRepository(path).get_user(1)
    rules = await SqliteRuleRepository(path).get_all()
    assert user.username == 'synthetic' and user.session_string and user.is_premium
    assert user.ai_api_key == 'synthetic-key'
    assert len(rules) == 1 and rules[0].chat_id == -100123 and rules[0].topic_id == 7
    assert rules[0].config == {'emoji': '👍', 'target_users': [123]}
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    return user.session_string

async def main():
    with sqlite3.connect('/app_data/backup.db') as backup:
        with sqlite3.connect('/app_data/restored.db') as restored:
            backup.backup(restored)
    assert await verify('/app_data/data.db') == await verify('/app_data/restored.db')
    for repo_type in (ValkeyActionRepository, ValkeyEventRepository):
        repo = repo_type('redis://127.0.0.1:6379/0')
        now = time.time()
        await repo.redis.delete(repo.key_prefix)
        entries = {f'fixture-{i}': now - 100 + i / 100_000 for i in range(10_000)}
        entries['expired-fixture'] = now - 10_801
        await repo.redis.zadd(repo.key_prefix, entries)
        await repo._add_item({'fixture': 'new'}, now)
        assert await repo.redis.zcard(repo.key_prefix) == 10_000
        assert await repo.redis.zscore(repo.key_prefix, 'expired-fixture') is None
        assert await repo.redis.zscore(repo.key_prefix, 'fixture-0') is None
        assert await repo.redis.zscore(repo.key_prefix, 'fixture-1') is not None
        assert 0 < await repo.redis.ttl(repo.key_prefix) <= 10_800
        await repo.redis.zadd(repo.key_prefix, {'extra-fixture': now + 1})
        await repo.cleanup_expired()
        assert await repo.redis.zcard(repo.key_prefix) == 10_000
        assert await repo.redis.zscore(repo.key_prefix, 'fixture-1') is None
        await asyncio.gather(*(
            repo._add_item({'fixture': f'concurrent-{i}'}, now + 2 + i / 1000)
            for i in range(25)
        ))
        assert await repo.redis.zcard(repo.key_prefix) == 10_000
        await repo.close()

asyncio.run(main())
PY
test "$(docker exec "$container" /usr/local/bin/valkey-cli GET acceptance:synthetic)" = persistence-ok
stop_container
echo 'Synthetic production image smoke test passed.'
