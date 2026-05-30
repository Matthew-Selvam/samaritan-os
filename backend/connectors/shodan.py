import asyncio
import shodan
from typing import Dict, Any

class ShodanConnector:
    """
    Connector for Shodan API.
    Uses the official shodan Python SDK.
    """
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = shodan.Shodan(api_key)

    async def lookup(self, target: str) -> Dict[str, Any]:
        """
        Takes an IP or domain, returns open ports, services, vulnerabilities, geolocation.
        """
        loop = asyncio.get_event_loop()
        try:
            # Shodan SDK is synchronous, so we run it in a thread pool
            host = await loop.run_in_executor(None, self.client.host, target)
            
            return {
                "ip": host.get("ip_str"),
                "hostnames": host.get("hostnames", []),
                "org": host.get("org"),
                "os": host.get("os"),
                "ports": host.get("ports", []),
                "vulns": host.get("vulns", []),
                "city": host.get("city"),
                "country": host.get("country_name"),
                "latitude": host.get("latitude"),
                "longitude": host.get("longitude"),
                "services": [
                    {
                        "port": item.get("port"),
                        "protocol": item.get("transport"),
                        "product": item.get("product"),
                        "version": item.get("version"),
                        "data": item.get("data")
                    } for item in host.get("data", [])
                ]
            }
        except Exception as e:
            return {"error": str(e), "target": target}

async def run_shodan(target: str, **kwargs) -> Dict[str, Any]:
    api_key = kwargs.get("api_key")
    if not api_key:
        return {"error": "Shodan API key is required"}
    connector = ShodanConnector(api_key)
    return await connector.lookup(target)
