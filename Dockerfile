# syntax=docker/dockerfile:1
#
# Pumpkin Server Manager: web dashboard + supervisor for Pumpkin (Rust) Minecraft servers.
#
# Alpine matches what the Pumpkin project itself uses, and the statically linked "-Linux-musl" Pumpkin
# binaries the manager downloads at runtime run on it without extra libraries.
FROM python:3.12-alpine

ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Pumpkin Server Manager" \
      org.opencontainers.image.description="Create, run, back up and auto-update Pumpkin Minecraft servers from a web dashboard" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

# tini    - PID 1 that reaps zombies and forwards SIGTERM, so "docker stop" shuts servers down cleanly
# su-exec - drops privileges to PUID:PGID after the entrypoint has prepared the folders
# tzdata  - makes the TZ variable work (the daily backup time is interpreted in this time zone)
# Modpack servers run on a Java that the manager downloads when it is needed (Temurin builds for Alpine, and Azul's Zulu 8u312 for
# Java 8, which Temurin never built for Alpine), so Java itself is not in the image. What it needs around it is: zlib and libstdc++ for the JVM, fontconfig and a font for the few mods that use
# AWT, and gcompat so that mods with native libraries built for glibc (zstd, opus, ...) can still load.
RUN apk add --no-cache tini su-exec tzdata ca-certificates zlib libstdc++ gcompat fontconfig ttf-dejavu

WORKDIR /app

# Every dependency is pinned and has a prebuilt musllinux wheel for amd64 and arm64: never compile in the image.
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY pumpkin_manager ./pumpkin_manager
# Self-checks you can run inside the container: python /app/scripts/real_binary_check.py --download
# and, for modpack servers (real Java, Mojang, Modrinth and loaders), python /app/scripts/real_java_check.py --accept-eula
COPY scripts/real_binary_check.py ./scripts/real_binary_check.py
COPY scripts/real_java_check.py ./scripts/real_java_check.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh \
 && python -m compileall -q pumpkin_manager

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    WEB_PORT=8420 \
    PUID=99 \
    PGID=100

# Web dashboard, Java game ports, Bedrock (UDP). The ranges are defaults; see PORT_RANGE / BEDROCK_PORT_RANGE.
EXPOSE 8420
EXPOSE 25565-25575/tcp
EXPOSE 19132-19142/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${WEB_PORT}/api/health" || exit 1

STOPSIGNAL SIGTERM
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "pumpkin_manager"]
