/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_BACKEND_URL:
      process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8766",
  },
  rewrites: async () => {
    return {
      beforeFiles: [
        {
          source: "/api/:path*",
          destination: "http://localhost:8766/api/:path*",
        },
        {
          source: "/ws/:path*",
          destination: "http://localhost:8766/ws/:path*",
        },
      ],
    };
  },
};

module.exports = nextConfig;
