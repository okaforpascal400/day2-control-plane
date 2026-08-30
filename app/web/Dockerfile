# The dashboard is plain ES2020 + CSS: no bundler, so there is nothing to compile.
# The "build" stage exists only to assemble and verify the payload, keeping the
# runtime image to nginx plus static files.
#   docker build -f app/web/Dockerfile app/web
#
# Bases pinned by digest per CLAUDE.md rule 2 — tags move, digests do not.
# 1.31-alpine: the 1.27/1.29 lines still carry fixable HIGH CVEs in musl,
# zlib, and nghttp2 that the trivy gate rejects.
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.31-alpine@sha256:18d67281256ded39ff65e010ae4f831be18f19356f83c60bc546492c7eb6dd23

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

# CVE-2026-14456 (HIGH) — openssl: DoS via unbounded memory growth in the QUIC
# server. libssl3/libcrypto3 3.5.7-r0 ship in nginx-unprivileged:1.31-alpine;
# fixed in 3.5.8-r0. BRIDGE, not a fix: when a rebuilt base image carries
# openssl >= 3.5.8-r0 the apk constraint is already satisfied, this RUN becomes
# a no-op, and it should be removed together with a digest bump of NGINX_IMAGE.
USER root
RUN apk add --no-cache --upgrade libssl3=3.5.8-r0 libcrypto3=3.5.8-r0

# The base image already runs as this unprivileged uid on port 8080.
USER 101

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/healthz || exit 1
