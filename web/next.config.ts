import type { NextConfig } from "next";

// Static export served by the FastAPI process from the same origin (§4:
// no CORS, one deploy target). Build output lands in web/out.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
};

export default nextConfig;
