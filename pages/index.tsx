/**
 * Signal-OS Web Interface
 * Main dashboard for intelligence investigations
 */

import React, { useState, useEffect } from "react";
import {
  Container,
  TextField,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Typography,
  Box,
  Grid,
  Chip,
  Divider,
  Alert,
} from "@mui/material";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8766";

interface Investigation {
  inv_id: string;
  case_id: string;
  status: "queued" | "running" | "done" | "error";
  input: string;
  input_type: string;
  report: {
    summary: string;
    agents_activated: string[];
    entities: any[];
    signals: any[];
    graph: { nodes: any[]; edges: any[] };
    timeline: any[];
    markdown: string;
  };
  error?: string;
}

export default function Home() {
  const [input, setInput] = useState("");
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;

    setLoading(true);
    setError(null);

    try {
      // Submit investigation
      const submitRes = await fetch(`${BACKEND_URL}/api/investigate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: input.trim() }),
      });

      if (!submitRes.ok) throw new Error("Failed to submit investigation");

      const { inv_id } = await submitRes.json();

      // Poll for results
      let result = null;
      for (let i = 0; i < 60; i++) {
        await new Promise((resolve) => setTimeout(resolve, 1000));

        const getRes = await fetch(`${BACKEND_URL}/api/investigate/${inv_id}`);
        if (!getRes.ok) continue;

        result = await getRes.json();
        if (result.status === "done" || result.status === "error") break;
      }

      setInvestigation(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Container maxWidth="lg" sx={{ py: 4 }}>
      {/* Header */}
      <Box sx={{ mb: 4, textAlign: "center" }}>
        <Typography variant="h3" gutterBottom sx={{ fontWeight: "bold" }}>
          🔍 Signal-OS
        </Typography>
        <Typography variant="subtitle1" color="textSecondary">
          AI-Native Multimodal Intelligence Fusion Platform
        </Typography>
      </Box>

      {/* Input Section */}
      <Card sx={{ mb: 4 }}>
        <CardContent>
          <form onSubmit={handleSubmit}>
            <TextField
              fullWidth
              multiline
              rows={3}
              label="Enter target (username, email, phone, URL, domain, IP, etc.)"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="+1 202-456-1111 or john_doe or test@example.com"
              disabled={loading}
              sx={{ mb: 2 }}
            />
            <Button
              type="submit"
              variant="contained"
              size="large"
              disabled={loading || !input.trim()}
              fullWidth
            >
              {loading ? (
                <>
                  <CircularProgress size={20} sx={{ mr: 1 }} />
                  Investigating...
                </>
              ) : (
                "Start Investigation"
              )}
            </Button>
          </form>
        </CardContent>
      </Card>

      {error && <Alert severity="error">{error}</Alert>}

      {/* Results Section */}
      {investigation && (
        <Box>
          {/* Summary */}
          <Card sx={{ mb: 3 }}>
            <CardContent>
              <Typography variant="h5" gutterBottom>
                Investigation Results
              </Typography>
              <Divider sx={{ my: 2 }} />

              <Grid container spacing={2} sx={{ mb: 2 }}>
                <Grid item xs={12} sm={6}>
                  <Typography variant="body2" color="textSecondary">
                    Input Type
                  </Typography>
                  <Typography variant="body1">
                    {investigation.input_type}
                  </Typography>
                </Grid>
                <Grid item xs={12} sm={6}>
                  <Typography variant="body2" color="textSecondary">
                    Status
                  </Typography>
                  <Chip
                    label={investigation.status}
                    color={
                      investigation.status === "done" ? "success" : "warning"
                    }
                  />
                </Grid>
              </Grid>

              {investigation.report?.summary && (
                <Alert severity="info" sx={{ mb: 2 }}>
                  {investigation.report.summary}
                </Alert>
              )}
            </CardContent>
          </Card>

          {/* Agents Activated */}
          {investigation.report?.agents_activated && (
            <Card sx={{ mb: 3 }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Agents Activated ({investigation.report.agents_activated.length})
                </Typography>
                <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
                  {investigation.report.agents_activated.map((agent) => (
                    <Chip key={agent} label={agent} variant="outlined" />
                  ))}
                </Box>
              </CardContent>
            </Card>
          )}

          {/* Signals & Entities */}
          <Grid container spacing={3} sx={{ mb: 3 }}>
            <Grid item xs={12} sm={6}>
              <Card>
                <CardContent>
                  <Typography variant="h6">
                    Signals ({investigation.report?.signals?.length || 0})
                  </Typography>
                  <Divider sx={{ my: 1 }} />
                  {investigation.report?.signals?.slice(0, 10).map(
                    (signal, i) => (
                      <Typography key={i} variant="body2" sx={{ mb: 0.5 }}>
                        • {signal.type}
                      </Typography>
                    )
                  )}
                </CardContent>
              </Card>
            </Grid>

            <Grid item xs={12} sm={6}>
              <Card>
                <CardContent>
                  <Typography variant="h6">
                    Entities ({investigation.report?.entities?.length || 0})
                  </Typography>
                  <Divider sx={{ my: 1 }} />
                  {investigation.report?.entities?.slice(0, 10).map(
                    (entity, i) => (
                      <Typography key={i} variant="body2" sx={{ mb: 0.5 }}>
                        • {entity.label} ({entity.type})
                      </Typography>
                    )
                  )}
                </CardContent>
              </Card>
            </Grid>
          </Grid>

          {/* Graph Stats */}
          {investigation.report?.graph?.nodes && (
            <Card sx={{ mb: 3 }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Correlation Graph
                </Typography>
                <Grid container spacing={2}>
                  <Grid item xs={12} sm={6}>
                    <Typography variant="body2" color="textSecondary">
                      Nodes
                    </Typography>
                    <Typography variant="h6">
                      {investigation.report.graph.nodes.length}
                    </Typography>
                  </Grid>
                  <Grid item xs={12} sm={6}>
                    <Typography variant="body2" color="textSecondary">
                      Edges
                    </Typography>
                    <Typography variant="h6">
                      {investigation.report.graph.edges.length}
                    </Typography>
                  </Grid>
                </Grid>
              </CardContent>
            </Card>
          )}

          {/* Timeline */}
          {investigation.report?.timeline?.length > 0 && (
            <Card sx={{ mb: 3 }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Timeline ({investigation.report.timeline.length} events)
                </Typography>
                <Divider sx={{ my: 1 }} />
                {investigation.report.timeline.slice(0, 5).map((event, i) => (
                  <Box key={i} sx={{ mb: 1 }}>
                    <Typography variant="body2" color="textSecondary">
                      {event.timestamp?.slice(0, 10)}
                    </Typography>
                    <Typography variant="body2">{event.label}</Typography>
                  </Box>
                ))}
              </CardContent>
            </Card>
          )}

          {/* Full Report */}
          {investigation.report?.markdown && (
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Full Intelligence Report
                </Typography>
                <Divider sx={{ my: 2 }} />
                <Typography
                  variant="body2"
                  component="pre"
                  sx={{
                    whiteSpace: "pre-wrap",
                    wordWrap: "break-word",
                    fontFamily: "monospace",
                    fontSize: "0.85rem",
                    maxHeight: "400px",
                    overflow: "auto",
                  }}
                >
                  {investigation.report.markdown}
                </Typography>
              </CardContent>
            </Card>
          )}

          {/* Error Display */}
          {investigation.status === "error" && investigation.error && (
            <Alert severity="error" sx={{ mt: 3 }}>
              Investigation error: {investigation.error}
            </Alert>
          )}
        </Box>
      )}

      {/* Footer */}
      <Box sx={{ mt: 8, py: 4, borderTop: "1px solid #eee", textAlign: "center" }}>
        <Typography variant="body2" color="textSecondary">
          Signal-OS • Authorized OSINT Investigation Platform
        </Typography>
        <Typography variant="caption" color="textSecondary">
          Use only with proper authorization and consent
        </Typography>
      </Box>
    </Container>
  );
}
