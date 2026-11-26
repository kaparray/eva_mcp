# Getting an EVA API token

The token is personal: it carries your permissions in EVA, and everything done through the MCP server is recorded
under your name. Do not share it or paste it into chats.

1. Sign in to your EVA instance, open the profile menu (your avatar) and go to your own profile page.
2. Open the **Security** tab and click **Generate API token**. Copy the token EVA shows.
   - Generating a new token usually revokes the previous one: anything that used the old token stops authenticating.
   - Do not confuse it with **Generate Webhook token**: that one is for webhooks and will not work with this server.
3. Save it. The easiest way is to run from the repository folder:

   ```bash
   ./install.sh
   ```

   The script asks for your EVA URL, stores the token (macOS: Keychain item `eva-api-token`; Linux/Windows:
   `~/.config/eva-mcp/token` with mode 600), checks it and registers the server in Claude Code.

   Manually on macOS (the command prompts for the token, so it does not end up in shell history):

   ```bash
   security add-generic-password -a "$USER" -s eva-api-token -w
   ```

4. Check: `python3 server.py --check` prints the account the token belongs to.

## If the token leaked

Generate a new one (step 2) and replace the stored one:

```bash
security delete-generic-password -a "$USER" -s eva-api-token
security add-generic-password -a "$USER" -s eva-api-token -w
```
