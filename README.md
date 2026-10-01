# Signal-OS — AI-Native Multimodal Intelligence Fusion Platform

**Signal-OS** is a modular OSINT (Open Source Intelligence) platform that transforms raw intelligence inputs into actionable correlations, timelines, and comprehensive reports through an orchestrated swarm of specialized agents.

## Features

### Input Types
- **Phone numbers** (E.164 + formatted) → carrier, region, line-type, breach exposure
- **Emails** → account existence enumeration across 50+ providers
- **Usernames** → identity resolution via Sherlock, people search, breach check
- **Photos** → EXIF extraction, face detection, reverse image search, geolocation
- **URLs/Domains** → live fetch, link extraction, email harvesting
- **IP addresses** → Shodan threat intel
- **Text** → stylometry fingerprinting
- **And more...**

### 15 Specialist Agents

| Agent | Role | Status |
|-------|------|--------|
| **SCOUT** ◎ | Search intelligence, dork generation, search federation | ✓ Live |
| **PHONOS** ☏ | Phone number workup (carrier, region, breaches) | ✓ Live |
| **EMAIL** ✉ | Email account existence enumeration | ✓ Live |
| **CRAWLER** ⟨/⟩ | Static web fetch + structured extraction | ✓ Live |
| **IRIS** ◉ | Vision: EXIF, face detection, reverse image search | ✓ Live |
| **PRISM** ◈ | Identity resolution: Sherlock, people search, breaches | ✓ Live |
| **TERRA** ⊛ | GEOINT: EXIF GPS, geolocation | ✓ Live |
| **ECHO** ~ | Audio metadata + optional Whisper transcription | ✓ Live |
| **INK** ✦ | Stylometry: writing fingerprinting | ✓ Live |
| **SIGMA** Σ | Threat intel: Shodan, breach analysis | ✓ Live |
| **NEXUS** ∞ | Knowledge graph: nodes, edges, clustering, influence | ✓ Live |
| **KRONOS** ⊕ | Timeline: event reconstruction from 11+ date formats | ✓ Live |
| **VAULT** □ | Memory: persistent entity store across cases | ✓ Live |
| **SENTINEL** ⊲ | Monitoring: baseline snapshots + change detection | ✓ Live |
| **QUILL** ⟁ | Intelligence reports: Markdown + optional PDF | ✓ Live |

### Output

Each investigation produces:
- **Knowledge graph** — nodes, edges, clusters, influence scoring
- **Timeline** — ordered events with dates, sources, context
- **Entity index** — all discovered identities, emails, accounts, locations
- **Signal tallies** — aggregated intelligence by type
- **Markdown report** — executive summary, agent breakdown, findings
- **Live stream** — WebSocket for real-time step updates

## Quick Start

### Local Development

```bash
# Clone the repo
git clone https://github.com/Matthew-Selvam/samaritan-os.git
cd samaritan-os

# Backend setup
cd backend
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Run the backend
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8766

# In another terminal, frontend setup
cd ..
npm install
npm run dev

# Open http://localhost:3000
```

### Docker Compose

```bash
docker-compose up -d
# Access at http://localhost:3000
```

### Vercel Deployment

Click the button below to deploy Signal-OS to Vercel in one click:

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FMatthew-Selvam%2Fsamaritan-os&env=NEXT_PUBLIC_BACKEND_URL,SECRET_KEY&project-name=signal-os&repo-name=samaritan-os)

Or manually:

```bash
npm install -g vercel
vercel
```

## Environment Variables

### Required
- `SECRET_KEY` — FastAPI secret key (set to a strong value in production)

### Optional OSINT Keys
- `SHODAN_API_KEY` — Shodan threat intel
- `HIBP_API_KEY` — Have I Been Pwned breach database
- `NUMVERIFY_API_KEY` — Phone carrier validation
- `DEHASHED_EMAIL` / `DEHASHED_API_KEY` — Breach exposure lookup

### Optional Infrastructure
- `DATABASE_URL` — PostgreSQL (for production storage)
- `REDIS_URL` — Redis (for caching)
- `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` — Neo4j graph database
- `QDRANT_URL` — Qdrant vector DB (for semantic search)

### OPSEC
- `OPSEC_ENABLED` — Enable Tor/OPSEC layer (default: `true`)
- `TOR_PROXY` — Tor proxy URL (default: `socks5h://127.0.0.1:9050`)
- `TOR_CONTROL` — Tor control port (default: `127.0.0.1:9051`)

## API Endpoints

### HTTP

