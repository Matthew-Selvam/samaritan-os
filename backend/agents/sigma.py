"""
SIGMA — Threat Intelligence Agent
=================================
IOC extraction, enrichment and scoring.

Four layers, each independently degradable:

1. **Local IOC extraction** — hashes, IPv4/IPv6, domains, URLs, emails, CVEs,
   registry keys, file paths, bitcoin addresses, mutexes, user-agents and
   ``key=value`` indicators, pulled from arbitrary text. Pure regex; this is the
   layer that always produces something.
2. **CVE → CISA KEV** — the public CISA Known Exploited Vulnerabilities catalog
   (no key) is fetched once and cached, so "is this CVE actually exploited in
   the wild" is answered from a real feed rather than a guess.
3. **MITRE ATT&CK mapping** — techniques inferred from the IOC mix and from
   explicit text, keyed to a compact offline technique table.
4. **Threat score** — a weighted aggregation of KEV presence, IOC volume,
   indicator class mix, and live-feed verdicts.

For IP / domain inputs SIGMA additionally queries Shodan (via
``connectors/shodan.py``) and the key-free public feeds urlscan.io and
ThreatFox. Those calls are cached, circuit-broken, and time-bounded, so a
missing key or a dead host degrades the result instead of failing the agent.

The Shodan connector hard-imports the ``shodan`` SDK (an optional dependency),
so it is imported lazily and degrades gracefully — the pipeline never breaks.
"""
from __future__ import annotations

import asyncio
import os
import re

from .base import BaseAgent, AgentResult
from router import detect_input_type  # absolute: backend/ is the path root


# ── MITRE ATT&CK (compact offline table) ─────────────────────────────────────

#: Technique id -> (name, tactics). Covers the techniques an IOC mix can
#: plausibly evidence without a full STIX bundle.
ATTACK_TECHNIQUES: dict[str, tuple[str, list[str]]] = {
    "T1566": ("Phishing", ["initial-access"]),
    "T1566.001": ("Spearphishing Attachment", ["initial-access"]),
    "T1566.002": ("Spearphishing Link", ["initial-access"]),
    "T1190": ("Exploit Public-Facing Application", ["initial-access"]),
    "T1195": ("Supply Chain Compromise", ["initial-access"]),
    "T1199": ("Trusted Relationship", ["initial-access"]),
    "T1204": ("User Execution", ["execution"]),
    "T1059": ("Command and Scripting Interpreter", ["execution"]),
    "T1059.001": ("PowerShell", ["execution"]),
    "T1047": ("Windows Management Instrumentation", ["execution"]),
    "T1106": ("Native API", ["execution"]),
    "T1547": ("Boot or Logon Autostart Execution", ["persistence", "privilege-escalation"]),
    "T1547.001": ("Registry Run Keys / Startup Folder", ["persistence"]),
    "T1053": ("Scheduled Task/Job", ["persistence", "privilege-escalation"]),
    "T1543": ("Create or Modify System Process", ["persistence", "privilege-escalation"]),
    "T1112": ("Modify Registry", ["defense-evasion"]),
    "T1027": ("Obfuscated Files or Information", ["defense-evasion"]),
    "T1027.002": ("Software Packing", ["defense-evasion"]),
    "T1070": ("Indicator Removal on Host", ["defense-evasion"]),
    "T1070.001": ("Clear Windows Event Logs", ["defense-evasion"]),
    "T1036": ("Masquerading", ["defense-evasion"]),
    "T1140": ("Deobfuscate/Decode Files or Information", ["defense-evasion"]),
    "T1105": ("Ingress Tool Transfer", ["command-and-control"]),
    "T1071": ("Application Layer Protocol", ["command-and-control"]),
    "T1071.001": ("Web Protocols", ["command-and-control"]),
    "T1573": ("Encrypted Channel", ["command-and-control"]),
    "T1090": ("Proxy", ["command-and-control"]),
    "T1090.003": ("Multi-hop Proxy", ["command-and-control"]),
    "T1572": ("Protocol Tunneling", ["command-and-control"]),
    "T1041": ("Exfiltration Over C2 Channel", ["exfiltration"]),
    "T1048": ("Exfiltration Over Alternative Protocol", ["exfiltration"]),
    "T1567": ("Exfiltration Over Web Service", ["exfiltration"]),
    "T1078": ("Valid Accounts", ["defense-evasion", "persistence", "privilege-escalation"]),
    "T1110": ("Brute Force", ["credential-access"]),
    "T1110.003": ("Password Spraying", ["credential-access"]),
    "T1555": ("Credentials from Password Stores", ["credential-access", "collection"]),
    "T1552": ("Unsecured Credentials", ["credential-access"]),
    "T1003": ("OS Credential Dumping", ["credential-access"]),
    "T1056": ("Input Capture", ["collection", "credential-access"]),
    "T1082": ("System Information Discovery", ["discovery"]),
    "T1016": ("System Network Configuration Discovery", ["discovery"]),
    "T1018": ("Remote System Discovery", ["discovery"]),
    "T1083": ("File and Directory Discovery", ["discovery"]),
    "T1057": ("Process Discovery", ["discovery"]),
    "T1497": ("Virtualization/Sandbox Evasion", ["discovery", "defense-evasion"]),
    "T1486": ("Data Encrypted for Impact", ["impact"]),
    "T1490": ("Inhibit System Recovery", ["impact"]),
    "T1489": ("Service Stop", ["impact"]),
    "T1491": ("Defacement", ["impact"]),
    "T1657": ("Financial Theft", ["impact", "collection"]),
}

