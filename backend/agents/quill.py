"""
quill.py — QUILL Agent (Report Generation)
===========================================

Compiles the whole investigation into an analyst-usable intelligence report
where **every claim is traceable to the signal or entity that produced it**.

Report structure (CONTRACTS §3 shapes, §12 quality bar):

1. Executive summary — deterministic facts, plus an optional LLM narrative that
   is explicitly labelled AI-generated analysis.
2. Key findings — each evidence-backed, with its source, confidence and the
   evidence chain behind it. Nothing appears here that no signal supports.
3. Entity appendix — resolved identities with aliases, clusters and influence.
4. Timeline appendix — dated events, tolerant of the field-name drift between
   KRONOS versions (``timestamp`` / ``ts`` / ``date``).
5. Agent contribution ledger — what each agent contributed **and what it could
   not determine**, including errors and explicit gaps.
6. Assessment — confidence plus the specific evidence that would raise it.
7. Gaps & recommended next steps — OSINT pivots derived from what was actually
   found (and from what was missing), not boilerplate.
8. Chain of custody / methodology — inputs, agents, timing, and the provenance
   model behind every number in the report.

Formats: ``markdown`` (canonical), ``json`` (structured), and ``pdf`` — real
ReportLab PDF when available, otherwise a clean self-contained printable HTML
document. Rendering never raises: a failed render is reported, not fatal.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from .base import BaseAgent, AgentResult

#: Wall-clock ceiling for the optional LLM executive summary.
#:
#: This is an *advisory* add-on, not a required section: the report is complete
#: without it. It therefore gets a tight budget rather than the agent's own
#: (much larger) timeout. The previous 90s default was sized for a slow local
#: model, which meant one unresponsive provider stalled a serial pipeline for
#: well over a minute — far past the platform's own 5-30s target. Override
#: with QUILL_LLM_TIMEOUT_S when using a deliberately slow local model.
try:  # pragma: no cover - env parsing
    LLM_TIMEOUT_S = float(os.environ.get("QUILL_LLM_TIMEOUT_S", "20"))
except (TypeError, ValueError):  # pragma: no cover
    LLM_TIMEOUT_S = 20.0

#: Master switch for the advisory LLM executive summary. The report is fully
#: written without it; the model only adds a clearly-labelled narrative on top.
#: Operators needing minimum latency (or running a slow local model) set this
#: false and pay nothing; every deterministic section is unchanged.
try:  # pragma: no cover - env parsing
    ADVISORY_LLM = os.environ.get("SIGNAL_OS_ADVISORY_LLM", "true").casefold() \
        not in ("0", "false", "no", "off")
except Exception:  # pragma: no cover
    ADVISORY_LLM = True

_STATUS_MARK = {"done": "✓", "error": "✗", "partial": "◐", "skipped": "⊘",
                "stub": "◌"}


def _esc(value: Any, limit: int = 200) -> str:
    """Escape a value for safe inclusion in Markdown / HTML output.

    Args:
        value: Arbitrary value.
        limit: Maximum length before truncation.

    Returns:
        str: Escaped single-line string.
    """
    s = " " + str(value if value is not None else "").strip() + " "
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
    s = re.sub(r"[*`#<>]", "", s)
    return s.strip()[:limit]


def _html_esc(value: Any) -> str:
    """Escape a value for HTML output.

    Args:
        value: Arbitrary value.

    Returns:
        str: HTML-escaped text.
    """
    return _html.escape(str(value if value is not None else ""), quote=True)


def _conf(value: Any) -> float:
    """Coerce a confidence value to a float in ``0.0``–``1.0``.

    Args:
        value: Candidate confidence.

    Returns:
        float: Clamped confidence.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return max(0.0, min(1.0, v))


def _event_date(ev: dict) -> str:
    """Read a timeline event's date tolerantly across agent versions.

    KRONOS has emitted ``timestamp``, ``ts`` and ``date`` at different points;
    reading defensively keeps the appendix working with whichever shape the
    running agent produced.

    Args:
        ev: Timeline event dict.

    Returns:
        str: Date prefix (first 10 chars) or ``"undated"``.
    """
    for key in ("timestamp", "ts", "date", "when", "datetime"):
        val = ev.get(key)
        if val:
            return str(val)[:10]
    return "undated"


def _event_time(ev: dict) -> str:
    """Read a timeline event's full timestamp tolerantly.

    Args:
        ev: Timeline event dict.

    Returns:
        str: Full timestamp, or ``""`` when absent.
    """
    for key in ("timestamp", "ts", "date", "when", "datetime"):
        val = ev.get(key)
        if val:
            return str(val)
    return ""


