"""
router.py — AI Input Router
============================
Detects input type and determines which agents to activate.

Input types:
  username, email, phone, domain, crypto_wallet, ip_address,
  image, video, audio, document, url, license_plate, text, unknown
"""
from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field


class InputType(str, Enum):
    USERNAME      = "username"
    EMAIL         = "email"
    PHONE         = "phone"
    DOMAIN        = "domain"
    IP            = "ip_address"
    CRYPTO_WALLET = "crypto_wallet"
    URL           = "url"
    IMAGE         = "image"
    PHOTO         = "photo"
    FACE          = "face"
    PERSON_NAME   = "person_name"
    VIDEO         = "video"
    AUDIO         = "audio"
    DOCUMENT      = "document"
    TEXT          = "text"
    UNKNOWN       = "unknown"


# Which agents activate for each input type
AGENT_MAP: dict[InputType, list[str]] = {
    InputType.USERNAME:      ["SCOUT", "PRISM", "NEXUS", "VAULT"],
    InputType.EMAIL:         ["EMAIL", "SCOUT", "SIGMA", "PRISM", "NEXUS", "VAULT"],
    InputType.PHONE:         ["PHONOS", "SCOUT", "NEXUS", "VAULT"],
    InputType.DOMAIN:        ["SCOUT", "SIGMA", "CRAWLER", "NEXUS"],
    InputType.IP:            ["SIGMA", "TERRA", "NEXUS"],
    InputType.CRYPTO_WALLET: ["SIGMA", "NEXUS", "VAULT"],
    InputType.URL:           ["SCOUT", "CRAWLER", "IRIS", "SIGMA"],
    InputType.IMAGE:         ["IRIS", "TERRA", "NEXUS"],
    InputType.PHOTO:         ["IRIS", "TERRA", "NEXUS", "VAULT", "PRISM"],
    InputType.FACE:          ["IRIS", "PRISM", "NEXUS"],
    InputType.PERSON_NAME:   ["SCOUT", "PRISM", "NEXUS", "VAULT", "INK"],
    InputType.VIDEO:         ["IRIS", "ECHO", "TERRA"],
    InputType.AUDIO:         ["ECHO", "INK"],
    InputType.DOCUMENT:      ["IRIS", "INK", "CRAWLER"],
    InputType.TEXT:          ["INK", "PRISM", "NEXUS"],
    InputType.UNKNOWN:       ["SCOUT", "IRIS", "INK"],
}


@dataclass
class RoutingDecision:
    input_type: InputType
    agents: list[str]
    confidence: float
    reasoning: str


def detect_input_type(raw: str) -> RoutingDecision:
    """Heuristic input type detection. Override with LLM for ambiguous inputs."""
    raw = raw.strip()

    # Email
    if re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", raw):
        return RoutingDecision(InputType.EMAIL, AGENT_MAP[InputType.EMAIL], 0.99,
                               "RFC 5321 email format")

    # IPv4
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", raw):
        return RoutingDecision(InputType.IP, AGENT_MAP[InputType.IP], 0.97,
                               "IPv4 address pattern")

    # Phone number — E.164 (+…) or a formatted number with 7-15 digits.
    # Checked before domain/username so "+1 (415) 555-0123" routes correctly.
    _digits = re.sub(r"[^\d]", "", raw)
    if (raw.startswith("+") and re.match(r"^\+\d[\d\s().\-]{6,20}$", raw)) or \
       (re.match(r"^[\d\s().\-]{7,20}$", raw) and 7 <= len(_digits) <= 15
            and re.search(r"[\s().\-+]", raw)):
        return RoutingDecision(InputType.PHONE, AGENT_MAP[InputType.PHONE], 0.9,
                               "Phone number pattern (E.164 / formatted)")

    # Domain
    if re.match(r"^([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}$", raw) and "." in raw:
        return RoutingDecision(InputType.DOMAIN, AGENT_MAP[InputType.DOMAIN], 0.9,
                               "Domain name pattern")

    # URL
    if re.match(r"^https?://", raw):
        return RoutingDecision(InputType.URL, AGENT_MAP[InputType.URL], 0.98,
                               "HTTP/HTTPS URL")

    # Crypto wallets (Bitcoin, Ethereum)
    if re.match(r"^(1|3|bc1)[a-zA-Z0-9]{25,62}$", raw) or \
       re.match(r"^0x[a-fA-F0-9]{40}$", raw):
        return RoutingDecision(InputType.CRYPTO_WALLET, AGENT_MAP[InputType.CRYPTO_WALLET], 0.95,
                               "Cryptocurrency wallet address")

    # File extensions → media/doc types
    lower = raw.lower()
    if any(lower.endswith(ext) for ext in [".jpg",".jpeg",".png",".webp",".gif",".bmp"]):
        return RoutingDecision(InputType.IMAGE, AGENT_MAP[InputType.IMAGE], 0.99, "Image file extension")
    if any(lower.endswith(ext) for ext in [".mp4",".mov",".avi",".mkv",".webm"]):
        return RoutingDecision(InputType.VIDEO, AGENT_MAP[InputType.VIDEO], 0.99, "Video file extension")
    if any(lower.endswith(ext) for ext in [".mp3",".wav",".ogg",".flac",".m4a"]):
        return RoutingDecision(InputType.AUDIO, AGENT_MAP[InputType.AUDIO], 0.99, "Audio file extension")
    if any(lower.endswith(ext) for ext in [".pdf",".docx",".xlsx",".pptx",".txt"]):
        return RoutingDecision(InputType.DOCUMENT, AGENT_MAP[InputType.DOCUMENT], 0.99, "Document file extension")

    # Short alphanumeric with no spaces → username
    if re.match(r"^[a-zA-Z0-9_.\-]{3,30}$", raw) and " " not in raw:
        return RoutingDecision(InputType.USERNAME, AGENT_MAP[InputType.USERNAME], 0.7,
                               "Short alphanumeric — likely username or alias")

    # Person name — 2-4 words, each capitalized or short, no special chars
    words = raw.split()
    if 2 <= len(words) <= 4 and all(re.match(r"^[A-Za-z'\-]{2,20}$", w) for w in words):
        return RoutingDecision(InputType.PERSON_NAME, AGENT_MAP[InputType.PERSON_NAME], 0.75,
                               "Multi-word input resembles a person name")

    # Long text → stylometry + text analysis
    if len(raw) > 100:
        return RoutingDecision(InputType.TEXT, AGENT_MAP[InputType.TEXT], 0.8,
                               "Long text input — stylometry and content analysis")

    return RoutingDecision(InputType.UNKNOWN, AGENT_MAP[InputType.UNKNOWN], 0.4,
                           "Could not determine input type with confidence")
