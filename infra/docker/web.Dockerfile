# The Next.js frontend, for Cloud Run.
#
# Built from the repo root, not from `apps/web`: this is a pnpm workspace, and `web`
# depends on `@jutsu/ui` by workspace link. A context rooted at the app would resolve that
# to nothing and the build would fail on an import that works locally.
#
# Three stages so the runtime image carries neither the package manager nor the source.
# `output: "standalone"` in next.config.ts is what makes that possible — it emits a server
# with only the files actually reached, instead of expecting the whole node_modules tree.

# ---------------------------------------------------------------- dependencies
FROM node:22-alpine AS deps
WORKDIR /repo

RUN corepack enable

# Only the manifests, so this layer is reused for every build that does not change a
# dependency — which is almost all of them.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY packages/ui/package.json packages/ui/

RUN pnpm install --frozen-lockfile

# ---------------------------------------------------------------- build
FROM node:22-alpine AS build
WORKDIR /repo

RUN corepack enable

COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/web/node_modules ./apps/web/node_modules
COPY . .

# Baked into the bundle at build time, not read at runtime: `NEXT_PUBLIC_*` values are
# inlined by the compiler, so a Cloud Run environment variable set later would be
# ignored and the canonical URLs would quietly point at localhost.
ARG NEXT_PUBLIC_SITE_URL
ENV NEXT_PUBLIC_SITE_URL=${NEXT_PUBLIC_SITE_URL}
ENV NEXT_TELEMETRY_DISABLED=1

RUN pnpm --filter web build

# ---------------------------------------------------------------- runtime
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Not root. Cloud Run does not require it, which is exactly why it is easy to skip.
RUN addgroup -g 1001 -S nodejs && adduser -S -u 1001 -G nodejs nextjs

# `standalone` already contains the pruned node_modules and the server entrypoint.
# `static` and `public` are deliberately separate: Next does not copy them in, and the
# symptom of forgetting is a page that renders with no CSS and no images.
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/public ./apps/web/public

USER nextjs

# Cloud Run injects PORT and it is not always 8080. Binding 0.0.0.0 matters too: the
# default localhost bind is unreachable from outside the container, and the failure looks
# like a container that started fine and answers nothing.
ENV PORT=8080
ENV HOSTNAME=0.0.0.0
EXPOSE 8080

CMD ["node", "apps/web/server.js"]
