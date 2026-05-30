import asyncio
import json
import os
import subprocess
from typing import Dict, Any

class TheHarvesterConnector:
    """
    Connector for theHarvester CLI tool.
    Calls theHarvester via subprocess and parses its JSON output.
    """
    async def scan(self, domain: str, limit: int = 500, source: str = "all") -> Dict[str, Any]:
        """
        Returns emails, domains, IPs, URLs found.
        """
        output_base = f"harvester_{domain}"
        output_file = f"{output_base}.json"
        
        # Command: theHarvester -d {domain} -l {limit} -b {source} -f {output_base}
        cmd = ["theHarvester", "-d", domain, "-l", str(limit), "-b", source, "-f", output_base]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if os.path.exists(output_file):
                with open(output_file, "r") as f:
                    data = json.load(f)
                os.remove(output_file)
                # theHarvester also creates an XML file sometimes, let's clean it up if it exists
                if os.path.exists(f"{output_base}.xml"):
                    os.remove(f"{output_base}.xml")
                
                return {
                    "domain": domain,
                    "emails": data.get("emails", []),
                    "hosts": data.get("hosts", []),
                    "ips": data.get("ips", []),
                    "urls": data.get("urls", []),
                    "interesting_urls": data.get("interesting_urls", [])
                }
            else:
                return {
                    "domain": domain,
                    "error": "No output file generated",
                    "stdout": stdout.decode().strip(),
                    "stderr": stderr.decode().strip()
                }
        except Exception as e:
            return {"error": str(e), "domain": domain}

async def run_theharvester(domain: str, **kwargs) -> Dict[str, Any]:
    connector = TheHarvesterConnector()
    return await connector.scan(
        domain, 
        limit=kwargs.get("limit", 500), 
        source=kwargs.get("source", "all")
    )
