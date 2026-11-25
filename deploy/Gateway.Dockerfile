FROM node:22-bookworm-slim
WORKDIR /app
COPY web/package*.json ./
RUN npm ci
COPY web ./
ARG VITE_SEREIN_COMMIT
ARG VITE_SEREIN_REPOSITORY
ARG VITE_SEREIN_BRANCH
RUN npm run build
CMD ["node", "server/gateway.mjs"]
