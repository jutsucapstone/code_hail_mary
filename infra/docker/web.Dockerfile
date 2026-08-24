# The Next.js frontend, for Cloud Run.
#
# Built from the repo root, not from `apps/web`: this is a pnpm workspace, and `web`
# depends on `@jutsu/ui` by workspace link. A context rooted at the app would resolve that
# to nothing and the build would fail on an import that works locally.
#
# Three stages so the runtime image carries neither the package manager nor the source.
#
# The image is ~760MB, and most of that is `next` plus its platform swc binary. Neither
# is safe to remove by hand — next resolves the binary lazily, so stripping it fails at
# runtime rather than at build. `shadcn` was moved to devDependencies for this reason
# (it is a codegen CLI that dragged typescript and ts-morph in, ~100MB), and that is the
# honest limit of what could be trimmed without guessing.

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

# A self-contained tree for the runtime stage: real directories, no store symlinks.
RUN pnpm --filter web deploy --prod --legacy /deploy

# ---------------------------------------------------------------- runtime
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Not root. Cloud Run does not require it, which is exactly why it is easy to skip.
RUN addgroup -g 1001 -S nodejs && adduser -S -u 1001 -G nodejs nextjs

# The standalone server, plus a real node_modules produced by `pnpm deploy`.
#
# Standalone alone is not enough in a pnpm workspace. Its file tracer copies the *real*
# files out of the `.pnpm` store but not the symlinks that point at them, so a package
# like @swc/helpers ends up present at its store path and missing from the path `next`
# actually resolves — leaving an empty `@swc/` directory and a container that dies at
# startup on MODULE_NOT_FOUND. `next build` reports success either way: the files are
# missing from the output, not from the build. Found by running the image.
#
# Fixing the one package would be whack-a-mole — any other symlinked transitive fails the
# same way, later, in production. `pnpm deploy` is the supported answer: it resolves the
# workspace into a self-contained tree with no symlinks to lose.
#
# `static` and `public` are copied separately because Next does not place them in
# standalone, and the symptom of forgetting is a page that renders with no CSS.
COPY --from=build --chown=nextjs:nodejs /deploy/node_modules ./node_modules
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/.next/standalone/apps/web ./apps/web
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
