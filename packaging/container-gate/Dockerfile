FROM alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce

LABEL io.local-agent.gate-protocol="signal-v1"

COPY isolation-gate /opt/local-agent/bin/isolation-gate
COPY isolation-gate-ready /opt/local-agent/bin/isolation-gate-ready
COPY isolation-gate-mount-proof /opt/local-agent/bin/isolation-gate-mount-proof
COPY isolation-gate-stage-proof /opt/local-agent/bin/isolation-gate-stage-proof
RUN chmod 0555 \
    /opt/local-agent/bin/isolation-gate \
    /opt/local-agent/bin/isolation-gate-ready \
    /opt/local-agent/bin/isolation-gate-mount-proof \
    /opt/local-agent/bin/isolation-gate-stage-proof

ENTRYPOINT ["/opt/local-agent/bin/isolation-gate"]
