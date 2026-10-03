ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    ca-certificates curl python3 qemu-system-x86 qemu-utils cloud-image-utils openssh-client \
    && rm -rf /var/lib/apt/lists/*
