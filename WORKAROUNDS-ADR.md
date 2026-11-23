# Telethon transport workarounds

- `recovering_client.py`, Telethon 1.46.0: reset the sender's pending ping at each connection boundary so a lost pong
  cannot trigger repeated reconnects. Remove when Telethon resets it itself; verify by interrupting a connection with a
  pending ping.
- `recovering_client.py`, Telethon 1.46.0: sticker and reaction mutations use one sender call and a packer that rejects
  requeues of those requests. The normal RPC loop retries some errors, and the sender can replay pending requests on a
  new MTProto session before the reconnect callback runs. Revisit when Telethon exposes a per-request no-replay option
  for both layers. Verify that a lost response never sends the same mutation again and that ordinary RPCs still
  reconnect.
