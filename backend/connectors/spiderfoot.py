import httpx
import asyncio
import time
from typing import Dict, Any, List

class SpiderFootConnector:
    """
    Connector for SpiderFoot REST API.
    Hits SpiderFoot's REST API (self-hosted on port 5001) to kick off a scan and poll for results.
    """
    def __init__(self, base_url: str = "http://localhost:5001"):
        self.base_url = base_url.rstrip("/")

    async def start_scan(self, scan_name: str, target: str, modules: List[str] = None) -> str:
        """Starts a scan and returns the scan ID."""
        url = f"{self.base_url}/startscan"
        payload = {
            "scanname": scan_name,
            "scantarget": target,
            "use_modules": ",".join(modules) if modules else ""
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=payload)
            response.raise_for_status()
            # SpiderFoot returns the scan ID in the response
            # Note: Depending on version, this might be in JSON or a redirect
            data = response.json()
            return data.get("id")

    async def get_scan_status(self, scan_id: str) -> str:
        """Returns the status of the scan."""
        url = f"{self.base_url}/scanstatus?id={scan_id}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            return data.get("status") # e.g., "FINISHED", "RUNNING"

    async def get_scan_results(self, scan_id: str) -> List[Dict[str, Any]]:
        """Returns the results of the scan."""
        url = f"{self.base_url}/scanresults?id={scan_id}&format=json"
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def run_and_poll(self, target: str, scan_name: str = None, timeout: int = 600) -> Dict[str, Any]:
        if not scan_name:
            scan_name = f"scan_{int(time.time())}"
            
        try:
            scan_id = await self.start_scan(scan_name, target)
            if not scan_id:
                return {"error": "Failed to start scan", "target": target}
            
            start_time = time.time()
            while time.time() - start_time < timeout:
                status = await self.get_scan_status(scan_id)
                if status == "FINISHED":
                    results = await self.get_scan_results(scan_id)
                    return {
                        "scan_id": scan_id,
                        "target": target,
                        "status": status,
                        "results": results
                    }
                elif status == "ERROR":
                    return {"error": "Scan failed", "scan_id": scan_id, "target": target}
                
                await asyncio.sleep(10) # Poll every 10 seconds
            
            return {"error": "Scan timed out", "scan_id": scan_id, "target": target}
        except Exception as e:
            return {"error": str(e), "target": target}

async def run_spiderfoot(target: str, **kwargs) -> Dict[str, Any]:
    connector = SpiderFootConnector(base_url=kwargs.get("base_url", "http://localhost:5001"))
    return await connector.run_and_poll(
        target, 
        scan_name=kwargs.get("scan_name"),
        timeout=kwargs.get("timeout", 600)
    )
