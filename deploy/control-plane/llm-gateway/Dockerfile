# Prototype image: the herdr-engineering control-plane image plus a Linux inferweave binary.
# The binary must be a reviewed release build (for example the pinned Whiteale release);
# a macOS or unpinned build must not be used here.
ARG HERDR_IMAGE
FROM ${HERDR_IMAGE}
COPY --chmod=0755 inferweave /usr/local/bin/inferweave
