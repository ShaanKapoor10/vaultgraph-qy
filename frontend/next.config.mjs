/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit .next/standalone: a self-contained server with only the node_modules
  // it actually imports. Without it the runtime image has to carry the full
  // dependency tree, which is most of the image size for a Next app.
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
  // In local dev (outside Vercel), proxy /api/* to the FastAPI backend.
  // On Vercel, experimentalServices handles the routing automatically.
  async rewrites() {
    const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8001"
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/:path*`,
      },
    ]
  },
}

export default nextConfig
