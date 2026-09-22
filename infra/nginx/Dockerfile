# Build from infra/nginx: docker build -f infra/nginx/Dockerfile infra/nginx
# (a plain `nginx:1.27-alpine` image, plus openssl for the TLS-bootstrap
# entrypoint and the config/entrypoint baked in — every other service in
# this repo builds a custom image rather than bind-mounting config into a
# stock one in production; this keeps the reverse proxy consistent with
# that, while dev doesn't use this service at all (see docker-compose.dev.yml).
FROM nginx:1.27-alpine

RUN apk add --no-cache openssl

COPY nginx.conf /etc/nginx/nginx.conf
COPY docker-entrypoint.sh /usr/local/bin/yonixalpha-entrypoint.sh
RUN chmod +x /usr/local/bin/yonixalpha-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/yonixalpha-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
