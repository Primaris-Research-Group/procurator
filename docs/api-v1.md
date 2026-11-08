# PROCURATOR dashboard API v1 (0.28.0)

Base: `https://<box>/assistant/dashboard/api/v1/`. The unversioned `/api/` paths answer the same for one release.
Auth: the dashboard session cookie (`procurator_session`), issued by a Telegram link (`/panel`, 30 days) or the Telegram Mini App
(`POST auth/telegram`, 12 hours). Writes need `Content-Type: application/json`, the `X-Procurator-CSRF` header and a same-site Origin.

**There is no approval endpoint.** Sends, posts, calls, bookings, purchases, home access actions and Live reveals are approved only
with Telegram commands. The API returns the pending line and a link back to the chat.

| Method | Path | Returns |
| --- | --- | --- |
| POST | `auth/telegram` `{init_data}` | `{ok, go}` + session cookie. Telegram launch data checked with the bot token; owner's primary channel id; `auth_date` ≤ 5 min |
| GET | `overview` | model, today's usage, budget, packets, builders |
| GET | `devices` | `{devices:[{id, kind, name, provider, room, capabilities, safety_tier, status, last_seen}], counts, untrusted_fields}` |
| GET | `events?since=ID` | `{events:[{id, ts, source, task_id, packet_id, kind, summary, labels, lane}]}`; with `Accept: text/event-stream` a 25-second SSE stream, resume with `Last-Event-ID` |
| GET | `live` | `{watched, frames, recent, approvals, chat}` |
| GET | `live/<task>/frame` | `image/jpeg` (ETag; 304 when unchanged), JSON `{placeholder}` for hidden pages, 204 when no frame. Reading marks the session as watching |
| POST | `live/<task>/reveal` | asks for `/livereveal CODE` in Telegram; nothing is revealed until then |
| GET | `library?collection=` | `{items, collections}` |
| GET | `library/<id>?part=N` | `{item: {text, next_offset, …}}` |
| POST | `library/upload?name=&collection=&use_chat=&use_packets=&pinned=` | body = file bytes (≤ 25 MB) → `{item}` |
| POST | `library/note` `{text, title, collection, pinned}` | `{item}` |
| POST | `library/<id>/update` `{collection, use_chat, use_packets, pinned, title}` | `{item}` |
| POST | `library/<id>/delete` | `{deleted}` |
| GET | `packets`, `packets/<slug>`, `builds`, `usage`, `machines`, `activity`, `settings`, `compose` | as in 0.24–0.27 |
| POST | `oauth/begin` `{provider}` | `{go}` to the provider's sign-in; the callback is `/assistant/dashboard/oauth/callback` |
| POST | `oauth/disconnect` `{grant}` | tokens deleted |

Activity summaries name tools and sites only: never mail bodies, transcripts, page text, typed values or labeled (health,
presence, camera) values. Live frames exist only in memory, only while someone watches, with password, card and one-time-code
fields masked; banks, payment sites and health portals show a placeholder.
