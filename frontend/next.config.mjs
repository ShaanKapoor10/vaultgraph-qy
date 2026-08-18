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
  // No /api rewrite here on purpose: it forwards requests untouched and
  // cannot attach an Authorization header, so it 401s the moment the API
  // requires a key. app/api/[...path]/route.ts proxies instead, keeping
  // BRAHMASTRA_API_KEY server-side.
}

export default nextConfig
