# =============================================================================
# ASTERIX REFERENCE DECODER — GPL v2, SEPARATE IMAGE, SEPARATE BUILD CONTEXT
# =============================================================================
# This image exists to VERIFY the MIT decode path against an independent,
# field-proven implementation. Two decoders sharing no upstream is the
# two-independent-sources epistemics this project already relies on: a
# generator checked by its own parser proves nothing, and a decoder checked
# only by the definitions it reads proves little more.
#
# LICENCE BOUNDARY — the reason this is a separate image and not a dependency.
# asterix-decoder (CroatiaControl python-asterix) is GPL v2 and a C extension.
# OpenDDIL is MIT. Importing it into an MIT service would raise combined-work
# questions; running it as its own process that speaks JSON over a topic is
# AGGREGATION, not linking (ADR-0030 §Decoder choice is a licensing decision).
#
# So the boundary is load-bearing and must stay intact:
#   * separate image, separate build context — nothing here is copied into
#     the MIT sensor-ingest image;
#   * the wrapper is thin glue and carries NO OpenDDIL internals — it emits
#     plain JSON against the same ingress-asterix-raw contract;
#   * the wrapper source and the GPL text ship INSIDE the container, so a
#     recipient of the binary has the source of the GPL-covered work;
#   * opt-in profile only. Never a default.
# =============================================================================
FROM python:3.11-slim AS build

# The C extension needs a compiler and Python headers — which is precisely
# why this belongs in a container rather than on a developer host.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --target=/pkg asterix-decoder==0.7.11

FROM python:3.11-slim
LABEL org.opencontainers.image.licenses="GPL-2.0-only"
LABEL org.opencontainers.image.description="ASTERIX reference decoder (GPL). Verification and benchmarking only; not a default component."

# libexpat is a RUNTIME dependency of the C extension (the decoder parses its
# category XML with it). Present in the build stage via build-essential, absent
# from the slim runtime — so the import fails at start with a bare
# "libexpat.so.1: cannot open shared object file", which names a library
# rather than the cause. Installed explicitly here so the failure cannot
# recur silently on a base-image bump.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1     && rm -rf /var/lib/apt/lists/*

COPY --from=build /pkg /pkg
ENV PYTHONPATH=/pkg

# GPL obligation: the licence text and the wrapper source travel with the
# binary. `decode_stdin.py` is the only thing we wrote here, and it is
# deliberately small enough to read in one sitting.
COPY decode_stdin.py /app/decode_stdin.py
RUN cp /pkg/asterix/LICENSE /app/LICENSE.asterix-decoder 2>/dev/null || true

WORKDIR /app
ENTRYPOINT ["python", "/app/decode_stdin.py"]
