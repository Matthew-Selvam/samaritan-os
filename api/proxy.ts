/**
 * Serverless API proxy for Signal-OS
 * Routes all requests to the backend FastAPI server
 */

import { NextApiRequest, NextApiResponse } from "next";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8766";

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  const path = req.query.proxy as string[];
  const endpoint = "/" + path.join("/");

  try {
    const backendUrl = `${BACKEND_URL}${endpoint}`;

    const response = await fetch(backendUrl, {
      method: req.method,
      headers: {
        "Content-Type": "application/json",
        ...req.headers,
      },
      body: req.body ? JSON.stringify(req.body) : undefined,
    });

    const data = await response.json();

    res.status(response.status).json(data);
  } catch (error) {
    console.error("Proxy error:", error);
    res.status(500).json({
      error: "Backend service unavailable",
      message: error instanceof Error ? error.message : "Unknown error",
    });
  }
}
