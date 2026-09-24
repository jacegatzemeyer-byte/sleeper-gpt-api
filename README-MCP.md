# Sleeper Dynasty Advisor — REST + MCP

This service keeps the original REST endpoints and adds a Streamable HTTP MCP server for ChatGPT-compatible clients.

## MCP endpoint

After deployment, connect the client to:

`https://<your-railway-domain>/mcp`

Exposed read-only MCP tools:

- `getRoster`
- `getMatchup`
- `getFreeAgents`
- `getInjuries`
- `getBorisTiers`
- `getWaiverAnalysis`

The existing REST endpoints remain available at `/roster`, `/matchup`, `/free-agents`, `/injuries`, and `/boris-tiers`.

## Railway

The existing Railway deployment model still works. `Procfile.txt` now enables proxy headers so HTTPS redirects and forwarded host information work correctly behind Railway.

## Local run

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Health check: `GET http://localhost:8000/`
MCP endpoint: `http://localhost:8000/mcp`
