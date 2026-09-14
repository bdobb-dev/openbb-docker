# MinIO joined to the tailnet, serving S3 over a Tailscale-issued certificate.
#
# tailscaled runs in THIS container rather than a sidecar. Its control socket
# is a file, and network_mode shares only the network namespace -- so a sidecar
# daemon is unreachable from here no matter what is mounted. Co-locating the
# daemon, the CLI, and the process that must be SIGHUP'd on renewal makes all
# three share one filesystem and one lifecycle.
FROM tailscale/tailscale:v1.98.9 AS ts

FROM quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z

COPY --from=ts /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=ts /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY cert-sync.sh /usr/local/bin/cert-sync.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/cert-sync.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/data"]
