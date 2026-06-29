# --- deps ---
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund

# --- build ---
FROM node:20-slim AS build
WORKDIR /app
ENV NEXT_OUTPUT=standalone
# NEXT_PUBLIC_* is baked into the client bundle at build time, so the API URL
# must be supplied as a build arg (defaults to localhost for compose).
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- run (Next.js standalone output) ---
FROM node:20-slim AS run
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
