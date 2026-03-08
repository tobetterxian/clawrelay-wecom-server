# WebSocket Long Connection Migration Design

Date: 2026-03-09
Status: Approved

## Background

The current ClawRelay WeCom Server uses HTTP callback mode to receive messages from Enterprise WeChat. This requires a publicly accessible URL, message encryption/decryption, and relies on WeChat polling for stream updates. Enterprise WeChat now supports a WebSocket long connection mode for intelligent robots that eliminates these requirements.

## Decision Summary

| Decision | Choice |
|----------|--------|
| Migration strategy | Full replacement (remove HTTP callback entirely) |
| Stream push strategy | Throttled push (~500ms interval) |
| StreamingThinkingManager | Remove, push directly via WebSocket |
| Media decryption | Use per-message `aeskey` from callback body |
| HTTP server | Remove FastAPI entirely, pure asyncio program |
| Health check | Not required by deployment environment |

## Architecture

```
main.py (asyncio)
  startup:
    1. Load all enabled bot configs from DB
    2. Start a WsClient Task per bot

  WsClient (per bot)
    connect() → wss://openws.work.weixin.qq.com
    subscribe() → aibot_subscribe (bot_id + secret)
    heartbeat_loop() → ping every 30s
    receive_loop() → dispatch by cmd type
    reconnect() → exponential backoff (1s → 2s → ... → 60s max)
        │
        ▼
  MessageDispatcher (per bot)
    on_msg_callback() → message handlers → orchestrator → SSE → throttled WS push
    on_event_callback() → enter_chat / template_card_event / feedback / disconnected
        │
        ▼
  claude_relay_orchestrator (largely unchanged)
    → ClaudeRelayAdapter → clawrelay-api (SSE)
```

## WsClient Design

### Connection Lifecycle

```
while True:
    connect to wss://openws.work.weixin.qq.com
    send aibot_subscribe (bot_id + secret)
    run concurrently:
        heartbeat_loop (ping every 30s)
        receive_loop (read messages, dispatch by cmd)
    on error/disconnect:
        exponential backoff reconnect (min(2^n, 60) seconds)
```

### req_id Routing

WebSocket is full-duplex: outgoing requests and incoming callbacks share one connection. A `Dict[req_id, asyncio.Future]` maps request IDs to pending futures:

- **Outgoing request**: Generate req_id, create Future, send message, await Future
- **Incoming response**: Match req_id to pending Future, set_result
- **Incoming callback** (aibot_msg_callback / aibot_event_callback): No pending Future, route to MessageDispatcher

### Reconnection Strategy

- Exponential backoff: `min(2^n, 60)` seconds, reset counter on successful subscribe
- `disconnected_event` from server also triggers reconnect
- Log all reconnection attempts with attempt count

## MessageDispatcher Design

### Message Handling

Receives `aibot_msg_callback`, extracts body fields (msgtype, from.userid, chatid, chattype), routes to appropriate handler (text, image, voice, file, mixed).

Handlers call `claude_relay_orchestrator.handle_text_message()` (or multimodal variant) with a stream push callback.

### Stream Push (Throttled)

```
SSE events:   --T--T--T--T--T--T--T--T--T--T--finish
                |        |        |        |    |
throttle:     --+--------+--------+--------+----+-->
(500ms)         v        v        v        v    v
WS push:     push1    push2    push3    push4  push(finish=true)
```

- Accumulate TextDelta in SSE loop
- An asyncio Task checks every ~500ms for new content, pushes if changed
- On `finish=true`, push immediately without waiting for throttle interval
- ThinkingDelta and ToolUseStart optionally prepended to stream text

### Event Handling

| Event | Action |
|-------|--------|
| `enter_chat` | Reply welcome message via `aibot_respond_welcome_msg` (within 5s) |
| `template_card_event` | Update card via `aibot_respond_update_msg` (within 5s) |
| `feedback_event` | Log user feedback |
| `disconnected_event` | Trigger reconnection |

## Orchestrator Changes

`ClaudeRelayOrchestrator.handle_text_message()` modifications:

1. Remove `StreamingThinkingManager` dependency entirely
2. Add `on_stream_delta: Callable[[str, bool], Awaitable[None]]` callback parameter
3. In SSE loop, call `on_stream_delta(accumulated_text, finish)` via throttle mechanism
4. Simplify return value: just return final text string (streaming already handled via callback)
5. ThinkingDelta/ToolUseStart info can be passed through the same callback

## Database Migration

