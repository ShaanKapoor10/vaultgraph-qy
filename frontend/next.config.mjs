/** @type {import('next').NextConfig} */
const nextConfig = {
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
