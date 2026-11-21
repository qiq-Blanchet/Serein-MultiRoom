# Serein local Codex Hook

This manually installed plugin adds opt-in memory recall and completed-turn archiving to local Codex CLI or desktop Codex chats. It does not store the Serein credential in its state database.

Set `SEREIN_BASE_URL` to the Serein instance root and `SEREIN_GATEWAY_KEY` to its Gateway Key in the environment that starts Codex. Install this folder through a local plugin marketplace, enable the plugin, then review and trust its three hooks with `/hooks` or **Settings > Hooks**.

In a local chat, send `serein:on` as a standalone message. That control message is blocked before model execution. Subsequent prompts recall Serein context. After a turn completes, the original user prompt and final assistant message are archived as a pair; interrupted or failed turns are not archived. Use `serein:status` or `serein:off` in the same way.

The plugin uses `PLUGIN_DATA/serein-hook.sqlite3` for per-session enablement, retryable pending turns, and the five-turn recall cooldown. Do not copy that runtime file into source control.

Plugin command hooks do not run in cloud-orchestrated ChatGPT Work, ChatGPT web, or mobile chats. Those surfaces can still use Serein's MCP tools, but this local automatic recall/archive loop is unavailable there.
