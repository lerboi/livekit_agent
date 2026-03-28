FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --production=false

COPY tsconfig.json ./
COPY src/ ./src/

RUN npm run build

RUN npm prune --production

CMD ["node", "dist/agent.js", "start"]
