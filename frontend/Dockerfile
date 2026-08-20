# Brahmastra frontend — Next.js dashboard.
#
# The route handler in app/api/[...path] proxies /api/* to the backend, so
# BACKEND_URL must be readable AT RUNTIME, not baked in at build. That is why
# it is an ENV here and not a build ARG: the same image then runs against a
# local backend, a staging one, and production.
#
# The pnpm version is pinned by `packageManager` in package.json, and corepack
# honours it. Leaving it unpinned made this image depend on whatever pnpm was
# newest at build time: it broke without a single line changing here when pnpm
# 11 shipped, because that release requires Node >= 22.13 for `node:sqlite`
# while this base is Node 20. Bump the base image and the pin together.

FROM node:20-slim AS deps
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:20-slim AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# next build reads BACKEND_URL for the rewrite default; the runtime value below
# is what actually takes effect, this only has to be syntactically valid.
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    BACKEND_URL=http://backend:8001

RUN useradd --system --create-home --uid 10001 nextjs

# standalone carries its own minimal server.js plus the node_modules it needs.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

USER nextjs
EXPOSE 3000

# Next's standalone server binds to whatever HOSTNAME says, and Docker sets
# HOSTNAME to the container id. Left alone it therefore listened only on the
# container's own eth0 address: published traffic worked, so the dashboard
# looked fine, while the loopback healthcheck below got ECONNREFUSED and the
# container sat permanently unhealthy. Anything gating on that health -- a
# depends_on, a load balancer, a platform's deploy check -- would refuse to
# proceed against a server that was serving perfectly.
ENV HOSTNAME=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000),r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