class QuillAgent(BaseAgent):
    """Report Generation — evidence-traceable intelligence brief.

    Fully deterministic by construction: every section is derived from the
    agent results, so a report is always produced even with no LLM, no network
    and no connectors. The LLM pass only ever *adds* a clearly-labelled
    narrative on top; it can never introduce a fact the signals do not support.
    """

    name = "QUILL"
    role = "Report Generation"
    icon = "⟁"
    description = ("AI-generated intelligence summaries, evidence-traceable "
                   "reporting, PDF reports, evidence bundles, graph exports")
    preferred_models = ["qwen2.5vl:7b", "gemma2:9b", "claude-opus-4"]
    token_budget = 16384

    # ── Finding extraction ──────────────────────────────────────────────────
    @staticmethod
    def _findings(signals: list[dict], graph: dict, entities: list[dict],
                  agent_outputs: dict[str, dict]) -> list[dict]:
        """Build evidence-backed findings from the raw investigation data.

        Each finding records the source that produced it, so the report can
        cite evidence instead of asserting conclusions.

        Args:
            signals: Aggregated peer signals.
            graph: NEXUS output.
            entities: Aggregated peer entities.
            agent_outputs: ``agent -> output`` map from the agent results.

        Returns:
            list[dict]: Findings with ``claim``, ``source``, ``confidence`` and
            ``evidence``.
        """
        findings: list[dict] = []
        by_type: dict[str, list[dict]] = defaultdict(list)
        for s in signals or []:
            if isinstance(s, dict):
                by_type[str(s.get("type", "unknown"))].append(s)

        def add(claim: str, source: str, confidence: float, evidence: list[str],
                severity: str = "info") -> None:
            findings.append({
                "claim": claim, "source": source,
                "confidence": round(_conf(confidence), 3),
                "evidence": evidence[:6], "severity": severity,
            })

        accounts = by_type.get("account", [])
        if accounts:
            plats = sorted({str(a.get("platform") or a.get("attrs", {}).get("platform")
                               or "unknown") for a in accounts})
            add(f"{len(accounts)} account(s) attributed to the target across "
                f"{len(plats)} platform(s): {', '.join(plats[:8])}",
                "sherlock", max((_conf(a.get("confidence")) for a in accounts),
                                default=0.6),
                [f"{a.get('source', '?')}: {a.get('url') or a.get('value')}"
                 for a in accounts[:5]], "high")

        breaches = by_type.get("breach", [])
        if breaches:
            names = sorted({str(b.get("name") or b.get("value") or "unnamed")
                            for b in breaches})
            add(f"Exposure in {len(breaches)} known data breach(es): "
                f"{', '.join(names[:6])}",
                str(breaches[0].get("source", "hibp")),
                max((_conf(b.get("confidence")) for b in breaches), default=0.7),
                [f"{b.get('name') or b.get('value')} "
                 f"({b.get('date') or 'date unknown'})" for b in breaches[:5]],
                "high")

        gps = by_type.get("gps", []) + by_type.get("geolocation", [])
        if gps:
            add(f"{len(gps)} geolocation signal(s) recovered from media metadata",
                str(gps[0].get("source", "exif")),
                max((_conf(g.get("confidence")) for g in gps), default=0.6),
                [_esc(g.get("address") or f"{g.get('lat')}, {g.get('lon')}", 80)
                 for g in gps[:5]])

        matches = by_type.get("face_match", []) + by_type.get("image_match", [])
        if matches:
            add(f"{len(matches)} media/face match(es) found via reverse search",
                str(matches[0].get("source", "reverse_image")),
                max((_conf(m.get("confidence")) for m in matches), default=0.55),
                [f"{m.get('url') or m.get('value')}" for m in matches[:5]])

        for c in (graph.get("contradictions") or [])[:8]:
            add(f"Source conflict on {_esc(c.get('field'), 40)}: "
                f"{_esc(c.get('detail'), 160)}",
                "nexus", _conf(c.get("confidence")),
                [f"{v.get('value')} [{v.get('source')}]"
                 for v in (c.get("values") or [])], "high")

        ids = (graph.get("identities") or [])
        real_ids = [i for i in ids if len(i.get("members", [])) > 1]
        if real_ids:
            add(f"{len(real_ids)} identity/identities resolved from multiple "
                f"records, collapsing variant spellings to canonical identities",
                "nexus", 0.8,
                [f"{i.get('canonical_id')}: {len(i.get('members', []))} records, "
                 f"aliases {i.get('aliases', [])[:3]}" for i in real_ids[:5]])

        clusters = graph.get("clusters") or []
        if clusters:
            big = max(clusters, key=lambda c: len(c.get("members", [])))
            if len(big.get("members", [])) > 1:
                add(f"Correlation produced {len(clusters)} community/communities; "
                    f"largest links {len(big['members'])} entities "
                    f"({_esc(big.get('label'), 60)})",
                    "nexus", 0.7,
                    [f"cohesion {big.get('cohesion')}"], "medium")

        for top in (graph.get("top_entities") or [])[:3]:
            if _conf(top.get("influence")) >= 0.6:
                add(f"'{_esc(top.get('label'), 60)}' is a central node in the "
                    f"correlation graph (influence {_conf(top.get('influence')):.2f}, "
                    f"{top.get('degree')} direct links)",
                    "nexus", 0.6,
                    [f"cluster {top.get('cluster') or 'n/a'}"], "medium")

        prism_out = agent_outputs.get("PRISM") or {}
        for cl in (prism_out.get("account_clusters") or [])[:4]:
            if cl.get("size", 0) > 1:
                add(f"{cl.get('size')} account(s) on "
                    f"{', '.join(cl.get('platforms', [])[:4])} cluster to one "
                    f"person (confidence {cl.get('confidence')})",
                    "prism", _conf(cl.get("confidence")),
                    cl.get("evidence", [])[:4], "high")
        profile = (prism_out.get("person_profile") or {})
        for field, data in list((profile.get("fields") or {}).items())[:6]:
            conf = _conf(data.get("confidence"))
            if conf >= 0.5:
                add(f"Profile field '{field}': {_esc(data.get('value'), 80)}",
                    "prism", conf, [f"reported by {s}" for s in data.get("sources", [])])

        phonos = (agent_outputs.get("PHONOS") or {}).get("offline") or {}
        if phonos.get("valid"):
            bits = [b for b in (phonos.get("carrier"), phonos.get("line_type"),
                                phonos.get("location") or phonos.get("region"))
                    if b]
            add(f"Phone number validates ({', '.join(str(b) for b in bits)})",
                "phonos", 0.8, ["libphonenumber offline parse"])

        if not findings:
            add("No corroborating public-source signals were recovered for this "
                "target; absence of evidence is reported as such rather than "
                "inferred away", "quill", 0.3, [], "info")
        order = {"high": 0, "medium": 1, "info": 2}
        findings.sort(key=lambda f: (order.get(f.get("severity"), 3),
                                     -f.get("confidence", 0)))
        return findings

    # ── Agent ledger ────────────────────────────────────────────────────────
    @staticmethod
    def _ledger(agent_results: list[dict], findings: list[dict]) -> list[dict]:
        """Build the agent contribution ledger.

        For each agent this records what it contributed *and* what it could not
        determine — the second half is what stops a report implying coverage
        that was never achieved.

        Args:
            agent_results: Serialised agent results.
            findings: Extracted findings, used to attribute contributions.

        Returns:
            list[dict]: Ledger entries per agent.
        """
        by_agent_findings: dict[str, list[dict]] = defaultdict(list)
        for f in findings:
            by_agent_findings[str(f.get("source", "?"))].append(f)
        ledger: list[dict] = []
        for r in agent_results or []:
            name = str(r.get("agent", "?"))
            out = r.get("output") or {}
            role = r.get("role", "")
            contributed: list[str] = []
            if isinstance(out, dict):
                if out.get("nodes") or out.get("edges"):
                    contributed.append(f"correlation graph ({len(out.get('nodes', []))} "
                                       f"nodes, {len(out.get('edges', []))} edges)")
                if out.get("contradictions"):
                    contributed.append(f"{len(out['contradictions'])} contradiction(s)")
                if out.get("clusters"):
                    contributed.append(f"{len(out['clusters'])} cluster(s)")
                if out.get("account_clusters"):
                    contributed.append(f"{len(out['account_clusters'])} identity cluster(s)")
                if out.get("events") is not None and out.get("events") is not False:
                    contributed.append(f"{len(out.get('events') or [])} timeline event(s)")
                if out.get("person_profile"):
                    contributed.append("person profile with per-field confidence")
                if out.get("candidates"):
                    contributed.append(f"{len(out['candidates'])} ranked name candidate(s)")
                if out.get("signals") or out.get("breaches"):
                    contributed.append("breach exposure record")
                # A connector that validated an identifier is a real
                # contribution even when it returns no list-shaped payload.
                offline = out.get("offline") if isinstance(out.get("offline"), dict) else {}
                if offline:
                    bits = [str(b) for b in (offline.get("carrier"),
                                             offline.get("line_type"),
                                             offline.get("location")
                                             or offline.get("region")) if b]
                    contributed.append(
                        "identifier validated"
                        + (f" ({', '.join(bits)})" if bits else ""))
                if out.get("valid") is True:
                    contributed.append("identifier validated")
            sig_count = len(r.get("signals") or [])
            ent_count = len(r.get("entities_found") or [])
            if sig_count or ent_count:
                contributed.append(f"{sig_count} signal(s), {ent_count} entit(ies)")
            not_determined: list[str] = []
            if r.get("status") in ("error", "partial"):
                not_determined.append(f"status={r.get('status')}"
                                      + (f" — {r.get('error')}" if r.get("error") else ""))
            if not contributed:
                not_determined.append("no structured contribution")
            ledger.append({
                "agent": name,
                "role": role,
                "status": r.get("status", "?"),
                "contributed": contributed or ["nothing structured"],
                "not_determined": not_determined or [
                    "no gaps flagged by this agent"],
                "confidence": _conf(r.get("confidence")),
                "latency_s": r.get("latency_s", 0),
                "findings_attributed": len(by_agent_findings.get(name, [])),
            })
        return ledger

    # ── Next steps ──────────────────────────────────────────────────────────
    @staticmethod
    def _next_steps(signals: list[dict], graph: dict, entities: list[dict],
                    input_type: str, agent_results: list[dict]) -> list[dict]:
        """Derive specific OSINT pivots from what was actually found.

        Each recommendation names the evidence that prompted it, so the analyst
        can see why it is being suggested.

        Args:
            signals: Aggregated peer signals.
            graph: NEXUS output.
            entities: Aggregated peer entities.
            input_type: Detected input type.
            agent_results: Serialised agent results.

        Returns:
            list[dict]: Recommendations with ``action``, ``rationale`` and
            ``priority``.
        """
        steps: list[dict] = []
        types = {str(s.get("type")) for s in (signals or []) if isinstance(s, dict)}
        entity_types = {str(e.get("type")) for e in (entities or [])
                        if isinstance(e, dict)}
        agents_run = {str(r.get("agent")) for r in (agent_results or [])}

        if not types - {"correlation_summary"}:
            steps.append({
                "action": "Re-run against a different identifier: the seed "
                          "target produced no signals at all",
                "rationale": "no signal types were collected for this target",
                "priority": "high",
            })
        if "breach" in types:
            emails = [s for s in signals if s.get("type") == "breach"]
            domains = sorted({str(v) for v in entity_types if v == "domain"})
            steps.append({
                "action": "Pivot the leaked identifiers: attempt credential "
                          "reuse across the platforms found in section 1, and "
                          "check every exposed address against the breach set",
                "rationale": f"{len(emails)} breach record(s) expose contact "
                             f"identifiers"
                             + (f"; {len(domains)} domain(s) in play" if domains else ""),
                "priority": "high",
            })
        if "account" in types:
            plats = sorted({str(s.get("platform") or "") for s in signals
                            if s.get("type") == "account" and s.get("platform")})
            steps.append({
                "action": f"Cross-check the {len(plats)} discovered account(s) "
                          f"({', '.join(plats[:5])}) for mutual follows, shared "
                          "media and reused avatar hashes to widen the cluster",
                "rationale": "account clustering is currently limited to "
                             "handle/avatar/bio evidence",
                "priority": "medium",
            })
        held = [c for c in (graph.get("merge_candidates") or [])
                if not c.get("merged")][:5]
        if held:
            steps.append({
                "action": f"Adjudicate {len(held)} held-back identity link(s) "
                          "with additional evidence before treating them as one person",
                "rationale": "candidate links scored above the soft threshold but "
                             "below the merge threshold; they are the highest-value "
                             "remaining resolution work",
                "priority": "high",
            })
        contradictions = graph.get("contradictions") or []
        if contradictions:
            fields = sorted({str(c.get("field")) for c in contradictions})
            steps.append({
                "action": f"Resolve the conflicting {', '.join(fields)} attribute(s) "
                          "by querying a source independent of the disagreeing agents",
                "rationale": f"{len(contradictions)} contradiction(s) mean the "
                             "current profile carries conflicting facts",
                "priority": "high",
            })
        singletons = [a for a in (graph.get("anomalies") or [])
                      if a.get("type") == "single_source"]
        if singletons:
            steps.append({
                "action": f"Corroborate the {len(singletons)} single-source "
                          "entit(ies) before relying on them",
                "rationale": "an entity seen by only one source is a lead, not a fact",
                "priority": "medium",
            })
        if "gps" in types or "geolocation" in types:
            steps.append({
                "action": "Pivot the recovered coordinates against historical "
                          "imagery and geocoding to date and name the locations",
                "rationale": "media geolocation is present and can be narrowed "
                             "to specific places and times",
                "priority": "medium",
            })
        if input_type == "person_name":
            steps.append({
                "action": "Disambiguate the person before any further collection: "
                          "add a location, employer or age to narrow the candidate set",
                "rationale": "a bare name matches many unrelated people; this "
                             "report ranks candidates but cannot choose between them",
                "priority": "high",
            })
        for agent, why in (("PRISM", "no account clustering was produced"),
                           ("NEXUS", "no graph was produced"),
                           ("KRONOS", "no dated events were reconstructed"),
                           ("SENTINEL", "no change/hash analysis was produced")):
            if agent not in agents_run:
                steps.append({
                    "action": f"{agent} did not run ({why}); re-run the "
                              "investigation to close that coverage gap",
                    "rationale": "agent absent from the run ledger",
                    "priority": "medium",
                })
        if not steps:
            steps.append({
                "action": "No pivots outstanding: coverage was complete and no "
                          "leads were left unresolved",
                "rationale": "all agents ran, signals were collected and no "
                             "contradictions or unmerged candidates remain",
                "priority": "low",
            })
        return steps

    # ── LLM executive summary ───────────────────────────────────────────────
    async def _llm_summary(self, target: str, input_type: str, by_type: dict,
                           findings: list[dict], timeline: dict, graph: dict
                           ) -> dict[str, Any]:
        """Generate an executive-summary narrative, clearly labelled as AI output.

        The model is given only facts already established in this report and is
        instructed not to introduce new ones, so a hallucinated detail cannot
        masquerade as a finding.

        Args:
            target: Investigation target.
            input_type: Detected input type.
            by_type: Signal tally.
            findings: Extracted findings.
            timeline: KRONOS output.
            graph: NEXUS output.

        Returns:
            dict: ``{"status", "text", "error", "model"}``.
        """
        out: dict[str, Any] = {"status": "skipped", "text": "", "error": None,
                               "model": None}
        if not findings:
            out["status"] = "skipped_no_findings"
            return out
        try:
            fact_lines = "\n".join(
                f"- [{f.get('severity')}] {f.get('claim')} "
                f"(source: {f.get('source')}, confidence: {f.get('confidence')})"
                for f in findings[:12])
            span = (timeline or {}).get("span") or {}
            prompt = (
                "You are an intelligence analyst writing the executive summary of a "
                "finished OSINT report.\n"
                f"Target: {target[:120]}\n"
                f"Input type: {input_type}\n"
                f"Signal counts: {json.dumps(by_type, ensure_ascii=False)}\n"
                f"Timeline: {span.get('earliest', 'n/a')} → {span.get('latest', 'n/a')}"
                f" ({len((timeline or {}).get('events') or [])} events)\n"
                f"Graph: {len((graph or {}).get('nodes') or [])} nodes, "
                f"{len((graph or {}).get('edges') or [])} edges, "
                f"{len((graph or {}).get('clusters') or [])} clusters\n\n"
                "ESTABLISHED FACTS (this is the only evidence you may use):\n"
                f"{fact_lines}\n\n"
                "Write a 3-5 sentence analyst executive summary of what the "
                "evidence shows and how far it can be trusted. Do NOT introduce "
                "any fact, name, number or conclusion that is not in the list "
                "above. If the evidence is thin, say the assessment is limited."
            )
            kwargs = {"max_tokens": 500, "temperature": 0.2}
            fn = getattr(self, "llm", None)
            if callable(fn):
                res = await asyncio.wait_for(fn(prompt, **kwargs),
                                             timeout=LLM_TIMEOUT_S)
            else:
                from llm import get_llm  # lazy: llm.py is a runtime dep
                res = await asyncio.wait_for(get_llm().complete(prompt, **kwargs),
                                             timeout=LLM_TIMEOUT_S)
            error = getattr(res, "error", None)
            text = getattr(res, "text", "") or ""
            if error or not text.strip():
                out["status"] = "unavailable"
                out["error"] = str(error or "empty response")[:200]
                return out
            out["status"] = "ok"
            out["text"] = text.strip()
            out["model"] = getattr(res, "model", None)
        except asyncio.TimeoutError:
            out["status"] = "timeout"
            out["error"] = f"llm summary exceeded {LLM_TIMEOUT_S}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out["status"] = "error"
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out

    # ── Entry point ─────────────────────────────────────────────────────────
    async def run(self, input_data, context=None):
        """Compile the investigation into markdown, JSON and PDF/HTML reports.

        Args:
            input_data: Investigation target.
            context: Pipeline context. Uses ``peer_signals``, ``peer_entities``,
                ``agent_results``, ``input_type``, ``case_id``, ``report_dir``,
                ``format`` and ``deep``.

        Returns:
            AgentResult: ``output`` always contains the original ``markdown``,
            ``signal_tally``, ``event_count``, ``entity_count``, ``pdf_path`` and
            ``generated_at`` keys, plus ``findings``, ``report`` (json), ``html``
            and ``formats``. Degradation is reported in ``output['errors']``.
        """
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = [s for s in (context.get("peer_signals") or [])
                                   if isinstance(s, dict)]
        peer_entities: list[dict] = [e for e in (context.get("peer_entities") or [])
                                     if isinstance(e, dict)]
        agent_results: list[dict] = [r for r in (context.get("agent_results") or [])
                                     if isinstance(r, dict)]
        input_type = str(context.get("input_type") or "unknown")
        case_id = str(context.get("case_id") or "—")
        errors: list[str] = []

        self.log(f"generating intelligence report for {input_type} target")
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # ── Signal tallies ──────────────────────────────────────────────────
        by_type: dict[str, int] = {}
        for s in peer_signals:
            key = str(s.get("type", "unknown"))
            by_type[key] = by_type.get(key, 0) + 1

        graph: dict = next((r.get("output") or {} for r in agent_results
                            if r.get("agent") == "NEXUS"), {}) or {}
        timeline: dict = next((r.get("output") or {} for r in agent_results
                               if r.get("agent") == "KRONOS"), {}) or {}
        agent_outputs: dict[str, dict] = {}
        for r in agent_results:
            out = r.get("output")
            if isinstance(out, dict):
                agent_outputs[str(r.get("agent"))] = out

        completed = [r for r in agent_results if r.get("status") == "done"]
        errored = [r for r in agent_results if r.get("status") in ("error", "partial")]

        # ── Evidence-backed findings ────────────────────────────────────────
        try:
            findings = self._findings(peer_signals, graph, peer_entities, agent_outputs)
        except Exception as exc:
            findings = []
            errors.append(f"findings_error: {exc}"[:200])
            self.log(errors[-1])

        try:
            ledger = self._ledger(agent_results, findings)
        except Exception as exc:
            ledger = []
            errors.append(f"ledger_error: {exc}"[:200])
            self.log(errors[-1])

        try:
            next_steps = self._next_steps(peer_signals, graph, peer_entities,
                                          input_type, agent_results)
        except Exception as exc:
            next_steps = []
            errors.append(f"next_steps_error: {exc}"[:200])
            self.log(errors[-1])

        # ── Optional LLM executive summary (additive, clearly labelled) ─────
        llm_report: dict[str, Any] = {"status": "disabled", "text": "",
                                      "error": None, "model": None}
        if not context.get("disable_llm") and ADVISORY_LLM:
            try:
                llm_report = await self._llm_summary(target, input_type, by_type,
                                                     findings, timeline, graph)
                self.log(f"llm executive summary: {llm_report.get('status')}")
                if llm_report.get("status") not in ("ok", "skipped", "skipped_no_findings"):
                    errors.append(f"llm_summary: {llm_report.get('error')}"[:200])
            except Exception as exc:
                llm_report = {"status": "error", "text": "",
                              "error": f"{type(exc).__name__}: {exc}"[:200],
                              "model": None}
                errors.append(f"llm_error: {exc}"[:200])
                self.log(errors[-1])

        # ── Assessment ──────────────────────────────────────────────────────
        agent_confs = [_conf(r.get("confidence")) for r in agent_results
                        if r.get("status") == "done"]
        ev_confs = [f["confidence"] for f in findings if f.get("confidence")]
        base_conf = sum(agent_confs) / len(agent_confs) if agent_confs else 0.0
        ev_conf = sum(ev_confs) / len(ev_confs) if ev_confs else 0.0
        multi_source = sum(1 for e in peer_entities
                           if len(e.get("sources") or []) > 1) if peer_entities else 0
        assessment_conf = round(min(0.95, 0.45 * base_conf + 0.4 * ev_conf
                                    + 0.15 * min(1.0, multi_source / 3)), 3)
        if (graph.get("contradictions") or []):
            assessment_conf = round(assessment_conf * 0.85, 3)
        if not peer_signals:
            assessment_conf = min(assessment_conf, 0.2)
        assessment_conf = round(assessment_conf, 3)

        raise_conf: list[str] = []
        for c in (graph.get("contradictions") or [])[:3]:
            raise_conf.append(f"resolve the {c.get('field')} conflict "
                              f"({c.get('detail', '')[:80]}) with an independent source")
        if multi_source == 0 and peer_entities:
            raise_conf.append("obtain a second independent source for the "
                              f"{len(peer_entities)} entity/entities — none is "
                              "currently corroborated")
        if not peer_signals:
            raise_conf.append("collect any signal at all: this investigation "
                              "recovered none from public sources")
        for single in [a for a in (graph.get("anomalies") or [])
                       if a.get("type") == "single_source"][:3]:
            raise_conf.append(f"corroborate '{single.get('entity_label', '')[:50]}' "
                              f"(seen only by {single.get('source', '?')})")
        if not raise_conf:
            raise_conf.append("confidence is already limited by source quality "
                              "rather than coverage; further pivots would add "
                              "volume, not certainty")

        # ── Markdown report ─────────────────────────────────────────────────
        md = self._render_markdown(
            target=target, input_type=input_type, case_id=case_id,
            generated=generated, by_type=by_type, graph=graph, timeline=timeline,
            peer_entities=peer_entities, agent_results=agent_results,
            findings=findings, ledger=ledger, next_steps=next_steps,
            assessment_conf=assessment_conf, raise_conf=raise_conf,
            llm_report=llm_report, errors=errors,
        )

        # ── Structured JSON report ──────────────────────────────────────────
        report_json = {
            "case_id": case_id,
            "target": target,
            "input_type": input_type,
            "generated_at": generated,
            "executive_summary": {
                "ai_generated": llm_report.get("text") or "",
                "ai_model": llm_report.get("model"),
                "ai_status": llm_report.get("status"),
                "deterministic": self._summary(target, input_type, by_type,
                                               graph, timeline, agent_results),
            },
            "key_findings": findings,
            "assessment": {
                "confidence": assessment_conf,
                "basis": {
                    "mean_agent_confidence": round(base_conf, 3),
                    "mean_finding_confidence": round(ev_conf, 3),
                    "corroborated_entities": multi_source,
                    "contradictions": len(graph.get("contradictions") or []),
                },
                "would_raise_confidence": raise_conf,
            },
            "entities": [
                {"id": n.get("id"), "label": n.get("label"), "type": n.get("type"),
                 "confidence": _conf(n.get("confidence")),
                 "aliases": n.get("aliases", []), "cluster": n.get("cluster")
                 or n.get("group") or "", "influence": _conf(n.get("influence")),
                 "sources": n.get("sources", [])}
                for n in (graph.get("nodes") or [])
            ],
            "timeline": [
                {"ts": _event_time(ev), "date": _event_date(ev),
                 "label": ev.get("label", ""),
                 "description": ev.get("description", ""),
                 "source": ev.get("source", ""),
                 "confidence": _conf(ev.get("confidence")),
                 "entities": ev.get("entities", [])}
                for ev in (timeline.get("events") or [])
            ],
            "agent_ledger": ledger,
            "gaps_and_next_steps": next_steps,
            "graph": {"nodes": graph.get("nodes", []), "edges": graph.get("edges", []),
                      "clusters": graph.get("clusters", []),
                      "contradictions": graph.get("contradictions", []),
                      "anomalies": graph.get("anomalies", [])},
            "signal_tally": by_type,
            "methodology": self._methodology(agent_results, by_type),
            "errors": errors,
        }

        # ── PDF / printable HTML ────────────────────────────────────────────
        report_dir = context.get("report_dir")
        pdf_path: Optional[str] = None
        html_path: Optional[str] = None
        render_format = str(context.get("format") or "markdown").casefold()
        html_doc = self._render_html(md, target, case_id, generated)
        if report_dir:
            try:
                html_path = self._write_html(html_doc, report_dir, case_id)
            except Exception as exc:
                errors.append(f"html_render_error: {exc}"[:200])
                self.log(errors[-1])
            try:
                pdf_path = self._render_pdf(md, report_dir, case_id)
                if pdf_path:
                    self.log(f"PDF written: {pdf_path}")
            except Exception as exc:
                errors.append(f"pdf_render_error: {exc}"[:200])
                self.log(errors[-1])
        else:
            # No directory configured: still prove the renderers work so a
            # report request can never fail on a rendering dependency.
            try:
                probe = self._render_pdf(md, "", case_id, in_memory=True)
                render_format = "pdf" if probe else "html"
            except Exception:
                render_format = "html"

        self.log(f"report ready — {len(peer_signals)} signals, "
                 f"{len(timeline.get('events') or [])} events, "
                 f"{len(findings)} finding(s), {len(md)} chars")

        return AgentResult(
            agent=self.name,
            status="partial" if errors and not findings else "done",
            output={
                # ── keys preserved from the previous implementation ────────
                "markdown": md,
                "signal_tally": by_type,
                "event_count": len(timeline.get("events") or []),
                "entity_count": len(peer_entities),
                "pdf_path": pdf_path,
                "generated_at": generated,
                # ── new reporting product ───────────────────────────────────
                "report": report_json,
                "findings": findings,
                "agent_ledger": ledger,
                "gaps_and_next_steps": next_steps,
                "assessment": report_json["assessment"],
                "html": html_doc,
                "html_path": html_path,
                "formats": {
                    "markdown": True,
                    "json": True,
                    "pdf": bool(pdf_path),
                    "html_fallback": bool(html_path),
                    "rendered": render_format,
                },
                "ai_summary": {
                    "present": bool(llm_report.get("text")),
                    "status": llm_report.get("status"),
                    "model": llm_report.get("model"),
                    "disclaimer": "AI-generated analysis, not a corroborated fact",
                },
                "errors": errors,
            },
            confidence=round(assessment_conf if findings else 0.4, 3),
            reasoning=(f"Compiled brief covering {len(agent_results)} agents, "
                      f"{len(peer_signals)} signals, "
                      f"{len(timeline.get('events') or [])} timeline events; "
                      f"{len(findings)} evidence-backed finding(s), "
                      f"{len(ledger)} ledger entries, {len(next_steps)} next step(s); "
                      f"assessment confidence {assessment_conf:.0%}"
                      + (f"; {len(errors)} issue(s)" if errors else "")),
            signals=[{
                "type": "report", "format": "markdown",
                "length": len(md), "pdf": bool(pdf_path), "source": "quill",
                "confidence": round(assessment_conf, 3),
            }],
            latency_s=self._elapsed(t0),
        )

    # ── Renderers ───────────────────────────────────────────────────────────
    def _render_markdown(self, *, target: str, input_type: str, case_id: str,
                         generated: str, by_type: dict, graph: dict, timeline: dict,
                         peer_entities: list[dict], agent_results: list[dict],
                         findings: list[dict], ledger: list[dict],
                         next_steps: list[dict], assessment_conf: float,
                         raise_conf: list[str], llm_report: dict,
                         errors: list[str]) -> str:
        """Render the canonical Markdown intelligence report.

        Args:
            target: Investigation target.
            input_type: Detected input type.
            case_id: Case identifier.
            generated: Human-readable generation timestamp.
            by_type: Signal tally.
            graph: NEXUS output.
            timeline: KRONOS output.
            peer_entities: Aggregated entities.
            agent_results: Serialised agent results.
            findings: Evidence-backed findings.
            ledger: Agent contribution ledger.
            next_steps: Recommended pivots.
            assessment_conf: Overall assessment confidence.
            raise_conf: What would raise confidence.
            llm_report: LLM executive-summary result.
            errors: Non-fatal issues encountered.

        Returns:
            str: The full Markdown report.
        """
        L: list[str] = [
            f"# Intelligence Report — {_esc(target, 80)}",
            "",
            f"- **Case ID:** `{case_id}`",
            f"- **Input type:** {input_type}",
            f"- **Generated:** {generated}",
            f"- **Agents completed:** {len([r for r in agent_results if r.get('status') == 'done'])}"
            + (f" · **degraded/errored:** "
               f"{len([r for r in agent_results if r.get('status') in ('error', 'partial')])}"
               if any(r.get("status") in ("error", "partial") for r in agent_results) else ""),
            "",
            "## 1. Executive Summary",
            "",
        ]

        # Deterministic summary first — it is the part that is always true.
        L += [self._summary(target, input_type, by_type, graph, timeline, agent_results),
              ""]
        if llm_report.get("text"):
            L += ["> **AI-generated analysis** _(model: "
                  f"{llm_report.get('model') or 'unknown'})_ — the following "
                  "narrative was written by a language model from the findings "
                  "above. It is analysis, not corroborated fact; verify each "
                  "claim against the evidence cited in section 2.",
                  ">",
                  "> " + llm_report["text"].replace("\n", "\n> "),
                  ""]

        L += ["## 2. Key Findings", ""]
        if findings:
            for i, f in enumerate(findings, 1):
                L.append(f"**F{i}.** {f['claim']}")
                L.append(f"  - _Source:_ `{f.get('source', '?')}` · "
                         f"_Confidence:_ {f.get('confidence', 0):.0%} · "
                         f"_Severity:_ {f.get('severity', 'info')}")
                if f.get("evidence"):
                    L.append("  - _Evidence:_")
                    for ev in f["evidence"]:
                        L.append(f"    - {_esc(ev, 140)}")
                L.append("")
        else:
            L += ["_No findings could be evidenced from the collected data._", ""]

        L += ["## 3. Entity Appendix", ""]
        nodes = graph.get("nodes") or []
        if nodes:
            L += ["| Entity | Type | Conf | Sources | Aliases | Cluster |",
                  "|---|---|---|---|---|---|"]
            for n in sorted(nodes, key=lambda x: -_conf(x.get("influence")))[:40]:
                L.append(f"| {_esc(n.get('label'), 50)} | {n.get('type', '?')} "
                         f"| {_conf(n.get('confidence')):.0%} "
                         f"| {len(n.get('sources', []) or [])} "
                         f"| {len(n.get('aliases', []) or [])} "
                         f"| {_esc((n.get('cluster') or '—'), 20)} |")
            L.append("")
            real_ids = [i for i in (graph.get("identities") or [])
                        if len(i.get("members", [])) > 1]
            if real_ids:
                L += ["**Resolved identities** (multiple records collapsed to "
                      "one canonical entity):", ""]
                for i in real_ids[:12]:
                    L.append(f"- `{_esc(i.get('canonical_id'), 60)}` — "
                             f"{len(i.get('members', []))} records, aliases: "
                             f"{', '.join(_esc(a, 30) for a in (i.get('aliases') or [])[:5])}")
                L.append("")
        else:
            unique = {str(e.get("id") or e.get("label")) for e in peer_entities}
            if unique:
                L += [f"_{len(unique)} unique entity/entities were collected but the "
                      "correlation graph was not produced._", ""]
                for eid in sorted(unique)[:40]:
                    L.append(f"- {_esc(eid, 80)}")
                L.append("")
            else:
                L += ["_No entities resolved._", ""]

        L += ["## 4. Timeline Appendix", ""]
        events = timeline.get("events") or []
        if events:
            span = timeline.get("span") or {}
            if span:
                L += [f"_Span: {str(span.get('earliest', ''))[:10]} → "
                      f"{str(span.get('latest', ''))[:10]}_", ""]
            for ev in events:
                L.append(f"- `{_event_date(ev)}` — **{_esc(ev.get('label', 'event'), 70)}**"
                         f" _(via {_esc(ev.get('source', '?'), 30)}, "
                         f"{_conf(ev.get('confidence')):.0%})_")
                if ev.get("description"):
                    L.append(f"  - {_esc(ev.get('description'), 160)}")
            L.append("")
        else:
            L += ["_No dated events were reconstructed — every collected signal "
                  "lacked a parseable timestamp._", ""]

        L += ["## 5. Agent Contribution Ledger", ""]
        if ledger:
            L += ["| Agent | Role | Status | Conf | Contributed | Could not determine |",
                  "|---|---|---|---|---|---|"]
            for e in ledger:
                L.append(f"| **{e['agent']}** | {_esc(e.get('role'), 24)} "
                         f"| {_STATUS_MARK.get(e.get('status'), '•')} {e.get('status')} "
                         f"| {e.get('confidence', 0):.0%} "
                         f"| {_esc('; '.join(e.get('contributed', []))[:90], 95)} "
                         f"| {_esc('; '.join(e.get('not_determined', []))[:70], 75)} |")
            L.append("")
        else:
            L += ["_No agent results were supplied._", ""]

        L += ["## 6. Assessment", "",
              f"**Overall confidence: {assessment_conf:.0%}**", ""]
        for r in raise_conf:
            L.append(f"- Would raise confidence: {r}")
        L.append("")

        L += ["## 7. Gaps & Recommended Next Steps", ""]
        if next_steps:
            for s in next_steps:
                L.append(f"- **[{str(s.get('priority', 'medium')).upper()}]** "
                         f"{s.get('action', '')}")
                L.append(f"  - _Rationale:_ {_esc(s.get('rationale', ''), 160)}")
        else:
            L += ["_No gaps identified._"]
        L.append("")

        contradictions = graph.get("contradictions") or []
        if contradictions:
            L += ["### Contradictions requiring resolution", ""]
            for c in contradictions[:10]:
                L.append(f"- **{_esc(c.get('field'), 30)}** "
                         f"({c.get('severity', '?')}): {_esc(c.get('detail'), 160)}")
            L.append("")

        L += ["## 8. Chain of Custody & Methodology", ""]
        meth = self._methodology(agent_results, by_type)
        L += [f"- **Report generated:** {generated}",
              f"- **Target:** `{_esc(target, 100)}`",
              f"- **Input classification:** {input_type}",
              f"- **Agents executed:** {', '.join(str(r.get('agent')) for r in agent_results) or 'none'}",
              f"- **Signals ingested:** {sum(by_type.values())} "
              f"({', '.join(f'{k}×{v}' for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:8]) or 'none'})",
              f"- **Entities ingested:** {len(peer_entities)}",
              f"- **Entity resolution:** {meth['entity_resolution']}",
              f"- **Clustering:** {meth['clustering']}",
              f"- **Centrality:** {meth['centrality']}",
              f"- **Provenance rule:** every claim in section 2 is derived from a "
              "signal or agent output listed above; any LLM-authored prose is "
              "labelled as AI-generated analysis and is not treated as evidence.",
              f"- **Confidence rule:** {meth['confidence_rule']}",
              ""]
        if errors:
            L += ["### Processing issues (non-fatal)", ""]
            for e in errors[:10]:
                L.append(f"- {_esc(e, 160)}")
            L.append("")
        L += ["---",
              "_Generated by Signal-OS · QUILL · evidence-traceable intelligence "
              "brief. AI-generated passages are marked as such._"]
        return "\n".join(L)

    @staticmethod
    def _methodology(agent_results: list[dict], by_type: dict) -> dict[str, str]:
        """Describe how the report was produced.

        Args:
            agent_results: Serialised agent results.
            by_type: Signal tally.

        Returns:
            dict: Human-readable methodology statements, plus the
            ``ai_generation`` disclosure.
        """
        graph = next((r.get("output") or {} for r in agent_results
                      if r.get("agent") == "NEXUS"), {}) or {}
        return {
            "entity_resolution": (graph.get("method", {}) or {}).get(
                "resolution", "not executed"),
            "clustering": (graph.get("method", {}) or {}).get(
                "clustering", "not executed"),
            "centrality": (graph.get("method", {}) or {}).get(
                "centrality", "not executed"),
            "confidence_rule": (
                "an entity counts as corroborated only when more than one agent "
                "reports it; a single-source entity is reported as a lead. "
                "Unresolved source conflicts reduce the overall assessment."),
            "ai_generation": (
                "the executive summary may include language-model prose, which "
                "is labelled as AI-generated analysis. It is constrained to the "
                "established findings and is never emitted as a finding itself."),
            "signal_count": str(sum(by_type.values())),
        }

    @staticmethod
    def _summary(target, input_type, by_type, graph, timeline, agent_results=None) -> str:
        """Build the deterministic executive summary.

        Args:
            target: Investigation target.
            input_type: Detected input type.
            by_type: Signal tally.
            graph: NEXUS output.
            timeline: KRONOS output.
            agent_results: Serialised agent results (used for the phone profile).

        Returns:
            str: One-paragraph deterministic summary.
        """
        parts = [f"Investigation of `{str(target)[:60]}` classified as "
                 f"**{input_type}**."]
        phone = None
        for r in (agent_results or []):
            if r.get("agent") == "PHONOS":
                phone = (r.get("output") or {}).get("offline", {})
                break
        if phone and phone.get("valid"):
            bits = [b for b in (phone.get("carrier"), phone.get("line_type"),
                                phone.get("location") or phone.get("region")) if b]
            parts.append(f"Number is valid ({', '.join(str(b) for b in bits)}).")
        accounts = by_type.get("account", 0)
        breaches = by_type.get("breach", 0)
        gps = by_type.get("gps", 0) + by_type.get("geolocation", 0)
        matches = by_type.get("face_match", 0) + by_type.get("image_match", 0)
        if accounts:
            parts.append(f"{accounts} linked account(s) identified across platforms.")
        if breaches:
            parts.append(f"Exposure in {breaches} known data breach(es).")
        if gps:
            parts.append(f"{gps} geolocation signal(s) recovered from media metadata.")
        if matches:
            parts.append(f"{matches} media/face match(es) found via reverse search.")
        if graph.get("clusters"):
            parts.append(f"Correlation produced {len(graph['clusters'])} entity "
                         f"cluster(s) from {len(graph.get('nodes', []))} nodes.")
        contradictions = graph.get("contradictions") or []
        if contradictions:
            parts.append(f"{len(contradictions)} source contradiction(s) were "
                         f"flagged and remain unresolved.")
        span = (timeline or {}).get("span")
        if span:
            parts.append(f"Activity timeline spans {str(span.get('earliest'))[:10]} → "
                         f"{str(span.get('latest'))[:10]}.")
        if len(parts) == 1:
            parts.append("No corroborating signals were recovered from public "
                         "sources; this is reported as an absence of evidence "
                         "rather than a negative finding.")
        return " ".join(parts)

    def _render_pdf(self, markdown: str, report_dir: str, case_id: str,
                    in_memory: bool = False) -> Optional[str]:
        """Render Markdown to PDF via ReportLab, when it is installed.

        Args:
            markdown: The Markdown report body.
            report_dir: Output directory (ignored when ``in_memory``).
            case_id: Case id, used in the filename.
            in_memory: When True, build the document and return a truthy marker
                without writing — used to detect availability cheaply.

        Returns:
            str | None: Path to the written PDF, ``""`` when ReportLab is
            unavailable, or ``None`` on a hard failure.
        """
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.units import inch
            from reportlab.pdfgen import canvas
        except ImportError:
            # ReportLab is an optional dependency (CONTRACTS §12.8): the
            # printable-HTML fallback is the supported path, never a crash.
            return "" if in_memory else None
        if in_memory:
            return "reportlab-available"
        try:
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, f"signal-os-report-{case_id}.pdf")
            c = canvas.Canvas(path, pagesize=letter)
            width, height = letter
            y = height - inch
            for raw in markdown.splitlines():
                line = raw.replace("**", "").replace("`", "").replace("_", "")
                if y < inch:
                    c.showPage()
                    y = height - inch
                font, size = ("Helvetica", 9)
                if line.startswith("# "):
                    font, size, line = "Helvetica-Bold", 16, line[2:]
                elif line.startswith("## "):
                    font, size, line = "Helvetica-Bold", 13, line[3:]
                elif line.startswith("### "):
                    font, size, line = "Helvetica-Bold", 11, line[4:]
                elif line.startswith("|"):
                    # Table rows are rendered as plain text; the HTML fallback
                    # is the format that keeps real tables.
                    line = "  ".join(x.strip() for x in line.split("|") if x.strip())
                c.setFont(font, size)
                c.drawString(inch, y, line[:110])
                y -= size + 4
            c.save()
            return path
        except Exception as exc:
            self.log(f"pdf render failed: {exc}")
            return None

    @staticmethod
    def _render_html(markdown: str, target: str, case_id: str, generated: str) -> str:
        """Render a self-contained printable HTML version of the report.

        This is the PDF fallback: one file, no external CSS or JS, opens in any
        browser and prints to PDF from there.

        Args:
            markdown: The Markdown report body.
            target: Investigation target.
            case_id: Case id.
            generated: Generation timestamp.

        Returns:
            str: A complete standalone HTML document.
        """
        body_lines: list[str] = []
        in_table = False
        table_rows: list[str] = []

        def close_table() -> None:
            nonlocal in_table
            if in_table:
                body_lines.append("<table>" + "".join(table_rows) + "</table>")
                table_rows.clear()
                in_table = False

        for raw in markdown.splitlines():
            stripped = raw.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if all(set(c) <= set("-: ") for c in cells):
                    continue  # separator row
                in_table = True
                tag = "th" if not table_rows else "td"
                table_rows.append(
                    "<tr>" + "".join(f"<{tag}>{_html_esc(c)}</{tag}>" for c in cells)
                    + "</tr>")
                continue
            close_table()
            if not stripped:
                body_lines.append("")
            elif stripped.startswith("### "):
                body_lines.append(f"<h3>{_html_esc(stripped[4:])}</h3>")
            elif stripped.startswith("## "):
                body_lines.append(f"<h2>{_html_esc(stripped[3:])}</h2>")
            elif stripped.startswith("# "):
                body_lines.append(f"<h1>{_html_esc(stripped[2:])}</h1>")
            elif stripped.startswith("> "):
                body_lines.append(
                    f'<blockquote>{_html_esc(stripped[2:])}</blockquote>')
            elif stripped.startswith("  - ") or stripped.startswith("    - "):
                body_lines.append(
                    f'<div class="sub">{_html_esc(stripped.lstrip("- "))}</div>')
            elif stripped.startswith("- ") or stripped.startswith("* "):
                body_lines.append(f"<li>{_html_esc(stripped[2:])}</li>")
            elif stripped in ("---", "***"):
                body_lines.append("<hr/>")
            else:
                body_lines.append(f"<p>{_html_esc(stripped)}</p>")
        close_table()
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Signal-OS Intelligence Report — {_html_esc(target)[:80]}</title>
<style>
 body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
   max-width:900px;margin:2rem auto;padding:0 1.5rem;color:#16202b;line-height:1.55;
   background:#fff;}}
 h1{{border-bottom:3px solid #1f6feb;padding-bottom:.4rem;}}
 h2{{margin-top:2rem;border-bottom:1px solid #d8dee4;padding-bottom:.25rem;}}
 h3{{margin-top:1.4rem;color:#3b5b7a;}}
 table{{border-collapse:collapse;width:100%;margin:.8rem 0;font-size:.9em;}}
 th,td{{border:1px solid #ccd6e0;padding:.4rem .6rem;text-align:left;vertical-align:top;}}
 th{{background:#eef3f8;}}
 blockquote{{border-left:4px solid #1f6feb;background:#f4f8ff;margin:.6rem 0;
   padding:.6rem 1rem;color:#33475b;}}
 .sub{{margin-left:1.4rem;color:#54687a;font-size:.92em;}}
 hr{{border:0;border-top:1px solid #d8dee4;margin:2rem 0;}}
 @media print{{body{{max-width:none;margin:0;}}}}
</style></head><body>
{"".join(x for x in body_lines if x is not None)}
<hr/><p><small>Case {_html_esc(case_id)} · generated {_html_esc(generated)} ·
Signal-OS QUILL. AI-generated passages are labelled in the report.</small></p>
</body></html>"""

    @staticmethod
    def _write_html(html_doc: str, report_dir: str, case_id: str) -> str:
        """Write the printable HTML report to disk.

        Args:
            html_doc: The rendered HTML document.
            report_dir: Output directory (created if absent).
            case_id: Case id, used in the filename.

        Returns:
            str: Path to the written file.
        """
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"signal-os-report-{case_id}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html_doc)
        return path
