FROM node:24-alpine AS build

WORKDIR /app

COPY apps/frontend/package*.json ./
RUN npm ci

COPY apps/frontend/ ./
ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_WS_BASE_URL=ws://localhost:8000
ARG VITE_TRADING_RUNTIME_MODE=historical_replay
ARG VITE_API_AUTH_MODE=dev
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_WS_BASE_URL=$VITE_WS_BASE_URL
ENV VITE_TRADING_RUNTIME_MODE=$VITE_TRADING_RUNTIME_MODE
ENV VITE_API_AUTH_MODE=$VITE_API_AUTH_MODE
RUN npm run build

FROM nginx:1.27-alpine

COPY deploy/nginx/frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
