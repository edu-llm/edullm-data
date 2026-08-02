# Runtime image for the eduLLM data publisher and validator.
#
# The platform passes BASE_IMAGE as a digest-pinned build argument.  Keep this bare
# (no default) so the image cannot silently build from an unregistered base.
ARG BASE_IMAGE

FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/edullm-data

# Copy only the package inputs.  The command is deliberately left unspecified:
# Batch job definitions select either the ingress or validator entry point with
# their command override, while both use this same immutable image digest.
COPY pyproject.toml README.md ./
COPY src ./src
COPY families ./families

RUN python -m pip install --no-cache-dir . \
    && python -c "import boto3, numpy, edullm_data; assert edullm_data.__version__ == '0.7.3'"

# families/ must resolve from the INSTALLED package, not from the build context above.
# pyproject's force-include maps it to edullm_data/families, and corpus_pack REFUSES to start
# when it cannot find families/pretrain.json rather than falling back to the profile's laxer 0.5
# EOS bound — which is how the live corpus once came to be validated at 50% EOS while declaring
# 5% (CLAUDE.md gotcha 2). Asserted at BUILD time so a packaging regression fails the image
# instead of the job that uses it.
RUN python -c "\
from edullm_data.corpus_pack import _family_decode_bounds; \
b = _family_decode_bounds(); \
assert b[0] == 0.05, f'families/pretrain.json missing or wrong: {b}'; \
print('families OK', b)"

# artifacts/ is excluded by .dockerignore, so the 26 KB corpus registry is deliberately NOT in
# this image — `corpus_build` takes --registry and the job definition stages it from S3. The
# registry changes far more often than the code, so baking it in would mean a new image per pin
# correction, and would let a stale registry ride along inside an image that looks current.
