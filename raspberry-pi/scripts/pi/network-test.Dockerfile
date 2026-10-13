FROM debian:bookworm-20261005@sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858
RUN apt-get update && apt-get install -y --no-install-recommends network-manager dnsmasq-base nftables openssh-server sudo openssl iproute2 iputils-ping curl python3 shellcheck && rm -rf /var/lib/apt/lists/*
