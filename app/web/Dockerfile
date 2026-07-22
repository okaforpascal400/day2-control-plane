# The dashboard is plain ES2020 + CSS: no bundler, so there is nothing to compile.
# The "build" stage exists only to assemble and verify the payload, keeping the
# runtime image to nginx plus static files.
#   docker build -f app/web/Dockerfile app/web
#
# Bases pinned by digest per CLAUDE.md rule 2 — tags move, digests do not.
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0

FROM ${NGINX_IMAGE} AS build

USER root
COPY static/ /payload/
# Fail the build rather than ship a half-copied dashboard.
RUN test -s /payload/index.html && test -s /payload/app.js && test -s /payload/styles.css


FROM ${NGINX_IMAGE} AS runtime

# Upstream the /api/ location proxies to; overridden per environment by Helm.
ENV API_UPSTREAM=http://day2-api:8000

COPY --from=build /payload/ /usr/share/nginx/html/
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

# The base image already runs as this unprivileged uid on port 8080.
USER 101

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/healthz || exit 1
