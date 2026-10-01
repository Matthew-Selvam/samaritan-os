import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * Pin the workspace root to this app.
   *
   * Without it Next infers the root from the nearest lockfile above
   * `frontend/`, which pulls an unrelated `/Users/matthewselvam/package-lock.json`
   * into the build and emits a warning on every run.
   */
  turbopack: {
    root: import.meta.dirname,
  },
};

export default nextConfig;