```
POST /api/investigate
  body: { input: "...", input_type?: "...", case_id?: "..." }
  response: { inv_id, case_id, status }

GET /api/investigate/{inv_id}
  response: { status, report, agents, ... }

GET /api/investigations
  response: [{ inv_id, status, input }, ...]

POST /api/search
  body: { query, engines?: [...], use_dorks?: true }
  response: { query, agent, output, ... }

POST /api/photo-search
  body: FormData(file)
  response: { inv_id, case_id, ... }

GET /api/opsec/status
  response: { opsec_enabled, proxy, tor_connected, ... }

POST /api/opsec/newcircuit
  response: { success, ip, ... }

GET /api/health
  response: { status, platform, version }
```

### WebSocket

```
WS /ws/pipeline/{inv_id}
  events: { type: "step" | "done" | "error", ... }
```

## Architecture

### Modular Design

**Correlation Tier**
```
Primary Swarm (concurrent) → [SCOUT, PHONOS, EMAIL, CRAWLER, IRIS, ...]
                             ↓
Correlation Tier (sequential) → [NEXUS, KRONOS, VAULT, SENTINEL, QUILL]
```

- **Primary agents** run in parallel, each producing signals/entities
- **Correlation agents** run after collection, consuming aggregated outputs
- Graceful degradation: missing services = clean warnings, never crashes

**Configuration**
- Centralized `config.py` — all env vars, timeouts, feature flags
- Feature toggles for optional functionality
- Per-agent timeout configuration

**Connectors**
- Modular OSINT connectors in `/backend/connectors/`
- OPSEC layer with Tor support
- Graceful fallbacks for missing keys/services

## Security & Compliance

⚠️ **IMPORTANT: Authorized Use Only**

Signal-OS is designed for **authorized security research, penetration testing, and threat intelligence** within defined scopes. Users are solely responsible for ensuring they have proper authorization to:

- Investigate specific targets
- Collect personal information
- Monitor individuals or organizations
- Access protected data

**GDPR/CCPA Compliance**
- Audit logging of all investigations (optional)
- No automatic data persistence (uses file-backed stores by default)
- Easy purge of case memory via `vault_store/` and `sentinel_store/`

**OPSEC**
- Optional Tor routing for all HTTP requests
- Automatic circuit rotation
- No IP leaks via DNS

## Troubleshooting

### Backend won't start
```bash
# Check Python 3.10+
python --version

# Reinstall deps
pip install --upgrade -r requirements.txt

# Check port is free
lsof -i :8766
```

### "No module named X"
```bash
# Activate venv
source backend/.venv/bin/activate

# Reinstall
pip install -r backend/requirements.txt
```

### Agents returning empty results
- Check `.env` for API keys (HIBP, NumVerify, Dehashed, Shodan)
- Verify internet connectivity
- Check server logs for connector errors

### WebSocket not streaming
- Ensure backend is accessible at `NEXT_PUBLIC_BACKEND_URL`
- Check browser console for CORS errors
- Verify `/ws/pipeline/{id}` endpoint is running

## Development

### Adding a New Agent

1. Create class in `backend/agents/stubs.py` or a new file
2. Inherit from `BaseAgent`
3. Implement `async def run(input_data, context=None) -> AgentResult`
4. Emit `signals`, `entities`, and a structured `output`
5. Register in `backend/agents/__init__.py`
6. Update router if needed in `backend/router.py`

### Adding a New Connector

1. Create function in `backend/connectors/`
2. Use `OpsecClient` for OPSEC-routed requests
3. Return structured dict with explicit error keys
4. Never throw exceptions — degrade gracefully

### Running Tests

```bash
cd backend
pytest tests/
```

## Performance

- **Typical investigation**: 5-30 seconds (varies by input, live sources enabled)
- **Graph build**: O(n) entities, O(e) edges
- **Memory per case**: ~1 MB (includes agents' steps)
- **Concurrency**: 10+ simultaneous investigations on modest hardware

## Roadmap

- [ ] Browser automation (Playwright) for CRAWLER
- [ ] WhisperX integration for ECHO (audio transcription)
- [ ] PlantNet/BioCLIP for TERRA (visual geolocation)
- [ ] Qdrant vector store for VAULT (semantic memory)
- [ ] Dark web marketplace monitoring (SENTINEL)
- [ ] Multi-language support
- [ ] Case management UI (create, label, export)

## License

Proprietary — Educational & Authorized Use Only

## Support

For issues, questions, or contributions:

- GitHub Issues: [Matthew-Selvam/samaritan-os/issues](https://github.com/Matthew-Selvam/samaritan-os/issues)
- Email: skulptoutreach@gmail.com

---

**Signal-OS** — *Authorized Intelligence Fusion*