```sql
-- Add secret field for WebSocket authentication
ALTER TABLE robot_bots ADD COLUMN `secret` VARCHAR(200) DEFAULT NULL
  COMMENT 'WebSocket long connection secret' AFTER `encoding_aes_key`;

-- Make HTTP callback fields nullable (no longer required)
ALTER TABLE robot_bots MODIFY COLUMN `token` VARCHAR(100) DEFAULT NULL
  COMMENT 'Enterprise WeChat token (HTTP callback mode, deprecated)';
ALTER TABLE robot_bots MODIFY COLUMN `encoding_aes_key` VARCHAR(50) DEFAULT NULL
  COMMENT 'Enterprise WeChat AES key (HTTP callback mode, deprecated)';

-- callback_path no longer needed
ALTER TABLE robot_bots MODIFY COLUMN `callback_path` VARCHAR(200) DEFAULT NULL
  COMMENT 'Callback path (HTTP callback mode, deprecated)';
```

## Media File Decryption Change

In long connection mode, image and file messages include a per-message `aeskey`:

```json
{"image": {"url": "URL", "aeskey": "AESKEY"}}
{"file": {"url": "URL", "aeskey": "AESKEY"}}
```

Decryption uses AES-256-CBC with PKCS#7 padding (same algorithm), but:
- Key: `base64decode(aeskey)` (from each message, not from bot config)
- IV: first 16 bytes of the decoded key

`ImageUtils` and `FileUtils` decryption methods will accept per-message `aeskey` parameter.

## File Changes

### New Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, asyncio main loop, bot loading, WsClient task management |
| `src/transport/ws_client.py` | WsClient: WS connection, subscribe, heartbeat, reconnect, req_id routing |
| `src/transport/message_dispatcher.py` | MessageDispatcher: message dispatch, throttled stream push, reply wrapping |
| `src/transport/__init__.py` | Package init |
| `sql/migrate_websocket.sql` | DB migration script |

### Modified Files

| File | Changes |
|------|---------|
| `config/bot_config.py` | Add `secret` field; `token`/`encoding_aes_key`/`callback_path` optional; remove `callback_path` from required |
| `src/core/claude_relay_orchestrator.py` | Remove STM; add `on_stream_delta` callback; simplify return |
| `src/handlers/message_handlers.py` | Adapt to long connection body format; remove encryption logic |
| `src/utils/weixin_utils.py` | Decryption methods accept per-message `aeskey`; remove `StreamManager`; simplify `MessageBuilder` |
| `requirements.txt` | Add `websockets` |
| `Dockerfile` | Entry point: `python main.py` |
| `docker-compose.yml` | Remove port mapping |

### Deleted Files

| File | Reason |
|------|--------|
| `app.py` | FastAPI entry point, replaced by `main.py` |
| `src/bot/bot_instance.py` | Replaced by WsClient + MessageDispatcher |
| `src/bot/bot_manager.py` | Replaced by direct Task management in main.py |
| `src/utils/message_crypto.py` | No encryption in long connection mode |
| `src/utils/crypto_libs/` | Entire crypto library directory |
| `src/core/streaming_thinking_manager.py` | Replaced by direct WS push |
| `src/core/thinking_collector.py` | STM dependency, removed together |
| `static/` | Test page and homepage, no longer needed |

### Unchanged Files

| File | Notes |
|------|-------|
| `src/adapters/claude_relay_adapter.py` | SSE client to clawrelay-api |
| `src/core/session_manager.py` | Session management |
| `src/core/chat_logger.py` | Chat logging |
| `src/core/choice_manager.py` | AskUserQuestion handling |
| `src/core/task_registry.py` | Task registry |
| `src/handlers/command_handlers.py` | Command handling |
| `src/handlers/custom/` | Custom commands |
| `src/utils/database.py` | Database utilities |
| `src/utils/text_utils.py` | Text utilities |

## Dependencies

New: `websockets` (lightweight async WebSocket client, pure Python)

Remove: No packages removed (pycryptodome still needed for media file decryption)

## Message Format Reference

### Subscribe
```json
{"cmd": "aibot_subscribe", "headers": {"req_id": "ID"}, "body": {"bot_id": "X", "secret": "Y"}}
```

### Receive Message Callback
```json
{"cmd": "aibot_msg_callback", "headers": {"req_id": "ID"}, "body": {"msgid": "X", "aibotid": "X", "chatid": "X", "chattype": "group", "from": {"userid": "X"}, "msgtype": "text", "text": {"content": "hello"}}}
```

### Reply Stream Message
```json
{"cmd": "aibot_respond_msg", "headers": {"req_id": "ID"}, "body": {"msgtype": "stream", "stream": {"id": "SID", "finish": false, "content": "text..."}}}
```

### Heartbeat
```json
{"cmd": "ping", "headers": {"req_id": "ID"}}
```

### Reply Welcome Message
```json
{"cmd": "aibot_respond_welcome_msg", "headers": {"req_id": "ID"}, "body": {"msgtype": "text", "text": {"content": "Welcome!"}}}
```

### Proactive Push Message
```json
{"cmd": "aibot_send_msg", "headers": {"req_id": "ID"}, "body": {"chatid": "X", "msgtype": "markdown", "markdown": {"content": "text"}}}
```
