# Samaritan OS — Agent Context

**AI-Native Multimodal Intelligence Fusion Platform**

## What this is
An OSINT intelligence operating system. One dashboard, one search bar, one memory system, one entity graph — many agents and connectors.

One prompt in → AI router → multi-agent orchestration → tool connectors → signal extraction → correlation engine → knowledge graph → timeline → report → monitoring.

## Core Philosophy
- ONE DASHBOARD · ONE SEARCH BAR · ONE MEMORY SYSTEM · ONE ENTITY GRAPH
- MANY AGENTS · MANY CONNECTORS

## Target Use Cases
Cyber intelligence, public-data investigations, OSINT automation, threat intelligence, social media analysis, case management, research workflows.

## Stack
- **Frontend**: Next.js, TailwindCSS, shadcn/ui, Framer Motion, Cytoscape.js (graph), vis.js (timeline), MapLibre GL (maps)
- **Backend**: FastAPI, Celery, Redis, LangGraph orchestration
- **AI**: Local vLLM/Ollama (Qwen3 32B, DeepSeek V3) + cloud (Anthropic, OpenAI, DeepSeek)
- **DB**: PostgreSQL (relational) · Neo4j (graph) · Qdrant (vector) · MinIO (objects) · Redis (cache)
- **UI aesthetic**: Dark futuristic intelligence dashboard — Palantir/Maltego/SOC-inspired

## Agents
| Name | Role |
|------|------|
| SCOUT | Search — dorks, federation, query expansion |
| CRAWLER | Scraper — structured extraction, browser automation |
| PRISM | Social Intelligence — cross-platform identity resolution |
| IRIS | Vision — face clustering, object detection, OCR |
| ECHO | Audio — transcription, accent/environment analysis |
| TERRA | GEOINT — geolocation, environmental inference |
| INK | Stylometry — writing fingerprinting, authorship |
| NEXUS | Correlation — hidden relationship detection |
| KRONOS | Timeline — chronology reconstruction |
| VAULT | Memory — persistent entity memory, semantic summarization |
| SENTINEL | Monitoring — live public activity tracking |
| QUILL | Reporting — AI-generated intelligence summaries |
| SIGMA | Threat Intel — IOC correlation, breach analysis |
| APEX | Supervisor — master orchestrator, task routing |

## Brief
→ `~/Downloads/🎱.pdf`

## Status
POC scaffold. Building modular backend + Next.js dashboard.
