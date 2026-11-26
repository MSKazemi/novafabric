FROM scratch
ARG TARGETARCH
COPY etc /etc
COPY status.${TARGETARCH} /var/lib/dpkg/status
LABEL org.opencontainers.image.title=release-gate-mixed
