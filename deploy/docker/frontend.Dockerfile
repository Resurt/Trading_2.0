FROM node:24-alpine AS build

WORKDIR /app

COPY apps/frontend/package*.json ./
RUN npm ci

COPY apps/frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

COPY deploy/nginx/frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