#: IOC kinds that imply a technique when merely *present* in a report.
_IOC_TECHNIQUE_HINTS: dict[str, tuple[str, ...]] = {
    "registry_key": ("T1547.001", "T1112"),
    "bitcoin_address": ("T1657",),
    "c2_domain": ("T1071.001",),
    "c2_url": ("T1071.001",),
    "powershell": ("T1059.001",),
    "encoded_blob": ("T1027", "T1140"),
    "office_macro": ("T1204.002", "T1566.001"),
    "mutex": ("T1547.001",),
    "user_agent": ("T1071.001",),
    "executable_path": ("T1204.002", "T1036"),
    "library_path": ("T1574.001", "T1036"),
    "scheduled_task": ("T1053.005",),
}


class SigmaAgent(BaseAgent):
    name = "SIGMA"
    role = "Threat Intelligence"
    icon = "⊗"
    description = "IOC ingestion, phishing detection, breach correlation, infrastructure mapping"
    preferred_models = ["gemma2:9b"]
    token_budget = 8192

    SHODAN_TYPES = {"ip_address", "ip_address", "ipv6", "domain", "url", "onion_address"}
    SHODAN_TIMEOUT = 10.0
    #: The public CISA KEV catalog is small and changes rarely.
    KEV_URL = ("https://www.cisa.gov/sites/default/files/feeds/"
               "known_exploited_vulnerabilities.json")
    KEV_CACHE_TTL = 24 * 3600.0
    KEV_TIMEOUT = 15.0

    # ── IOC extraction ───────────────────────────────────────────────────────

    _EXTRACTORS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
        ("cve", "cve", re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)),
        ("ipv4", "ip", re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")),
        ("ipv6", "ip", re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b")),
        ("md5", "hash", re.compile(r"\b[a-fA-F0-9]{32}\b")),
        ("sha1", "hash", re.compile(r"\b[a-fA-F0-9]{40}\b")),
        ("sha256", "hash", re.compile(r"\b[a-fA-F0-9]{64}\b")),
        ("sha512", "hash", re.compile(r"\b[a-fA-F0-9]{128}\b")),
        ("email", "email", re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
        ("bitcoin", "bitcoin_address", re.compile(
            r"\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[ac-hj-np-z02-9]{11,71})\b")),
        ("registry_key", "registry_key", re.compile(
            r"\b(?:HKEY_[A-Z_]+|HKLM|HKCU|HKCR|HKU)\\(?:[^\\\s]+\\)*[^\\\s]+\b", re.I)),
        ("scheduled_task", "scheduled_task", re.compile(
            r"\b(?:schtasks(?:\.exe)?\s+/create|at\s+\\\\[^\s]+|Register-ScheduledTask)\b", re.I)),
        ("mutex", "mutex", re.compile(
            r"\b(?:Global\\|Local\\)[A-Za-z0-9_\-{}]{4,64}\b")),
        ("powershell", "powershell", re.compile(
            r"\b(?:powershell(?:\.exe)?|pwsh)(?:\s+-[a-zA-Z]+)*\b", re.I)),
        ("encoded_blob", "encoded_blob", re.compile(
            r"\b[A-Za-z0-9+/]{120,}={0,2}\b")),
        ("user_agent", "user_agent", re.compile(
            r"\bMozilla/5\.0\s*\([^)]{10,200}\)[\w.\-/+;]*", re.I)),
        ("url", "url", re.compile(
            r"\bhttps?://[^\s<>\"'\\)\]]{6,300}")),
        ("windows_path", "executable_path", re.compile(
            r"\b[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]*", re.I)),
        ("registry_run", "scheduled_task", re.compile(
            r"\bSoftware\\Microsoft\\Windows\\CurrentVersion\\Run[A-Za-z]*\b", re.I)),
    )
    _DOMAIN_RE = re.compile(
        r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
        r"(?:com|net|org|io|ru|cn|info|biz|xyz|top|cc|tk|ml|ga|cf|gq|pw|"
        r"onion|site|online|live|club|shop|icu|vip|work|link|click|space)\b", re.I)
    _IPV4_STRICT = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

    @classmethod
    def extract_iocs(cls, text: str) -> list[dict]:
        """Pull every indicator out of arbitrary text.

        Args:
            text: Threat report, log line, paste, anything.
        Returns:
            De-duplicated IOC dicts, each ``{"type", "value", "source"}``.
        """
        if not text:
            return []
        found: dict[tuple[str, str], dict] = {}

        def add(kind: str, value: str, source: str = "extraction") -> None:
            value = value.strip().strip(".,;:)\"'")
            if not value:
                return
            key = (kind, value.lower())
            if key not in found:
                found[key] = {"type": kind, "value": value, "source": source}

        for name, kind, pattern in cls._EXTRACTORS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if kind == "ip" and cls._IPV4_STRICT.match(value):
                    if value.count(".") == 3:
                        parts = value.split(".")
                        if int(parts[0]) == 0 and all(p == "0" for p in parts[1:]):
                            continue
                if kind == "url":
                    # The hostname is also a domain IOC.
                    host = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0]
                    add("domain", host)
                    if re.search(r"\d{1,3}(\.\d{1,3}){3}", host):
                        add("ip", host.split(":")[0])
                    else:
                        add("c2_domain", host)
                    add("c2_url", value)
                elif kind == "executable_path":
                    if "\\" in value and len(value) > 6:
                        add("executable_path", value)
                else:
                    add(kind, value)

        # Domains (skip ones already captured from a URL).
        for match in cls._DOMAIN_RE.finditer(text):
            value = match.group(0)
            if ("domain", value.lower()) not in found:
                add("domain", value)

        return list(found.values())

    @staticmethod
    def map_attack(iocs: list[dict], text: str = "") -> list[dict]:
        """Infer MITRE ATT&CK techniques from an IOC mix and the raw text.

        Args:
            iocs: IOCs from :meth:`extract_iocs`.
            text: The raw report, searched for explicit technique ids.
        Returns:
            Technique dicts with id, name, tactics and why they were inferred.
        """
        techniques: dict[str, dict] = {}

        def add(tid: str, reason: str) -> None:
            if tid in techniques or tid not in ATTACK_TECHNIQUES:
                return
            name, tactics = ATTACK_TECHNIQUES[tid]
            techniques[tid] = {"technique_id": tid, "technique": name,
                               "tactics": tactics, "reason": reason}

        present = {ioc["type"] for ioc in iocs}
        for kind in present:
            for tid in _IOC_TECHNIQUE_HINTS.get(kind, ()):
                add(tid, f"indicator class '{kind}' present")

        # A KEV-listed CVE exploited against an internet-facing service is the
        # canonical exploitation technique.
        if any(i["type"] == "cve" for i in iocs):
            add("T1190", "CVE referenced")
            add("T1204", "CVE referenced")

        for match in re.finditer(r"\b(T\d{4}(?:\.\d{3})?)\b", text or "", re.I):
            add(match.group(1).upper(), "cited explicitly in the report text")

        return list(techniques.values())

    @staticmethod
    def threat_score(iocs: list[dict], *, kev_hits: list[str] | None = None,
                     live_verdicts: int = 0, techniques: list[dict] | None = None) -> dict:
        """Aggregate a 0–100 threat score from the evidence.

        Args:
            iocs: Extracted indicators.
            kev_hits: CVE ids present in the CISA KEV catalog.
            live_verdicts: How many public feeds flagged the target.
            techniques: Inferred ATT&CK techniques.
        Returns:
            ``{"score", "band", "components"}``.
        """
        components: dict[str, int] = {}
        kev_hits = kev_hits or []
        if kev_hits:
            components["known_exploited"] = min(50, 25 + 10 * len(kev_hits))
        hash_iocs = sum(1 for i in iocs if i["type"] == "hash")
        if hash_iocs:
            components["file_hashes"] = min(20, 5 * hash_iocs)
        if any(i["type"] in ("c2_url", "c2_domain", "bitcoin_address") for i in iocs):
            components["command_and_control"] = 15
        if any(i["type"] in ("registry_key", "scheduled_task", "mutex",
                             "executable_path", "powershell") for i in iocs):
            components["host_artifacts"] = 12
        if any(i["type"] == "encoded_blob" for i in iocs):
            components["obfuscation"] = 8
        components["live_verdicts"] = min(15, 5 * max(0, live_verdicts))
        if techniques:
            components["technique_breadth"] = min(10, 2 * len(techniques))

        score = min(100, sum(components.values()))
        band = ("informational" if score < 20 else "low" if score < 40
                else "moderate" if score < 65 else "high" if score < 85
                else "critical")
        return {"score": score, "band": band, "components": components}

    # ── Free feeds ───────────────────────────────────────────────────────────

    @classmethod
    async def fetch_kev(cls, timeout: float | None = None) -> dict[str, dict]:
        """Fetch + cache the CISA KEV catalog (no API key required).

        Args:
            timeout: Override the request timeout.
        Returns:
            ``{cve_id: entry}``. An empty dict when the feed is unreachable —
            the caller must treat "no KEV data" as unknown, not as "not KEV".
        """
        try:
            from cache import cache_get, cache_set
        except Exception:  # noqa: BLE001 — cache is optional
            cache_get = cache_set = None  # type: ignore[assignment]

        cache_key = "sigma:cisa_kev:v1"
        if cache_get is not None:
            try:
                cached = await cache_get(cache_key)
                if isinstance(cached, dict) and cached:
                    return cached
            except Exception:  # noqa: BLE001
                pass

        entries: dict[str, dict] = {}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout or cls.KEV_TIMEOUT,
                                         trust_env=False) as client:
                response = await client.get(cls.KEV_URL,
                                            headers={"User-Agent": "Signal-OS/0.1"})
                if response.status_code == 200:
                    body = response.json() or {}
                    for vuln in body.get("vulnerabilities", []):
                        cve = str(vuln.get("cveID") or "").upper()
                        if cve:
                            entries[cve] = {
                                "vendor": vuln.get("vendorProject"),
                                "product": vuln.get("product"),
                                "name": vuln.get("vulnerabilityName"),
                                "date_added": vuln.get("dateAdded"),
                                "due_date": vuln.get("dueDate"),
                                "known_ransomware_use": vuln.get("knownRansomwareCampaignUse"),
                                "notes": (vuln.get("notes") or "")[:200],
                            }
        except Exception:  # noqa: BLE001 — a dead feed must not fail the agent
            entries = {}

        if entries and cache_set is not None:
            try:
                await cache_set(cache_key, entries, cls.KEV_CACHE_TTL)
            except Exception:  # noqa: BLE001
                pass
        return entries

    @staticmethod
    async def _free_feed(url: str, timeout: float = 10.0) -> dict | None:
        """Best-effort GET against a key-free public feed, circuit-broken."""
        try:
            from circuit import get_breaker
            breaker = get_breaker("sigma.freesfeed")
        except Exception:  # noqa: BLE001
            breaker = None
        if breaker is not None and not breaker.allow():
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.get(url, headers={"User-Agent": "Signal-OS/0.1"})
                if response.status_code >= 400:
                    if breaker:
                        breaker.record_failure()
                    return None
                if breaker:
                    breaker.record_success()
                return response.json()
        except Exception:  # noqa: BLE001
            if breaker:
                breaker.record_failure()
            return None

    async def _public_feeds(self, target: str, is_ip: bool) -> tuple[list[dict], int]:
        """Query urlscan.io (search) + ThreatFox (exact IOC lookup).

        Both feeds are key-free. Failure of either is non-fatal: the agent
        reports whatever it got.

        Args:
            target: The IP, domain or URL to look up.
            is_ip: True when ``target`` is an IP address (changes the query).
        Returns:
            ``(verdicts, feed_hit_count)``.
        """
        from urllib.parse import quote

        verdicts: list[dict] = []
        hits = 0

        scan_url = (f"https://urlscan.io/api/v1/search/?q=ip:{quote(target, safe='')}&size=10"
                    if is_ip else
                    f"https://urlscan.io/api/v1/search/?q=domain:{quote(target, safe='')}&size=10")
        scan = await self._free_feed(scan_url)
        if isinstance(scan, dict):
            for result in (scan.get("results") or [])[:10]:
                page = result.get("page") or {}
                verdicts.append({
                    "feed": "urlscan.io", "task": (result.get("task") or {}).get("uuid"),
                    "url": page.get("url"), "domain": page.get("domain"),
                    "ip": page.get("ip"), "country": page.get("country"),
                    "server": page.get("server"), "asn": page.get("asn"),
                    "verdict": (result.get("verdicts") or {}).get("overall", {}).get("malicious"),
                })
            if verdicts:
                hits += 1

        # ThreatFox: POST /api/v1/ with {"query": "search_ioc", "search_term": …}.
        # It is abuse.ch, which retired anonymous API access in 2024, so the
        # call is best-effort and its absence is reported, never fatal.
        threatfox = await self._free_feed_post(
            "https://threatfox-api.abuse.ch/api/v1/",
            {"query": "search_ioc", "search_term": target},
        )
        if isinstance(threatfox, dict) and threatfox.get("query_status") == "ok":
            for entry in (threatfox.get("data") or [])[:10]:
                verdicts.append({
                    "feed": "threatfox", "ioc": entry.get("ioc"),
                    "ioc_type": entry.get("ioc_type"),
                    "malware": entry.get("malware_printable"),
                    "confidence": entry.get("confidence_level"),
                    "first_seen": entry.get("first_seen"),
                    "tags": entry.get("tags"),
                })
            if verdicts and threatfox.get("data"):
                hits += 1
        return verdicts, hits

    @staticmethod
    async def _free_feed_post(url: str, payload: dict, timeout: float = 10.0) -> dict | None:
        """POST to a key-free feed, circuit-broken like :meth:`_free_feed`."""
        try:
            from circuit import get_breaker
            breaker = get_breaker("sigma.threatfox")
        except Exception:  # noqa: BLE001
            breaker = None
        if breaker is not None and not breaker.allow():
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.post(url, json=payload,
                                             headers={"User-Agent": "Signal-OS/0.1"})
                if response.status_code >= 400:
                    if breaker:
                        breaker.record_failure()
                    return None
                if breaker:
                    breaker.record_success()
                return response.json()
        except Exception:  # noqa: BLE001
            if breaker:
                breaker.record_failure()
            return None

    # ── Agent entry point ────────────────────────────────────────────────────

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        target = (input_data if isinstance(input_data, str) else str(input_data)).strip()
        input_type = context.get("input_type") or detect_input_type(target).input_type.value

        # Layer 1: always-on local extraction. A long target is a report.
        iocs = self.extract_iocs(target) if len(target) > 40 else []
        if len(target) > 40:
            self.log(f"extracted {len(iocs)} indicator(s) from {len(target)} chars of text")

        # Layer 2: CISA KEV cross-reference for any CVE in scope.
        cves = sorted({i["value"].upper() for i in iocs if i["type"] == "cve"})
        if input_type == "cve" and target.upper().startswith("CVE-"):
            cves = sorted(set(cves) | {target.upper()})
        kev_hits: list[str] = []
        kev_data: dict[str, dict] = {}
        if cves:
            kev_data = await self.fetch_kev()
            kev_hits = [c for c in cves if c in kev_data]
            self.log(f"CISA KEV: {len(kev_hits)}/{len(cves)} CVE(s) known-exploited"
                     + (f" — {', '.join(kev_hits[:3])}" if kev_hits else ""))

        # Layer 3: ATT&CK mapping.
        techniques = self.map_attack(iocs, target)
        if techniques:
            self.log(f"mapped {len(techniques)} ATT&CK technique(s)")

        # Layer 4: free public feeds for infra targets.
        verdicts: list[dict] = []
        live_hits = 0
        if input_type in ("ip_address", "ipv6", "domain", "url", "onion_address"):
            verdicts, live_hits = await self._public_feeds(
                target, input_type in ("ip_address", "ipv6"))
            self.log(f"public feeds: {len(verdicts)} record(s), {live_hits} feed hit(s)")

        signals: list[dict] = list(iocs)
        for cve in cves:
            entry = kev_data.get(cve)
            signals.append({"type": "cve", "cve": cve, "in_kev": bool(entry),
                            **({"kev": entry} if entry else {})})
        for hit in kev_hits:
            signals.append({"type": "kev", "cve": hit, "source": "cisa"})
        for technique in techniques:
            signals.append({"type": "attack_technique", **technique})
        for verdict in verdicts:
            signals.append({"type": "threat_feed", **verdict})

        score = self.threat_score(iocs, kev_hits=kev_hits,
                                  live_verdicts=live_hits, techniques=techniques)

        # Shodan, when it applies and is available.
        shodan_res: dict | None = None
        if input_type in self.SHODAN_TYPES:
            shodan_res = await self._shodan(target, input_type)
            if shodan_res:
                signals.extend(self._to_iocs(input_type, target, shodan_res))

        confidence = 0.7 if (iocs or kev_hits or verdicts or shodan_res) else 0.2
        output = {
            # Legacy keys — stored reports read these.
            "iocs": signals,
            "input_type": input_type,
            "shodan": shodan_res,
            # New.
            "extracted": iocs,
            "iocs_by_type": self._group(iocs),
            "cves": cves,
            "kev": {c: kev_data[c] for c in kev_hits},
            "kev_available": bool(kev_data),
            "techniques": techniques,
            "threat_score": score,
            "feeds": verdicts,
            "note": self._note(input_type, bool(iocs), bool(kev_data), bool(verdicts)),
        }
        self.log(f"threat score {score['score']}/100 ({score['band']})")
        return AgentResult(
            agent=self.name, status="done", output=output,
            confidence=confidence,
            reasoning=(
                f"{len(iocs)} IOC(s) extracted, {len(cves)} CVE(s) "
                f"({len(kev_hits)} in CISA KEV), {len(techniques)} ATT&CK technique(s), "
                f"{len(verdicts)} public-feed record(s); threat score "
                f"{score['score']}/100 ({score['band']})."
            ),
            signals=signals,
            latency_s=self._elapsed(t0),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _shodan(self, target: str, input_type: str) -> dict | None:
        """Query Shodan, degrading to ``None`` on any problem."""
        try:
            from connectors.shodan import run_shodan
        except Exception as e:  # noqa: BLE001 — SDK missing or worse
            self.log(f"Shodan connector unavailable: {e}")
            return None
        api_key = os.getenv("SHODAN_API_KEY")
        self.log(f"querying Shodan for {input_type} {target[:60]}"
                 + ("" if api_key else " (no SHODAN_API_KEY set)"))
        try:
            res = await asyncio.wait_for(run_shodan(target, api_key=api_key),
                                         timeout=self.SHODAN_TIMEOUT)
        except asyncio.TimeoutError:
            self.log(f"Shodan timed out after {self.SHODAN_TIMEOUT:.0f}s")
            return None
        except Exception as e:  # noqa: BLE001 — never let a connector kill the agent
            self.log(f"Shodan call failed: {e}")
            return None
        if not isinstance(res, dict) or "error" in res:
            self.log("Shodan inactive")
            return None
        self.log(f"Shodan live — {len(self._to_iocs(input_type, target, res))} IOC(s)")
        return res

    @staticmethod
    def _group(iocs: list[dict]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for ioc in iocs:
            grouped.setdefault(ioc["type"], []).append(ioc["value"])
        return grouped

    @staticmethod
    def _note(input_type: str, has_iocs: bool, has_kev: bool, has_feeds: bool) -> str:
        if input_type in ("ip_address", "ipv6", "domain", "url", "onion_address"):
            if has_feeds:
                return "Infrastructure target: public-feed and Shodan enrichment attempted."
            return "Infrastructure target: Shodan/public feeds inactive (no key or unreachable)."
        if has_iocs:
            return "Text target: local IOC extraction succeeded."
        return "Shodan applies to ip/domain targets only; no indicators found in text."

    @staticmethod
    def _to_iocs(input_type: str, target: str, res: dict) -> list[dict]:
        """Normalize the Shodan host payload into IOC signals."""
        signals: list[dict] = []
        ip = res.get("ip") or target
        for port in (res.get("ports") or []):
            signals.append({"type": "open_port", "ip": ip, "port": port})
        for host in (res.get("hostnames") or []):
            signals.append({"type": "hostname", "ip": ip, "hostname": host})
        for vuln in (res.get("vulns") or []):
            signals.append({"type": "vuln", "ip": ip, "cve": vuln})
        for svc in (res.get("services") or []):
            signals.append({"type": "service", "ip": ip, "port": svc.get("port"),
                            "product": svc.get("product"), "version": svc.get("version")})
        if res.get("org"):
            signals.append({"type": "org", "ip": ip, "org": res.get("org")})
        if res.get("country") or res.get("city"):
            signals.append({"type": "geo", "ip": ip, "city": res.get("city"),
                            "country": res.get("country"),
                            "lat": res.get("latitude"), "lon": res.get("longitude")})
        return signals


__all__ = ["SigmaAgent", "ATTACK_TECHNIQUES"]