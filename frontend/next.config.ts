import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return process.env.NODE_ENV === "development"
      ? [{ source: "/api/:path*", destination: `${process.env.DEVFLOW_API_ORIGIN ?? "http://127.0.0.1:8000"}/api/:path*` }]
      : [];
  },
};

export default nextConfig;
