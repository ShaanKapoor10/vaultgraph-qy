# Brahmastra frontend — Next.js dashboard.
#
# The rewrite in next.config.mjs proxies /api/* to the backend, so BACKEND_URL
# must be readable AT RUNTIME, not baked in at build. That is why it is an ENV
# here and not a build ARG: the same image then runs against a local backend,
# a staging one, and production.

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
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000),r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
