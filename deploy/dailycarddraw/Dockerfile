FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
WORKDIR /app
ENV NODE_ENV=production
COPY server/package.json server/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY server/src ./src
COPY server/public ./public
COPY server/resource.tar.gz ./resource.tar.gz
RUN tar -xzf resource.tar.gz && rm resource.tar.gz && mkdir -p cache/draw-images && chown -R node:node cache
USER node
EXPOSE 3100
CMD ["node", "src/server.js"]
