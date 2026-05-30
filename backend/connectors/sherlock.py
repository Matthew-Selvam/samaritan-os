import asyncio
import json
import os
import subprocess
from typing import Dict, Any

class SherlockConnector:
    """
    Connector for Sherlock CLI tool.
    Wraps the sherlock command to perform username lookups.
    """
    async def lookup(self, username: str) -> Dict[str, Any]:
        # Sherlock outputs to a file named {username}.txt by default
        # We can use --json to get a json file
        output_file = f"{username}.json"
        
        # Use subprocess to run sherlock
        # Command: sherlock {username} --json --output {output_file}
        cmd = ["sherlock", username, "--json", "--output", output_file]
        
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
                
                # Sherlock's JSON output is usually { "site": { "url_user": "...", "status": "..." }, ... }
                results = []
                for site, info in data.items():
                    if info.get("status") == "CLAIMED":
                        results.append({
                            "site": site,
                            "url": info.get("url_user"),
                        })
                
                return {
                    "username": username,
                    "results": results,
                    "count": len(results)
                }
            else:
                return {
                    "username": username,
                    "results": [],
                    "count": 0,
                    "error": stderr.decode().strip() if stderr else "No output file generated"
                }
        except Exception as e:
            return {"error": str(e), "username": username, "results": []}

async def run_sherlock(username: str, **kwargs) -> Dict[str, Any]:
    connector = SherlockConnector()
    return await connector.lookup(username)
