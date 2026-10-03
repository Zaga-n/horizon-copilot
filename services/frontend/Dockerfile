FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS build
WORKDIR /frontend
COPY services/frontend/package.json services/frontend/package-lock.json ./
RUN npm ci
COPY services/frontend/ ./
RUN npm run build

FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de
RUN apk add --no-cache jq
COPY --from=build /frontend/dist /usr/share/nginx/html
COPY services/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY services/frontend/runtime-config.sh /docker-entrypoint.d/40-horizon-config.sh
RUN chmod +x /docker-entrypoint.d/40-horizon-config.sh
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1
